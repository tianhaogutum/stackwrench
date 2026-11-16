# stackwrench cache-replay

`stackwrench cache-replay` (formerly `docker-cache-replay`) is an offline command
that explains which Dockerfile instructions a declared file
change would invalidate in a bounded, linear COPY dependency model. Compare a
broad COPY before an expensive RUN with a manually staged candidate using the
same deterministic change scenarios. Results are **model units**, never measured
build seconds. No Docker installation, daemon, credentials or network is needed.

It is an independent original implementation. It does not
execute RUN commands, rewrite Dockerfiles, read context file contents, modify the
context, resolve images, or build/pull/start containers.

## Install

Requires Python **3.12+** (the context scan uses `pathlib.Path.walk`). This command
has no runtime dependencies beyond the standard library.

```sh
pipx install stackwrench              # or: pip install stackwrench
stackwrench cache-replay --version    # prints the bare stackwrench version, e.g. 0.1.0

# Or run without installing:
uvx stackwrench cache-replay --help
```

`python -m stackwrench cache-replay` is equivalent to `stackwrench cache-replay`.
There are no subcommands. The examples below use files from a stackwrench source
checkout; run them from its root:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before \
  --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.json \
  --context examples/cache_replay/context --json
```

Weighted replay with readable reason chains:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json
```

The invalid fixture is deliberately rejected with exit **2** and no stdout
comparison (the test suite asserts this):

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.invalid.json --context examples/cache_replay/context --json
```

By default, exit 0 means all scenarios were evaluated, even if the candidate
has higher modeled costs. With `--fail-on-regression`, exit 1 means at least
one displayed scenario has negative saved model units; otherwise exit 0.
Exit 2 means invalid/unsupported input or CLI usage, with the reason on stderr
and empty stdout. There is no partial success report.
Library callers can use `stackwrench.cache_replay.model.compare` with `pathlib.Path`
arguments and optional keyword `mode="cumulative-history"` or
`mode="independent-cold"` or `mode="cumulative-cold"` (default:
`"independent-warm"`) and `scenario="ID"` (default: `None`, show all);
unsupported inputs raise `InputError` (filesystem failures can raise
`OSError`).

## Display one scenario

Add `--scenario ID` in any mode to display exactly one scenario, including
both Dockerfiles' complete step explanations, inventory/cache counts, triggers
and costs. The caller-supplied candidate diff, instruction inventories and
assumptions remain in the report. IDs match exactly and are case sensitive,
using the same 1–80 ASCII letters/digits/`_.-` grammar as input scenario IDs.

This is a report filter: **every input scenario is validated and evaluated** in
array order before selection. In cumulative history, earlier inventory changes,
revision IDs and retained cache states still affect the selected scenario.
Later scenarios are also evaluated. Invalid input anywhere rejects the entire
report with exit 2, stderr and empty stdout, including no candidate diff.
An invalid selector or unknown ID also fails with exit 2 and empty stdout;
library callers receive `InputError`. Selection does not skip unsupported input
or reduce replay work. Without the option, the report is unchanged.

Filtered JSON includes only the selected entry in `scenarios`; `totals` sums
only that displayed scenario. It also adds
`selection: {"scenarioId": "ID", "evaluatedScenarioCount": N, "displayedScenarioCount": 1}`.
Text labels the filter and evaluated count, and labels the total as covering
the displayed scenario. The full history's aggregate costs are not included.
Schema version stays 1; unfiltered reports have no `selection` field.

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.history.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --mode cumulative-history \
  --scenario remove-added-source --json
```

Expected: one displayed scenario out of five evaluated, inventory 5 → 4,
all 5 before / 6 after instructions `synthetic-hit`, and totals 0/0/0 model
units. The earlier `add-source` supplies the file being deleted; retained
states from `manifest-edit` allow reuse. Selecting `source-edit` from
`examples/cache_replay/changes.json` in the default independent mode instead gives
110/10/100 model units, rather than the full input's 440/140/300.
Selecting `declare-v1` from `examples/cache_replay/changes.revisions.invalid.json`
still fails on the later contradictory event; it cannot produce a partial report.

## Display a set of scenarios

Use `--select-scenarios ID [ID ...]` in any of the four modes. It is mutually
exclusive with `--scenario`. Supply a nonempty set of unique exact existing
IDs using the same case-sensitive grammar. Duplicate IDs (including across
repeated option groups), unknown IDs, missing values and combining selectors
fail atomically with exit 2, stderr and empty stdout.

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.initial-revisions.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --mode cumulative-history \
  --select-scenarios edit-v2 revert-initial --json
```

This displays `edit-v2`, then `revert-initial`, with before/after/saved totals
of **110/10/100 model units**. Reversing the two selector arguments gives the
same report: source array order always governs output. In `cumulative-cold`,
the same selection gives **220/120/100**, because the first edit starts empty
and the initial source revision has not yet been built. Independent modes also
accept this option, with their existing input restrictions; selecting
`source-edit mtime-only` from `examples/cache_replay/changes.json` gives **110/10/100** in
`independent-warm` and **220/220/0** in `independent-cold` with these weights.

Multi-selection adds this top-level JSON metadata instead of `selection`:

```json
"selectedScenarios": {
  "scenarioIds": ["edit-v2", "revert-initial"],
  "evaluatedScenarioCount": 4,
  "displayedScenarioCount": 2
}
```

Text names the selected IDs and labels totals as covering displayed scenarios.
Costs, reuse counts/units and per-instruction miss totals all cover exactly the
displayed set. **Every scenario is still validated and simulated in source
order before filtering**, including hidden later scenarios. Hidden inventory
changes, cache producers and immediate predecessors remain in selected scenario
evidence; filtering does not rebase the history, skip errors, or reduce replay
work/memory. This option changes presentation only, with no Docker cache or
timing claims. Schema 1 and singular/default report bytes remain unchanged.
Selecting one ID or all IDs still includes the multi-selection metadata.

Library callers pass `select_scenarios=["edit-v2", "revert-initial"]` (or a
tuple) to `compare`. The default `None` shows all; an empty list/tuple, other
container or scalar, invalid/non-string ID, duplicate, unknown ID, or combining
it with a non-`None` `scenario` raises `InputError`. Repeated CLI option groups
combine into one selection. `selectedScenarios` is output-only metadata;
changes/weights input schemas are unchanged.

## Fail CI on a modeled cost regression

Add `--fail-on-regression` to any comparison command, with text or `--json`:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --fail-on-regression --json
```

The CLI prints the **same complete report bytes** with or without the flag.
After printing, it exits **1** if any displayed scenario has
`savedModelUnits < 0` (candidate cost exceeds baseline cost), or **0** otherwise.
The predicate uses the existing unrounded numeric value, without a tolerance:
zero costs and exact ties pass; even a tiny negative fraction fails. A saving
scenario cannot offset a regressing scenario for this gate, even when displayed
totals are positive. Stderr remains empty for valid comparisons, including exit 1.
Without the flag, valid comparisons continue to exit 0 regardless of costs.

