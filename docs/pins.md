# stackwrench pins

`stackwrench pins` (formerly `mise-pinbridge`) is an offline command that reconciles
exact Node, Python and Go pins from one explicitly selected `mise.toml` into selected
Dockerfiles, runtime version files, and literal GitHub Actions setup inputs. Review
the unified diff, then apply the same edits. It never installs a runtime, evaluates
mise tasks, runs a shell, contacts a registry, or builds/pulls/starts containers.

## Install

Requires Python 3.12 or newer on Linux. This command has no runtime dependencies
beyond the standard library.

```sh
pipx install stackwrench          # or: pip install stackwrench
stackwrench pins --version        # prints the bare stackwrench version

# Or run without installing:
uvx stackwrench pins --help
```

`python -m stackwrench pins` is equivalent to `stackwrench pins`. The command has
three subcommands: `check`, `plan` and `apply`.

## Complete example

The checked-in fixtures are synthetic text, not runnable container builds.
The examples use files from a stackwrench source checkout; run them from its root:

```sh
# A drift finding is intentionally exit 1 (input errors would be exit 2).
if stackwrench pins check --root examples/pins/project --json; then
    echo "already clean"
else
    result=$?
    test "$result" -eq 1 || exit "$result"
fi
stackwrench pins plan --root examples/pins/project

# Keep the original fixture unchanged. Start with a new copy destination.
test ! -e /tmp/pins-copy
cp -R examples/pins/project /tmp/pins-copy
stackwrench pins apply --root /tmp/pins-copy
stackwrench pins check --root /tmp/pins-copy --json
stackwrench pins apply --root /tmp/pins-copy  # No changes.

# Deliberately unsupported source; assert failure rather than ignoring it.
if stackwrench pins check --root examples/pins/failure-range --json; then
    echo "unexpected success"; exit 1
else
    result=$?
    test "$result" -eq 2 || exit "$result"
fi
```

For example `FROM node:20.11.0-alpine AS app # note` becomes
`FROM node:22.14.0-alpine AS app # note` when `[tools].node = "22.14.0"`.
The Python and Go image names are `python` and `golang`; the mise keys are
`python` and `go`.

## Selection and supported inputs

All commands require `--root`. `--mise mise.toml` selects the single source
relative to that root (the default is exactly `mise.toml`, not mise discovery).
No parent configs, environment overrides, includes, `mise.lock`, registry
metadata or installed runtimes are read. Only the source's `[tools]` table is
interpreted. Other TOML tables are left untouched and never executed.

By default, inspect exactly these root-relative names, if present:
`Dockerfile`, `.nvmrc`, `.node-version`, `.python-version`, `.go-version`.
There is no recursive search. The source is always required. Repeat
`--dockerfile PATH` to replace the default Dockerfile selection; repeat
`--version-file PATH` to replace the four default runtime-file selections.
Repeat `--workflow PATH` to add explicitly selected GitHub Actions workflows;
none are selected by default and no workflow directory is scanned.
Every explicitly selected file must exist. All selected paths must be distinct,
root-relative, without `..`. For example:

```sh
# Select an existing nested Dockerfile and .node-version in your own project.
stackwrench pins plan --root /path/to/project --mise config/mise.toml \
  --dockerfile containers/Dockerfile --version-file web/.node-version
```

Supported declarations:

* `[tools]` must be nonempty and contain only `node`, `python`, `go`, each a
  string `major.minor.patch` with ASCII digits and no leading zero components.
  Versions are compared as strings; no validity/availability lookup is performed.
* Dockerfile `FROM [--platform=VALUE] IMAGE [AS ALIAS] [# comment]`, case-insensitive
  instruction/`AS`, on one physical line. The only supported external images are
  unqualified `node:X.Y.Z[-suffix]`, `python:X.Y.Z[-suffix]`, and
  `golang:X.Y.Z[-suffix]`. Tags must already contain three numeric components.
  The suffix is an opaque Docker tag string, retained exactly. `--platform` is
  retained literally, including variables; it is not evaluated. Comments,
  indentation, stage aliases and other instruction text are retained.
* `FROM scratch` and references to earlier named stages are recorded as
  `evaluated: false`, with no pin comparison. Unknown external images, forward
  stage references, duplicate aliases, digests and dynamic image names block all edits.
