"""Strict input parsing and two-phase provenance. Never reads os.environ or executes input."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BRACED = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(:-|:\?|:\+|-|\?|\+)?(.*)\Z", re.S)
MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 64
MAX_USES = 10_000


class TraceError(ValueError):
    """A safe diagnostic containing metadata, never input value text."""


class _MissingInput(TraceError):
    """Only a missing path/target may be skipped for an optional service file."""


@dataclass
class Entry:
    value: str | None
    source: str
    location: str
    syntax: str = "assignment"
    resolved_from: Entry | None = None
    uses: list[int] = field(default_factory=list)
    field_path: list[str | int] | None = None


@dataclass
class _TraceMetadata:
    """Private sidecar; never included in public reports, even with values shown."""

    service_file_identities: dict[str, tuple[int, int]] = field(default_factory=dict)
    service_file_path_uses: dict[str, list[int]] = field(default_factory=dict)


def state(value: str | None) -> str:
    return "unset" if value is None else "empty" if value == "" else "set"


def read_text(path: Path, label: str) -> str:
    return _read_text(path, label)[0]


def _read_text(path: Path, label: str) -> tuple[str, tuple[int, int]]:
    """Read content and private physical identity from the same open file."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise TraceError(f"{label}: expected a regular input file")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise TraceError(f"{label}: input exceeds 2 MiB limit")
        return data.decode("utf-8"), (info.st_dev, info.st_ino)
    except TraceError:
        raise
    except FileNotFoundError:
        raise _MissingInput(f"{label}: input file missing") from None
    except (OSError, UnicodeError, ValueError):
        raise TraceError(f"{label}: cannot read UTF-8 input (missing, inaccessible or invalid)") from None


def location(label: str, node: Node) -> str:
    return f"{label}:{node.start_mark.line + 1}:{node.start_mark.column + 1}"


def valid_name(name: str, loc: str) -> None:
    if not NAME.fullmatch(name):
        raise TraceError(f"{loc}: unsupported variable name; expected ASCII identifier")


def interpolation_tokens(text: str, loc: str):
    """Yield (offset, end, name, operator, operand) using the restricted grammar.

    This lexer validates every expression, including unused literal operands.
    It performs no lookup or evaluation and never emits values into diagnostics.
    """
    i = 0
    while i < len(text):
        i = text.find("$", i)
        if i < 0:
            break
        offset = i
        if text.startswith("$$", i):
            yield offset, i + 2, None, "$$", ""
            i += 2
            continue
        if text.startswith("${", i):
            end = text.find("}", i + 2)
            if end < 0:
                raise TraceError(f"{loc}, offset {i}: unterminated interpolation")
            body = text[i + 2:end]
            match = BRACED.fullmatch(body)
            if match is None or "{" in body or "$" in body:
                raise TraceError(f"{loc}, offset {i}: unsupported interpolation or nesting")
            name, op, word = match.groups()
            op = op or "direct"
            if op == "direct" and word:
                raise TraceError(f"{loc}, offset {i}: unsupported interpolation operator")
            i = end + 1
        else:
            match = BARE.match(text, i + 1)
            if match is None:
                i += 1
                continue
            name, op, word = match.group(), "direct", ""
            i = match.end()
        yield offset, i, name, op, word


def _dynamic_environment_name(text: str, loc: str) -> bool:
    """Classify a list declaration without substituting a name or operand.

    Only an equals sign in a literal span separates name from value. A variable
    before it can also expand to a whole assignment, so its kind stays unknown.
    Validate all tokens first; dollar escapes alone never make a name dynamic.
    """
    tokens = list(interpolation_tokens(text, loc))
    end = 0
    dynamic = False
    for offset, token_end, name, _, _ in tokens:
        if "=" in text[end:offset]:
            break
        dynamic = dynamic or name is not None
        end = token_end
    return dynamic


