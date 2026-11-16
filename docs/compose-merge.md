# stackwrench compose-merge

`stackwrench compose-merge` (formerly `compose-merge-trace`) explains, offline, how an
**ordered, literal subset of Compose YAML** is merged. `render` produces stable JSON;
`explain --path` gives the per-file history of one JSON Pointer, with old values, new
values and file/line/column provenance. `compare` compares two explicit orders of the
same set of files and reports which final fields change, together with the file order of
the declarations involved. `history-paths` lists every JSON Pointer that has history, in
first-event order, including deleted paths. `trace` exports the chronological events of
the whole project across paths, keeping repeated paths, old values, new values and
sources. Use it when reviewing why a stack of overrides changed or kept some setting.
It reads only the files you name and, when explicitly requested, a pointer from stdin. It
does not modify inputs, execute commands, read environment variables, read secret/env
files, or connect to a container runtime.

This is an independent implementation, not a fork of Docker Compose and not a complete
Compose engine. The output is a merged view that keeps the authored literals; it is
**not Compose canonical config**: no implicit networks are added, host paths are not
resolved and no runtime project validation happens. Only the subset documented here is
accepted; unsupported fields or semantics are rejected with exit code 2, stdout stays
empty, and no "best effort" configuration is printed.

## Install

Requires Python 3.12 or newer. The only runtime dependency is PyYAML.

```sh
pipx install stackwrench              # or: pip install stackwrench
stackwrench compose-merge --version   # prints the bare stackwrench version

# Or run without installing:
uvx stackwrench compose-merge --help
```

`python -m stackwrench compose-merge` is equivalent to `stackwrench compose-merge`.
Subcommands: `render`, `explain`, `history-paths`, `trace`, `compare`. The examples
below use files from a stackwrench source checkout; run them from its root.

## Complete runnable examples

All examples in `examples/compose_merge/` are synthetic; `example.invalid` image
references are only text and are never pulled.

```bash
stackwrench compose-merge render -f examples/compose_merge/base.yaml -f examples/compose_merge/override.yaml
stackwrench compose-merge explain -f examples/compose_merge/base.yaml -f examples/compose_merge/override.yaml \
  --path /services/web/command
stackwrench compose-merge explain -f examples/compose_merge/base.yaml -f examples/compose_merge/override.yaml \
  --path /services/web/volumes/0/source
stackwrench compose-merge explain -f examples/compose_merge/base.yaml -f examples/compose_merge/override.yaml \
  --path /services/web/environment/REMOVE
stackwrench compose-merge render -f examples/compose_merge/base.yaml -f examples/compose_merge/override.yaml \
  -f examples/compose_merge/tags.yaml
```

The complete expected result for the first two files is
[`examples/compose_merge/merged.json`](../examples/compose_merge/merged.json); the result
with the tags file appended is
[`examples/compose_merge/tagged.json`](../examples/compose_merge/tagged.json). `command`
is **replaced** from `[echo, old]` to `[echo, new]`, not concatenated; the explanation
events include both base line 5 and override line 3. The source of the old `/cache` mount
becomes `fresh`; the history of environment variable `REMOVE` can still be queried, but
`present` is false.

Error examples must exit non-zero and never print a partial configuration:

```bash
python - <<'PY'
import subprocess
p = subprocess.run([
    'stackwrench', 'compose-merge', 'render', '-f', 'examples/compose_merge/unsupported.yaml'
], capture_output=True, text=True)
assert p.returncode == 2 and p.stdout == ''
assert 'interpolation' in p.stderr
print(p.stderr.strip())
PY
```

It can also be used as a Python library; failures raise `MergeError`:

```python
from stackwrench.compose_merge import merge_files
project = merge_files(['examples/compose_merge/base.yaml', 'examples/compose_merge/override.yaml'])
assert project.render()['services']['web']['command'] == ['echo', 'new']
history = project.explain('/services/web/command')
assert history['events'][-1]['previous'] == ['echo', 'old']
```

### Bounded batch explanations in Python

`Project.explain_many(paths)` accepts a concrete `list` of **1..64 strings** and returns
`{"explanations": [...]}`; each entry is exactly what `project.explain(path)` returns,
preserving input order, duplicates, exact values, history and sources. The method only
queries the already merged Project: it does not read files/stdin, decode JSON, or
consume arbitrary iterators. All three CLI batch transports use this same public API
after their own decoding.

```python
from stackwrench.compose_merge import merge_files
project = merge_files(['examples/compose_merge/nul-key.yaml'])
paths = ['/x-data/\0', '']
batch = project.explain_many(paths)
assert batch == {'explanations': [project.explain(path) for path in paths]}
assert batch['explanations'][0]['value'] == 'old'
assert batch['explanations'][1]['value'] == {'x-data': {'\0': 'old'}}
```

An empty list, 65 entries (duplicates count), non-string members, and non-list inputs
such as str/bytes/mappings/tuples/generators all raise `MergeError`; the shape and count
of the whole list are checked before any explanation runs. Any invalid RFC 6901 pointer
or a pointer that never appeared also makes the whole batch raise `MergeError`; no
partial result is returned. Neither success nor failure modifies the input list or the
Project; return values, sources and events are independent snapshots, and duplicate
queries do not share mutable results. The single-path `explain` contract is unchanged.
The Python API adds no JSON byte budget or global budget; the three CLI batch transports
each keep their 8 MiB encoded-input limit. The 64-entry cap is not an output-size or
runtime guarantee.