* Runtime files have exactly one version line: optional horizontal whitespace,
  optional `v`, exact `X.Y.Z`, optional whitespace/`# comment`, optional LF/CRLF.
  The optional `v`, comment and whitespace are preserved. Multi-line files,
  `lts/*`, ranges, prefixes in mise, prereleases and build metadata are refused.
  Only the four named basenames are recognized even when nested.

Each selected file must be a regular, singly linked, UTF-8 file of at most 1 MiB,
with LF/CRLF (or no final newline), without a BOM or other control/Unicode line
separator characters. Symlinks in **any** path component, including
the root and its ancestors, are refused. Absolute selected paths and parent
traversals are refused, even when they would lead back inside root. No file is
created for an absent optional default.

## Commands, JSON and failure behavior

| Command | Output | Exit status |
| --- | --- | --- |
| `check --root ROOT [--json]` | Comparison summary or schemaVersion 1 JSON | 0 clean, 1 drift, 2 blocked/error |
| `plan --root ROOT` | Unified diff; empty when clean; no writes | 0 valid plan (including drift), 2 blocked/error |
| `plan --root ROOT --explain` | Per-file explanations, including blocked batches; no writes | 0 valid plan, 2 blocked/error |
| `plan --root ROOT --json` | Per-file explanations and applicable diff as JSON; no writes | 0 valid plan, 2 blocked/error |
| `apply --root ROOT` | Number of updated files or `No changes.` | 0 successful/no-op, 2 blocked/error |

Human `check` prints only drifting declarations, each with its current target
location, tool, old/new values, mise source reference, physical source location
and existing declaration identity:

```text
ci.yml:6: node: 20.11.0 -> 22.14.0 (source mise.toml:[tools].node; location mise.toml:2); declaration: "jobs.test.steps[0] actions/setup-node@v4 with.node-version"
Drift detected.
```

The `; location PATH:LINE` segment inside the source parentheses uses the existing
`sourcePath` and `sourceLine` fields. Comments and blank lines count toward the
physical line, including supported quoted/dotted TOML keys. An unknown source
line is shown as `?`, for example `location config/pins.toml:?`; no line is inferred.
The `; declaration: "IDENTITY"` suffix uses the existing JSON `declaration`
value quoted as a JSON string, escaping quotes and non-ASCII characters.
Runtime-file and Docker drift rows use the same suffix. Human check rows retain
selection order; JSON and plan outputs retain their existing sorted order.
Matches, mise source rows and skipped declarations are absent from this drift
summary. A clean check still prints only `All evaluated declarations match.`;
a blocked check prints only findings on stderr and exits 2, suppressing all
drift rows. Neither check form writes files.

For blocked human `check`, these finding/declaration pairs can append the
existing mise source reference/location and JSON-quoted declaration identity:

| Finding code | Existing blocked declaration kind |
| --- | --- |
| `unsupported_action_pin` | `actions-version` |
| `unsupported_pin` | `mise` (value only), `version-file` |
| `missing_pin` | `actions-version`, `version-file`, `docker-image` |
| `digest_pinned`, `dynamic_image`, `unsupported_image` | `docker-image` |

Exactly one eligible blocked row must have the finding's exact path and equal
non-null line. Multiple rows remain ambiguous even with identical labels.

For `unsupported_pin` and `unsupported_action_pin` findings with exactly one such
association, the human line also ends with `; value: JSON_QUOTED_OLD_VALUE` when
that row's existing `oldValue` is a string. This is the existing rejected value
string, such as `"<missing>"` for a file-only Actions input, an empty string, or
the existing diagnostic text for a non-string `[tools]` value. It is never read
from an indirect file, reconstructed from raw syntax or inferred from a typed
source value. A `mise` row gets only the value suffix, with no source or identity
suffix. `missing_pin` and Docker findings never get a value.

