# Demo repository for `stackwrench check`

Everything here is **synthetic**: a tiny Node app with a Compose stack, a Dev
Container, mise pins, a Dockerfile, a Taskfile and a justfile, each with a
mistake planted on purpose. Nothing is ever started, built or run.

| File | Planted problem |
| --- | --- |
| `compose.yaml`, `.env`, `web.env` | `${API_URL}` only exists in `web.env`; `LOG_LEVEL: ""` hides `web.env`; `SENTRY_DSN:` is set nowhere; `FEATURE_FLAGS` in `.env` is unused; `web` waits on `db` being healthy, but `db` has no healthcheck |
| `infra/compose.yaml` + `compose.override.yaml` | the override adds a second published Redis port instead of replacing it; `start_interval` differs on podman-compose |
| `.devcontainer/devcontainer.json` | `build.dockerfile` is resolved from `.devcontainer/`, not the repo root; a bind mount source does not exist |
| `mise.toml`, `.nvmrc`, `Dockerfile` | mise pins node 22.14.0, the Dockerfile still builds on 20.11.1 |
| `Dockerfile` (no `.dockerignore`) | `node_modules/` and `.env` go to the builder; `COPY . .` before `npm install` |
| `Taskfile.yml` | a `sources` glob matches nothing; `{{.VERSION}}` is not a source; outputs without inputs |
| `justfile` | `deploy` depends on a misspelled recipe |

```sh
cd examples/check
stackwrench check
```

`tests/check/test_cli.py::test_readme_demo_output` pins the key lines of the output.
