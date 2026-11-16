"""Versioned metadata inventories; no hashes, payload reads or symlink text."""

import json
import os
from pathlib import Path
import secrets

from .dimensions import extension as final_extension, validate_limits
from .filesystem import open_directory, read_regular_file
from .ignore import InputError


FORMAT = "docker-context-budget.inventory"
MODEL = "logical regular-file payload bytes; controls separate; no content or symlink target comparison"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
ENTRY_FIELDS = {"path", "kind", "logical_bytes", "role", "included_by_rules", "last_rule"}
RULE_FIELDS = {"source", "line", "raw", "pattern", "negated", "matched_path"}
METADATA_FIELDS = ("context", "dockerfile", "ignore_selection", "rules_source")


def snapshot_path(path: str | Path, context: str | Path) -> Path:
    """Require artifacts outside the scanned tree, even if ignored by rules."""
    absolute = Path(os.path.abspath(path))
    root = Path(os.path.abspath(context))
    if absolute.is_relative_to(root):
        raise InputError("snapshot file must be outside the context tree")
    return absolute


def from_report(report: dict) -> dict:
    """Capture only stable accounting metadata; never persist link_target."""
    return {
        "format": FORMAT, "schema_version": 1, "model": MODEL,
        "context": report["context"], "dockerfile": report["dockerfile"],
        "ignore_selection": report["ignore_selection"],
        "rules_source": report["baseline"]["rules_source"],
        "entries": [{key: row[key] for key in sorted(ENTRY_FIELDS)}
                    for row in report["baseline"]["entries"]],
    }


def _relative_path(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and "\x00" not in value
            and all(part not in ("", ".", "..") for part in value.split("/")))


def validate(snapshot: object) -> dict:
    """Reject incompatible/malformed metadata instead of reporting a false diff."""
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise InputError("invalid snapshot: " + message)

    require(isinstance(snapshot, dict), "expected an object")
    require(set(snapshot) == {"format", "schema_version", "model", "entries", *METADATA_FIELDS},
            "unexpected or missing fields")
    require(snapshot["format"] == FORMAT and type(snapshot["schema_version"]) is int
            and snapshot["schema_version"] == 1 and snapshot["model"] == MODEL,
            "unsupported format, schema version or accounting model")
    context = snapshot["context"]
    require(isinstance(context, str) and "\x00" not in context and context.startswith("/")
            and os.path.abspath(context) == context, "context must be an absolute normalized path")
    require(_relative_path(snapshot["dockerfile"]), "invalid Dockerfile path")
    require(snapshot["ignore_selection"] in ("none", "root", "dockerfile-specific", "explicit"),
            "invalid ignore selection")
    source = snapshot["rules_source"]
    require((source is None and snapshot["ignore_selection"] == "none")
            or (isinstance(source, str) and source.startswith("/") and "\x00" not in source
                and snapshot["ignore_selection"] != "none"), "invalid rules source")
    require(isinstance(snapshot["entries"], list), "entries must be a list")
    seen = {}
    previous = ""
    for row in snapshot["entries"]:
        require(isinstance(row, dict) and set(row) == ENTRY_FIELDS, "invalid entry fields")
        path = row["path"]
        require(_relative_path(path), "invalid entry path")
        require(path > previous, "entry paths must be unique and sorted")
        previous = path
        parent = path.rpartition("/")[0]
        require(not parent or seen.get(parent) == "directory", "missing directory parent")
        require(row["kind"] in ("file", "directory", "symlink"), "invalid entry kind")
        seen[path] = row["kind"]
        require(type(row["logical_bytes"]) is int and row["logical_bytes"] >= 0
                and (row["kind"] == "file" or row["logical_bytes"] == 0), "invalid logical bytes")
        require(row["role"] in ("payload", "dockerfile", "ignore")
                and (row["role"] == "payload" or row["kind"] == "file"), "invalid entry role")
        expected_role = "payload"
        if path == snapshot["dockerfile"]:
            expected_role = "dockerfile"
        if source == str(Path(context) / path):
            expected_role = "ignore"
        require(row["role"] == expected_role, "control role disagrees with snapshot metadata")
        require(type(row["included_by_rules"]) is bool, "invalid inclusion decision")
        rule = row["last_rule"]
        if rule is None:
            require(row["included_by_rules"], "default decision must include")
            continue
        require(isinstance(rule, dict) and set(rule) == RULE_FIELDS, "invalid rule fields")
        require(source is not None and rule["source"] == source, "rule source mismatch")
        require(type(rule["line"]) is int and rule["line"] > 0, "invalid rule line")
        require(isinstance(rule["raw"], str) and isinstance(rule["pattern"], str), "invalid rule text")
        require(type(rule["negated"]) is bool and rule["negated"] == row["included_by_rules"],
                "rule disagrees with inclusion decision")
        matched = rule["matched_path"]
        require(_relative_path(matched) and (path == matched or path.startswith(matched + "/")),
                "matched path must be entry or ancestor")
    return snapshot


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("invalid snapshot: duplicate JSON key " + repr(key))
        result[key] = value
    return result


