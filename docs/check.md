# stackwrench check

`stackwrench check` reads a repository and tells you **what will break its
local dev stack** before you run `docker compose up`, open the Dev Container or
start a task: Compose variables that are empty when you think they are set,
override files that add instead of replace, podman-compose differences, Dev
Container paths resolved from the wrong folder, runtime versions that drifted
from `mise.toml`, build contexts that ship `node_modules` and `.env`, COPY
orders that bust the cache, and Taskfile/justfile mistakes.

It is the front door to the other stackwrench commands. It does not reimplement
their analysis: each check calls the read-only Python API of one command and
turns the result into one report. It never writes a file, never executes
anything from the repository and never calls docker, podman, kind, just or Task.

```sh
uvx stackwrench check .                                     # or: pipx install stackwrench
stackwrench check path/to/repo -v                           # fix hint under each finding
stackwrench check . --format markdown >> "$GITHUB_STEP_SUMMARY"
stackwrench check . --format sarif > stackwrench.sarif
```

## Try it on the demo repository

[`examples/check/`](../examples/check/) is a small synthetic repository with
one planted mistake per tool (its README lists them).

```sh
cd examples/check
stackwrench check
```

It reports 10 files, 3 errors, 14 warnings and 4 info notices, among them:

```text
Compose
  compose.yaml  Compose file
    warn   compose-env/env-file-not-interpolated  API_URL: ${API_URL} is not set for interpolation (.env does not define it), so `environment: API_URL` becomes "" and hides API_URL from web.env line 2  line 12
  infra/compose.override.yaml  Compose file
    warn   compose-merge/ports-merged             services.redis.ports: infra/compose.override.yaml adds 6380:6379, and 6379:6379 from infra/compose.yaml:7 stays published: ...

Dev environments
  .devcontainer/devcontainer.json  Dev Container config
    error  devcontainer/missing-host-path         Missing file: .devcontainer/Dockerfile  line 6
  Dockerfile  Dockerfile
    warn   pins/drift                             node: `FROM node:20.11.1-alpine` uses node 20.11.1; mise.toml:3 pins 22.14.0  line 1
```

`tests/check/test_cli.py::test_readme_demo_output` pins these lines.

## What is discovered

`check` walks the repository (up to 6 levels, 50,000 entries, symlinked
folders not followed). Hidden folders are skipped except `.devcontainer/` and
`.github/workflows/`; so are `node_modules`, `vendor`, `venv`, `site-packages`,
`__pycache__`, `dist` and `target`.

| File | Recognised by |
| --- | --- |
| Compose files | `compose.yaml`, `compose.yml`, `docker-compose.yaml`, `docker-compose.yml`; their `*.override.*` file; `compose.<name>.yaml` variants |
| `.env` | a `.env` next to Compose files (the interpolation inputs Compose reads by default) |
| Dev Container config | `.devcontainer/devcontainer.json`, `.devcontainer/<name>/devcontainer.json`, `.devcontainer.json` |
| mise config | `mise.toml` or `.mise.toml` at the root; `.tool-versions` is noted but not compared |
| Version files | `.nvmrc`, `.node-version`, `.python-version`, `.go-version` |
| Dockerfiles | `Dockerfile`, `Containerfile`, `Dockerfile.*`, `*.Dockerfile` |
| Build contexts | every literal Compose `build:` (context + dockerfile); otherwise a `Dockerfile` builds from its own folder, and `Dockerfile.*` only at the root |
| Actions workflows | `.github/workflows/*.yml` that use `actions/setup-node`, `setup-python` or `setup-go` (only with a mise config) |
| justfile / Taskfile | `justfile`, `.justfile` (any case); `Taskfile.yml`, `Taskfile.yaml`, `Taskfile.dist.yml` |
| kind | YAML with `kind: Cluster` and `kind.x-k8s.io`; then Kubernetes workload YAML and kind node inventories (`schemaVersion: 1`, `cluster`, `nodes`) |

Interpolated (`${...}`) or remote build contexts are not guessed.

## Checks

`stackwrench check --list-checks` prints this list. All checks run by default;
a check with nothing to look at is listed under "Not applicable here".

