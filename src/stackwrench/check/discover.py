"""Find the files that make up a repository's local dev stack.

Discovery only reads: it lists directories (without following symlinks),
matches file names and, for a few kinds that have no fixed name (kind cluster
configs, Kubernetes manifests, kind node inventories, Actions workflows), reads
the first 256 KiB of a file to recognise it. Nothing is executed or written.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re

import yaml

# kind -> label, in the order the report lists them.
KINDS = {
    "compose": "Compose file",
    "dotenv": ".env file",
    "devcontainer": "Dev Container config",
    "mise": "mise config",
    "tool-versions": ".tool-versions file",
    "version-file": "version file",
    "dockerfile": "Dockerfile",
    "dockerignore": ".dockerignore file",
    "workflow": "Actions workflow",
    "kind-config": "kind cluster config",
    "k8s-manifest": "Kubernetes manifest",
    "kind-inventory": "kind node inventory",
    "taskfile": "Taskfile",
    "justfile": "justfile",
}

COMPOSE_BASE = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
COMPOSE_OVERRIDE = ("compose.override.yaml", "compose.override.yml",
                    "docker-compose.override.yaml", "docker-compose.override.yml")
COMPOSE_VARIANT = re.compile(r"(?:docker-)?compose\.[A-Za-z0-9_][A-Za-z0-9_.-]*\.ya?ml\Z")
MISE_FILES = ("mise.toml", ".mise.toml")
VERSION_FILES = (".nvmrc", ".node-version", ".python-version", ".go-version")
TASKFILES = ("Taskfile.yml", "taskfile.yml", "Taskfile.yaml", "taskfile.yaml",
             "Taskfile.dist.yml", "taskfile.dist.yml", "Taskfile.dist.yaml", "taskfile.dist.yaml")
JUSTFILES = ("justfile", ".justfile")  # just matches these case-insensitively

# Directories that never hold dev-stack configuration worth reading.
SKIP_DIRS = frozenset({"node_modules", "bower_components", "vendor", "venv", "site-packages",
                       "__pycache__", "dist", "target"})
# Hidden directories that do.
HIDDEN_DIRS = frozenset({".devcontainer", ".github"})
MAX_DEPTH = 6
MAX_ENTRIES = 50_000
SNIFF_BYTES = 256 * 1024
INVENTORY_BYTES = 1024 * 1024

KIND_CLUSTER = re.compile(r"^kind:[ \t]*Cluster[ \t]*$", re.MULTILINE)
WORKLOAD = re.compile(r"^kind:[ \t]*(?:Pod|Deployment|StatefulSet|DaemonSet|Job|CronJob)[ \t]*$", re.MULTILINE)
SETUP_ACTION = re.compile(r"actions/setup-(?:node|python|go)@")


class DiscoveryError(ValueError):
    """The repository path cannot be scanned (usage error, exit 2)."""


@dataclass(frozen=True)
class Source:
    path: Path
    display: str
    kind: str

    @property
    def label(self) -> str:
        return KINDS[self.kind]


@dataclass(frozen=True)
class ComposeGroup:
    """The Compose files of one directory, which share its ``.env`` file."""

    directory: Path
    # The files `docker compose` loads by default there: the base file, then its override.
    default: tuple[Path, ...]
    # compose.<name>.yaml files, used with `-f`; each is checked on its own.
    variants: tuple[Path, ...] = ()
    env_file: Path | None = None

    @property
    def files(self) -> tuple[Path, ...]:
        return self.default + self.variants

    @property
    def standalone(self) -> tuple[Path, ...]:
        """Files that make sense on their own: every file except the override."""
        return self.default[:1] + self.variants


@dataclass(frozen=True)
class BuildContext:
    context: Path
    dockerfile: Path
    # Where the context came from: "compose.yaml (service web)" or the Dockerfile itself.
    origin: str


@dataclass
class Discovery:
    root: Path
    sources: list[Source] = field(default_factory=list)
    compose_groups: list[ComposeGroup] = field(default_factory=list)
    build_contexts: list[BuildContext] = field(default_factory=list)
    scanned: int = 0
    truncated: bool = False

    def of(self, kind: str) -> list[Source]:
        return [source for source in self.sources if source.kind == kind]

    def paths(self, kind: str) -> list[Path]:
        return [source.path for source in self.of(kind)]

    def display(self, path: Path) -> str:
        try:
            relative = Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
        return relative or "."

    def mix(self) -> Counter:
        return Counter(source.kind for source in self.sources)


def is_dockerfile(name: str) -> bool:
    if name in ("Dockerfile", "Containerfile"):
        return True
    if name.endswith(".dockerignore"):
        return False
    return name.startswith(("Dockerfile.", "Containerfile.")) or name.endswith((".Dockerfile", ".dockerfile"))


def _head(path: Path, limit: int = SNIFF_BYTES) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        with path.open("rb") as stream:
            return stream.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _is_inventory(path: Path) -> bool:
    text = _head(path, INVENTORY_BYTES)
    if text is None or '"cluster"' not in text or '"nodes"' not in text or '"schemaVersion"' not in text:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return (isinstance(data, dict) and data.get("schemaVersion") == 1 and isinstance(data.get("cluster"), str)
            and isinstance(data.get("nodes"), dict))


def _walk(root: Path, discovery: Discovery):
    entries = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        parts = directory.relative_to(root).parts
        keep = []
        for name in sorted(dirnames):
            if len(parts) + 1 > MAX_DEPTH or name in SKIP_DIRS:
                continue
            if name.startswith(".") and name not in HIDDEN_DIRS:
                continue
            # Inside .github only workflows/ matters.
            if parts[:1] == (".github",) and (len(parts) > 1 or name != "workflows"):
                continue
            if (directory / name).is_symlink():
                continue
            keep.append(name)
        dirnames[:] = keep
        entries += len(dirnames) + len(filenames)
        if entries > MAX_ENTRIES:
            discovery.truncated = True
            dirnames[:] = []
        yield directory, parts, sorted(filenames)
        if discovery.truncated:
            return


def discover(path: str | Path) -> Discovery:
    """Scan ``path`` (a repository folder) for dev-stack files. Raises DiscoveryError."""
    given = Path(path)
    if not given.exists():
        raise DiscoveryError(f"no such directory: {path}")
    if not given.is_dir():
        raise DiscoveryError(f"not a directory: {path}")
    # Resolve symlinks once, so every tool sees the same canonical paths (macOS /var, /tmp).
    root = Path(os.path.realpath(given))
    discovery = Discovery(root)
    compose: dict[Path, list[str]] = {}
    found: dict[str, list[Path]] = {kind: [] for kind in KINDS}
    for directory, parts, filenames in _walk(root, discovery):
        in_workflows = parts == (".github", "workflows")
        for name in filenames:
            file = directory / name
            if file.is_symlink():
                continue
            discovery.scanned += 1
            lower = name.lower()
            if name in COMPOSE_BASE or name in COMPOSE_OVERRIDE or COMPOSE_VARIANT.fullmatch(name):
                compose.setdefault(directory, []).append(name)
            elif name == "devcontainer.json" and ".devcontainer" in parts[-2:] or name == ".devcontainer.json":
                found["devcontainer"].append(file)
            elif name in MISE_FILES and not parts:
                found["mise"].append(file)
            elif name == ".tool-versions" and not parts:
                found["tool-versions"].append(file)
            elif name in VERSION_FILES:
                found["version-file"].append(file)
            elif is_dockerfile(name):
                found["dockerfile"].append(file)
            elif name == ".dockerignore" or name.endswith(".dockerignore"):
                found["dockerignore"].append(file)
            elif lower in JUSTFILES:
                found["justfile"].append(file)
            elif name in TASKFILES:
                found["taskfile"].append(file)
            elif in_workflows and lower.endswith((".yml", ".yaml")):
                text = _head(file)
                if text and SETUP_ACTION.search(text):
                    found["workflow"].append(file)
            elif lower.endswith((".yml", ".yaml")):
                text = _head(file)
                if not text:
                    continue
                if "kind.x-k8s.io" in text and KIND_CLUSTER.search(text):
                    found["kind-config"].append(file)
                elif "apiVersion:" in text and WORKLOAD.search(text):
                    found["k8s-manifest"].append(file)
            elif lower.endswith(".json") and not lower.startswith(("package", "tsconfig", "composer")):
                if _is_inventory(file):
                    found["kind-inventory"].append(file)
        if ".env" in filenames and directory in compose:
            found["dotenv"].append(directory / ".env")

    for directory in sorted(compose):
        names = compose[directory]
        base = next((name for name in COMPOSE_BASE if name in names), None)
        override = next((name for name in COMPOSE_OVERRIDE if name in names), None)
        default = tuple(directory / name for name in (base, override) if name)
        variants = tuple(directory / name for name in sorted(names)
                         if name not in COMPOSE_BASE and name not in COMPOSE_OVERRIDE)
        env = directory / ".env"
        group = ComposeGroup(directory, default, variants, env if env in found["dotenv"] else None)
        discovery.compose_groups.append(group)
        found["compose"].extend(group.files)

    # Kubernetes manifests only matter to the kind check; do not list them otherwise.
    if not found["kind-config"] and not found["kind-inventory"]:
        found["k8s-manifest"] = []
    # Workflows only matter as targets of mise pins.
    if not found["mise"]:
        found["workflow"] = []
    found["mise"] = found["mise"][:1]
    for kind in KINDS:
        for file in sorted(set(found[kind])):
            discovery.sources.append(Source(file, discovery.display(file), kind))
    discovery.build_contexts = _build_contexts(discovery)
    return discovery


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normal(path: Path) -> Path:
    return Path(os.path.normpath(path))


def _compose_builds(file: Path):
    """(service, context, dockerfile) for literal `build:` entries of one Compose file."""
    try:
        text = file.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError):
        return
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return
    for name, service in services.items():
        build = service.get("build") if isinstance(service, dict) else None
        if isinstance(build, str):
            context, dockerfile = build, "Dockerfile"
        elif isinstance(build, dict):
            if "dockerfile_inline" in build:
                continue
            context, dockerfile = build.get("context", "."), build.get("dockerfile", "Dockerfile")
        else:
            continue
        if not isinstance(context, str) or not isinstance(dockerfile, str):
            continue
        # Interpolated or remote contexts cannot be resolved without running Compose.
        if "$" in context or "$" in dockerfile or "://" in context or context.startswith("git@"):
            continue
        yield str(name), context, dockerfile


def _build_contexts(discovery: Discovery) -> list[BuildContext]:
    root = discovery.root
    contexts: list[BuildContext] = []
    seen: set[tuple[Path, Path]] = set()

    def add(context: Path, dockerfile: Path, origin: str):
        key = (context, dockerfile)
        if key in seen or not context.is_dir() or not dockerfile.is_file():
            return
        if not _inside(context, root) or not _inside(dockerfile, context):
            return
        seen.add(key)
        contexts.append(BuildContext(context, dockerfile, origin))

    for file in discovery.paths("compose"):
        for service, context, dockerfile in _compose_builds(file):
            folder = _normal(file.parent / context)
            path = _normal(folder / dockerfile) if not PurePosixPath(dockerfile).is_absolute() else Path(dockerfile)
            add(folder, path, f"{discovery.display(file)} (service {service})")
    used = {dockerfile for _, dockerfile in seen}
    for file in discovery.paths("dockerfile"):
        if file in used:
            continue
        # A plain Dockerfile builds from its own folder; Dockerfile.dev and api.Dockerfile
        # are usually built from elsewhere, so only assume that at the repository root.
        if file.name in ("Dockerfile", "Containerfile") or file.parent == root:
            add(file.parent, file, discovery.display(file))
    return contexts