class Model:
    def __init__(self, show_values: bool, *, syntax_only: bool = False):
        self.show_values = show_values
        self.syntax_only = syntax_only
        self.uses: list[dict] = []
        self.warnings: list[dict] = []
        self.metadata = _TraceMetadata()

    def warn(self, code: str, loc: str, name: str | None = None) -> None:
        warning = {"code": code, "location": loc}
        if name is not None:
            warning["variable"] = name
        self.warnings.append(warning)

    def describe(self, entry: Entry, *, include_value: bool = True) -> dict:
        result = {"source": entry.source, "location": entry.location,
                  "status": state(entry.value), "syntax": entry.syntax,
                  "interpolation_uses": entry.uses}
        if self.show_values and include_value:
            result["value"] = entry.value
        if entry.field_path is not None:
            result["field_path"] = entry.field_path
        if entry.resolved_from is not None:
            result["resolved_from"] = self.describe(entry.resolved_from, include_value=include_value)
        return result

    def record_use(self, event: dict, loc: str) -> int:
        if len(self.uses) >= MAX_USES:
            raise TraceError(f"{loc}: report exceeds 10000 interpolation-use limit")
        index = len(self.uses)
        self.uses.append(event)
        return index

    def expand(self, text: str, lookup: Callable[[str], Entry | None], loc: str,
               phase: str) -> tuple[str, list[int]]:
        """Single pass. Substituted values and escaped dollars are never scanned again."""
        output: list[str] = []
        ids: list[int] = []
        end = 0
        expanded_size = 0
        for offset, token_end, name, op, word in interpolation_tokens(text, loc):
            literal = text[end:offset]
            output.append(literal)
            expanded_size += len(literal)
            end = token_end
            if name is None:
                output.append("$")
                expanded_size += 1
                event = {"phase": phase, "location": loc, "offset": offset,
                         "operator": "$$", "branch": "literal-dollar", "result_status": "set"}
                if self.show_values:
                    event["result"] = "$"
                ids.append(self.record_use(event, loc))
                continue
            if self.syntax_only:
                # Keep syntax internally for structural checks; do not substitute,
                # select branches, call lookup, or evaluate required operators.
                output.append(text[offset:token_end])
                expanded_size += token_end - offset
                ids.append(self.record_use({"phase": phase, "location": loc,
                    "offset": offset, "variable": name, "operator": op}, loc))
                continue
            entry = lookup(name)
            value = entry.value if entry is not None else None
            present = value is not None
            nonempty = present and value != ""
            branch = "source"
            if op == "direct":
                result = value if present else ""
                if not present:
                    self.warn("unresolved-substituted-empty", loc, name)
                    branch = "unset-to-empty"
            elif op in ("-", ":-"):
                use_source = nonempty if op == ":-" else present
                result, branch = (value, "source") if use_source else (word, "default")
            elif op in ("?", ":?"):
                ok = nonempty if op == ":?" else present
                if not ok:
                    raise TraceError(f"{loc}, offset {offset}: required variable {name} ({op}); message redacted")
                result = value
            else:
                use_word = nonempty if op == ":+" else present
                result, branch = (word, "alternative") if use_word else ("", "empty-alternative")
            event = {"phase": phase, "location": loc, "offset": offset,
                     "variable": name, "operator": op, "input_status": state(value),
                     "input_source": self.describe(entry, include_value=False) if entry is not None else None,
                     "branch": branch, "result_status": state(result)}
            if self.show_values:
                event["result"] = result
            ids.append(self.record_use(event, loc))
            expanded_size += len(result)
            if expanded_size > MAX_BYTES:
                raise TraceError(f"{loc}: expanded value exceeds 2 MiB character limit")
            output.append(result)
        output.append(text[end:])
        expanded_size += len(text[end:])
        if expanded_size > MAX_BYTES:
            raise TraceError(f"{loc}: expanded value exceeds 2 MiB character limit")
        return "".join(output), ids


