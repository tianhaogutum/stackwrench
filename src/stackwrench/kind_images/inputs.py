"""Bounded, data-only readers with strict duplicate-key checks."""

import json
from pathlib import Path

import yaml
from yaml.tokens import AliasToken, AnchorToken


class InputError(ValueError):
    """Invalid or unsupported input; suitable for a concise CLI diagnostic."""


MAX_BYTES = 8 * 1024 * 1024
MAX_DOCUMENTS = 1000


def read_text(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise InputError(f"{path}: exceeds {MAX_BYTES}-byte input limit")
        return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputError(f"{path}: {exc}") from exc


def mapping(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise InputError(f"{where}: expected an object/mapping")
    return value


def string(value, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InputError(f"{where}: expected a nonempty string without edge whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InputError(f"{where}: control characters are not supported")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _bad_constant(value):
    raise InputError(f"non-JSON numeric constant: {value}")


def read_json(path: Path):
    try:
        return json.loads(read_text(path), object_pairs_hook=_unique_pairs,
                          parse_constant=_bad_constant)
    except (ValueError, RecursionError) as exc:
        raise InputError(f"{path}: {exc}") from exc


class StrictLoader(yaml.SafeLoader):
    pass


def _yaml_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise InputError(f"YAML line {key_node.start_mark.line + 1}: mapping keys must be strings")
        if key in result:
            raise InputError(f"YAML line {key_node.start_mark.line + 1}: duplicate key {key!r}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


StrictLoader.add_constructor("tag:yaml.org,2002:map", _yaml_mapping)


def read_documents(path: Path) -> list:
    text = read_text(path)
    try:
        # Rendered inputs should not need aliases. Reject them before construction
        # to avoid cycles, alias expansion, and implicit YAML merge semantics.
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken)):
                raise InputError("YAML anchors/aliases are unsupported; expand them before analysis")
        docs = []
        for doc in yaml.load_all(text, Loader=StrictLoader):
            if len(docs) >= MAX_DOCUMENTS:
                raise InputError(f"more than {MAX_DOCUMENTS} YAML documents")
            docs.append(doc)
        return docs
    except (yaml.YAMLError, InputError, RecursionError) as exc:
        raise InputError(f"{path}: {exc}") from exc
