# stackwrench task-audit

`stackwrench task-audit` (formerly `task-fingerprint-audit`) is an offline command
for reviewing possible input gaps in a **documented subset of
Taskfile v3**. It finds literal command references, hashes declared source files,
and compares redacted snapshots with explanations of source, variable and task
definition changes. It never runs Task, shell commands, dynamic variables or
status scripts. Runtime behavior is offline and does not read the host environment
for input values. No Docker, server, credentials or paid API is needed.

Use it when a command consumes a version, build mode or environment input and you
want to review how changes to that input relate to the task's declared sources
and status strategy. A finding is a configuration-review prompt, not proof of a
Task bug. All reports say `native_execution_decision: unknown`.

## Install

Requires Python 3.12 or newer. The only runtime dependency is PyYAML, which is a
base dependency of stackwrench.

```sh
pipx install stackwrench            # or: pip install stackwrench
stackwrench task-audit --version    # prints the bare stackwrench version, e.g. 0.1.0

# Or run without installing:
uvx stackwrench task-audit --help
```

`python -m stackwrench task-audit` is equivalent to `stackwrench task-audit`. The
subcommands are `check`, `snapshot`, `suggest` and `compare`. The CLI never uses
the network.

## Complete examples

Run from the root of a stackwrench source checkout:

```sh
stackwrench task-audit check examples/task_audit/Taskfile.yml --root examples/task_audit --json
stackwrench task-audit snapshot examples/task_audit/Taskfile.yml --root examples/task_audit --output snapshot.json
```

`check` writes only to stdout. `snapshot` writes only to the explicitly requested
**new** output path, with mode 0600; it refuses to overwrite an existing file.
The synthetic commands in `examples/task_audit/Taskfile.yml` are read as text, never run.
The default example has an unresolved external `BUILD_MODE` reference on purpose;
non-strict mode returns a report with that task marked `partial`.

Compare explicit environment inputs without inheriting host values:

```sh
work_dir=$(mktemp -d)
stackwrench task-audit snapshot examples/task_audit/Taskfile.yml --root examples/task_audit --env-snapshot examples/task_audit/env.before.json --output "$work_dir/before.json"
stackwrench task-audit snapshot examples/task_audit/Taskfile.yml --root examples/task_audit --env-snapshot examples/task_audit/env.after.json --output "$work_dir/after.json"
stackwrench task-audit compare "$work_dir/before.json" "$work_dir/after.json" --json
```

Only task `external` reports `shell:BUILD_MODE` changed. Its source-only model
stays the same; its explicit-input advisory fingerprint changes. Task `build`
still raises a structural review finding about `template:VERSION`. No native
execution/skip decision is inferred.

Audit only named tasks with repeatable `--task`:

```sh
stackwrench task-audit check examples/task_audit/Taskfile.yml --root examples/task_audit --task build --task status-demo
```

A supplied env snapshot is a JSON object of identifier names to **strings**:
`{"BUILD_MODE":"synthetic-before"}`. Duplicate keys, non-string values and invalid
names are errors. An absent snapshot means unknown external values, not empty
strings. There is no host-environment import option.

Failure examples with asserted exit codes:

```sh
stackwrench task-audit check examples/task_audit/unsupported.yml --strict --json
test "$?" -eq 1
stackwrench task-audit check examples/task_audit/missing-source.yml --root examples/task_audit
test "$?" -eq 2
```

Run failure examples in a shell without `set -e`.

## Read-only declaration suggestions

`suggest` produces a separate review report with candidate `sources` / `env`
entries, their command locations, uncertainty reasons, false-positive boundaries
and a copyable YAML fragment. It never edits a Taskfile and has no `--output`
option. JSON uses `kind: task-fingerprint-audit.suggestions`, not the snapshot
format; existing snapshots and comparisons are unchanged.

```sh
stackwrench task-audit suggest examples/task_audit/suggestions.yml --task review --json
stackwrench task-audit suggest examples/task_audit/suggestions.yml --task review
```

The local synthetic `review` task reads `cat base.txt` and references `"$MODE"`
in its first command. Expected candidates are source `base.txt` and env `MODE`,
both with location `{"path":["tasks","review","cmds",0]}` (zero-based indices).
Its second command contains single-quoted `$LITERAL` and escaped `\$ESCAPED`;
neither becomes an env candidate. The suggested fragment is:

```yaml
tasks:
  review:
    env:
      MODE: REVIEW_REQUIRED
    sources:
      - base.txt
```

Review the env value and merge entries into existing `sources` lists / `env`
maps; do not replace existing declarations. `REVIEW_REQUIRED` is a literal
placeholder, never a captured value. Applying this fragment to the example
removes its `missing-source-declaration` and `missing-env-declaration` advisories
on the next `suggest` run. Task/global `env` declarations suppress env candidates;
`vars` entries do not export shell values. An `--env-snapshot` value also does
not declare a Taskfile env entry, and is never copied into the fragment. Existing
literal source globs suppress candidates they already cover. These declaration
advisories are distinct from `check`'s `potential-fingerprint-gap`: adding an env
entry does not prove native fingerprint coverage and does not remove that
structural review heuristic.

