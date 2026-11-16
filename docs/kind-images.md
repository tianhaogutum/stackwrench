# stackwrench kind-images

`stackwrench kind-images` (formerly `kind-image-plan`) is an offline command that
joins **rendered Kubernetes image intent** with **per-node `crictl images -o json`
snapshots**. It explains why an image already loaded into kind may still request
the registry, and where a required local image is missing. It prints proposed
`kind load docker-image` commands as **argv JSON**, without executing them or
changing manifests. It can also compare two explicit inventories without workload
manifests or load proposals.

## Install

Requires Python 3.12 or newer. The only runtime dependency is PyYAML (installed
with stackwrench). No Docker, kind, kubectl, credentials, or kubeconfig is needed;
all analysis runs offline.

```sh
pipx install stackwrench          # or: pip install stackwrench
stackwrench kind-images --version # prints the bare stackwrench version, e.g. 0.1.0

# Or run without installing:
uvx stackwrench kind-images --help
```

`python -m stackwrench kind-images` is equivalent to `stackwrench kind-images`.
There are no subcommands. The examples below use files from a stackwrench source
checkout; run them from its root:

```sh
stackwrench kind-images --manifests examples/kind_images/workloads.yaml --inventory examples/kind_images/nodes.json --json
stackwrench kind-images --manifests examples/kind_images/workloads.yaml --inventory examples/kind_images/nodes.json
stackwrench kind-images --manifests examples/kind_images/ready.yaml --inventory examples/kind_images/nodes.json --json --check
```

The first example is deliberately **not ready**. Its eight containers produce
16 node checks: 9 `local-available`, 2 `will-request-registry`,
2 `missing-required-local`, and 3 `would-pull-if-scheduled`. The custom
`DemoRollout` is explicitly unassessed. The report proposes three deduplicated
loads, each targeting only `demo-worker-b`. Example argv:

```json
["kind", "load", "docker-image", "docker.io/library/demo:latest", "--name", "demo", "--nodes", "demo-worker-b"]
```

This is data for review. The host image's existence/content, the cluster name and
node names must be independently verified before someone elects to run a proposal.
Loading `demo:latest` **does not fix its default `Always` policy**. The CLI never
changes that policy. A digest with missing evidence receives a `loadPlanOmissions`
entry, not a speculative tag load command. Nothing in these examples builds,
pulls, starts, loads, or deploys an image.

Multiple manifest files are accepted in order:

```sh
stackwrench kind-images --manifests examples/kind_images/ready.yaml examples/kind_images/workloads.yaml --inventory examples/kind_images/nodes.json --json
```

Exit status is **0** for a valid report, even with blockers/unassessed objects;
**2** for invalid input or CLI usage. Add `--check` to return **1** when any node
check is not `local-available`, anything is unassessed, or there are no assessed
containers. `summary.localUsePossibleEverywhere` describes only this static image
check, never overall deployment readiness.

Partial inventories are also usable:

```sh
stackwrench kind-images --manifests examples/kind_images/partial-workloads.yaml --inventory examples/kind_images/partial-nodes.json --json
stackwrench kind-images --manifests examples/kind_images/partial-workloads.yaml --inventory examples/kind_images/partial-nodes.json
```

This synthetic fixture repeats the same canonical image under `Always`,
`IfNotPresent`, and `Never` across three nodes. Worker-a has it; worker-b's
captured image list is empty; worker-c is declared schedulable but omits its
snapshot. Each container reports availability as `present`, `missing`, and
`unassessed`, respectively. The `Always` risk is reported separately on **all
three nodes**, including the uncaptured node. The single deduplicated load argv
targets only `demo-worker-b`; there is no evidence to propose a load for
worker-c. The custom workload remains unassessed. There are 9 node checks:
2 `local-available`, 2 `will-request-registry`, 1 `missing-required-local`,
1 `would-pull-if-scheduled`, and 3 `unassessed`. Adding `--check` exits **1**.
The report never treats omitted captures as empty snapshots or a successful
local-use check, and loading never resolves an `Always` requirement.

These complete failure examples assert their expected status:

```sh
python - <<'PY'
import subprocess
cases = [
    (["stackwrench", "kind-images", "--manifests", "examples/kind_images/workloads.yaml",
      "--inventory", "examples/kind_images/nodes.json", "--json", "--check"], 1),
    (["stackwrench", "kind-images", "--manifests", "examples/kind_images/invalid-policy.yaml",
      "--inventory", "examples/kind_images/nodes.json", "--json"], 2),
    (["stackwrench", "kind-images", "--manifests", "examples/kind_images/ready.yaml"], 2),
]
for argv, expected in cases:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == expected, (argv, result.returncode, result.stderr)
    print(f"expected exit {expected}: {' '.join(argv)}")
PY
```

## Inventory and snapshot contract

`examples/kind_images/nodes.json` documents the version-1 inventory:

```json
{
  "schemaVersion": 1,
  "cluster": "demo",
  "nodes": {
    "demo-worker-a": {"schedulable": true, "snapshot": "snapshots/worker-a.json"},
    "demo-worker-b": {"schedulable": true, "snapshot": "snapshots/worker-b.json"},
    "demo-control-plane": {"schedulable": false}
  }
}
```

The caller supplies captures already obtained independently; the tool does not
capture them. **Inventory is the trust boundary.** Include every schedulable
node, with complete, sufficiently fresh, unfiltered CRI image lists from the
named cluster and the relevant default runtime handler. Missing nodes cannot be
discovered. A `schedulable` boolean is mandatory for every node; at least one
must be true. If a schedulable node has no capture yet, omit its `snapshot` key:
it remains in the matrix with unknown availability and is listed once in
`unassessedNodes`. Even an inventory with no captures can produce a report,
but it cannot pass `--check`. A node omitted from `nodes` entirely cannot be
detected; declare it without a snapshot to make the gap visible.
False nodes are listed in `excludedNodes` and may omit `snapshot`.
If any node includes a snapshot, it is validated: null/empty/non-string paths,
unreadable files, and malformed captures remain input errors (exit 2, no partial
report), even alongside omitted captures. An explicit `{"images": []}` is
captured absence evidence, distinct from an omitted capture. Paths resolve relative
to the inventory file, not the current directory; absolute local paths also work.
Cluster/node names are restricted to lowercase letters/digits, dots and hyphens,
with an alphanumeric first/last character (maximum 253 characters).
Unknown inventory fields are errors, including misspellings.

Snapshots use the actual CRI camelCase JSON field names:

```json
{
  "images": [
    {
      "id": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      "repoTags": ["docker.io/library/demo:latest"],
      "repoDigests": ["docker.io/library/demo@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
      "size": "123456",
      "uid": null,
      "username": "",
      "spec": null,
      "pinned": false
    }
  ]
}
```

