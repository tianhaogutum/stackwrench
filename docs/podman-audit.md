# stackwrench podman-audit

`stackwrench podman-audit` (formerly `compose-podman-audit`) reviews **declared
Compose dependencies and healthchecks** before a migration to **podman-compose**,
offline. It joins exact service paths to a small, version-scoped evidence catalog.
It does not certify Compose validity or provider compatibility, and it never runs
containers, commands, health probes, or scripts.

No daemon, credentials or API keys are needed. All examples are synthetic, using
`example.invalid` images.

## Install

Requires Python 3.12 or newer. The only runtime dependency is PyYAML, which is a
base dependency of stackwrench.

```sh
pipx install stackwrench              # or: pip install stackwrench
stackwrench podman-audit --version    # prints the bare stackwrench version

# Or run without installing:
uvx stackwrench podman-audit --help
```

`python -m stackwrench podman-audit` is equivalent to `stackwrench podman-audit`.
The command takes one positional Compose file; there are no subcommands. The
examples below use files from a stackwrench source checkout; run them from its root:

```sh
stackwrench podman-audit examples/podman_audit/podman_audit/compose.yaml --provider podman-compose --provider-version 1.6.0 --json
```

This acceptance example exits **0** and reports two warnings, at
`$.services.api.depends_on.db` (PC1541) and
`$.services.db.healthcheck.start_interval` (PC1500). Warning findings do not fail
by default; use `--fail-on warning` for a stricter CI gate.

```sh
stackwrench podman-audit examples/podman_audit/podman_audit/compose.yaml --provider podman-compose --provider-version 1.6.0
stackwrench podman-audit examples/podman_audit/podman_audit/compose.json --provider podman-compose --provider-version 1.6.0 --json
stackwrench podman-audit examples/podman_audit/podman_audit/clean.yaml --provider podman-compose --provider-version 1.6.0 --json
stackwrench podman-audit examples/podman_audit/podman_audit/compose.yaml --provider podman-compose --provider-version 1.6.1 --json
```

The last command reports **unverified compatibility**, including a catalog
coverage warning. No version interval is inferred. Even a clean input's report
says `compatibility: not-established`.

JSON reports keep the identifier `"tool": "compose-podman-audit"` (with
`toolVersion` set to the stackwrench version) so existing consumers keep working.

## Static reverse dependency impact

```sh
stackwrench podman-audit examples/podman_audit/impact-chain.yaml --impact-service db --json
stackwrench podman-audit examples/podman_audit/impact-chain.json --impact-service db
```

This separate read-only mode lists declared services that depend directly or
transitively on one exact, case-sensitive service name. It includes optional edges
and all three supported conditions. It describes declarations only: no predicted
startup order, health, completion, restart behavior or provider compatibility.
It never runs declared commands, probes or containers, or reads provider profiles.
The existing single-file restricted YAML/JSON validation and limits apply.

`--impact-service SERVICE` accepts `--json` but conflicts with `--provider`,
`--provider-version`, `--compare-profiles`, `--dependency-path`, `--explain`,
and any explicit `--fail-on` (including `error` or `never`). Locations are always included.
Valid queries exit **0**, including cycles, no dependents, disabled healthchecks,
and dangling dependency declarations elsewhere in the document. Invalid input,
unknown/empty selectors, option conflicts, and I/O errors exit **2** before any
impact result. JSON errors retain `assessment: not-evaluated` and `error`;
human errors go to stderr. There is no exit 1 in this mode.

The impact JSON v1 schema has `mode: "impact"`, `assessment:
"declared-dependency-impact"`, tool identity, `selectedService`, its original
`selectedServiceLocation`, `selectedCycle`, `directDependents`,
`transitiveDependents`, `dependents`, and `scope`. Both name lists and `dependents`
sort by exact service name. The transitive list contains only dependents whose
shortest distance exceeds one; a service with a direct edge appears only in the
direct list. The selected service is always omitted from both lists and rows.
For the chain fixture, direct is `["api"]`, transitive is `["web"]`.

Each dependent row has `service`, `direct`, `distance`, `witnessPath`,
`witnessEdges`, and `cycle`. A witness follows dependency declarations from the
dependent to the selected service (e.g. `["web", "api", "db"]`), using the fewest
edges. Ties choose the lexicographically smallest full service-name sequence in
that direction, independent of mapping/list order. Each service appears once;
traversal terminates on cycles. Cycle objects contain `participates`, `selfCycle`
(an explicit self-edge), and sorted `services` (the cyclic SCC, or `[]`).
`selectedCycle` exposes the selected service's cycle membership separately even
though it has no dependent row. These are the audit's existing declared-graph
SCC semantics, including optional edges.

Each witness edge has `service`, `target`, `form` (`short`/`long`), `condition`,
`conditionDeclared`, `required`, `requiredDeclared`, `restart` (`null` if omitted,
otherwise its declared boolean), `path`, and `fieldLocations`. Locations identify
the dependency list item or long key first, then present `condition`, `required`,
and `restart` keys in that order. Short form retains the implicit
`service_started`/required-true defaults without inventing keys. Coordinates use
the existing 1-based Unicode source convention; plain Python mappings yield
`declaration: "unavailable"` and null coordinates. Dangling targets are excluded
from traversal, never synthesized into services. `scope.notEvaluated` retains
opaque field paths without copying commands or other source values. There are
no findings, severity thresholds, catalog judgments or provider fields in an
impact result. Human output shows the same rows, witnesses, edge metadata,
coordinates and cycle membership as deterministic JSON detail lines.