This works in all four modes with `--scenario` or `--select-scenarios`.
Only displayed scenarios affect the gate; selection arguments still display in
source order. **All scenarios are validated and replayed first**, including
hidden later inputs. Invalid schema, revision, COPY, weight or selector input
takes precedence: exit 2, diagnostic stderr and empty text/JSON stdout.
The flag is CLI-only; it adds no report, library argument or changes-document
field. These are arbitrary weighted model costs, not real build timings or a
claim of measured speedup or slowdown.

For a concrete mixed result, use a baseline `FROM scratch`, `COPY src /app/`,
`RUN baseline` and candidate `FROM scratch`, `COPY docs /docs/`, `RUN candidate`
(each instruction on its own line). With an inventory containing `src/app.js`
and `docs/note.txt`, give baseline `I003` weight 10 and candidate `I003` weight
0.25 (all other weights default to zero). Replay a no-op `prime`, a content edit
to `docs/note.txt` named `regress`, a content edit to `src/app.js` named `save`,
then a no-op `tie`. In independent-warm or cumulative-history the saved costs
are 0, -0.25, 10, 0; cumulative-cold starts with 9.75, then -0.25, 10, 0.
The flag exits 1 even with positive totals. Selecting only `save` exits 0;
selecting `save regress` displays `regress` then `save` and exits 1.
In independent-cold these weights give 9.75 saved for every scenario;
raising candidate `I003` to 10.25 instead gives -0.25 each and exits 1.

## Input contract

`--context` is a **directory used as a path inventory**, including dotfiles.
Only regular files and directories are accepted; no symlinks or special files.
Contents, hashes and timestamps are not read. Supply a small, prefiltered copy
of the effective context. A root `.dockerignore` or a Dockerfile-specific
`<Dockerfile>.dockerignore` next to either input is rejected, even if empty.
No ignore syntax is simulated. Scanning a live changing directory is not a
snapshot; callers must provide a stable inventory.

Dockerfiles accept exactly one stage, starting with FROM:

| Instruction | Accepted subset |
| --- | --- |
| FROM | Lowercase repository path, optional registry/port, tag and/or `@sha256:` with 64 lowercase hex digits, or `scratch`; no alias/flags/global ARG/IPv6 registry. |
| WORKDIR | One unquoted absolute literal POSIX path. |
| COPY | Local file/directory sources, shell words (quotes allowed) or JSON string array, then destination. Multiple sources (including wildcard expansion) require destination ending `/`. |
| RUN | Opaque shell text or nonempty JSON string array; never executed or interpreted. Shell `$VARIABLE` text is allowed here only. |
| ENV | One or more literal `NAME=value` assignments; quoted spaces allowed. |
| ARG | One literal `NAME=default` assignment; no build-argument overrides. |

Backslash line continuations and full-line comments (including comment lines
inside a continuation) are supported. Continuations remove backslash/newline
without inserting a space; indentation on continued lines remains argument text.
No inline comment stripping occurs. Standalone continuation lines are rejected.
Parser directives (`syntax`, `escape`, `check`), ADD, multi-stage FROM, COPY flags
(including `--from`), RUN flags/mounts/heredocs, other instructions, and expansions
in FROM/WORKDIR/COPY/ENV/ARG are explicitly rejected. ENV/ARG declarations remain
fixed; they are not expanded by the model. RUN filesystem or network behavior
does not contribute additional file dependencies. A RUN argument starting with
`[` must be valid JSON; any RUN containing `<<` is rejected, even inside quotes.

COPY source matching is case sensitive, POSIX and component based: `*` and `?`
match within one component, never across `/`. A matching directory includes its
regular descendants; `COPY . .` includes all inventory files. Every source must
match at least one regular file both before and after every scenario. A delete
that removes a sole required source is therefore an error, not a predicted
successful build. Empty-directory-only COPY is unevaluated. Directory metadata
changes are unmodeled.

Brackets, braces, `**`, backslashes, control characters, colons/remote paths,
absolute sources/events, parent traversal and repeated slashes are rejected.
Leading `./` and trailing `/` are accepted for COPY sources; event paths must
already be canonical. This intentionally narrow glob grammar does not claim
full Docker/Go filepath.Match compatibility. Text inputs are UTF-8, at most
5 MiB each; duplicate JSON keys, unknown fields and nonfinite numbers are errors.

Changes JSON has this exact shape:

```json
{
  "schemaVersion": 1,
  "scenarios": [
    {"id": "edit", "changes": [{"path": "src/app.js", "kind": "content"}]},
    {"id": "new", "changes": [{"path": "src/new.js", "kind": "add"}]}
  ]
}
```

By default (`--mode independent-warm`), each scenario starts independently from
the original inventory and assumes a warm cache for **each Dockerfile separately**.
Use `--mode cumulative-history` to advance the inventory and simulated caches in
scenario array order, `--mode cumulative-cold` for ordered replay starting
empty, or `--mode independent-cold` for an empty synthetic cache per scenario,
as described below. All four modes keep the Dockerfiles' caches
separate. Both text and JSON reports label the selected mode. Scenario IDs are unique ASCII
letters/digits/`_.-`, 1–80 characters. An empty changes array is a no-op. Kinds:

- `content`: caller asserts different contents for an existing regular file.
- `mtime`: existing file changed only mtime; ignored for invalidation.
- `delete`: existing regular file disappears in this scenario.
- `add`: canonical new regular file path, absent from the scenario's starting inventory;
  parent directories are inferred. Use this explicitly for new files.

Duplicate paths, unknown content/delete/mtime paths and file/directory conflicts
fail. Renames require a delete plus add. Permission/ownership-only or directory
events are not supported; do not encode them as mtime events. New and deleted
files participate in matching using the union of pre/post COPY selections.
Paths not selected by any COPY are reported as `uncopiedChangePaths`.
Content/add/delete events targeting either input Dockerfile inside the inventory,
root `.dockerignore` or either active Dockerfile-specific ignore file are rejected:
instruction and ignore-rule changes are outside this replay model.

## Independent cold replay

Use `--mode independent-cold` to assume an **empty synthetic cache for each
Dockerfile in every scenario**. Every instruction, including FROM and metadata,
is a `synthetic-miss`. No-op, mtime-only, uncopied changes and repeated scenarios
still miss every instruction. Each scenario starts from the original inventory;
an earlier add cannot supply a later content/delete event. Repeating an add of
the same originally absent path, or a delete of the same originally present
path, is valid independently if every required COPY selection remains valid.

Cold mode validates every source against both pre/post inventories. Deleting
the sole required COPY source remains an error. Unsupported syntax, invalid
weights and all other input checks still apply. `revisionId` and
`initialRevisionIds` (even `{}`) require `cumulative-history` or `cumulative-cold`. `--scenario` remains
presentation only: invalid later input rejects the whole report with exit 2,
stderr and empty stdout. Totals cover only displayed scenarios.