```text
refusal.yml:7: unsupported_action_pin: node-version-file indirection is unsupported; no version files are followed (source mise.toml:[tools].node; location mise.toml:2); declaration: "jobs.test.steps[1] actions/setup-node@v4 with.node-version-file"; value: "<missing>"
.node-version:1: unsupported_pin: expected one exact major.minor.patch line (optional v prefix, whitespace and trailing # comment) (source mise.toml:[tools].node; location mise.toml:2); declaration: ".node-version"; value: "latest"
Dockerfile:1: digest_pinned: digest-pinned images cannot be rewritten automatically (source mise.toml:[tools].node; location mise.toml:2); declaration: "FROM node@sha256:abc"
```

The finding location, code, reason and selection order stay intact. Actions
actual-version/file-only/missing-input label precedence is reused. Mise source
findings, structural/file errors, absent locations, and zero or multiple eligible
rows retain their existing text. A structural finding such as `duplicate_stage`
stays unadorned even beside an eligible blocked image row. Source reference,
path and line come only from the associated row's existing fields. An unknown
source line uses `?`, including an absent mise pin: `source mise.toml:[tools].go;
location mise.toml:?`. If source reference or path is absent, only the existing
identity suffix is shown. Unknown/dynamic image repositories with no established
tool therefore get their existing FROM identity without invented mise provenance.
An unsupported runtime filename has no declaration and gains no metadata.
Plan and apply diagnostics keep their existing text. JSON and plan explanations
are unchanged.

JSON contains `status` (`clean`, `drift`, `blocked`), `mismatch`, `declarations`,
`findings`, and planned `changedFiles`. Each declaration has `declaration`,
`path`, `line`, `source` (`mise.toml:[tools].node`, for example),
`tool`, `kind`, `expected`, `actual`, `mismatch`, `evaluated`. Versions exclude
the image suffix / optional file `v`. A missing or unsupported runtime pin is not
evaluated and blocks the operation. Unknown Actions types are skipped without
blocking; they never count as a runtime comparison. Findings include file paths, codes and
line numbers where available; malformed TOML includes the parser location.
Argument syntax errors use argparse's stderr/exit 2 rather than JSON.

### Explicit GitHub Actions workflow inputs

For a synthetic project with `[tools].node = "22.14.0"`, this step under
`jobs.test.steps`:

```yaml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4 # skipped, unknown action type
      - uses: actions/setup-node@v4 # action reference stays unchanged
        with:
          node-version: '20.11.0' # keep this comment
```

is planned as `node-version: '22.14.0' # keep this comment`. Save it as
`.github/workflows/ci.yml` in your project and select that exact path:

```sh
stackwrench pins check --root /path/to/project --workflow .github/workflows/ci.yml --json
# Exit 1 for the example drift; exit 2 if a selected input is unsafe.
stackwrench pins plan --root /path/to/project --workflow .github/workflows/ci.yml
stackwrench pins plan --root /path/to/project --workflow .github/workflows/ci.yml --json
# After reviewing the diff:
stackwrench pins apply --root /path/to/project --workflow .github/workflows/ci.yml
```

The literal, case-sensitive action repositories `actions/setup-node@REF`,
`actions/setup-python@REF`, and `actions/setup-go@REF` map to the mise keys
`node`, `python`, and `go`, and to `with.node-version`, `with.python-version`,
and `with.go-version`, respectively. `REF` is a nonempty literal starting with
an ASCII letter/digit and containing only ASCII letters/digits, `.`, `_`, `/`,
or `-` (for example, `v4` or a commit SHA). No action metadata or ref is fetched,
validated for existence, or changed. The step's `with` may precede `uses` and
may be a block mapping or a flat single-line inline mapping, for example:

```yaml
on: [push, pull_request]
jobs:
  test:
    steps:
      - uses: actions/setup-node@v4
        with: {node-version: '20.11.0', cache: npm} # keep
```

With mise Node `22.14.0`, check exits 1 and plan replaces only `20.11.0` with
`22.14.0` inside the existing quotes; braces, cache input and comment remain.
The cache input may also contain quoted expression text, for example
`with: {node-version: '20.11.0', cache: '${{ inputs.cache }}'} # keep`.
The expression text is retained literally and never evaluated. An expression
in `node-version` itself remains unevaluated and blocks the entire batch.
Unrelated flat lists may also retain quoted expression text, for example
`metadata: ['${{ inputs.note }}', "uses: actions/setup-go@v5"]`. Check still
reports only the actual Node input; plan/apply leave every list byte untouched.

