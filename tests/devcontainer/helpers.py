"""Shared helpers for the ``stackwrench devcontainer`` tests.

The original node:test suite built fixtures with ``JSON.stringify`` and the
npm ``yaml`` package's ``stringify`` and spawned the CLI. These helpers
reproduce those inputs byte for byte (for the value shapes the tests use) so
expected coordinates can still be computed from literal fixture text.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from stackwrench.devcontainer.jsutil import js_keys, utf16_len

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SARIF_SCHEMA = FIXTURES / "sarif" / "sarif-schema-2.1.0.json"
TMP = tempfile.gettempdir()


class _Undefined:
    """JavaScript ``undefined`` in fixture objects: dropped by js_stringify."""

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED: Any = _Undefined()


def entries(obj: dict[str, Any]) -> list[tuple[str, Any]]:
    """``Object.entries(obj)`` (array-index-like keys first)."""
    return [(key, obj[key]) for key in js_keys(obj)]


def without_undefined(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in obj.items() if value is not UNDEFINED}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(value[key]) for key in js_keys(value) if value[key] is not UNDEFINED}
    if isinstance(value, list):
        return [None if item is UNDEFINED else _jsonable(item) for item in value]
    return value


def js_stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value, null, indent)`` for plain fixture data."""
    if indent:
        return json.dumps(_jsonable(value), ensure_ascii=False, indent=indent)
    return json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"))


# --- npm yaml 2.x ``stringify`` (default options) -----------------------------
# Covers dicts, lists, strings, ints, floats, booleans and None on single-line
# strings shorter than the 80-column fold width, which is all the tests use.

_PLAIN_UNSAFE = re.compile(r"^[\n\t ,\[\]{}#&*!|>'\"%@`]|^[?-]\Z|^[?-][ \t]|[\n:][ \t]|[ \t]\n|[\n\t ]#|[\n\t :]\Z")
_DOCUMENT_MARKER = re.compile(r"^(?:%|---|\.\.\.)", re.MULTILINE)
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_CORE_TESTS = [
    re.compile(r"(?:~|[Nn]ull|NULL)?\Z"),
    re.compile(r"(?:[Tt]rue|TRUE|[Ff]alse|FALSE)\Z"),
    re.compile(r"0o[0-7]+\Z"),
    re.compile(r"[-+]?[0-9]+\Z"),
    re.compile(r"0x[0-9a-fA-F]+\Z"),
    re.compile(r"(?:[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN)\Z"),
    re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+\Z"),
    re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)\Z"),
]
_SHORT_ESCAPES = {"00": "\\0", "07": "\\a", "0b": "\\v", "1b": "\\e"}


