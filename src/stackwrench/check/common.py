"""Helpers shared by the checks: display paths, YAML line lookup, per-input crash isolation."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Callable, Iterable

import yaml

from .model import ERROR, INFO, Finding

ISSUES_URL = "https://github.com/tianhaogutum/stackwrench/issues"
# Set to 1 to let a crashing check raise its traceback instead of reporting it.
DEBUG_ENV = "STACKWRENCH_CHECK_DEBUG"
MAX_TEXT = 4 * 1024 * 1024


@dataclass
class Context:
    """What a check gets: the discovery result, the options and a per-run memo."""

    discovery: object  # discover.Discovery
    options: object  # registry.CheckOptions
    memo: dict = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.discovery.root

    def display(self, path) -> str:
        return display_path(self.root, path)

    def scrub(self, text) -> str:
        return scrub(self.root, text)

    def yaml_line(self, path: Path, parts) -> int | None:
        """1-based line of the deepest key of ``parts`` found in a YAML file."""
        key = ("yaml-node", str(path))
        if key not in self.memo:
            text = read_text(path)
            self.memo[key] = compose_yaml(text) if text is not None else None
        return node_line(self.memo[key], parts)


def display_path(root: Path, path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    for option in (candidate, Path(os.path.realpath(candidate))):
        try:
            relative = option.relative_to(root).as_posix()
        except ValueError:
            continue
        return relative or "."
    return str(candidate)


def scrub(root: Path, text) -> str:
    """Make absolute paths under the repository root relative in a tool message."""
    value = str(text)
    prefix = str(root).rstrip("/") + "/"
    return value.replace(prefix, "").replace(str(root), ".")


def read_text(path: Path, limit: int = MAX_TEXT) -> str | None:
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data.decode("utf-8", errors="replace")


def compose_yaml(text: str):
    try:
        return yaml.compose(text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError, ValueError):
        return None


def node_line(node, parts) -> int | None:
    line = None
    for part in parts:
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value == str(part):
                    line, node = key.start_mark.line + 1, value
                    break
            else:
                return line
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and 0 <= part < len(node.value):
            node = node.value[part]
            line = node.start_mark.line + 1
        else:
            return line
    return line


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


def size(value: int) -> str:
    """Bytes with SI units, as `du --si` would print them: 512 B, 1.2 kB, 34 MB."""
    if value < 1000:
        return f"{value} B"
    for unit in ("kB", "MB", "GB", "TB"):
        value /= 1000
        if value < 999.95:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


def short(text: str, limit: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def names(items, limit: int = 5) -> str:
    items = list(items)
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def internal(check: str, exc: BaseException, file: str | None) -> Finding:
    # Only the exception type: messages of unexpected errors may quote file content.
    return Finding(check, "internal", ERROR,
                   f"the {check} check crashed ({type(exc).__name__}); its results for this file are missing, "
                   "other checks still ran", file,
                   hint=f"please report it at {ISSUES_URL} (set {DEBUG_ENV}=1 for the traceback)")


def isolated(check: str, file: str | None, run: Callable[[], Iterable[Finding]]) -> list[Finding]:
    """Run one input of a check; a crash becomes one internal finding for that input.

    ModuleNotFoundError is re-raised so the registry can report a tool that is
    missing from this install once, instead of once per file.
    """
    try:
        return list(run())
    except ModuleNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001 - one broken input must not hide the others
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return [internal(check, exc, file)]


LOCATION = re.compile(r"(?P<file>.+?):(?P<line>[0-9]+)(?::(?P<column>[0-9]+))?\Z")


def split_location(location: str) -> tuple[str | None, int | None]:
    """``/repo/compose.yaml:12:9`` (or ``…:9:9->env_file[0]:2``) to (file, line)."""
    head = str(location).partition("->")[0]
    match = LOCATION.fullmatch(head)
    if not match:
        return None, None
    return match["file"], int(match["line"])


def not_analysed(check: str, file: str | None, reason: str, hint: str | None = None,
                 line: int | None = None) -> Finding:
    return Finding(check, "not-analysed", INFO, f"not analysed: {reason}", file, line=line, hint=hint)