### History path discovery in Python

`Project.history_paths()` returns an independent list of strings, deduplicated in the
order of the **first recorded event**, including the root path `''` and deleted paths.
A path that is deleted and re-added is not listed twice and keeps its original position;
a `Project()` with no inputs returns `[]`, and after adding an empty mapping it returns
`['']`. RFC 6901 tokens are not sorted, decoded or normalized: escapes of `/` and `~`,
the difference between a real NUL and a literal backslash escape, Unicode, empty keys and
very long numeric keys are preserved. The method does not read files/stdin, decode JSON,
perform lookup/merge, or modify the Project; mutating the returned list does not change
later results.

```python
from stackwrench.compose_merge import Project, merge_files
assert Project().history_paths() == []
project = merge_files(['examples/compose_merge/nul-key.yaml', 'examples/compose_merge/type-change-next.yaml'])
paths = project.history_paths()
assert paths == ['/x-data/\0', '/x-data', '', '/x-data/0']
assert project.explain(paths[0])['present'] is False  # the old NUL key still has history
for start in range(0, len(paths), 64):
    chunk = paths[start:start + 64]
    assert project.explain_many(chunk) == {
        'explanations': [project.explain(path) for path in chunk]
    }
```

Every discovered path can be queried with single-path `explain`; pointers that were never
recorded are not listed and still fail when queried. Discovery can return more than 64
entries; split it into consecutive non-empty batches as above, since `explain_many` keeps
its 1..64 limit. This API adds no limits and makes no output-size or runtime promise.

### History path discovery on the CLI

`history-paths` requires files given explicitly with repeatable `-f` / `--file`; it
merges them once in the given order and prints one JSON array, identical to
`Project.history_paths()`. It does not sort or truncate to 64 entries; it includes the
root `""`, deleted/re-added paths, and paths that never existed but recorded a reset; a
repeated path appears once, at its first event. Escapes, real NUL vs. literal backslash,
Unicode, empty keys and numeric keys are preserved exactly.

```bash
stackwrench compose-merge history-paths -f examples/compose_merge/nul-key.yaml -f examples/compose_merge/type-change-next.yaml
# JSON array: ["/x-data/\u0000", "/x-data", "", "/x-data/0"]

python - <<'PY'
import json
import subprocess
from stackwrench.compose_merge import merge_files
files = ['examples/compose_merge/nul-key.yaml', 'examples/compose_merge/type-change-next.yaml']
result = subprocess.run([
    'stackwrench', 'compose-merge', 'history-paths',
    *[arg for file in files for arg in ('-f', file)],
], capture_output=True, text=True, check=True)
assert result.stderr == ''
paths = json.loads(result.stdout)
assert paths == ['/x-data/\0', '/x-data', '', '/x-data/0']
project = merge_files(files)
assert paths == project.history_paths()
for start in range(0, len(paths), 64):
    chunk = paths[start:start + 64]
    assert project.explain_many(chunk) == {
        'explanations': [project.explain(path) for path in chunk]
    }
print('PASS: CLI discovery equals Python; removed NUL path remains discoverable')
PY
```

An explicit empty-mapping file `{}` returns `[""]`; a missing `-f` exits 2. The command
has no pointer/JSON input options, does not read stdin, does not guess default Compose
files, and `-` is just a file name. It does not read the environment or a container
runtime and does not modify inputs. Unknown arguments and file read, parse, merge or
budget failures all exit 2 with empty stdout; no partial array is printed. The 32-file
limit, per-file/total byte and node budgets, depth and event budgets are unchanged.

### Complete chronological trace on the CLI

`trace` requires files given explicitly with repeatable `-f` / `--file`; it merges them
once in the given order and returns `{"events": [...]}`, identical to
`{'events': merge_files(files).events}`. The array keeps the original `order` sequence
and repeated paths; each raw event keeps `path`, `action`, `source`, `previousSources`,
`previousPresent`, `previous`, `present`, `value` and `reason`. You can therefore see the
relative order of add/retain/merge/reset/override operations across different paths.
JSON object members use the CLI's stable key sorting; the event array is not sorted,
grouped by path, or deduplicated.

```bash
stackwrench compose-merge trace -f examples/compose_merge/nul-key.yaml -f examples/compose_merge/type-change-next.yaml

python - <<'PYCODE'
import json
import subprocess
from stackwrench.compose_merge import merge_files
files = ['examples/compose_merge/nul-key.yaml', 'examples/compose_merge/type-change-next.yaml']
result = subprocess.run([
    'stackwrench', 'compose-merge', 'trace',
    *[arg for file in files for arg in ('-f', file)],
], capture_output=True, text=True, check=True)
assert result.stderr == ''
trace = json.loads(result.stdout)
assert trace == {'events': merge_files(files).events}
events = trace['events']
assert [e['order'] for e in events] == list(range(1, 8))
assert [e['path'] for e in events] == [
    '/x-data/\0', '/x-data', '', '/x-data', '/x-data/\0', '/x-data/0', ''
]
removed = events[4]
assert (removed['previousPresent'], removed['previous'],
        removed['present'], removed['value']) == (True, 'old', False, None)
assert removed['previousSources'] == [dict(
    file=files[0], line=1, column=16, input=0)]
assert removed['source'] == dict(file=files[1], line=1, column=9, input=1)
print('PASS: complete chronological trace equals Python; exact removed NUL event')
PYCODE
```