def read_snapshot(path: Path) -> dict:
    text = read_regular_file(path, limit=MAX_SNAPSHOT_BYTES, label="snapshot file")
    try:
        return validate(json.loads(text, object_pairs_hook=_unique_object))
    except (ValueError, OverflowError) as exc:
        raise InputError(f"{path}: invalid snapshot: {exc}") from exc


def write_snapshot(path: Path, snapshot: dict) -> None:
    """Publish a complete snapshot exclusively, without replacing any file."""
    validate(snapshot)
    data = (json.dumps(snapshot, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise InputError("snapshot exceeds 64 MiB limit")
    parent_fd = open_directory(path.parent)
    temporary = ".docker-context-budget-" + secrets.token_hex(12)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # link() publishes atomically and refuses existing files/symlinks.
            os.link(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _payload(row: dict | None) -> tuple[int, int]:
    included = bool(row and row["kind"] == "file" and row["role"] == "payload"
                    and row["included_by_rules"])
    return (row["logical_bytes"] if included else 0, int(included))


def _summary(snapshot: dict) -> dict:
    counts = [_payload(row) for row in snapshot["entries"]]
    return {**{key: snapshot[key] for key in METADATA_FIELDS},
            "included_payload_bytes": sum(size for size, _ in counts),
            "included_payload_files": sum(count for _, count in counts)}


def inspect_snapshot(snapshot: dict, path: str | None = None, *,
                     subtree: str | None = None, extension: str | None = None,
                     subtree_extension: tuple[str, str] | None = None,
                     decision: str | None = None,
                     limit: int | None = None) -> dict:
    """Validate and account completely before exact metadata selection and limiting."""
    validate(snapshot)
    if sum(value is not None for value in (path, subtree, extension, subtree_extension, decision)) > 1:
        raise InputError("--path, --subtree, --extension, --subtree-extension and --decision are mutually exclusive")
    if decision is not None and (type(decision) is not str or decision not in ("included", "excluded")):
        raise InputError("inspection decision must be exactly 'included' or 'excluded'")
    if limit is not None and all(value is None for value in (subtree, extension, subtree_extension, decision)):
        raise InputError("--limit requires --subtree, --extension, --subtree-extension or --decision")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise InputError("inspection limit must be a nonnegative integer")
    inspection = {"format": snapshot["format"], "schema_version": snapshot["schema_version"],
                  "summary": _summary(snapshot), "entry_count": len(snapshot["entries"])}
    if path is not None:
        if not _relative_path(path):
            raise InputError("inspection path must be an exact normalized context-relative entry path")
        for row in snapshot["entries"]:
            if row["path"] == path:
                inspection["selected_entry"] = row
                break
        else:
            raise InputError(f"no recorded entry for inspection path: {path!r}")
    if subtree is not None:
        if not _relative_path(subtree):
            raise InputError("inspection subtree must be an exact normalized context-relative directory path")
        if not any(row["path"] == subtree and row["kind"] == "directory"
                   for row in snapshot["entries"]):
            raise InputError(f"no recorded directory for inspection subtree: {subtree!r}")
        rows = [row for row in snapshot["entries"]
                if row["path"] == subtree or row["path"].startswith(subtree + "/")]
        counts = [_payload(row) for row in rows]
        returned = rows[:100 if limit is None else limit]
        inspection["selected_subtree"] = {
            "path": subtree, "matching_entry_count": len(rows),
            "included_payload_bytes": sum(size for size, _ in counts),
            "included_payload_files": sum(count for _, count in counts),
            "entries": returned, "returned_entry_count": len(returned),
            "truncated": len(returned) < len(rows),
        }
    if extension is not None or subtree_extension is not None:
        directory = None
        if subtree_extension is not None:
            if not isinstance(subtree_extension, tuple) or len(subtree_extension) != 2:
                raise InputError("inspection subtree extension must be a (directory, extension) tuple")
            directory, extension = subtree_extension
            if not _relative_path(directory):
                raise InputError("inspection subtree extension requires an exact normalized context-relative directory path")
            if not any(row["path"] == directory and row["kind"] == "directory"
                       for row in snapshot["entries"]):
                raise InputError(f"no recorded directory for inspection subtree extension: {directory!r}")
        if not isinstance(extension, str):
            raise InputError("inspection extension must be an exact final suffix or <none>")
        validate_limits({extension: 0}, directory=False)
        rows = [row for row in snapshot["entries"]
                if row["kind"] == "file" and final_extension(row["path"]) == extension
                and (directory is None or row["path"].startswith(directory + "/"))]
        counts = [_payload(row) for row in rows]
        returned = rows[:100 if limit is None else limit]
        key = "selected_extension" if directory is None else "selected_subtree_extension"
        inspection[key] = {
            **({"path": directory} if directory is not None else {}),
            "extension": extension, "matching_entry_count": len(rows),
            "included_payload_bytes": sum(size for size, _ in counts),
            "included_payload_files": sum(count for _, count in counts),
            "entries": returned, "returned_entry_count": len(returned),
            "truncated": len(returned) < len(rows),
        }
    if decision is not None:
        rows = [row for row in snapshot["entries"] if row["included_by_rules"] is (decision == "included")]
        counts = [_payload(row) for row in rows]
        returned = rows[:100 if limit is None else limit]
        inspection["selected_decision"] = {
            "decision": decision, "matching_entry_count": len(rows),
            "included_payload_bytes": sum(size for size, _ in counts),
            "included_payload_files": sum(count for _, count in counts),
            "entries": returned, "returned_entry_count": len(returned),
            "truncated": len(returned) < len(rows),
        }
    return inspection


def compare_snapshots(baseline: dict, current: dict) -> dict:
    validate(baseline)
    validate(current)
    for key in ("context", "dockerfile"):
        if baseline[key] != current[key]:
            raise InputError(f"snapshot {key} differs between inventories; use the same context and Dockerfile")
    before_rows = {row["path"]: row for row in baseline["entries"]}
    after_rows = {row["path"]: row for row in current["entries"]}
    changes = []
    for path in sorted(before_rows.keys() | after_rows.keys()):
        before, after = before_rows.get(path), after_rows.get(path)
        if before == after:
            continue
        if before is None:
            reasons = ["added"]
        elif after is None:
            reasons = ["removed"]
        else:
            reasons = [reason for field, reason in (
                ("kind", "kind_changed"), ("logical_bytes", "size_changed"),
                ("role", "role_changed"), ("included_by_rules", "inclusion_changed"),
                ("last_rule", "rule_changed")) if before[field] != after[field]]
        before_bytes, before_count = _payload(before)
        after_bytes, after_count = _payload(after)
        changes.append({"path": path, "reasons": reasons, "before": before, "after": after,
                        "included_payload_bytes_delta": after_bytes - before_bytes,
                        "included_payload_files_delta": after_count - before_count})
    before_summary, after_summary = _summary(baseline), _summary(current)
    return {"baseline": before_summary, "current": after_summary,
            "included_payload_bytes_delta": after_summary["included_payload_bytes"] - before_summary["included_payload_bytes"],
            "included_payload_files_delta": after_summary["included_payload_files"] - before_summary["included_payload_files"],
            "changes": changes}
