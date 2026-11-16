"""Bounded, duplicate-rejecting data loading; never construct Python objects."""

import copy
import json
import math
import re
from bisect import bisect_right
from pathlib import Path

import yaml

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 50_000


class SourceDocument(dict):
    """A normal mapping with out-of-band positions from its original source.

    Metadata is not a model key, so equality and JSON serialization remain data
    only. Reload edited source before asking for declaration coordinates.
    """

    def __init__(self, model, locations):
        super().__init__(model)
        self._source_locations = locations


def field_location(model, path):
    """Never present a missing declaration as a position on its parent."""
    locations = getattr(model, "_source_locations", None)
    position = locations.get(path) if locations is not None else None
    return {"path": path,
            "declaration": ("present" if position else "absent") if locations is not None else "unavailable",
            "line": position[0] if position else None,
            "column": position[1] if position else None}


def _yaml_locations(node, locations, parts=(), mark=None):
    mark = mark if mark is not None else node.start_mark
    locations[json_path(parts)] = (mark.line + 1, mark.column + 1)
    if isinstance(node, yaml.MappingNode):
        for key, child in node.value:
            _yaml_locations(child, locations, (*parts, key.value), key.start_mark)
    elif isinstance(node, yaml.SequenceNode):
        for index, child in enumerate(node.value):
            _yaml_locations(child, locations, (*parts, index))


def _json_locations(text, model):
    """Index already validated/bounded JSON, using its actual token positions.

    Keep strict JSON decoding authoritative (YAML does not decode every JSON
    escape identically). Mapping insertion order follows the source tokens.
    """
    locations = {}
    line_starts = [0] + [match.end() for match in re.finditer(r"\r\n|[\r\n]", text)]
    whitespace = re.compile(r"[ \t\r\n]*")
    decoder = json.JSONDecoder()

    def skip(index):
        return whitespace.match(text, index).end()

    def visit(value, parts, index, key_start=None):
        index = skip(index)
        start = index if key_start is None else key_start
        line = bisect_right(line_starts, start)
        locations[json_path(parts)] = (line, start - line_starts[line - 1] + 1)
        if isinstance(value, dict):
            index = skip(index + 1)  # opening brace
            for key, child in value.items():
                start = index
                _, index = decoder.raw_decode(text, index)  # quoted key
                index = visit(child, (*parts, key), skip(index) + 1, start)  # colon
                index = skip(index)
                if text[index] == ",":
                    index = skip(index + 1)
            return index + 1  # closing brace
        if isinstance(value, list):
            index = skip(index + 1)
            for item, child in enumerate(value):
                index = skip(visit(child, (*parts, item), index))
                if text[index] == ",":
                    index = skip(index + 1)
            return index + 1
        return decoder.raw_decode(text, index)[1]

    visit(model, (), 0)
    return locations


def json_path(parts):
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", part):
            result += "." + part
        else:
            result += "[" + json.dumps(part, ensure_ascii=True) + "]"
    return result


class InputError(ValueError):
    def __init__(self, code, message, path="$", line=None, column=None):
        super().__init__(message)
        self.detail = {"code": code, "message": message, "path": path}
        if line is not None:
            self.detail.update(line=line, column=column)


class StrictLoader(yaml.SafeLoader):
    """Use only true/false booleans, no timestamps or YAML 1.1 sexagesimal."""

    yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)


for first, resolvers in StrictLoader.yaml_implicit_resolvers.items():
    StrictLoader.yaml_implicit_resolvers[first] = [
        (tag, pattern) for tag, pattern in resolvers
        if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp",
                       "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"}
    ]
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"),
    list("tTfF"),
)
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), list("-+0123456789"),
)
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*(?:[eE][-+]?[0-9]+)?|[0-9]+[eE][-+]?[0-9]+)$"),
    list("-+0123456789"),
)


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise InputError("unsupported", "YAML merge keys are not supported.")
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise InputError("invalid-input", "Mapping keys must be strings.",
                             line=key_node.start_mark.line + 1,
                             column=key_node.start_mark.column + 1)
        if key in result:
            raise InputError("duplicate-key", "Duplicate mapping key.",
                             line=key_node.start_mark.line + 1,
                             column=key_node.start_mark.column + 1)
        result[key] = loader.construct_object(value_node, deep=True)
    return result