The scanner recognizes active `$NAME` / `${NAME}` shell references, single/double
quotes, escapes, comments and semicolon/newline-separated simple commands. File
candidates include literal `cat` / `wc` / `sort` / `head` / `tail` file operands,
`cmp` / `comm` paired file operands and `uniq` input operands without options (except `--`),
`<` input redirections and Python/Node.js script-file operands, including quoted filenames.
Arbitrary program arguments and output redirections are not treated as source reads. Candidate
files must already be regular files beneath `--root` without symlink components;
only their metadata is inspected. Declared sources are hashed by the existing
audit before suggestion generation. Missing, unsafe or unsupported candidate
paths become uncertainty records, without reading or displaying their contents.

For example, with an existing regular `scripts/build.py` beneath the root:

```yaml
version: '3'
tasks:
  build:
    cmds: ['python3 scripts/build.py "$MODE" config.json']
```

`suggest` proposes source `scripts/build.py` and env `MODE`, both at
`[tasks, build, cmds, 0]`, and emits a fragment containing those declarations.
It does not infer `config.json` as a source from an arbitrary script argument.
Literal executable basenames `python`, `python3` and `python3.<numeric version>`
(such as `/usr/bin/python3.12`) are recognized without locating or running them.
Only a script immediately after the executable, or after a single `--`, is
supported. Interpreter options (including `-c`, `-m`, `-I`, `-W`), stdin `-`,
missing scripts and dash-leading script names remain `interpreter-mode-not-modeled`;
dynamic executable/script operands also withhold actionable entries for the
entire command.

Node.js uses the same literal script-file boundary. With existing regular files
`scripts/build.js` and `config.json`, command
`node scripts/build.js "$MODE" config.json` suggests only source `scripts/build.js`
and env `MODE` at `[tasks, build, cmds, 0]`. Only the literal executable basename
`node` is recognized (including paths such as `/usr/bin/node`); no executable is
located or run. The script must immediately follow it or a single `--`; quoted
and escaped paths are supported, with no script filename-extension requirement.
All other pre-script options, including `-e`, `-p`, `--eval`, `--print`, `--require`,
`--import` and attached option values, remain `interpreter-mode-not-modeled`.
No script, stdin `-`, dynamic executable/script operands, shell wrappers/control
and unsupported Task contexts yield no actionable entries for that command.
Other interpreter families and wrappers remain outside this subset.

Python and Node.js script-file operands may have exactly one leading `./`:
`node ./scripts/build.js "$MODE"` suggests normalized source `scripts/build.js`
and env `MODE` at `[tasks, build, cmds, 0]`. This also applies after the optional
`--` and to quoted script paths. Normalized and unprefixed occurrences share one
candidate with distinct command locations; existing literal sources or globs
covering that normalized path suppress it. Only this leading prefix is removed:
repeated `./`, absolute paths, traversal, internal `.` or empty components,
symlinks and nonregular files remain uncertain. This does not change declared
source/output pattern validation. No script contents are read to produce the
candidate.

Literal `cat` operands and `<` input redirections accept the same single leading
`./`. With an existing regular `config file.txt`, command
`cat "./config file.txt"; cat < "./config file.txt"; echo "$MODE"` suggests one
normalized source `config file.txt` and env `MODE`, both at
`[tasks, build, cmds, 0]`. Repeated reads within that command share one location;
reads in other commands add their actual YAML locations. Existing normalized
literal source declarations or matching globs suppress the source candidate.
Quoted/escaped paths and `cat --` keep their existing behavior; other `cat`
options remain uncertain. Arbitrary arguments and `>` output targets do not
become sources. Only recognized literal input operands are normalized, never
declaration patterns or arbitrary path components. Repeated `./`, absolute or
traversal paths, internal dot/empty components, symlinks, FIFOs, directories and
missing files remain uncertain. Candidates use metadata only. Merge reviewed
fragment entries into existing declarations; the declaration advisories then
clear, while `dependency_graph_complete` remains false.

The literal command name `wc` supports file operands with the same path safety,
normalization and declaration coverage. With existing regular `config file.txt`
and `ignored.txt`, command
`wc "./config file.txt"; wc -- "config file.txt"; echo "$MODE"` suggests only
normalized source `config file.txt` and env `MODE` at `[tasks, build, cmds, 0]`.
Repeated reads share one location; `ignored.txt` is not inferred. Multiple file
operands are supported. Bare `-` is stdin and produces no source candidate;
`wc` with no operands also produces none. A single initial `--` ends options,
so dash-leading filenames after it are literal operands. A repeated or noninitial
`--` is outside this subset. All other options before that delimiter or without
it, including `-l`, `-c`, `--files0-from`, attached values and trailing flags,
yield `wc-options-not-modeled` and withhold **all** actionable entries for that
YAML command. No option values are inferred and no file lists are read. Only the
exact command word `wc` is recognized, without executable lookup; executable
paths such as `/usr/bin/wc` remain outside this contract. Dynamic operands or
executables, wrappers/control and unsupported task contexts retain the existing
uncertainty rules. Output targets and arbitrary other-program arguments remain
excluded. Candidate contents are never read and commands never execute. Reviewed
fragment merging clears declaration advisories while graph completeness stays false.

