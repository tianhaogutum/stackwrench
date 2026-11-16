# stackwrench context-budget

`stackwrench context-budget` (formerly `docker-context-budget`) is an offline Linux
command for inspecting a local Docker build context before a build.
It lists files deterministically, explains inclusion with the last matching
ignore rule and line, shows the largest included files, compares a candidate
ignore file without editing either file, inspects one explicitly saved inventory,
compares saved inventory
snapshots with or without a live context, checks total/directory/extension/intersection
logical payload byte budgets, and reports contribution groups with independently
verifiable candidate ignore advice.
No daemon, Docker installation, network, credentials or paid APIs
are needed. It never executes Dockerfile instructions or user scripts.

## Install

Requires Linux and Python 3.12 or newer (tested with CPython 3.14.4). This command
has no runtime dependencies beyond the standard library.

```sh
pipx install stackwrench                # or: pip install stackwrench
stackwrench context-budget --version    # prints the bare stackwrench version, e.g. 0.1.0

# Or run without installing:
uvx stackwrench context-budget --help
```

`python -m stackwrench context-budget` is equivalent to `stackwrench context-budget`.
The examples below use files from a stackwrench source checkout; run them from its root.

## Usage and complete examples

```text
stackwrench context-budget [compare] CONTEXT
  [--dockerfile CONTEXT_RELATIVE_FILE]
  [--ignore BASELINE_FILE]
  [--candidate-ignore CANDIDATE_FILE | --save-snapshot NEW_FILE | --baseline-snapshot EXISTING_FILE]
  [--max-bytes NONNEGATIVE_INTEGER] [--largest NONNEGATIVE_INTEGER] [--json]
  [--max-dir-bytes DIR=BYTES ...] [--max-extension-bytes EXT=BYTES ...]
  [--max-dir-extension-bytes DIR EXT=BYTES ...]
  [--top NONNEGATIVE_INTEGER]

stackwrench context-budget inspect-snapshot SNAPSHOT
  [--path EXACT_RELATIVE_PATH | --subtree EXACT_RECORDED_DIRECTORY | --extension EXACT_FINAL_SUFFIX |
   --subtree-extension EXACT_RECORDED_DIRECTORY EXACT_FINAL_SUFFIX | --decision included|excluded]
  [--limit NONNEGATIVE_INTEGER] [--json]

stackwrench context-budget compare-snapshots BASELINE CURRENT
  [--max-bytes NONNEGATIVE_INTEGER] [--max-dir-bytes DIR=BYTES ...]
  [--max-extension-bytes EXT=BYTES ...] [--max-dir-extension-bytes DIR EXT=BYTES ...]
  [--top NONNEGATIVE_INTEGER] [--top-changes NONNEGATIVE_INTEGER] [--json]
```

The optional `compare` alias requires `--candidate-ignore`. Without the alias,
providing `--candidate-ignore` also requests a comparison. A directory literally
named `compare`, `compare-snapshots` or `inspect-snapshot` can be passed with a `./` prefix.

All rule-file arguments are relative to the **current working directory**.
`--dockerfile` is relative to the **context root**. For example:

```sh
# Real, committed synthetic data; no creation or build step needed.
stackwrench context-budget examples/context_budget/context --largest 1
stackwrench context-budget compare examples/context_budget/context --ignore examples/context_budget/context/.dockerignore --candidate-ignore examples/context_budget/lean.dockerignore --max-bytes 20 --json

# Failure paths: the shell asserts the documented nonzero status.
stackwrench context-budget examples/context_budget/context --max-bytes 1019 --json
test "$?" -eq 1
stackwrench context-budget examples/context_budget/context --candidate-ignore examples/context_budget/invalid.dockerignore --json
test "$?" -eq 2
```

Run failure examples in a shell without `set -e`; tests assert those statuses
directly using subprocesses. Exit codes: **0** complete evaluation within budget
(or no budget), **1** complete evaluation exceeding budget, **2** invalid,
unreadable, unsupported or incomplete input. Argument parsing errors use stderr;
analysis errors with `--json` produce a JSON `status: "error"` object and no
partial budget result. `--max-bytes` accepts integer bytes, not `100KB` suffixes.
In candidate-ignore comparisons, the exit code checks the **candidate**, and
each side also reports its own budget result. Saved-only comparisons check
**CURRENT saved metadata** when total, directory, extension or intersection limits are requested.

The baseline synthetic payload is `src/a.py` (20 bytes) and `build/b.bin`
(1000 bytes). The candidate's line 2, `build/**`, excludes the latter. Baseline
is **1020 bytes / 2 files**, candidate **20 bytes / 1 file**, candidate minus
baseline **−1000 bytes / −1 file**. The sample Dockerfile is illustrative;
the tool neither runs it nor proves that a candidate preserves build behavior.

These are measured local file sizes under a **static inclusion model**, not
measured Docker transfer, compression, image size, build time or speedup. No
network/compression/timing improvement is claimed.

## Directory/extension budgets and candidate ignore advice

`--max-dir-bytes` and `--max-extension-bytes` are repeatable, exact integer-byte
limits. All specified limits must pass; **equality passes**. Exit 1 means at
least one limit is exceeded, even when the total passes. Candidate-ignore mode
checks the candidate; snapshot modes check the current inventory. Directory
selectors must name an existing, normalized context-relative directory (no
leading/trailing/repeated slash, `.` or `..` components). A missing directory or
a file/symlink selector is an input error, exit 2. Use `--max-bytes` for the root.
Duplicate selectors within one dimension and malformed/negative limits are
errors, never silently overwritten. Split on the final `=` so directory names
containing `=` work. Quote shell-sensitive selectors.

