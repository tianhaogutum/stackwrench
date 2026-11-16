# Third-party notices

stackwrench is MIT-licensed (see [LICENSE](LICENSE)). Each command started as a
standalone MIT tool by the same author; they were merged into this package and keep
the MIT license. This file lists runtime dependencies, ported code, bundled data and
the specifications each command follows. Full license texts are in [licenses/](licenses/).

## Runtime dependency

- **PyYAML** (MIT) — Compose, Taskfile, Kubernetes manifest and Dev Container YAML
  parsing. License: [licenses/PyYAML/LICENSE](licenses/PyYAML/LICENSE).

Test-only: pytest (MIT), jsonschema (MIT).

## Per command

### compose-env (formerly compose-env-trace)
Original MIT implementation. Behaviour follows the Compose Specification interpolation
and container-environment documents (Apache-2.0). No Docker Compose, ComposeLens
(MPL-2.0) or podman-compose (GPL) code is copied. All examples are synthetic.

### compose-merge (formerly compose-merge-trace)
Original MIT implementation. Merge behaviour follows the Compose Specification merge
rules (Apache-2.0) and was cross-checked by reading compose-go (Apache-2.0), but is
independently written: no compose-go, Docker Compose, podman-compose or ComposeLens
code is copied. Examples and fixtures are synthetic.

### podman-audit (formerly compose-podman-audit)
Original MIT code. The bundled `catalog.json` holds short, independently written
summaries and URLs of public podman-compose issues #1541, #1500 and #1388; no issue
text or podman-compose (GPL) / ComposeLens (MPL) / podlet source is copied. Compose
semantics follow the Compose Specification (Apache-2.0). Fixtures use `example.invalid`.

### context-budget (formerly docker-context-budget)
Original MIT code, standard library only. Behavioural references, none vendored:
Docker build-context documentation, moby/patternmatcher (Apache-2.0),
moby/buildkit #1181 (Apache-2.0).

### cache-replay (formerly docker-cache-replay)
Original MIT code, standard library only. Behavioural references: Docker build cache
invalidation documentation, moby/buildkit README and issue #2039 (Apache-2.0), the
Dockerfile reference. No upstream code is incorporated.

### devcontainer (formerly devcontainer-wirecheck, Node.js)
Python port of the original MIT tool.
- `src/stackwrench/devcontainer/jsonc.py` is a port of Microsoft **jsonc-parser** 3.3.1
  (MIT, https://github.com/microsoft/node-jsonc-parser).
  License: [licenses/devcontainer/jsonc-parser-LICENSE.md](licenses/devcontainer/jsonc-parser-LICENSE.md).
- YAML behaviour follows npm **yaml** 2.9.1 by Eemeli Aro (ISC,
  https://github.com/eemeli/yaml). License: [licenses/devcontainer/yaml-LICENSE](licenses/devcontainer/yaml-LICENSE).
- Format references (no code copied): devcontainers/cli (MIT), Dev Container
  specification (CC-BY-4.0), Compose Specification (Apache-2.0).
- Test-only: `tests/devcontainer/fixtures/sarif/` holds the unmodified OASIS SARIF 2.1.0
  schema under OASIS terms (LICENSE.md in that folder). It is not part of the wheel and
  is not relicensed.

### pins (formerly mise-pinbridge)
Original MIT code, standard library only (`tomllib`). Interoperability references, no
upstream code: jdx/mise (MIT) configuration format, the Dockerfile reference, the input
names of GitHub actions/setup-node, setup-python and setup-go. Fixtures are synthetic.

### kind-images (formerly kind-image-plan)
Original MIT code, tests and synthetic examples. Checked against the public kind and
Kubernetes images documentation (CC-BY-4.0, paraphrased) and the CRI `api.proto` /
crictl documentation (Apache-2.0). No upstream source is copied.

### just (formerly just-task-bridge)
Original MIT code, standard library only. Behaviour and format references: casey/just
(CC0-1.0) documentation and source, go-task/task (MIT, Copyright (c) 2016 Andrey Nering)
schema and guides. No runner or schema code is bundled; the optional live tests and
`verify` use preinstalled `just` and `task` binaries only.

### task-audit (formerly task-fingerprint-audit)
Original MIT code and synthetic Taskfile fixtures. Format references only: go-task/task
(MIT), the Taskfile v3 schema and the variable/environment guides, issues #548, #411
and #417. No Task code is copied.

### check
Original MIT code that calls the commands above through their Python APIs.