`images` must be an array (an empty array is valid). Every image requires a
nonempty string `id`, but it is **never used as evidence of a repository digest**.
`repoTags` must contain explicit tag references; `repoDigests` must contain
`repository@digest` references without a tag. Omitted/null reference arrays
mean empty, as permitted by protobuf JSON. `id`, `size`, `uid`, `username`, `spec`
and `pinned` are recognized metadata only; except for `id`, their internal types
are not validated and their values do not participate in matching. Unknown
snapshot top-level/image fields fail explicitly, including snake_case aliases;
a future CRI schema may therefore need an adapter. This is a focused format
reader, not a complete CRI or Kubernetes schema validator.

All example names, IDs, digests and snapshots are synthetic. In particular,
worker-b's ID matching the requested digest is intentional negative evidence:
without the matching `repoDigests` entry, digest presence cannot be established.

### Review current capture evidence

Every report includes `currentInventoryEvidence`, even without a baseline.
It contains `inventory` (the supplied inventory path), `nodes` (all declared
nodes sorted by name, including excluded nodes), and `evidenceNote`. Each node
retains `name`, `schedulable`, `snapshot`, `capturedAt`, `source`, and
`missingEvidence`, using the same fields as `inventoryDiff.after`.
Absent snapshot/time/source values are JSON `null`; `missingEvidence` lists
the absent keys in that order. A captured empty image list has a snapshot;
it is distinct from an omitted capture.

```sh
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/provenance-nodes.json --json --check
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/provenance-nodes.json
```

Node-a retains `capturedAt: "2026-11-11T22:15:00.123456+01:00"` and the source
label `synthetic fixture / record disagreement` verbatim. Node-b has a capture
but `missingEvidence: ["capturedAt", "source"]`. Node-unknown has no capture
and lists all three missing keys. Node-excluded retains its supplied metadata
but stays outside workload checks and tag comparisons. Text prints a separate
`Current inventory evidence (caller-supplied)` section, with quoted present
strings, `MISSING` for absent values, and each node's `missingEvidence` list.
A literal source label `"MISSING"` remains distinguishable from absent metadata.

Metadata is optional and requires a snapshot when supplied; timestamp/source
validation is the same with or without a baseline (see the format below).
Source labels are never fetched or executed. Timestamps are not compared with
the clock or used to certify freshness, completeness, cluster identity or
chronological order. Missing metadata alone does not fail `--check`.
This example exits **1** with `--check` because node-unknown has no capture;
the other valid JSON/text invocations exit **0**. No load is inferred for the
unknown or excluded node. The equal unions and differing node-a record sets
remain visible independently. With a baseline, current evidence matches
`inventoryDiff.after`'s inventory/nodes; inventory-only reports include evidence
but still infer no workload readiness or loads. All existing policies, exact
digest checks, comparisons, load plans and exit rules remain unchanged.

## Supported intent and matching

Only these stable API/kind combinations are assessed, including every
`containers` and `initContainers` entry:

| API / kind | PodSpec path |
| --- | --- |
| `v1` Pod | `spec` |
| `apps/v1` Deployment, StatefulSet, DaemonSet | `spec.template.spec` |
| `batch/v1` Job | `spec.template.spec` |
| `batch/v1` CronJob | `spec.jobTemplate.spec.template.spec` |

Use already-rendered UTF-8 multi-document YAML (JSON objects are also valid
YAML). Empty documents are skipped. `metadata.name` or `metadata.generateName`
is required for assessed workloads; generated names appear as `prefix*`.
Unknown kinds/API versions, including ConfigMaps, Secrets, CRDs, legacy APIs,
and `List` wrappers, are explicitly reported as `unassessed`; a List's children
are not traversed. Split wrappers into individual documents first. Supported
workloads with malformed pod-template paths, missing/invalid images, empty
container lists, duplicate container names, or invalid policies are errors.
Ephemeral containers, image volumes and runtime-class-specific availability
are explicitly unassessed if present. Ordinary Kubernetes fields such as
commands, selectors, labels, replicas, resource requests and secrets are outside
this image join; they are not executed, rewritten, or fully schema validated.

Literal image reference rules:

- Lowercase Docker-style repository paths; case-sensitive Docker tags of up to
  128 characters. DNS/IPv4-style registries and numeric ports 1–65535 are accepted;
  IPv6 registry literals and nonstandard name forms are rejected.
- Missing registry means `docker.io`; single-component Docker Hub repositories
  gain `library/`. `index.docker.io` aliases `docker.io`. Registry host case is
  normalized; tag case is retained. Other registries, explicit ports and repository
  namespaces remain distinct, including `registry-1.docker.io`.
- Missing tag means `latest` for tag matching. Only lowercase `sha256` (64 hex)
  and `sha512` (128 hex) digests are accepted. For `name:tag@digest`, the digest
  takes precedence, and only the normalized `name@digest` is joined.
- Tag presence requires the exact normalized reference in `repoTags`. Digest
  presence requires the exact normalized repository **and digest** in
  `repoDigests`. No inference through IDs, shared image records, similar tags,
  different repository names, registry resolution, or manifest-list digests.

