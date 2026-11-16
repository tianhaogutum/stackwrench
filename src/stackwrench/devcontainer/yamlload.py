"""Source-located YAML 1.2 core-schema documents on top of PyYAML.

The Node.js tool used npm ``yaml`` 2.x ``parseDocument`` (``version: '1.2'``,
``uniqueKeys: true``, ``strict: true``, pretty errors via a ``LineCounter``)
and then walked the node tree with ``visit``/``isAlias``/``isMap``/``isSeq``/
``isScalar``, read node ranges for locations and called ``toJS``. This module
reproduces the parts of that behaviour the checker observes:

* a node tree built from PyYAML's *event* stream (PyYAML's own composer would
  resolve aliases and use YAML 1.1 implicit types), with ``start`` set to the
  offset npm ``yaml`` reports as ``range[0]`` (content start after any anchor
  or tag; empty scalars sit after the indicator and following spaces);
* aliases, anchors and explicit tags kept visible so callers can reject them;
* YAML 1.2 core-schema scalar resolution (``yes``/``on``/``0b1``/``1_000``
  stay strings, ``0o17``/``0x1F``/``1e3``/``.inf`` are numbers);
* duplicate keys (``Map keys must be unique``), unresolved explicit tags
  (warnings in npm ``yaml``) and multiple documents reported as problems with
  npm-``yaml``-style "at line L, column C" excerpts.

Adapted from the PyYAML 1.2 core-schema loader written for the flowwrench
``windmill_dryrun`` port (same monorepo family, same author), reworked to keep
node positions instead of constructing Python values directly.

PyYAML's YAML 1.1 scanner and parser remain underneath, so syntax-error
*messages* (and occasionally which inputs are syntax errors) differ from npm
``yaml``; see docs/devcontainer-port.md.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from yaml.error import MarkedYAMLError, YAMLError
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
    StreamEndEvent,
)
from yaml.parser import Parser
from yaml.reader import Reader, ReaderError
from yaml.scanner import Scanner

from .jsutil import combine_surrogates, js_object

_TAG = "tag:yaml.org,2002:"
STR_TAG = _TAG + "str"
NULL_TAG = _TAG + "null"
BOOL_TAG = _TAG + "bool"
INT_TAG = _TAG + "int"
FLOAT_TAG = _TAG + "float"
MAP_TAG = _TAG + "map"
SEQ_TAG = _TAG + "seq"

# YAML 1.2 core schema exactly as npm yaml 2.x implements it (schema/core/*).
_NULL = re.compile(r"(?:~|[Nn]ull|NULL)?\Z")
_BOOL = re.compile(r"(?:[Tt]rue|TRUE|[Ff]alse|FALSE)\Z")
_INT_OCT = re.compile(r"0o[0-7]+\Z")
_INT = re.compile(r"[-+]?[0-9]+\Z")
_INT_HEX = re.compile(r"0x[0-9a-fA-F]+\Z")
_FLOAT_NAN = re.compile(r"(?:[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN)\Z")
_FLOAT_EXP = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+\Z")
_FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)\Z")

# Tags npm yaml knows in its core schema: scalar tags with their tests, and the
# YAML 1.1 "known tags" it resolves on request (coreKnownTags).
_SCALAR_TESTS: dict[str, tuple[re.Pattern[str], ...]] = {
    NULL_TAG: (_NULL,),
    BOOL_TAG: (_BOOL,),
    INT_TAG: (_INT_OCT, _INT, _INT_HEX),
    FLOAT_TAG: (_FLOAT_NAN, _FLOAT_EXP, _FLOAT),
}
_KNOWN_SCALAR_TAGS = {_TAG + "binary", _TAG + "merge", _TAG + "timestamp"}
_KNOWN_COLLECTION_TAGS = {_TAG + "set": "map", _TAG + "omap": "seq", _TAG + "pairs": "seq"}


@dataclass(eq=False)
class YamlNode:
    """A composed node. ``kind`` is ``scalar``, ``seq``, ``map`` or ``alias``."""

    kind: str
    start: int
    value: Any = None  # scalar source text / alias name
    items: list[Any] = field(default_factory=list)  # YamlNode (seq) or YamlPair (map)
    anchor: str | None = None
    tag: str | None = None
    tag_start: int | None = None
    tag_end: int | None = None
    style: str | None = None


@dataclass(eq=False)
class YamlPair:
    key: YamlNode
    value: YamlNode | None = None


@dataclass
class Problem:
    """A parse error or warning: prettified ``message`` and ``pos`` [start, end]."""

    message: str
    pos: tuple[int, int]


@dataclass
class Composed:
    root: YamlNode | None
    errors: list[Problem]
    warnings: list[Problem]


class _EventLoader(Reader, Scanner, Parser):
    def __init__(self, stream: str) -> None:
        Reader.__init__(self, stream)
        Scanner.__init__(self)
        Parser.__init__(self)


class _LineIndex:
    """``LineCounter`` equivalent (line starts after each ``\\n``)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0]
        index = text.find("\n")
        while index != -1:
            self.starts.append(index + 1)
            index = text.find("\n", index + 1)

    def line_col(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.starts, offset) - 1
        return line + 1, offset - self.starts[line] + 1

    def prettify(self, message: str, pos: tuple[int, int]) -> str:
        """npm yaml ``prettifyError``: append position and a source excerpt."""
        src = self.text
        line, col = self.line_col(pos[0])
        message += f" at line {line}, column {col}"
        ci = col - 1
        end = self.starts[line] if line < len(self.starts) else None
        line_str = re.sub(r"[\n\r]+\Z", "", src[self.starts[line - 1] : end])
        if ci >= 60 and len(line_str) > 80:
            trim_start = min(ci - 39, len(line_str) - 79)
            line_str = "…" + line_str[trim_start:]
            ci -= trim_start - 1
        if len(line_str) > 80:
            line_str = line_str[:79] + "…"
        if line > 1 and re.fullmatch(" *", line_str[:ci]):
            prev = src[self.starts[line - 2] : self.starts[line - 1]]
            if len(prev) > 80:
                prev = prev[:79] + "…\n"
            line_str = prev + line_str
        if re.search("[^ ]", line_str):
            count = 1
            end_line, end_col = self.line_col(pos[1])
            if end_line == line and end_col > col:
                count = max(1, min(end_col - col, 80 - ci))
            message += f":\n\n{line_str}\n{' ' * ci}{'^' * count}\n"
        return message


