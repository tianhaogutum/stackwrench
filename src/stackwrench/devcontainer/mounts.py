"""Dev Container ``--mount`` strings/objects and Compose service volumes.

``fields`` is an optional dict of per-field locations (``type``, ``source``,
``target``, ``read_only``, ``consistency``, ``propagation``,
``create_host_path``, ``nocopy``, ``subpath``) plus an optional
``optionLocation(parts)`` callable; absent entries fall back to ``location``.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .documents import InputError, Location
from .jsutil import js_keys

_ALIASES = {"src": "source", "dst": "target", "destination": "target", "readonly": "read_only"}
# The original looked aliases up on a plain object literal, so Object.prototype
# members resolved to (and were reported as) their string conversions.
_PROTOTYPE_MEMBERS = {
    "__proto__": "[object Object]",
    **{
        name: f"function {'Object' if name == 'constructor' else name}() {{ [native code] }}"
        for name in (
            "constructor",
            "hasOwnProperty",
            "isPrototypeOf",
            "propertyIsEnumerable",
            "toLocaleString",
            "toString",
            "valueOf",
            "__defineGetter__",
            "__defineSetter__",
            "__lookupGetter__",
            "__lookupSetter__",
        )
    },
}
_VOLUME_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")
_SHORT_OPTIONS = ("ro", "rw", "z", "Z", "cached", "delegated", "consistent")
_CONSISTENCY = ("consistent", "cached", "delegated")
_GROUPS = (
    ("bind", ("propagation", "create_host_path", "selinux")),
    ("volume", ("nocopy", "subpath")),
    ("tmpfs", ("size", "mode")),
)


def _fail(message: str, location: Location | None) -> None:
    raise InputError("invalid-mount", message, location)


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _field(fields: dict[str, Any] | None, name: str, fallback: Location | None) -> Location | None:
    value = fields.get(name) if fields else None
    return value if value is not None else fallback


def _option_location(
    option_location: Callable[[list[Any]], Location | None] | None, parts: list[Any], fallback: Location | None
) -> Location | None:
    value = option_location(parts) if option_location is not None else None
    return value if value is not None else fallback


def _allowed(
    value: dict[str, Any],
    keys: tuple[str, ...],
    location: Location | None,
    option_location: Callable[[list[Any]], Location | None] | None,
    prefix: tuple[str, ...] = (),
) -> None:
    for key in js_keys(value):
        if key not in keys:
            _fail(f"Unsupported mount option {key}", _option_location(option_location, [*prefix, key], location))


def _is_string_or_bool(value: Any, options: tuple[Any, ...]) -> bool:
    """``options.includes(value)`` with strict equality (``1 !== true``)."""
    return any(type(value) is type(option) and value == option for option in options)


def dev_mount(value: Any, location: Location | None = None, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dev Container ``mounts`` entry (Docker ``--mount`` string or object)."""
    if isinstance(value, str):
        mount: dict[str, Any] = {}
        for item in value.split(","):
            pos = item.find("=")
            key = item if pos < 0 else item[:pos]
            val: Any = True if pos < 0 else item[pos + 1 :]
            key = _ALIASES.get(key) or _PROTOTYPE_MEMBERS.get(key, key)
            if not key or key in mount:
                _fail("Empty or duplicate mount option", location)
            mount[key] = val
    elif _is_object(value):
        mount = dict(value)
    else:
        _fail("Mount must be an object or Docker --mount string", location)
    option_location = fields.get("optionLocation") if (fields and _is_object(value)) else None
    _allowed(mount, ("type", "source", "target", "read_only", "consistency"), location, option_location)
    if "read_only" in mount and not _is_string_or_bool(mount["read_only"], (True, False, "true", "false")):
        _fail("Invalid read_only value", _field(fields, "read_only", location))
    if "consistency" in mount and not _is_string_or_bool(mount["consistency"], _CONSISTENCY):
        _fail("Unsupported consistency value", _field(fields, "consistency", location))
    return _validate(mount, location, fields)


