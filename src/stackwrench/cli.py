"""Top-level ``stackwrench`` command: dispatches to one tool per subcommand.

Each tool lives in its own subpackage exposing ``cli.main(argv) -> int``. Tools
are imported lazily, so ``stackwrench --help`` stays fast and a broken or missing
tool only affects its own command. Tools with optional third-party dependencies
declare them in ``requires``; when one is not installed the command prints a
one-line install hint for the matching extra instead of a traceback.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib
import importlib.util
import sys

from . import __version__

PROG = "stackwrench"
REPO_URL = "https://github.com/tianhaogutum/stackwrench"


@dataclass(frozen=True)
class Command:
    name: str
    # Dotted path of the module exposing ``main(argv)``.
    module: str
    summary: str
    group: str
    # Top-level import names of optional dependencies, and the extra that installs them.
    requires: tuple[str, ...] = ()
    extra: str | None = None


GROUPS = ("Check", "Compose", "Docker builds", "Dev environments", "Task runners")

COMMANDS: tuple[Command, ...] = (
    Command("check", "stackwrench.check.cli",
            "Scan a project folder and report what breaks its local dev stack", "Check"),
    Command("compose-env", "stackwrench.compose_env.cli",
            "Trace where Compose interpolation inputs and container environment values come from",
            "Compose"),
    Command("compose-merge", "stackwrench.compose_merge.cli",
            "Explain how ordered Compose files merge, field by field, with file/line provenance",
            "Compose"),
    Command("podman-audit", "stackwrench.podman_audit.cli",
            "Audit Compose depends_on and healthchecks before moving to podman-compose",
            "Compose"),
    Command("context-budget", "stackwrench.context_budget.cli",
            "Inspect a Docker build context: ignore-rule decisions, largest files, size budgets",
            "Docker builds"),
    Command("cache-replay", "stackwrench.cache_replay.cli",
            "Replay which Dockerfile instructions a file change would invalidate in the build cache",
            "Docker builds"),
    Command("devcontainer", "stackwrench.devcontainer.cli",
            "Check Dev Container and Compose wiring: services, build inputs, mounts, volumes (SARIF)",
            "Dev environments"),
    Command("pins", "stackwrench.pins.cli",
            "Reconcile exact mise pins into runtime version files, Dockerfiles and Actions inputs",
            "Dev environments"),
    Command("kind-images", "stackwrench.kind_images.cli",
            "Join Kubernetes image intent with kind node image snapshots; find registry pulls",
            "Dev environments"),
    Command("just", "stackwrench.just_bridge.cli",
            "Translate simple justfile recipes to Taskfile v3 and report every known loss",
            "Task runners"),
    Command("task-audit", "stackwrench.task_audit.cli",
            "Audit Taskfile v3 sources, fingerprints and inputs for possible gaps",
            "Task runners"),
)
BY_NAME = {command.name: command for command in COMMANDS}

# Former standalone tool names, used only to point users at the new subcommand.
LEGACY = {
    "compose-env-trace": "compose-env", "compose-merge-trace": "compose-merge",
    "compose-podman-audit": "podman-audit", "docker-context-budget": "context-budget",
    "docker-cache-replay": "cache-replay", "devcontainer-wirecheck": "devcontainer",
    "mise-pinbridge": "pins", "kind-image-plan": "kind-images", "just-task-bridge": "just",
    "task-fingerprint-audit": "task-audit",
}


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _installed(command: Command) -> bool:
    return _present(command.module.rpartition(".")[0])


def missing_dependency_message(command: Command, dependency: str) -> str:
    extra = f"{PROG}[{command.extra}]"
    return (f"{PROG} {command.name} needs {dependency}: pipx install '{extra}' "
            f"(or pip install '{extra}')\n")


def _missing_requirement(command: Command, exc: ModuleNotFoundError) -> str | None:
    """The optional dependency behind ``exc``, if it is one this command declares."""
    root = (exc.name or "").partition(".")[0]
    return root if root and root in command.requires else None


def overview() -> str:
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = [
        f"{PROG} {__version__} - offline checks that find why your local dev stack breaks",
        "",
        f"usage: {PROG} <command> [args...]",
        f"       {PROG} <command> --help",
    ]
    for group in GROUPS:
        lines += ["", f"{group}:"]
        for command in COMMANDS:
            if command.group != group:
                continue
            note = "" if _installed(command) else "  (coming soon)"
            lines.append(f"  {command.name.ljust(width)}{command.summary}{note}")
    lines += [
        "",
        "options:",
        "  -h, --help     show this overview",
        "  -V, --version  show the version and exit",
        "",
        "Every command runs offline and never starts containers, builds or task runners.",
        "",
        f"Docs and issues: {REPO_URL}",
    ]
    return "\n".join(lines) + "\n"


def _unknown(name: str) -> int:
    message = f"{PROG}: unknown command {name!r}"
    if name in LEGACY:
        message += f"; {name} is now '{PROG} {LEGACY[name]}'"
    else:
        match = difflib.get_close_matches(name, [*BY_NAME, *LEGACY], n=1, cutoff=0.6)
        if match:
            suggestion = LEGACY.get(match[0], match[0])
            message += f"; did you mean '{suggestion}'?"
    sys.stderr.write(message + f"\nRun '{PROG} --help' to list commands.\n")
    return 2


def run(command: Command, argv: list[str]) -> int:
    if not _installed(command):
        sys.stderr.write(f"{PROG}: command {command.name!r} is not available in "
                         f"{PROG} {__version__} yet; see {REPO_URL}\n")
        return 2
    try:
        module = importlib.import_module(command.module)
        result = module.main(argv)
    except ModuleNotFoundError as exc:
        dependency = _missing_requirement(command, exc)
        if dependency is None:
            raise
        sys.stderr.write(missing_dependency_message(command, dependency))
        return 2
    return 0 if result is None else result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(overview())
        return 0
    first, rest = args[0], args[1:]
    if first in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {__version__}\n")
        return 0
    if first == "help":
        if not rest:
            sys.stdout.write(overview())
            return 0
        first, rest = rest[0], ["--help"]
    if first.startswith("-"):
        sys.stderr.write(f"{PROG}: unknown option {first!r}\nRun '{PROG} --help' for usage.\n")
        return 2
    command = BY_NAME.get(first)
    if command is None:
        return _unknown(first)
    return run(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