def _skip_props(text: str, index: int) -> tuple[int, int | None, int | None]:
    """Skip anchor/tag properties (and the space/comments after them).

    Returns ``(content_start, tag_start, tag_end)``.
    """
    n = len(text)
    tag_start = tag_end = None
    i = index
    while i < n:
        ch = text[i]
        if ch in " \t":
            i += 1
        elif ch in "&!":
            start = i
            if text.startswith("!<", i):
                close = text.find(">", i)
                i = n if close == -1 else close + 1
            else:
                while i < n and text[i] not in " \t\r\n,[]{}":
                    i += 1
            if ch == "!" and tag_start is None:
                tag_start, tag_end = start, i
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif ch in "\r\n":
            i += 1
        else:
            break
    return i, tag_start, tag_end


def _skip_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _event_node(text: str, kind: str, event: Any) -> YamlNode:
    start = event.start_mark.index
    anchor = getattr(event, "anchor", None)
    tag = getattr(event, "tag", None)
    tag_start = tag_end = None
    if anchor is not None or tag is not None:
        start, tag_start, tag_end = _skip_props(text, start)
    node = YamlNode(kind, start, anchor=anchor, tag=tag, tag_start=tag_start, tag_end=tag_end)
    if kind == "scalar":
        node.value = event.value
        node.style = event.style
        if (
            event.value == ""
            and event.style is None
            and anchor is None
            and tag is None
            and event.start_mark.index == event.end_mark.index
        ):
            # npm yaml places an empty value after the indicator and its spaces.
            node.start = _skip_spaces(text, start)
    return node


def resolve_plain(value: str) -> Any:
    """YAML 1.2 core-schema value of an untagged plain scalar (``toJS``)."""
    if _NULL.match(value):
        return None
    if _BOOL.match(value):
        return value[0] in "tT"
    if _INT_OCT.match(value):
        return int(value[2:], 8)
    if _INT.match(value):
        number = int(value)
        return -0.0 if number == 0 and value.startswith("-") else number
    if _INT_HEX.match(value):
        return int(value[2:], 16)
    if _FLOAT_NAN.match(value):
        if value.lower().endswith("nan"):
            return math.nan
        return -math.inf if value.startswith("-") else math.inf
    if _FLOAT_EXP.match(value) or _FLOAT.match(value):
        return float(value)
    return value


class _NotAString:
    """Stand-in for npm yaml values that are neither strings nor plain JSON
    (``Uint8Array`` for ``!!binary``, ``Date`` for ``!!timestamp``, the merge
    symbol for ``!!merge``)."""


_NOT_A_STRING = _NotAString()


