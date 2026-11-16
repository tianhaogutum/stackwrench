"""Read the explicit inventory trust boundary and real CRI JSON image fields."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from .inputs import InputError, mapping, read_json, string
from .references import parse_reference


@dataclass
class Node:
    name: str
    snapshot: str | None
    # None is an omitted capture; an empty dict is a captured, empty inventory.
    references: dict[str, list[dict]] | None
    schedulable: bool = True
    captured_at: str | None = None
    source: str | None = None


@dataclass
class Inventory:
    path: Path
    cluster: str
    nodes: list[Node]

    @property
    def schedulable_nodes(self) -> list[Node]:
        return [node for node in self.nodes if node.schedulable]

    @property
    def excluded(self) -> list[str]:
        return [node.name for node in self.nodes if not node.schedulable]


def _keys(obj: dict, allowed: set, where: str):
    unknown = obj.keys() - allowed
    if unknown:
        raise InputError(f"{where}: unknown fields {sorted(unknown)}")


def _safe_name(value, where: str) -> str:
    value = string(value, where)
    if len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise InputError(f"{where}: expected a lowercase node/cluster name (letters, digits, dots, hyphens)")
    return value


def read_snapshot(path: Path) -> dict[str, list[dict]]:
    data = mapping(read_json(path), str(path))
    _keys(data, {"images"}, str(path))
    images = data.get("images")
    if not isinstance(images, list):
        raise InputError(f"{path}: expected crictl JSON with an 'images' array")
    refs = {}
    for index, image in enumerate(images):
        where = f"{path}: images[{index}]"
        image = mapping(image, where)
        _keys(image, {"id", "repoTags", "repoDigests", "size", "uid", "username", "spec", "pinned"}, where)
        string(image.get("id"), f"{where}.id")
        for field in ("repoTags", "repoDigests"):
            # Protobuf JSON can omit empty repeated fields or encode them as null.
            values = image.get(field)
            if values is None:
                values = []
            if not isinstance(values, list):
                raise InputError(f"{where}.{field}: expected an array or null")
            for raw in values:
                ref = parse_reference(raw, f"{where}.{field}")
                if field == "repoTags" and (ref.digest or not ref.tag):
                    raise InputError(f"{where}.repoTags: expected explicit tag references")
                if field == "repoDigests" and (not ref.digest or ref.tag):
                    raise InputError(f"{where}.repoDigests: expected repository@digest references without tags")
                evidence = {"imageIndex": index, "field": field, "reference": raw}
                refs.setdefault(ref.canonical, []).append(evidence)
    return refs


def _captured_at(value, where: str) -> str:
    value = string(value, where)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value):
        raise InputError(f"{where}: expected an RFC3339 timestamp with timezone and seconds")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{where}: invalid timestamp: {exc}") from exc
    return value


def read_inventory_details(path: Path) -> Inventory:
    data = mapping(read_json(path), str(path))
    _keys(data, {"schemaVersion", "cluster", "nodes"}, str(path))
    if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        raise InputError(f"{path}: schemaVersion must be integer 1")
    cluster = _safe_name(data.get("cluster"), f"{path}: cluster")
    entries = mapping(data.get("nodes"), f"{path}: nodes")
    nodes = []
    for name, entry in sorted(entries.items()):
        _safe_name(name, f"{path}: node name")
        where = f"{path}: nodes.{name}"
        entry = mapping(entry, where)
        _keys(entry, {"snapshot", "schedulable", "capturedAt", "source"}, where)
        if type(entry.get("schedulable")) is not bool:
            raise InputError(f"{where}.schedulable: explicit boolean is required")
        raw_path = None
        refs = None
        if "snapshot" in entry:
            raw_path = string(entry.get("snapshot"), f"{where}.snapshot")
            snapshot_path = Path(raw_path)
            if not snapshot_path.is_absolute():
                snapshot_path = path.parent / snapshot_path
            refs = read_snapshot(snapshot_path)
        captured_at = _captured_at(entry["capturedAt"], f"{where}.capturedAt") if "capturedAt" in entry else None
        source = string(entry["source"], f"{where}.source") if "source" in entry else None
        if raw_path is None and (captured_at is not None or source is not None):
            raise InputError(f"{where}: capturedAt/source require a snapshot")
        nodes.append(Node(name, raw_path, refs, entry["schedulable"], captured_at, source))
    inventory = Inventory(path, cluster, nodes)
    if not inventory.schedulable_nodes:
        raise InputError(f"{path}: at least one explicitly schedulable node is required")
    return inventory


def read_inventory(path: Path) -> tuple[str, list[Node], list[str]]:
    inventory = read_inventory_details(path)
    return inventory.cluster, inventory.schedulable_nodes, inventory.excluded