StrictLoader.add_constructor("tag:yaml.org,2002:map", _mapping)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate-key", "Duplicate JSON mapping key.")
        result[key] = value
    return result


def _constant(value):
    raise InputError("invalid-input", "Non-finite JSON numbers are not supported.")


def _preflight_yaml(text):
    depth, count = 0, 0
    for event in yaml.parse(text, Loader=StrictLoader):
        count += 1
        if count > MAX_NODES:
            raise InputError("input-limit", "Document exceeds 50,000 YAML events.")
        if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
            depth += 1
            if depth > MAX_DEPTH:
                raise InputError("input-limit", "Document nesting exceeds 64 levels.")
        elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
            depth -= 1
        if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
            raise InputError("unsupported", "YAML anchors and aliases are not supported.")
        if getattr(event, "tag", None):
            raise InputError("unsupported", "Explicit YAML tags are not supported.")


def _walk(value, parts=(), depth=0, counter=None):
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > MAX_DEPTH or counter[0] > MAX_NODES:
        raise InputError("input-limit", "Document exceeds depth or node limit.")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise InputError("invalid-input", "Mapping keys must be strings.", json_path(parts))
            # Values and keys are data; no environment expansion is attempted.
            _walk(key, (*parts, key), depth + 1, counter)
            _walk(child, (*parts, key), depth + 1, counter)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, (*parts, index), depth + 1, counter)
    elif isinstance(value, str):
        # A doubled dollar escapes interpolation in Compose. A remaining
        # $NAME or ${...} is unresolved, even if a host environment value exists.
        if re.search(r"\$(?:\{|[A-Za-z_])", value.replace("$$", "")):
            raise InputError("unresolved-interpolation", "Resolve Compose interpolation before auditing.",
                             json_path(parts))
    elif value is not None and type(value) not in (int, float, bool):
        raise InputError("unsupported", "Only JSON-compatible YAML values are supported.", json_path(parts))
    elif isinstance(value, float) and not math.isfinite(value):
        raise InputError("invalid-input", "Non-finite numbers are not supported.", json_path(parts))


def load_document(text, *, format="yaml"):
    """Load one UTF-8 YAML/JSON document. Raise InputError on unsupported data."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise InputError("input-limit", "Input exceeds 2 MiB.")
    try:
        if format == "json":
            model = json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_constant)
        elif format == "yaml":
            _preflight_yaml(text)
            loader = StrictLoader(text)
            try:
                node = loader.get_single_node()
                model = loader.construct_document(node) if node is not None else None
            finally:
                loader.dispose()
        else:
            raise InputError("invalid-input", "Format must be yaml or json.")
        _walk(model)
        if not isinstance(model, dict):
            raise InputError("invalid-input", "Compose document must be a mapping.")
        locations = {}
        if format == "yaml":
            _yaml_locations(node, locations)
        else:
            locations = _json_locations(text, model)
    except InputError:
        raise
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        # Do not echo a source line which could contain secrets.
        mark = getattr(exc, "problem_mark", None)
        raise InputError("invalid-input", "Invalid syntax or multiple documents.",
                         line=mark.line + 1 if mark else getattr(exc, "lineno", None),
                         column=mark.column + 1 if mark else getattr(exc, "colno", None)) from None
    except (RecursionError, ValueError, OverflowError):
        raise InputError("input-limit", "Document exceeds parser limits.") from None
    return SourceDocument(model, locations)


def read_document(filename):
    path = Path(filename)
    try:
        if not path.is_file():
            raise InputError("input-io", "Input must be one regular local file.")
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise InputError("input-limit", "Input exceeds 2 MiB.")
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise InputError("input-io", "Cannot read input as UTF-8.") from None
    return load_document(text, format="json" if path.suffix.lower() == ".json" else "yaml")