The exact literal command word `sort` supports the same bounded file-operand
contract as `wc`. With existing regular `config file.txt` and `ignored.txt`,
command `sort "./config file.txt"; sort -- "config file.txt"; echo "$MODE"`
suggests one normalized source `config file.txt` and env `MODE`, both at
`[tasks, build, cmds, 0]`. Repeated reads share one location; `ignored.txt` is
not inferred. Multiple literal operands, quoted/escaped paths, one initial `--`
and stdin `-` (ignored) are supported; no operands yields no source candidate.
After `--`, dash-leading filenames are literal operands. Repeated or noninitial
`--` is unsupported. All other options before that delimiter or without it,
including `-o` / `--output`, attached output values, key/field/separator options,
file-list options and trailing flags, yield `sort-options-not-modeled` and
withhold **all** source/env entries for the YAML command. Option values, output
targets and arbitrary other-program arguments are never inferred as inputs.
Executable paths such as `/usr/bin/sort` are outside this exact-word contract;
no executable is located or run. Dynamic operands/executables, wrappers/control
and unsupported Task contexts retain the existing uncertainty rules.

Sort operands reuse exactly-one-leading-`./` normalization, metadata-only safe
regular-file checks, normalized literal/glob declaration coverage, deduplication
and actual YAML locations. Repeated prefixes, traversal, internal dot/empty
components, absolute paths, symlinks, FIFOs, directories and missing files stay
uncertain; declaration patterns remain strict. Candidate contents are never read.
Merge reviewed fragment entries into existing declarations to clear corresponding
advisories; `dependency_graph_complete` stays false and native execution unknown.

The exact literal command word `uniq` accepts zero to two operands after an
optional single initial `--`: the first is input, the second is output. With
existing regular `config file.txt`, `output.txt` and `ignored.txt`, command
`uniq "./config file.txt" "output.txt"; uniq -- "config file.txt"; echo "$MODE"`
suggests one normalized source `config file.txt` and env `MODE`, both at
`[tasks, build, cmds, 0]`. Repeated reads share one location; neither `output.txt`
nor `ignored.txt` is inferred. No operands, or input `-`, means stdin and yields
no source candidate. After `--`, dash-leading input/output names are literals;
input `-` is still stdin. Executable paths such as `/usr/bin/uniq` are outside
this exact-word contract.

Other options (including `-c`, `-d`, `-f`, `-s`, `-w` and attached forms), repeated
or noninitial `--` yield `uniq-options-not-modeled`; more than two operands yield
`uniq-operand-count-not-modeled`. Dynamic input/output operands yield
`dynamic-source-operand` / `dynamic-output-operand`. These cases withhold **all**
actionable entries for the YAML command, including earlier simple commands.
Dynamic executables, wrappers/control and unsupported Task contexts retain
existing withholding. Option values and arbitrary arguments are never inferred.

Only the input gets one-leading-`./` normalization, safe regular-file metadata
checks and normalized declaration literal/glob coverage. The positional output
is never normalized, checked for existence, read or inferred as a source, even
when it exists; a missing, unsafe or nonregular literal output does not affect
input inference. Unsafe/repeated-dot/traversal/absolute input paths, internal
dot/empty components, symlinks, FIFOs, directories and missing inputs remain
uncertain. Declaration validation stays strict. Merge reviewed fragments into
existing declarations to clear corresponding advisories; graph completeness
stays false. Candidate contents are never read and commands never execute.

The exact literal command word `head` uses the same bounded file-operand
contract as `wc` and `sort`. With existing regular `config file.txt` and
`ignored.txt`, command
`head "./config file.txt"; head -- "config file.txt"; echo "$MODE"`
suggests one normalized source `config file.txt` and env `MODE`, both at
`[tasks, build, cmds, 0]`. Repeated reads share one location; `ignored.txt` is
not inferred. Zero or more literal operands, quoted/escaped paths and a single
initial `--` are supported; stdin `-` is ignored, including after `--`.
After `--`, dash-leading names are literal file operands. Repeated or noninitial
`--` is outside this subset. All other options, including `-n` / `--lines`,
`-c` / `--bytes`, attached values, obsolete numeric forms such as `-10` and
trailing flags, yield `head-options-not-modeled` and withhold **all** source/env
entries for the YAML command, including earlier simple commands. Option values
are never inferred. Executable paths such as `/usr/bin/head` remain outside the
exact-word contract; no executable is located or run.

Head reuses one-leading-`./` normalization, metadata-only safe regular-file
checks, normalized declaration literal/glob coverage, deduplication and actual
YAML locations. Unsafe/missing/repeated-dot/traversal/absolute paths, internal
dot/empty components, symlinks, FIFOs and directories remain uncertain.
Dynamic operands/executables, wrappers/control and unsupported Task contexts
retain their withholding rules. Declaration patterns remain strict; arbitrary
arguments and output targets are not inferred. Candidate contents are never
read and commands never execute. Merge reviewed fragment entries into existing
declarations to clear corresponding advisories; graph completeness stays false.

