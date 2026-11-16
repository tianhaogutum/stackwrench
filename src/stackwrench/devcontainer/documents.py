"""Read JSONC/JSON/YAML documents with 1-based UTF-16 columns and JSON pointers."""

from __future__ import annotations

import bisect
import os
import stat as stat_module
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from . import jsonc, yamlload
from .jsutil import FsError, fs_error, utf16_len

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 100

Location = dict[str, Any]


class InputError(Exception):
    """A rejected input, reported as an error diagnostic with ``code``/``location``."""

    def __init__(self, code: str, message: str, location: Location | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location


def pointer(parts: list[Any]) -> str:
    """RFC 6901 JSON pointer for ``parts`` (``/`` for the document root)."""
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


class Document:
    """A parsed document: ``file``, ``data`` and ``at(parts) -> location``."""

    def __init__(self, file: str, data: Any, at: Callable[[list[Any]], Location]) -> None:
        self.file = file
        self.data = data
        self.at = at


class _NotRegular(Exception):
    pass


@contextmanager
def _recursion_headroom(limit: int = 25000) -> Iterator[None]:
    # jsonc-parser recurses once per nesting level, like its JavaScript original;
    # give deep (but bounded) inputs the same headroom V8's stack would.
    old = sys.getrecursionlimit()
    if old < limit:
        sys.setrecursionlimit(limit)
    try:
        yield
    finally:
        if old < limit:
            sys.setrecursionlimit(old)


def _read_text(file: str) -> str:
    syscall = "stat"
    try:
        st = os.stat(file)
        if not stat_module.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise _NotRegular()
        syscall = "open"
        with open(file, "rb") as handle:
            raw = handle.read()
    except _NotRegular:
        raise InputError(
            "read-error",
            f"Cannot read {file}: expected a regular file of at most 2 MiB",
            {"file": file, "pointer": "/", "line": None, "column": None},
        ) from None
    except OSError as error:
        message = fs_error(error, syscall, file).message
        raise InputError(
            "read-error", f"Cannot read {file}: {message}", {"file": file, "pointer": "/", "line": None, "column": None}
        ) from None
    except ValueError as error:  # e.g. embedded NUL in the path
        raise InputError(
            "read-error", f"Cannot read {file}: {error}", {"file": file, "pointer": "/", "line": None, "column": None}
        ) from None
    # readFileSync(file, 'utf8'): invalid bytes become U+FFFD, a BOM is kept.
    return raw.decode("utf-8", errors="replace")


def _unsupported_key(key: yamlload.YamlNode) -> bool:
    # !isScalar(p.key) || typeof p.key.value !== 'string' || p.key.value === '<<'
    if key.kind != "scalar":
        return True
    value = yamlload.scalar_value(key)
    return not isinstance(value, str) or value == "<<"


def read_document(file: str, format: str) -> Document:
    """Parse ``file`` as ``jsonc``, ``json`` (strict) or ``yaml``."""
    text = _read_text(file)
    lines = [0]
    index = text.find("\n")
    while index != -1:
        lines.append(index + 1)
        index = text.find("\n", index + 1)
    astral = any(ord(ch) > 0xFFFF for ch in text)

    def loc(offset: int, parts: list[Any] | None = None) -> Location:
        parts = parts or []
        lo = bisect.bisect_right(lines, offset) - 1
        if lo < 0:
            lo = 0
        start = lines[lo]
        column = (utf16_len(text[start:offset]) if astral else offset - start) + 1
        return {"file": file, "pointer": pointer(parts), "line": lo + 1, "column": column}

    if format in ("jsonc", "json"):
        errors: list[jsonc.ParseError] = []
        with _recursion_headroom():
            tree = jsonc.parse_tree(
                text, errors, allow_trailing_comma=format == "jsonc", disallow_comments=format == "json"
            )
        if errors or tree is None:
            first = errors[0] if errors else None
            message = jsonc.print_parse_error_code(first.error) if first else "Empty document"
            raise InputError("parse-error", message, loc(first.offset if first else 0))

        def check(node: jsonc.Node, parts: list[Any], depth: int) -> None:
            if depth > MAX_DEPTH:
                raise InputError("input-limit", "Document nesting exceeds 100 levels", loc(node.offset, parts))
            if node.type == "object":
                keys: set[str] = set()
                for prop in node.children or []:
                    assert prop.children is not None
                    key = prop.children[0].value
                    if key in keys:
                        raise InputError("duplicate-key", f"Duplicate key {key}", loc(prop.offset, [*parts, key]))
                    keys.add(key)
                    check(prop.children[1], [*parts, key], depth + 1)
            elif node.type == "array":
                for i, child in enumerate(node.children or []):
                    check(child, [*parts, i], depth + 1)

        check(tree, [], 0)
        data = jsonc.get_node_value(tree)
        root = tree

        def at(parts: list[Any]) -> Location:
            parent = list(parts)
            while parent and jsonc.find_node_at_location(root, parent) is None:
                parent = parent[:-1]
            node = jsonc.find_node_at_location(root, parent) or root
            location = loc(node.offset, parts)
            if len(parent) < len(parts):
                location["inferred"] = True
            return location

    else:
        composed = yamlload.compose(text)
        problems = [*composed.errors, *composed.warnings]
        if problems:
            problem = problems[0]
            raise InputError("parse-error", problem.message, loc(problem.pos[0]))
        doc_root = composed.root

        def start_of(node: Any) -> int:
            # Pairs (and absent values) have no range in npm yaml: offset 0.
            return node.start if isinstance(node, yamlload.YamlNode) else 0

        def visitor(node: Any, collections: int) -> None:
            if collections > MAX_DEPTH:
                raise InputError("input-limit", "Document nesting exceeds 100 levels", loc(start_of(node)))
            if not isinstance(node, yamlload.YamlNode):
                return
            bad_key = node.kind == "map" and any(_unsupported_key(pair.key) for pair in node.items)
            if node.kind == "alias" or node.anchor or node.tag is not None or bad_key:
                raise InputError(
                    "unsupported-yaml",
                    "Use plain YAML maps with string keys; anchors, aliases, merge keys and explicit tags require pre-rendered JSON",
                    loc(node.start),
                )

        yamlload.walk(doc_root, visitor)
        data = yamlload.to_js(doc_root)

        def at(parts: list[Any]) -> Location:
            parent = list(parts)
            while parent and yamlload.get_in(doc_root, parent) is None:
                parent = parent[:-1]
            node = yamlload.get_in(doc_root, parent)
            if node is None:
                node = doc_root
            location = loc(node.start if node is not None else 0, parts)
            if len(parent) < len(parts):
                location["inferred"] = True
            return location

    if not isinstance(data, dict):
        raise InputError("invalid-document", "Document root must be an object", loc(0))
    return Document(file, data, at)


__all__ = ["InputError", "Document", "FsError", "pointer", "read_document"]