def dotenv(path: Path, label: str, model: Model, lookup: Callable[[str], Entry | None],
           source: str, phase: str,
           fallback: Callable[[str], Entry | None] | None = None, *,
           raw_format: bool = False) -> list[tuple[str, Entry]]:
    """One-line KEY=VALUE: normal parsing/expansion, or an untouched raw remainder."""
    entries: list[tuple[str, Entry]] = []
    local: dict[str, Entry] = {}
    text, identity = _read_text(path, label)
    for number, line in enumerate(text.split("\n"), 1):
        loc = f"{label}:{number}"
        line = line.removesuffix("\r") if raw_format else line.strip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, equal, raw = line.partition("=")
        key = key.strip()
        valid_name(key, loc)
        if not equal:
            raise TraceError(f"{loc}: key-only dotenv declarations unsupported; use KEY= for empty or omit unset names")
        else:
            if raw_format:
                value, literal, uses = raw, False, []
            else:
                raw = raw.strip()
                literal = False
                if raw.startswith(("'", '"')):
                    quote = raw[0]
                    literal = quote == "'"
                    chars: list[str] = []
                    index = 1
                    closed = False
                    while index < len(raw):
                        char = raw[index]
                        if char == quote:
                            tail = raw[index + 1:]
                            if tail.strip() and not (tail[:1].isspace() and tail.lstrip().startswith("#")):
                                raise TraceError(f"{loc}: unsupported text after quoted dotenv value")
                            closed = True
                            break
                        if char == "\\" and index + 1 < len(raw):
                            following = raw[index + 1]
                            escapes = {"'": "'"} if literal else {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
                            if following in escapes:
                                chars.append(escapes[following])
                                index += 2
                                continue
                            if not literal:
                                raise TraceError(f"{loc}: unsupported double-quoted dotenv escape")
                        chars.append(char)
                        index += 1
                    if not closed:
                        raise TraceError(f"{loc}: unterminated quote; multiline dotenv is unsupported")
                    value = "".join(chars)
                else:
                    value = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].rstrip()
                    if "'" in value or '"' in value:
                        raise TraceError(f"{loc}: quotes inside unquoted dotenv values are unsupported")
                uses: list[int] = []
                if not literal:
                    # Explicit context (shell/interpolation) wins over earlier service dotenv entries.
                    def resolve(name: str) -> Entry | None:
                        outer = lookup(name)
                        if outer is not None and outer.value is not None:
                            return outer
                        return local[name] if name in local else fallback(name) if fallback else None
                    value, uses = model.expand(value, resolve, loc, phase)
                    for index in uses:
                        model.uses[index]["field_path"] = [key]
            entry = Entry(value, source, loc, "raw-literal" if raw_format else
                          "single-quoted-literal" if literal else "assignment",
                          uses=uses, field_path=[key])
        entries.append((key, entry))
        local[key] = entry
        if source == "service-env-file":
            model.metadata.service_file_identities[loc] = identity
    return entries


def shell_snapshot(path: Path) -> dict[str, Entry]:
    label = str(path)
    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise TraceError(f"{label}: duplicate JSON key")
            result[key] = value
        return result
    try:
        data = json.loads(read_text(path, label), object_pairs_hook=pairs)
    except (ValueError, RecursionError) as error:
        if isinstance(error, TraceError):
            raise
        raise TraceError(f"{label}: invalid shell snapshot JSON") from None
    if not isinstance(data, dict):
        raise TraceError(f"{label}: shell snapshot must be an object of string values")
    result = {}
    for key, value in data.items():
        valid_name(key, label)
        if not isinstance(value, str):
            raise TraceError(f"{label}: shell snapshot values must be strings (omit unset names)")
        result[key] = Entry(value, "shell-snapshot", f"{label}:key:{key}")
    return result


def load_compose(path: Path) -> Node:
    label = str(path)
    text = read_text(path, label)
    try:
        # Reject graph features before composing to avoid cycles/alias expansion.
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise TraceError(f"{label}:{token.start_mark.line + 1}: YAML anchors, aliases and explicit tags unsupported")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except RecursionError:
        raise TraceError(f"{label}: YAML nesting exceeds supported parser depth") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        suffix = f":{mark.line + 1}:{mark.column + 1}" if mark is not None else ""
        raise TraceError(f"{label}{suffix}: invalid YAML or multiple documents (details redacted)") from None
    if root is None:
        raise TraceError(f"{label}: empty Compose input")
    return root