def scalar_value(node: YamlNode) -> Any:
    """``Scalar.value`` as npm yaml would resolve it for this node."""
    tag = node.tag
    if tag is None:
        if node.style is None:
            return resolve_plain(node.value)
        return combine_surrogates(node.value)
    if tag in ("!", STR_TAG):
        return combine_surrogates(node.value)
    if tag in _SCALAR_TESTS:
        if any(test.match(node.value) for test in _SCALAR_TESTS[tag]):
            resolved = resolve_plain(node.value)
            if not isinstance(resolved, str):
                return resolved
        return combine_surrogates(node.value)
    if tag in _KNOWN_SCALAR_TAGS:
        return _NOT_A_STRING
    return combine_surrogates(node.value)


def _key_identity(node: YamlNode) -> tuple[str, Any] | None:
    """Equality key for npm yaml's ``uniqueKeys`` check (``===`` on values)."""
    if node.kind != "scalar":
        return None  # collections and aliases only equal themselves
    value = scalar_value(node)
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None  # NaN !== NaN
        if isinstance(value, int) and abs(value) > 2**53:
            value = float(value)  # parseInt() rounds to a double
        return ("number", value)  # 1 == 1.0 and -0.0 == 0, as with ===
    if isinstance(value, str):
        return ("string", value)
    return None


def _tag_problem(node: YamlNode, kind: str) -> str | None:
    """npm yaml warning text for an explicit tag it cannot resolve, if any."""
    tag = node.tag
    if tag is None or tag == "!":
        return None
    if kind == "scalar":
        if tag == STR_TAG or tag in _KNOWN_SCALAR_TAGS:
            return None
        tests = _SCALAR_TESTS.get(tag)
        if tests is not None and any(test.match(node.value) for test in tests):
            return None
        return f"Unresolved tag: {tag}"
    if (tag == MAP_TAG and kind == "map") or (tag == SEQ_TAG and kind == "seq"):
        return None
    expected = _KNOWN_COLLECTION_TAGS.get(tag)
    if expected == kind:
        return None
    if expected is not None:
        return f"{tag} used for {kind} collection, but expects {expected}"
    if tag in _KNOWN_SCALAR_TAGS:
        return f"{tag} used for {kind} collection, but expects scalar"
    return f"Unresolved tag: {tag}"


def compose(text: str) -> Composed:
    """Compose the first YAML document of ``text``.

    Problems that npm ``yaml`` reports as errors (duplicate keys, syntax errors,
    multiple documents) and as warnings (unresolved tags) are collected in
    encounter order; syntax errors stop composition.
    """
    lines = _LineIndex(text)
    errors: list[Problem] = []
    warnings: list[Problem] = []

    def problem(target: list[Problem], message: str, start: int, end: int | None = None) -> None:
        pos = (start, start + 1 if end is None else end)
        target.append(Problem(lines.prettify(message, pos), pos))

    def tag_check(node: YamlNode, kind: str) -> None:
        message = _tag_problem(node, kind)
        if message is not None:
            start = node.tag_start if node.tag_start is not None else node.start
            problem(warnings, message, start, node.tag_end)

    def reader_problem(error: ReaderError) -> None:
        position = error.position if isinstance(error.position, int) else 0
        character = error.character if isinstance(error.character, int) else 0
        problem(errors, f"unacceptable character #x{character:04x}: {error.reason}", position)

    root: YamlNode | None = None
    try:
        loader = _EventLoader(text)
    except ReaderError as error:  # PyYAML checks every character up front
        reader_problem(error)
        return Composed(None, errors, warnings)
    try:
        loader.get_event()  # StreamStart
        if not loader.check_event(StreamEndEvent):
            loader.get_event()  # DocumentStart
            root = _compose_node(text, loader, errors, problem, tag_check)
            loader.get_event()  # DocumentEnd
            if not loader.check_event(StreamEndEvent):
                start = loader.peek_event().start_mark.index
                problem(errors, "Source contains multiple documents; please use YAML.parseAllDocuments()", start)
    except MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        message = error.problem or error.context or "invalid YAML"
        problem(errors, message, mark.index if mark is not None else 0)
    except ReaderError as error:
        reader_problem(error)
    except YAMLError as error:
        problem(errors, str(error) or "invalid YAML", 0)
    finally:
        loader.dispose()
    return Composed(root, errors, warnings)