An empty-mapping file `{}` produces one add event for the root path `""`; NUL is escaped
in JSON, and literal backslashes, Unicode, RFC 6901 escapes and very long numeric keys are
kept exactly. The independent expectation for all seven events is in
[`tests/compose_merge/test_cli_trace.py`](../tests/compose_merge/test_cli_trace.py). The
command only exports existing records; it does not reinterpret events or infer runtime
behaviour or a minimal causal set.

There are no pointer/JSON input, filter or sort options; it does not read stdin, guess
files, read the environment or a container runtime, or modify inputs, and `-` is a
literal file name. A missing `-f`, unknown arguments, and file/parse/merge/budget
failures all exit 2 with empty stdout and no partial events. The 32-file limit,
per-file/total byte and node budgets, depth and the 100,000-event budget are unchanged.
The format repeats subtree snapshots, so output can be large; there is no output-size or
runtime guarantee. render/explain/history-paths/compare keep their own output contracts.

### Nested tags inside a null base value

[`examples/compose_merge/null-base.yaml`](../examples/compose_merge/null-base.yaml)
declares `volumes.cache: null` and `x-data: null`;
[`examples/compose_merge/null-next.yaml`](../examples/compose_merge/null-next.yaml)
replaces them with mappings containing `!reset null`. The complete expected output is
[`examples/compose_merge/null-merged.json`](../examples/compose_merge/null-merged.json):
only `volumes.cache.labels.KEEP: "yes"` and `x-data.keep: live` remain; the reset payload
is not left in the result as a null value.

```bash
stackwrench compose-merge render -f examples/compose_merge/null-base.yaml -f examples/compose_merge/null-next.yaml
stackwrench compose-merge explain -f examples/compose_merge/null-base.yaml -f examples/compose_merge/null-next.yaml \
  --path /volumes/cache/labels/DROP
```

The second command returns `present: false`, empty `winningSources` and a `reset` event
whose source is `examples/compose_merge/null-next.yaml:4:13`. The parent path
`/volumes/cache` keeps the null declaration from base line 2 and the later `replace`; the
winning source of `KEEP` is next line 5. A null base has no old children, so the DROP
event's `previousPresent` is also false. `!reset {}` / `!reset []` and `!override` on
child mappings are evaluated the same way and record their own events. Querying a path
that never appeared still fails with exit code 2 and empty stdout; unknown tags,
interpolation and other unsupported input are still rejected before merging.

## Comparing two explicit file orders

`examples/compose_merge/order-one.yaml` and `order-two.yaml` declare
`services.web.image: example.invalid/app:one` / `example.invalid/app:two` respectively;
`order-labels.yaml` only declares `services.web.labels.stable: unchanged`.

```bash
stackwrench compose-merge compare \
  --left examples/compose_merge/order-one.yaml --left examples/compose_merge/order-labels.yaml \
  --left examples/compose_merge/order-two.yaml \
  --right examples/compose_merge/order-two.yaml --right examples/compose_merge/order-one.yaml \
  --right examples/compose_merge/order-labels.yaml
```

The complete expected output is
[`examples/compose_merge/order-comparison.json`](../examples/compose_merge/order-comparison.json):
`equivalent: false`, and `differences` contains **only** `/services/web/image`. The left
value is `example.invalid/app:two` and the right is `example.invalid/app:one`; each
side's `winningSources` points at line 3 of the winning file. The entry's
`decisiveOrder.left` is one, two (inputs 0 and 2) and right is two, one (inputs 0 and 1).
The unrelated labels file is not part of that field's order evidence. Moving only the
labels file without changing the relative order of one and two yields
`equivalent: true` with no differences:

```bash
stackwrench compose-merge compare \
  --left examples/compose_merge/order-one.yaml --left examples/compose_merge/order-labels.yaml \
  --left examples/compose_merge/order-two.yaml \
  --right examples/compose_merge/order-labels.yaml --right examples/compose_merge/order-one.yaml \
  --right examples/compose_merge/order-two.yaml
```

Each side must list the same set of files one by one with `--left` / `--right`; each file
appears once, with exactly the same path spelling. Default Compose files are not guessed
and permutations are not enumerated. The Python API is
`compare_files(left_files, right_files)`. In one comparison each file is read once and
both sides share the bytes read; each side is parsed, merged and subject to all existing
limits separately (at most 32 files per side, 4 MiB / 100,000 value nodes in total,
100,000 trace events, plus the per-file limits). If parsing, merging or a limit fails on
either side, the whole command exits 2 with empty stdout, and stderr names the side that
could not be evaluated; no partial report or equivalence verdict is printed. For example,
if a resource collision is allowed in one→two but the reverse order omits old options,
the comparison still fails.