Only a plain or simply quoted ASCII `major.minor.patch` value participates in
comparison. Only its version span is edited. Quotes, whitespace, comments,
line endings, final-newline presence and unrelated YAML remain byte-for-byte.

Version expressions (including `${{ matrix.node }}`), ranges, aliases,
prereleases, `v` prefixes, lists, block strings, missing version inputs, and any
corresponding `*-version-file` input are unevaluated and block **all** selected
edits (exit 2). No matrix expansion, environment evaluation or file indirection
is attempted. Literal exact step inputs can be compared even in a matrix job;
versions inside the matrix definition itself are never scanned or rewritten.
An absent matching mise pin also blocks the batch. Other literal or dynamic
`uses` values and reusable-workflow jobs are `actions-other`, `evaluated: false`,
`status: skipped`, with no proposed replacement. This allows checkout and other
unrelated steps to coexist with the three supported setup actions.
Reusable-workflow jobs without `steps` must also have a single-line scalar
`uses` value. Literal and dynamic scalars are skipped without resolution;
flow lists, block sequences/mappings and literal/folded block strings in that
position produce a located `unsupported_workflow` finding and block all edits.

To retain the zero-dependency/offline contract, workflows use a deliberately
restricted block-YAML reader, not a general YAML loader or Actions validator:

* One root block mapping, a `jobs` block mapping, literal job keys, and block
  `steps` sequences. Child collections must be indented more than their parent;
  indentless sequences are refused. Sequence entries use `- key: value` (one
  separating space) or a standalone `-` with an indented child. Spaces indent
  collections; nesting is limited to 64 levels.
* Keys are ASCII identifiers starting with a letter/underscore and continuing
  with letters, digits, underscores or hyphens, optionally simply quoted.
  Scalars are single-line plain or single/double quoted without escapes.
  Literal/folded `|`/`>` bodies (optional `+`/`-` chomping) are opaque and retained;
  setup-looking text inside `run`, comments, environment or unrelated mappings
  cannot establish a version declaration.
* Flat single-line flow lists such as `on: [push, pull_request]` are validated
  and retained as opaque values. Items are strings: plain items match
  `[A-Za-z0-9_][A-Za-z0-9_./@-]*`; single/double quoted items allow literal
  punctuation (including commas, brackets, colons and `#`), without escapes or
  an embedded matching quote. Horizontal whitespace, empty lists and a single
  trailing comma are allowed; a trailing comment requires separating whitespace.
  Quoted items may contain `${{` as opaque text; expression syntax is neither
  parsed nor validated (even an incomplete expression stays literal).
  Missing/repeated commas, unclosed lists, nested collections, escapes and
  unquoted expressions block the batch with a located `unsupported_workflow` finding. Event
  names are not checked against GitHub's schema. Lists are never expanded,
  implicitly typed, or searched for setup inputs, even when strings look like
  setup declarations. A well-formed setup `*-version: [X.Y.Z]` remains
  unevaluated with `unsupported_action_pin` and blocks all selected edits;
  lists cannot replace required block `jobs`/`steps`, an action `with` mapping,
  or a literal `uses` scalar.
* Flat single-line flow mappings such as the inline `with` example above are
  validated as `{key: value, ...}`. Keys use the same optionally quoted ASCII
  identifiers as block mappings; the colon must be followed by a space/tab,
  even for quoted keys/values. Values use exactly the simple string atom grammar
  of flat lists above, including opaque `${{` text inside quoted values.
  Expression syntax is not parsed or validated. Horizontal
  whitespace, empty mappings and a single trailing comma are allowed; trailing
  comments require separating whitespace.
  Quoted values can contain literal commas, braces, colons and `#`. Duplicate
  keys (including differently quoted spellings), missing/repeated commas,
  missing values, nesting (including list-valued inputs), escapes, multiline
  text and unquoted expressions produce located `unsupported_workflow` findings.
  Only an actual literal supported setup step's `with` mapping is inspected
  for runtime inputs. Other flat mappings remain opaque and cannot establish
  declarations; they cannot replace block workflow/job/step mappings or scalar
  `uses`. Empty/missing/non-exact setup versions and corresponding version-file
  keys remain `unsupported_action_pin`, with no replacement and no partial diff.
  Quoted expressions in a setup version input also produce this located
  diagnostic; quoted expressions in unrelated map fields cannot establish pins.
