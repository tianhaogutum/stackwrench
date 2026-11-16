"""Host and container path resolution (read-only; never creates any path)."""

from __future__ import annotations

import os
import re
import stat as stat_module
from typing import Any

from .documents import InputError, Location
from .jsutil import FsError, errno_code, fs_error, js_basename, js_dirname, js_join, js_relative, js_resolve

_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def _realpath(path: str) -> str:
    """``fs.realpathSync``: resolve every symlink; missing components raise."""
    try:
        return os.path.realpath(path, strict=True)
    except OSError as error:
        code = errno_code(error)
        raise fs_error(error, "stat" if code == "ELOOP" else "lstat") from None


def canonical(file: str) -> str:
    """Resolve existing ancestors as well as the final component to catch
    symlink escapes, including a missing leaf below an existing symlink."""
    absolute = js_resolve(file)
    try:
        return _realpath(absolute)
    except FsError as error:
        if error.code not in ("ENOENT", "ENOTDIR"):
            raise
        parent = js_dirname(absolute)
        return absolute if parent == absolute else js_join(canonical(parent), js_basename(absolute))


def inside(root: str, file: str) -> bool:
    rel = js_relative(root, file)
    return rel == "" or (not rel.startswith("../") and rel != ".." and not rel.startswith("/"))


def host_path(raw: Any, base: str, root: str, location: Location | None = None) -> str:
    if not isinstance(raw, str) or not raw or "\0" in raw or "\\" in raw:
        raise InputError("invalid-path", "Expected a nonempty Linux path without NUL or backslash", location)
    if raw.startswith("~") or _SCHEME.match(raw) or "$" in raw:
        raise InputError("unsupported-path", f"Path requires unsupported expansion or a remote context: {raw}", location)
    result = canonical(js_resolve(base, raw))
    if not inside(root, result):
        raise InputError(
            "path-escape",
            f"Path {result} escapes declared workspace root {root}; intentionally supply a broader --workspace root to permit it",
            location,
        )
    return result


def container_path(raw: Any, location: Location | None = None) -> str:
    if not isinstance(raw, str) or not raw.startswith("/") or "\\" in raw or "\0" in raw or "$" in raw:
        raise InputError(
            "invalid-container-path", "Expected an absolute POSIX container path without unresolved variables", location
        )
    # normalize() preserves a trailing slash, but mount identity and coverage must
    # treat /work/src and /work/src/ as the same directory. Preserve only root /.
    segments: list[str] = []
    for segment in raw.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def stat(file: str) -> dict[str, Any]:
    try:
        st = os.stat(file)
    except (FileNotFoundError, NotADirectoryError):
        return {"exists": False, "kind": "missing"}
    except OSError as error:
        if errno_code(error) in ("ENOENT", "ENOTDIR"):
            return {"exists": False, "kind": "missing"}
        raise fs_error(error, "stat", file) from None
    if stat_module.S_ISDIR(st.st_mode):
        kind = "directory"
    elif stat_module.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"
    return {"exists": True, "kind": kind}