The report compares this tool's literal final values; it is not a proof of full Compose
canonical equivalence. Equal values with different sources are not reported; mapping key
order does not matter, and bool/int/float types are distinguished. Mappings recurse down
to changed fields; added/removed subtrees are reported once at their boundary; command,
ordinary lists and lists inside `x-*` are reported as one ordered field. Service resource
lists are compared by unique key (see the next section), with permutation differences
reported separately. `present` distinguishes missing from explicit null, paths use
RFC 6901, and differences are sorted stably by path traversal.

Each entry contains, for both sides, `value`, `winningSources` and the path's
`operations` in evaluation order (action and source). `decisiveOrder` lists, in each
side's input order, the union of files involved in those operations and in the winning
child values, keeping 0-based input numbers, so you can review the concrete
override/append/reset order. It does not claim to be the minimal causal set obtained by
re-evaluating without arbitrary files. Only changed fields get evidence entries; moving
files without changing final values does not create source-difference noise. A
successful comparison exits 0 whether or not differences exist.

### Comparing resource subfields and permutations by unique key

```bash
stackwrench compose-merge compare \
  --left examples/compose_merge/resources-one.yaml --left examples/compose_merge/resources-two.yaml \
  --right examples/compose_merge/resources-two.yaml --right examples/compose_merge/resources-one.yaml
```

one's volumes are `[old:/cache, stable:/stable]`, two's are
`[stable:/stable, new:/cache]`. The complete output is
[`examples/compose_merge/resource-comparison.json`](../examples/compose_merge/resource-comparison.json).
`differences` has a single resource-field entry:

```json
{
  "path": "/services/web/volumes",
  "resourceKey": {"target": "/cache"},
  "fieldPath": "/source",
  "left": {"path": "/services/web/volumes/0/source", "present": true, "value": "new"},
  "right": {"path": "/services/web/volumes/1/source", "present": true, "value": "old"}
}
```

The snippet omits source evidence; the real output keeps `winningSources` and
`operations` on each side and `decisiveOrder` on the entry. `path` is the position of the
resource list, and `fieldPath` is the RFC 6901 path relative to the resource; each side's
`path` points at the actual array position in that side's final result, and is null for a
missing field/resource. Only changed subfields are listed; added/removed resources are
reported once at the entry boundary with `fieldPath: ""`. `resourceKey` uses the same
normalized unique key as merging: volumes/secrets/configs use target; ports use host_ip,
integer target, published and protocol, where omitted host_ip is `0.0.0.0` and omitted
published is `""`. Short and long forms share the normalized identity; a literal field
that is missing vs. explicitly set to its default can still differ. Entries are sorted
stably by unique key and subfield; `/` or `~` in a target is never spliced into a
fictitious array path.

The report always contains a `resourceOrderChanges` array. The example lists the volumes
permutation separately: left `identities` is `[{"target":"/cache"},{"target":"/stable"}]`
and right is the reverse; each side has list-level source and operation evidence plus
`decisiveOrder`. A permutation entry is listed only when the **relative order of
identities shared by both sides** changes; pure additions/removals are reported as field
differences. Permutation entries show the full identity sequence for context. A pure
permutation difference also returns `equivalent: false`, in which case `differences` can
be empty; `equivalent: true` requires both arrays to be empty. When the whole list or an
ancestor exists on only one side, it is reported at the ordinary subtree boundary without
expanding resources; so "missing" and an empty list also differ.

Resource operation evidence matches identities using each input's before/after list
snapshots, following deletion, re-adding and override moves, without mixing in other
resources that occupy the same array position; a cross-position operation from one
override is listed once. Winning sources still refer to the actual value token; operation
sources keep the original trace semantics, so an initial whole-list addition can point at
the list declaration rather than an individual field token. `explain` still explains by
array position; its history API is unchanged. All collision/not-evaluated gates remain:
if either side omits old options, changes the mount type, repeats a unique key, uses an
invalid tag/interpolation or exceeds a budget, the whole command exits 2 with empty
stdout and no partial resource report.

### Reset history inside an ancestor override

The input `x-data: !override {gone: !reset null, keep: live}` produces
`{"x-data": {"keep": "live"}}`. `explain --path /x-data/gone` returns `present: false`,
empty `winningSources` and a `reset` event pointing at **the reset itself**, even if the
path never existed before. If an old value existed, the event keeps its `previous` and
`previousSources`; the ancestor still records `override`. Deeper empty-container resets
and delete-then-re-add keep continuous history, and inputs are unchanged.

If an ancestor override contains an explicit nested `!override`, that operation and its
untagged children reference the nearest override tag; `winningSources` still references
the actual value token. A replayable input is:

```yaml
x-data: !override
  nested: !override
    gone: !reset null
    keep: live
```

```bash
stackwrench compose-merge render -f examples/compose_merge/override-provenance.yaml
stackwrench compose-merge explain -f examples/compose_merge/override-provenance.yaml --path /x-data/nested
stackwrench compose-merge explain -f examples/compose_merge/override-provenance.yaml --path /x-data/nested/gone
```