| Area | Check | Uses | Reports |
| --- | --- | --- | --- |
| Compose | `compose-env` | `compose-env` (`trace`) | `env-file-not-interpolated` `${VAR}` unset for interpolation although `VAR` is in a service `env_file`; `unset-substituted-empty` `${VAR}` not in `.env`, substituted as `""`; `inherited-unset` `VAR:` / `- VAR` pass-through set nowhere; `empty-overrides-value` an explicit `""` hides an `env_file` value; `required-unset` `${VAR:?}` not set; `dotenv-unused` `.env` entries no Compose file uses |
| | `compose-merge` | `compose-merge` (`merge_files`, `Project.events`) | `ports-merged` an override adds ports instead of replacing them; `null-ignored` `null` removes nothing; `command-replaced`; `list-merged` volumes/secrets/configs merged by target; `reset`, `override` |
| | `podman-audit` | `podman-audit` (`load_document`, `audit`) | `healthy-gate-hang` `condition: service_healthy` (podman-compose#1541), `start-interval` (podman-compose#1500), `healthy-gate-without-healthcheck`, `missing-dependency`, `dependency-cycle`, `unverified-version` |
| Docker builds | `context-budget` | `context-budget` (`analyze`, ignore matcher) | `junk-included` `node_modules`, `.git`, venvs, caches in the context; `sensitive-file` `.env`, `.env.*`, keys and credential files in the context; `over-budget` with `--context-budget`; `size` |
| | `cache-replay` | `cache-replay` (Dockerfile parser, `copy_files`, `replay`) | `broad-copy-before-run` a `COPY .` (or a directory/glob) before a RUN: editing any copied file re-runs it; a warning when the RUN installs dependencies |
| Dev environments | `devcontainer` | `devcontainer` (`check`) | the tool's diagnostics: `missing-host-path`, `wrong-host-kind`, `missing-service`, `missing-volume`, `path-escape`, `workspace-uncovered`, ... |
| | `pins` | `pins` (`Root`, `inspect`) | `drift` a Dockerfile `FROM`, version file or `setup-*` input differs from `mise.toml`; `floating-tag` `node:22-alpine` cannot be compared; `missing-pin`, `not-exact`, `tools-not-compared`, `summary` |
| | `kind-images` | `kind-images` (`read_manifests`, `build_report`) | `always-pull` images `kind load` cannot help (policy Always, the default for `:latest`); with node inventories also `missing-local-image` and `would-pull`; otherwise `needs-snapshots` |
| Task runners | `just` | `just` (`analyze`) | `missing-dependency`, `dependency-cycle`, `duplicate-recipe`, `duplicate-variable`; `outside-subset` what the just→Task bridge cannot translate |
| | `task-audit` | `task-audit` (`audit`) | `sources-match-nothing` a `sources` glob matches no file; `input-not-in-sources` a task with sources whose commands read `{{.VAR}}` / `$VAR` no source covers; `outputs-without-inputs`, `no-source-inputs`, `partial` |

Rule ids are `check/rule`, for example `pins/drift`; the JSON and SARIF output
use the same ids. Every check also has a `not-analysed` info rule for input
outside what its tool reads, with the tool's (value-free) reason.

### Severities

- **error**: it is broken as written: a devcontainer.json path that does not
  exist, a justfile recipe that depends on a missing recipe, a `depends_on`
  naming no service, an image with `imagePullPolicy: Never` that is not loaded.
- **warning**: it works, but not the way you probably think: empty
  interpolation, values hidden by empty strings, ports that stay published,
  podman-compose differences, `.env` or `node_modules` sent to the builder,
  cache-busting COPYs, version drift, Taskfile fingerprint gaps.
- **info**: worth knowing: summaries, sizes, inputs a check does not read.

`--fail-on` (default `error`) picks the level that makes `check` exit 1.

### How the checks decide

- **compose-env** traces each Compose file on its own with the `.env` of its
  folder as the only interpolation input. Your shell environment is **never
  read**, so a variable your shell exports still shows as unset: the finding
  says so. Values are never printed. Files with YAML anchors, `extends` or
  `include` are outside the tool's subset (`not-analysed`).
- **compose-merge** merges the base file with its override in Compose's default
  order and reads the merge events. It accepts a literal subset of Compose (no
  `${...}`, `build`, `depends_on`, `env_file`); other files are `not-analysed`
  with the exact field and line.
- **podman-audit** only evaluates `depends_on` and `healthcheck`, so `check`
  hands it exactly those two fields of every service (other fields such as
  `build` would make the tool refuse the file). Evidence is version-scoped: the
  bundled catalog covers podman-compose 1.6.0; `--podman-compose-version`
  picks another version, whose findings are then marked unverified.
- **context-budget** lists each build context once, applying the
  Dockerfile-specific or root `.dockerignore`, and looks at what is included.
  The directory and file-name lists are fixed (see `docker.py`); a suggested
  pattern is checked with the tool's own ignore matcher.
- **cache-replay** parses the Dockerfile with the tool's linear COPY model,
  after blanking metadata-only instructions (`CMD`, `ENTRYPOINT`, `EXPOSE`,
  `LABEL`, `USER`, `HEALTHCHECK`, ...; line numbers are kept). The inventory is
  the context after `.dockerignore`. It replays one edited file and reports the
  RUN steps that would re-run. Multi-stage Dockerfiles and `ADD` are outside the
  model. Results are model units, not build seconds.
- **devcontainer** runs the wiring check with the repository as workspace root
  and a local daemon assumption. "Unknown" and "not evaluated" diagnostics are
  info.
- **pins** reads exact `major.minor.patch` pins for node, python and go from
  `mise.toml` and compares every discovered Dockerfile, version file and
  setup-* workflow input. Other images (postgres, nginx) are ignored.
- **kind-images** needs per-node `crictl images -o json` snapshots to say where
  an image is missing; `check` never contacts a cluster. Without an inventory
  it reports only what the manifests alone prove (Always pull policies).
- **just** reports problems that also stop just itself. A dependency on a
  recipe the bridge could not parse (parameters, attributes) is not reported as
  missing.
- **task-audit** audits the whole Taskfile, and task by task when one task
  fails (a `sources` glob that matches nothing). `$HOME`, `$PATH`, `$USER` and
  similar ambient variables are not reported as inputs.

## Output

### Text (default)

Findings are grouped by area, then by file, most severe first, and end with a
one-line summary:

```text
10 files · 3 errors · 14 warnings · 4 info
```

`-v` adds a fix hint under each finding. Colour is used on a terminal unless
`NO_COLOR` is set; force it with `--color always|never`.

### Markdown (`--format markdown`)

A summary line, then one table per area with errors and warnings and the info
notices folded into `<details>`. Cells are escaped and each area shows at most
40 rows, so it fits a PR comment or `$GITHUB_STEP_SUMMARY`.

### JSON (`--format json`)

```json
{
  "format": "stackwrench.check/v1",
  "tool": {"name": "stackwrench", "version": "0.1.0"},
  "repo": ".",
  "checks": ["compose-env", "compose-merge", "podman-audit", "..."],
  "skipped": [{"check": "kind-images", "reason": "no kind cluster config"}],
  "files": [{"path": "compose.yaml", "kind": "compose", "label": "Compose file", "findings": 5}],
  "findings": [{
    "check": "pins", "rule": "drift", "ruleId": "pins/drift", "severity": "warning",
    "area": "Dev environments", "message": "`FROM node:20.11.1-alpine` uses node 20.11.1; mise.toml:3 pins 22.14.0",
    "file": "Dockerfile", "line": 1, "pointer": null, "subject": "node", "hint": "..."
  }],
  "summary": {"files": 10, "scanned": 14, "error": 3, "warning": 14, "info": 4},
  "failOn": "error",
  "failed": true
}
```

### SARIF (`--format sarif`)

SARIF 2.1.0 for GitHub code scanning, with each finding on its file and line.

```yaml
- run: uvx stackwrench check . --format sarif > stackwrench.sarif
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: stackwrench.sarif
```

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | No finding at or above `--fail-on` |
| 1 | At least one finding at or above `--fail-on` |
| 2 | Usage error: unknown option or check, REPO is not a folder |

A check that crashes becomes one `internal` error finding (for that file, or
for the run); the other checks still run. Set `STACKWRENCH_CHECK_DEBUG=1` to
get the traceback instead. A command missing from the install becomes one
`unavailable` info finding.

## Library use

```python
from stackwrench.check import CheckOptions, discover, run_checks, select

discovery = discover(".")
outcome = run_checks(discovery, select(only=["compose-env", "pins"]), CheckOptions())
for finding in outcome.findings:
    print(finding.severity, finding.rule_id, finding.file, finding.line, finding.message)
```

## Limits

- `check` reads files; it does not know your shell environment, the images'
  own `ENV`/`HEALTHCHECK`, the Docker context in use or the state of a cluster.
- Each tool reads a strict, documented subset and refuses what it cannot
  account for completely (`not-analysed` says why). Many real Compose files are
  outside compose-merge's literal subset; render them with
  `docker compose config` and use `stackwrench compose-merge` directly.
- The directory and file-name lists of context-budget and the install-command
  pattern of cache-replay are fixed heuristics.
