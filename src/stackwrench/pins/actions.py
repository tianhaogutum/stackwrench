"""Conservative block-YAML reader for explicitly selected Actions workflows.

This is a bounded subset, not a YAML loader: no implicit scalar typing, aliases,
nested flow collections, escapes, multiline strings or tag resolution.
Only exact input spans under jobs.<id>.steps are eligible for replacement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .core import EXACT, Report, expected_pin, text_of
from .fs import Snapshot

KEY = re.compile(r'''(?P<key>[A-Za-z_][A-Za-z0-9_-]*|'[A-Za-z_][A-Za-z0-9_-]*'|"[A-Za-z_][A-Za-z0-9_-]*"):(?:[ \t]+|$)''')
ACTION = re.compile(r"actions/setup-(node|python|go)@[A-Za-z0-9][A-Za-z0-9._/-]*\Z", re.ASCII)
FLOW_ATOM = re.compile(r'''[A-Za-z0-9_][A-Za-z0-9_./@-]*|'[^'\\]*'|"[^"\\]*"''')
HSPACE = re.compile(r"[ \t]*")


class Unsupported(Exception):
    def __init__(self, line: int, message: str):
        self.line, self.message = line, message


@dataclass
class Node:
    kind: str
    line: int
    value: str = ""
    start: int = 0
    end: int = 0
    fields: dict[str, Node] = field(default_factory=dict)
    items: list[Node] = field(default_factory=list)


@dataclass
class Token:
    indent: int
    line: int
    kind: str
    key: str = ""
    value: Node | None = None


def flow_list(raw: str, start: int, number: int) -> Node:
    """Validate a flat string list in one pass; retain no traversable children."""
    message = "expected a single-line flat flow list of simple strings; nested collections, unquoted expressions and escapes are unsupported"
    index = HSPACE.match(raw, 1).end()
    while index < len(raw) and raw[index] != "]":
        atom = FLOW_ATOM.match(raw, index)
        if atom is None:
            raise Unsupported(number, message)
        # Quoted expression text is opaque; list items never become runtime pins.
        index = HSPACE.match(raw, atom.end()).end()
        if index < len(raw) and raw[index] == ",":
            index = HSPACE.match(raw, index + 1).end()
        elif index >= len(raw) or raw[index] != "]":
            raise Unsupported(number, message)
    if index >= len(raw):
        raise Unsupported(number, message)
    end = index + 1
    tail = raw[end:]
    if tail and not re.fullmatch(r"[ \t]+(?:#.*)?", tail):
        raise Unsupported(number, message)
    return Node("flow-sequence", number, raw[:end], start, start + end)


def flow_mapping(raw: str, start: int, number: int) -> Node:
    """Retain flat string fields; only a setup step's with may inspect them."""
    message = "expected a single-line flat flow mapping of simple string keys and values; nested collections, unquoted expressions and escapes are unsupported"
    node = Node("flow-mapping", number, start=start)
    index = HSPACE.match(raw, 1).end()
    while index < len(raw) and raw[index] != "}":
        key = KEY.match(raw, index)
        if key is None:
            raise Unsupported(number, message)
        name = key["key"].strip("\"'")
        if name in node.fields:
            raise Unsupported(number, f"duplicate mapping key {name!r}")
        atom = FLOW_ATOM.match(raw, key.end())
        if atom is None:
            raise Unsupported(number, message)
        quoted = atom[0][0] in "\"'"
        # Quoted expression text is opaque. Actual setup pins still require EXACT.
        value = atom[0][1:-1] if quoted else atom[0]
        node.fields[name] = Node("scalar", number, value,
                                 start + atom.start() + int(quoted),
                                 start + atom.end() - int(quoted))
        index = HSPACE.match(raw, atom.end()).end()
        if index < len(raw) and raw[index] == ",":
            index = HSPACE.match(raw, index + 1).end()
        elif index >= len(raw) or raw[index] != "}":
            raise Unsupported(number, message)
    if index >= len(raw):
        raise Unsupported(number, message)
    end = index + 1
    tail = raw[end:]
    if tail and not re.fullmatch(r"[ \t]+(?:#.*)?", tail):
        raise Unsupported(number, message)
    node.value, node.end = raw[:end], start + end
    return node


