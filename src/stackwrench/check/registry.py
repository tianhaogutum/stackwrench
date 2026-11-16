"""The list of checks, and the crash-isolating loop that runs them."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Callable, Iterable

from . import compose, devenv, docker, tasks
from .common import DEBUG_ENV, Context, internal
from .discover import Discovery
from .model import INFO, Finding

COMPOSE = "Compose"
BUILDS = "Docker builds"
DEVENV = "Dev environments"
TASKS = "Task runners"
AREAS = (COMPOSE, BUILDS, DEVENV, TASKS)


@dataclass(frozen=True)
class CheckOptions:
    # Build context budget in bytes (included payload); None: report sizes only.
    context_budget: int | None = None
    # podman-compose version to audit against; the bundled evidence covers 1.6.0.
    podman_compose_version: str = "1.6.0"


@dataclass(frozen=True)
class Check:
    name: str
    area: str
    summary: str
    # The stackwrench command whose Python API the check calls, and which functions.
    uses: str
    run: Callable[[Context], Iterable[Finding]]
    # Returns None when the repository has something for this check, else why not.
    applies: Callable[[Discovery], str | None]
    rules: tuple[str, ...] = field(default=())


def _needs(*kinds: str, reason: str):
    def applies(discovery: Discovery) -> str | None:
        return None if any(discovery.of(kind) for kind in kinds) else reason
    return applies


def _merge_applies(discovery: Discovery) -> str | None:
    if any(len(group.default) > 1 for group in discovery.compose_groups):
        return None
    return "no Compose override file" if discovery.compose_groups else "no Compose files"


def _builds_apply(discovery: Discovery) -> str | None:
    return None if discovery.build_contexts else "no Dockerfile build context"


def _pins_apply(discovery: Discovery) -> str | None:
    return None if discovery.of("mise") or discovery.of("tool-versions") else "no mise.toml"


CHECKS: tuple[Check, ...] = (
    Check("compose-env", COMPOSE,
          "Compose interpolation and container environment: ${VAR} unset vs empty, values that only live in "
          "env_file, empty strings that hide values, .env entries no file uses",
          "compose-env (trace)", compose.compose_env, _needs("compose", reason="no Compose files"),
          rules=("env-file-not-interpolated", "unset-substituted-empty", "inherited-unset", "empty-overrides-value",
                 "required-unset", "dotenv-unused", "not-analysed")),
    Check("compose-merge", COMPOSE,
          "Compose override files: ports that stay published, null that removes nothing, commands that are "
          "replaced rather than appended, !reset and !override",
          "compose-merge (merge_files, Project.events)", compose.compose_merge, _merge_applies,
          rules=("ports-merged", "null-ignored", "command-replaced", "list-merged", "reset", "override",
                 "not-analysed")),
    Check("podman-audit", COMPOSE,
          "depends_on and healthcheck declarations that podman-compose was reported to handle differently, "
          "missing or cyclic dependencies, healthy gates without a healthcheck",
          "podman-audit (load_document, audit)", compose.podman_audit, _needs("compose", reason="no Compose files"),
          rules=("healthy-gate-hang", "start-interval", "healthy-gate-without-healthcheck", "missing-dependency",
                 "dependency-cycle", "unverified-version", "not-analysed")),
    Check("context-budget", BUILDS,
          "Docker build contexts: what .dockerignore misses (node_modules, .git, venvs), credential-like files "
          "sent to the builder, size against --context-budget",
          "context-budget (analyze, ignore matcher)", docker.context_budget, _builds_apply,
          rules=("junk-included", "sensitive-file", "over-budget", "size", "not-analysed")),
    Check("cache-replay", BUILDS,
          "Dockerfile cache order: a broad COPY before an expensive RUN, so editing any copied file re-runs it",
          "cache-replay (Dockerfile parser, copy_files, replay)", docker.cache_replay, _builds_apply,
          rules=("broad-copy-before-run", "not-analysed")),
    Check("devcontainer", DEVENV,
          "devcontainer.json wiring: missing Dockerfiles, build contexts and bind sources, unknown Compose "
          "services and volumes, uncovered workspace folders",
          "devcontainer (check)", devenv.devcontainer, _needs("devcontainer", reason="no devcontainer.json"),
          rules=("missing-host-path", "wrong-host-kind", "missing-service", "missing-volume", "path-escape",
                 "workspace-uncovered", "workspace-content-unknown", "parse-error", "unsupported-*")),
    Check("pins", DEVENV,
          "mise.toml pins against Dockerfile FROM tags, .nvmrc/.python-version/.go-version and setup-* "
          "workflow inputs",
          "pins (Root, inspect)", devenv.pins, _pins_apply,
          rules=("drift", "floating-tag", "missing-pin", "not-exact", "not-comparable", "tools-not-compared",
                 "tool-versions-not-read", "summary", "not-analysed")),
    Check("kind-images", DEVENV,
          "kind clusters: images that will still be pulled from a registry (Always policy) or are missing on a "
          "node; needs node snapshots for availability",
          "kind-images (read_manifests, build_report)", devenv.kind_images,
          _needs("kind-config", "kind-inventory", reason="no kind cluster config"),
          rules=("missing-local-image", "always-pull", "would-pull", "needs-snapshots", "needs-input",
                 "unassessed-nodes", "unassessed-objects", "not-analysed")),
    Check("just", TASKS,
          "justfiles: recipes that depend on missing recipes, dependency cycles, duplicates; what the "
          "just→Task bridge cannot translate",
          "just (analyze)", tasks.just, _needs("justfile", reason="no justfile"),
          rules=("missing-dependency", "dependency-cycle", "duplicate-recipe", "duplicate-variable",
                 "outside-subset", "convertible")),
    Check("task-audit", TASKS,
          "Taskfile v3: sources globs that match nothing, commands that read values no source covers, outputs "
          "without inputs",
          "task-audit (audit)", tasks.task_audit, _needs("taskfile", reason="no Taskfile"),
          rules=("sources-match-nothing", "input-not-in-sources", "outputs-without-inputs", "no-source-inputs",
                 "partial", "not-analysed")),
)
BY_NAME = {check.name: check for check in CHECKS}


def select(only: Iterable[str] = (), skip: Iterable[str] = ()) -> list[Check]:
    """Checks to run, in registry order. Raises ValueError for unknown names."""
    only, skip = list(only), list(skip)
    unknown = sorted({name for name in [*only, *skip] if name not in BY_NAME})
    if unknown:
        raise ValueError(f"unknown check(s): {', '.join(unknown)} (see --list-checks)")
    return [check for check in CHECKS if (not only or check.name in only) and check.name not in skip]


def _guarded(check: Check, context: Context) -> list[Finding]:
    try:
        return list(check.run(context))
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing.startswith("stackwrench.") and os.environ.get(DEBUG_ENV) != "1":
            return [Finding(check.name, "unavailable", INFO,
                            f"{missing} is not available in this install; the {check.name} check did not run")]
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return [internal(check.name, exc, None)]
    except Exception as exc:  # noqa: BLE001 - one broken checker must not abort the run
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return [internal(check.name, exc, None)]


@dataclass
class Outcome:
    findings: list[Finding]
    # (check name, reason) for selected checks that had nothing to look at.
    skipped: list[tuple[str, str]]


def run_checks(discovery: Discovery, selected: Iterable[Check] | None = None,
               options: CheckOptions | None = None) -> Outcome:
    """Run ``selected`` checks (default: all) over one repository; never raises for a checker bug."""
    selected = list(CHECKS if selected is None else selected)
    context = Context(discovery, options or CheckOptions())
    findings: list[Finding] = []
    skipped: list[tuple[str, str]] = []
    for check in selected:
        reason = check.applies(discovery)
        if reason is not None:
            skipped.append((check.name, reason))
            continue
        findings.extend(_guarded(check, context))
    return Outcome(sorted(findings, key=Finding.sort_key), skipped)