Every recognized script call retains `script-transitive-inputs-not-modeled`:
the script's imports (including Node.js `require()`), file reads and embedded
environment inputs are unknown. Later script arguments such as `config.json`
are not inferred file dependencies.
Its contents are not inspected unless it is already a declared source being
hashed by the audit. Merging the reviewed source/env fragment into this task
removes its declaration advisories, but the transitive warning remains and
`suggest --strict` still returns 1. Declared source globs also suppress covered
script candidates without removing that warning.

Dynamic file operands, parameter operators, indirect reads, substitutions,
heredocs, pipelines, shell control/scoping commands and embedded scripts are
uncertain; the entire affected command yields no actionable entries. Possible
shell-local names are also withheld conservatively across the task. Go templates
in commands and unsupported Task context (such as includes or `dir`) withhold
actionable entries. Unsupported declaration findings remain visible. Locations
refer to actual YAML paths, including command-string/list task shorthands;
command text and assignment values are omitted.

This is a deliberately incomplete heuristic: custom commands/functions, shell
scope, indirect inputs and embedded languages can cause false positives or missed
inputs. Every report says `dependency_graph_complete: false` and native execution
`unknown`. Status/dynamic commands never run. Names and paths are visible metadata;
env values, candidate contents and host env values are not output. Text escapes
terminal control characters while preserving valid YAML fragment syntax.

`suggest --strict` returns 1 for declaration advisories, uncertainty or unassessed
findings, while still printing the report. A clean review returns 0. Malformed
YAML/env JSON, invalid task selection and existing declared-source audit errors
(including missing files) return 2 without a report; parser errors omit excerpts.
For example, `suggest examples/task_audit/suggestions.yml --task dynamic --strict --json`
returns 1 with `dynamic-source-operand` and no fabricated `$FILE` source path.

The exact literal command word `tail` supports zero or more file operands after
an optional single initial `--`, ignoring stdin `-`. With existing regular
`config file.txt` and `ignored.txt`,
`tail "./config file.txt"; tail -- "config file.txt"; echo "$MODE"`
suggests one normalized source `config file.txt` and env `MODE`, both at
`[tasks, build, cmds, 0]`; repeated reads share that location. `ignored.txt` is
not inferred. Existing normalized literal/glob declarations suppress the source.

All other options, including `-n`/`-c`, `-f`/`-F`/`--follow`, attached values and
obsolete signed numeric forms, withhold **all** source/env entries for that YAML
command with `tail-options-not-modeled`. Any plus-leading operand before `--`
is also withheld conservatively to avoid obsolete count ambiguity, even if a
file with that name exists. After `--`, dash/plus-leading names are literal file
operands; repeated or noninitial `--` remains unsupported. A single `./` prefix
can explicitly spell a relative filename such as `./+10`. Option values and
follow-mode dependencies are never inferred. Executable paths such as
`/usr/bin/tail` and arbitrary arguments to other commands remain outside scope.

Shared one-leading-`./` normalization, safe regular-file metadata checks,
normalized declaration coverage, deduplication and actual YAML paths apply.
Dynamic operands/executables, wrappers, shell control and unsupported Task
contexts still withhold entries; unsafe/missing/nonregular paths stay uncertain.
Candidate contents are never read and commands never execute. Declarations keep
strict path rules. Reviewed fragment merges preserve existing declarations and
clear corresponding advisories; `dependency_graph_complete` stays false.

The exact literal command word `cmp` accepts **exactly two** literal operands
after an optional single initial `--`. With existing regular `left file.txt`,
`right.txt` and `ignored.txt`, command
`cmp "./left file.txt" right.txt; cmp -- "left file.txt" ./right.txt; echo "$MODE"`
suggests normalized sources `left file.txt` and `right.txt` plus env `MODE`, all
at `[tasks, build, cmds, 0]`. Repeated reads share that location; `ignored.txt`
is not inferred. Stdin `-` is ignored in either position, including after `--`.
Dash-leading names after `--` are literal file operands.

Other arities yield `cmp-operand-count-not-modeled`; optional positional skip
counts are outside this subset. Other options, including `-n`/`--bytes`,
`-i`/`--ignore-initial`, `-s`/`--silent`, `-l`/`--verbose`, attached values and
repeated/noninitial `--`, yield `cmp-options-not-modeled`. These cases and dynamic
operands withhold **all** source/env entries for the YAML command. Option values,
arbitrary arguments and output targets are never inferred. Executable paths such
as `/usr/bin/cmp`, wrappers/control and unsupported Task contexts retain the
existing boundaries; no executable is located or run.

Both operands reuse one-leading-`./` normalization, safe regular-file metadata
checks, normalized declaration literal/glob coverage, deduplication and actual
YAML paths. Unsafe/missing/repeated-dot/traversal/absolute paths, internal dot/empty
components, symlinks, FIFOs and directories remain uncertain; a separate safe
literal partner may still be suggested. Declarations remain strict. Candidate
contents are never read; this does not compare bytes or predict command success.
Merge reviewed fragments into existing declarations to clear corresponding
advisories; `dependency_graph_complete` remains false and native execution unknown.

