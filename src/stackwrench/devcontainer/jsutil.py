"""JavaScript semantics that the Node.js implementation relied on implicitly.

The original tool ran on Node.js; several observable behaviours come from the
language rather than from its own code. They are reproduced here so reports
stay identical:

* object key order (array-index-like keys first, ascending, then insertion);
* ``JSON.stringify`` formatting, including escaping of lone surrogates;
* UTF-16 code unit lengths (columns are reported in UTF-16 code units);
* ``path.resolve`` / ``path.join`` / ``path.relative`` (POSIX) normalisation;
* Node-style filesystem error messages (``ENOENT: no such file ..., stat '...'``).
"""

from __future__ import annotations

import errno
import json
import re
from typing import Any, Iterable

_ARRAY_INDEX = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_MAX_ARRAY_INDEX = 2**32 - 2


def is_array_index(key: str) -> bool:
    """True for property keys that JavaScript orders before all other keys."""
    return bool(_ARRAY_INDEX.match(key)) and int(key) <= _MAX_ARRAY_INDEX


def js_keys(obj: dict[str, Any]) -> list[str]:
    """``Object.keys(obj)``: integer-like keys ascending, then insertion order."""
    keys = list(obj)
    indices = [k for k in keys if is_array_index(k)]
    if not indices:
        return keys
    indices.sort(key=int)
    return indices + [k for k in keys if not is_array_index(k)]


def js_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Build a dict whose iteration order matches a JavaScript object's."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        result[key] = value
    if any(is_array_index(k) for k in result):
        return {k: result[k] for k in js_keys(result)}
    return result


def utf16_len(text: str) -> int:
    """``text.length`` in JavaScript (UTF-16 code units)."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def combine_surrogates(text: str) -> str:
    """Join surrogate pairs produced by ``\\uXXXX`` escapes; keep lone ones."""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def json_stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value, null, indent)`` for JSON-compatible data."""
    if indent is None:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    else:
        text = json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False)
    # JSON.stringify escapes lone surrogates (well-formed JSON.stringify).
    return _LONE_SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), text)


def console_text(text: str) -> bytes:
    """What ``console.log`` writes: UTF-8, lone surrogates as U+FFFD."""
    return _LONE_SURROGATE.sub("�", text).encode("utf-8")


# --- POSIX path helpers (node:path semantics) -------------------------------


def _normalize_segments(path: str, absolute: bool) -> list[str]:
    out: list[str] = []
    for segment in path.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if out and out[-1] != "..":
                out.pop()
            elif not absolute:
                out.append("..")
            continue
        out.append(segment)
    return out


def posix_normalize(path: str) -> str:
    """``path.posix.normalize`` (keeps a trailing slash, collapses ``//``)."""
    if not path:
        return "."
    absolute = path.startswith("/")
    trailing = path.endswith("/")
    segments = _normalize_segments(path, absolute)
    result = "/".join(segments)
    if not result and not absolute:
        result = "."
    if result and trailing:
        result += "/"
    return ("/" + result) if absolute else result


def js_resolve(*paths: str, cwd: str | None = None) -> str:
    """``path.posix.resolve(...paths)``: absolute, normalised, no trailing slash."""
    import os

    resolved = ""
    for path in reversed(paths):
        if not path:
            continue
        resolved = path + ("/" + resolved if resolved else "")
        if path.startswith("/"):
            break
    if not resolved.startswith("/"):
        base = cwd if cwd is not None else os.getcwd()
        resolved = base + ("/" + resolved if resolved else "")
    return "/" + "/".join(_normalize_segments(resolved, True))


def js_join(*paths: str) -> str:
    """``path.posix.join(...paths)``."""
    joined = "/".join(p for p in paths if p)
    return posix_normalize(joined) if joined else "."


def js_relative(start: str, target: str) -> str:
    """``path.posix.relative(from, to)``."""
    if start == target:
        return ""
    start = js_resolve(start)
    target = js_resolve(target)
    if start == target:
        return ""
    a = [s for s in start.split("/") if s]
    b = [s for s in target.split("/") if s]
    common = 0
    while common < len(a) and common < len(b) and a[common] == b[common]:
        common += 1
    return "/".join([".."] * (len(a) - common) + b[common:])


def js_dirname(path: str) -> str:
    """``path.posix.dirname`` for the absolute, normalised paths used here."""
    if not path:
        return "."
    stripped = path.rstrip("/")
    if not stripped:
        return "/"
    head = stripped.rsplit("/", 1)[0] if "/" in stripped else ""
    if not head:
        return "/" if path.startswith("/") else "."
    return head.rstrip("/") or "/"


def js_basename(path: str) -> str:
    """``path.posix.basename`` (trailing slashes ignored)."""
    stripped = path.rstrip("/")
    return stripped.rsplit("/", 1)[-1] if stripped else ""


# --- Node.js filesystem error messages ---------------------------------------

_UV_DESCRIPTIONS = {
    "ENOENT": "no such file or directory",
    "ENOTDIR": "not a directory",
    "EACCES": "permission denied",
    "EPERM": "operation not permitted",
    "ELOOP": "too many symbolic links encountered",
    "ENAMETOOLONG": "name too long",
    "EISDIR": "illegal operation on a directory",
    "EIO": "i/o error",
    "EMFILE": "too many open files",
    "ENFILE": "file table overflow",
}


class FsError(Exception):
    """A filesystem failure carrying Node.js-style ``code`` and ``message``."""

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def errno_code(error: OSError) -> str | None:
    return errno.errorcode.get(error.errno) if error.errno is not None else None


def fs_error(error: OSError, syscall: str, path: str | None = None) -> FsError:
    """Convert an ``OSError`` into the message Node.js would have produced."""
    code = errno_code(error)
    description = _UV_DESCRIPTIONS.get(code or "") or (error.strerror or str(error)).lower()
    target = path if path is not None else error.filename
    message = f"{code}: {description}, {syscall}" if code else description
    if target is not None:
        message += f" '{target}'"
    return FsError(code, message)