render gives `{"x-data": {"nested": {"keep": "live"}}}`; the source of nested's override
event is line 2 column 11, gone's reset is line 3 column 11, and the ancestor is still
line 1 column 9. When an existing value is deleted, `previous` and `previousSources` are
kept; omitting untagged old children is still an override deletion. Paths that do not
exist and were never declared still error; invalid tags, non-empty resets, tags inside
sequences and interpolation are still rejected with exit code 2 and empty stdout.

### Same-path history across mapping/list type replacement

[`examples/compose_merge/type-change-base.yaml`](../examples/compose_merge/type-change-base.yaml)
is `x-data: {"0": old}` and next is `x-data: !override [new]`. The render is
`{"x-data": ["new"]}`:

```bash
stackwrench compose-merge render -f examples/compose_merge/type-change-base.yaml -f examples/compose_merge/type-change-next.yaml
stackwrench compose-merge explain -f examples/compose_merge/type-change-base.yaml -f examples/compose_merge/type-change-next.yaml \
  --path /x-data/0
```

The path's history is `add old`, then **one** `override old → new`. The last event has
`previousPresent` true, `previous` `"old"`, and `previousSources` at base line 1 column
15; the event source is next line 1 column 9, and the winning value's source is column
20. The reverse list → mapping replacement also pairs children by the same JSON Pointer,
nested containers are handled recursively, and old values and sources of deleted children
are kept. Explicit nested override/reset still reference their own tags; untagged
children inherit the nearest operation source; parsed inputs are unchanged.

Only **identical path tokens** are paired: mapping key `"0"` equals index 0 and `"10"`
equals index 10; `"00"`, `"01"`, `"-0"` and similar stay distinct. `/`, `~` and empty keys
are still escaped per RFC 6901. A real cross-file delete followed by re-add still keeps
both operations; paths that never appeared still error. Non-null type changes still need
an explicit `!override`; this does not widen the accepted input.

Very long numeric keys can be queried too: even if a historical mapping key is 4301 `9`s,
after being deleted by a list override `explain` returns `present: false`, empty
`winningSources` and `add old → override deletion`, keeping the old value and source,
without an integer-conversion traceback. Token digits are bounded by the current list
length before index conversion; Python's global integer-conversion limit is not changed.
Very long YAML keys must use explicit `?` / `:` syntax to avoid YAML's implicit-key
length limit. Offline replay:

```bash
python - <<'PYCODE'
import json
from pathlib import Path
import subprocess
import tempfile

key = '9' * 4301
with tempfile.TemporaryDirectory() as directory:
    base, later = [Path(directory) / name for name in ('base.yaml', 'next.yaml')]
    base.write_text('x-data:\n  ? "' + key + '"\n  : old\n')
    later.write_text('x-data: !override [new]\n')
    result = subprocess.run(['stackwrench', 'compose-merge', 'explain', '-f', str(base),
                             '-f', str(later), '--path', '/x-data/' + key],
                            check=True, capture_output=True, text=True)
    info = json.loads(result.stdout)
    assert not info['present'] and info['winningSources'] == []
    assert [e['action'] for e in info['events']] == ['add', 'override']
    assert info['events'][-1]['previous'] == 'old'
    print('PASS: removed long numeric key retains history')
PYCODE
```

A valid YAML key may contain NUL (for example `x-data: {"\0": old}`). The Python API can
query it with a real NUL, but OS argv cannot carry NUL and `--path` does not decode a
literal `\u0000`; use one of the JSON pointer transports described below.

## Supported input

Each file must be one UTF-8 YAML mapping. String keys and JSON-typed values are
supported; booleans are only true/false (on/off/yes/no stay strings); numbers support
decimal, YAML octal/hex/binary and finite floats. Dates are not converted. Duplicate keys,
non-string keys, unknown YAML tags, YAML directives, anchors/aliases/`<<`, multiple
documents and invalid Unicode are rejected. Write octal permissions as `0444` or `0o444`;
port `1:20` is short-syntax string, not a YAML 1.1 sexagesimal number.

Fields are allow-listed; any unlisted non-`x-` field is an error, including valid but
unimplemented Compose fields. Each file may be an override fragment and the final result
may be a fragment too; `image` is not required and resource references are not validated.

| Location | Accepted fields and shapes |
| --- | --- |
| Top level | `name`, `version` strings; `services`, `volumes`, `networks`, `secrets`, `configs` mappings |
| Service strings | `image`, `container_name`, `hostname`, `domainname`, `user`, `working_dir`, `restart`, `stop_signal`, `stop_grace_period`, `network_mode`, `ipc`, `pid`, `pull_policy`, `platform` |
| Service booleans | `init`, `privileged`, `read_only`, `stdin_open`, `tty` |
| Service commands | `command`, `entrypoint`: string, list of strings or null; `healthcheck.test`: string or a list of strings starting with `CMD` / `CMD-SHELL` / `NONE` |
| Service mappings | `environment`, `labels`: mappings with explicit string values only; lists of variable names and nulls that need host environment resolution are not accepted |
| Service plain lists | `dns`, `dns_search`: lists of strings; duplicates and `key=value` items are rejected |
| Service resource lists | `ports`, `volumes`, `secrets`, `configs`, see below |
| healthcheck | besides `test`: `interval`, `timeout`, `start_period`, `start_interval` strings, `retries` non-negative integer, `disable` boolean |
| Top-level volume definition | null or mapping: `name`, `driver` strings, `external` boolean, `driver_opts`, `labels` string-valued mappings |
| Top-level network definition | volume definition fields plus `internal`, `attachable`, `enable_ipv4`, `enable_ipv6` booleans |
| Top-level secret/config definition | null or mapping: `name`, `file` strings, `external` boolean. `file` is only text and is never read |
| `x-*` extensions | extension keys in the mappings above keep generic JSON data and merge by the generic rules; Compose fields inside extensions are not interpreted. Extension keys are not accepted inside resource list entries or mount options |