## Static shortest dependency path

```sh
stackwrench podman-audit examples/podman_audit/impact-chain.yaml --dependency-path web db --json
stackwrench podman-audit examples/podman_audit/impact-chain.json --dependency-path db web
```

`--dependency-path SOURCE TARGET` is a separate read-only mode accepting two exact,
case-sensitive declared service names. It accepts `--json` and conflicts with
`--impact-service`, `--provider`, `--provider-version`, `--compare-profiles`,
`--explain`, and every explicit `--fail-on`. The same restricted single-document
YAML/JSON validation and limits apply, including validation outside the path.
Unknown/empty endpoints, malformed input, I/O errors and option conflicts exit
**2** before a path result (the existing `not-evaluated` JSON error or stderr
human error). Every valid query exits **0**, reachable or unreachable; no exit 1.

JSON v1 includes tool identity, `mode: "dependency-path"`, `assessment:
"declared-dependency-path"`, `sourceService`, `targetService`,
`sourceServiceLocation`, `targetServiceLocation`, `sourceCycle`, `targetCycle`,
`reachable`, `distance`, `witnessPath`, `witnessEdges`, and `scope`. The witness
follows dependency direction from source to target, choosing the fewest edges,
then the lexicographically smallest full exact service-name sequence on ties,
independent of mapping/list order. The example web-to-db path is
`["web", "api", "db"]`, distance 2; db-to-web is unreachable with `distance: null`,
`witnessPath: []` and `witnessEdges: []`. An identical source and target is always
reachable at distance 0 with `[SOURCE]` and no edges, even inside a cycle or with
a self-edge. This empty path does not assert a self-dependency; endpoint cycle
objects independently expose explicit self-edges and cyclic SCC membership.

Edge metadata, defaults, original coordinates, endpoint cycle objects and
`scope.notEvaluated` use the impact schema described above. Optional edges and
all three supported conditions count. Absent dependency targets are excluded
from traversal and cannot be selected as endpoints. Malformed declarations
anywhere still fail. Human output includes the same values as JSON detail lines,
with escaped names. No commands, probes or containers are run, and source is
never modified. There are no provider/catalog judgments, findings or thresholds;
reachability describes declarations, not startup order, health, completion,
restart behavior, enforcement or compatibility.

## Findings and exit codes

| Rule | Scope | Meaning |
| --- | --- | --- |
| PC1541 | Long `depends_on`, `condition: service_healthy` | Provider report, exact observed version 1.6.0; other versions unverified |
| PC1500 | Declared `healthcheck.start_interval` | Provider report, exact observed version 1.6.0; other versions unverified |
| DEP001 | Short/long dependencies | Target absent from this document: error if required, warning if `required: false` |
| DEP002 | Healthy dependency target | Explicit disabled check: error; no declared test: warning/unverified because the image may supply HEALTHCHECK |
| DEP003 | Dependency graph | Each edge inside a strongly connected component, including self-cycles and optional edges; graph inference, not a provider bug |
| CAT001 | Requested provider version | Warning if no provider report in this catalog observes the exact supplied version |

Each finding contains `ruleId`, `service`, JSONPath `path`, `severity`,
`reportedBehavior`, `confidence`, `suggestion`, and evidence metadata with a URL,
observed versions, status at retrieval, context, and uncertainty. CAT001 is
project-wide: `service: null`, `path: "$"`. Short dependencies use the real list
index; names with dots or hyphens use bracket paths. Cycles report component
members and each cyclic edge, not an invented execution trace.

Provider reports have confidence `reported` for exact matches and `unverified`
for all others. They are not reproduced runtime failures. Structural graph
observations use `high`; absent image-healthcheck information uses `unverified`.
Reports sort findings by path and rule ID, omit current time and source filename,
and are byte deterministic for the same input text, target, options and catalog.
Without `--explain`, formatting differences in the same model are inert. Human output
includes the same evidence and scope. Source values/commands are not echoed.
Paths, service names and target names are shown, so the report is not a general
redaction tool.

| Exit | Meaning |
| --- | --- |
| 0 | Completed; threshold not reached. **Does not mean compatible.** |
| 1 | Findings reached `--fail-on error` (default) or `--fail-on warning` |
| 2 | Invalid/unsupported input, unsupported provider/version syntax, I/O or CLI error; no audit result |

`--fail-on never` suppresses findings-based exit 1 only. It cannot suppress input
errors. JSON-mode errors have `assessment: not-evaluated` and `error` instead of
an empty successful findings array. Human errors go to stderr; JSON errors go
to stdout. Help/version exit 0.

Reproducible failure examples (the shell assertions themselves succeed):

```sh
stackwrench podman-audit examples/podman_audit/structural-errors.yaml --provider podman-compose --provider-version 1.6.0 --json
test "$?" -eq 1
stackwrench podman-audit examples/podman_audit/unsupported.yaml --provider podman-compose --provider-version 1.6.0 --json
test "$?" -eq 2
```

The structural fixture yields four structural errors and one provider warning.
Nothing removes or weakens a readiness gate automatically.

## Explain a finding offline

Add `--explain` to either output format to trace **each emitted finding** to its
checked fields, selected provider/version, evidence scope and local catalog rule:

```sh
stackwrench podman-audit examples/podman_audit/health-dependency.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/healthcheck-options.yaml --provider podman-compose --provider-version 1.6.0 --explain
```