Directories count their whole subtree, including reincluded descendants.
Nested directory totals **overlap and must not be added**. Extensions partition
included payload files by their case-sensitive final suffix: `.tar.gz` counts
as `.gz`, `.BIN` differs from `.bin`, and `.hidden`, `name.` and names without a
suffix use `<none>`. Use `--max-extension-bytes '<none>=1000'` for that group.
An absent extension has zero bytes. Controls and symlinks contribute no payload
bytes in either dimension; hardlink names and sparse logical sizes use the same
accounting as the total budget.

`--max-dir-extension-bytes DIR EXT=BYTES` limits the **intersection**: only
included payload files under `DIR/` whose final suffix is exactly `EXT` count.
It takes **two operands**, so directory names containing `=` need no extra
delimiter or escaping beyond shell quoting. Repeat the flag for multiple pairs;
the same directory with different suffixes, or the same suffix under different
directories, is allowed. An exact duplicate pair is an error. Directory, suffix,
integer and exit-code rules are the same as above, including `<none>`, zero
bytes for absent suffixes, and equality passing. A directory named `cache.bin`
does not make its `source.py` descendant count as `.bin`. Sibling `building/`
does not count under `build/`. Nested pairs can overlap. All four kinds of
configured limits must pass, independently of contribution top-N.

In live scan modes, `--top N` (default 10) limits each dimension's contribution list and the number
of suggestions independently. Groups sort by descending bytes, then ascending
path/extension; suggestions sort by descending savings, then ascending pattern.
`--largest` independently limits individual files. Top-N never truncates budget
evaluation or configured budget results. `--top 0` suppresses contribution rows
and suggestions while still evaluating every threshold. Root-level files enter
extension and total counts; the directory list contains actual subdirectories,
including zero-byte subtrees, without a synthetic root group.

For the committed **1020-byte** payload (`build/b.bin`: 1000, `src/a.py`: 20):

```sh
stackwrench context-budget examples/context_budget/context --max-bytes 2000 --max-dir-bytes build=999 --top 3 --json
test "$?" -eq 1  # total passes; build alone exceeds its limit
stackwrench context-budget examples/context_budget/context --max-dir-bytes build=1000 --max-extension-bytes .bin=1000
test "$?" -eq 0  # equality passes
stackwrench context-budget examples/context_budget/context --max-dir-extension-bytes build .bin=999 --json
test "$?" -eq 1  # only .bin payload under build/: 1000 bytes
stackwrench context-budget examples/context_budget/context --max-dir-extension-bytes build .bin=1000
test "$?" -eq 0
```

JSON scenarios add `contributions.directories`, `contributions.extensions` and
`ignore_suggestions`. Each contribution has its selector, `included_payload_bytes`
and `included_payload_files`. `budget.passed` checks **all** configured limits
(null if none); `budget.total_passed` checks only `max_bytes` (null if absent).
`budget.directories` and `budget.extensions` list every configured limit with
its observed bytes and pass result, sorted by selector.
`budget.directory_extensions` lists every configured pair, sorted by
`(path, extension)`, with `path`, `extension`, `included_payload_bytes`,
`included_payload_files`, `max_bytes` and `passed`; it is empty without pair
limits. Zero-byte included files count toward the file count. For `build .bin=999`
above the row reports 1000 bytes, 1 file and `passed: false`. There is no
Cartesian product contribution list. The library equivalent is
`analyze(context, max_dir_extension_bytes={("build", ".bin"): 999})`.

When a threshold is exceeded, advice considers literal paths for top-N
directories/files, suffix patterns for top-N extensions, and every exceeded
directory/extension selector. Exceeded intersection pairs also propose a scoped
`DIR/**/*EXT` pattern with literal selector characters escaped. For `<none>`,
they propose up to N matching literal file paths, since the matcher has no exact
suffix-less glob. These candidates are considered even when the pair's files
are below the global contribution top-N. A suggestion must save positive payload bytes and
reduce at least one exceeded limit. It need not fix all limits. Each is parsed
and checked with the existing matcher on the same inventory, as **one exclusion
appended after all rules of that scenario**, including negations. Suggestions
are independent and their savings are **not additive**. Nothing is applied,
deleted or rewritten. To verify one, copy the scenario's rule text to an external
candidate file, add a newline and that one `append_pattern`, then run
`--candidate-ignore` with the same thresholds, baseline `--ignore` selection
and Dockerfile.

Each suggestion reports `logical_payload_bytes_delta`, file-count delta,
`scope`, every currently included path whose decision would change in
`changed_entries` (including controls/directories/symlinks), and `budget_after`.
Scoped patterns have `scope.kind: "directory_extension_pattern"`, the directory
in `scope.selector`, and `scope.extension`; `budget_after` includes every pair.
The `build` suggestion in this example saves **1000 logical payload bytes**;
`**/*.bin` also saves 1000 here. An extension pattern can match directories
named with that suffix, excluding descendants with other extensions too; those
changes are explicitly listed and counted. All patterns may affect future
matching paths/descendants. Review build inputs: a passing estimate does not
prove a build still works or that controls remain COPY-accessible.

No unsupported pattern is emitted. Literal metacharacters are escaped. Names
containing line/control characters, boundary whitespace, a leading BOM, or
other unrepresentable/overlong input are omitted from advice rather than
approximated. The advice pool is bounded by top-N contributors and exceeded
selectors; it is not an exhaustive or minimum-damage exclusion search. A failed
budget may have no safe representable suggestion. Changed-path lists are not
truncated. Deltas describe this static logical model, not measured transfer.

## Inventory snapshots across scans

