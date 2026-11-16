# stackwrench compose-env

`stackwrench compose-env` (formerly `compose-env-trace`) is an offline CLI and Python
library explaining **two separate phases** of Compose environment handling: where
interpolation inputs came from, and which declaration wins in each service's container
environment. Values are hidden by default.

Use it when `.env`, shell variables, service `env_file`, explicit empty values,
and inherited nulls are being confused. It is a static explanation aid; it never
launches Compose, executes commands, contacts a daemon, or discovers image ENV.

## Install

Requires Python 3.12 or newer. The only runtime dependency is PyYAML.

```sh
pipx install stackwrench            # or: pip install stackwrench
stackwrench compose-env --version   # prints the bare stackwrench version

# Or run without installing:
uvx stackwrench compose-env --help
```

`python -m stackwrench compose-env` is equivalent to `stackwrench compose-env`. The
command has no subcommands; modes are selected with options. The examples below use
files from a stackwrench source checkout; run them from its root:

```sh
stackwrench compose-env --compose examples/compose_env/compose.yaml --env-file examples/compose_env/.env --json
stackwrench compose-env --compose examples/compose_env/compose.yaml --env-file examples/compose_env/.env
```

All CLI and library behaviour is offline.

All supplied examples are synthetic. Explicitly reveal their values and add an
explicit shell snapshot (the process's actual shell environment is never used):

```sh
stackwrench compose-env --compose examples/compose_env/compose.yaml --env-file examples/compose_env/.env --shell-env examples/compose_env/shell.json --json --show-values
```

This shows `PORT` interpolated from the snapshot as `8181`, then supplied by service
`environment`; `APP_MODE` is explicitly empty from `environment`, shadowing `dev`
from `base.env`. `ORDER` comes from the second service env_file. `INHERIT` is a null
or key-only declaration resolved from the explicit interpolation inputs.
`UNRESOLVED` is unset/removed, while `EXPLICIT_EMPTY` remains an empty string.
`ESCAPED` is the literal `$PORT`, without another expansion pass.

Expected failures (exit **2**, errors on stderr, no partial success output):

```sh
stackwrench compose-env --compose examples/compose_env/required-error.yaml --json
stackwrench compose-env --compose examples/compose_env/compose.yaml --env-file examples/compose_env/invalid.env --json
```

Both nonzero results are asserted in the test suite. Required-variable errors
include the variable, operator, source line/column and decoded scalar offset;
the caller's custom error message remains redacted even with `--show-values`.
Exit 0 means the supported static analysis completed, including any warnings.

Library API:

```sh
python - <<'PY'
from stackwrench.compose_env import trace
report = trace("examples/compose_env/compose.yaml", ["examples/compose_env/.env"])
mode = report["services"]["web"]["variables"]["APP_MODE"]
assert mode["winner"]["status"] == "empty"
assert mode["shadowed"][0]["source"] == "service-env-file"
print(mode)
PY
```

`trace(..., shell_env="snapshot.json", show_values=True)` opts into values.
Invalid or unsupported inputs raise `TraceError` with a value-redacted diagnostic.
The API and CLI return the same schema; no files are written by analysis.

## Optional and raw service env_files

Service `env_file` accepts a path string or an ordered list mixing strings and
`{path, required, format}` entries. `path` must be a string. `required` defaults to
`true` and accepts a YAML `true`/`false` boolean. Omit `format` for the normal dotenv
parser, or use the literal string `raw`; other formats, types and options fail.
Mappings are supported as **list entries**, matching the
[Docker service env_file reference](https://docs.docker.com/reference/compose-file/services/#env_file).
This does not change interpolation `--env-file` inputs or add implicit files.

```yaml
services:
  app:
    env_file:
      - base.env
      - path: optional.env
        required: false
      - path: raw.env
        format: raw
    environment:
      OVERRIDE: ''
```

Run the checked-in synthetic example:

```sh
stackwrench compose-env --compose examples/compose_env/envfile-options/compose.yaml --json --show-values
stackwrench compose-env --compose examples/compose_env/envfile-options/compose.yaml
```

`BASE` is `base`, `TOKEN` remains `${NOT_FROM_SHELL}`, `QUOTED` remains
`"literal $HOME"` including quotes, and `OVERRIDE` is the explicit empty string
from `environment`, with both file declarations in its shadowed history.
`optional.env` is absent: the second file declaration reports
`skipped-optional-missing` with `reason: "missing"`. Missing required files fail.
Optional files that exist must still be regular, readable UTF-8 inputs within the
2 MiB limit; invalid content, directories and access errors fail. A dangling
symlink follows missing-target semantics. Errors produce no partial success or
exported template. Relative paths, absolute-path warnings and empty/NUL/URL path
rejections retain their existing behavior.

The bounded raw parser accepts one-line ASCII `KEY=VALUE`, blank lines and
full-line comments (leading whitespace allowed). Key whitespace is trimmed;
**everything after the first `=` is literal**, including surrounding spaces,
quotes, backslashes, inline `#`, extra `=`, and malformed dollar expressions.
CRLF is a line ending. Raw values are never interpolated, unquoted, unescaped or
validated as expression syntax. Their declarations use `syntax: "raw-literal"`
and `interpolation_uses: []`. Key-only, `export`, colon and multiline declarations
remain unsupported. Later normal files may reference raw values without another
expansion pass; explicit interpolation inputs retain their existing lookup priority.

Trace and discovery JSON both expose ordered `service_env_files` metadata:
`service`, `location`, literal `field_path`, `required`, `format` (`compose` or
`raw`), and `status`. For mappings the field/location points to the `path` scalar;
strings retain their existing field/location. No selected filename is added.
Tracing statuses are `scanned` and `skipped-optional-missing`; discovery also uses
`unresolved-dynamic`. Optional missing literal files count as covered; every
variable-dependent path stays unopened and makes discovery incomplete, even with
`required: false`. Raw value contents contribute no syntax references or template
assignments. Default JSON/text and templates keep values hidden; text reports
file status/format and winner syntax. `--show-values` explicitly reveals values.

## Export a minimal referenced-variable template

Explain the synthetic fixture first, then explicitly export a new dotenv checklist:

```sh
stackwrench compose-env --compose examples/compose_env/template/compose.yaml --env-file examples/compose_env/template/inputs.env --json
stackwrench compose-env --compose examples/compose_env/template/compose.yaml --env-file examples/compose_env/template/inputs.env --env-template ./compose.env.template --json
```

The regular explanation still goes to stdout. `--env-template` creates the named
file with mode `0600` (subject to a more restrictive umask); existing files,
directories, and symlinks are refused, including dangling symlinks. It never
overwrites an input. The command fails with exit **2**, redacted stderr and empty
stdout if analysis or export fails. Missing required inputs and unsupported syntax
still fail; no template is written when analysis fails. By default export requires
a successful single trace, so supply required values explicitly to explain them
first. For missing required inputs, use the separate syntax-only mode below.
Scenario export requires `--discover-template`; ordinary comparisons cannot export a template.

The example exports exactly `EMPTY`, `MODE`, `PASS`, `TAG`, and `TOKEN`, sorted and
deduplicated. It excludes the unused supplied `UNUSED`, output names such as
`TOKEN_VALUE`, and escaped `$$IGNORED`. Each name has comments identifying every
distinct source field/operator, phase, and location. Field paths are JSON arrays
of literal mapping keys and list indexes; dotenv paths contain the assignment key.
For example, the TOKEN source field is
`["services", "demo", "environment", "TOKEN_VALUE"]`. Metadata is JSON quoted so
newlines cannot create active assignments. Values, fallback/alternative text, and
custom required messages are omitted even when `--show-values` reveals the report.

Selected lines from the generated checklist (source comments omitted here):

```dotenv
# variable: EMPTY; required: set (empty allowed)
# EMPTY=
# variable: MODE; required: no
# MODE=
# variable: PASS; required: no
# PASS=
# variable: TAG; required: no
# TAG=
# variable: TOKEN; required: nonempty
# TOKEN=
```

**Every assignment remains commented out**, leaving all names unset when the
untouched file is read. Uncomment `NAME=` only to deliberately supply empty, or
fill in a nonempty value. This preserves `${V-word}` versus `${V:-word}` and
`${V?message}` versus `${V:?message}` semantics; the source comments spell out each
condition. A name used by both required operators is marked nonempty. Direct
references and inheritance are optional in this model, with their warning/removal
behavior explained in comments. Required labels describe expression constraints,
not whether the current input already satisfies them.

Scope is all **evaluated references** in the successful explanation: Compose string
values, explicit interpolation dotenv dependencies, service dotenv dependencies,
and null/key-only environment inheritance, including shadowed declarations.
Single-quoted dotenv literals do not count. Supplied assignment names alone do
not count. Review scopes before editing: a global interpolation input can override
a service-local lookup. This is a referenced-name checklist, not a set of unresolved
external dependencies or a copy of the final container environment. The file is
never sourced, automatically loaded, or inserted into the process environment.
Use it only through an explicit `--env-file` after reviewing and filling it in.

Library rendering is pure and accepts the current `trace()` report, with values
hidden or shown; it does not write a file:

```python
from stackwrench.compose_env import env_template, trace

report = trace("examples/compose_env/template/compose.yaml", ["examples/compose_env/template/inputs.env"])
text = env_template(report)
assert "# TOKEN=" in text and "synthetic-token" not in text
```

## Discover a template before required inputs exist

Use `--discover-template` to collect references without evaluating any operators,
including required checks. An optional `--env-template` exports the checklist using
the same exclusive, private file creation; omit it to inspect the discovery only:

```sh
stackwrench compose-env --compose examples/compose_env/discovery/compose.yaml --discover-template --json
stackwrench compose-env --compose examples/compose_env/discovery/compose.yaml --discover-template --env-template ./discovered.env.template --json
```

The example succeeds with no supplied values and exports exactly `EMPTY`, `FILE`,
`LOCAL`, `PASS`, and `TOKEN`. `TOKEN` and `LOCAL` require nonempty, while `EMPTY`
requires set (empty allowed). It scans literal `static.env`, including its unsatisfied
required expression. It **does not open** the variable-dependent second env_file.
Every path with a variable reference is `unresolved-dynamic`, even if a fallback or
an explicit input value could suggest a filename. No candidate is guessed, resolved,
or read. Literal `$$` in a path decodes to `$` without making the path dynamic.
The template itself also comments on each unresolved path, using field/location
metadata only. References inside skipped files cannot appear in the checklist.

Discovery JSON has `report_type: "env-template-discovery"`, `schema_version: 1`,
`evaluation: "syntax-only"`, and `values_shown: false`. `references` contains
variable/operator/phase/location/field_path metadata (and scalar offsets for expression
uses). `service_env_files` lists each declaration with its service, location,
field_path and `status` (`scanned`, `skipped-optional-missing`, or `unresolved-dynamic`).
`unresolved_environment` is an ordered array (empty when none) for variable-dependent
environment list declarations. Each row has exactly `service`, `field_path`,
`location` and `status: "unresolved-dynamic"`. Paths and locations identify the original
list scalar, for example `["services", "demo", "environment", 0]`; no expression,
assignment name, inheritance name, value or guessed declaration kind is included.
The array follows service/declaration order. This discovery-only addition retains
schema version 1; ordinary trace reports are unchanged.

`complete` is false if any dynamic file was skipped **or** any environment declaration
name remains unresolved. True means coverage of explicit inputs, literal service files
and statically named inheritance, **not** successful Compose evaluation or a runnable project.
Missing optional literal files count as covered. Supplied inputs never resolve these
dynamic scopes in discovery.
There are no evaluated winners, states, branches, values or expansion results.

Optional repeated `--env-file` inputs are parsed for references without evaluation;
assignment names alone do not count. Compose string values, dotenv dependencies,
and static null/key-only inheritance count, including shadowed declarations.
Single-quoted dotenv literals and escaped dollars do not count. The discovery report
and template omit values, default/alternative operands and required messages. Names,
literal mapping keys and caller-supplied input paths remain visible metadata, with
JSON escaping in comments and text output.

The restricted expression, YAML, dotenv and resource-limit rules still apply.
Malformed/nested/unsupported syntax fails, even after an unsatisfied required
expression. Missing required or malformed **literal** files fail; missing optional literal files are reported as skipped. Failures return exit **2**,
redacted stderr and empty stdout, and create no template. Variable-dependent
**environment list names** such as `${KEY:?message}=value` and whole declarations
such as `${WHOLE}` retain their syntax references and become unresolved scopes.
No required check is evaluated, and no assignment/inherited name is synthesized.
An equals sign separates a name from its value only outside lexer token spans:
`${WHOLE:-name=value}` remains unresolved, while `VALUE=${TOKEN:?message=detail}`
has a static name. Escaped dollars do not count as references; invalid static names
still fail ASCII identifier validation. The template includes a comment identifying
each unresolved declaration by literal field path/location, with no expression text.
`VALUE=${TOKEN:?message}` and literal key-only inheritance remain fully covered.
Discovery rejects `--shell-env` and `--show-values`. It never reads
host inputs or implicit `.env`. Fill the checklist manually, then use ordinary tracing
with explicit inputs to validate required conditions and resolve dynamic files.

For a synthetic list-name example (no inputs needed for discovery):

```sh
stackwrench compose-env --compose examples/compose_env/dynamic-environment/compose.yaml --discover-template --json
stackwrench compose-env --compose examples/compose_env/dynamic-environment/compose.yaml --discover-template --env-template ./dynamic.env.template
stackwrench compose-env --compose examples/compose_env/dynamic-environment/compose.yaml --env-file examples/compose_env/dynamic-environment/inputs.env --json
```

Discovery reports references `KEY`, `WHOLE`, `PASS`, `TOKEN`, two unresolved
declarations and `complete: false`. Ordinary tracing uses the explicit inputs to
resolve `RENAMED`, inherited `INHERITED`, `PASS` and `VALUE` as before.

Library equivalent (reads inputs, writes nothing):

```python
from stackwrench.compose_env import discover_template, env_template

report = discover_template("examples/compose_env/discovery/compose.yaml")
assert report["complete"] is False
text = env_template(report)
assert "# TOKEN=" in text and "synthetic-private-message" not in text
```

### Discover named scenarios without comparing values

Combine `--discover-template` with `--scenario-file` to scan each scenario's explicit
dotenv inputs independently. The manifest uses the same strict 2–16 ASCII-identifier
names and `env_files` lists as comparison; paths resolve relative to the manifest.
Any `shell_env` member is rejected, even for an empty snapshot. The entire manifest
is validated, including unselected definitions and this restriction, before Compose
or any scenario inputs are read.
Top-level `--env-file`, `--shell-env` and `--show-values` are rejected in this mode.

```sh
stackwrench compose-env --compose examples/compose_env/scenario-discovery/compose.yaml --scenario-file examples/compose_env/scenario-discovery/scenarios.json --discover-template --json
stackwrench compose-env --compose examples/compose_env/scenario-discovery/compose.yaml --scenario-file examples/compose_env/scenario-discovery/scenarios.json --discover-template --env-template ./scenarios.env.template
```

Omitting `--scenario` selects all scenarios. Repeat `--scenario NAME` to discover
only specific scenarios; output and reads follow **manifest order**, regardless of
request order. Each occurrence takes one literal ASCII identifier, not a comma-separated
list. Empty, unknown, invalid and duplicate names fail before Compose/dotenv reads.
This option requires both `--discover-template` and `--scenario-file`; it is rejected
for single discovery, ordinary tracing and comparison.

The three-scenario selection example adds `broken`, whose explicit dotenv is missing:

```sh
stackwrench compose-env --compose examples/compose_env/scenario-discovery/compose.yaml --scenario-file examples/compose_env/scenario-discovery/selected-scenarios.json --discover-template --scenario supplied --json
stackwrench compose-env --compose examples/compose_env/scenario-discovery/compose.yaml --scenario-file examples/compose_env/scenario-discovery/selected-scenarios.json --discover-template --scenario supplied --env-template ./supplied.env.template
```

These commands produce just the `supplied` report/section, including `UPSTREAM` and
the unresolved environment/file scopes with `complete: false`. Only selected
explicit dotenv inputs are read, in their declared order. The unselected missing
dotenv and malformed dynamic fallback target remain unopened. Unselected dotenv
contents may be missing or malformed, but invalid unselected manifest definitions
or any `shell_env` member still fail the operation.

JSON has exactly `schema_version: 1`, `report_type: "scenario-template-discovery"`,
`evaluation: "syntax-only"`, `values_shown: false`, `host_environment: "not-read"`,
`implicit_dotenv: "not-read"`, `complete` and `scenarios`. `scenarios` preserves
manifest order and maps each selected name to an ordinary `env-template-discovery` report.
Each child retains its own references, file scopes, unresolved environment scopes
and completeness; outer `complete` is true only if every selected child is complete.
It describes selected scope coverage, not whole-manifest coverage or runnability. There
is no baseline, winner, comparison, change flag, evaluated state or value. Reference
and scope arrays retain single-discovery traversal order (explicit dotenv inputs
in supplied order, Compose strings, then service files/static inheritance).

The example reports `KEY`, `WHOLE`, `PASS`, `TOKEN` in both scenarios; only `supplied`
also references `UPSTREAM` from `COPY=${UPSTREAM:?private-message}`. Each has two
unresolved environment declarations and one unresolved dynamic env_file, so both
are incomplete. The malformed fallback target stays unopened. No required check
is evaluated, no name or path is guessed, and no host or implicit input is read.

Text output labels each scenario in manifest order. `--env-template` creates one
private file exclusively, with a `# Scenario NAME; complete: true/false` section per
scenario. Each section is an independent comment-only checklist: names are sorted
and deduplicated within that section, never unioned across scenarios. Shared names
repeat in each section; unresolved scope comments stay with their scenario. This
is a review checklist; nothing is loaded automatically. Existing files, directories
and symlinks are refused without partial stdout or overwriting their contents.

Manifest/selection errors, unsupported expressions in selected inputs, missing required
selected literal files and malformed selected literal inputs fail the whole operation:
exit 2, redacted stderr, empty stdout and
no export. Input errors identify the scenario; later failures never publish earlier
successful reports. Optional missing literal files still count as covered. Dynamic
file paths remain unopened even with supplied values or fallbacks. Ordinary tracing
and comparison continue to evaluate inputs and required checks as before.

```python
from stackwrench.compose_env import discover_scenarios, env_template

report = discover_scenarios("examples/compose_env/scenario-discovery/compose.yaml",
                            "examples/compose_env/scenario-discovery/selected-scenarios.json",
                            scenario_names=["supplied"])
text = env_template(report)  # Pure rendering; reads and writes nothing.
```

`scenario_names` is keyword-only. Omit it or pass `None` for all; an explicit value
must be a nonempty `list[str]` of unique known names. An empty list, string, tuple,
non-string element or invalid name raises `TraceError` with selection names redacted.
No partial report is returned if any selected scenario fails.


### Select services for syntax discovery

Repeat `--service NAME` with `--discover-template` to scan selected literal services.
Omission retains all-service discovery and its existing report/read behavior. This
option works with single or named discovery, `--scenario`, `--require-complete` and
`--env-template`; ordinary trace/comparison reject it before reading inputs.

```sh
stackwrench compose-env --compose examples/compose_env/service-discovery/compose.yaml --env-file examples/compose_env/service-discovery/explicit.env --discover-template --service a-worker --service z.api --require-complete --env-template ./services.env.template --json
stackwrench compose-env --compose examples/compose_env/service-discovery/compose.yaml --scenario-file examples/compose_env/service-discovery/scenarios.json --discover-template --scenario a_supplied --scenario z_baseline --service z.api --service a-worker --require-complete --json
```

The first command succeeds despite unsatisfied required expressions and the
unselected `missing` service's absent required file. It reports services `z.api`,
`a-worker` in Compose order and references `SHARED`, `PROJECT`, `TOKEN`, `LOCAL`,
`PASS`. The checklist sorts those names and keeps every assignment commented.
The second command returns independent `z_baseline`, `a_supplied` sections in
manifest order; only the supplied section includes `SHARED`. The unselected
`broken` scenario's dotenv stays unopened. Without `--service`, this Compose file
fails because all-service discovery attempts the missing literal service file.

Selection requires a nonempty list of unique, exact, known literal service keys.
CLI names are repeated arguments, not a comma-separated list. Empty, unknown and
duplicate selections fail with a fixed name-redacted diagnostic. Service keys are
not variable identifiers: literal dots/dashes are allowed. Library equivalents:

```python
report = discover_template("examples/compose_env/service-discovery/compose.yaml",
                           ["examples/compose_env/service-discovery/explicit.env"],
                           service_names=["a-worker", "z.api"], require_complete=True)
report = discover_scenarios("examples/compose_env/service-discovery/compose.yaml",
                            "examples/compose_env/service-discovery/scenarios.json",
                            scenario_names=["a_supplied", "z_baseline"],
                            service_names=["z.api", "a-worker"], require_complete=True)
```

Both APIs accept keyword-only `service_names: list[str] | None = None`; other
shapes, nonstring/empty/NUL names and duplicates raise `TraceError`. Names are
matched exactly without modifying the caller's list. Explicit selection adds this
`scope` object to the single discovery report or each named child (schema version
1); default all-service reports retain their existing shape:

```json
{"services": ["z.api", "a-worker"], "shared": ["explicit-dotenv-inputs", "top-level-compose-values"]}
```

All explicitly supplied dotenv files remain shared: their syntax dependencies
count and their contents are strictly parsed even if unrelated to the selected
services. All non-`services` top-level Compose string values also remain shared.
Only selected service string values, static inheritance and literal service file
contents contribute service references. Unselected service references, file rows
and unresolved declarations are excluded. No dependency/profile closure is
computed. Text and comment-only checklists state the selection and shared scope;
service names and metadata are JSON escaped.

With explicit service selection, Compose is read once per selected scenario.
Selection and the **whole document's supported structure and expression grammar**
are validated before any explicit dotenv or selected service-file read. Invalid
unselected expressions, static environment names/values, `env_file` options or
literal paths still fail. Preflight never opens or stats any service file, and
contributes no checklist references. This validates the documented bounded subset,
not the full upstream Compose schema. Named manifest validation, all shell
restrictions and scenario selection still happen first. Omission keeps the prior
all-service input ordering. Actual selected inputs follow manifest/Compose order;
references retain explicit-dotenv, Compose-scalar, then service-file/inheritance
ordering. Unselected scenario inputs and service files are neither opened nor
statted. Selected required literal files and shared explicit inputs remain strict;
optional missing/raw literal file behavior is unchanged.

`complete` and `--require-complete` cover shared and selected scopes only. Selecting
`dynamic` makes coverage incomplete; fallback text or supplied values never resolve
its names/targets. Unselected dynamic scopes cannot block a selected complete
checklist. No required operator is evaluated. Failures in preflight, strict selected
inputs, later scenarios or the completeness gate return no partial report/stdout
and create no export. Successful exports still require a new private file;
existing files and symlinks remain untouched on refusal.


### Require complete discovery coverage

Add `--require-complete` to single or named `--discover-template` analysis when
automation must refuse an incomplete checklist. The default still returns and can
export incomplete discoveries. The gate checks the existing `complete` field after
all selected strict input reads succeed, before any stdout or template creation.
It never evaluates operators, guesses environment names, resolves dynamic paths or
opens their targets to obtain coverage, even when fallback text or supplied values
suggest a filename. Required expressions can remain unsatisfied in a complete scan.
Missing optional literal files count as covered.

These complete literal-scope controls succeed, including the selected `supplied`
scenario despite the unselected `broken` scenario's missing dotenv:

```sh
stackwrench compose-env --compose examples/compose_env/template/compose.yaml --discover-template --require-complete --json
stackwrench compose-env --compose examples/compose_env/template/compose.yaml --scenario-file examples/compose_env/scenario-discovery/selected-scenarios.json --discover-template --scenario supplied --require-complete --env-template ./complete.env.template --json
```

With the dynamic Compose fixture instead, this command exits **2**, writes only a
redacted error to stderr, leaves stdout empty and creates no checklist:

```sh
stackwrench compose-env --compose examples/compose_env/scenario-discovery/compose.yaml --scenario-file examples/compose_env/scenario-discovery/selected-scenarios.json --discover-template --scenario supplied --require-complete --env-template ./refused.env.template --json
```

The fixed gate message is `syntax-only discovery incomplete: dynamic file contents
or environment declaration names remain unresolved`. It includes no input names,
paths, expressions, values, operands or required messages. With `--json`, it uses
the existing `error.code: "invalid-or-unsupported-input"` envelope. Success returns
the unchanged report schema. Named completeness covers only selected children;
whole-manifest validation and manifest-order reads/output are unchanged. Later
selected strict input errors still take precedence over the gate. Unselected inputs
and all variable-dependent file targets stay unopened. Existing export files and
symlinks are untouched on failure; successful exports still require a new private file.

Both discovery APIs accept keyword-only `require_complete: bool = False`:

```python
report = discover_template("examples/compose_env/template/compose.yaml", require_complete=True)
report = discover_scenarios("examples/compose_env/template/compose.yaml",
                            "examples/compose_env/scenario-discovery/selected-scenarios.json",
                            scenario_names=["supplied"], require_complete=True)
```

Non-boolean API values raise a redacted `TraceError` before input reads. Incomplete
coverage raises `TraceError` after successful selected scans, without returning a
partial report. The CLI gate requires `--discover-template` and rejects ordinary
trace/comparison before analysis; those APIs do not accept this option. Existing
discovery restrictions on shell snapshots and value disclosure still apply.

### Consume a structured discovery checklist

Use `--checklist-json` with `--discover-template` to receive per-variable requirements
and sources as JSON. It implies JSON output; adding `--json` is harmless. Omit the
flag for the unchanged per-use discovery report or text output. For example:

```sh
stackwrench compose-env --compose examples/compose_env/service-discovery/compose.yaml --env-file examples/compose_env/service-discovery/explicit.env --discover-template --service a-worker --service z.api --require-complete --checklist-json
stackwrench compose-env --compose examples/compose_env/service-discovery/compose.yaml --scenario-file examples/compose_env/service-discovery/scenarios.json --discover-template --scenario a_supplied --scenario z_baseline --service z.api --service a-worker --checklist-json
stackwrench compose-env --compose examples/compose_env/service-discovery/compose.yaml --discover-template --service dynamic --checklist-json
```

The first command reports sorted variables `LOCAL`, `PASS`, `PROJECT`, `SHARED`,
`TOKEN`, despite unsatisfied required checks. The second retains manifest order
`z_baseline`, `a_supplied`; only `a_supplied` includes `SHARED`. The third preserves
unresolved file/environment scopes and `complete: false` without opening targets.

The exact version-1 single checklist object has these fields:

| Field | Contract |
| --- | --- |
| `schema_version`, `report_type` | `1`, `"env-template-checklist"` |
| `evaluation`, `values_shown` | `"syntax-only"`, `false` |
| `host_environment`, `implicit_dotenv` | Both `"not-read"` |
| `complete` | Original discovery coverage boolean; true does not imply runnability or satisfied inputs |
| `scope` | Exactly `service_selection`, `services`, `shared` as described below |
| `variables` | Array sorted lexicographically by ASCII variable name, empty if none |
| `service_env_files` | Original ordered file declaration metadata/status, including unresolved and optional missing rows |
| `unresolved_environment` | Original ordered unresolved declaration metadata, with no guessed names |

`scope.service_selection` is `"selected"` with the original Compose-order names in
`scope.services` for explicit service selection. Otherwise it is `"all"` and
`scope.services` is JSON null: service names are not inferred from references.
`scope.shared` is always `["explicit-dotenv-inputs", "top-level-compose-values"]`.
Selected service scope and shared inputs have the same discovery semantics above;
there is no profile or dependency closure.

Each `variables` row has exactly `variable`, `operators`, `required`, `sources`.
`operators` is a lexicographically sorted array of distinct operator strings:
`direct`, `-`, `:-`, `?`, `:?`, `+`, `:+`, `inherited-null`, `inherited-key-only`.
`required` is `"nonempty"` if any `:?` occurs, otherwise `"set"` if any `?` occurs
(empty allowed), otherwise `"no"`. These use the same rules as `env_template()`.
Defaults, alternatives, direct references and inheritance imply no unconditional
requirement. Requirements describe syntax across all listed scopes, including
service-local and shadowed references; they do not describe current input state,
only missing external inputs, or a globally sufficient assignment plan.

`sources` keeps **every reference in original discovery order**, including repeated
references and shadowed declarations. Every source has exactly `operator`, `phase`,
`field_path`, `location`, plus `offset` when present in the original reference.
Phases are `interpolation-input`, `compose`, `service-env-file`; offsets are zero-based
positions in decoded scalars. Source references are not deduplicated; operator names
are. Field paths and locations retain their original strings/indexes, including
escaped metadata. File rows contain only `service`, `field_path`, `location`,
`required`, `format`, `status` and, for optional missing files, `reason: "missing"`.
Unresolved environment rows contain only `service`, `field_path`, `location`, `status`.
No values, expressions, operands, required messages or expanded filenames are copied.

Named output has exactly the six common metadata fields above, `complete`, and
`scenarios`, with `report_type: "scenario-template-checklist"`. Each selected name
maps to its independent single checklist in manifest order. There is no merged
variable array or comparison. Aggregate completeness covers only selected children.

The public projection API reads/writes no files, consults no host inputs, evaluates
no operators, and returns detached lists/dicts without changing the source report:

```python
from stackwrench.compose_env import discover_template, discovery_checklist

report = discover_template("examples/compose_env/service-discovery/compose.yaml",
                           service_names=["z.api", "a-worker"])
checklist = discovery_checklist(report)
assert checklist["variables"][-1]["required"] == "nonempty"  # TOKEN
```

`discovery_checklist(report: dict) -> dict` accepts version-1 single/named syntax-only
discovery reports (including equivalent decoded JSON). Trace, comparison, already
projected checklist, wrong modes/versions, or malformed known metadata raise the
fixed `TraceError`: `checklist requires a valid syntax-only discovery report`.
Unknown extra keys are ignored, never copied. Metadata remains visible; the CLI
JSON-escapes it, including controls and newlines. This is a redacted projection of
the bounded discovery model, not validation of arbitrary Compose input.

The CLI requires discovery and retains its shell/value restrictions, selection,
strict-read order and `--require-complete` gate. Runtime failures use the existing
JSON error envelope on stderr with exit 2 and empty stdout, even without `--json`;
argument conflicts keep the existing redacted usage diagnostic. Combining
`--env-template NEW_FILE` exports the **original** discovery's comment-only checklist
with exclusive private creation. Projection finishes before export, so a failed
projection or analysis creates no file. Existing outputs/symlinks remain untouched.

## Compare syntax-only checklists across named scenarios

Use `--compare-checklists-json` with `--discover-template --scenario-file` to
compare syntax requirements without evaluating inputs. Select at least two scenarios;
the baseline is the **first selected scenario in manifest order**, regardless of
`--scenario` argument order. Each other selected scenario compares independently
against that baseline. For example (all inputs are synthetic):

```sh
stackwrench compose-env --compose examples/compose_env/checklist-comparison/compose.yaml --scenario-file examples/compose_env/checklist-comparison/scenarios.json --discover-template --scenario a_candidate --scenario z_baseline --scenario m_identical --service worker --service api --require-complete --compare-checklists-json
```

The baseline is `z_baseline`, followed by comparisons for `a_candidate` and
`m_identical`. The candidate adds `ADDED`, removes `REMOVED`, changes `OPERATOR`
from direct to default without a requirement change, and strengthens `REQUIRED`
from `set` to `nonempty`. `REPEATED` retains its operator and requirement but has
different source metadata. The identical scenario has no differences. Shared
Compose references and inherited `PASS` stay in each independent section. Required
inputs need not be supplied: `complete: true` describes selected syntax coverage.
Missing unselected inputs and service files are never opened or statted.

For incomplete discovery, add `--service dynamic` and omit `--require-complete`.
The comparison retains each child's unresolved file and environment declarations,
with `complete: false`; dynamic paths and declaration names remain unknown even
if inputs or defaults could supply them. The gate still rejects incomplete
selected coverage before any output or template creation.

The pure public API accepts a version-1 **named discovery report**, including an
equivalent decoded JSON object; it does not read the manifest or inputs again:

```python
from stackwrench.compose_env import discover_scenarios, compare_discovery_checklists

report = discover_scenarios(
    "examples/compose_env/checklist-comparison/compose.yaml",
    "examples/compose_env/checklist-comparison/scenarios.json",
    scenario_names=["a_candidate", "z_baseline"], service_names=["worker", "api"])
comparison = compare_discovery_checklists(report)
assert comparison["baseline"] == "z_baseline"
```

`compare_discovery_checklists(report: dict) -> dict` returns exactly the common
metadata (`schema_version: 1`, `evaluation: "syntax-only"`, `values_shown: false`,
`host_environment: "not-read"`, `implicit_dotenv: "not-read"`, original `complete`),
`report_type: "scenario-template-checklist-comparison"`, `baseline`, `scenarios`,
and `comparisons`. `scenarios` is identical to `discovery_checklist(report).scenarios`:
independent single checklist objects in original manifest order, retaining every
source, scope, file status and unresolved declaration. Nothing merges their coverage
or equates coverage with satisfied requirements.

`comparisons` maps each nonbaseline name in manifest order to exactly
`{"variables": [...]}`. Every array contains the lexicographically sorted union of
names in the baseline and that child only, **including unchanged rows**. Each row
has exactly these fields:

| Field | Contract |
| --- | --- |
| `variable` | ASCII variable name |
| `baseline_present`, `scenario_present` | Booleans describing syntax references, not supplied values |
| `presence_change` | `"added"`, `"removed"`, or `"unchanged"` relative to baseline |
| `baseline_operators`, `scenario_operators` | Sorted distinct operators; `[]` on an absent side |
| `baseline_required`, `scenario_required` | Shared checklist classification `"nonempty"`, `"set"`, `"no"`; JSON null on an absent side |
| `operators_changed` | Both present and operator arrays differ |
| `requirement_changed` | Both present and requirement classifications differ |
| `source_changed` | Both present and ordered source descriptors differ, excluding their `operator` field |

Presence changes leave all three `*_changed` flags false: an absent name has no
requirement or source to compare. A common name can change operators without
changing its requirement. Source descriptors compare `phase`, `field_path`,
`location`, and optional `offset`; order and multiplicity matter. An operator
replacement at the same descriptor alone does not flag `source_changed`, though
changed offsets or other metadata independently can. Literal labels are compared
as recorded, without physical file identity checks or inferred names/paths. Exact
source arrays including operators remain available under `scenarios`.

The API is a detached, allowlisted projection: no mutation, input reads/stats,
host/implicit dotenv lookup, operator evaluation, value/operand/message copying,
or dynamic inference. Unknown extra keys are ignored. It requires 2–16 valid selected
children and validates metadata through the existing checklist projection. Single
discovery, trace/evaluated comparison, checklist/already-compared output, wrong
versions/modes and malformed known metadata raise the fixed redacted `TraceError`:
`checklist comparison requires a valid named syntax-only discovery report with at least two scenarios`.

The CLI flag implies JSON; `--json` is allowed. `--checklist-json` is mutually
exclusive, and discovery's shell/value/shared-input restrictions still apply.
Invalid modes fail before input reads with the existing redacted argument diagnostic
and exit 2. Selecting fewer than two children fails after normal discovery with a
JSON runtime error on stderr, exit 2, empty stdout and no export. Other runtime
errors use the same envelope, including later selected input failures. Comparison
finishes before `--env-template NEW_FILE` exclusively creates the original discovery's
comment-only template; existing files and symlinks are untouched. Omitting this
flag retains the existing single/named discovery and checklist shapes.

## Compare two saved discovery artifacts offline

Capture each revision independently with `--discover-template --json` (and any
explicit input/service selection), saving only successful single-discovery stdout.
Later compare those two files, even when every original Compose/dotenv file has
been removed. No scenario manifest or Compose file is needed:

```sh
stackwrench compose-env --compare-discovery-artifacts examples/compose_env/artifact-comparison/baseline.json examples/compose_env/artifact-comparison/candidate.json
```

These checked-in synthetic captures were produced from separate temporary Compose
revisions; their original files are absent. Baseline selects `api` plus shared inputs
and is complete, retaining scanned Compose/raw and skipped optional file metadata.
Candidate covers all its services, with unresolved dynamic file/environment scopes
and `complete: false`. The result adds `ADDED`, removes `REMOVED`, changes `TOKEN`
from required `set` to `nonempty`, and retains both independent scopes. Different
source labels can flag `source_changed` even when an operator is unchanged.

```python
from stackwrench.compose_env import compare_discovery_artifacts

comparison = compare_discovery_artifacts(
    "examples/compose_env/artifact-comparison/baseline.json",
    "examples/compose_env/artifact-comparison/candidate.json")
assert comparison["baseline"] == "baseline"
assert comparison["scenarios"]["baseline"]["complete"] is True
assert comparison["scenarios"]["candidate"]["complete"] is False
```

`compare_discovery_artifacts(baseline_path: str | Path, candidate_path: str | Path)
-> dict` reads only the two explicit local artifacts, in that order, and writes
nothing. It reuses the exact comparison schema and row semantics documented above:
`schema_version: 1`, `report_type: "scenario-template-checklist-comparison"`, fixed
`baseline: "baseline"`, `scenarios` ordered `baseline`, `candidate`, and only
`comparisons.candidate`. The existing `scenario_present`, `scenario_operators` and
`scenario_required` row fields refer to candidate. Top-level `complete` is the
conjunction; each side retains its own scope, completeness, source arrays, file
statuses and unresolved metadata. The same artifact can be supplied twice.

Each input must be a regular UTF-8 file of at most **2 MiB (2,097,152 bytes)**,
with at most **64 JSON object/array container levels**, counting the root as 1.
Depth is checked before JSON decoding, including unknown fields. Reads are capped
at limit + 1 byte. Duplicate decoded JSON keys at **any** depth (including escaped
aliases), malformed/trailing JSON, BOM, invalid UTF-8, NaN/Infinity and numbers
that overflow to infinity are rejected. There is no extra row-count cap; memory
and output scale with the two bounded artifacts. These are parser limits, not a
hostile-filesystem or concurrent-replacement sandbox.

Only `schema_version: 1`, `report_type: "env-template-discovery"`,
`evaluation: "syntax-only"`, `values_shown: false`, `host_environment` and
`implicit_dotenv` both `"not-read"` are accepted. Known references, scope, file and
unresolved metadata must pass `discovery_checklist()` validation, including a
boolean `complete` consistent with unresolved rows. Named discoveries, already
projected checklists/comparisons and evaluated traces are rejected. Unknown extra
keys, including value/operand/message fields, are ignored after strict JSON
validation and never copied. Original allowlisted source labels/field names remain
visible metadata; the CLI JSON-escapes them. Artifacts are caller-provided assertions,
not authenticated evidence of their capture or of current project state.

No recorded Compose, dotenv, dynamic, implicit or host path is opened, statted,
resolved or canonicalized. Operators are never evaluated. Differences are syntax
and ordered literal metadata only; independently differing scopes are retained,
without any equivalent-coverage or input-satisfaction claim. No current input state,
physical identity, guessed dynamic name or global assignment plan is inferred.

The pair option takes exactly two arguments and may occur once. It always emits
JSON; `--json` is allowed but redundant. Every live-input/discovery/selection/value
or export option is excluded: `--compose`, `--env-file`, `--shell-env`,
`--scenario-file`, `--scenario`, `--service`, `--discover-template`,
`--require-complete`, `--checklist-json`, `--compare-checklists-json`, `--show-values`
and `--env-template`. Conflicts and missing pair arguments fail before input reads
with exit 2 and the fixed redacted argument diagnostic. Original live modes still
require exactly one `--compose`; their defaults and exports are unchanged.

Any artifact read/parse/schema failure raises the fixed `TraceError`:
`artifact comparison requires two valid v1 single syntax-only discovery JSON files`.
CLI data errors return exit 2, empty stdout, and JSON stderr:
`{"error":{"code":"invalid-or-unsupported-input","message":"artifact comparison requires two valid v1 single syntax-only discovery JSON files"}}`.
Both artifacts validate before success output; artifact mode never exports a file.

## Compare named environment scenarios

Run the same two-phase analysis with explicitly supplied development/CI inputs:

```sh
stackwrench compose-env --compose examples/compose_env/scenarios/compose.yaml --scenario-file examples/compose_env/scenarios/scenarios.json --json
stackwrench compose-env --compose examples/compose_env/scenarios/compose.yaml --scenario-file examples/compose_env/scenarios/scenarios.json
```

The scenario manifest is a JSON object, with the first scenario as the baseline:

```json
{
  "development": {"env_files": ["dev.env"]},
  "ci": {"env_files": ["ci.env"], "shell_env": "ci.json"}
}
```

All scenario input paths resolve relative to the **manifest's directory**; `--compose`
still resolves relative to the working directory. Each scenario independently uses
the existing precedence and parsing rules. `env_files` is an ordered list, defaulting
to `[]`; optional `shell_env` names a JSON string-to-string snapshot. `{}` means no
interpolation inputs. Omit unset names in those inputs; use `KEY=` or `"KEY": ""`
for empty, and a nonempty string for an explicit value. No host environment, implicit
`.env`, shell execution, or input sharing between scenarios is added.

The example's development input omits `OPTIONAL` and `MODE`; CI supplies empty
`OPTIONAL` and explicit `MODE=synthetic-default`. Both scenarios produce the same
nonempty `DEFAULTED` container value, while its expression branch changes from
`default` to `source`. `OPTIONAL` changes from unset/removed to empty. `SAME` keeps
the same value but changes its input-file origin; `CHANGED` changes its value at the
same Compose declaration. All fixture values are synthetic. Add `--show-values`
only when you want successful reports to reveal them.

Comparison JSON has its own `report_type: "scenario-comparison"`, `schema_version: 1`,
`baseline`, full two-phase reports under `scenarios`, and `comparisons` keyed by each
nonbaseline scenario. Each comparison has `interpolation.variables` and
`services.<name>.variables`. Rows cover the union of winning variable names in the
pair; referenced-but-unsupplied Compose inputs are also listed as unset. Each row
contains `baseline_status` and `scenario_status` (`unset`, `empty`, or `set`), plus:

- `source_changed`: winning declaration metadata differs (source, reported location,
  syntax, inheritance origin, or service env_file physical identity). It ignores
  values, states and local use indexes.
- `resolution_changed`: recorded value-interpolation provenance differs, including
  branch (`default`, `source`, etc.), expression-file identity and upstream source
  metadata. This follows sequential dotenv dependencies too; unrelated use-index
  shifts are ignored.
- `value_changed`: exact final values differ, even when both are nonempty and hidden.
  Unset compares as null and empty as `""`. No hashes/fingerprints are emitted.
- `path_selection_changed`: the winning service env_file declaration's recorded
  selector provenance differs, including branch and upstream interpolation source
  metadata. It ignores values, input/result states and local use indexes. It is
  separate from physical source identity and value-resolution provenance.
- `dependency_path_selection_changed`: selector provenance differs for service
  env_file entries reached through the winner's recorded value dependencies. It
  excludes the direct winner, using the same provenance equality as the direct flag.

For example, `comparisons.ci.services.demo.variables.DEFAULTED` is:

```json
{
  "baseline_status": "set",
  "scenario_status": "set",
  "source_changed": false,
  "resolution_changed": true,
  "value_changed": false,
  "path_selection_changed": false,
  "dependency_path_selection_changed": false
}
```

The five flags are independent. Changes compare **winners and recorded value
dependencies**; shadowed histories remain available in each full scenario report
but are not themselves diffed. Service env_file origins compare both the reported
Compose declaration slot and a **private physical identity** (device/inode from the
file opened for reading). Different files at the same slot count as different
sources even when their content is identical. Relative/absolute spellings, symlinks
and hard links to the same file at the same slot do not create a source change.
Identity also follows recorded value dependencies; changing only shadowed file
origins does not flag an unchanged winner. Identity is local to the analysis run;
no paths, identity numbers or hashes are added to reports, including with
`--show-values`. This is not a filesystem snapshot: concurrent replacements or
inode reuse are not guarded against. Image ENV remains unmodeled. These boundaries
are also identified by JSON `comparison_scope`, whose `service_env_file_identity`
is `"private-device-inode"`.

An equal-content example selects two files through the same Compose slot:

```sh
stackwrench compose-env --compose examples/compose_env/file-scenarios/compose.yaml --scenario-file examples/compose_env/file-scenarios/scenarios.json --json
```

`comparisons.right.services.demo.variables.PLAIN` reports `source_changed: true`,
`resolution_changed: false`, and `value_changed: false`. `COPY` also changes
resolution through its service-local dependency; `FOLLOW` in a stable winning file
changes resolution and dependency selector provenance. `OVERRIDDEN` stays unchanged because its winner is the
Compose environment declaration. Neither expanded selected filename appears in
default JSON or text output. Path-selection expressions remain in each scenario's
use list with results hidden by default; `--show-values` can reveal selector values
and expansion results under the existing explicit value-disclosure policy.

To distinguish a changed selector from a changed file, run:

```sh
stackwrench compose-env --compose examples/compose_env/path-selection/compose.yaml --scenario-file examples/compose_env/path-selection/scenarios.json --json
```

Here `${FILE:-selected.env}` selects the same physical file in all three scenarios.
`baseline` leaves `FILE` unset; `explicit` supplies `FILE=selected.env`; `identical`
repeats the baseline. `comparisons.explicit.services.demo.variables.PLAIN` has
`path_selection_changed: true` while `source_changed`, `resolution_changed` and
`value_changed` are all false. `OVERRIDDEN` is won by `environment` and keeps all
five flags false, as does the identical-input control.

Direct `path_selection_changed` follows only the winning service env_file's path
uses and their upstream **explicit interpolation** dependencies. It does not follow
selectors of files supplying the winner's value dependencies. Non-file or absent
winners contribute no direct selector events; interpolation-input rows always
report false for both selector flags. Neither flag diffs shadowed-only histories
or counts unrelated input/use-index noise.
Thus a dynamic file winner appearing/disappearing can change selector provenance,
while literal-path winners have no variable selector provenance. Changing only a
selector value with the same recorded provenance does not set this flag; physical
file changes remain covered by `source_changed`. Even unused-by-branch sources
count when recorded by interpolation, matching `resolution_changed`'s provenance
model. JSON `comparison_scope.path_selection` identifies this scope as
`"winning-service-env-file-selector-provenance"`.

The separate `dependency_path_selection_changed` follows `resolved_from` and
recorded `interpolation_uses -> input_source` links strictly below the winner,
including transitive sequential dotenv dependencies. Only reachable entries sourced
from service env_files contribute selectors; it never scans variable pools or
shadowed arrays. A historical declaration **does** count when a recorded dependency
actually refers to it. Sources recorded for unused operator branches still count,
matching value-resolution provenance. The walk visits each entry/use once to stop
cycles and deduplicate shared dependencies; local use indexes locate events but
are not compared. The union of dependency selector uses is normalized with the
same upstream provenance rules as the direct flag. Empty/literal selector sets
have no events. A literal environment winner has neither selector signal.
`comparison_scope.dependency_path_selection` is
`"recorded-value-dependency-service-env-file-selector-provenance"`.

```sh
stackwrench compose-env --compose examples/compose_env/dependency-path-selection/compose.yaml --scenario-file examples/compose_env/dependency-path-selection/scenarios.json --json
```

Here the same `${FILE:-selected.env}` supplies `PLAIN=synthetic-equal`, and a later
literal `stable.env` supplies `FOLLOW=${PLAIN}`. In `explicit`, `FOLLOW` has only
`dependency_path_selection_changed: true`; its source, resolution, value and direct
selector flags stay false. `PLAIN` has only `path_selection_changed: true`, with no
dependency selector change. Identical-input, shadowed-selector and literal
environment-winner controls keep all five flags false. No expanded filenames,
values or private identities appear in default JSON or text.

Winner-to-path-use attribution stays in the private sidecar with physical identity;
no expanded filenames, identity numbers, selector fingerprints or hashes are added
to public traces. Default output remains redacted. `--show-values` retains its
explicit ability to reveal existing selector input values and expansion results.

Require 2–16 scenarios, each named with an ASCII identifier; the manifest has the
same 2 MiB input-file limit. Unknown options, duplicate JSON keys, invalid/empty
paths, null/nonstring snapshots, missing inputs, and required-variable failures
return exit **2**, with redacted stderr and **no partial stdout**, even if an earlier
scenario succeeded. `--scenario-file` cannot be combined with top-level `--env-file`
or `--shell-env`; put every scenario's inputs in the manifest. Comparisons reveal
state and equality information intentionally; actual values stay hidden by default.

Library equivalent (same validation, redaction, and atomic failure behavior):

```python
from stackwrench.compose_env import compare_scenarios

report = compare_scenarios(
    "examples/compose_env/scenarios/compose.yaml", "examples/compose_env/scenarios/scenarios.json"
)
change = report["comparisons"]["ci"]["services"]["demo"]["variables"]["DEFAULTED"]
assert change["resolution_changed"] and not change["value_changed"]
```

## Supported model and precedence

1. Read only explicitly named `--env-file` inputs, in order; later declarations win.
   No implicit `.env`, `COMPOSE_FILE`, `COMPOSE_ENV_FILES`, or host-environment lookup.
   `--shell-env` is an optional JSON object of ASCII identifier names to **strings**
   and wins over all explicit interpolation files, including empty strings. Omit
   unset names; JSON null, nonstring values, and duplicate keys are rejected.
2. Expand string **values**, never YAML mapping keys, throughout the one literal
   Compose document. Interpolation sources are the inputs above, never service
   env_files. Unresolved direct references become empty with a warning. A source
   value containing `$OTHER` is data and is not recursively expanded.
3. Read each service's env_file path(s) relative to the Compose file's directory,
   in list order. Explicit interpolation inputs win when expanding their values;
   otherwise earlier declarations in the current service env_file win over earlier
   files. Those files populate the container environment without becoming global
   interpolation inputs.
4. Service `environment` mapping/list entries override all service env_files,
   including explicit empty values and unresolved null/key-only declarations.
   Null/key-only resolves from the explicit interpolation pool; an unresolved
   declaration removes that variable. Duplicate list entries retain their ordered
   history. Duplicate YAML mapping keys are rejected.

CLI `--compose`, `--env-file`, and `--shell-env` paths are relative to the current
working directory. Service env_file paths can themselves use supported
interpolation. Absolute service file paths generate a portability warning.
Every opened file must be a local regular file, readable UTF-8, at most 2 MiB; YAML depth
is limited to 64. Each expanded value is limited to 2 MiB characters and a report
to 10,000 interpolation uses. These are guardrails, not a resource-isolation sandbox.

Supported expressions (ASCII names `[A-Za-z_][A-Za-z0-9_]*`):

| Expression | Unset | Empty | Nonempty `value` |
| --- | --- | --- | --- |
| `$V`, `${V}` | empty + warning | empty | `value` |
| `${V:-word}` | `word` | `word` | `value` |
| `${V-word}` | `word` | empty | `value` |
| `${V:?message}` | error | error | `value` |
| `${V?message}` | error | empty | `value` |
| `${V:+word}` | empty | empty | `word` |
| `${V+word}` | empty | `word` | `word` |
| `$$V` | literal `$V` | literal `$V` | literal `$V` |

Operands are literal strings; `$` or `{` inside a braced operand is rejected,
even in an unused branch. Nesting, shell evaluation, replacement, assignments,
indirection, and substring operators are unsupported. Standalone `$` followed by
an invalid variable-start character remains literal, including `$(command)`.
Backticks and command strings are never executed. YAML single quotes do not
suppress Compose interpolation; **dotenv** single quotes do.

Normal dotenv subset, for explicit interpolation files and service env_files without `format: raw`:

```dotenv
# Full-line comment; blank lines also accepted.
KEY=value # inline comment preceded by whitespace
HASH=value#literal
EMPTY=
SPACES = spaces inside an unquoted value
LITERAL='$OTHER # literal'
ESCAPED_SINGLE='Let\'s go'
DOUBLE="tab\tnewline\nreturn\rbackslash\\quote\"" # comment
COPY=${KEY:-fallback}
```

Only one-line `KEY=VALUE` declarations are accepted. Leading/trailing whitespace
is trimmed. Quoted closing delimiters can be followed by whitespace and a comment.
Double quotes support `\n`, `\r`, `\t`, `\\`, `\"`; other double-quote escapes are
rejected. Single quotes preserve backslashes except `\'`. Unquoted values preserve
backslashes; quotes inside an unquoted value are rejected. Previous declarations
can be referenced sequentially, respecting the precedence above. **Bare dotenv
keys, `export`, colon delimiters, multiline quotes and unsupported escapes fail**.
Bare keys remain fully supported in the service **environment list**, where they
mean inheritance. This distinction is intentional.

Environment mapping values accept strings, nulls, and canonical signed 64-bit
integers; quote booleans, floats, hex/octal, underscored or noncanonical numbers.
Environment list entries must be strings. Names are restricted to ASCII identifiers.

## Reading the report and privacy

JSON `schema_version: 1` contains:

- `interpolation.variables`: each supplied variable's `winner` and `shadowed` inputs.
- `interpolation.uses`: ordered uses from all three parse contexts (`phase` identifies
  interpolation-input, Compose, or service-env-file expansion). Each use has input
  state/source (metadata only, even with values enabled), operator, branch, result state, location, and a zero-based offset
  in the **decoded** value. This is not a raw-byte YAML column for folded scalars.
  `field_path` identifies the literal Compose field or dotenv assignment key;
  dotenv/environment declarations also carry this additive metadata.
- `services.<name>.variables`: separate container winners and shadowed declarations.
  `interpolation_uses` indexes the use list; `resolved_from` explains inheritance.
- `service_env_files`: ordered file declaration metadata/status, as described above;
  no expanded selected paths.
- `warnings` and `not_evaluated_fields`: explicit boundaries, plus image ENV unknown.

States are `unset`, `empty`, `set`; `set` means nonempty, not secret classification.
Default reports contain no values, raw expressions, fallback text, custom required
messages, or expanded env_file paths. `--show-values` adds `value`/`result` fields
on success. Metadata (names, service names, caller-supplied filenames, line numbers,
field names) is visible: do not encode secrets into these identifiers/paths. Errors
suppress parser excerpts and supplied values regardless of `--show-values`.

## Deliberate limits and upstream relationship

This is an independent small model, not a Compose launcher, schema validator, merge
engine, or fully compatible implementation. It does not discover image ENV or CLI
`run -e` overrides. An `unset` winner means removed by this model; it is not a claim
that an uninspected image has no other environment variables.

Exactly one Compose YAML/JSON document is accepted. Multiple `--compose` arguments,
`include`, `extends`, YAML anchors/aliases/merge keys/explicit tags, top-level
env_file/jobs/models, unsupported env_file options/formats, and missing required
env_files are errors. Optional/raw list entries use the bounded contract above.
Other fields are traversed for interpolation, then explicitly listed as not
evaluated: commands, image validity, profiles, build args, hooks, secrets, networks,
unknown extension fields, etc. All declared services are explained regardless of
profiles. No external referenced file other than service env_files is read.

The [Compose interpolation specification](https://github.com/compose-spec/compose-spec/blob/main/12-interpolation.md)
and [container environment specification](https://github.com/compose-spec/compose-spec/blob/main/container_spec.md#environment)
are the behavioral references. [Docker Compose](https://github.com/docker/compose)
already offers `config --environment` and resolved configuration.
[ComposeLens](https://github.com/Strukturpiloten/compose-lens) already offers
interpolation and provenance. This tool's focused workflow is a deterministic,
redacted, per-variable explanation of **both** phases and shadowed declarations;
no novelty, performance or complete compatibility claim is made. No GPL
podman-compose or MPL ComposeLens code was copied. See [THIRD_PARTY.md](../THIRD_PARTY.md).