def mapping(node: Node, label: str) -> dict[str, Node]:
    loc = location(label, node)
    if not isinstance(node, MappingNode):
        raise TraceError(f"{loc}: expected mapping")
    result = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
            raise TraceError(f"{location(label, key)}: mapping keys must be strings; YAML merge unsupported")
        if key.value in result:
            raise TraceError(f"{location(label, key)}: duplicate mapping key (name redacted)")
        result[key.value] = value
    return result


def trace(compose: str | Path, env_files: list[str | Path] | None = None,
          shell_env: str | Path | None = None, *, show_values: bool = False) -> dict:
    """Explain explicit inputs. Paths passed to this API have the same semantics as the CLI."""
    report, _ = _trace(compose, env_files, shell_env, show_values=show_values)
    return report


def _trace(compose: str | Path, env_files: list[str | Path] | None = None,
           shell_env: str | Path | None = None, *, show_values: bool = False,
           syntax_only: bool = False, service_names: list[str] | None = None,
           _root: Node | None = None, _preflight: bool = False) -> tuple[dict, _TraceMetadata]:
    """Internal trace or syntax discovery, plus private file/selector metadata."""
    compose = Path(compose)
    selected = None
    if service_names is not None:
        if not syntax_only:
            raise TraceError("service selection requires syntax-only discovery")
        selection_error = "service selection requires a nonempty list of unique known names (names redacted)"
        if (not isinstance(service_names, list) or not service_names or
            any(not isinstance(name, str) or not name or "\x00" in name for name in service_names)):
            raise TraceError(selection_error)
        selected = set(service_names)
        if len(selected) != len(service_names):
            raise TraceError(selection_error)
        _root = load_compose(compose)
        top = mapping(_root, str(compose))
        available = mapping(top["services"], str(compose)) if "services" in top else {}
        if not selected <= available.keys():
            raise TraceError(selection_error)
        # Reuse the strict syntax/structure parser without opening any dotenv.
        # The same immutable YAML nodes are then scanned for selected references.
        _trace(compose, syntax_only=True, _root=_root, _preflight=True)
    model = Model(show_values, syntax_only=syntax_only)
    label = str(compose)
    shell = shell_snapshot(Path(shell_env)) if shell_env is not None else {}
    inputs: dict[str, list[Entry]] = {}
    pool: dict[str, Entry] = {}
    for path_arg in env_files or []:
        path = Path(path_arg)
        entries = dotenv(path, str(path), model, shell.get,
                         "interpolation-env-file", "interpolation-input", fallback=pool.get)
        for name, entry in entries:
            inputs.setdefault(name, []).append(entry)
            pool[name] = entry
    for name, entry in shell.items():
        inputs.setdefault(name, []).append(entry)
        pool[name] = entry

    root = _root if _root is not None else load_compose(compose)
    rendered: dict[int, str] = {}
    scalar_uses: dict[int, list[int]] = {}
    node_paths: dict[int, list[str | int]] = {}
    not_evaluated: list[dict] = []

    def walk(node: Node, path: list[str | int], depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            raise TraceError(f"{location(label, node)}: YAML nesting exceeds 64 levels")
        if selected is not None and len(path) == 2 and path[0] == "services" and path[1] not in selected:
            return
        node_paths[id(node)] = path
        if isinstance(node, MappingNode):
            mapping(node, label)
            for key, value in node.value:
                walk(value, [*path, key.value], depth + 1)
        elif isinstance(node, SequenceNode):
            for index, value in enumerate(node.value):
                walk(value, [*path, index], depth + 1)
        elif isinstance(node, ScalarNode) and node.tag == "tag:yaml.org,2002:str":
            rendered[id(node)], scalar_uses[id(node)] = model.expand(node.value, pool.get, location(label, node), "compose")
            for index in scalar_uses[id(node)]:
                model.uses[index]["field_path"] = path

    walk(root, [])
    top = mapping(root, label)
    for forbidden in ("include", "env_file", "models", "jobs"):
        if forbidden in top:
            raise TraceError(f"{location(label, top[forbidden])}: unsupported top-level {forbidden}")
    if "services" not in top:
        raise TraceError(f"{label}: services mapping required")
    for key, node in top.items():
        if key != "services":
            not_evaluated.append({"scope": "top-level", "field": key, "location": location(label, node)})
    services = {}
    service_files = []
    unresolved_environment = []
    for service, node in mapping(top["services"], label).items():
        if selected is not None and service not in selected:
            continue
        attributes = mapping(node, label)
        if "extends" in attributes:
            raise TraceError(f"{location(label, attributes['extends'])}: extends/multi-file merge unsupported")
        history: dict[str, list[Entry]] = {}
        service_pool: dict[str, Entry] = {}
        for key, value in attributes.items():
            if key not in ("environment", "env_file"):
                not_evaluated.append({"scope": f"service:{service}", "field": key,
                                      "location": location(label, value)})
        files_node = attributes.get("env_file")
        if files_node is not None:
            files = files_node.value if isinstance(files_node, SequenceNode) else [files_node]
            for index, file_node in enumerate(files):
                required = True
                file_format = "compose"
                if isinstance(file_node, MappingNode) and isinstance(files_node, SequenceNode):
                    options = mapping(file_node, label)
                    option_loc = location(label, file_node)
                    if set(options) - {"path", "required", "format"} or "path" not in options:
                        raise TraceError(f"{option_loc}: env_file entry requires path and only supports required/format options")
                    if "required" in options:
                        required_node = options["required"]
                        if (not isinstance(required_node, ScalarNode) or
                            required_node.tag != "tag:yaml.org,2002:bool" or
                            required_node.value.lower() not in {"true", "false"}):
                            raise TraceError(f"{location(label, required_node)}: env_file required must be a true/false boolean")
                        required = required_node.value.lower() == "true"
                    if "format" in options:
                        format_node = options["format"]
                        if (not isinstance(format_node, ScalarNode) or
                            format_node.tag != "tag:yaml.org,2002:str" or format_node.value != "raw"):
                            raise TraceError(f"{location(label, format_node)}: env_file format must be raw or omitted")
                        file_format = "raw"
                    file_node = options["path"]
                loc = location(label, file_node)
                if not isinstance(file_node, ScalarNode) or file_node.tag != "tag:yaml.org,2002:str":
                    raise TraceError(f"{loc}: env_file expects a path string or list of strings/path mappings")
                file_info = {"service": service, "location": loc,
                             "field_path": node_paths[id(file_node)],
                             "required": required, "format": file_format}
                if syntax_only and any("variable" in model.uses[use]
                                       for use in scalar_uses[id(file_node)]):
                    service_files.append({**file_info, "status": "unresolved-dynamic"})
                    continue
                filename = rendered[id(file_node)]
                if not filename or "\x00" in filename or "://" in filename:
                    raise TraceError(f"{loc}: env_file must resolve to a nonempty local file path")
                if _preflight:
                    continue
                path = Path(filename)
                if path.is_absolute():
                    model.warn("absolute-service-env-file", loc)
                else:
                    path = compose.parent / path
                # Do not expose evaluated filenames: they may contain interpolated secret values.
                file_label = f"{loc}->env_file[{index}]"
                try:
                    entries = dotenv(path, file_label, model, pool.get, "service-env-file",
                                     "service-env-file", fallback=service_pool.get,
                                     raw_format=file_format == "raw")
                except _MissingInput:
                    if required:
                        raise
                    service_files.append({**file_info, "status": "skipped-optional-missing", "reason": "missing"})
                    continue
                for name, entry in entries:
                    model.metadata.service_file_path_uses[entry.location] = scalar_uses[id(file_node)]
                    history.setdefault(name, []).append(entry)
                    service_pool[name] = entry
                service_files.append({**file_info, "status": "scanned"})
        env_node = attributes.get("environment")
        if env_node is not None:
            declarations: list[tuple[str, str | None, str, str, list[int], list[str | int]]] = []
            if isinstance(env_node, MappingNode):
                for name, value in mapping(env_node, label).items():
                    loc = location(label, value)
                    if not isinstance(value, ScalarNode) or value.tag not in {
                        "tag:yaml.org,2002:str", "tag:yaml.org,2002:null", "tag:yaml.org,2002:int"
                    }:
                        raise TraceError(f"{loc}: environment values must be strings, canonical integers or null; quote booleans")
                    if value.tag == "tag:yaml.org,2002:int" and (
                        len(value.value) > 20 or
                        not re.fullmatch(r"(?:0|-?[1-9][0-9]*)", value.value) or
                        not -(2**63) <= int(value.value) < 2**63
                    ):
                        raise TraceError(f"{loc}: only canonical signed 64-bit decimal environment integers supported; quote this value")
                    # PyYAML resolves YAML 1.1; avoid interpreting YAML 1.2 numeric
                    # spellings as strings when Compose would coerce them instead.
                    if value.tag == "tag:yaml.org,2002:str" and value.style is None and re.fullmatch(
                        r"[-+]?(?:(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)(?:[eE][-+]?[0-9_]+)?|0[oOxXbB][0-9a-fA-F_]+)", value.value
                    ):
                        raise TraceError(f"{loc}: ambiguous YAML numeric spelling; quote this environment value")
                    val = None if value.tag.endswith(":null") else rendered.get(id(value), value.value)
                    declarations.append((name, val, loc, "inherited-null" if val is None else "mapping-value",
                                         scalar_uses.get(id(value), []), node_paths[id(value)]))
            elif isinstance(env_node, SequenceNode):
                for value in env_node.value:
                    loc = location(label, value)
                    if not isinstance(value, ScalarNode) or value.tag != "tag:yaml.org,2002:str":
                        raise TraceError(f"{loc}: environment list items must be strings")
                    if syntax_only and _dynamic_environment_name(value.value, loc):
                        unresolved_environment.append({"service": service, "location": loc,
                            "field_path": node_paths[id(value)], "status": "unresolved-dynamic"})
                        continue
                    name, equal, val = rendered[id(value)].partition("=")
                    declarations.append((name, val if equal else None, loc,
                                         "list-value" if equal else "inherited-key-only", scalar_uses[id(value)],
                                         node_paths[id(value)]))
            else:
                raise TraceError(f"{location(label, env_node)}: environment must be mapping or list")
            for name, value, loc, syntax, uses, field_path in declarations:
                valid_name(name, loc)
                if syntax_only and syntax in ("inherited-null", "inherited-key-only"):
                    model.record_use({"variable": name, "operator": syntax, "phase": "compose",
                                      "location": loc, "field_path": field_path}, loc)
                inherited = pool.get(name) if value is None else None
                if value is None:
                    value = inherited.value if inherited is not None else None
                    if value is None:
                        model.warn("unresolved-inherited-removed", loc, name)
                history.setdefault(name, []).append(Entry(value, "environment", loc, syntax, inherited, uses, field_path))
        variables = {}
        for name, declarations in sorted(history.items()):
            variables[name] = {"winner": model.describe(declarations[-1]),
                               "shadowed": [model.describe(e) for e in declarations[:-1]]}
        services[service] = {"image_env": "not-modeled", "variables": variables}
    if syntax_only:
        return {"schema_version": 1, "report_type": "env-template-discovery",
                "evaluation": "syntax-only", "values_shown": False,
                "host_environment": "not-read", "implicit_dotenv": "not-read",
                "complete": not unresolved_environment and
                            all(f["status"] != "unresolved-dynamic" for f in service_files),
                **({"scope": {"services": list(services),
                              "shared": ["explicit-dotenv-inputs", "top-level-compose-values"]}}
                   if selected is not None else {}),
                "references": [use for use in model.uses if "variable" in use],
                "service_env_files": service_files,
                "unresolved_environment": unresolved_environment}, model.metadata
    model.warn("image-env-not-modeled", label)
    if not_evaluated:
        model.warn("non-environment-fields-not-evaluated", label)
    report = {"schema_version": 1, "values_shown": show_values, "host_environment": "not-read",
            "implicit_dotenv": "not-read", "interpolation": {
                "variables": {name: {"winner": model.describe(entries[-1]),
                                     "shadowed": [model.describe(e) for e in entries[:-1]]}
                              for name, entries in sorted(inputs.items())}, "uses": model.uses},
            "services": services, "service_env_files": service_files, "not_evaluated_fields": not_evaluated, "warnings": model.warnings}
    return report, model.metadata