[The Kubernetes image documentation](https://kubernetes.io/docs/concepts/containers/images/)
defines default-on-create behavior: digest references default to `IfNotPresent`;
otherwise omitted/`latest` tags default to `Always`, and other tags to
`IfNotPresent`. Explicit `Always`, `IfNotPresent`, and `Never` are honored;
null/empty policies are rejected in this input contract. Existing objects keep
their previously defaulted policy, so rendered intent without a persisted policy
cannot predict a live object's historical default. Admission can also change it.

| Effective policy | Snapshot match | Verdict |
| --- | --- | --- |
| `Always` | either | `will-request-registry` |
| `IfNotPresent` | yes | `local-available` |
| `IfNotPresent` | no | `would-pull-if-scheduled` |
| `Never` | yes | `local-available` |
| `Never` | no | `missing-required-local` |

Each container includes its source file, 1-based YAML document, exact template
path, original/canonical reference, policy provenance, and every supplied
schedulable node. Successful matches retain snapshot image-array index and raw
reference. JSON reports now use **report `schemaVersion: 2`** (inventory stays
version 1). Each matrix row separates `availability` and `availabilityReason`
from `policyRisk` and the combined `verdict`:

- `availability` is `present`, `missing`, or `unassessed`; corresponding `present`
  values are `true`, `false`, or **null**, respectively. A null snapshot is also
  emitted in the report's `nodes` entry for an omitted capture.
- `policyRisk` is the unconditional `Always` warning, independent of capture
  coverage or matching. Null means no unconditional `Always` warning; it does
  **not** promise that `IfNotPresent` will avoid a pull.
- Without a capture the verdict is `unassessed`, even for `Always`; its known
  registry risk remains in `policyRisk`. No such node enters `loadPlan` or the
  digest-specific `loadPlanOmissions` missing-node lists.

Version-1 report consumers must handle the new nullable `present`/`snapshot`
values and `unassessed` verdict before accepting version 2. The summary's
`nodeChecks` and `verdicts` include uncaptured container/node pairs;
`summary.unassessed` continues to count workload/scope entries in `unassessed`,
while `unassessedNodes` lists inventory coverage gaps independently, even if
there are no supported containers. Any gap prevents `--check` success.

`present: false` means **not established from this capture**, not an independent
runtime observation. A stale or filtered snapshot invalidates that conclusion.
Tag matches establish name availability, not identical image content.

## Review each requested image once

Every report includes an additive `imageOverview` array in report schema 2.
The default text report groups by image too. Inputs and exit statuses are unchanged:

```sh
stackwrench kind-images --manifests examples/kind_images/partial-workloads.yaml --inventory examples/kind_images/partial-nodes.json --json
stackwrench kind-images --manifests examples/kind_images/partial-workloads.yaml --inventory examples/kind_images/partial-nodes.json
```

For these inputs the overview has **one** `docker.io/library/demo:latest` group,
with availability `present` on worker-a, `missing` on worker-b and `unassessed`
on worker-c. Its three uses retain `Always`, `IfNotPresent` and `Never`, with
paths `spec.containers[0]`, `[1]` and `[2]`. On worker-b their verdicts remain
`will-request-registry`, `would-pull-if-scheduled` and `missing-required-local`.
The Always use retains its registry warning even on uncaptured worker-c.
The custom `DemoRollout` stays in `unassessed`, outside the overview.

The output contract is:

- Groups sort lexically by canonical `image` and carry `referenceType` (`tag`
  or `digest`). Only supported, assessed containers contribute groups; snapshot
  images alone do not. No assessed containers yields `imageOverview: []`.
- Each group's `nodes` lists all supplied schedulable nodes, sorted by name,
  once each, with `node`, nullable `present`, `availability`, `availabilityReason`
  and raw `matches` evidence. This availability is independent of pull policy;
  no single group-wide policy or local-use verdict is inferred.
- `uses` preserves assessment order (manifest argument order, document order,
  init containers then regular containers). Each use has a zero-based
  `assessmentIndex` into the unchanged `assessments` array, plus `source`,
  1-based `document`, `apiVersion`, `kind`, `namespace`, `name`, exact `path`,
  `container`, `containerType`, original `image`, `effectivePullPolicy` and
  `policySource`. No occurrence is deduplicated, even identical identities in
  separate documents or files. `policyRisk` retains the Always warning (otherwise
  null), and `nodeVerdicts` retains each node's `node`, `verdict` and `reason`.
- Canonical aliases group together; tags, digests, repositories, registries,
  ports and case-sensitive tags remain distinct under the existing matching
  rules. Tag+digest aliases group by repository@digest. Shared CRI IDs/records
  never merge groups or prove digest presence.

Text prints shared availability once per image/node, followed by every use's
identity, location, original image, policy/provenance, warning and node verdicts.
JSON retains the full workload-centric `assessments` matrix. Grouping never
changes `summary`, `--check`, load proposals or digest omissions. Missing captures
stay unknown, and invalid manifests/inventories/captures still exit 2 with no
partial report, including an error after an earlier valid use.

### Compare current nodes' recorded tag evidence

Tag groups also carry additive `tagDigestEvidence` in report schema 2. No
baseline is required. These synthetic examples request the same tag on two
captured nodes whose recorded digest sets differ:

```sh
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/nodes.json --json --check
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/nodes.json
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/partial-nodes.json --json
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/partial-nodes.json
```

The complete inventory has one `docker.io/library/demo:v1` group, two uses and
two present nodes. Its `tagDigestEvidence.comparison` is
`different-digest-evidence` and `knownDifference` is true: node-a records
`docker.io/library/demo@sha256:aaaa…` and node-b records
`docker.io/library/demo@sha256:bbbb…` (the actual JSON contains full digests).
Both uses permit local images, so **`--check` still exits 0** and `loadPlan` is
empty. Recorded drift does not automatically reload a present tag.

The partial inventory adds node-c with two matching tag records, one lacking a
same-repository digest, node-missing with a captured empty list, and node-unknown
without a snapshot. Overall comparison is **`unassessed`**, while
**`knownDifference: true`** preserves the difference established between complete
nodes a and b. All known evidence remains visible, including c's partial set and
empty second record. Availability stays `present` on a/b/c, `missing` on
node-missing and `unassessed` on node-unknown. The one load proposal targets only
node-missing. Valid JSON/text reports exit 0; adding `--check` exits 1.

The evidence contract is:

- `nodes` retains every current schedulable node, sorted by name, with `node`,
  `status` (`complete` or `unassessed`), `reason`, sorted canonical `repoDigests`
  and `records`. Each record retains its zero-based CRI `imageIndex` and sorted
  same-repository `repoDigests`. Existing overview `nodes[].matches` retains raw
  tag spellings. Alias/duplicate references count once per record/digest.
- Only `repoDigests` on **the same CRI image record** as the exact normalized tag
  and for **that tag's repository** contribute. Every matching record must have
  at least one such digest for the node to be complete. Multiple records retain
  their individual sets; comparison uses their deduplicated union per node.
- `comparison` is `same-digest-evidence` for equal complete node unions or
  `different-digest-evidence` for unequal complete unions, only when there are
  at least two nodes and **every** current schedulable node is complete.
  Otherwise it is `unassessed`, including missing tags, incomplete tag records,
  omitted snapshots and a single node. Empty evidence never proves equality.
- `knownDifference` is true when at least two **complete** nodes have unequal
  unions, including when gaps leave the overall result unassessed. False means
  no difference was established; it does not prove equality. Unequal partial
  unions cannot establish a difference because missing evidence could fill them.
- `reason` explains the result and `note` states the limit: recorded evidence
  only, with **no runtime content-equivalence or freshness guarantee**. Text
  prints the result, known difference, each node/record set and evidence gaps.

Each evidence node also has `recordComparison` with `comparison`,
`knownDifference` and `reason` for **records within that node**, independently
of the cross-node union comparison:

- At least two matching tag records, all with nonempty same-repository digest
  sets, yield `same-digest-evidence` or `different-digest-evidence`. A single
  record, an absent tag/capture, or any record without such digests yields
  `unassessed`. Multiple aliases of a tag on one record count as one record.
- `knownDifference` is true when at least two complete records have unequal
  sets, even if another record lacks evidence. False does not establish equality.
  CRI IDs and unrelated records/repositories do not participate. Identical
  complete sets on distinct records report the same recorded evidence.
- This signal does not change a node's `status`, its union, the cross-node
  result, or workload availability. Equal unions can contain differing record
  sets. Text labels this separately as `Within-node tag records` and retains
  every record and gap. These are recorded sets, never runtime content claims.

Current tag evidence also has an independent `recordSetComparison` with
`comparison`, `reason`, `nodes`, `cohorts` and `unassessedNodes` for
**complete per-record groupings across
nodes**. Each node retains `node`, `status`, `reason`, `repoDigestSets` and
`missingRecordCount`. The sets are sorted distinct nonempty canonical
same-repository digest sets from the exact tag's matching records. Record
order/index, CRI IDs, alias/duplicate digests and identical complete duplicate
records do not change a grouping. This uses the same normalization as temporal
`inventoryDiff` record-set evidence.

At least two current schedulable nodes, all with complete groupings, are needed
for `same-digest-evidence` or `different-digest-evidence`. Any incomplete record,
absent exact tag or omitted snapshot leaves this comparison `unassessed`.
Partial sets remain visible without establishing equality or difference, even
when two other complete nodes differ. There is no new `knownDifference` flag.
A missing tag/capture has `repoDigestSets: []`, `missingRecordCount: 0`, an
`unassessed` status and an explicit reason; zero missing records does not certify
a complete grouping. Incomplete matching records count individually, including
duplicates. Only complete duplicate sets are deduplicated. Text labels the
result separately as `Cross-node tag record sets` and each node as
`Node record sets`, retaining its status, sets, count and gap reason. The existing
union and within-node comparisons and both existing `knownDifference` flags
keep their independent meanings. All three comparisons describe recorded
evidence only; none guarantees runtime content or freshness.

`cohorts` aggregates identical **complete current schedulable-node** groupings.
Each row contains `repoDigestSets` and a sorted `nodes` list. Rows are ordered
lexicographically by their normalized digest-set lists, without generated IDs.
Record order/index/ID, aliases, duplicate digests and identical complete duplicate
records cannot split a cohort. A singleton complete node can form a cohort;
this does not establish cross-node equality. With no complete nodes, `cohorts`
is `[]`.

`unassessedNodes` is a sorted list of node names referencing the existing
`nodes` rows with their explicit statuses, reasons, partial sets and missing
record counts. It is `[]` when every grouping is complete. Partial nodes never
join a complete cohort, even when their visible sets match it. Excluded nodes
appear in neither list. Cohorts summarize recorded evidence and never override
an overall `unassessed` result, either for gaps or for a single supplied node.
Text prints `Complete tag record-set cohort` rows and an explicit
`Unassessed tag record-set nodes` list before the unchanged per-node details.
This additive JSON/text output keeps report schema 2; strict consumers must
accept the new fields/rows. It adds no runtime identity or workload intent.

For a many-node example with repeated groupings and gaps:

```sh
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/cohort-nodes.json --json --check
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/cohort-nodes.json --check
```

The complete cohorts are `[[A],[B]]` on `node-a`/`node-c` and `[[A,B]]` on
`node-b`/`node-d`, using full canonical repository digests in JSON. Aliases,
reordered records/digests, different IDs and complete duplicates do not split
them. `unassessedNodes` lists `node-incomplete`, `node-missing` and `node-unknown`;
their details retain an incomplete record, absent tag and omitted capture,
respectively. `node-excluded` stays outside workload evidence. The overall
record-set comparison remains `unassessed`, union `knownDifference` stays false,
and node-a's within-node `knownDifference` stays true. Only `node-missing` gets
a load proposal; both commands exit **1** because workload coverage has gaps.

For a concrete equal-union example:

```sh
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/records-nodes.json --json --check
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/evidence/records-nodes.json
```

Node-a has matching records with sets `{A}` and `{B}`; node-b has one record
with `{A,B}`. Both unions are `{A,B}`, so the cross-node result remains
`same-digest-evidence`, with `knownDifference: false`. Node-a's
`recordComparison` reports `different-digest-evidence`, with
`knownDifference: true`; node-b's is `unassessed` because only one matching
record exists. The new `tagDigestEvidence.recordSetComparison.comparison` is
`different-digest-evidence`: its complete node-a grouping is `[[A],[B]]` and
node-b's is `[[A,B]]` (full canonical repository digests in JSON). All four
workload checks remain `local-available`, no loads are proposed, and `--check`
exits **0**.

Replacing the inventory with `examples/kind_images/evidence/records-partial-nodes.json`
adds a node with `{A}` and an empty record, a captured empty node and an omitted
snapshot. The cross-node result becomes `unassessed` with
`knownDifference: false`; node-a's complete-record difference stays visible.
The new cross-node record-set comparison is also `unassessed`, retaining the
complete a/b groupings and all gaps. Node-c retains `[[A]]` with
`missingRecordCount: 1`; the absent tag and omitted capture each retain `[]`,
count 0 and their distinct reasons. The incomplete node's within-node record
comparison stays `unassessed` with `knownDifference: false`. Only
`node-missing` receives a load proposal;
`--check` exits **1**. `Always` policies, when supplied, retain their independent
warnings on every node.

CRI IDs, unrelated records/repositories, case-distinct tags, registry/port
variants, excluded nodes and an optional baseline cannot fill current tag
evidence gaps. Digest groups, including tag+digest spellings, keep exact
repository@digest matching and do not receive `tagDigestEvidence`. No tag/digest
alias is inferred. Per-use policies/warnings/verdicts, summary, `--check`, load
targets, digest omissions and the baseline comparison remain independent.
Inventory-only mode still has no workload groups or load proposals. Invalid
inputs still fail atomically with exit 2.

## Compare two explicit inventories

Add `--baseline-inventory` to compare an earlier caller-supplied inventory with
the current `--inventory`. Omit `--manifests` for an inventory-only comparison:

```sh
stackwrench kind-images --inventory examples/kind_images/diff/after.json --baseline-inventory examples/kind_images/diff/before.json --json
stackwrench kind-images --inventory examples/kind_images/diff/after.json --baseline-inventory examples/kind_images/diff/before.json
```

This produces the same node/reference/digest-evidence comparison described below,
including `node-old` removed, `node-new` and `node-unknown` added, and changed
digest evidence for `demo:v1` on `node-a`. It makes **no load proposals**: an
observed reference or coverage loss alone does not establish workload intent.

The inventory-only output keeps report `schemaVersion: 2` and `inventoryDiff`
`schemaVersion: 1`, and adds `mode: "inventory-only"`. Current `nodes`,
`excludedNodes`, `unassessedNodes` and `assumptions` retain their usual meaning.
`assessments`, `imageOverview`, `unassessed`, `loadPlan` and `loadPlanOmissions`
are empty arrays. `summary` has `containers: 0`, `nodeChecks: 0`, `verdicts: {}`,
`unassessed: 0` and `localUsePossibleEverywhere: false`. Text explicitly states
that no workload readiness was assessed. Comparison coverage includes only
references observed on supplied schedulable nodes; empty or entirely uncaptured
inventories can therefore have empty coverage without proving readiness.

Valid inventory-only reports exit **0**; adding `--check` exits **1**, even for
identical complete captures, because no workload has been assessed. `--check`
remains a workload local-use check, never a drift/equality gate. Both flags
`--inventory` and `--baseline-inventory` are required when manifests are omitted.
Omitting the baseline too, supplying `--manifests` with no file arguments, or
invalid data on either side exits **2** with no report. Both inventories still
require at least one explicitly schedulable node. Missing captures, excluded
nodes, relative paths, evidence validation and same-cluster rules are unchanged.
No manifest files are implicitly discovered or read.

Supply `--manifests` to include the current workload matrix and load plan:

```sh
stackwrench kind-images --manifests examples/kind_images/diff/workloads.yaml --inventory examples/kind_images/diff/after.json --baseline-inventory examples/kind_images/diff/before.json --json
stackwrench kind-images --manifests examples/kind_images/diff/workloads.yaml --inventory examples/kind_images/diff/after.json --baseline-inventory examples/kind_images/diff/before.json
```

These local synthetic fixtures have the following expected changes:

- `node-old` is removed; `node-new` and `node-unknown` are added. The latter
  has no capture, so its availability stays unknown.
- On retained `node-a`, `old:v1` disappears and `demo:v1` retains its tag while
  its recorded repository digest changes from `sha256:aaaa…` to `sha256:bbbb…`.
  The unchanged tag is reported as **`digest-evidence-changed`**, not equivalent.
- Current coverage for `demo:v1` is present on `node-a`, missing on `node-new`,
  and unassessed on `node-unknown`. Its load proposal targets only `node-new`.
  The `old:v1` proposal targets `node-a,node-new`. No load targets a removed or
  uncaptured node; digest drift does not by itself propose reloading a present tag.
- Some captures lack time/source; these are explicitly `null` with
  `missingEvidence` entries in JSON, and `MISSING` in text. `node-unknown` also
  lists its missing snapshot. Excluded nodes remain visible as excluded evidence.

The comparison text uses the same evidence format as the current-inventory
section: inventory paths and supplied snapshot/time/source strings are JSON
quoted, absent values are bare `MISSING`, and every node includes its ordered
`missingEvidence` array. For example, supplied `source="MISSING"` differs from
absent `source=MISSING`; quotes, backslashes and Unicode in labels remain
escaped inside the quoted value. This applies to both before/after sections,
with or without manifests. JSON fields and exit statuses are unchanged.

Optional per-node evidence belongs in the **inventory**, never in CRI JSON:

```json
{"schedulable": true, "snapshot": "after-a.json", "capturedAt": "2026-11-11T11:00:00Z", "source": "synthetic fixture after/a"}
```

`source` is a nonempty string label, never fetched or executed. `capturedAt`
accepts an RFC3339 subset: valid calendar date, uppercase `T`, seconds,
optional 1–6 fractional digits, and uppercase `Z` or a numeric `±HH:MM` offset.
Leap seconds are unsupported. Both keys are optional, but explicit null/empty
or invalid values fail; either key requires a snapshot. These caller-supplied
values **do not guarantee freshness**, completeness or cluster identity.
Timestamps never sort the inputs, reject a future capture, or certify readiness;
the flags alone define before/after. A snapshot path identifies the local file,
not its capture provenance, so it does not fill a missing `source`.

With this flag, report schema 2 gains an additive `inventoryDiff` object with
its own `schemaVersion: 1`; without it the report structure is unchanged:

- `before`/`after` contain inventory paths and every declared node's capture
  evidence. Each side resolves snapshot paths relative to its own inventory.
- `nodesAdded`/`nodesRemoved` compare **all declared names**, including excluded
  nodes; `schedulabilityChanges` records retained nodes entering/leaving coverage.
- `imageChanges` contains one comparison for each retained node with two
  captures, including excluded nodes. `addedReferences`/`removedReferences` use
  exact canonical tags and repository digests. Removal means absent from that
  capture, not an independently observed runtime deletion. Nodes without both
  captures enter `unassessedComparisons`; a lost capture never means lost images.
- `tags` compares retained tags using only same-repository `repoDigests` on the
  **same CRI image record** as that tag. Different nonempty digest sets yield
  `digest-evidence-changed`; equal sets yield `same-digest-evidence`. If any
  matching record on either side lacks such evidence, comparison is `unassessed`.
  CRI IDs, other repositories and unrelated records cannot establish equality.
  Even equal digest evidence does not validate actual runtime content.
- Each retained tag also has independent `recordSetComparison` with `before`,
  `after`, `comparison` and `reason`. Each side has `status` (`complete` or
  `unassessed`), `repoDigestSets` (sorted distinct nonempty per-record canonical
  digest sets), and `missingRecordCount` (matching records without any
  same-repository digest). Only the exact tag's own records/repository contribute.
  Digests within each set and the outer list are sorted and deduplicated; record
  order, index renumbering, CRI IDs, alias spellings and identical duplicate
  complete records do not affect the result.
- Temporal record-set comparison is `same-digest-evidence` for equal complete
  groupings or `different-digest-evidence` for different complete groupings,
  **only when both sides are complete**. Any incomplete matching record leaves
  it `unassessed`, even if retained complete sets appear equal or different.
  Partial sets remain visible with their status/count; no partial difference
  is inferred. This does not alter the union `comparison`, current within-node
  `recordComparison`/`knownDifference`, availability, policies, loads or `--check`.
  Missing captures still enter `unassessedComparisons`; absent tags use reference
  additions/removals, not an empty-set equality. This comparison is recorded
  evidence only, never record identity, runtime content or freshness.
- Each existing retained tag row also has `recordEvidence` with `before` and
  `after` sides. Each side is `{captureRef, records}`: `captureRef` is a nonnull
  report-root JSON pointer `/inventoryDiff/{side}/nodes/{index}` to that declared
  node's capture metadata; `records` is the existing `tag_digest_evidence` list
  `[{imageIndex, repoDigests}]`, reused without changing comparisons. Resolve the
  metadata row's `snapshot` relative to that side's `inventory` path, then use
  zero-based `imageIndex` in its supplied CRI `images` array.
  Records sort by original index; each record's canonical same-repository digest
  list sorts and deduplicates. Empty lists are retained, so a missing-record
  count can be traced to specific supplied records. Aliases on one record yield
  one index; identical duplicate records retain their distinct indices. Other
  repositories, unrelated records and CRI IDs cannot supply digest evidence.
  Before/after indices are local positions, **not cross-capture record identity**.
- Record evidence covers only the already-existing retained-node/retained-tag
  rows with two captures, including excluded and changed-schedulability nodes.
  Missing captures, captured tag absences and added/removed nodes gain no pairs,
  states or transitions. Union and record-set results, current within-node
  `knownDifference`, transitions, participation, availability/cohorts/pointers,
  node-first navigation, coverage and workload scope remain authoritative.
  Supplied provenance does not establish runtime content, freshness or capture
  completeness. Text adds separate `Temporal tag record evidence before/after`
  lines and a caution; strict consumers must accept additive JSON/text fields.
  Report schema 2 and inventoryDiff schema 1 remain unchanged.
- `tagRecordSetTransitions` groups the **existing** per-node temporal tag rows
  by canonical `image`. Each entry adds `cohorts` and `unassessedNodes`, sorted
  by image. A cohort has `beforeRepoDigestSets`, `afterRepoDigestSets` and sorted
  `nodes`; it contains only pairs whose two record-set sides are `complete`.
  Cohorts sort lexicographically by normalized before sets, then after sets.
  The normalization above also applies here, so aliases, record order/IDs,
  duplicate digests and identical complete duplicate records do not split a
  transition. A singleton cohort is allowed and makes no cross-node equality
  claim. Cohort membership describes recorded transitions, never runtime identity.
- Transition scope is **all retained declared nodes with two captures and the
  exact tag on both sides**, including `schedulable=false` nodes and nodes whose
  schedulability changed. It is independent of current-workload cohort scope,
  requested images and schedulable-only coverage. Before/after evidence and
  `schedulabilityChanges` keep each node's explicit metadata. `unassessedNodes`
  references this image's existing `imageChanges[node].tags[image]` temporal
  comparison, retaining its statuses, partial sets, reasons and missing counts.
  Partial pairs never enter complete cohorts or establish equality/difference.
  An all-gap tag has `cohorts: []`; no retained tag pairs means
  `tagRecordSetTransitions: []`. Missing captures, one-sided tags, and added or
  removed nodes remain in their existing explicit evidence and are never
  synthesized into tag transitions. No new overall comparison, workload intent,
  readiness gate or load proposal is inferred.
- `tagRecordSetParticipation` summarizes participation for **every canonical
  explicit tag observed in either inventory's supplied captures**, including
  excluded and added/removed nodes. It is independent of workload intent;
  requested-only tags, digest references and CRI IDs cannot create entries.
  Rows sort by `image`; all node lists sort by name. Each row has:
  - `completePairNodes` / `incompletePairNodes`: references to this image's
    existing `imageChanges[node].tags[image].recordSetComparison`. A complete
    pair requires both sides `complete`; any incomplete matching record leaves
    the pair incomplete. These lists do not change transitions or comparisons.
  - `missingCaptureNodes: {beforeOnly, afterOnly, both}`: references to retained
    `unassessedComparisons` and before/after capture rows, grouped by exactly
    which captures are missing. Tag availability on those sides stays **unknown**,
    even when the supplied opposite capture is empty. No tag presence or absence
    is assigned to a missing capture.
  - `capturedTagAbsenceNodes: {beforeOnly, afterOnly, both}`: retained nodes with
    **two captures** and this tag absent from exactly the named sides. Before-only
    absence references `addedReferences`, after-only absence references
    `removedReferences`; both-side absence references the captures and lack of
    this tag on both sides. Absence on both sides never creates a retained pair.
  - `nodesAdded` / `nodesRemoved`: references to the existing lifecycle lists and
    side evidence, with **no claim about this tag's presence** on lifecycle nodes.
  Every declared node in the union of both inventories appears exactly once per
  observed tag. Lifecycle takes precedence over missing capture, then existing
  retained pairs, then captured absence. Excluded nodes and schedulability
  changes participate consistently with temporal evidence; current workload
  scope and `coverage` remain schedulable-only. These are evidence references,
  not a new transition, equality/difference result, readiness gate or load plan.
- `tagAvailabilityEvidence: [{image, nodes, cohorts}]` exposes **per-side name evidence
  for nodes without a retained tag pair**. It reuses the participation view's
  observed explicit canonical tag universe and all-declared-node scope,
  including excluded nodes and schedulability changes. For each tag, it excludes
  both `completePairNodes` and `incompletePairNodes`; incomplete digest evidence
  does not make a captured tag name unknown. Tags sort by `image`, nodes by name;
  a tag whose nodes all have retained pairs has `nodes: []`. No observed tags
  produces `tagAvailabilityEvidence: []`, even when manifests request tags.
  Each node row has `node`, `before`, `after` and `evidenceRefs`:
  - Each side is `{state, captureRef, matches}`. `state` is exactly one of
    `captured-present` (supplied capture contains this exact canonical tag),
    `captured-absent` (supplied capture lacks it), `missing-capture` (declared
    node without a supplied snapshot), or `node-not-declared` (node absent from
    this side's inventory). The last two are distinct **unknown** states. An
    empty supplied capture establishes captured absence; missing captures and
    undeclared sides establish neither presence nor absence.
  - `captureRef` is a JSON pointer from the report root to
    `/inventoryDiff/before/nodes/<index>` or `/inventoryDiff/after/nodes/<index>`.
    The referenced metadata row names the declared node and supplied snapshot;
    resolve a relative snapshot path against that side's `inventory` file.
    Missing captures still reference their declared row (`snapshot: null`);
    only `node-not-declared` has `captureRef: null`.
  - `matches` contains deduplicated `{imageIndex, field, reference}` facts from
    that side's supplied CRI snapshot, sorted by those fields. It is nonempty
    only for `captured-present`; `field` is `repoTags`, `imageIndex` is the
    zero-based CRI `images` index, and `reference` retains the original tag
    spelling. Canonical aliases match; tag case, repository, registry and port
    differences do not. Digest completeness is irrelevant to name presence;
    `repoDigests` and CRI IDs cannot establish it.
  - `evidenceRefs` is a lexicographically sorted list of report-root JSON
    pointers to declared before/after capture rows plus the existing
    `imageChanges`, `unassessedComparisons`, `nodesAdded` or `nodesRemoved`
    entry for this node. Lifecycle entries justify an undeclared opposite
    side; no nonexistent capture row is referenced. These pointers are local
    to this report, not stable identifiers across reports.
  `cohorts: [{beforeState, afterState, nodes}]` groups these existing node rows
  by both literal side states. Cohorts sort lexicographically by `beforeState`,
  then `afterState`; member names sort lexicographically and reference the
  `nodes` rows in the same tag entry. Every nonparticipant appears exactly once;
  complete/incomplete retained tag pairs never enter. Empty `nodes` has
  `cohorts: []`. Canonical aliases, duplicate capture records, different matches
  or metadata do not split a state pair. All four states retain their exact
  meanings, including the distinction between `missing-capture` and
  `node-not-declared`. Cohorts summarize supplied side evidence only: unknown
  and lifecycle pairs do not imply tag gains or losses, content transitions or
  workload intent. Text prints `Observed tag availability state cohort` rows
  beside the unchanged per-node evidence and an explicit caution.
  This additive view leaves participation partitions, transitions and all
  comparisons unchanged. Captured absence describes the supplied name list,
  not runtime deletion. No availability is inferred on unknown sides, and no
  tag transition, workload intent, readiness or content identity is inferred.
  Report schema 2 and inventoryDiff schema 1 stay unchanged; strict consumers
  must accept the additive JSON field and text block.
- `nodeTagEvidenceIndex: [{node, tags}]` provides node-first navigation across
  those existing temporal views. It includes every declared node from either
  inventory, sorted by name, including excluded nodes and schedulability changes.
  Each node's `tags` lists every observed explicit canonical tag once, sorted by
  `image`, with exactly these fields:
  - `image`: the same observed tag as in `tagRecordSetParticipation`. Requests,
    digests and CRI IDs cannot add tags; aliases do not split entries. If neither
    inventory observes any tags, each declared node still has a row with `tags: []`.
  - `participationBucket`: the exact existing membership path:
    `completePairNodes`, `incompletePairNodes`, `missingCaptureNodes.beforeOnly`,
    `missingCaptureNodes.afterOnly`, `missingCaptureNodes.both`,
    `capturedTagAbsenceNodes.beforeOnly`, `capturedTagAbsenceNodes.afterOnly`,
    `capturedTagAbsenceNodes.both`, `nodesAdded` or `nodesRemoved`.
    These preserve the participation view's precedence and distinctions.
  - `participationRef`: a report-root JSON pointer to the node-name member in
    `/inventoryDiff/tagRecordSetParticipation/<tag-index>/<bucket>/<member-index>`;
    dotted buckets use separate path components, for example
    `.../missingCaptureNodes/beforeOnly/0`.
  - `evidenceRefs`: a sorted list containing exactly one report-root JSON pointer
    to the authoritative row. Both complete and incomplete pairs point to
    `/inventoryDiff/imageChanges/<node-index>/tags/<tag-index>`. Every other
    bucket points to
    `/inventoryDiff/tagAvailabilityEvidence/<tag-index>/nodes/<node-index>`.
    Follow that row's existing capture and evidence pointers for side details.
  All indices are zero-based array positions local to this report, never stable
  identities across reports. The index copies no states, matches, comparisons or
  cohorts and infers no availability on unknown sides, tag gains/losses, content
  transitions or workload intent. Existing views remain authoritative. Text prints
  an `Observed tag evidence index node=...` block per node with the same buckets
  and pointers, or `(no observed tags)`. This additive JSON/text keeps report
  schema 2 and inventoryDiff schema 1; strict consumers must accept the addition.
- `coverage` includes observed references on schedulable nodes and every
  requested image when manifests are supplied, even one absent from both inventories. Each side lists
  `presentNodes`, `missingNodes` and `unassessedNodes` separately, using that
  side's schedulable membership. For requested images, `after.missingNodes`
  matches the current tag load plan or digest load omission. Name availability
  is independent of digest-evidence comparison and `Always` risk.

For a temporal equal-union example (synthetic local captures):

```sh
stackwrench kind-images --inventory examples/kind_images/diff/record-sets/after.json --baseline-inventory examples/kind_images/diff/record-sets/before.json --json
stackwrench kind-images --inventory examples/kind_images/diff/record-sets/after.json --baseline-inventory examples/kind_images/diff/record-sets/before.json
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/diff/record-sets/after.json --baseline-inventory examples/kind_images/diff/record-sets/before.json --json --check
```

On retained `node-a`, `demo:v1` changes from complete records `{A}`, `{B}`
to one complete record `{A,B}`. The existing union `comparison` stays
`same-digest-evidence`. `recordSetComparison.before.repoDigestSets` contains
`[[A],[B]]`, and `after.repoDigestSets` contains `[[A,B]]` (full canonical
repository digests in actual output); both statuses are `complete`, both missing
counts are 0, and the new result is `different-digest-evidence`. Text shows a
separate `Temporal tag record sets` line and both sides. The workload command
still exits **0** with no loads; inventory-only `--check` still exits **1**.

Replace `after.json` with `incomplete.json` in the commands above to retain
complete `{A}`, `{B}` sets plus a third matching record without a digest.
The new result and union comparison are `unassessed`; the after side retains
`[[A],[B]]`, `status: "unassessed"` and `missingRecordCount: 1`. Current
within-node `recordComparison.knownDifference` remains true independently.
The tag is present, so the workload command still exits **0** with no loads.
Neither recorded change nor a digest-evidence gap automatically fails `--check`.

For traceable duplicate and incomplete records (synthetic local captures):

```sh
stackwrench kind-images --inventory examples/kind_images/diff/record-provenance/after.json --baseline-inventory examples/kind_images/diff/record-provenance/before.json --json
stackwrench kind-images --inventory examples/kind_images/diff/record-provenance/after.json --baseline-inventory examples/kind_images/diff/record-provenance/before.json
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/diff/record-provenance/after.json --baseline-inventory examples/kind_images/diff/record-provenance/before.json --json --check
```

For `node-a`'s retained `demo:v1`, the before records have indices
`1: {A,B}`, `2: {A}`, `3: {A}`, `4: {}`; after records have indices
`0: {A}`, `2: {A,B}`, `3: {}`. Actual output uses full canonical repository
digests. Before index 0 is unrelated; after index 1 is digest-only and cannot
fill another record's gap. Duplicate tag/digest aliases on before index 1
deduplicate, while indices 2 and 3 both survive. The unrelated repository digest
and digest-shaped ID on before index 4 do not fill its empty list.
`recordEvidence.before.captureRef` is `/inventoryDiff/before/nodes/0`;
`recordEvidence.after.captureRef` is `/inventoryDiff/after/nodes/0`.

The unions and distinct-group summaries coincide on both sides, with
`missingRecordCount: 1`, while the supplied record positions/multiplicity differ.
Both comparisons remain `unassessed`; current within-node `knownDifference`
remains true, and the present tag still permits workload `--check` to exit **0**
with no loads. Plain inventory-only JSON/text exits **0**; its `--check` exits
**1** because there is no workload intent. Neither matching indices nor matching
summaries certify record identity, runtime content or freshness.

For observed-tag participation and explicit gaps (synthetic local captures):

```sh
stackwrench kind-images --inventory examples/kind_images/diff/participation/after.json --baseline-inventory examples/kind_images/diff/participation/before.json --json
stackwrench kind-images --inventory examples/kind_images/diff/participation/after.json --baseline-inventory examples/kind_images/diff/participation/before.json
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/diff/participation/after.json --baseline-inventory examples/kind_images/diff/participation/before.json --json --check
```

For `demo:v1`, complete pair references are `a,b,c,d,entering,excluded,leaving`;
incomplete pair references are `partial-after,partial-before`. Missing captures
are before-only `missing-before,unknown-absent-before`, after-only
`missing-after,unknown-absent-after`, and both `missing-both`. The two
`unknown-absent-*` nodes have a captured-empty opposite side and still never
enter captured tag absence. Captured tag absence is before-only `tag-added`,
after-only `tag-removed`, and both `absent-both`.

Lifecycle references are `added,added-missing,added-observed` and
`removed,removed-missing`, independent of tag availability. `solo:v1` is observed
only on the added excluded `added-observed` node: it gets a participation entry
with no complete/incomplete retained pairs and no transition cohort. It does not
enter schedulable-only coverage or imply workload intent. Text shows a separate
`Observed tag temporal participation` block with reference lists and cautions.
The same commands also show `tagAvailabilityEvidence` and a separate
`Observed tag side availability` text block. For `demo:v1`, `missing-before`
has before `missing-capture` and after `captured-present`, while
`unknown-absent-before` has before `missing-capture` and after
`captured-absent`. `missing-both` has two `missing-capture` sides. `added` has
before `node-not-declared` and after `captured-present`; `added-missing` has
before `node-not-declared` and after `missing-capture`. For `solo:v1`, only
`added-observed` has a captured-present after side; the same node's demo tag
is captured-absent. Original CRI tag matches and evidence pointers accompany
these states. Complete and incomplete retained tag pairs stay in their
existing participation/temporal views.
JSON/text inventory-only commands exit **0**; adding `--check` exits **1** without
workload readiness or load proposals. The workload `--check` example exits **1**
for existing missing/unknown availability, with one tag load targeting the known
current absences `absent-both,tag-removed,unknown-absent-before`. Missing current
captures do not enter that load proposal.

For repeated nonparticipant state pairs (synthetic local captures):

```sh
stackwrench kind-images --inventory examples/kind_images/diff/availability-cohorts/after.json --baseline-inventory examples/kind_images/diff/availability-cohorts/before.json --json
stackwrench kind-images --inventory examples/kind_images/diff/availability-cohorts/after.json --baseline-inventory examples/kind_images/diff/availability-cohorts/before.json
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/diff/availability-cohorts/after.json --baseline-inventory examples/kind_images/diff/availability-cohorts/before.json --json --check
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/diff/availability-cohorts/after.json --baseline-inventory examples/kind_images/diff/availability-cohorts/before.json
```

For both `demo:v1` and `solo:v1`, `present-missing-a,present-missing-b` share
`captured-present` / `missing-capture`; aliases and duplicate records on `b`
keep their distinct per-node matches. `absent-missing-a,absent-missing-b` share
`captured-absent` / `missing-capture`, and `unknown-a,unknown-b` share two
`missing-capture` sides. `added-present-a,added-present-b` share
`node-not-declared` / `captured-present`; `added-absent-a,added-absent-b` share
`node-not-declared` / `captured-absent`. Added and removed uncaptured nodes
remain distinct literal state pairs. Excluded and changed-schedulability nodes
are included. The demo `complete-pair` and `incomplete-pair` stay outside these
cohorts. No pair involving an unknown or undeclared side asserts a tag gain or
loss. JSON/text exits **0**; workload `--check` exits **1** for current
missing/unknown availability. Inventory-only `--check` also exits **1**, with no
workload intent or load proposals. Cohorts leave those decisions unchanged.

The same commands demonstrate node-first navigation in both modes, using the
existing synthetic captures. Find `node: "complete-pair"` in
`inventoryDiff.nodeTagEvidenceIndex`: its `demo:v1` entry has
`participationBucket: "completePairNodes"` and
`evidenceRefs: ["/inventoryDiff/imageChanges/1/tags/0"]`. Its `solo:v1` entry also
has `completePairNodes`, pointing to `/inventoryDiff/imageChanges/1/tags/1`.
`incomplete-pair` keeps `incompletePairNodes` for demo and
`capturedTagAbsenceNodes.both` for solo, pointing to that tag's availability row.
`present-missing-b`
keeps `missingCaptureNodes.afterOnly` despite its schedulability change, and
`added-missing-b` keeps `nodesAdded` despite lacking a capture. Pointers resolve
from the full JSON report root; they introduce no new availability or transitions.
The text output lists each node's two tags, buckets and pointers together.

For repeated temporal transitions and explicit gaps (synthetic local captures):

```sh
stackwrench kind-images --inventory examples/kind_images/diff/transitions/after.json --baseline-inventory examples/kind_images/diff/transitions/before.json --json
stackwrench kind-images --inventory examples/kind_images/diff/transitions/after.json --baseline-inventory examples/kind_images/diff/transitions/before.json
stackwrench kind-images --manifests examples/kind_images/evidence/workloads.yaml --inventory examples/kind_images/diff/transitions/after.json --baseline-inventory examples/kind_images/diff/transitions/before.json --json --check
```

For `demo:v1`, the `[[A],[B]] -> [[A,B]]` cohort contains
`a,c,entering,excluded`; the `[[A,B]] -> [[A,B]]` cohort contains `b,d,leaving`.
Actual output uses full canonical digests. `c,d` use aliases, reordered digests
and duplicate complete records. `excluded` stays unschedulable; `entering`
becomes schedulable and `leaving` becomes unschedulable. All participate in
existing temporal evidence. Text shows separate `Complete temporal tag
record-set cohort` rows and `Unassessed temporal tag record-set nodes` references.
The two partial pairs appear only in `unassessedNodes` (`partial-after`,
`partial-before`); neither complete-looking subset becomes a cohort. Missing
captures, tag additions/removals and node additions/removals retain their separate
rows. The workload `--check` command exits **1** for current missing/unknown
availability; only `tag-removed` receives a tag load proposal. Inventory-only
commands without `--check` exit **0** with no workload assessments or loads;
adding `--check` exits **1**. Existing current cohorts, union/within-node evidence,
per-node temporal comparisons and capture labels are preserved. Report schema 2
and inventoryDiff schema 1 are unchanged; strict consumers must accept these
additive fields and text rows.

Different cluster names, invalid evidence, malformed/unreadable captures on
either side, or invalid supplied manifests return **2 with no partial report**.
An explicitly supplied empty manifest file remains an error; it does not activate
inventory-only mode. Valid comparisons return 0; with manifests, `--check` still
checks **current** static local-use readiness only, returning 1 for this fixture.
It is not a drift/freshness gate: a changed
digest beneath an available tag can coexist with a successful `--check`.
Both inventories and their captures remain unchanged. No daemon, kubeconfig,
network, clock query, registry lookup, capture command or load command is used.

## Relationship, scope and licenses

This independent MIT implementation interoperates with
[kind](https://github.com/kubernetes-sigs/kind), [Kubernetes](https://kubernetes.io/)
and [cri-tools](https://github.com/kubernetes-sigs/cri-tools); it is not an upstream
component or endorsement. [kind's existing load command](https://kind.sigs.k8s.io/docs/user/quick-start/#loading-an-image-into-your-cluster)
transfers images, while kubectl can inspect live objects and crictl can inspect
one runtime's inventory. This tool's narrower contribution is the offline join
of rendered intent and per-node captures, plus reviewable load proposals.
It does not replace those tools or claim exclusive functionality. No upstream
source was copied. See the repository's `LICENSE` and `THIRD_PARTY.md`.

Limits: no cluster mutation, kubeconfig/daemon access, registry checks, pulling,
building, loading, scripts, live scheduling, vulnerability scans, CRD discovery,
architecture validation or credential validation. Node selectors, affinity,
taints and explicit `nodeName` are not placement filters: the matrix always
covers **all supplied schedulable nodes**. Credential verification, custom
runtime handlers, admission and kubelet configuration may change live behavior.
`Always` denotes a registry/pull requirement, not proof of transferred bytes.
YAML anchors, aliases, merges, custom tags and duplicate/non-string keys are
rejected; render/expand them first. Each input file is bounded to 8 MiB and each
manifest file to 1,000 documents. This is a static integration tool, with no
performance or deployment-success claims.