The exact literal command word `comm` accepts **exactly two** literal operands
after an optional single initial `--`. With existing regular `left file.txt`,
`right.txt` and `ignored.txt`, command
`comm "./left file.txt" right.txt; comm -- "left file.txt" ./right.txt; echo "$MODE"`
suggests normalized sources `left file.txt` and `right.txt` plus env `MODE`, all
at `[tasks, build, cmds, 0]`. Repeated reads share one location; `ignored.txt`
is not inferred. Stdin `-` is ignored in either position, including after `--`;
dash-leading names after `--` are literal file operands.

Other arities yield `comm-operand-count-not-modeled`. All other options, including
`-1`/`-2`/`-3`, `--check-order`/`--nocheck-order`/`--output-delimiter`, attached
values and repeated/noninitial `--`, yield `comm-options-not-modeled`. These
cases and dynamic operands withhold **all** source/env entries for that YAML
command. Dynamic words such as `"${FILE}-"` and `"${FILE}--"` stay unsupported;
no expansion is evaluated. Option values,
arbitrary arguments and output targets are never inferred. Executable paths such
as `/usr/bin/comm`, wrappers/control and unsupported Task contexts keep the
existing boundaries.

Both operands reuse one-leading-`./` normalization, safe regular-file metadata
checks, normalized declaration literal/glob coverage, deduplication and actual
YAML paths. Unsafe/missing/nonregular paths stay uncertain; a separate safe
literal partner may still be suggested. Declarations remain strict. No candidate
content reads, execution, sortedness, locale, content/order validation or command
success model is attempted. Merge reviewed fragments into existing declarations
to clear corresponding advisories; `dependency_graph_complete` remains false
and native execution unknown.

## Supported input and interpretation

- One UTF-8 YAML document with `version: '3'` (also numeric 3 or a 3.x / 3.x.y
  version) and a nonempty `tasks` map. Task values may be a mapping, a command
  string, or a list of commands. Mapping keys must be strings. YAML block, folded
  and flow syntax work. Scalar coercion is limited to `true`/`false`, `null`,
  decimal integers and finite decimal/exponent floats; dates, `yes`/`no`, hex and
  octal-looking values remain strings. Quote literals for predictable types.
- Root fields: `version`, `tasks`, `vars`, `env`. Task fields: `cmds`, `vars`,
  `env`, `sources`, `generates`, `status`, `desc`, `summary`. Metadata
  `desc`/`summary` is ignored for fingerprints. `cmds`, `sources`, `generates`,
  `status` must be string lists in the supported mapping form.
- `vars`/`env` support literal strings, integers, booleans and finite floats.
  Values containing `{{` / `}}`, maps (`sh`, `ref`, etc.), arrays and null are
  unassessed. Dynamic values are not run. Task vars shadow global vars;
  templates can also read root `env` and finally the explicit env snapshot.
  Task-level `env` is for shell references, not template resolution. This is a
  closed-world review model without Task command-line variables or includes.
- Template references in `cmds`: `{{.NAME}}`, allowing whitespace inside braces.
  Identifiers match `[A-Za-z_][A-Za-z0-9_]*`. Other Go-template expressions are
  unassessed. No expression interpreter or template expansion is implemented.
- Shell references in `cmds`: lexical `$NAME` and `${NAME}` candidates, excluding
  an immediately backslash-escaped dollar. The scan does not parse quoting,
  comments, heredocs, shell scopes, or embedded scripting languages: it may flag
  text in single quotes or comments. Command-local `NAME=...` assignments create
  a `possible-shell-local` hint and leave that name unresolved, conservatively
  across the task. `read`/`for` locals and indirect reads are not inferred.
  Parameter operators, command substitutions, backticks and special parameters
  are unassessed. Literal command text and assignment values are not reported.
- `$NAME` resolves only from task/global `env` or the explicit env snapshot.
  **`vars` is not exported to `cmds`.** A snapshot/declaration env collision is
  unassessed, since Task's env precedence experiment is not modeled. A snapshot
  describes supplied explicit values, not a captured runtime environment.
- Template names `CHECKSUM`, `TIMESTAMP`, `SOURCES`, `GENERATES`, `TASK`,
  `ROOT_DIR`, `TASKFILE_DIR`, `TASK_VERSION`, `USER_WORKING_DIR` are recognized as
  Task runtime/derived variables unless explicitly declared. They are not
  mislabeled external inputs, but remain unresolved/unassessed. `CLI_ARGS` and
  other unsupplied runtime inputs are unknown. A shell `$CHECKSUM` is still an
  external candidate: Task vars are not automatically shell env.
- Source patterns use project-root-relative POSIX paths: literal file paths,
  `*` and `?` within one component, and `**` as a complete nonfinal component
  (`src/**/*.txt`). Matching is case-sensitive, includes dotfiles, hashes regular
  files only, and deduplicates overlaps. Each pattern must match at least one
  file. Symlinks encountered by a matching expansion are rejected, even internal
  ones; `**` conservatively rejects encountered symlink entries. Symlinks are
  never followed. Absolute paths, `.`/`..`/empty components, backslashes, bracket
  classes, braces, negation, tilde, colon and terminal `**` are rejected. A
  literal directory is an error. This is not Task's complete glob dialect.