Create a baseline explicitly, then compare later scans of the **same absolute
context path and selected Dockerfile**. Snapshot paths are cwd-relative and
must be **outside the context**, even if the destination would be ignored.
The parent directory must already exist. Creation never overwrites a file;
use a new filename to establish a new baseline. Comparison never creates or
updates a baseline. Neither command writes to the input tree.

```sh
# Choose a fresh destination outside examples/context_budget/context.
stackwrench context-budget examples/context_budget/context --save-snapshot /tmp/context-baseline.json --json
stackwrench context-budget examples/context_budget/context --baseline-snapshot /tmp/context-baseline.json --json
# Immediately after creation: changes=[], byte delta=0, file delta=0.
```

Snapshots store path, entry kind, logical size, control role, ignore decision
and last matching rule provenance. They read no payload contents, hashes,
symlink text or symlink target contents. Only the effective ignore file and,
when comparing, the saved JSON metadata are read. Same-size content edits,
mtime changes and changes only to a symlink's target are intentionally invisible.
Symlinks remain zero-byte inventory entries and are never traversed.

JSON reports add `snapshot_written` on creation or `snapshot_comparison` when
comparing. The latter contains saved `baseline` and `current` summaries,
current-minus-saved logical payload byte/file deltas, and path-sorted `changes`.
Each change includes complete `before`/`after` metadata (null for absence),
per-path payload deltas and reasons: `added`, `removed`, `kind_changed`,
`size_changed`, `role_changed`, `inclusion_changed`, `rule_changed`.
Provenance-only changes are visible even if byte counts are unchanged. Default
inclusion has a null rule. Entries include excluded files, directories and
controls for explanation, while totals count only included regular payload
files. A zero-byte file addition changes the file count but not bytes.
The existing top-level `baseline` field describes the **current scan's baseline
rules**; the saved inventory summary is `snapshot_comparison.baseline`.

Unlike candidate-ignore comparison, a fresh inventory scan reselects the active
ignore file and control roles. Both saved and current sources/selections are
reported, and control-role transitions appear in changes. Snapshot modes cannot
be combined with `--candidate-ignore` or each other. `--max-bytes` always checks
the current scan; differences alone do not cause a nonzero exit. A complete
scan can be saved even if it exceeds that budget (exit 1). Missing, unreadable,
malformed, incompatible or oversized baselines fail with exit 2 and no partial
budget result. Snapshot files use format `docker-context-budget.inventory`,
schema version 1, strict fields, unique sorted paths and a 64 MiB size limit.
They are metadata supplied by the operator, not authenticated evidence.
Symlink components are rejected for both input and output snapshot paths.

## Inspect one saved snapshot without a context scan

`inspect-snapshot SNAPSHOT [--path EXACT_RELATIVE_PATH |
--subtree EXACT_RECORDED_DIRECTORY | --extension EXACT_FINAL_SUFFIX |
--subtree-extension EXACT_RECORDED_DIRECTORY EXACT_FINAL_SUFFIX]
[--limit NONNEGATIVE_INTEGER] [--json]` reads only
the saved inventory artifact. The recorded context, selected Dockerfile and
ignore source may all have been deleted. It never opens those paths, payloads
or symlink targets, scans a context, evaluates budgets, generates advice or
hashes file contents. Artifact paths are cwd-relative, reported as absolute
paths, and may be copied anywhere. The strict version-1 loader validates the
**complete inventory** before any selection or output, including sorted unique
paths, parents, control roles, inclusion decisions and rule provenance. The
64 MiB bound, regular-file requirement and rejection of symlink components
apply to the artifact path.

```sh
stackwrench context-budget inspect-snapshot /tmp/context-before.json --json
stackwrench context-budget inspect-snapshot /tmp/context-before.json --path build/b.bin
stackwrench context-budget inspect-snapshot /tmp/context-before.json --path .dockerignore --json
stackwrench context-budget inspect-snapshot /tmp/context-before.json --subtree build --limit 20 --json
stackwrench context-budget inspect-snapshot /tmp/context-before.json --subtree build --limit 0
stackwrench context-budget inspect-snapshot /tmp/context-before.json --extension .bin --limit 20 --json
stackwrench context-budget inspect-snapshot /tmp/context-before.json --extension '<none>' --limit 0
stackwrench context-budget inspect-snapshot /tmp/context-before.json --subtree-extension build .bin --limit 20 --json
stackwrench context-budget inspect-snapshot /tmp/context-before.json --subtree-extension build '<none>' --limit 0
stackwrench context-budget inspect-snapshot /tmp/context-before.json --decision excluded --limit 20 --json
stackwrench context-budget inspect-snapshot /tmp/context-before.json --decision included --limit 0
```

Success returns **0** with these JSON fields: `schema_version: 1`,
`status: "ok"`, `model` (the recorded accounting model) and
`snapshot_inspection`. The latter has these fields:

- `snapshot`: absolute artifact path; `format: "docker-context-budget.inventory"`;
  `schema_version: 1` (the saved format version).
- `summary`: recorded `context`, `dockerfile`, `ignore_selection`, `rules_source`
  (null without rules), plus `included_payload_bytes` and
  `included_payload_files`. Totals are calculated from **all validated saved
  rows**, using included regular payload entries only; zero-byte files count
  as files. Version 1 stores row sizes and decisions, not aggregate totals.
- `entry_count`: all recorded entries, including directories, controls,
  excluded entries and symlinks.
- `selected_entry`: present **only** with `--path`, containing the complete
  recorded row: `path`, `kind`, `logical_bytes`, `role`, `included_by_rules`,
  `last_rule`. A non-null rule retains all six fields: `source`, `line`, `raw`,
  `pattern`, `negated`, `matched_path`. Null means default inclusion.