Short and long resource forms are first normalized to a comparable long form; synthesized
fields reference the source of the short entry or container node:

- **ports**: short forms `80`, `'8080:80'`, `'127.0.0.1:8080:80/udp'`; long-form fields
  `target`, `published`, `host_ip`, `protocol`, `mode`, `name`, `app_protocol`. target
  normalizes to an integer and published to a decimal string, port range 1–65535;
  defaults tcp/ingress. host_ip must be a canonical literal IP; IPv6 is accepted only in
  long form. The unique key uses host_ip (omitted is equivalent to `0.0.0.0`), target,
  published and protocol. Different quadruples stay distinct entries.
- **volumes**: short forms `'/cache'`, `'cache:/cache:ro'`, `'./data:/data:rw'`; long
  form `type`, `source`, `target`, `read_only`, `bind`, `volume`. type accepts only
  volume/bind, and the target must be a clean absolute POSIX path (`/` itself is not
  accepted in this subset). A bind source must explicitly start with `/`, `./` or `../`;
  short binds inject `bind.create_host_path: true`, long binds keep their explicit
  meaning. Bind options are the `create_host_path` boolean and a standard `propagation`
  string; volume options are the `nocopy` boolean and the `subpath` string. read_only
  defaults to false; entries are deduplicated by target.
- **secrets/configs**: short form is the source name; long form is `source`, `target`,
  `uid`, `gid`, `mode`. uid/gid must be strings and mode an integer 0–0777. The default
  secret target is `/run/secrets/<source>`; the default config target is `/<source>`.
  Explicit relative targets are kept as written, not made absolute. Unique by
  normalized target.

## Merge rules, history and rejection boundaries

- Mappings merge recursively; plain lists append in order; scalars take the later value.
  A null base value can be replaced by a later definition. command/entrypoint/test are
  replaced wholesale, including empty lists; an explicit null for command/entrypoint
  also replaces wholesale with null. Other allowed null override values do not change
  the existing declaration.
- Resource lists append new unique keys; an existing unique key keeps its position and
  merges as a mapping. **On collision, every field and nested option of the old entry
  must be restated**, otherwise it is an error; switching the mount type of the same
  target is also an error. This is a deliberate compatibility gate: the spec text and
  different Compose implementations disagree on keeping omitted old options, and the
  observed Compose v5.7.0 behaviour tends to keep the last whole resource entry.
  Restating the fields or using a whole-list `!override` removes the ambiguity. Old
  options are never silently dropped, and such inputs are never presented as verified.
- `!reset null` on mapping values and type-matched `!reset []` / `!reset {}` are
  supported; they clear the declaration and record deletion history for old child
  paths. Non-empty reset payloads, although allowed by the full spec, are explicitly
  rejected. `!override` replaces wholesale with a valid field value, bypassing merge
  rules. Tags are not accepted on the root node or anywhere inside a sequence; a reset
  inside a nested override is applied as part of the ancestor's wholesale replacement.
  Non-null type changes need an explicit `!override`, except for command fields.
- Duplicate resource unique keys within one file and identical plain-list entries across
  files are rejected; engine deduplication behaviour varies by version, and this tool
  does not pass off a literal append as Compose canonical in that case.
- Keys or strings containing `$` (including `$VAR`, `${...}`, `$$` and a literal `$`),
  `include`, `extends`, `env_file`, `build`, port ranges, IPv6 short form, Windows/home
  mounts, unsupported short mount modes and other unlisted semantics are rejected
  unconditionally. Content imported from outside the named files is never accessed.
- Full schema and runtime validation (whether resources exist, whether a configuration
  can run on a given platform, whether durations/image names are valid) is not
  implemented. Resource paths are not made absolute, files are not read, and project
  environment or profiles are not filled in.
- Limits: at most 32 files; per file 1 MiB / 50,000 value nodes / 64 levels of nesting;
  4 MiB / 100,000 value nodes in total; at most 100,000 trace events. Reaching a limit is
  an explicit error.

`--path` uses RFC 6901: `~1` means `/`, `~0` means `~`, and the empty string is the root.
Array indexes refer to positions in the merged result; when a position is replaced, the
old element's history is still recorded at that path, not treated as a cross-position
resource ID. A path that does not exist and never appeared returns an error; deleted
paths can be explained.

### Querying keys containing NUL with a JSON argument