- `generates` uses the same path-pattern syntax for declaration review; output
  files are never opened or checked for existence. With no sources/status it
  warns about missing reproducibility declarations and suggests reviewing native
  input/status configuration; it does not produce arbitrary shell fixes.
- `status` strings are counted and included in the definition digest, but are
  **not parsed for input references or executed**. Templates beyond literal
  `{{.VAR}}` still produce an unassessed syntax finding. `native_status` always stays
  unknown. Sources present, outputs present, stable fingerprints and even a
  `modeled-subset` assessment do not imply Task will skip a task.

All other fields (including `includes`, `dotenv`, `deps`, `dir`, `method`,
`preconditions`, task calls and command objects) receive explicit unassessed
findings, not silently accepted semantics. A literal `cmd` inside an unsupported
command object is scanned for candidates, but the object remains unassessed.
All root-level unassessed findings make selected tasks partial. Task selection
limits per-task assessment to the requested task names. Unknown includes are
not loaded; other files are never read on their behalf.

Malformed structures, missing files, unsafe/unsupported path syntax, duplicate
YAML/JSON keys, YAML aliases/anchors/merge keys/explicit tags, incompatible or
malformed snapshots, non-regular document inputs (such as named pipes), and I/O failures are errors. Parser errors omit input
excerpts to avoid leaking values.

## Fingerprints, comparisons and redaction

Each task snapshot includes:

1. `source_hashes`: sorted relative paths mapped to SHA-256 of file **contents**.
   File mtimes/modes are not inputs. Empty-source tasks hash an empty source map.
2. `inputs`: unique `template:NAME` / `shell:NAME` entries with classification,
   resolution state, origin, `<redacted>` and a digest of the typed literal value.
   Unknown values are null, not fabricated. Unreferenced literal declarations
   do not affect these input digests.
3. `definition_digest`: SHA-256 of task fields other than `vars`, `env`,
   `desc`, `summary`. Thus command/source-pattern/status-definition changes have
   a separate explanation from variable-value changes. Unknown task fields can
   affect this structural digest without being evaluated. New snapshots also
   record `definition_components`, mapping each included field name to SHA-256
   of its canonical JSON value. Task string/list shorthands normalize to `cmds`.
   An absent field has no entry; a present empty list hashes as `[]`. List order
   matters; mapping key order does not. Unsupported task fields are hashed as
   opaque values and remain unassessed. No field values or command/status text
   are copied into this map.
4. `source_only_fingerprint`: SHA-256 of a model-tagged source map.
   `advisory_fingerprint`: SHA-256 of a model-tagged source map, definition digest
   and each reference's origin, resolution, classification and value digest.
   JSON canonicalization sorts keys, uses ASCII escapes and compact separators;
   the model tag is `task-fingerprint-audit/explicit-inputs-v1`.

The component map is supplemental explanation metadata; both existing v1
fingerprint formulas and the aggregate `definition_digest` stay unchanged.
`definition_components_digest` is SHA-256 of canonical JSON
`{"definition_digest": <aggregate digest>, "components": <component map>}`.
The map/checksum pair is validated together. Incomplete pairs, invalid digests,
excluded field names and checksum mismatches are errors. This checks recorded
metadata consistency; without the raw definition, validation cannot prove that
component hashes came from that definition. It is not an authenticity check.

New snapshots also include `definition_entries`, mapping each present `cmds` /
`status` field to an ordered array of SHA-256 digests of its canonical JSON
entries. Empty lists are `[]`; absent fields are omitted. Structured commands
are hashed as opaque entries and retain their unassessed findings. String/list
task shorthands normalize to `cmds`. Other fields have no entry metadata.
`definition_entries_digest` hashes canonical JSON
`{"definition_components_digest": <component checksum>, "entries": <entry map>}`.
The pair requires valid component metadata and exact `cmds`/`status` field
coverage; invalid hashes/types, incomplete pairs, inconsistent empty lists or
status counts, and stale checksums are errors. This remains a consistency check,
not proof of authenticity or derivation from the unavailable raw definition.
The aggregate, component and both v1 fingerprint formulas are unchanged.

`definition_subfields` adds a bounded projection of immediate keys inside
structured `cmds` entries. A supported mapping has a required string `cmd`,
optional strictly boolean `silent`, `ignore_error`, `defer`, and **no other
keys**. This is an attribution boundary, not support for native execution of
those options: every structured command still has `unsupported-command` and
remains unassessed. Strings, task calls, mappings with unknown keys, nested
values or invalid option types are opaque; no arbitrary child traversal occurs.
`status` has no child projection. Existing field and whole-entry hashes remain.