- `selected_subtree`: present **only** with `--subtree`, an object containing
  `path` (the exact selected directory), `matching_entry_count` (the directory
  row plus all descendants), `included_payload_bytes`, `included_payload_files`,
  `entries` (complete metadata/provenance rows in ascending path order),
  `returned_entry_count` (the length of `entries`) and `truncated` (returned
  count is less than matching count). Counts and payload totals cover the
  **complete subtree before truncation**, including zero-byte file counts.
  Only included regular payload contributes bytes/files; controls, symlinks,
  directories and excluded rows still appear in the listing and matching count.
  `summary` and `entry_count` remain inventory-wide.
- `selected_extension`: present **only** with `--extension`, an object containing
  `extension`, `matching_entry_count`, `included_payload_bytes`,
  `included_payload_files`, `entries`, `returned_entry_count` and `truncated`.
  It lists complete regular-file metadata/provenance in ascending path order.
  Matching count includes **every regular-file match**, including controls and
  excluded rows. Payload totals count only included regular payload files;
  zero-byte included files count. All counts cover the full selection before
  limiting; `summary` and `entry_count` remain inventory-wide. Directories and
  symlinks never match. An absent suffix succeeds with zero counts, empty rows
  and `truncated: false`.

- `selected_subtree_extension`: present **only** with `--subtree-extension`,
  containing `path`, `extension`, `matching_entry_count`,
  `included_payload_bytes`, `included_payload_files`, `entries`,
  `returned_entry_count` and `truncated`. It lists regular-file rows under
  `DIR/` with the exact final suffix, across all roles and inclusion decisions.
  Full matching counts and included regular payload totals precede path-sorted
  limiting; zero-byte payload files count. Controls and excluded files appear
  but contribute no payload totals; symlinks and directories never match.
  Empty directories and absent suffixes succeed with zeros, empty rows and
  `truncated: false`. `summary` and `entry_count` remain inventory-wide.

- `selected_decision`: present **only** with `--decision`, containing `decision`
  (`included` or `excluded`), `matching_entry_count`, `included_payload_bytes`,
  `included_payload_files`, `entries`, `returned_entry_count` and `truncated`.
  It selects every recorded kind and role whose stored `included_by_rules`
  boolean equals the requested decision, including directories, controls,
  symlinks and zero-byte files, with complete metadata/provenance in ascending
  path order. Payload totals count only included regular payload files, so the
  excluded selection always reports zero bytes and zero files while still
  retaining each row's recorded `logical_bytes`. An empty selection succeeds
  with zero counts, `entries: []` and `truncated: false`. `summary` and
  `entry_count` remain inventory-wide.

`--path` selection is exact and case-sensitive. It does not normalize, trim, resolve,
expand globs or select descendants. Paths must be nonempty normalized relative
inventory paths: no leading/trailing/repeated slash, NUL, `.` or `..` component.
The context root has no row. Other characters, including Unicode, whitespace,
backslashes and terminal controls, are literal. For a name beginning with `-`,
use `--path=-name`. Directories, excluded files, controls and symlinks are
selectable just like included payload files; selection never changes totals.
Unknown paths, malformed paths and repeated `--path` are errors, never empty or
partial successes. An invalid late row invalidates the whole inventory even
when the selected row occurs earlier.

`--subtree` uses the same literal path rules and requires an actual recorded
**directory** row. It selects that row plus exactly the paths beginning with
`DIR/`: selecting `build` excludes `build2` and `build.bin`. Nested, empty and
excluded directories are selectable; reincluded descendants count normally.
Files (including controls), symlinks, unknown paths and root aliases are errors.
For a directory beginning with `-`, use `--subtree=-name`. Globs are never
interpreted; a literal metacharacter name must itself be a recorded directory.

`--extension` uses the existing exact, case-sensitive **final suffix** rules:
`.tar.gz` matches `.gz`, `.BIN` differs from `.bin`, and leading-only dotfiles
such as `.hidden`, trailing-dot names and names without a suffix match `<none>`.
A name such as `.hidden.bin` matches `.bin`. Quote `'<none>'` in a shell.
Selectors must be `<none>` or a nonempty final suffix beginning with a dot,
without NUL or `/`; `.`, `..`, `.bin.` and `.tar.gz` are invalid selectors.
Nothing is trimmed or expanded: `.*` matches a literal final suffix `.*`,
not every suffix. Files anywhere in the inventory can match, including nested
and sibling directories; suffix-like directory names do not select descendants.
All roles and inclusion states are eligible, so a control-only or excluded-only
suffix can have matching rows with zero payload bytes **and zero payload files**.

`--subtree-extension DIR EXT` is a dedicated **two-operand** selector. DIR
must be an actual exact normalized recorded directory, using the same literal
path rules as `--subtree`; EXT uses the exact final-suffix rules above. Selecting
`build .bin` excludes `build2/a.bin`, `build.bin` and `build/a.BIN`, while
including nested `build/nested/a.bin`. An excluded directory can contain
reincluded payload files. No Cartesian groups, glob interpretation, root alias
or live existence check is added. For a directory beginning with `-`, use
`--subtree-extension=-name .bin`. The Python API accepts the pair as
`inspect_snapshot(saved, subtree_extension=("build", ".bin"), limit=20)`.

`--decision included|excluded` is a dedicated selector for the **stored**
`included_by_rules` boolean only. Its value must be exactly lowercase `included`
or `excluded`; case variants, prefixes and whitespace are errors. It reports the
decision exactly as recorded at save time; the Python API accepts it as
`inspect_snapshot(saved, decision="excluded", limit=0)`. It never re-evaluates ignore rules,
reads live paths or hashes content, so the answer does not change if the rule
file has since been edited or deleted. It cannot be combined with `--path`,
`--subtree`, `--extension` or `--subtree-extension`.