def compose_mount(value: Any, location: Location | None = None, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compose service ``volumes`` entry (short string or long-syntax object)."""
    if isinstance(value, str):
        parts = value.split(":")
        if len(parts) < 1 or len(parts) > 3 or any(not p for p in parts):
            _fail("Invalid Compose short mount syntax", location)
        source: str | None = None
        mode: str | None = None
        if len(parts) == 1:
            target = parts[0]
        else:
            source, target = parts[0], parts[1]
            mode = parts[2] if len(parts) > 2 else None
        if mode:
            options = mode.split(",")
            if any(p not in _SHORT_OPTIONS for p in options) or ("ro" in options and "rw" in options):
                _fail("Unsupported or conflicting short mount options", location)
        kind = "volume" if not source or not source.startswith((".", "/", "~")) else "bind"
        short: dict[str, Any] = {"type": kind, "target": target}
        if source is not None:
            short["source"] = source
        return _validate(short, location, fields)
    if not _is_object(value):
        _fail("Compose volume must be a string or object", location)
    option_location = fields.get("optionLocation") if fields else None
    _allowed(
        value, ("type", "source", "target", "read_only", "consistency", "bind", "volume", "tmpfs"), location, option_location
    )
    if "read_only" in value and not isinstance(value["read_only"], bool):
        _fail("read_only must be boolean", _field(fields, "read_only", location))
    if "consistency" in value and not _is_string_or_bool(value["consistency"], _CONSISTENCY):
        _fail("Unsupported consistency value", _field(fields, "consistency", location))
    for key, options in _GROUPS:
        if key not in value:
            continue
        group = value[key]
        if not (isinstance(value.get("type"), str) and value["type"] == key) or not _is_object(group):
            _fail(f"{key} options require type={key} and an object", _option_location(option_location, [key], location))
        _allowed(group, options, location, option_location, (key,))
        # Subpaths and special propagation can alter availability; deliberately refuse.
        if key == "volume" and "subpath" in group:
            _fail("volume.subpath is not evaluated", _field(fields, "subpath", location))
        if key == "bind" and "propagation" in group and not (
            isinstance(group["propagation"], str) and group["propagation"] == "rprivate"
        ):
            _fail("Only default rprivate bind propagation is supported", _field(fields, "propagation", location))
        for flag in ("create_host_path", "nocopy"):
            if flag in group and not isinstance(group[flag], bool):
                _fail(f"{flag} must be boolean", _field(fields, flag, location))
    return _validate(value, location, fields)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _validate(mount: dict[str, Any], location: Location | None, fields: dict[str, Any] | None) -> dict[str, Any]:
    fields = fields or {}
    kind = mount.get("type")
    if not _is_string_or_bool(kind, ("bind", "volume", "tmpfs")):
        _fail("Supported mount types: bind, volume, tmpfs", _field(fields, "type", location))
    if not _nonempty_string(mount.get("target")):
        _fail("Mount target is required", _field(fields, "target", location))
    has_source = "source" in mount
    if kind == "bind" and not _nonempty_string(mount.get("source")):
        _fail("Bind mount source is required", _field(fields, "source", location))
    if has_source and not _nonempty_string(mount["source"]):
        _fail("Mount source must be a nonempty string", _field(fields, "source", location))
    if kind == "tmpfs" and has_source:
        _fail("tmpfs cannot have a source", _field(fields, "source", location))
    if kind == "volume" and has_source and not _VOLUME_NAME.match(mount["source"]):
        _fail("Named volume source must be a name, not a host path", _field(fields, "source", location))
    # ``source`` is None where the JavaScript value was undefined.
    return {"type": kind, "source": mount.get("source"), "target": mount["target"]}


# Names used by the Node.js implementation.
devMount = dev_mount
composeMount = compose_mount