The map contains only present `cmds`, with one array slot per command: a sorted
map of allowed child names to SHA-256(canonical JSON of each value), or null for
an opaque entry. Absent `cmds` is omitted; empty `cmds` is `[]`. No command,
status or option values are emitted. `definition_subfields_digest` hashes
canonical JSON `{"definition_entries_digest": <entry checksum>, "subfields":
<subfield map>}`. The exact pair requires entry and component metadata. Validation
rejects unknown fields/child names, wrong types, missing `cmd`, non-SHA-256
digests, flags whose digests are not those of JSON true/false, positional length
mismatches and stale checksums/bindings. Recomputing a checksum cannot repair an
invalid shape. Checksums establish recorded consistency, not authenticity or
derivation of child/entry hashes from unavailable raw definitions; valid-looking
forged metadata is not authenticated. All v1 formulas stay unchanged. Allowed
key names and unsalted digests remain sensitive metadata (booleans in particular
are guessable); digest-only does not mean secret against guessing.

`compare` validates snapshot format and recomputes both fingerprints before
attributing added/removed/changed source paths, input references, definitions,
and assessment/findings changes. It can run without the original project tree.
The checksum checks consistency, not authenticity; snapshots are not signed.
`unchanged` means only these models and recorded assessments did not change.
Unknown external values can change undetected. Native Task execution is always
unknown, and partial fingerprints must never be used as a cache authorization.

Comparisons now carry their own redacted explanation, without requiring readers
to reopen the snapshots:

- `variable_changes` retains the compact name/change list. `input_changes` adds
  redacted `before`/`after` input records and sorted `changed_fields` drawn from
  `classification`, `origin`, `resolved`, `value_digest`. Added/removed references
  use `["presence"]` and null for the absent side. A digest change means a
  **typed explicit value** changed; an origin-only change means provenance
  changed even when that value stayed equal.
- `context_before` / `context_after` contain `assessment`, `unresolved_inputs`
  (reference keys mapped to classification and origin), and `unassessed_findings`
  with `root` / `task` scope. Context remains visible for **unchanged** tasks.
  An absent task has null context and assessment. Added/removed tasks also carry
  source/input membership explanations and `task-membership` in `reasons`;
  fingerprint-change flags compare presence as well as digest equality.
- `cache_reuse_authorized` is always **false**, including for unchanged tasks
  with `modeled-subset` assessments. This static comparison cannot authorize
  native cache reuse. Text output shows the same assessment transitions,
  unresolved references, unassessed findings and input-change fields. JSON task
  names, reference names and source paths are sorted; no timestamps or host
  environment values are added. Existing v1 snapshots remain compatible.
- `definition_changes` lists sorted field-level `added`, `removed` or `changed`
  records with `field`, `before_digest` and `after_digest` (null on an absent
  side). `definition_changes_complete: true` means every present task on both
  sides carries components; an absent task contributes an empty map. An empty
  list means no field changes. If either present task is a legacy snapshot
  without components, the list is **null** and completeness is **false**; text
  says `definition fields: unavailable (legacy snapshot)`. The existing
  `definition_changed` and `task-definition` reason remain valid. Adding
  component metadata alone does not change either fingerprint or make an
  otherwise identical legacy/new pair report changed. Older CLI releases do
  not accept the new fields; use this release to compare new snapshots.

For example, changing only `sources: [base.txt]` to `sources: ['*.txt']` when
`base.txt` is the only matching file produces one `definition_changes` record
with `field: sources` and `change: changed`, while `source_changes` stays empty
and `source_only_changed` is false. Changing only `cmds`, `generates` or `status`
similarly names that field in JSON and text (`definition: status (changed)`).
Status definitions are compared as opaque declarations, never executed or
scanned for input values. A field digest change describes syntax/data changes,
not a native execution decision or a guarantee of changed behavior.

`definition_entry_changes` supplements that field summary with digest-only
records: `location: ["cmds", 1]`, `change: changed`, `before_digest` and
`after_digest`. For example, editing only command entry 1 in a two-command list
reports only `[cmds, 1]`, while `definition_changes` still names `cmds`.
Status-only edits similarly identify `[status, index]`. Indices are zero-based;
results sort by field then **numeric** index. Append adds tail indices; removing
an entry removes tail indices and changes shifted positions. Reorders change
positions whose digests differ; equal duplicate entries stay unchanged. No
entry identity, move or behavioral equivalence is inferred. Text displays the
same positions, change types and before/after digests.

`definition_entry_changes_complete` is true when each present task has entry
metadata; an unchanged list yields `[]`. `definition_entry_context_before` /
`definition_entry_context_after` use `state: available` and sorted `fields`
to distinguish absent fields from present empty lists. An absent entry has a
null digest on that side; an absent task has `state: task-absent, fields: null`.
Old snapshots, including those with only field components, remain valid. Any
present task without entry metadata has `state: metadata-unavailable, fields:
null`; entry changes are then null, completeness false, and text explicitly
says `definition entries: unavailable (legacy snapshot)`. No entry details are
inferred from aggregate/field hashes. Field summaries still work where field
metadata exists. Metadata availability alone never changes the fingerprints or
task change status. Older CLI releases reject the new snapshot fields.