Both synthetic fixtures exit 0 with one warning each. The first explains PC1541
at `$.services.api.depends_on.db.condition`; the second explains PC1500 at
`$.services.db.healthcheck.start_interval`. Each has
`explanation.evidenceScope.observedVersions: ["1.6.0"]` and `versionMatch: exact`.
The existing evidence context records Podman **4.9.3**; it is an observation,
not a detected or selected runtime version. The dependency finding's existing
`path` still identifies the dependency entry; `explanation.fieldPaths` identifies
the precise condition checked.

In JSON, `explanation` contains:

- `fieldPaths` and `reason`: inspected paths and why the rule applies. A related
  healthcheck path can be absent when testing for a missing declaration. CAT001
  is target-wide and has an empty path list.
- `fieldLocations`: one entry per `fieldPaths` item, in the same order, with
  `path`, `declaration`, `line`, and `column`. YAML and JSON positions are
  **1-based Unicode character** coordinates, pointing to the start of the
  mapping key token (including its opening quote when quoted), or to the start
  of the list item token for a short dependency. Columns are not UTF-8 byte
  offsets or tab-expanded display widths. UTF-8 file BOMs are removed before
  parsing; LF and CRLF line endings are supported.
  `declaration: present` has integer coordinates; `declaration: absent` has
  `line: null, column: null`. There is no invented position on a parent for an
  absent field. CAT001 has an empty location list.
- `target`: the selected `provider` and `version`, separate from observed versions.
  `evidenceScope.versionMatch` is `exact`, `unknown`, or `not-applicable` for
  structural rules. Structural observations do not establish provider support.
- `localEvidence`: package `stackwrench.podman_audit`, resource `catalog.json`, a
  `jsonPointer` such as `/rules/PC1541`, and SHA256 of the **entire resource's raw
  bytes**. It identifies the bundled **curated catalog**, not an upstream API
  response, upstream content checksum, or a new runtime reproduction. The source
  URL, retrieval date, observation context and uncertainty remain in the report.
- `adviceScope`: `runtimeVerificationRequired: true` and
  `guaranteesRuntimeSuccess: false`. The existing `suggestion` provides bounded
  review steps; keeping a readiness gate or reviewing startup cadence cannot
  guarantee that migration succeeds.

The resource is installed in the wheel. Read it without this checkout or network
access, and compare its hash with the report:

```sh
python -c 'from importlib.resources import files; import hashlib, json; raw = files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes(); print(hashlib.sha256(raw).hexdigest()); print(json.loads(raw)["rules"]["PC1541"])'
```

For `examples/podman_audit/health-dependency.yaml`, PC1541 includes this exact location:

```json
{"path":"$.services.api.depends_on.db.condition","declaration":"present","line":6,"column":9}
```

PC1541 explanations retain `.condition` first and additionally locate a present
long-entry `.required` key. The reason distinguishes `required: false` (an optional
dependency), `required: true` (a required dependency), and an omitted key (defaults
to required). The omitted default invents no `.required` field or location.
This is declared healthy-gate requirement context, not evidence that optional
dependencies are enforced, a probe ran, or runtime compatibility is established.

```sh
stackwrench podman-audit examples/podman_audit/healthy-required.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/healthy-required.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both synthetic examples exit **0**. Three healthy dependencies target active
declared healthchecks; the `service_started` edge produces no PC1541. The audit
has three warnings. Comparison has three changed capabilities and four warnings,
including the right-side CAT001 coverage warning. Selected targets remain
`podman-compose` **1.6.0** / **1.6.1**, with `reported-risk` / `unknown` judgments
and exact / unknown evidence matches; the declaration does not change them.

| Dependency under `$.services["api-v1"].depends_on` | YAML condition / required | JSON condition / required |
| --- | --- | --- |
| `["db.optional"]` (`required: false`) | **6:9 / 5:9** | **7:11 / 6:11** |
| `["db.required"]` (`required: true`) | **8:9 / 9:9** | **10:11 / 11:11** |
| `["db.default"]` (omitted) | **11:9 / no key** | **14:11 / no key** |

Condition remains first even when `required` appears earlier in the source.
JSON and human audit/comparison output preserve these original key coordinates
without exposing test commands. Counts, ordering, thresholds, structural
explanations and output without `--explain` are unchanged. `--fail-on warning`
exits **1**, and `--fail-on never` exits **0**. Nonboolean `required` and malformed
healthcheck values still exit **2**, including with `--explain --fail-on never`.

An absent Compose healthcheck remains unknown because the image may supply one.
DEP002 marks its healthcheck path as an absent declaration. When a healthcheck
block exists without a test, it retains the block path and additionally checks
the absent `.test` path. For a minimal example:

```sh
stackwrench podman-audit examples/podman_audit/inherited-healthcheck.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
```

Expected: exit 0, PC1541 and DEP002 warnings. DEP002's `fieldLocations` contains
the condition at **6:9**, the healthcheck key at **8:5**, and
`{"path":"$.services.db.healthcheck.test","declaration":"absent","line":null,"column":null}`.
Human `--explain` output prints each position or `absent declaration` too.
An explicitly disabled check retains the condition and present healthcheck block,
then identifies the disabling key: `.disable` when true, `.test` when the test is
the list `[NONE]`, or both in that order when both apply. Each key has its own
source coordinate. `disable: false` is not a disabling declaration, and the
string `"NONE"` is a command rather than the disabling list. A disabled check
remains an error; the reason describes declarations, not a runtime health result.
Test command contents are not copied into explanations.

The matching synthetic YAML/JSON fixtures illustrate both disabling forms:

```sh
stackwrench podman-audit examples/podman_audit/disabled-healthchecks.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/disabled-healthchecks.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both exit **1**, with two DEP002 errors. The single-target audit has two PC1541
warnings; comparison has two changed capability rows and a right-side CAT001
warning (`summary.error: 2`, `summary.warning: 3`). The disabling fields are
`$.services.cache.healthcheck.test` at YAML **10:7**, JSON **15:9**, and
`$.services["db.primary"].healthcheck.disable` at YAML **13:7**, JSON **22:9**.
JSON and human output retain those positions. Shared comparison explanations
omit the provider/version `target`; builtin assessment explanations retain it.
Without `--explain`, output and thresholds are unchanged. Malformed disabling
fields (for example `disable: "true"` or `test: [NONE, extra]`) still exit **2**,
even with `--fail-on never`. No image inspection or runtime result is inferred.