def scalar(raw: str, start: int, number: int) -> Node | None:
    raw = raw.rstrip(" \t")
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith("["):
        return flow_list(raw, start, number)
    if raw.startswith("{"):
        return flow_mapping(raw, start, number)
    if raw[0] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        tail = raw[end + 1:] if end >= 0 else "invalid"
        if (end < 0 or "\\" in raw[1:end] or
                (tail and not re.fullmatch(r"[ \t]+(?:#.*)?", tail))):
            raise Unsupported(number, "only single-line quoted scalars without escapes are supported")
        return Node("scalar", number, raw[1:end], start + 1, start + end)
    # A comment starts only at a separation space (not at an embedded '#').
    comment = re.search(r"[ \t]+#", raw)
    value = raw[:comment.start()] if comment else raw
    value = value.rstrip(" \t")
    if re.fullmatch(r"[|>][-+]?", value):
        return Node("block", number, value)
    if value[0] in "[]{}&*!|>%@`" or re.search(r":(?:[ \t]|$)", value):
        raise Unsupported(number, "anchors, aliases, tags and complex/multiline scalars are unsupported")
    return Node("scalar", number, value, start, start + len(value))


def tokenize(text: str) -> list[Token]:
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for raw in lines:
        offsets.append(offset)
        offset += len(raw)
    result = []
    index = 0
    while index < len(lines):
        raw = lines[index].rstrip("\r\n")
        number = index + 1
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw[indent:]
        if not content or content.startswith("#"):
            index += 1
            continue
        if content.startswith("\t") or content in ("---", "...") or content.startswith("%"):
            raise Unsupported(number, "tabs in indentation, directives and document markers are unsupported")
        if content == "-" or content.startswith("- "):
            result.append(Token(indent, number, "dash"))
            if content == "-":
                index += 1
                continue
            # Exactly one separating space keeps the virtual map key column clear.
            indent += 2
            content = content[2:]
            if not content or content[0] in " \t-":
                raise Unsupported(number, "use '- key: value' or a standalone '-' with an indented child")
        match = KEY.match(content)
        if match:
            key = match["key"].strip("\"'")
            value = scalar(content[match.end():], offsets[index] + indent + match.end(), number)
            result.append(Token(indent, number, "map", key, value))
        else:
            value = scalar(content, offsets[index] + indent, number)
            if value is None:
                raise Unsupported(number, "expected a block mapping or sequence entry")
            result.append(Token(indent, number, "scalar", value=value))
        index += 1
        if value is not None and value.kind == "block":
            # Treat the whole literal/folded body as opaque, including YAML decoys.
            body_indent = None
            while index < len(lines):
                body = lines[index].rstrip("\r\n")
                if not body.strip():
                    index += 1
                    continue
                depth = len(body) - len(body.lstrip(" "))
                if depth <= indent:
                    break
                if body_indent is None:
                    body_indent = depth
                elif depth < body_indent:
                    raise Unsupported(index + 1, "inconsistent block scalar indentation")
                index += 1
    return result


class BlockReader:
    def __init__(self, tokens: list[Token]):
        self.tokens, self.index = tokens, 0

    def block(self, indent: int, depth: int = 0) -> Node:
        first = self.tokens[self.index]
        if depth > 64:
            raise Unsupported(first.line, "workflow nesting exceeds 64 levels")
        if first.kind == "scalar":
            self.index += 1
            return first.value
        kind = "sequence" if first.kind == "dash" else "mapping"
        node = Node(kind, first.line)
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if token.indent < indent:
                break
            if token.indent != indent or token.kind != first.kind:
                raise Unsupported(token.line, "inconsistent block indentation or mixed collection types")
            self.index += 1
            if token.kind == "map" and token.key in node.fields:
                raise Unsupported(token.line, f"duplicate mapping key {token.key!r}")
            value = token.value
            if self.index < len(self.tokens) and self.tokens[self.index].indent > indent:
                if value is not None:
                    raise Unsupported(self.tokens[self.index].line, "multiline plain/quoted scalars are unsupported")
                value = self.block(self.tokens[self.index].indent, depth + 1)
            if value is None:
                value = Node("scalar", token.line)
            if kind == "mapping":
                node.fields[token.key] = value
            else:
                node.items.append(value)
        return node


def mapping(node: Node, label: str) -> dict[str, Node]:
    if node.kind != "mapping":
        raise Unsupported(node.line, f"{label} must be a block mapping")
    return node.fields