`definition_subfield_changes` supplements both summaries with digest-only
`location: ["cmds", index, key]`, `change: added|removed|changed`, and
`before_digest` / `after_digest`. Changing only `silent: false` to `silent: true`
in `cmds: [{cmd: "echo private", silent: false}]` identifies only
`[cmds, 0, silent]`, preserving `[cmds, 0]` and `cmds` summaries. Key addition or
removal within a supported mapping has a null digest on the absent side. Entry,
field or task addition/removal adds/removes its supported children. String to
supported mapping, or any transition into/out of the supported boundary,
adds/removes the supported projection, **not a claim that an opaque entry lacks
those raw keys**. Reorders and shifts compare the same numeric indices only,
with no identity/move inference. Changes sort by numeric index then child name.

`definition_subfield_changes_complete: true` means metadata covers this documented
projection, not all raw children or command behavior. Opaque-only or unchanged
projections yield `[]` even if an opaque entry's whole digest changes. Per-side
`definition_subfield_context_before` / `definition_subfield_context_after` have
`state: available` and `fields`, mapping present `cmds` to a parallel array of
sorted child-name lists or null opaque entries. `{}`, `{"cmds": []}`, a missing
array index, a missing child name, and a null slot distinguish field, entry,
child and opaque states. Task absence is `state: task-absent, fields: null`.
Original v1, component-only and entry-only snapshots remain valid. Any present
task without child metadata makes child changes null, completeness false, its
context `state: metadata-unavailable, fields: null`, and text says
`definition subfields: unavailable (legacy snapshot)`. No child names are inferred
from existing digests, including equal digests. Metadata availability alone never
changes task status or fingerprints. JSON/text are deterministic and retain
unassessed context, unknown native execution and false cache authorization;
compare needs no original Taskfile. Only explicitly requested snapshot outputs
are written; nothing is evaluated or executed.

For example, this local synthetic input has a known variable and unknown shell
input (the command is only read):

```yaml
version: '3'
vars: {VERSION: one}
tasks:
  build:
    cmds: ['echo {{.VERSION}} $MODE']
```

Changing only `VERSION` to `two` produces an `input_changes` entry named
`template:VERSION` with `changed_fields: ["value_digest"]`; both values remain
`<redacted>`. Moving the same value from root `vars` to build's `vars` produces
`["origin"]`, with `global.vars.VERSION` and `task.vars.VERSION` as the origins.
Providing `{"MODE":"synthetic"}` via `--env-snapshot` changes classification,
origin, resolution and value digest for `shell:MODE`. Without that explicit
snapshot, comparing the same snapshot to itself still says `unchanged`,
`assessment: partial -> partial`, and lists `shell:MODE` as unresolved on both
sides. Unsupported syntax without a named reference is retained in scoped
unassessed findings. Neither a missing value nor an unsupported expression is
evaluated to explain it. Malformed/incompatible snapshots still exit 2 without
producing a comparison.

Values are always redacted (there is no reveal switch); commands, status text,
source contents and environment values are not emitted. Names, paths and
unsalted digests are visible. Digests can reveal equality and permit guessing
low-entropy secrets. Treat snapshots as sensitive metadata. Redaction is not
cryptographic secrecy. Text output escapes terminal control characters.

## Exit codes and limits

- `0`: report/snapshot/comparison produced, possibly with warnings or unassessed
  syntax in non-strict mode. Differences in `compare` also return 0.
- `1`: `check --strict` or `snapshot --strict` found a warning, unresolved input,
  or unassessed syntax. The report/snapshot is still produced. Structural input
  review warnings count, even when all referenced values resolve. A status-only
  informational finding alone does not trigger strict failure.
- `2`: input, schema, path, snapshot, output or argument error. No successful
  audit is claimed.

Taskfiles/env JSON are limited to 2 MiB; snapshots to 16 MiB; YAML nesting to 64
levels. Per task: at most 10,000 distinct source files, 100,000 glob traversal
steps per pattern, and 256 MiB total source content. Source stat checks detect
ordinary concurrent writes, but the tool is not a filesystem sandbox against
hostile concurrent path replacement. Audit a stable, trusted local tree.
No size/performance improvement, timing estimate, exact native Task checksum
parity, or complete shell/Go-template compatibility is claimed.

## Upstream and alternatives

[go-task/task](https://github.com/go-task/task) is the MIT-licensed upstream
format provider and the actual task runner. Its `task --status` evaluates native
status/fingerprinting; this CLI is complementary static input review and snapshot
comparison, not a replacement or native checksum reimplementation. Prefer Task
for authoritative runtime decisions.

The historical discussion [#548](https://github.com/go-task/task/issues/548)
is closed as **not planned**; related
[#411](https://github.com/go-task/task/issues/411) and
[#417](https://github.com/go-task/task/issues/417) were also reviewed.
Stale-output risk depends on configuration; these issues do not prove
a current Task defect. Format/scope references are the
[Task schema](https://taskfile.dev/docs/reference/schema),
[variable guide](https://taskfile.dev/docs/guide/variables) and
[environment guide](https://taskfile.dev/docs/guide/environment).
This tool's narrower difference is an offline explanation of possible undeclared
inputs and two advisory models across snapshots, with native status unknown.

## License

Original code and synthetic examples are MIT licensed; see [LICENSE](../LICENSE).
See [THIRD_PARTY.md](../THIRD_PARTY.md) for the PyYAML notice and the Task format
references. No Task implementation code is copied.