def _compose_node(
    text: str,
    loader: _EventLoader,
    errors: list[Problem],
    problem: Callable[..., None],
    tag_check: Callable[[YamlNode, str], None],
) -> YamlNode:
    """Iteratively compose one node (deep nesting must not hit Python's stack)."""
    root: YamlNode | None = None
    # Each frame: [collection node, pending key node or None, seen key identities]
    stack: list[list[Any]] = []

    def attach(node: YamlNode) -> None:
        nonlocal root
        if not stack:
            root = node
            return
        frame = stack[-1]
        parent: YamlNode = frame[0]
        if parent.kind == "seq":
            parent.items.append(node)
        elif frame[1] is None:
            frame[1] = node
            identity = _key_identity(node)
            if identity is not None:
                if identity in frame[2]:
                    problem(errors, "Map keys must be unique", node.start)
                frame[2].add(identity)
        else:
            parent.items.append(YamlPair(frame[1], node))
            frame[1] = None

    while True:
        event = loader.get_event()
        if isinstance(event, AliasEvent):
            node = YamlNode("alias", event.start_mark.index, value=event.anchor)
            attach(node)
        elif isinstance(event, ScalarEvent):
            node = _event_node(text, "scalar", event)
            tag_check(node, "scalar")
            attach(node)
        elif isinstance(event, (SequenceStartEvent, MappingStartEvent)):
            kind = "seq" if isinstance(event, SequenceStartEvent) else "map"
            node = _event_node(text, kind, event)
            tag_check(node, kind)
            attach(node)
            stack.append([node, None, set()])
            continue
        elif isinstance(event, (SequenceEndEvent, MappingEndEvent)):
            frame = stack.pop()
            if frame[1] is not None:
                # A key without a value cannot be produced by PyYAML; keep it.
                frame[0].items.append(YamlPair(frame[1], None))
        if not stack:
            assert root is not None
            return root


def walk(root: YamlNode | None, callback: Callable[[Any, int], None]) -> None:
    """npm yaml ``visit`` order: node, then items; pairs before key and value.

    ``callback(node, collections)`` receives the number of map/seq ancestors.
    Pairs are passed as :class:`YamlPair`; absent values as ``None``.
    """
    callback(root, 0)
    if root is not None:
        _walk_children(root, 0, callback)


def _walk_children(node: YamlNode, depth: int, callback: Callable[[Any, int], None]) -> None:
    # ``depth`` is the node's own count of collection ancestors. The callback
    # raises before nesting exceeds 101 collections, which bounds recursion.
    if node.kind not in ("map", "seq"):
        return
    inner = depth + 1
    for item in node.items:
        callback(item, inner)
        if isinstance(item, YamlPair):
            callback(item.key, inner)
            _walk_children(item.key, inner, callback)
            callback(item.value, inner)
            if item.value is not None:
                _walk_children(item.value, inner, callback)
        else:
            _walk_children(item, inner, callback)


def to_js(node: YamlNode | None) -> Any:
    """``doc.toJS()`` for documents without aliases, anchors or tags."""
    if node is None:
        return None
    if node.kind == "map":
        return js_object(
            (str(scalar_value(pair.key)), to_js(pair.value)) for pair in node.items
        )
    if node.kind == "seq":
        return [to_js(item) for item in node.items]
    if node.kind == "scalar":
        return scalar_value(node)
    return None


def _as_item_index(key: Any) -> int | None:
    """npm yaml ``asItemIndex``: sequence lookups accept numeric strings."""
    index = key
    if isinstance(index, str) and index:
        index = _js_number(index)
    if isinstance(index, bool):
        return None
    if isinstance(index, int):
        return index if index >= 0 else None
    if isinstance(index, float) and index.is_integer() and index >= 0:
        return int(index)
    return None


_JS_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def _js_number(text: str) -> float | int:
    """``Number(text)`` for the strings a pointer part can hold."""
    stripped = text.strip(" \t\n\r\v\f         "
                          "        　﻿")
    if not stripped:
        return 0
    lowered = stripped[:2].lower()
    try:
        if lowered == "0x":
            return int(stripped[2:], 16)
        if lowered == "0o":
            return int(stripped[2:], 8)
        if lowered == "0b":
            return int(stripped[2:], 2)
    except ValueError:
        return math.nan
    if stripped in ("Infinity", "+Infinity"):
        return math.inf
    if stripped == "-Infinity":
        return -math.inf
    if _JS_DECIMAL.match(stripped):
        return float(stripped)
    return math.nan


def get_in(root: YamlNode | None, parts: list[Any]) -> YamlNode | None:
    """``doc.getIn(parts, true)``: the node at ``parts`` or ``None``."""
    node = root
    if not parts:
        return node
    for part in parts:
        if node is None:
            return None
        if node.kind == "map":
            found = None
            for pair in node.items:
                if pair.key.kind == "scalar" and _same_value(scalar_value(pair.key), part):
                    found = pair
                    break
            node = found.value if found is not None else None
        elif node.kind == "seq":
            index = _as_item_index(part)
            node = node.items[index] if index is not None and index < len(node.items) else None
        else:
            return None
    return node


def _same_value(a: Any, b: Any) -> bool:
    """JavaScript ``===`` for scalar values."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a is b