`--limit` is accepted only with `--subtree`, `--extension`,
`--subtree-extension` or `--decision` and defaults to
**100**. It uses the existing nonnegative integer parser. A zero limit returns
`entries: []`, with complete matching counts and payload totals; `truncated`
is true exactly when matching rows were omitted. Even an empty directory has
its own row, so zero truncates that subtree. A sufficient limit returns every
row with `truncated: false`. Limiting never bypasses the strict bounded loader:
an invalid late row outside the selection still fails at limit zero, including
an absent suffix, empty pair or empty decision selection. `--path`, `--subtree`,
`--extension`, `--subtree-extension` and `--decision` are mutually exclusive;
repeated selection flags, `--limit` or `--json` are errors. Default, exact-path,
subtree, extension and pair JSON/text are unchanged without decision mode.

Text reports recorded identities, rules, entry and payload totals, then every
selected metadata/provenance field. Subtree, extension, pair and decision text label complete
payload totals, returned/matching entry counts and truncation before the returned
rows. Operator strings are escaped with `repr`;
JSON uses ASCII escapes. Both formats are deterministic. Saved metadata is
unauthenticated and says nothing about current contents, freshness, hashes,
Docker transfer, compression, image size or build correctness.

Artifact/selection errors return **2**: JSON contains only `schema_version: 1`,
`status: "error"`, `error`; text emits only a diagnostic on stderr. Missing
operands, malformed/negative limits and unrelated scan/comparison/budget flags
are parser errors on stderr
with no stdout. Inspection never returns the budget-exceeded exit code 1.
The inventory format and existing scan/comparison outputs are unchanged.

## Compare two saved snapshots without a context scan

`compare-snapshots BASELINE CURRENT [--max-bytes NONNEGATIVE_INTEGER]
[--max-dir-bytes DIR=BYTES ...] [--max-extension-bytes EXT=BYTES ...]
[--max-dir-extension-bytes DIR EXT=BYTES ...] [--top NONNEGATIVE_INTEGER]
[--top-changes NONNEGATIVE_INTEGER] [--json]`
reads only the two saved JSON files. It never opens the recorded context,
Dockerfile, ignore sources, payload
files or symlink targets; those live inputs may be absent. Both snapshots must
record the **same absolute context path and selected Dockerfile**. Renaming or
copying the saved JSON files is allowed: their paths are cwd-relative, can be
anywhere, and are reported as normalized absolute `baseline_snapshot` and
`current_snapshot` paths. The external-path requirement still applies when
creating a snapshot or comparing a saved snapshot against a live scan.

```sh
# Save both while the context is available; use fresh external destinations.
stackwrench context-budget examples/context_budget/context --save-snapshot /tmp/context-before.json --json
# After making the desired context changes, save a second inventory explicitly.
stackwrench context-budget examples/context_budget/context --save-snapshot /tmp/context-after.json --json
# This command works even after the context and its rule files are removed.
stackwrench context-budget compare-snapshots /tmp/context-before.json /tmp/context-after.json --json
```

Without budget flags, `--top` or `--top-changes`, JSON output contains exactly `schema_version: 1`,
`status: "ok"`, `model` (the snapshot accounting model), and
`snapshot_comparison`. The comparison reuses
the saved-versus-live structure above, adding `current_snapshot`. Its `baseline`
and `current` summaries describe the two saved inventories; deltas always mean
**CURRENT minus BASELINE**, with the same path-sorted reasons, full before/after
metadata and rule provenance. There are no synthetic live-scan fields.
Text output labels both saved files, shows the recorded context/Dockerfile and
escapes paths/rule text for terminal safety.

Without a budget, exit **0** means a complete comparison, including nonempty
differences. Optional `--max-bytes` evaluates only the validated CURRENT saved
summary's included regular-file payload bytes, never the baseline or delta.
Equality passes: for CURRENT = 16 bytes, limit 16 returns **0**, limit 15 returns
**1**, even if a larger baseline makes the delta negative. With only a total
limit and neither contribution flag, the report adds only the top-level optional field below; `snapshot_comparison` is unchanged:

```json
{"current_saved_budget": {"max_bytes": 15, "included_payload_bytes": 16, "passed": false}}
```

`status` is `"ok"` when every requested budget passes, `"budget_exceeded"` otherwise.
Text output adds `Current saved budget: 16 / 15 bytes; EXCEEDED` (or `PASS`).
Zero is a valid threshold and passes for an empty payload. Controls, excluded
files and symlinks do not enter the payload total. Contribution rows require
explicit `--top` for CURRENT summaries or `--top-changes` for group deltas;
ignore advice is never emitted from saved metadata.

Optional repeatable `--max-dir-bytes DIR=BYTES` evaluates directory subtrees
using **CURRENT saved entries**. `DIR` must be a normalized, context-relative
non-root directory present as a directory row in CURRENT. BASELINE-only,
missing, file and symlink selectors are errors; live directories are never
consulted. Split on the final `=` so `cache=a=8` selects `cache=a`.
Only included regular payload files count. Controls, symlinks and excluded
files contribute neither bytes nor files; zero-byte included files count.
Excluded directories can have reincluded descendants that count. Nested
subtrees overlap; `building/` does not count under `build/`. Empty directories
have zero bytes/files; use `--max-bytes` for the root.

```sh
stackwrench context-budget compare-snapshots /tmp/context-before.json /tmp/context-after.json --max-dir-bytes build=1020 --max-dir-bytes build/nested=7 --json
```