`explain` requires exactly one of `--path`, `--path-json`, `--paths-json`,
`--path-json-file`, `--paths-json-file`, `--path-json-stdin` or `--paths-json-stdin`.
`--path` takes the pointer verbatim and does not interpret backslash escapes;
`--path-json` takes one **JSON string**, decodes it once, then performs the same RFC 6901
validation and history lookup. This lets you query keys containing NUL in valid YAML
without putting NUL itself into OS arguments. The key in
[`examples/compose_merge/nul-key.yaml`](../examples/compose_merge/nul-key.yaml) is a real
NUL:

```bash
stackwrench compose-merge explain -f examples/compose_merge/nul-key.yaml --path-json '"/x-data/\u0000"'
```

The result is `present: true`, `value: "old"`, and the single add event and the winning
source both point at `examples/compose_merge/nul-key.yaml:1:16`. `--path-json '""'`
queries the root; `~1` / `~0` are still interpreted per JSON Pointer rules. A real NUL and
a literal `\u0000` are different keys; the latter can be queried verbatim with
`--path '/x-data/\u0000'` or with the JSON argument `'"/x-data/\\u0000"'`. Delete and
re-add, nested/empty/Unicode keys, and very long canonical or non-canonical numeric keys
follow the same history and source semantics. Write very long YAML keys with explicit
`? "key"` / `: value` syntax to avoid YAML's implicit-key length limit.

Invalid or non-string JSON, invalid pointers, paths that never appeared, and missing or
conflicting path options all exit 2 with empty stdout and no traceback. JSON arguments
do not change merge rules or any input limit, nor Python's integer digit limit. Argument
length is still bounded by the OS: library history for a 140,000-digit key is correct,
but the corresponding JSON argument can hit `E2BIG` before the process starts.

### Reading a pointer from a bounded JSON file

`--path-json-file FILE` reads only the explicitly named local regular file, whose content
must be one UTF-8 JSON string (no BOM, JSON whitespace allowed); it is decoded once and
then follows the same validation and history lookup. The pointer never enters argv, so
it can query keys longer than the OS argument limit; NUL is still written as a JSON
escape.

```bash
stackwrench compose-merge explain -f examples/compose_merge/nul-key.yaml --path-json-file examples/compose_merge/nul-pointer.json
```

The result is identical to the `--path-json` example above.
[`examples/compose_merge/nul-pointer.json`](../examples/compose_merge/nul-pointer.json)
contains `"/x-data/\u0000"`; file content `""` queries the root. The file may be at most
**8 MiB (8,388,608 bytes)** including whitespace; at most the limit plus 1 byte is read,
and oversize input is rejected before decoding. This budget is independent of the 1 MiB
per-file YAML limit and does not relax any existing limit. Symlinks to regular files are
allowed; directories, devices and FIFOs are rejected, and a FIFO never waits for a
writer. `-` is a literal file name, not stdin. A missing or unreadable file, oversize
input, invalid UTF-8/JSON, non-string JSON, invalid pointers, paths that never appeared,
and option conflicts all exit 2 with empty stdout and no traceback. All input and pointer
files stay unchanged.

### Reading a JSON pointer explicitly from a pipe

`--path-json-stdin` reads one UTF-8 JSON string from stdin, decodes it once, and then
performs the same RFC 6901 validation and history lookup. All path options are mutually
exclusive; stdin is read only when this option is given explicitly and argument
validation has passed. Help, option conflicts, other path options, render and compare do
not read stdin.

```bash
printf '%s\n' '"/x-data/\u0000"' | stackwrench compose-merge explain -f examples/compose_merge/nul-key.yaml --path-json-stdin
```

The result matches the argument/file examples above: `present: true`, `value: "old"`,
source `examples/compose_merge/nul-key.yaml:1:16`, one add event, `previousPresent:
false`. Input `""` queries the root; real NUL, literal `\u0000`, Unicode, empty tokens and
very long numeric keys follow the same history and source semantics. The pointer never
enters argv and needs no temporary pointer file.

The budget is still **8 MiB (8,388,608 bytes, including JSON whitespace)**; at most the
limit plus 1 byte is read and oversize input is rejected before decoding. Input must be
strict UTF-8 (no BOM) and exactly one JSON string. Reading waits for EOF or the limit plus
1 byte; the producer should close the pipe; there is no read timeout or interactive
prompt. Empty input, read failure, oversize input, invalid encoding/JSON, non-string JSON,
invalid pointers, paths that never appeared and option conflicts all exit 2 with empty
stdout and no traceback. `--path-json-file` still accepts only regular files, `-` is
still a literal file name, and a piped `/dev/stdin` is still rejected by the file option.

### Batch queries from one JSON array

`--paths-json-stdin` explicitly reads one UTF-8 JSON array containing **1..64 strings**.
Each string is decoded once and follows the same RFC 6901 validation and history lookup.
Input files are merged once; the output is `{"explanations": [...]}`, in query order, and
duplicate queries keep separate entries. Each entry equals the full result of a single
query, including values, events, exact sources and delete/re-add history.

```bash
printf '%s\n' '["/x-data/\u0000", ""]' | stackwrench compose-merge explain -f examples/compose_merge/nul-key.yaml --paths-json-stdin
```