def scan_workflow(snapshot: Snapshot, pins: dict[str, str], report: Report) -> None:
    text = text_of(snapshot)
    edits: list[tuple[int, int, str]] = []
    try:
        tokens = tokenize(text)
        if not tokens or tokens[0].indent != 0:
            raise Unsupported(tokens[0].line if tokens else 1, "expected a workflow block mapping at column 1")
        reader = BlockReader(tokens)
        document = reader.block(0)
        if reader.index != len(tokens):
            raise Unsupported(tokens[reader.index].line, "unconsumed workflow text")
        fields = mapping(document, "workflow")
        if "jobs" not in fields:
            raise Unsupported(document.line, "workflow requires a jobs block mapping")
        jobs = mapping(fields["jobs"], "jobs")
        for job_id, job in jobs.items():
            job_fields = mapping(job, f"jobs.{job_id}")
            steps = job_fields.get("steps")
            if steps is None:
                # Reusable workflow jobs have no action steps to scan.
                if "uses" in job_fields:
                    uses = job_fields["uses"]
                    if uses.kind != "scalar":
                        raise Unsupported(uses.line, "reusable workflow uses must be a scalar; collections and block strings are unsupported")
                    report.declaration(path=snapshot.path, line=uses.line, tool=None, kind="actions-other",
                                       declaration=f"jobs.{job_id}.uses", expected=None, actual=uses.value,
                                       evaluated=False, reason="reusable workflow job is outside the action-step scope")
                    continue
                raise Unsupported(job.line, f"jobs.{job_id} requires block steps or reusable-workflow uses")
            if steps.kind != "sequence":
                raise Unsupported(steps.line, f"jobs.{job_id}.steps must be a block sequence")
            for index, step in enumerate(steps.items):
                fields = mapping(step, f"jobs.{job_id}.steps[{index}]")
                uses = fields.get("uses")
                if uses is None:
                    continue
                if uses.kind != "scalar" or "run" in fields:
                    raise Unsupported(uses.line, "an action step needs a literal uses scalar and no run key")
                action = ACTION.fullmatch(uses.value)
                label = f"jobs.{job_id}.steps[{index}] {uses.value}"
                if action is None:
                    report.declaration(path=snapshot.path, line=uses.line, tool=None, kind="actions-other",
                                       declaration=label, expected=None, actual=uses.value, evaluated=False,
                                       reason="unknown or dynamic action; only literal actions/setup-node|python|go@REF are evaluated")
                    continue
                tool = action[1]
                key = f"{tool}-version"
                inputs = fields.get("with")
                values = (inputs.fields if inputs.kind == "flow-mapping" else mapping(inputs, "action with")) if inputs else {}
                version = values.get(key)
                version_file = values.get(f"{key}-file")
                input_key = f"{key}-file" if version is None and version_file is not None else key
                location = version if version is not None else version_file
                line = location.line if location is not None else uses.line
                reason = None
                if version_file is not None:
                    reason = f"{key}-file indirection is unsupported; no version files are followed"
                elif version is None or version.kind != "scalar" or not EXACT.fullmatch(version.value):
                    reason = f"{key} requires a literal exact major.minor.patch; matrices, expressions, ranges and structured values are not evaluated"
                expected = expected_pin(tool, pins, report, snapshot.path, line)
                if reason:
                    report.issue(snapshot.path, "unsupported_action_pin", reason, line)
                report.declaration(path=snapshot.path, line=line, tool=tool, kind="actions-version",
                                   declaration=label + f" with.{input_key}", expected=expected,
                                   actual=version.value if version else "<missing>",
                                   evaluated=reason is None and expected is not None, reason=reason)
                if reason is None and expected is not None and version.value != expected:
                    edits.append((version.start, version.end, expected))
    except Unsupported as exc:
        report.issue(snapshot.path, "unsupported_workflow", exc.message, exc.line)
        report.declaration(path=snapshot.path, line=exc.line, tool=None, kind="workflow",
                           declaration="workflow YAML", expected=None, actual="<unsupported workflow>",
                           evaluated=False, reason=exc.message)
        return
    for start, end, expected in sorted(edits, reverse=True):
        text = text[:start] + expected + text[end:]
    if edits:
        report.updates[snapshot.path] = text.encode("utf-8")