Only directory flags add `current_saved_directory_budgets`, sorted by path
and containing every requested limit. For CURRENT `build/` = 1020 bytes across
five files, 1020 passes (exit **0**) and 1019 exceeds (exit **1**), regardless of
BASELINE size or delta:

```json
{
  "current_saved_directory_budgets": [
    {"path": "build", "included_payload_bytes": 1020,
     "included_payload_files": 5, "max_bytes": 1019, "passed": false}
  ],
  "current_saved_budgets_passed": false
}
```

Text adds `Current saved directory budget 'build': 1020 / 1019 bytes; 5 files; EXCEEDED`
(or `PASS`). Directory, extension, intersection and total limits may be combined;
all must pass. Without directory flags, existing comparison, budget rows and
text output retain their shape.

Optional repeatable `--max-extension-bytes EXT=BYTES` evaluates exact,
case-sensitive final suffixes across **CURRENT saved entries**. It reuses the
live suffix validation: `.tar.gz` counts as `.gz`, `.BIN` differs from `.bin`,
and dotfiles such as `.hidden`, suffix-less names and trailing dots use `<none>`.
Only included regular payload files count; zero-byte files count as files,
while controls, symlinks and excluded files contribute neither bytes nor files.
An absent suffix has zero bytes/files. No live inputs are required or accessed.

```sh
stackwrench context-budget compare-snapshots /tmp/context-before.json /tmp/context-after.json --max-extension-bytes .bin=1017 --max-extension-bytes '<none>=10' --json
```

Only extension flags add `current_saved_extension_budgets`, sorted by suffix
and containing every requested limit. For CURRENT `.bin` payload of 1017 bytes
across four files, 1017 passes (exit **0**) and 1016 exceeds (exit **1**):

```json
{
  "current_saved_extension_budgets": [
    {"extension": ".bin", "included_payload_bytes": 1017,
     "included_payload_files": 4, "max_bytes": 1016, "passed": false}
  ],
  "current_saved_budgets_passed": false
}
```

BASELINE's size and the comparison delta do not decide this budget. Text adds
`Current saved extension budget '.bin': 1017 / 1016 bytes; 4 files; EXCEEDED`
(or `PASS`). Directory, extension, intersection and total limits can be combined; **all**
must pass. `current_saved_budgets_passed` is emitted whenever directory, extension or pair
limits are requested and covers every requested limit, including `--max-bytes`.
The existing comparison, total-only budget and no-budget schema stay unchanged.

Optional repeatable `--max-dir-extension-bytes DIR EXT=BYTES` evaluates exact
directory-subtree/final-suffix intersections using **CURRENT saved entries**.
The same pair validation and accounting as live mode apply: case-sensitive
final suffix, `<none>`, included regular payload only, zero-byte file counts,
and no controls or symlinks. `DIR` must exist as a directory row in CURRENT,
even if excluded by its saved rules; a reincluded child can still count. A
directory present only in BASELINE, a file or a symlink is an error. No live
directory is required or consulted. Nested pairs overlap; absent suffixes have
zero bytes/files. An exact duplicate pair is an error.

```sh
stackwrench context-budget compare-snapshots /tmp/context-before.json /tmp/context-after.json --max-dir-extension-bytes build .bin=1000 --json
```

For CURRENT `build/a.bin` = 1000 bytes, the limit above passes (exit **0**);
999 exceeds (exit **1**). Same-suffix files outside `build/`, other suffixes
inside it, and BASELINE's sizes do not decide this pair's result. Only pair
limits add `current_saved_directory_extension_budgets`; pair-only JSON is shown
below. Rows sort by `(path, extension)` and include every requested pair:

```json
{
  "current_saved_directory_extension_budgets": [
    {"path": "build", "extension": ".bin", "included_payload_bytes": 1000,
     "included_payload_files": 1, "max_bytes": 999, "passed": false}
  ],
  "current_saved_budgets_passed": false
}
```

`current_saved_budgets_passed` checks **all** directory, pair, extension and optional total
limits; equality passes. A passing total cannot override a failed pair,
or vice versa. The existing `current_saved_budget` retains its total-only
meaning and shape. `snapshot_comparison` stays unchanged. Text adds
`Current saved intersection budget 'build' extension '.bin': 1000 / 999 bytes; 1 files; EXCEEDED`
and `Current saved budgets: EXCEEDED` (or `PASS`). Without pair flags, no pair
rows or intersection text is emitted; without directory, extension or pair flags,
the combined pass field and combined status text are absent.

Optional **explicit** `--top N` adds CURRENT saved contribution summaries, using
only the validated CURRENT rows. Unlike live mode, it has no default output:
omitting both contribution flags preserves the existing JSON and text byte-for-byte. Only the
explicit flag adds `current_saved_contributions` with `directories` and
`extensions` lists. Each list independently sorts by descending
`included_payload_bytes`, then ascending path/extension, and keeps at most N
rows. `--top 0` emits both empty lists. For example:

```sh
stackwrench context-budget compare-snapshots /tmp/context-before.json /tmp/context-after.json --top 1 --json
```

For a saved CURRENT with 15 bytes / 5 files under `build/` and 23 bytes / 6
files with final suffix `.bin`, leading groups have this shape:

```json
{
  "current_saved_contributions": {
    "directories": [{"path": "build", "included_payload_bytes": 15, "included_payload_files": 5}],
    "extensions": [{"extension": ".bin", "included_payload_bytes": 23, "included_payload_files": 6}]
  }
}
```