The result contains two explanations: the first has `path` `/x-data/\u0000` (a real NUL),
`present: true`, `value: "old"`, with winning source and single add event at
`examples/compose_merge/nul-key.yaml:1:16`; the second has `path: ""` and the full root
object as value. Empty tokens, Unicode, literal backslashes, very long numeric keys and
deleted history paths can be mixed.

This option is mutually exclusive with all other path options, and stdin is read only
after argument validation succeeds. The total budget is still **8 MiB** (including JSON
whitespace), strict UTF-8, no BOM, reading at most the limit plus 1 byte, waiting for EOF
or the limit plus 1 byte, with no read timeout. Exactly one array is required: JSON
Lines, empty arrays, more than 64 entries, or any non-string member are rejected, and
duplicates count toward 64. Any invalid or never-seen pointer, read failure, oversize
input, invalid encoding/JSON, conflict, or existing input-gate failure makes the whole
batch exit 2 with empty stdout and no traceback; successful parts are never printed
first. `--path-json-stdin` still accepts only one JSON string and rejects arrays. YAML and
event budgets are not relaxed and there is no output-size or runtime guarantee; up to 64
queries may repeat the full tree and history.

### Batch queries from one bounded JSON file

`--paths-json-file FILE` reads the same **1..64 string** array from an explicitly named
regular file. It is mutually exclusive with all other path options and shares the batch
stdin behaviour: single decode, single merge, order/duplicate preservation, full
history/sources and all-or-nothing failure. The example file
[`examples/compose_merge/nul-pointers.json`](../examples/compose_merge/nul-pointers.json)
contains `["/x-data/\u0000", ""]`:

```bash
stackwrench compose-merge explain -f examples/compose_merge/nul-key.yaml --paths-json-file examples/compose_merge/nul-pointers.json
```

The result is identical to the batch pipe above. The file must be strict UTF-8, no BOM,
exactly one JSON array; at most **8 MiB (8,388,608 bytes, including whitespace)**,
reading at most the limit plus 1 byte. Symlinks to regular files are allowed;
directories, devices, FIFOs and a piped `/dev/stdin` are rejected, and a FIFO never waits
for a writer. `-` is a literal file name and stdin is never read implicitly. Missing or
unreadable files, oversize input, invalid encoding/JSON, empty arrays, more than 64
entries, non-string members, any invalid/never-seen pointer, option conflicts and
existing input-gate failures all exit 2 with empty stdout and no traceback. Input and
query files stay unchanged. Single-path `--path-json-file` still accepts only a JSON
string and rejects arrays. File reads follow ordinary filesystem semantics; no atomic
snapshot is guaranteed under concurrent modification.

### Batch queries from an inline JSON argument

`--paths-json ARRAY` accepts the same **1..64 string** array directly from one argv
argument, without reading a pointer file or stdin. It is mutually exclusive with all
other path options and shares the batch file/pipe array validation, single decode, single
merge, order/duplicate preservation, full history/sources and all-or-nothing failure
semantics.

```bash
stackwrench compose-merge explain -f examples/compose_merge/nul-key.yaml --paths-json '["/x-data/\u0000", ""]'
```

The result is identical to the batch file/pipe examples above. Exactly one JSON array is
required, not JSON Lines; empty arrays, more than 64 entries (duplicates count),
non-string members, invalid JSON/UTF-8, any invalid or never-seen pointer, option
conflicts and existing input-gate failures all exit 2 with empty stdout and no
traceback. `--path-json` still accepts only one JSON string and rejects arrays.

The inline argument has its own budget of **8 MiB (8,388,608 UTF-8 encoded bytes,
including JSON whitespace)**, checked before JSON decoding, independent of YAML/event
budgets. **OS argv limits still apply**, and the argument that can actually be launched
may be much smaller than 8 MiB; use the batch file or stdin transport for very long
pointers.

## Output format

Single-path `explain` returns `path`, `present`, `value`, `winningSources`, `events`;
batch mode puts the same objects into `explanations`. Events are ordered by input, and
within one file by merge traversal order; `order` is the global event sequence number.
Each event has `action`, `source`, `previousSources`, `previousPresent`, `previous`,
`present`, `value` and `reason`. Actions are add / replace / append / merge / reset /
override / retain. `input` is the 0-based file index; lines and columns are 1-based and
positions refer to the value token. A container's winningSources include the last
structural edit and the sources of child values still kept; a single scalar's source is
its winning definition. null and "missing" are distinguished by `present`. Rendered keys
are sorted, lists keep merge order, and the output has no timestamps. stdout is JSON;
errors go only to stderr, prefixed `stackwrench compose-merge: error:`, with exit code 2.

## Relation to upstream and similar tools

The [Compose merge specification](https://raw.githubusercontent.com/compose-spec/compose-spec/main/13-merge.md)
is the behavioural reference; [Docker Compose](https://github.com/docker/compose) already
renders with `config`. [ComposeLens](https://github.com/Strukturpiloten/compose-lens)
already offers provenance-rich processing and broader semantic support. This command's
narrow purpose is chronological per-path explanation on the CLI, fail-closed subset gates
and bounded fixture replay; it does not claim to have invented provenance or to exceed
those tools in features or performance. No GPL podman-compose or MPL ComposeLens source
is copied.