def _double_quoted(value: str) -> str:
    def escape(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return _SHORT_ESCAPES.get(match.group(1), "\\x" + match.group(1))
        return match.group(0)

    return re.sub(r"\\(?:u00([0-9a-f]{2})|.)", escape, json.dumps(value, ensure_ascii=False))


def _quoted(value: str, implicit_key: bool) -> str:
    has_double, has_single = '"' in value, "'" in value
    if has_double and not has_single and not (implicit_key and "\n" in value) and not re.search(r"[ \t]\n|\n[ \t]", value):
        return "'" + value.replace("'", "''") + "'"
    return _double_quoted(value)


def _yaml_string(value: str, implicit_key: bool) -> str:
    if _CONTROL.search(value):
        return _double_quoted(value)
    if (
        "\n" in value
        or _PLAIN_UNSAFE.search(value)
        or _DOCUMENT_MARKER.search(value)
        or any(test.match(value) for test in _CORE_TESTS)
    ):
        return _quoted(value, implicit_key)
    return value


def _yaml_scalar(value: Any, implicit_key: bool = False) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return ".nan"
        if value in (float("inf"), float("-inf")):
            return ".inf" if value > 0 else "-.inf"
        if value == 0 and str(value).startswith("-"):
            return "-0"
        return str(int(value)) if value.is_integer() and abs(value) < 1e21 else repr(value)
    return _yaml_string(value, implicit_key)


def _yaml_value(value: Any, indent: str) -> str:
    if isinstance(value, dict) and value:
        return _yaml_map(value, indent)
    if isinstance(value, list) and value:
        return _yaml_seq(value, indent)
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _yaml_scalar(value)


def _yaml_map(value: dict[str, Any], indent: str) -> str:
    lines = []
    for key, item in entries(value):
        inner = indent + "  "
        rendered = _yaml_value(item, inner)
        separator = "\n" + inner if isinstance(item, (dict, list)) and item else " "
        lines.append(f"{_yaml_scalar(key, True)}:{separator}{rendered}")
    return ("\n" + indent).join(lines)


def _yaml_seq(value: list[Any], indent: str) -> str:
    return ("\n" + indent).join("- " + _yaml_value(item, indent + "  ") for item in value)


def yaml_stringify(value: Any) -> str:
    """npm ``yaml`` ``stringify(value)`` with default options."""
    return _yaml_value(value, "") + "\n"


# --- coordinates ----------------------------------------------------------------


def line_column(text: str, offset: int) -> dict[str, int]:
    """1-based line and UTF-16 column of ``offset`` (as the JS tests computed)."""
    prefix = text[:offset].split("\n")
    return {"line": len(prefix), "column": utf16_len(prefix[-1]) + 1}


def position(text: str, token: str, start: int = 0) -> dict[str, int]:
    offset = text.find(token, max(start, 0))
    assert offset >= 0, token
    return line_column(text, offset)


def after_token(text: str, token: str) -> dict[str, int]:
    """Coordinates of the first non-space character after ``token``."""
    end = text.find(token) + len(token)
    offset = end + len(re.match(r"\s*", text[end:]).group(0))
    return line_column(text, offset)


def search(text: str, pattern: str) -> int:
    """``text.search(regex)``."""
    match = re.search(pattern, text)
    return match.start() if match else -1


# --- processes ------------------------------------------------------------------


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str
    report: Any = None


def cli_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "stackwrench", "devcontainer", *args]


def run_cli(args: list[str], cwd: str | Path | None = None, env: dict[str, str] | None = None) -> Result:
    environment = dict(os.environ if env is None else env)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    completed = subprocess.run(
        cli_command(*[str(a) for a in args]), cwd=cwd, env=environment, capture_output=True, timeout=120
    )
    return Result(completed.returncode, completed.stdout.decode("utf-8"), completed.stderr.decode("utf-8"))


# --- SARIF ----------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _sarif_validator() -> Any:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SARIF_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft4Validator(schema, format_checker=jsonschema.Draft4Validator.FORMAT_CHECKER)


def sarif_errors(log: Any) -> list[str]:
    """Schema violations of ``log`` against the pinned SARIF 2.1.0 schema."""
    return [f"{list(error.absolute_path)}: {error.message}" for error in _sarif_validator().iter_errors(log)]


def assert_valid_sarif(log: Any) -> None:
    errors = sarif_errors(log)
    assert not errors, errors


def decode_location(run: dict[str, Any], location: dict[str, Any]) -> str:
    """``fileURLToPath(new URL(uri, base))`` for a SARIF location."""
    artifact = location["physicalLocation"]["artifactLocation"]
    uri = artifact["uri"]
    if artifact.get("uriBaseId"):
        uri = urllib.parse.urljoin(run["originalUriBaseIds"][artifact["uriBaseId"]]["uri"], uri)
    return urllib.parse.unquote(urllib.parse.urlparse(uri).path)


def find(items: list[Any], predicate: Any) -> Any:
    """``Array.prototype.find``: first match or None."""
    return next((item for item in items if predicate(item)), None)


def write(path: str | Path, data: str | bytes) -> None:
    Path(path).write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def read(path: str | Path) -> str:
    return Path(path).read_bytes().decode("utf-8")