Directory rows include actual CURRENT saved directories, even empty or excluded
ones; subtree totals overlap and must not be added. There is no synthetic root
group. Reincluded descendants count under excluded ancestors; siblings such as
`building/` do not count under `build/`. Extensions partition all included
regular payload files, including root-level files, by the case-sensitive final
suffix described above. Zero-byte files count and create suffix groups; absent
suffixes and suffixes appearing only on controls/symlinks/excluded files do not
create groups. An empty CURRENT produces empty lists. BASELINE sizes and
comparison deltas do not decide contributions. No recorded live path is opened,
and no advice or Cartesian intersection contributions are generated.

Text adds `Current saved contributions: directory subtrees overlap; final extensions are case-sensitive.`
followed by escaped selector rows such as `directories: 'build': 15 bytes / 5 files`
and `extensions: '.bin': 23 bytes / 6 files`. The heading is present even at
`--top 0`. Every comparison and configured budget row remains unchanged;
all limits are fully evaluated, including those below either top cutoff.
Contribution output alone does not change status or exit decisions.

Optional **explicit** `--top-changes N` adds `saved_contribution_changes` with
`directories` and `extensions` lists. It aggregates **both validated saved
inventories** using the same included regular payload accounting, independently
of CURRENT `--top`. Each changed group from the union of both sides reports its
`path` or `extension`, `baseline_included_payload_bytes`,
`baseline_included_payload_files`, `current_included_payload_bytes`,
`current_included_payload_files`, `included_payload_bytes_delta` and
`included_payload_files_delta`. Deltas mean **CURRENT minus BASELINE**;
a group absent on either side has zero bytes/files there.

Each list independently sorts by **descending absolute byte delta**, then
ascending selector (including opposite-sign ties), and keeps at most N rows.
Only groups with both zero byte delta **and** zero file delta are omitted.
Zero-byte file additions/removals remain visible; unchanged groups,
provenance-only changes and added/removed empty directories are omitted.
Actual directory ancestors overlap, siblings stay separate, and there is no
synthetic root. Exact final suffix/case/`<none>` semantics, reinclusion, and
control/symlink/exclusion filtering apply separately on each side.

```sh
stackwrench context-budget compare-snapshots /tmp/context-before.json /tmp/context-after.json --top-changes 1 --top 1 --max-bytes 44 --json
```

For example, an isolated subtree growing from 2 bytes / 1 file to 12 bytes /
2 files, while `.bin` grows from 9 bytes / 3 files to 21 bytes / 3 files,
can lead the two lists as follows:

```json
{
  "saved_contribution_changes": {
    "directories": [{"path": "a/nested", "baseline_included_payload_bytes": 2,
      "baseline_included_payload_files": 1, "current_included_payload_bytes": 12,
      "current_included_payload_files": 2, "included_payload_bytes_delta": 10,
      "included_payload_files_delta": 1}],
    "extensions": [{"extension": ".bin", "baseline_included_payload_bytes": 9,
      "baseline_included_payload_files": 3, "current_included_payload_bytes": 21,
      "current_included_payload_files": 3, "included_payload_bytes_delta": 12,
      "included_payload_files_delta": 0}]
  }
}
```

`--top-changes 0` emits both empty lists after validating both artifacts.
Without this flag, the field and its text section are absent and existing
JSON/text stays byte-identical. Text appends
`Saved contribution changes: CURRENT minus BASELINE; directory subtrees overlap; final extensions are case-sensitive.`
and repr-escaped rows, for example
`directories: 'a/nested': baseline 2 bytes / 1 files; current 12 bytes / 2 files; delta +10 bytes / +1 files`.
The heading remains at N=0. Existing comparison, CURRENT contributions, full
budget results, status and exit decisions are unchanged; a valid comparison
without budgets exits **0** even with changed groups. No advice, snapshot
format change or live access is introduced.

Other scan, ignore and presentation flags such as `--ignore`, `--dockerfile`
and `--largest` remain rejected. Malformed/negative/missing `--top` or
`--top-changes` values are
parser errors (exit **2**, stderr, no stdout even with `--json`). Malformed,
negative or missing total thresholds are argument errors (exit **2**, stderr,
no stdout even with `--json`). Malformed/negative/duplicate directory or extension limits,
non-final suffixes such as `.tar.gz`, malformed/duplicate pair values and invalid or
missing CURRENT directory selectors return **2**, with error-only JSON when
`--json` is active. Missing directory, extension or pair operands at the end
of a command are parser errors (stderr, no stdout). No partial comparison or budgets are printed.
Missing, unreadable, nonregular, oversized, malformed or incompatible inputs
return **2**; with `--json` they emit only
`schema_version`, `status: "error"` and `error`, with no partial comparison or
budget or contribution result, even when thresholds, `--top` or `--top-changes`
were requested.
Both inputs use the same strict version-1 validation, duplicate-key rejection,
64 MiB per-file limit and symlink-component rejection as live snapshot mode.
Neither saved file is written or replaced. Self-comparison gives `changes: []`
and zero deltas; reversing inputs reverses the byte/file deltas.

## Ignore semantics and supported input

Only one local filesystem directory is accepted, on Linux. There are no
implicit exclusions for `.git`, dependencies or hidden files. Regular files,
directories and symlinks are represented; ordinary inspection displays symlink
text without opening, reading or traversing target contents. Snapshot modes
omit even symlink text. Root/rule-file path components must also not
be symlinks. Special devices, sockets and FIFOs cause an explicit error even
when ignored. File/dir read permission errors, including mode-000 entries,
prevent a budget pass. All directories are inspected, including ignored ones,
so a later `!` can reinclude a descendant. This is intentionally more
conservative than a builder that prunes directories.