* Nested flow collections, YAML anchors/aliases/merges/tags, duplicate keys,
  multiline lists/mappings/plain/quoted scalars,
  explicit block indentation indicators, document markers/directives and tabs
  in collection indentation are unsupported. They produce an unevaluated
  `workflow` declaration with a located finding and block the entire batch;
  no partial diff is offered. Use forms within this explicit subset.

`actions-version` declarations carry the setup input's physical line, job/step
identity, mise source line, normalized old/new versions and safety reason in
check/plan JSON. Human-readable explanations show the line, source, old/new
values, safety reason and existing declaration identity. Unknown action rows use
the `uses` line.
Refusal labels and locations prefer an actual version node, even when a
corresponding version-file input is also present: its label remains
`with.node-version` (or the Python/Go key). If the version node is absent, a
corresponding `*-version-file` node supplies both its file-input label and line;
if both inputs are absent, the expected version-input label and `uses` line are
retained. Thus a block `node-version-file: '${{ inputs.file }}'` on line 8
with no `node-version` reports `unsupported_action_pin` and its unevaluated row
on line 8. The check/plan JSON declaration ends in `with.node-version-file`,
with `actual: "<missing>"`, `oldValue: "<missing>"`, `evaluated: false` and
`newValue: null`. The file input never
supplies a version and is never followed. This also applies to inline `with`
and to Python/Go. A bare `with:` remains an unsupported mapping-shape error.
Workflow files use the same path, size, UTF-8, identity revalidation and atomic
replacement safeguards as the other selected inputs. Workflows and their run
steps are never executed; the CLI never contacts GitHub or any network service.

### Explain a drift plan

Use `stackwrench pins plan --root examples/pins/project --explain` to review every
declaration, including matches and named Docker stages. Use `--json` instead
for machine-readable output. The two flags are mutually exclusive; plain
`plan` continues to emit only the unified diff.

Every declaration row ends with `; declaration: "IDENTITY"`, using the existing
JSON `declaration` value, quoted as a JSON string (including escaped quotes and
non-ASCII characters). This includes match, drift, blocked, skipped, mise,
Docker, runtime-file and structural workflow rows. Finding rows and the final
batch summary retain their format. For the file-only Node example above, saved
as `refusal.yml`, mise Node on line 2 and the input on line 8, the row is:

```text
refusal.yml:8: blocked: "<missing>" -> null (source mise.toml:2 [tools].node); node-version-file indirection is unsupported; no version files are followed; declaration: "jobs.test.steps[0] actions/setup-node@v4 with.node-version-file"
```

The identity follows existing input precedence: actual version node, otherwise
corresponding file-input node, otherwise expected version key at `uses`. It
adds no comparisons or file indirection. Explanations remain read-only, and a
blocked batch still exits 2 with no applicable diff or writes.

For a synthetic project with this `mise.toml`:

```toml
[tools]
node = "22.14.0"
python = "3.13.2"
```

and this `Dockerfile`:

```dockerfile
# synthetic
FROM node:20.11.0-alpine AS build
FROM python:3 AS py
FROM build AS final
FROM scratch
```

plus `.nvmrc` containing `22.14.0` and `.python-version` containing `^3.13`,
the explanation identifies `.nvmrc:1` as `match`, `Dockerfile:2` as `drift`
(`20.11.0` → `22.14.0`, sourced from `mise.toml:2`), `Dockerfile:3` and
`.python-version:1` as `blocked`, and Dockerfile lines 4–5 as `skipped`.
It never infers a Python version from `3` or `^3.13`. The exact mise Python
pin is still shown as context, with its source line, but no replacement is
proposed for either unsafe declaration.

The explanation adds these fields to declarations in both check and plan JSON:

* `status`: `match`, `drift`, `blocked`, or `skipped`; `reason` explains why.
* `sourcePath` and `sourceLine`: the selected mise file and the tool key's
  physical line. Quoted keys, dotted keys and inline tables are supported;
  text inside comments, strings and tasks cannot supply a source location.
  A missing/unreadable/unparseable source has no inferred line (`null` in
  JSON, `?` in terminal output). Stages have no mise source.