DEP002 also appends a present dependency `.required` key **after** those existing
condition, block, disabling and absent-test fields. Its static reason distinguishes
explicit false (optional), explicit true (required), and an omitted key (required
by default). An omitted default invents no `.required` path or location. Optional
context does not suppress DEP002 or lower its severity: an explicitly disabled
target remains an error; no declared test remains an unverified warning because
an inherited image HEALTHCHECK is unknown. This describes declarations, not
runtime enforcement, probe execution or compatibility.

```sh
stackwrench podman-audit examples/podman_audit/target-required.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/target-required.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both synthetic examples exit **1**. Each of `api.default`, `api.optional` and
`api.required` targets four states: `disable: true`, `[NONE]`, an absent healthcheck,
and a block without a test. Across those edges there are six DEP002 errors and
six DEP002 warnings. An active-check edge and a `service_started` edge produce no
DEP002. The audit also has thirteen PC1541 warnings (`summary.warning: 19`);
comparison has thirteen changed capabilities and the right-side CAT001 warning
(`summary.error: 6`, `summary.warning: 20`). Provider targets/judgments stay
1.6.0 / reported-risk and 1.6.1 / unknown; shared explanations omit `target`.

For the `disabled` edge, exact condition / healthcheck / disable / required
coordinates are YAML **16:9 / 49:5 / 50:7 / 15:9** for `api.optional`, and
**32:9 / 49:5 / 50:7 / 33:9** for `api.required`. Strict JSON equivalents are
**23:11 / 72:7 / 73:9 / 22:11** and **46:11 / 72:7 / 73:9 / 47:11**.
`api.default` locates no required key. Both human and JSON formats retain these
positions, absent markers and the original disabling order. Counts, sorting,
thresholds and output without `--explain` remain unchanged. `--fail-on never`
exits **0**; malformed required/healthcheck inputs still exit **2**.

For DEP001, the explanation retains the missing dependency entry and additionally
locates an explicit long-entry `.required` key. Its reason distinguishes
`required: false` (warning), `required: true` (error), an omitted long-entry key
(defaults to required/error), and short-form dependencies (required/error).
Implicit requirement defaults invent no `.required` declaration or coordinate.
Long entries additionally locate their declared `.condition` key after the edge
and any explicit `.required` key. Reasons name `service_started`,
`service_healthy` or `service_completed_successfully` as declarations only.
Short-list dependencies retain only the list-item field: their started/required
defaults invent neither key. An absent target remains a document observation,
with no runtime ordering, completion, health, recovery or provider enforcement
inferred. For matching synthetic YAML/JSON examples:

```sh
stackwrench podman-audit examples/podman_audit/missing-required.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/missing-required.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both exit **1**, with three DEP001 errors and one DEP001 warning. The audit also
has one PC1541 warning; comparison has one changed capability and a right-side
CAT001 warning (`summary.error: 3`, `summary.warning: 3`). The explicit keys are
`$.services["api-v1"].depends_on.optional_missing.required` at YAML **7:9**, JSON
**8:11**, and `$.services["api-v1"].depends_on.required_missing.required` at YAML
**10:9**, JSON **12:11**. Both report formats retain those coordinates; shared
comparison explanations omit the selected provider/version target. Finding
order, severity, judgments, thresholds and output without `--explain` remain
unchanged. Non-boolean `required` values, including `"false"`, `0` and `null`,
still exit **2** even with `--fail-on never`.

The matching `missing-conditions.yaml` / `.json` fixtures also exercise all
three long conditions, present targets, a self-cycle and an incoming acyclic
edge:

```sh
stackwrench podman-audit examples/podman_audit/missing-conditions.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/missing-conditions.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both exit **1**. The four DEP001 findings remain three errors and one warning;
existing targets emit no DEP001. There is also one DEP003 self-cycle error, one
DEP002 inherited-check warning and four provider warnings (three PC1541 and one
PC1500). Audit totals are **4 errors, 6 warnings**; comparison has **4 changed
capabilities, 4 errors, 7 warnings**, including right-side CAT001. Fields below
are in explanation order under `$.services["api-v1"].depends_on`:

| Entry | YAML edge / required / condition | JSON edge / required / condition |
| --- | --- | --- |
| `optional_missing` (healthy, false) | 5:7 / 7:9 / 6:9 | 6:9 / 8:11 / 7:11 |
| `required_missing` (started, true) | 8:7 / 9:9 / 10:9 | 10:9 / 11:11 / 12:11 |
| `implicit_missing` (completed, default true) | 11:7 / none / 12:9 | 14:9 / none / 15:11 |

The missing short-list field `$.services.worker.depends_on[0]` is YAML **20:9**,
JSON **28:9**, with no required or condition location. Human and JSON audit and
comparison formats retain these exact coordinates. Shared explanations remain
independent of selected targets, and existing edge/required field order,
severity, graph membership, judgments, counts, thresholds and plain output stay
unchanged. Malformed condition, required and healthcheck values exit **2** even
with `--fail-on never`; valid examples exit **0** with that threshold.

For DEP003, the explanation retains the cycle-edge entry and any explicit
long-entry `.required` key, then appends the declared `.condition` key. The reason
names the declared condition (`service_started`, `service_healthy` or
`service_completed_successfully`) and distinguishes explicit true/false,
an omitted long-entry requirement default, and short-form defaults. A short-list
edge defaults to `service_started` and required, but invents neither key.
All declared edge conditions and optional edges count in the graph:
`required: false` does not remove an edge from cycle detection or lower a cycle
error's severity. These are declarations, with no runtime ordering, completion,
health, recovery or provider enforcement inferred. Matching synthetic examples
include a four-service cycle, an optional self-cycle and an incoming acyclic edge:

```sh
stackwrench podman-audit examples/podman_audit/cycle-conditions.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/cycle-conditions.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both exit **1**, with five DEP003 errors. The audit also has one PC1541 warning;
comparison has one changed capability and a right-side CAT001 warning
(`summary.error: 5`, `summary.warning: 2`). In finding order, locations are
**line:column**, with fields ordered entry / required / condition:

| Edge and declared condition | YAML entry / required / condition | JSON entry / required / condition |
| --- | --- | --- |
| `job -> job`, completed, `required: false` | **29:7 / 30:9 / 31:9** | **45:9 / 46:11 / 47:11** |
| `optional -> required`, started, `required: false` | **8:7 / 9:9 / 10:9** | **12:9 / 13:11 / 14:11** |
| `required -> short`, healthy, `required: true` | **13:7 / 15:9 / 14:9** | **20:9 / 22:11 / 21:11** |
| `short -> a.b`, short list | **20:9 / no key / no key** | **31:9 / no key / no key** |
| `a.b -> optional`, completed, omitted required | **4:7 / no key / 5:9** | **5:9 / no key / 6:11** |

Here, started/healthy/completed abbreviate the three condition values above.
The incoming `outside -> a.b` edge produces no DEP003 finding. Human and JSON
audit/comparison reports retain these coordinates; shared comparison explanations
omit selected provider/version targets. Commands are not disclosed. Cycle
membership, severity, counts, ordering, judgments, thresholds and output without
`--explain` are unchanged. `--fail-on never` exits **0** for these findings;
malformed or missing long-entry `condition`, nonboolean `required`, and malformed
healthcheck values still exit **2**.

For PC1500, a declared `healthcheck.start_period` is located after `start_interval`
and any disabling fields, with exact original YAML/JSON key coordinates. The
reason names only its presence as static declaration context: no timing,
execution, health result or provider enforcement is inferred. The duration
string `"0"` counts as present; an omitted key gains no path or location, and
`start_period` alone emits no PC1500. Existing findings, disabled states,
`details.startPeriodDeclared`, judgments, evidence scopes and plain output are
unchanged. Matching [YAML](../examples/podman_audit/start-period-context.yaml) and
[JSON](../examples/podman_audit/start-period-context.json) fixtures include active, singly and
doubly disabled checks, omitted keys and the start-period-only negative case.
Audit and builtin 1.6.0/1.6.1 comparison explanations share this contract in
human and JSON output. Numeric `0` remains an invalid duration (exit **2**);
use the documented duration strings.

```sh
stackwrench podman-audit examples/podman_audit/start-period-context.yaml --provider podman-compose --provider-version 1.6.0 --explain --json --fail-on never
stackwrench podman-audit examples/podman_audit/start-period-context.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain --fail-on never
```

Both exit **0** with `--fail-on never`; the disabled healthy target remains a
structural error at the default threshold (exit **1**). Service `both` has
`start_interval`, `disable`, `test`, `start_period` at YAML **6:7**, **7:7**,
**5:7**, **4:7**, or JSON **9:9**, **10:9**, **6:9**, **5:9**, respectively.

For PC1500 on a disabled healthcheck, the explanation retains `start_interval`
first, then locates `.disable` when true and `.test` when `[NONE]`, in that
order when both apply. The reason names only the active disabling declarations
and describes an option declaration, not a running probe. `disable: false`,
the string `"NONE"` and active test commands add no disabling fields or values.
The matching synthetic fixtures exercise both causes, both together, and an
active check:

```sh
stackwrench podman-audit examples/podman_audit/disabled-start-interval.yaml --provider podman-compose --provider-version 1.6.0 --explain --json
stackwrench podman-audit examples/podman_audit/disabled-start-interval.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both exit **0** at the default error threshold. The audit has four PC1500
warnings; comparison has four changed capabilities and a right-side CAT001
warning (`summary.error: 0`, `summary.warning: 5`). For service `both`, the
`healthcheck` fields are `start_interval`, `disable`, `test` at YAML **5:7**,
**6:7**, **4:7**, or JSON **8:9**, **9:9**, **5:9**, respectively. Both output
formats retain these coordinates. Builtin comparison explanations retain each
selected provider/version target and exact/unknown evidence scope; shared
structural explanations remain target-independent. Counts, severity, judgments,
ordering and output without `--explain` are unchanged. `--fail-on warning`
exits **1**; malformed healthcheck fields still exit **2**, including with
`--fail-on never`.

The Python loaders return a `dict` subclass with source metadata outside the
model's keys; equality, serialization and profile hashes still use only data.
Coordinates refer to the **original loaded source**: reload edited source before
requesting locations. Converting to a plain `dict` drops the metadata and yields
`declaration: unavailable` with null coordinates, rather than guessing absence.
No filename, source line, command value or source content is added to locations.

Selecting `1.6.1`, a prerelease or a build-metadata variant produces `unknown`
matches and explicitly unverified compatibility. An unsupported provider fails
with exit 2 and `unknown-provider`, explicitly stating compatibility is unverified;
it never falls back to podman-compose. Invalid input/version syntax still yields
`assessment: not-evaluated` in JSON, even with `--explain --fail-on never`.
Explanation mode preserves finding counts, severities and exit thresholds, and
does not echo source command values. Without the flag, reports retain their
existing shape. The Python interface accepts the equivalent `audit(...,
explain=True)` option.

## Compare two explicit provider profiles offline

Use `--compare-profiles LEFT RIGHT` instead of `--provider` and
`--provider-version`. Both profiles are explicit local files, independent of the
Compose service `profiles` directive (which remains unsupported). This mode
compares the **same Compose document** under two configurations. It never runs a
provider or infers support from an empty finding list.

```sh
stackwrench podman-audit examples/podman_audit/compose.yaml --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --json
stackwrench podman-audit examples/podman_audit/compose.yaml --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/custom.json
```

Both commands exit 0 by default. The first uses built-in observations for exact
`1.6.0` versus the unobserved `1.6.1`. Its two capability rows, in report order:

| Checked field | Rule | Left judgment | Right judgment | Changed |
| --- | --- | --- | --- | --- |
| `$.services.api.depends_on.db.condition` | PC1541 | reported-risk | unknown | true |
| `$.services.db.healthcheck.start_interval` | PC1500 | reported-risk | unknown | true |

`unknown` means missing matching evidence, **not pass or a fixed release**.
Prereleases and build metadata likewise require exact observations. Right-side
CAT001 remains in `profileFindings.right`, including when the Compose document
uses neither capability. Compatibility always remains `not-established`.

A minimal one-row contrast uses `examples/podman_audit/healthcheck-options.yaml` with these
two checked-in files (shown in full):

```json
{"schemaVersion":1,"kind":"builtin","provider":"podman-compose","version":"1.6.0"}
```

```json
{"schemaVersion":1,"kind":"custom","provider":"podman-compose","version":"1.6.1","rules":{"PC1500":"supported"}}
```

```sh
stackwrench podman-audit examples/podman_audit/healthcheck-options.yaml --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/custom.json --json
```

Expected: exit 0, `summary.capabilities: 1`, `summary.changed: 1`,
`capabilities[0].ruleId: "PC1500"`, and `reported-risk -> supported`. The left
basis is `builtin-observation`; the right basis is `custom-assertion`, with
`evidence: null`. The custom fixture is **synthetic input**, not evidence that
`1.6.1` supports this behavior. In the two-row example its omitted PC1541 rule
stays `unknown` with basis `custom-unspecified`, even if a custom profile selects
the observed `1.6.0` version. Custom profiles do not inherit catalog observations.

The strict v1 profile contract is:

- Required keys: integer `schemaVersion: 1`, `kind: builtin` or `custom`,
  `provider: podman-compose`, and an exact SemVer `version`. Other providers are
  rejected in this bounded catalog, including in custom profiles.
- Built-in profiles have exactly those four keys. Custom profiles additionally
  require `rules`, a mapping (possibly empty) of PC1541 and/or PC1500 to either
  a judgment string (`reported-risk`, `supported`, or `unknown`) or an assertion
  object. Objects require `judgment` with one of those same values and allow only
  two optional keys: `evidenceReferences` (a nonempty list of nonblank strings)
  and `observationNotes` (a nonblank string). Either optional key may appear
  independently. Text and reference order are preserved, including Unicode and
  multiline notes; strings are checked for nonblank content, not trimmed.
  These are profile assertions about each rule's capability, not overrides of
  structural checks. Existing string assertions remain valid with unchanged output.
- Reject unknown keys/rules/judgments, wrong types (including boolean schema
  versions), missing keys, duplicate keys, invalid syntax, and non-finite values.
  YAML profiles use the same restricted safe YAML loader as Compose input:
  no tags, anchors, aliases, merge keys, interpolation or multiple documents.
  Each file has the same 2 MiB, depth and node limits.

For example, replacing `"supported"` with `"pass"` is invalid: exit **2**,
`assessment: not-evaluated`, `error.code: invalid-profile`,
`error.path: $.rules.PC1500`, and no comparison result. `--fail-on never` cannot
suppress this failure. Mixing profile comparison with either target flag also
exits 2.

For custom observation context, `examples/podman_audit/profiles/custom-evidence.json` contains
this **synthetic assertion**, not a newly verified provider observation:

```json
{"schemaVersion":1,"kind":"custom","provider":"podman-compose","version":"1.6.1","rules":{"PC1500":{"judgment":"supported","evidenceReferences":["https://example.invalid/observations/start-interval","local-run-17"],"observationNotes":"Synthetic example only; no runtime verification was performed."}}}
```

```sh
stackwrench podman-audit examples/podman_audit/healthcheck-options.yaml --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/custom-evidence.json --json
stackwrench podman-audit examples/podman_audit/healthcheck-options.yaml --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/custom-evidence.json
```

Expected: exit 0 and one changed row (`reported-risk -> supported`). The right
assessment retains `basis: custom-assertion`, `evidence: null`, and its custom
profile hash/pointer. It additionally contains `evidenceReferences` and
`observationNotes` exactly as supplied. Human output labels them **Custom evidence
references** and **Custom observation notes**, with JSON-escaped text so newlines
and control characters cannot introduce report lines. Both appear without
`--explain`; omitted metadata keys are absent, not filled from the catalog.
References are opaque text (URLs, paths or identifiers): never opened, fetched,
executed, verified or treated as built-in observations. These user-supplied
strings are deliberately displayed and are not redacted.

Missing object `judgment`, unknown object keys, wrong types, empty reference lists
and blank text fail with `invalid-profile` at the offending path. For example,
`{"judgment":"supported","evidenceReferences":[""]}` exits **2** with
`error.path: $.rules.PC1500.evidenceReferences[0]`, even with `--fail-on never`.
Built-in profiles still reject `rules` or metadata; structural rule IDs cannot
carry custom assertions. Restricted parsing, interpolation rejection and file
limits apply to metadata too.

JSON `capabilities` includes changed **and unchanged** applicable provider-rule
judgments, sorted by checked field path then rule ID. `changed` compares only
judgments; metadata-only edits do not mark a row changed or affect thresholds.
Provenance remains visible even when judgments agree. These rows are
not a support matrix for all Compose fields. Built-in assessments retain the
catalog evidence and checksummed local reference. Custom local references hash
the **canonical parsed profile** (SHA256 of UTF-8 `json.dumps(profile,
sort_keys=True, ensure_ascii=True, separators=(",", ":"))`), not raw file bytes;
the hash includes all custom metadata, and the JSON Pointer identifies the whole
explicit assertion (string or object). Omitted assertions have no
local reference. `--explain` adds built-in finding explanations to capability
assessments and coverage findings, retaining each profile's selected `target`.
It also adds source explanations to shared, version-independent
`structuralFindings` (DEP001/DEP002/DEP003). Shared explanations contain
`fieldPaths`, `fieldLocations`, `reason`, `localEvidence`, `adviceScope` and
`evidenceScope` with `kind: structural`, `provider: null`, `observedVersions: []`,
and `versionMatch: not-applicable`. They **omit `target`**: neither profile is
selected for a shared structural observation. Swapping profiles leaves shared
findings identical. The `details.target` service name still identifies the
dependency's Compose target, not a provider/version.

For a source-navigation example with a missing short dependency and two healthy
dependencies whose checks may be inherited from images:

```sh
stackwrench podman-audit examples/podman_audit/comparison-structural.yaml --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain --json
stackwrench podman-audit examples/podman_audit/comparison-structural.json --compare-profiles examples/podman_audit/profiles/observed.json examples/podman_audit/profiles/unknown.json --explain
```

Both synthetic examples exit **1**, with two changed PC1541 capability rows,
one shared DEP001 error, two shared DEP002 warnings and a right-side CAT001
warning (`summary.error: 1`, `summary.warning: 5`). Shared locations, in order:

- DEP001 `$.services.api.depends_on[0]`: YAML **4:9**, JSON **5:9**.
- DEP002 `$.services.worker.depends_on.cache.condition`: YAML **8:9**, JSON
  **11:11**; `$.services.cache.healthcheck` is an absent declaration.
- DEP002 `$.services.worker.depends_on.db.condition`: YAML **10:9**, JSON
  **14:11**; `$.services.db.healthcheck` is at YAML **13:5**, JSON **20:7**;
  `$.services.db.healthcheck.test` is an absent declaration.

Absent locations have null line/column. Human comparison output retains these
same objects in each shared finding's JSON detail line. Missing tests remain
unverified because images may declare a HEALTHCHECK; structural evidence is the
checksummed local catalog reference, not a runtime or image observation.
Without `--explain`, comparison reports keep their existing shape and output.
The flag does not affect sorting, counts or failure thresholds. Malformed
documents/profiles still exit **2**, including with `--fail-on never`.

Default exit 1 still applies to structural errors. `--fail-on warning` also
counts each capability with either side `unknown` or `reported-risk`, each
profile's CAT001, and shared structural warnings. `supported` on both sides
adds no capability warning, but never establishes runtime compatibility. Human
output contains the same judgments, provenance, findings and unevaluated fields.
The Python API is `compare_profiles(model, left, right, explain=False)` from
`stackwrench.podman_audit.compare`; load all three mappings through
`load_document`/`read_document` first.

## Input contract and deliberately limited scope

Read exactly **one regular local UTF-8 Compose file**, one document, plus two
explicit local profile files in comparison mode. `.json` filenames
use strict JSON (no NaN/Infinity); other filenames use a restricted safe YAML
loader. No stdin, URLs, implicit discovery, `.env`, Dockerfile, image metadata,
external files, environment expansion or multi-file merge. `COMPOSE_FILE`,
`DOCKER_HOST`, and host environment values cannot select or resolve input.

- Require a nonempty `services` mapping. Service/dependency names match
  `[A-Za-z0-9][A-Za-z0-9_.-]*`. A service definition must be a mapping. This tool
  does not require `image`, because it does not validate full Compose schemas.
- Inspect short dependency lists and long dependency maps. Long entries require
  `condition` equal to `service_started`, `service_healthy`, or
  `service_completed_successfully`. Optional `required` and `restart` are strict
  booleans, defaulting to true and false respectively. Completion/restart runtime
  semantics are not simulated. Short-list duplicates are errors.
- Inspect healthcheck shape: `test` is a nonempty string or a list beginning with
  `CMD`, `CMD-SHELL`, or `NONE`; `NONE` is alone, `CMD` requires a command,
  `CMD-SHELL` requires one command string. `disable` is a strict boolean;
  `retries` is a nonnegative integer. Durations (`interval`, `timeout`,
  `start_period`, `start_interval`) accept nonnegative sequences with units
  `us`, `ms`, `s`, `m`, `h`, or string `"0"`. Other duration spellings, including
  numeric scalars, are rejected. No healthcheck command or timing is executed.
- Accept these **opaque, not evaluated** service fields: `image`, `command`,
  `entrypoint`, `environment`, `labels`, `ports`, `expose`, `volumes`, `networks`,
  `restart`, `container_name`, `user`, `working_dir`, `read_only`, `init`, `tty`,
  `stdin_open`, `stop_signal`, `stop_grace_period`, `hostname`, `domainname`,
  `shm_size`, `secrets`, `configs`. Their types, contents, referenced resources,
  schema and provider behavior are **not validated**. Each field is explicitly
  listed under `scope.notEvaluated` (also in human output).
- Top-level `name`, `version`, `networks`, `volumes`, `secrets`, `configs`, and
  `x-*` extensions at top/service levels are likewise listed as not evaluated.
  Extension content is inert. All other top-level/service directives and all
  unknown dependency/healthcheck options are rejected with an exact path.
- In particular, reject `include`, `extends`, `profiles`, `build`, `links`,
  `volumes_from`, `network_mode`, `ipc`, `pid`, `provider`, `deploy`, `develop`,
  lifecycle hooks and jobs. Some introduce implicit dependencies or incomplete
  model resolution. Do not delete them merely to obtain an empty report; only
  audit a complete model representable within this subset.
- Reject unresolved `$NAME` and `${...}` anywhere in keys or values, including
  default-value forms and single-quoted/extension strings. This is deliberately
  conservative. `$$` escapes are accepted and left untouched. Command
  substitutions such as `$(...)` are inert data; never executed.
- Reject duplicate mapping keys in YAML/JSON, non-string mapping keys, explicit
  YAML tags (including `!reset`/`!override`), anchors, aliases and merge keys.
  YAML scalars use true/false booleans, decimal numbers and null; timestamps,
  yes/no/on/off, octal and sexagesimal forms remain strings. This is a restricted
  YAML subset, not a promise of every YAML 1.2 spelling.
- Limits: 2 MiB input, 64 nesting levels, 50,000 YAML events/JSON tree nodes
  (keys count toward the latter), 500 services, 10,000 dependency edges.

The audit never claims a full schema check, support matrix, security scan,
performance improvement or runtime reproduction. Runtime, host, image and
release differences must be verified separately. Optional edges participate in
the declared-graph cycle check; the tool makes no claim about provider-specific
cycle recovery. A healthcheck missing from Compose is not proof that the image
has no check. A `start_interval` declaration is flagged even on a disabled check
as a declared-option review item; it does not mean that a disabled probe runs.

The small Python interface uses the same contract:

```python
from stackwrench.podman_audit.input import load_document
from stackwrench.podman_audit.audit import audit

