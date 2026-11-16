"""Bounded, non-executing input readers. YAML aliases/tags/merges are rejected."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import yaml


class AuditError(Exception):
    """Invalid input or unsafe/unsupported file access; safe to display."""


MAX_DOCUMENT = 2 * 1024 * 1024


def read_text(path: Path, limit: int = MAX_DOCUMENT) -> str:
    try:
        if not path.is_file():
            raise AuditError("input must be an existing regular file")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise AuditError(f"input exceeds {limit} bytes")
        return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuditError(f"cannot read UTF-8 input ({type(exc).__name__})") from None


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("duplicate mapping key")
        result[key] = value
    return result


def load_json(path: Path, limit: int = MAX_DOCUMENT):
    try:
        return json.loads(read_text(path, limit), object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(AuditError("non-finite JSON number")))
    except (ValueError, RecursionError):
        raise AuditError("invalid JSON document") from None


class LiteralLoader(yaml.SafeLoader):
    pass


# YAML 1.2-like scalar subset, avoiding YAML 1.1 yes/no and timestamp coercion.
LiteralLoader.yaml_implicit_resolvers = {}
LiteralLoader.add_implicit_resolver("tag:yaml.org,2002:null", re.compile(r"^(?:~|null|Null|NULL|)$"), ["~", "n", "N", ""])
LiteralLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF"))
LiteralLoader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^-?(?:0|[1-9][0-9]*)$"), list("-0123456789"))
LiteralLoader.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)$"), list("-0123456789"))


def construct_mapping(loader, node):
    pairs = []
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
            raise AuditError("YAML mapping keys must be strings (quote numeric names)")
        key = loader.construct_object(key_node)
        if key == "<<":
            raise AuditError("YAML merge keys are unsupported")
        pairs.append((key, loader.construct_object(value_node)))
    return unique_pairs(pairs)


LiteralLoader.add_constructor("tag:yaml.org,2002:map", construct_mapping)


def load_taskfile(path: Path):
    text = read_text(path)
    try:
        # Disable graph expansion and explicit object types before construction.
        for token in yaml.scan(text):
            if isinstance(token, (yaml.AliasToken, yaml.AnchorToken, yaml.TagToken)):
                raise AuditError("YAML anchors, aliases and explicit tags are unsupported")
        data = yaml.load(text, Loader=LiteralLoader)
    except (yaml.YAMLError, RecursionError, ValueError):
        # Parser excerpts may contain credentials; never echo input excerpts.
        raise AuditError("invalid YAML document") from None
    if not isinstance(data, dict):
        raise AuditError("Taskfile must be a mapping")
    validate_tree(data)
    return data


def validate_tree(value, depth=0):
    if depth > 64:
        raise AuditError("document nesting exceeds 64 levels")
    if isinstance(value, float) and not math.isfinite(value):
        raise AuditError("non-finite number in document")
    if isinstance(value, dict):
        for child in value.values():
            validate_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_tree(child, depth + 1)


def literal(value):
    return type(value) in (str, int, bool) or (type(value) is float and math.isfinite(value))


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def load_env(path: Path | None) -> dict:
    if path is None:
        return {}
    data = load_json(path)
    if not isinstance(data, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not isinstance(v, str) for k, v in data.items()):
        raise AuditError("env snapshot must be a JSON object of identifier keys and string values")
    return data