* `oldValue` and `newValue`: normalized version strings for exact declarations;
  image suffixes and optional runtime-file `v` prefixes remain in the original
  declaration/file. Rejected images retain the literal image reference and
  rejected runtime files retain their text without final newlines. Non-string
  mise values are rendered as diagnostic text. `newValue` is `null` when
  rewriting that declaration is unsafe or no comparison applies; `expected`
  can still show the exact mise pin as context.

Plan JSON also includes `files` (each with `path`, `declarations`, `findings`, `diff`),
`applicable`, and `diff`. Files, declarations, findings and diff files are sorted
by root-relative path, then physical line and declaration/code where relevant,
independent of selection argument order. Only existing optional defaults appear;
explicit missing files appear with their findings.

Each file's `diff` is a unified diff fragment with `a/PATH` and `b/PATH`
headers and up to three context lines on each side of a change. Unchanged files,
including the mise source and files containing only skipped declarations, have
`diff: ""`. Concatenating
`files[].diff` in the reported order equals the top-level `diff` and plain
`plan` output. Diff headers and hunk headers use LF; content lines retain their
original LF/CRLF endings. Lines without a final newline get the standard
`\ No newline at end of file` marker on a separate LF-terminated line, including
unchanged context at EOF. JSON escapes these line endings; decoded strings
retain them. For example, a drifting `.node-version` containing `20.11.0`
without a newline has this fragment:

```diff
--- a/.node-version
+++ b/.node-version
@@ -1 +1 @@
-20.11.0
\ No newline at end of file
+22.14.0
\ No newline at end of file
```

Any finding makes the entire batch inapplicable: explanation modes exit 2 but
still print the diagnostic report to stdout, with `applicable: false`,
`changedFiles: []`, top-level `diff: ""` and `diff: ""` on **every** file entry
in plan JSON, even otherwise valid drifting files. Exact rows may show candidate
new values, but none can be applied while the batch is blocked. Ordinary plan
still emits only errors to stderr and no partial diff. Check JSON's
`changedFiles` retains its existing meaning of candidate files even when blocked.
Check, plain plan and both explanation modes never write or stage files.

Every input is read and parsed before apply writes anything. Any finding
blocks the whole batch, including otherwise valid edits. On error, `plan`
emits no partial diff. Apply stages files and rollback copies in the same
directories, verifies input identities and bytes again, then uses atomic
per-file replacement. Mode bits and unchanged bytes are retained; inode,
mtime, owner, ACLs and extended attributes are not retained for edited files.
Unchanged files are not replaced. A second apply is a no-op.

## Limits and relationship to other tools

This is a narrow reconciler, not a complete Dockerfile validator or universal
version solver. Dockerfile heredocs (conservatively any `<<` outside comments),
non-default escape directives and continued FROM instructions are refused.
Non-FROM instructions are preserved without validation; their backslash
continuations are skipped. Registry-qualified names, floating image tags,
digest rewrites, complex mise objects, unknown tools, prereleases and dynamic
image tags require manual review. A three-component tag plus suffix does not
prove an image exists; the suffix can carry distribution or prerelease meaning
that this tool does not resolve. Review the diff before applying.

The implementation targets local Linux/POSIX filesystems. Do not run concurrent
editors/writers during apply. Revalidation reduces races, but replacement across
multiple files is not an ACID transaction. I/O failures trigger best-effort
rollback; a crash/power failure or rollback failure can leave partial changes.
There is no durability guarantee for directory entries. Keep normal version
control/backups. The tool never treats file contents as executable instructions.

[mise](https://github.com/jdx/mise) already manages runtimes and generates
configuration. [samever](https://github.com/Guflly/samever) already audits
Node/Python versions across many formats. This independent tool adds a deliberately
narrow, reviewed edit flow across exact mise pins, image tags, and runtime files,
including Go. It makes no novelty or universal compatibility claim and is not
an upstream mise extension. Compose spec is an inherited upstream reference;
this version does not parse Compose, general YAML/Actions syntax, package ranges
or `mise.lock`. Actions support is limited to the explicit subset above.
See [THIRD_PARTY.md](../THIRD_PARTY.md) for
attribution. Referenced projects retain their original licenses.