model = load_document("services: {api: {depends_on: [missing]}}")
report = audit(model, provider="podman-compose", provider_version="1.6.0")
assert report["findings"][0]["ruleId"] == "DEP001"
```

Load through `load_document`/`read_document` before passing a model to `audit`.
`InputError` carries a structured `.detail`; no input line or value is included
in parser errors.

## Evidence, upstreams and related tools

The bundled catalog was checked on **2026-11-11**. Both provider reports identify
**podman-compose 1.6.0 / Podman 4.9.3**. [#1541](https://github.com/containers/podman-compose/issues/1541)
reports a healthy dependency startup hang; [#1500](https://github.com/containers/podman-compose/issues/1500)
reports a startup interval mapping/cadence discrepancy and discusses proposed
emulation. Both were open at retrieval. This tool did not reproduce them, and
an issue's open/closed state is not a version-range oracle. Prefixes, prereleases
and build metadata do not match `1.6.0`; exact SemVer is required without `v`.

[Docker Compose](https://github.com/docker/compose) is a full Compose implementation;
its config parsing is not proof of identical provider runtime behavior.
The [Compose Specification](https://github.com/compose-spec/compose-spec) supplies
format semantics. Both are Apache-2.0. This CLI neither embeds nor invokes them.
[podman-compose](https://github.com/containers/podman-compose) is the migration
target, not a dependency. Its validation-related [#1388](https://github.com/containers/podman-compose/issues/1388)
is closed/completed; we do not claim upstream lacks validation.
[ComposeLens](https://raw.githubusercontent.com/Strukturpiloten/compose-lens/main/README.md)
already offers much broader Compose processing and compatibility validation;
[podlet](https://github.com/containers/podlet) converts to Quadlet. This tool's
narrow purpose is an inspectable evidence catalog with exact service paths and
explicit uncertainty, not a replacement for those projects. No GPL or MPL
source is reused. See [THIRD_PARTY.md](../THIRD_PARTY.md).