For a small reproducible two-Dockerfile comparison (run from this tool's root):

```sh
cold_dir=$(mktemp -d)
mkdir -p "$cold_dir/context/src"
printf 'inventory placeholder\n' > "$cold_dir/context/src/app.js"
printf 'FROM scratch\nCOPY . /app/\nRUN echo inert\n' > "$cold_dir/before"
printf 'FROM node:22\nCOPY src /app/\nRUN echo candidate\n' > "$cold_dir/after"
cat > "$cold_dir/changes.json" <<'JSON'
{"schemaVersion":1,"scenarios":[
  {"id":"noop","changes":[]},
  {"id":"edit","changes":[{"path":"src/app.js","kind":"content"}]}
]}
JSON
stackwrench cache-replay \
  --before "$cold_dir/before" --after "$cold_dir/after" \
  --context "$cold_dir/context" --changes "$cold_dir/changes.json" \
  --mode independent-cold --json
```

Each of the two scenarios reports three misses, zero hits and 1 model unit per
Dockerfile (default weights: FROM=0, COPY=0, RUN=1). Totals are before=2, after=2,
saved=0. To use the existing per-instruction weights contract, write:

```sh
cat > "$cold_dir/weights.json" <<'JSON'
{"schemaVersion":1,
 "before":{"I001":0.25,"I002":0,"I003":1.5},
 "after":{"I001":2,"I002":0.5,"I003":0}}
JSON
stackwrench cache-replay \
  --before "$cold_dir/before" --after "$cold_dir/after" \
  --context "$cold_dir/context" --changes "$cold_dir/changes.json" \
  --mode independent-cold --weights "$cold_dir/weights.json"
```

Each scenario now costs before=1.75, after=2.5, saved=-0.75; full totals are
3.5/5/-1.5 model units. FROM's weight counts despite its fixed image identity;
zero weights still miss. All instruction weights are summed once in instruction
order, using the existing finite [0, 1e12] range and `:g` text formatting.

JSON keeps schema version 1, `model: "linear-copy-v1"` and the independent report
structure, with `mode: "independent-cold"`. Each side's `cache` is exactly:

```json
{"hitInstructionIds":[],"missInstructionIds":["I001","I002","I003"],
 "entryCountBefore":0,"entryCountAfter":3}
```

End entries describe only that modeled build and are discarded before the next
scenario. Text prints `synthetic cold cache: 0 hits, 3 misses; entries 0 -> 3
(discarded after scenario)` on one line. `invalidatedInstructionIds` lists all
instructions; `invalidatedRunIds` lists all RUNs. Every `reasonChain` entry has
`because: "empty synthetic cache (independent-cold)"` and `causedBy: []`.
Every step has `decision: "synthetic-miss"`, `causedByInstructionId: null`, and
`why: "Empty synthetic cache for this independent scenario; every instruction, including FROM, is assumed to miss."`
`firstAffectedCopy`, `triggers` and `matchedChanges` still explain actual declared
COPY changes; `firstInvalidatedCopy` is the first COPY even for a no-op, or null
for a Dockerfile with no COPY. Mtime and uncopied path reporting stays available.

Independent-cold reports omit history predecessors, state origins, reuse labels, category
summaries and aggregate history fields. Caches are separate between Dockerfiles
and between scenarios. Misses are **synthetic cache assumptions**, not Docker
pulls, network work, real BuildKit cache keys or timings. Nothing is executed or
fetched. `actualCacheHitProven` and `semanticEquivalenceProven` remain false;
`measuredBuildSeconds` stays null. Default `independent-warm` and
`cumulative-history` behavior and report bytes are unchanged.

## Ordered cold-start replay

Use `--mode cumulative-cold` to advance inventory and retain synthetic cache
states in scenario array order, starting with **two separate empty caches**.
The first successful scenario misses every instruction, including FROM and
metadata. Later no-op/mtime-only scenarios reuse the preceding build; content
changes can miss COPY and its suffix while reusing FROM. Weights include every
missed instruction once, including zero/fractional FROM or metadata weights.

This mode uses the same ordered inventory, revision IDs, selection and validation
rules as `cumulative-history`. Every required COPY must select regular files in
the original inventory and every pre/post scenario inventory. Later invalid
schema, inventory, revision or COPY input rejects the entire text/JSON report
with exit 2 and empty stdout, including when an earlier scenario is selected.

`initialRevisionIds` labels content but never creates cache entries. Only states
produced by successful scenarios enter the caches. If the first scenario edits
v0 to v1, a later declared revert to v0 still misses unless a scenario has already
produced that v0 instruction state. A later return to v1 can reuse its retained
state. Unlabeled content/add events still create fresh anonymous revisions.

Using the `cold_dir` pair and weights from the independent-cold example above,
replace the changes document with:

```sh
cat > "$cold_dir/changes.json" <<'JSON'
{"schemaVersion":1,"scenarios":[
  {"id":"noop","changes":[]},
  {"id":"edit","changes":[{"path":"src/app.js","kind":"content"}]},
  {"id":"final","changes":[]}
]}
JSON
stackwrench cache-replay \
  --before "$cold_dir/before" --after "$cold_dir/after" \
  --context "$cold_dir/context" --changes "$cold_dir/changes.json" \
  --mode cumulative-cold --weights "$cold_dir/weights.json" --json
```

For each Dockerfile the ordered miss IDs are `[I001,I002,I003]`, `[I002,I003]`,
then `[]`. Retained entry counts are `0 -> 3`, `3 -> 5`, then `5 -> 5`.
Before costs are `1.75, 1.5, 0`; after costs are `2.5, 0.5, 0`. Totals are
before=3.25, after=3, saved=0.25 model units. Without weights, both totals are 2.

JSON keeps schema version 1, uses `mode: "cumulative-cold"` and
`model: "linear-copy-history-v1"`, and includes all history cache/reuse fields:

- `previousBuild` is **null** for the first scenario; later it is
  `{"kind":"scenario","scenarioId":"ID"}` for the immediately preceding
  full-input scenario. Text prints `previous modeled build: none (cold start)`
  initially, then `previous modeled build: scenario ID`.
- Each side's `cache` has `hitInstructionIds`, `missInstructionIds`,
  `entryCountBefore` and `entryCountAfter`. States are retained without eviction
  for this report only. Equal Dockerfiles still have separate caches.
- Every step has `syntheticStateOrigin`: null on a miss, otherwise the first
  producing scenario object above. There is no `initial-warm` origin.
  `syntheticStateReuse` is null on a miss, `previous-build` for a key matching
  the immediately preceding build, or `retained-history` for an older retained key.
- Per-side `syntheticReuseCounts` and `syntheticReuseModelUnits` contain
  `previousBuild`, `retainedHistory`, `misses`; totals sum each category over
  displayed scenarios only. The existing fractional tolerance below applies.
- First-build `reasonChain` entries say
  `empty synthetic cache (cumulative-cold)` with `causedBy: []`; each step has
  `causedByInstructionId: null` and the explanation `Empty synthetic cache for
  the first ordered scenario; every instruction, including FROM, is assumed to
  miss.` Actual COPY triggers and `firstAffectedCopy` still describe events;
  `firstInvalidatedCopy` is the first COPY (or null if none).

Adding `--scenario final` preserves the hidden `edit` predecessor, retained
entries and first-producing origins, while displaying only `final` and zero
cost totals. All input is evaluated. These fields are output-only. Other modes'
reports are unchanged. Caches model synthetic states only: no Docker pulls,
network work, real BuildKit keys, actual hits or measured timings are claimed.
The history sections below describe shared fields; their warm initial build and
`initial-warm` examples apply specifically to `cumulative-history`.

## Ordered cumulative history

The same schema is used for all modes; the CLI option controls interpretation.
In `cumulative-history`, a scenario starts from the preceding scenario's resulting
inventory. Events within one scenario form a single atomic change set, with no
intermediate builds. Thus add → content → delete across three scenarios is valid;
add → add or delete → content is rejected. Directories, including inferred parents,
remain after their last file is removed; directory deletion/replacement is outside
the subset. Missing COPY sources and unsupported Dockerfile syntax still fail the
**whole report**, with exit 2 and empty stdout, even after valid earlier scenarios.

Each Dockerfile starts with a separate simulated warm cache for the initial
inventory. A successful modeled build retains every instruction's synthetic state
for later steps, without eviction. A state depends on the instruction text, its
parent state and, for COPY, selected file paths and synthetic file revisions.
Unlisted initial files have anonymous revision zero; every content/add event
assigns a fresh revision. Optional caller `initialRevisionIds` declarations and
`revisionId` on content/add events can instead declare reusable content identity,
as specified below. mtime-only events leave revisions unchanged.
Contents are never read or hashed: without an explicit ID, a content edit back to
old bytes or deleting then re-adding the same file does **not** establish restoration
of its old content. Removing a newly added file can restore an already retained
state for all the other unchanged files.

The synthetic cache is in memory for one comparison; it is not a BuildKit cache,
and no cold build or initial warm-up cost is included. RUN outputs remain opaque.
All costs are arbitrary **model units**, with no timing or semantic-equivalence
claim. Reproduce the local synthetic history fixture:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.history.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --mode cumulative-history --json
```

Starting from the four bundled inventory files, the exact expected result is:

| Ordered step | Event | Inventory files | Before units | After units |
| --- | --- | ---: | ---: | ---: |
| source-edit | content src/app.js | 4 → 4 | 110 | 10 |
| manifest-edit | content package.json | 4 → 4 | 110 | 110 |
| add-source | add src/new.js | 4 → 5 | 110 | 10 |
| remove-added-source | delete src/new.js | 5 → 4 | 0 | 0 |
| no-change | empty changes | 4 → 4 | 0 | 0 |
| Total | | | 330 | 130 |

The second edit retains the first edit's revision. The fourth step restores the
second step's selected paths/revisions, so all instructions hit retained synthetic
states. The last step changes nothing. Totals save 200 model units. For this same
fixture, `independent-warm` rejects `remove-added-source`: `src/new.js` was absent
from the original inventory. The original independent experiment below is unchanged.

### Optional immutable content revision IDs

In `cumulative-history` or `cumulative-cold`, a `content` or `add` event may include `revisionId`:

```json
{"path": "src/app.js", "kind": "content", "revisionId": "source-v1"}
```

- IDs are case-sensitive strings of 1–128 ASCII letters/digits/`_.-`. Empty,
  overlong, non-string, whitespace, control and other characters are rejected.
  Unknown fields and duplicate JSON keys remain errors. Schema version stays 1.
- Identity is scoped to the **same canonical file path within one report**.
  Reusing an ID asserts the same immutable content each time. Different IDs assert
  different revisions; equal IDs on different paths do not equate those paths.
  IDs are opaque labels, not computed or verified content hashes.
- An ID can be reused after an intervening content edit (with or without an ID),
  or in an `add` after deletion. If selected paths, their revisions and parent
  instruction states match a retained state, the instruction is a synthetic hit.
  Other changed inputs can still cause misses. The two Dockerfiles keep separate
  caches. This does not prove real Docker cache reuse.
- A `content` event repeating that path's **current** ID contradicts the assertion
  of changed content and is rejected; omit the event for unchanged content.
  mtime/no-op scenarios preserve the current ID. An `add` still requires an absent
  file; IDs do not relax inventory or required-COPY-source checks.
- `revisionId` on `mtime`/`delete`, or in `independent-warm`, is rejected.
  All errors reject the whole report with exit 2 and empty stdout, including
  errors in later scenarios. No partial comparison or diff is emitted.
- Omitting the ID retains fresh-revision behavior. Caller IDs cannot collide with
  anonymous revisions or anonymous initial revision zero, including IDs `"0"` and
  `"1"`. An ID first introduced by an event cannot establish equality with
  anonymous initial content; use an initial declaration for that identity.

Replay the declared revert/delete/re-add fixture with the CLI:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.revisions.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --mode cumulative-history --json
```

The expected ordered before/after costs are `110/10`, `110/10`, `0/0`, `110/10`,
`0/0` model units: totals **330/30**, saving **300**. `revert-v1` and `readd-v1`
both match a previously retained state, with every instruction a `synthetic-hit`,
no invalidated COPY and no new cache entries despite a non-null `firstAffectedCopy`.
JSON preserves IDs in scenario `changes`, COPY `triggers` and step `matchedChanges`;
text lists declared path/kind/ID records, including changes outside COPY selections.
The invalid fixture `examples/cache_replay/changes.revisions.invalid.json` repeats the current
ID in its second content event and must exit 2 with empty stdout.

### Optional initial revision declarations

In `cumulative-history` or `cumulative-cold`, changes JSON may also contain an
`initialRevisionIds` object alongside required `schemaVersion` and `scenarios`:

```json
{
  "schemaVersion": 1,
  "initialRevisionIds": {"src/app.js": "source-v1"},
  "scenarios": [
    {"id": "edit-v2", "changes": [{"path": "src/app.js", "kind": "content", "revisionId": "source-v2"}]},
    {"id": "revert-initial", "changes": [{"path": "src/app.js", "kind": "content", "revisionId": "source-v1"}]}
  ]
}
```

Each key must be a canonical relative path of an **existing regular file in the
initial inventory**, using the event-path grammar. Directories, missing paths
(including files added later), leading `./`, trailing `/`, traversal, absolute
paths, repeated slashes, globs and other unsupported path syntax are rejected.
Context symlinks and special files remain rejected. Values use the same
case-sensitive 1–128 ASCII letters/digits/`_.-` grammar as event `revisionId`.
Unknown fields, duplicate JSON keys (including repeated declaration paths),
non-object declarations and invalid IDs are errors. An empty object is valid
in history mode; **any** declaration, including `{}`, is rejected in independent
mode. Schema version stays 1.

In `cumulative-history`, declarations label starting content before **both
separate warm synthetic caches** are seeded. In `cumulative-cold`, declarations
label starting content while both caches remain empty. They are not change events and may include uncopied files. IDs share
the event-ID namespace for the same path within one report; equal labels on
different paths do not equate them. Unlisted initial files remain anonymous.
A content edit or re-add using a declared initial ID can restore the initial
state if every selected input and parent state matches. Other changed inputs
can still cause misses. Content events repeating a file's current initial ID
are rejected, including after mtime-only/no-op scenarios. Omitted event IDs still
assign fresh revisions. Declarations do not relax inventory checks, required
COPY sources, or the prohibition on Dockerfile/ignore-file changes.

Successful JSON echoes top-level `initialRevisionIds` exactly when supplied,
including `{}` and with `--scenario` filtering. Text prints
`Initial revision declarations: N`, followed by path-sorted `path revisionId=ID`
lines. Omitted declarations add no field or text header. History assumptions
explain initial declarations, anonymous unlisted files and unverified caller
identity. No context contents or hashes are read. Invalid input anywhere still
rejects the whole report with exit 2, stderr and **empty stdout**, even when a
selected earlier scenario is valid; library callers receive `InputError`.

The bundled fixture also deletes and re-adds the initial source, while broad
COPY sources remain valid:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.initial-revisions.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --mode cumulative-history --json
```

Expected ordered before/after costs: `110/10`, `0/0`, `110/10`, `0/0` model units;
totals **220/20**, saving **200**. `revert-initial` and `readd-initial` have no
misses or new cache entries, with every instruction a `synthetic-hit` despite
input-change triggers. Adding `--scenario revert-initial` displays the same
revert entry and declarations with totals `0/0/0`. Deleting the sole source of
a required COPY still rejects the entire history, even if it is re-added later.
These are synthetic model results, not actual Docker cache-hit evidence.

JSON retains schema version 1 and adds `mode`, scenario
`inventoryBeforeFileCount`/`inventoryAfterFileCount`, and, in history mode, each
side's `cache` with `hitInstructionIds`, `missInstructionIds`,
`entryCountBefore`/`entryCountAfter`. `inventoryFileCount` is the initial count.
`firstAffectedCopy` and `triggers` describe this step's input changes, even when
retained cache entries satisfy them. In history mode, `invalidatedInstructionIds`,
`invalidatedRunIds`, costs and `reasonChain` include only cache misses; the first
miss can be later than the first affected COPY. In `independent-warm`, they still
include the entire suffix from the first affected COPY. In `independent-cold`,
they include every instruction, as specified above.

### First retained synthetic state origin

In `cumulative-history`, every instruction entry in `steps` also includes
`syntheticStateOrigin`. A `synthetic-hit` reports either
`{"kind": "initial-warm"}` or `{"kind": "scenario", "scenarioId": "ID"}`,
identifying where that state **first** entered that Dockerfile's separate cache.
A `synthetic-miss` reports null, even though the successful modeled build then
retains the state for later scenarios. Later hits never replace its first origin.
Origins include the full parent state and selected COPY inputs; a matching file
revision alone does not guarantee a hit. A single scenario can hit early steps
from warm-up or earlier scenarios and miss later steps.

Text adds `synthetic state origin: initial-warm`,
`synthetic state origin: scenario ID`, or
`synthetic state origin: none (miss)` below each history step's explanation.
`--scenario` preserves the same origins as full replay, including producers in
earlier undisplayed scenarios. For the initial-revision fixture above, every
step of `revert-initial` and `readd-initial` has origin `initial-warm`. For
`examples/cache_replay/changes.revisions.json`, restored source-dependent steps instead name
`declare-v1`; earlier unaffected steps still name `initial-warm`.

Schema version stays 1. Independent-mode text/JSON omit origins and remain
unchanged; existing history fields, decisions, costs and cache counts are
unchanged. No new input fields are accepted and all existing validation applies:
invalid later input, including missing required COPY sources, rejects the entire
report with exit 2 and empty stdout. Origins explain only this report's synthetic
cache retention; they do not prove real BuildKit cache hits or verify caller IDs.
Context contents are never read.

### Previous build or older retained history

Each history step also includes `syntheticStateReuse`:

- `"previous-build"`: a synthetic hit with the same key as that instruction in
  the immediately preceding modeled build for that Dockerfile. The first scenario
  compares against the initial warm build. No-op and mtime-only scenarios match
  the previous build, even when its state was first produced much earlier.
- `"retained-history"`: any other synthetic hit, restoring an older retained
  state after an intervening build used a different key for that instruction.
- `null`: a synthetic miss, before its state is retained.

Text adds `synthetic state reuse: previous-build`,
`synthetic state reuse: retained-history`, or
`synthetic state reuse: none (miss)` below each history step's origin.
This classification is per instruction and per Dockerfile; early steps can reuse
the previous build while later steps restore history or miss. First-producer
`syntheticStateOrigin` is preserved, including after repeated hits.

For `examples/cache_replay/changes.initial-revisions.json`, both `revert-initial` and
`readd-initial` show `retained-history` on the five source-dependent steps across
the two Dockerfiles, and `previous-build` on the six earlier steps. All eleven
origins remain `initial-warm`. A subsequent no-op would show `previous-build`
everywhere without changing origins.

`--scenario` classifies the full history before filtering, so its step entries
equal the unfiltered entries. Schema version stays 1; costs, decisions, cache
counts, origins and all existing report fields are unchanged. Independent output
omits this field and text line. No new input fields or switches are accepted;
later invalid input still rejects the entire report with exit 2 and empty stdout.
These labels describe synthetic keys only; they do not prove real cache reuse or
open context contents.

Each history scenario names that preceding build in `previousBuild`:
`{"kind": "initial-warm"}` for the first input scenario, otherwise
`{"kind": "scenario", "scenarioId": "ID"}` for the immediately preceding input
scenario. Both Dockerfiles follow this same build order while retaining separate
caches. This field is present regardless of hits or misses, including no-op,
mtime-only and anonymous-content scenarios. It names the comparison build for
`previous-build` reuse; first-producer origins can refer to much older builds.

Text prints one `previous modeled build: initial-warm` or
`previous modeled build: scenario ID` line after the scenario's inventory counts.
For `examples/cache_replay/changes.initial-revisions.json`, `edit-v2` names `initial-warm`,
`revert-initial` names scenario `edit-v2`, and `readd-initial` names scenario
`delete-source`. Selecting `--scenario revert-initial` still names the hidden
`edit-v2` predecessor and preserves the entire unfiltered scenario entry.
The field and text line are omitted in independent mode. Schema version, origins,
reuse categories, costs and cache counts are unchanged. `previousBuild` is
output-only: adding it to input is rejected as an unknown field. All input is
still evaluated before selection; later invalid input rejects the whole report.

### Synthetic reuse counts

Each cumulative-history scenario's `before` and `after` result also includes
`syntheticReuseCounts`, with exactly three nonnegative integer instruction counts:

```json
{"previousBuild": 2, "retainedHistory": 3, "misses": 0}
```

These count that side's steps labeled `previous-build`, `retained-history`, and
`null` respectively. They include all instructions, including FROM, metadata and
RUN; they are not weighted costs. The three counts sum to the instruction count.
The first two sum to `cache.hitInstructionIds.length`, and `misses` equals
`cache.missInstructionIds.length`. No-op and mtime-only builds count every step
as previous-build reuse, even when first origins are much older. A restoration
can combine previous-build hits, older retained-history hits and misses.

Text adds one line immediately after each existing synthetic cache summary:

```text
    synthetic reuse: previous-build=2, retained-history=3, misses=0
```

For `examples/cache_replay/changes.initial-revisions.json --mode cumulative-history
--scenario revert-initial`, the before counts are `2/3/0` and after counts are
`4/2/0` in the order above. These equal the full report's selected entry; hidden
scenarios do not contribute to selected totals. Counts appear on history
scenario sides and in displayed-history totals, as described below; they do not
appear on steps, cache objects or independent results. Independent JSON/text is unchanged. Schema version remains 1, and all prior history fields,
costs, decisions, cache counts, predecessors, origins and reuse labels are
preserved. `syntheticReuseCounts` is output-only: top-level, scenario or event
input fields with that name are rejected. Later invalid input still rejects the
entire report with exit 2 and empty stdout. Counts describe synthetic instruction
states, not proven BuildKit reuse, measured time or verified content identity.
Context contents are never opened.

Optional `--weights` JSON uses instruction IDs from a default report. IDs count
all instructions from `I001` in each file; blank/comments/continuations do not
add IDs. Moving instructions changes IDs. Unspecified RUN weights default to 1,
all other weights to 0. Supplied values must be finite numbers in `[0, 1e12]`,
including fractional values, never booleans. Unknown IDs fail:

```json
{"schemaVersion":1,"before":{"I004":100,"I005":10},"after":{"I004":100,"I006":10}}
```

### Weighted synthetic reuse model units

Each history scenario side also reports `syntheticReuseModelUnits`, with exactly
`previousBuild`, `retainedHistory` and `misses`. These sum the instruction weights
in the corresponding reuse categories, including FROM, metadata, COPY and RUN.
They use the existing `--weights` document; omitted weights still default to 1
for RUN and 0 for other instructions. Counts remain unweighted and unchanged.
The values are finite nonnegative **model units**, not measured time or proof
of avoided work in a real build. Empty categories have value 0.

`misses` exactly equals that side's existing `invalidatedModelUnits`. Each sum
uses instruction order and the existing numeric arithmetic: exact integers for
integer weights, binary floating point for fractional weights. The three
categories partition the instruction weights; grouped fractional sums can round
differently from a single full-inventory sum. Compare that partition with
`math.isclose(rel_tol=1e-12, abs_tol=1e-9)` rather than requiring exact fractional
equality. Text formats each value with the existing `:g` cost format; JSON
retains the numeric values.

Immediately after each synthetic reuse count line, text adds:

```text
    synthetic reuse model units: previous-build=0, retained-history=110, misses=0
```

Reproduce the weighted restoration summary with the CLI:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.initial-revisions.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --mode cumulative-history \
  --scenario revert-initial --json
```

Expected `syntheticReuseModelUnits` are `0/110/0` before and `100/10/0` after
(previousBuild/retainedHistory/misses). Counts stay `2/3/0` and `4/2/0`.
Both miss costs and selected cost totals stay zero; predecessor `edit-v2` remains
hidden but named. Full-input costs remain 220/20/200 before/after/saved units.

These per-scenario values also supply the displayed-history totals below;
hidden scenarios never contribute to selected totals. Selection preserves each
full scenario entry. Schema version 1, prior fields, origins, reuse labels, cache
counts, costs and exact independent text/JSON are unchanged. The field is
output-only: injecting `syntheticReuseModelUnits` into changes or weights input
is rejected. Invalid later input still rejects the entire selected report with
exit 2 and empty stdout. No context contents are opened and no Docker or RUN
commands are executed.

### Reuse totals across displayed history

History `totals` adds `syntheticReuseCounts` and `syntheticReuseModelUnits`, each
with separate `before` and `after` summaries. Each category is the sum of that
side's matching per-scenario category **over displayed scenarios only**, in array
order. Dockerfiles remain separate; their instruction inventories and weights
can differ. The cost and reuse portion of the selected `revert-initial` totals is:

```json
{
  "beforeModelUnits": 0,
  "afterModelUnits": 0,
  "savedModelUnits": 0,
  "syntheticReuseCounts": {
    "before": {"previousBuild": 2, "retainedHistory": 3, "misses": 0},
    "after": {"previousBuild": 4, "retainedHistory": 2, "misses": 0}
  },
  "syntheticReuseModelUnits": {
    "before": {"previousBuild": 0, "retainedHistory": 110, "misses": 0},
    "after": {"previousBuild": 100, "retainedHistory": 10, "misses": 0}
  }
}
```

Text adds these four lines immediately after the existing total cost line:

```text
Total (displayed scenario): before=0, after=0, saved=0 model units
  before synthetic reuse: previous-build=2, retained-history=3, misses=0
  before synthetic reuse model units: previous-build=0, retained-history=110, misses=0
  after synthetic reuse: previous-build=4, retained-history=2, misses=0
  after synthetic reuse model units: previous-build=100, retained-history=10, misses=0
```

Without `--scenario`, all four scenarios in that fixture contribute. The count
summaries become `8/6/6` before and `16/4/4` after; weighted summaries become
`0/220/220` before and `400/20/20` after, in previousBuild/retainedHistory/misses
order. Existing costs remain `220/20/200` before/after/saved. Text uses `Total`
without the displayed-scenario qualifier when no filter is supplied.

Counts are nonnegative integers and partition all displayed instruction steps.
Previous-build plus retained-history counts equal summed cache hits; miss counts
equal summed cache misses. Weighted aggregate misses exactly equal the existing
`beforeModelUnits` or `afterModelUnits` total, using the same per-scenario sum
order. All weighted categories are finite nonnegative model units and include
zero, fractional and non-RUN weights. Integer weights retain integer arithmetic;
fractional partition comparisons use the tolerance above because regrouping can
round differently. Text keeps integer counts and `:g` model-unit formatting.

Filtering still validates and replays the entire input before summation. The
selected entry, including a hidden predecessor and older origins, is unchanged;
its two summaries equal the selected totals exactly. Per-scenario summaries,
cache counts, costs and schema version 1 are preserved. Independent JSON/text
omits both aggregate fields and remains unchanged. These summaries and `totals`
are output-only: injecting them into changes root/scenarios/events or weights
root/side mappings is rejected. Later invalid input still exits 2 with empty
stdout in either format, even selecting an earlier valid scenario. Contents are
never read; synthetic reuse does not prove real cache hits or measured savings.

### Miss totals by instruction

Both ordered modes (`cumulative-history` and `cumulative-cold`) also add
`totals.syntheticMissesByInstruction`. It has separate `before` and `after`
arrays, each listing **every instruction in Dockerfile order**, including
instructions with no misses. Each record has exactly these fields:

```json
{"instructionId": "I004", "missCount": 2, "missedModelUnits": 200, "missedScenarioIds": ["edit-v2", "delete-source"]}
```

`missCount` is a nonnegative integer: the number of **displayed scenarios**
whose step for that instruction is a `synthetic-miss`. `missedModelUnits` sums
that instruction's weight once per displayed miss, in scenario order. An empty
sum is integer zero; integer weights retain integer arithmetic, fractional
weights use binary floating point. A zero weight can have a positive miss count
with zero missed units. Non-RUN instructions count too, including first-build
FROM misses in `cumulative-cold`.

`missedScenarioIds` lists exactly those contributing **displayed scenario IDs
in source array order**, including zero-weight misses. Its length equals
`missCount`; summing the instruction weight once per listed ID reproduces
`missedModelUnits` within the fractional tolerance below. No misses gives `[]`.
Both `--scenario` and `--select-scenarios` restrict attribution to their displayed
set; reversing selector arguments does not reorder the IDs. Hidden producers
and immediate predecessors can still appear in scenario evidence, but never in
these contribution lists. The two Dockerfiles keep separate lists even where
their instruction IDs coincide. No new inputs or switches are needed.

For example, run:

```sh
stackwrench cache-replay \
  --before examples/cache_replay/Dockerfile.before --after examples/cache_replay/Dockerfile.after \
  --changes examples/cache_replay/changes.initial-revisions.json --context examples/cache_replay/context \
  --weights examples/cache_replay/weights.json --mode cumulative-history
```

After both sides' existing aggregate reuse lines, text lists one line per
record, before then after, followed by the retained-history reuse summary below. Among the lines
from that command are:

```text
  before synthetic misses I001: count=0, missed=0 model units; scenarios=[]
  before synthetic misses I004: count=2, missed=200 model units; scenarios=[edit-v2, delete-source]
  after synthetic misses I006: count=2, missed=20 model units; scenarios=[edit-v2, delete-source]
```

Counts use integer formatting; model units use the existing `:g` format. For
each side, record counts sum exactly to `totals.syntheticReuseCounts[side].misses`.
Record units sum to `totals.syntheticReuseModelUnits[side].misses` (and the
side's existing cost total) within `math.isclose(rel_tol=1e-12, abs_tol=1e-9)`;
regrouping fractional additions by instruction can round differently.

`--scenario revert-initial` makes every record zero in the warm history example,
while retaining the full replay's predecessor, origin, reuse and cache fields.
Hidden scenarios never contribute to these arrays. Adding
`--select-scenarios revert-initial edit-v2` instead gives `[edit-v2]` for the
warm I004/I006 records and `[]` for warm I001. In cold history I001 lists only
`[edit-v2]`, while before I004 and after I006 list `[edit-v2, revert-initial]`:
the initial declarations do not warm caches, so the first restore can miss a
state not yet built. Filtering still evaluates every input scenario; later invalid schema,
revision or COPY sources reject the whole text/JSON report with exit 2 and
empty stdout. The new summary and its record metadata are output-only and are
rejected in changes or weights input, including `missedScenarioIds` at the
root, scenario, event or weight mapping locations. No switches or input fields
were added.
Schema 1, all existing scenario/aggregate fields and independent report bytes
are preserved; independent modes omit this summary and its text lines. These
are synthetic contributions, never Docker cache evidence or measured time.
The lists retain one ID per miss, so report size grows with displayed miss
contributions; filtering still performs the full replay and does not reduce its
work or retained cache memory.

### Retained-history reuse by instruction

Both ordered modes also add `totals.syntheticHistoryReusesByInstruction`,
with separate `before` and `after` arrays. Every source instruction gets one
record in Dockerfile order, including zero records:

```json
{"instructionId": "I004", "reuseCount": 2, "reusedModelUnits": 200, "reusedScenarioIds": ["revert-initial", "readd-initial"]}
```

A contribution requires both `decision: "synthetic-hit"` and
`syntheticStateReuse: "retained-history"`. Previous-build hits and misses are
excluded. `reuseCount` is an integer counting displayed contributions;
`reusedModelUnits` sums the instruction weight once per contribution in source
scenario order. Zero-weight and non-RUN instructions still contribute counts
and IDs. No contributions produces `reuseCount: 0`, `reusedModelUnits: 0` and
`reusedScenarioIds: []`. IDs are unique, source ordered, and their list length
equals `reuseCount`. The two Dockerfiles have separate records even when their
instruction IDs coincide.

For the weighted `changes.initial-revisions.json` command above in
`cumulative-history`, before I003–I005 and after I005–I006 each list
`["revert-initial", "readd-initial"]`. Before I004 totals 200 reused model
units; after I006 totals 20. The zero-weight COPY instructions have the same
two contributions and zero units. Text appends these lines after all instruction
miss records and before the first-producer lines below, before rows then after rows:

```text
  before synthetic retained-history reuses I001: count=0, reused=0 model units; scenarios=[]
  before synthetic retained-history reuses I004: count=2, reused=200 model units; scenarios=[revert-initial, readd-initial]
  after synthetic retained-history reuses I006: count=2, reused=20 model units; scenarios=[revert-initial, readd-initial]
```

Use `--scenario revert-initial` to display only the first restore. Reversed
`--select-scenarios readd-initial edit-v2` arguments still give source-ordered
scenarios, and only `readd-initial` contributes to these records. In
`cumulative-cold`, the full example also lists only `readd-initial`: the first
restore misses the unseeded initial state, then re-adding restores the state
produced by `revert-initial`. Hidden producer/predecessor IDs remain in the
existing step evidence but never add summary contributions. Every input is
still validated and replayed before selection.

Per side, counts sum exactly to `totals.syntheticReuseCounts[side].retainedHistory`.
Units reconcile with `totals.syntheticReuseModelUnits[side].retainedHistory`
using `math.isclose(rel_tol=1e-12, abs_tol=1e-9)`; regrouping binary fractional
additions may round differently. Empty sums are integer zero, integer weights
retain integer arithmetic, and text keeps existing `:g` precision.

No inputs or switches were added. The summary and all its record fields are
output-only: injecting them into changes root/scenario/event or weights
root/side mappings rejects the entire report with exit 2, stderr and empty
stdout, including invalid hidden input. Existing hidden schema/revision/COPY
validation stays atomic. Schema 1, all existing decisions and aggregate values
are preserved. Independent modes omit the summary and its text lines and retain
identical output bytes. These are synthetic instruction-state reuses, never
real Docker cache proof, verified content identity or measured savings. Lists
grow with displayed reuse contributions; filtering does not reduce full replay
work or cache memory.

### Retained-history reuse by first producer

Both ordered modes also add `totals.syntheticHistoryReusesByOrigin`, with
separate `before` and `after` arrays. It groups the same displayed
retained-history contributions as the instruction summary above, but by the
existing `syntheticStateOrigin` object of the reused synthetic state, which
names that state's first producer:

```json
{"origin": {"kind": "scenario", "scenarioId": "revert-initial"}, "reuseCount": 2, "reusedModelUnits": 10}
```

`origin` is copied exactly: `{"kind": "initial-warm"}` for the seeded warm
state, or `{"kind": "scenario", "scenarioId": ...}` with the original producer
ID. Reuse never reattributes a state; repeated hits keep the first producer.
Records follow `initial-warm` first, then scenario producers in full input
array order, regardless of display or selection order. A producer with no
displayed contributions is omitted, while a positive count with zero model units
is kept. No contributions produce `[]` for that side. `reuseCount` is an
integer; `reusedModelUnits` sums the same instruction weights, once per
contribution. Per side, counts sum exactly to
`totals.syntheticReuseCounts[side].retainedHistory`, and units reconcile within
`math.isclose(rel_tol=1e-12, abs_tol=1e-9)`.

With the weighted `changes.initial-revisions.json` example above:

```text
  before synthetic retained-history reuses from initial-warm: count=6, reused=220 model units
  after synthetic retained-history reuses from initial-warm: count=4, reused=20 model units
```

In `cumulative-cold`, the same restores are attributed to `revert-initial`
(`before` 3 reuses, 110 units; `after` 2 reuses, 10 units). Text prints these
lines after the instruction reuse lines, before `Assumptions`, before rows then
after rows, with no lines for empty arrays.

`--scenario` and `--select-scenarios` only choose which contributions are
displayed. Hidden consumers are excluded, and every displayed record keeps the
original producer identity even when that producer is hidden. For example, in
`cumulative-history`, `--select-scenarios readd-initial edit-v2` displays
`readd-initial` and `edit-v2` and reports only `from initial-warm` (before 3
reuses, 110 units; after 2 reuses, 10 units), the original producer of the
restored state. Every input is validated and replayed before selection.

The record fields are output-only, like the existing summaries. Injecting
`syntheticHistoryReusesByOrigin`, `origin`, `reuseCount` or `reusedModelUnits`
into the changes root, a scenario, an event, or the weights root or side
mappings rejects the report with exit 2, empty stdout and a stderr diagnostic,
including with `--fail-on-regression`. Schema 1, existing scenario and aggregate
values, text bytes, and the regression gate's exit codes are unchanged.
Independent modes omit this key and its text lines. These are synthetic
instruction-state attributions, not real cache provenance, verified content
identity or measured savings.

## Reproducible comparison experiment

The bundled synthetic inputs stage dependency manifests before `npm ci`; they
are **not executed**. A human must verify the candidate's behavior for their
project (install scripts can read source, and later COPY can overwrite outputs).
Semantic equivalence is not established by a lower modeled cost.

Every successful report now shows the **caller-supplied candidate diff**, followed
by a decision and `why` for **every instruction** in each scenario and Dockerfile.
The diff compares the two input texts with fixed labels `before/Dockerfile` and
`after/Dockerfile`; it preserves comments and continuation lines, normalizes text
newlines, and marks missing final newlines. Identical texts produce an empty diff.
It shows your proposed ordering, including the staged manifests and earlier
`npm ci` in the bundled candidate. It does not generate a new candidate or write
either file. **User must confirm semantic equivalence before adopting the
candidate**; install scripts, RUN behavior and COPY overwrites are not analyzed.

For example, run the weighted command above. In `source-edit`, candidate
I001–I004 are `unaffected`: they precede the first affected COPY. The last two
steps include:

```text
I005 COPY . .: invalidated
  why: Declared changes match this COPY's inputs.
  matched: src/app.js (content) via COPY .
I006 RUN npm run build: invalidated
  why: Parent I005 is invalidated downstream of COPY I005 in the linear model.
```

For `manifest-edit`, `package.json` matches both candidate I003 (manifest COPY)
and I005 (broad COPY). The first invalidated COPY is I003, and both RUN I004
(`npm ci`) and I006 (`npm run build`) are invalidated. Thus a source edit and a
dependency edit have different explained boundaries, even though both files
are eventually copied. A no-op, mtime-only or uncopied change yields `unaffected`
steps in `independent-warm`; this is not evidence of actual cache hits. External
image, network, secret and other unmodeled factors remain unknown.

JSON schema version 1 adds `actualCacheHitProven: false`, `candidateComparison`
(`source`, `format`, `diff`, `requiresSemanticEquivalenceConfirmation`, `notice`),
and each scenario side's `steps` plus `firstInvalidatedCopy`. Each step includes
`instructionId`, `line`, `op`, `argument`, `decision`, `why`, `matchedChanges`
(the same path/kind/COPY/source records as `triggers`) and
`causedByInstructionId` (invalidated immediate parent, or null). Independent-warm
decisions are `unaffected` or `invalidated`; independent-cold decisions are always
`synthetic-miss`. History decisions are `synthetic-hit`
or `synthetic-miss`; even a changed COPY can reuse a retained synthetic state.
`firstAffectedCopy` still identifies the earliest COPY matching this step's
changes, while `firstInvalidatedCopy` identifies the earliest COPY actually
invalidated by the selected model, or null. In history they can differ, or a
changed COPY can coexist with no misses at all. Neither synthetic reuse nor
`unaffected` proves a real BuildKit hit; `actualCacheHitProven` is always false.
Malformed inputs, missing required sources or invalid later scenarios still
reject the entire comparison with exit 2 and **no diff or partial stdout**.

Replay the weighted command above to reproduce:

| Independent scenario | Before invalidated RUN | After invalidated RUN | Before units | After units | Saved units |
| --- | --- | --- | ---: | ---: | ---: |
| source-edit | I004, I005 | I006 | 110 | 10 | 100 |
| manifest-edit | I004, I005 | I004, I006 | 110 | 110 | 0 |
| new-source | I004, I005 | I006 | 110 | 10 | 100 |
| deleted-doc | I004, I005 | I006 | 110 | 10 | 100 |
| mtime-only | none | none | 0 | 0 | 0 |
| Total | | | 440 | 140 | 300 |

Without `--weights`, the totals are 8 versus 5 RUN-unit invalidations, saving 3
units. These are deterministic static model results, **not performance
measurements**. No real build was timed. JSON always includes
`measuredBuildSeconds: null`, assumptions, unmodeled factors, mutable FROM flags,
instruction inventories, triggers, downstream reason chains and negative or
positive saved model units. Weights are arbitrary user estimates, not seconds.

In independent-warm mode, the first affected COPY invalidates itself and every later instruction,
including metadata instructions. Every matching changed path is explained; the
chain links later RUNs to the first COPY. mtime-only events never trigger this
chain. A mutable FROM is assumed to resolve to the same image in both snapshots,
without verification. Different before/after images can still be compared as
separate models; their equivalence remains the user's responsibility.

## Boundaries and upstream relationship

[BuildKit](https://github.com/moby/buildkit) executes real caching;
[hadolint](https://github.com/hadolint/hadolint) checks Dockerfile rules;
[dive](https://github.com/wagoodman/dive) inspects image layers. This tool provides
an offline declared-change replay and baseline/candidate explanation. It is not
an upstream fork, Docker frontend, cache-key implementation or universal cache
predictor. It does not diagnose the intermittent runtime cache miss in
[BuildKit #2039](https://github.com/moby/buildkit/issues/2039).

The linear rule and mtime exception are based on
[Docker's cache invalidation documentation](https://docs.docker.com/build/cache/invalidation/).
Actual BuildKit dependency graphs, cache mounts, bind/secret/SSH mounts, secret
values, external image mutation, network side effects, build-time environment,
directory metadata and nondeterministic RUN outputs are not modeled. Unsupported
mount syntax is rejected. A pinned image digest reduces one assumption but does
## License

Original code is MIT licensed; see [LICENSE](../LICENSE). See
[THIRD_PARTY.md](../THIRD_PARTY.md) for the behavioral references (Docker cache
invalidation documentation, BuildKit). No third-party code or model is bundled.
