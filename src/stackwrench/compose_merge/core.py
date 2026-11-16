"""Tag-aware input trees, loss gates, resource normalization, and merge history."""
from __future__ import annotations

import copy
import ipaddress
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, DirectiveToken


class MergeError(ValueError):
    """Invalid or unevaluated input; callers must not render a partial result."""


MAX_PATH_QUERIES = 64


def _validate_paths(paths):
    """Shared Python/CLI batch shape; transport byte budgets belong to the CLI."""
    if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATH_QUERIES
            or any(not isinstance(path, str) for path in paths)):
        raise MergeError(f"provide a list of 1..{MAX_PATH_QUERIES} strings")


@dataclass(frozen=True)
class Source:
    file: str
    line: int
    column: int
    input: int


@dataclass
class Node:
    value: Any
    source: Source
    tag: str | None = None

    def plain(self):
        if isinstance(self.value, dict):
            return {k: v.plain() for k, v in self.value.items()}
        if isinstance(self.value, list):
            return [v.plain() for v in self.value]
        return self.value


def fail(n: Node, message: str):
    s = n.source
    raise MergeError(f"{s.file}:{s.line}:{s.column}: {message}")


def pointer(parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


class LiteralLoader(yaml.SafeLoader):
    pass


# Compose uses true/false booleans; do not turn on/off/yes/no into booleans.
LiteralLoader.yaml_implicit_resolvers = {
    k: [(tag, exp) for tag, exp in v if tag not in
        ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float")]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
LiteralLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF"))

# No YAML 1.1 sexagesimal numbers: unquoted 1:20 is a port mapping.
LiteralLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^[+-]?(?:0|[1-9][0-9_]*|0[0-7_]+|0o[0-7_]+|0x[0-9a-fA-F_]+|0b[01_]+)$"),
    list("-+0123456789"),
)
LiteralLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[+-]?(?:[0-9]+\.[0-9]*(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+|\.[0-9]+|\.inf|\.Inf|\.INF|\.nan|\.NaN|\.NAN)$"),
    list("-+0123456789."),
)

MAX_BYTES = 1024 * 1024
MAX_NODES = 50000
MAX_DEPTH = 64
MAX_FILES = 32


def parse(text: str, filename: str, index: int = 0) -> Node:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as e:
        raise MergeError(f"{filename}: input is not valid Unicode") from e
    if size > MAX_BYTES:
        raise MergeError(f"{filename}: file exceeds {MAX_BYTES} bytes")
    loader = None
    count = 0
    try:
        loader = LiteralLoader(text)
        for token in yaml.scan(text, Loader=LiteralLoader):
            if isinstance(token, (AliasToken, AnchorToken, DirectiveToken)):
                raise MergeError(f"{filename}:{token.start_mark.line + 1}: YAML anchors, aliases and directives are unsupported")
        raw = loader.get_single_node()
        if raw is None:
            raise MergeError(f"{filename}: empty YAML document")

        def visit(raw, depth=0):
            nonlocal count
            count += 1
            s = Source(filename, raw.start_mark.line + 1, raw.start_mark.column + 1, index)
            n = Node(None, s)
            if depth > MAX_DEPTH or count > MAX_NODES:
                fail(n, "YAML depth/node limit exceeded")
            custom = raw.tag if raw.tag in ("!reset", "!override") else None
            if raw.tag.startswith("!") and custom is None:
                fail(n, f"unsupported YAML tag {raw.tag}")
            n.tag = custom
            if isinstance(raw, MappingNode):
                if raw.tag not in ("tag:yaml.org,2002:map", "!reset", "!override"):
                    fail(n, f"unsupported mapping tag {raw.tag}")
                n.value = {}
                for k, v in raw.value:
                    if not isinstance(k, ScalarNode) or k.tag != "tag:yaml.org,2002:str":
                        fail(n, "mapping keys must be strings; YAML merge keys are unsupported")
                    if k.value in n.value:
                        fail(Node(None, Source(filename, k.start_mark.line + 1, k.start_mark.column + 1, index)), f"duplicate key {k.value!r}")
                    n.value[k.value] = visit(v, depth + 1)
            elif isinstance(raw, SequenceNode):
                if raw.tag not in ("tag:yaml.org,2002:seq", "!reset", "!override"):
                    fail(n, f"unsupported sequence tag {raw.tag}")
                n.value = [visit(v, depth + 1) for v in raw.value]
            else:
                tag = raw.tag
                if custom:
                    tag = loader.resolve(ScalarNode, raw.value, (raw.style is None, False))
                allowed = {"str", "null", "bool", "int", "float"}
                if tag.removeprefix("tag:yaml.org,2002:") not in allowed:
                    fail(n, f"unsupported scalar tag {tag}")
                if tag in {"tag:yaml.org,2002:int", "tag:yaml.org,2002:float"} and ":" in raw.value:
                    fail(n, "sexagesimal numeric tags are unsupported")
                if tag == "tag:yaml.org,2002:bool" and raw.value.lower() not in {"true", "false"}:
                    fail(n, "booleans must be true or false")
                scalar = ScalarNode(tag, raw.value, raw.start_mark, raw.end_mark)
                n.value = loader.construct_object(scalar)
                if isinstance(n.value, float) and not math.isfinite(n.value):
                    fail(n, "non-finite numbers are unsupported")
            if isinstance(n.value, str):
                try:
                    n.value.encode("utf-8")
                except UnicodeError:
                    fail(n, "invalid Unicode scalar")
                if "$" in n.value:
                    fail(n, "dollar/interpolation syntax is unsupported (including $$)")
            return n

        result = visit(raw)
        validate_root(result)
        return result
    except (yaml.YAMLError, RecursionError, ValueError) as e:
        if isinstance(e, MergeError):
            raise
        raise MergeError(f"{filename}: invalid YAML: {e}") from e
    finally:
        if loader is not None:
            loader.dispose()


def expect(n, kind, label):
    if type(n.value) is not kind:
        fail(n, f"{label} must be {kind.__name__}")


def string(n, label="value"):
    expect(n, str, label)


def keys(n, allowed, label):
    expect(n, dict, label)
    for k, v in n.value.items():
        if k not in allowed and not k.startswith("x-"):
            fail(v, f"unsupported {label} field {k!r}; not evaluated")


def plain_map(n, label):
    expect(n, dict, label)
    for v in n.value.values():
        if v.tag == "!reset" and v.value is None:
            continue
        string(v, label + " value (explicit strings only; no host environment lookup)")


def str_list(n, label):
    expect(n, list, label)
    for v in n.value:
        string(v, label + " item")


def tag_check(n, path=()):
    if n.tag:
        if not path or any(isinstance(p, int) for p in path):
            fail(n, "custom tags are supported only on mapping values outside sequences")
        if n.tag == "!reset" and n.plain() not in (None, [], {}):
            fail(n, "supported !reset payloads are null, [] and {} only")
    if isinstance(n.value, dict):
        for k, v in n.value.items():
            try:
                k.encode("utf-8")
            except UnicodeError:
                fail(v, "invalid Unicode mapping key")
            if "$" in k:
                fail(v, "dollar/interpolation syntax in keys is unsupported")
            tag_check(v, path + (k,))
    elif isinstance(n.value, list):
        for i, v in enumerate(n.value):
            tag_check(v, path + (i,))


def reset(n, kind=None):
    if n.tag != "!reset":
        return False
    if n.value is not None and kind is not None and type(n.value) is not kind:
        fail(n, f"!reset payload must be null or empty {kind.__name__}")
    return True


SERVICE_STR = {"image", "container_name", "hostname", "domainname", "user", "working_dir", "restart", "stop_signal", "stop_grace_period", "network_mode", "ipc", "pid", "pull_policy", "platform"}
SERVICE_BOOL = {"init", "privileged", "read_only", "stdin_open", "tty"}
SERVICE_MAP = {"environment", "labels"}
SERVICE_LIST = {"dns", "dns_search"}
UNIQUE = {"ports", "volumes", "secrets", "configs"}
COMMANDS = {"command", "entrypoint"}
HEALTH = {"test", "interval", "timeout", "start_period", "start_interval", "retries", "disable"}
NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def validate_root(root):
    tag_check(root)
    keys(root, {"name", "version", "services", "volumes", "networks", "secrets", "configs"}, "top-level")
    for key, n in root.value.items():
        if key.startswith("x-") or reset(n, str if key in {"name", "version"} else dict):
            continue
        if key in {"name", "version"}:
            string(n, key)
        elif key == "services":
            expect(n, dict, key)
            for name, svc in n.value.items():
                if not NAME.fullmatch(name):
                    fail(svc, f"invalid service name {name!r}")
                if not reset(svc, dict):
                    validate_service(svc)
        else:
            expect(n, dict, key)
            for name, definition in n.value.items():
                if not NAME.fullmatch(name):
                    fail(definition, f"invalid resource name {name!r}")
                if reset(definition, dict) or definition.value is None:
                    continue
                allowed = {"name", "external", "file"} if key in {"secrets", "configs"} else {"name", "external", "driver", "driver_opts", "labels"}
                if key == "networks":
                    allowed |= {"internal", "attachable", "enable_ipv4", "enable_ipv6"}
                keys(definition, allowed, key + " definition")
                for k, v in definition.value.items():
                    if k.startswith("x-") or reset(v, dict if k in {"driver_opts", "labels"} else bool if k in {"external", "internal", "attachable", "enable_ipv4", "enable_ipv6"} else str):
                        continue
                    if k in {"external", "internal", "attachable", "enable_ipv4", "enable_ipv6"}:
                        expect(v, bool, k)
                    elif k in {"driver_opts", "labels"}:
                        plain_map(v, k)
                    else:
                        string(v, k)


def validate_service(svc):
    keys(svc, SERVICE_STR | SERVICE_BOOL | SERVICE_MAP | SERVICE_LIST | UNIQUE | COMMANDS | {"healthcheck"}, "service")
    for k, n in svc.value.items():
        expected = (str if k in SERVICE_STR else bool if k in SERVICE_BOOL else
                    dict if k in SERVICE_MAP or k == "healthcheck" else list if k in SERVICE_LIST | UNIQUE | COMMANDS else None)
        if k.startswith("x-") or reset(n, expected):
            continue
        if k in SERVICE_STR:
            string(n, k)
        elif k in SERVICE_BOOL:
            expect(n, bool, k)
        elif k in SERVICE_MAP:
            plain_map(n, k)
        elif k in SERVICE_LIST:
            str_list(n, k)
            values = [v.value for v in n.value]
            if len(set(values)) != len(values) or any("=" in v for v in values):
                fail(n, "duplicate or key=value DNS entries are unsupported")
        elif k in COMMANDS:
            if isinstance(n.value, list):
                str_list(n, k)
            elif n.value is not None:
                string(n, k)
        elif k == "healthcheck":
            keys(n, HEALTH, k)
            for h, v in n.value.items():
                if h.startswith("x-") or reset(v, list if h == "test" else bool if h == "disable" else int if h == "retries" else str):
                    continue
                if h == "test":
                    if isinstance(v.value, list):
                        str_list(v, h)
                        if not v.value or v.value[0].value not in {"CMD", "CMD-SHELL", "NONE"}:
                            fail(v, "healthcheck.test list must start with CMD, CMD-SHELL or NONE")
                    else:
                        string(v, h)
                elif h == "disable":
                    expect(v, bool, h)
                elif h == "retries":
                    expect(v, int, h)
                    if v.value < 0:
                        fail(v, "retries must be nonnegative")
                else:
                    string(v, h)
        elif k in UNIQUE:
            expect(n, list, k)
            normalized = [normalize_resource(k, item) for item in n.value]
            seen = set()
            for item in normalized:
                ident = identity(k, item)
                if ident in seen:
                    fail(item, f"duplicate {k} uniqueness key in one file: {ident!r}")
                seen.add(ident)
            n.value = normalized


def generated(value, source):
    if isinstance(value, dict):
        return Node({k: generated(v, source) for k, v in value.items()}, source)
    if isinstance(value, list):
        return Node([generated(v, source) for v in value], source)
    return Node(value, source)


def with_defaults(n, values):
    for k, value in values.items():
        if k not in n.value:
            n.value[k] = generated(value, n.source)
    return n


def port_number(n, label):
    if type(n.value) not in {str, int} or not re.fullmatch(r"[0-9]+", str(n.value)):
        fail(n, f"{label}: only single numeric ports are supported; no ranges")
    number = int(n.value)
    if not 1 <= number <= 65535:
        fail(n, f"{label} outside 1..65535")
    return number


def normalize_resource(kind, item):
    if item.tag:
        fail(item, "tags on resource list items are unsupported")
    if kind == "ports":
        return normalize_port(item)
    if kind == "volumes":
        return normalize_volume(item)
    if isinstance(item.value, str):
        item = generated({"source": item.value}, item.source)
    keys(item, {"source", "target", "uid", "gid", "mode"}, kind + " item")
    if any(k.startswith("x-") for k in item.value):
        fail(item, "resource-item extension keys are unsupported")
    if "source" not in item.value:
        fail(item, f"{kind} item requires source")
    string(item.value["source"], "source")
    if not NAME.fullmatch(item.value["source"].value):
        fail(item, "source must be a literal resource name")
    with_defaults(item, {"target": ("/run/secrets/" if kind == "secrets" else "/") + item.value["source"].value})
    for k, v in item.value.items():
        if v.tag:
            fail(v, "tags inside resource items are unsupported")
        if k == "mode":
            expect(v, int, "mode")
            if not 0 <= v.value <= 0o777:
                fail(v, "mode must be in 0..0777")
        else:
            string(v, k)
            if not v.value:
                fail(v, f"{k} cannot be empty")
    return item


def normalize_port(item):
    if type(item.value) in {str, int}:
        raw = str(item.value)
        if raw.count("/") > 1:
            fail(item, "ambiguous port syntax")
        raw, sep, protocol = raw.partition("/")
        parts = raw.split(":")
        if len(parts) not in {1, 2, 3} or any(not p for p in parts):
            fail(item, "unsupported port syntax; IPv6 requires long form, no empty segments")
        obj = {"target": parts[-1], "protocol": protocol if sep else "tcp"}
        if len(parts) >= 2:
            obj["published"] = parts[-2]
        if len(parts) == 3:
            obj["host_ip"] = parts[0]
        item = generated(obj, item.source)
    keys(item, {"target", "published", "host_ip", "protocol", "mode", "name", "app_protocol"}, "port")
    if any(k.startswith("x-") for k in item.value):
        fail(item, "port extension keys are unsupported")
    if "target" not in item.value:
        fail(item, "port requires target")
    for v in item.value.values():
        if v.tag:
            fail(v, "tags inside port items are unsupported")
    item.value["target"].value = port_number(item.value["target"], "target")
    if "published" in item.value:
        item.value["published"].value = str(port_number(item.value["published"], "published"))
    with_defaults(item, {"protocol": "tcp", "mode": "ingress"})
    for k, v in item.value.items():
        if k == "target":
            continue
        string(v, k)
        if k == "protocol" and v.value not in {"tcp", "udp", "sctp"}:
            fail(v, "unsupported protocol")
        if k == "mode" and v.value not in {"ingress", "host"}:
            fail(v, "mode must be ingress or host")
        if k == "host_ip":
            try:
                parsed = ipaddress.ip_address(v.value)
                if str(parsed) != v.value:
                    fail(v, "host_ip must be a canonical literal IP")
            except ValueError:
                fail(v, "host_ip must be a literal IP")
    return item


def mount_target(n):
    string(n, "mount target")
    value = n.value
    if not value.startswith("/") or value == "/" or any(p in {"", ".", ".."} for p in value[1:].split("/")) or "\\" in value:
        fail(n, "mount target must be a clean absolute POSIX path (not /)")


def normalize_volume(item):
    if isinstance(item.value, str):
        if "\\" in item.value:
            fail(item, "Windows mounts are unsupported")
        parts = item.value.split(":")
        if len(parts) > 3 or any(not p for p in parts):
            fail(item, "unsupported/ambiguous mount syntax")
        if len(parts) == 1:
            obj = {"type": "volume", "target": parts[0], "read_only": False}
        else:
            source, target = parts[:2]
            if len(parts) == 3 and parts[2] not in {"ro", "rw"}:
                fail(item, "short mounts only support ro/rw; use supported long form")
            if "\\" in source or source.startswith("~"):
                fail(item, "Windows and home-expanding mounts are unsupported")
            typ = "bind" if source.startswith(("/", "./", "../")) else "volume"
            obj = {"type": typ, "source": source, "target": target, "read_only": len(parts) == 3 and parts[2] == "ro"}
            if typ == "bind":
                obj["bind"] = {"create_host_path": True}
        item = generated(obj, item.source)
    keys(item, {"type", "source", "target", "read_only", "bind", "volume"}, "mount")
    if any(k.startswith("x-") for k in item.value):
        fail(item, "mount extension keys are unsupported")
    for k in {"type", "target"}:
        if k not in item.value:
            fail(item, f"long mount requires {k}")
    typ = item.value["type"]
    string(typ, "mount type")
    if typ.value not in {"volume", "bind"}:
        fail(typ, "only volume and bind mounts are supported")
    mount_target(item.value["target"])
    if "source" in item.value:
        s = item.value["source"]
        string(s, "mount source")
        if typ.value == "volume" and not NAME.fullmatch(s.value):
            fail(s, "volume source must be a literal resource name")
        if typ.value == "bind" and (not s.value.startswith(("/", "./", "../")) or "\\" in s.value or ":" in s.value):
            fail(s, "bind source must be an explicit POSIX path")
    elif typ.value == "bind":
        fail(item, "bind mount requires source")
    with_defaults(item, {"read_only": False})
    expect(item.value["read_only"], bool, "read_only")
    for k in {"bind", "volume"} & item.value.keys():
        if k != typ.value:
            fail(item, "mount options conflict with mount type")
        options = item.value[k]
        keys(options, {"create_host_path", "propagation"} if k == "bind" else {"nocopy", "subpath"}, k + " options")
        for o, v in options.value.items():
            if o.startswith("x-"):
                fail(v, "mount option extensions are unsupported")
            if o in {"create_host_path", "nocopy"}:
                expect(v, bool, o)
            else:
                string(v, o)
                if o == "propagation" and v.value not in {"private", "rprivate", "shared", "rshared", "slave", "rslave"}:
                    fail(v, "unsupported bind propagation")
    def no_tags(n):
        if n.tag:
            fail(n, "tags inside mount items are unsupported")
        if isinstance(n.value, dict):
            for v in n.value.values():
                no_tags(v)
    no_tags(item)
    return item


def identity(kind, n):
    return resource_identity(kind, n.plain())


def resource_identity(kind, v):
    """Uniqueness key for an already normalized resource, including trace values."""
    if kind == "ports":
        return (v.get("host_ip", "0.0.0.0"), v["target"], v.get("published", ""), v["protocol"])
    return v["target"]


def resource_path(path):
    return len(path) == 3 and path[0] == "services" and path[2] in UNIQUE


def command_path(path):
    return ((len(path) == 3 and path[0] == "services" and path[2] in COMMANDS) or
            (len(path) == 4 and path[0] == "services" and path[2:] == ("healthcheck", "test")))


def resource_collision_gate(old, new):
    if isinstance(old.value, dict) and isinstance(new.value, dict):
        missing = old.value.keys() - new.value.keys()
        if missing:
            fail(new, f"resource collision omits earlier options {sorted(missing)!r}; retention is not evaluated, repeat all options or use !override on the whole resource list")
        for k in old.value:
            resource_collision_gate(old.value[k], new.value[k])


MISSING = object()


def sources(n):
    if n is MISSING:
        return []
    found = {n.source}
    if isinstance(n.value, dict):
        for v in n.value.values():
            found.update(Source(**s) for s in sources(v))
    elif isinstance(n.value, list):
        for v in n.value:
            found.update(Source(**s) for s in sources(v))
    return [asdict(s) for s in sorted(found, key=lambda s: (s.input, s.file, s.line, s.column))]


class Project:
    """Merged tree with copy-returning views and chronological, per-pointer events."""

    def __init__(self):
        self.root = MISSING
        self.events = []

    def record(self, path, action, old, new, source, reason):
        if len(self.events) >= 100000:
            raise MergeError("trace event limit (100000) exceeded; reduce input size")
        self.events.append({
            "order": len(self.events) + 1, "path": pointer(path), "action": action,
            "source": asdict(source), "previousSources": sources(old),
            "previousPresent": old is not MISSING,
            "previous": None if old is MISSING else old.plain(),
            "present": new is not MISSING,
            "value": None if new is MISSING else new.plain(), "reason": reason,
        })

    def replace_tree(self, old, new, path, action, source, reason, tags=None):
        self.record(path, action, old, new, source, reason)
        oldkids = old.value if old is not MISSING and isinstance(old.value, (dict, list)) else {}
        newkids = new.value if new is not MISSING and isinstance(new.value, (dict, list)) else {}
        # History is keyed by JSON Pointer tokens: mapping key "0" and list
        # index 0 name the same child. Keep noncanonical keys (e.g. "00") exact.
        olddict = {str(i): v for i, v in enumerate(oldkids)} if isinstance(oldkids, list) else oldkids
        newdict = {str(i): v for i, v in enumerate(newkids)} if isinstance(newkids, list) else newkids
        taggedkids = tags.get(path, {}) if tags else {}
        for k in dict.fromkeys([*olddict, *newdict, *taggedkids]):
            child_action, child_source, child_reason = action, source, reason
            if k in taggedkids:
                tag, child_source = taggedkids[k]
                child_action = tag.removeprefix("!")
                child_reason = ("!reset removes the declaration" if tag == "!reset" else
                                "!override bypasses normal merge rules")
            self.replace_tree(olddict.get(k, MISSING), newdict.get(k, MISSING),
                              path + (k,), child_action, child_source, child_reason, tags)
        return new

    def merge(self, old, new, path=()):
        if new.tag == "!reset":
            return self.replace_tree(old, MISSING, path, "reset", new.source, "!reset removes the declaration")
        if new.tag == "!override":
            new = copy.deepcopy(new)
            new.tag = None
            # Apply any nested reset/override before replacing this subtree.
            tags = {}
            new = self.materialize(new, path, tags)
            return self.replace_tree(old, new, path, "override", new.source,
                                     "!override bypasses normal merge rules", tags)
        if old is MISSING or (old.value is None and isinstance(new.value, dict)):
            # A null base has no children to merge. Recurse just as for a new
            # mapping so nested tags are evaluated and retain their own history.
            if isinstance(new.value, dict):
                out = Node({}, new.source)
                for k, v in new.value.items():
                    child = self.merge(MISSING, v, path + (k,))
                    if child is not MISSING:
                        out.value[k] = child
                action = "add" if old is MISSING else "replace"
                reason = "new mapping" if old is MISSING else "later declaration replaces a null base"
                self.record(path, action, old, out, new.source, reason)
                return out
            return self.replace_tree(old, copy.deepcopy(new), path, "add", new.source, "new declaration")
        if command_path(path):
            return self.replace_tree(old, copy.deepcopy(new), path, "replace", new.source, "Compose shell command exception: replace, never append")
        if new.value is None:
            self.record(path, "retain", old, old, new.source, "untagged null does not override an existing declaration")
            return old
        if old.value is None:
            return self.replace_tree(old, copy.deepcopy(new), path, "replace", new.source, "later declaration replaces a null base")
        if type(old.value) is not type(new.value):
            fail(new, f"unsupported type change at {pointer(path)}; use !override explicitly")
        if isinstance(new.value, dict):
            out = copy.deepcopy(old)
            out.source = new.source
            for k, v in new.value.items():
                child = self.merge(out.value.get(k, MISSING), v, path + (k,))
                if child is MISSING:
                    out.value.pop(k, None)
                else:
                    out.value[k] = child
            self.record(path, "merge", old, out, new.source, "mapping keys merge recursively")
            return out
        if isinstance(new.value, list):
            out = copy.deepcopy(old)
            out.source = new.source
            if resource_path(path):
                kind = path[2]
                positions = {identity(kind, v): i for i, v in enumerate(out.value)}
                for v in new.value:
                    ident = identity(kind, v)
                    if ident in positions:
                        i = positions[ident]
                        if kind == "volumes" and out.value[i].value["type"].value != v.value["type"].value:
                            fail(v, "changing mount type for a shared target is unsupported; use !override on volumes")
                        # Spec wording and engine versions differ on absent resource
                        # options. Accept collisions only when no old field could be
                        # implicitly retained or discarded.
                        resource_collision_gate(out.value[i], v)
                        out.value[i] = self.merge(out.value[i], v, path + (i,))
                    else:
                        positions[ident] = len(out.value)
                        out.value.append(self.merge(MISSING, v, path + (len(out.value),)))
                reason = f"{kind} uniqueness merge ({'ip,target,published,protocol' if kind == 'ports' else 'target'})"
            else:
                existing = [v.plain() for v in out.value]
                for v in new.value:
                    if v.plain() in existing:
                        fail(v, "identical ordinary sequence entries across files are unsupported (engine deduplication differs from literal append)")
                    existing.append(v.plain())
                    out.value.append(self.merge(MISSING, v, path + (len(out.value),)))
                reason = "ordinary sequence append"
            self.record(path, "merge" if resource_path(path) else "append", old, out, new.source, reason)
            return out
        return self.replace_tree(old, copy.deepcopy(new), path, "replace", new.source, "later scalar wins")

    def materialize(self, n, path=(), tags=None):
        """Resolve nested tags and retain explicit declarations for history."""
        if isinstance(n.value, dict):
            if tags is not None:
                tags[path] = {k: (v.tag, v.source) for k, v in n.value.items() if v.tag}
            n.value = {k: self.materialize(v, path + (k,), tags)
                       for k, v in n.value.items() if v.tag != "!reset"}
        elif isinstance(n.value, list):
            n.value = [self.materialize(v, path + (str(i),), tags) for i, v in enumerate(n.value)]
        n.tag = None
        return n

    def add(self, node):
        self.root = self.merge(self.root, node)
        return self

    def render(self):
        return {} if self.root is MISSING else self.root.plain()

    def history_paths(self) -> list[str]:
        """Return distinct exact pointers in first-recorded-event order.

        Include removed paths; an untouched Project returns an empty list.
        The list is detached. No input I/O, decoding or Project mutation.
        """
        return list(dict.fromkeys(event["path"] for event in self.events))

    def explain_many(self, paths: list[str]):
        """Explain 1..64 pointers in list order, including duplicates.

        Raise MergeError for invalid shapes, invalid pointers or unseen paths.
        Complete every lookup before returning; neither paths nor this Project
        is mutated. No JSON decoding, iterator consumption or input I/O.
        """
        _validate_paths(paths)
        return {"explanations": [self.explain(path) for path in paths]}

    def explain(self, path):
        if path and (not path.startswith("/") or re.search(r"~(?![01])", path)):
            raise MergeError("--path must be an RFC 6901 JSON pointer; use '' for root")
        events = [e for e in self.events if e["path"] == path]
        if not events:
            raise MergeError(f"path has no history: {path!r}")
        parts = [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")] if path else []
        current = self.root
        for p in parts:
            if current is MISSING:
                break
            v = current.value
            if isinstance(v, dict):
                current = v.get(p, MISSING)
            elif (isinstance(v, list) and len(p) <= len(str(len(v)))
                  and re.fullmatch(r"0|[1-9][0-9]*", p)):
                # Historical mapping keys can be arbitrarily long digit strings.
                # Bound conversion by this list's size before interpreting an index.
                index = int(p)
                current = v[index] if index < len(v) else MISSING
            else:
                current = MISSING
        return {"path": path, "present": current is not MISSING,
                "value": None if current is MISSING else current.plain(),
                "winningSources": sources(current), "events": copy.deepcopy(events)}


def merge_files(files) -> Project:
    return _merge_files(files, {})


def _merge_files(files, contents) -> Project:
    """Evaluate an order with existing limits, sharing bounded bytes for comparison."""
    files = list(files)
    if not 1 <= len(files) <= MAX_FILES:
        raise MergeError(f"provide 1..{MAX_FILES} files")
    project = Project()
    # Parse all inputs before merging; failure cannot produce partial stdout.
    nodes = []
    total_bytes = 0
    total_nodes = 0
    def node_count(n):
        children = n.value.values() if isinstance(n.value, dict) else n.value if isinstance(n.value, list) else []
        return 1 + sum(node_count(v) for v in children)
    for i, filename in enumerate(files):
        try:
            if filename not in contents:
                with Path(filename).open("rb") as f:
                    contents[filename] = f.read(MAX_BYTES + 1)
            data = contents[filename]
            if len(data) > MAX_BYTES:
                raise MergeError(f"{filename}: file exceeds {MAX_BYTES} bytes")
            text = data.decode("utf-8")
        except (OSError, UnicodeError) as e:
            raise MergeError(f"{filename}: cannot read UTF-8 YAML: {e}") from e
        total_bytes += len(data)
        if total_bytes > 4 * MAX_BYTES:
            raise MergeError("combined input exceeds 4 MiB")
        node = parse(text, str(filename), i)
        total_nodes += node_count(node)
        if total_nodes > 100000:
            raise MergeError("combined input exceeds 100000 nodes")
        nodes.append(node)
    for n in nodes:
        project.add(n)
    return project