Automatic rule selection uses `<selected Dockerfile>.dockerignore` if present,
including an empty file, ahead of root `.dockerignore`; they are **not merged**.
The default Dockerfile name is `Dockerfile`. A context without that default file
is allowed for inspection; an explicitly selected non-default Dockerfile must
exist. `--ignore` overrides automatic selection. Each rule file must be a
regular UTF-8 file, at most 4 MiB, with lines at most 65535 bytes.

This independent matcher implements a documented **Docker-ignore subset**,
checked by the golden cases in `tests/test_ignore.py` and filesystem/CLI tests:

- `*` and `?` match within a path component, including leading dots.
- `**` as a complete component matches across zero or more directories:
  `**/*.tmp`, `src/**/generated`, `build/**`, `**`.
- Character classes: `[abc]`, `[a-z]`, Go-style negation `[^a-z]`, Unicode
  characters/ranges and escaped literal class characters. `[!a]` means the
  literal characters `!` or `a`, not shell-style negation. Class ranges require
  both endpoints; escape literal `-`. Classes cannot contain a slash.
- Leading/trailing whitespace is trimmed. UTF-8 BOM and CRLF are supported.
  Only `#` in column 1 starts a comment; ` #name` is a pattern for `#name`.
  Inline `#` is literal. Blank lines and the cleaned pattern `.` are ignored.
- POSIX path cleaning removes `.`/`..` components and redundant slashes.
  Leading/trailing slashes do not change anchoring. Both `cache` and `/cache/`
  mean `cache` at the context root; neither matches `nested/cache`. Use
  `**/cache` for arbitrary depth. A matched directory affects its descendants.
- Rules apply in order; the last matching rule on a path **or its ancestors**
  decides inclusion. `!` reincludes, including beneath an ignored parent.
  Provenance records the last match even when it repeats an existing decision.
- Backslash escapes literal `* ? [ ] ! # \\`, space, `-` or `^` on Linux.

Malformed classes/unescaped closing brackets, bare `!`, dangling escapes, NUL
and paths escaping the context after cleaning are errors. Cleaning that exposes
a previously nonleading `!` and control whitespace U+001C–U+001F are rejected;
write such a normalized rule explicitly. **Embedded/repeated double stars** such as
`foo**bar`, `**suffix`, `***`, nonliteral backslash escapes, and slash-containing
classes are explicitly rejected. Upstream accepts some of these; this release
does not claim full patternmatcher equivalence. Windows matching, remote Git,
tar contexts, named contexts, Dockerfile-from-stdin and Dockerfile syntax
evaluation are outside scope. Inputs are always data, never instructions.

## Accounting and report contract

`--json` emits schema version 1, an ordered `baseline.entries` inventory,
optional `candidate.entries`, summaries and changed decisions. Each entry has
`path`, `kind`, `logical_bytes`, `role`, `included_by_rules` and `last_rule`.
The rule includes source file, physical line, raw/cleaned pattern, negation and
the matched path/ancestor. A null rule means default inclusion. Largest files
sort by descending size, breaking ties by path; all entries sort by path.

The budget sums `st_size` for **included regular-file payload paths**. Sparse
files count their full logical size. Hardlinked names count separately. There
is no tar header, directory, symlink text, compression or filesystem allocation
estimate. Symlinks have a separate count and zero budget bytes; a passing budget
is specifically a regular-file payload pass, not a total context-size bound.

The selected Dockerfile and effective baseline ignore file, when inside the
context, have separate control roles/counts/on-disk bytes. Their ignore decisions
are still shown. They never enter the payload budget, even if rules include
them. Docker may send excluded controls to its builder while preventing COPY
access. This tool does not compute exact COPY visibility or builder transfers.
Other Dockerfiles/inactive ignore files are ordinary payload unless excluded.

A candidate replaces only the rule set for the same on-disk inventory and
**fixed baseline control partition**. It is not copied into the context and
does not change control-file size. A candidate stored inside the context is
ordinary payload unless it is already one of the selected controls. Prefer an
external candidate file for clear comparisons. Reports distinguish rule inputs
from the files that would actually be on disk.

## Relation to upstream and alternatives; limitations

[Docker documents the rules](https://docs.docker.com/build/concepts/context/);
[moby/patternmatcher](https://github.com/moby/patternmatcher) is the upstream
Apache-2.0 behavior reference. [BuildKit #1181](https://github.com/moby/buildkit/issues/1181)
motivates context listing; BuildKit itself reports transfer progress. `du` and
ordinary file listings report disk/filesystem sizes. This tool combines offline
per-rule provenance, candidate comparison and logical payload budgeting. It is
an independent tool, not a Docker/BuildKit fork, plugin or endorsed replacement.
Candidate ignore advice is generated only for exceeded thresholds and is never
applied automatically: exclusions may remove required sources. It does not scan
secrets, vulnerabilities or image layers.

The scan retains the inventory and both reports in memory and does not prune
ignored subtrees. Cost grows with entries × rules × path depth. It is intended
for stable developer trees, not hostile or concurrently modified filesystems.
Descriptor-relative `O_NOFOLLOW` traversal prevents following swapped-in
symlinks; several metadata changes are detected, but the scan is **not an atomic
snapshot** and does not hash payload contents. Deep trees exceeding Python's
recursion limit fail explicitly. It does not prove build correctness, count
extended attributes or model BuildKit's lazy transfer/COPY reachability.
Saved inventories retain these limits: they compare metadata from completed
scans of a stable tree and cannot detect same-size content or link-target edits.

## License

Original implementation, documentation and fixtures are MIT licensed; see
[LICENSE](../LICENSE). See [THIRD_PARTY.md](../THIRD_PARTY.md) for the behavioral
references (Docker documentation, moby/patternmatcher, BuildKit); no upstream source
or test tables are bundled.
