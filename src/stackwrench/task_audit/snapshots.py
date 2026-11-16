"""Strict snapshot validation and change attribution; no shell or Task calls."""
from __future__ import annotations

import re
from pathlib import Path

from .audit import (COMMAND_CHILD_KEYS, COMMAND_FLAGS, DEFINITION_EXCLUDED, DEFINITION_LIST_FIELDS, KIND, MODEL,
                    advisory_fingerprint, definition_components_digest,
                    definition_entries_digest, definition_subfields_digest, digest, source_fingerprint)
from .formats import AuditError, load_json

HEX = re.compile(r"[a-f0-9]{64}")
TOP = {"kind", "schema_version", "model", "values_redacted", "native_execution_decision", "findings", "tasks"}
RECORD = {"assessment", "native_execution_decision", "native_status", "status_script_count", "source_hashes", "inputs", "definition_digest", "findings", "source_only_fingerprint", "advisory_fingerprint"}
COMPONENT_RECORD = RECORD | {"definition_components", "definition_components_digest"}
ENTRY_RECORD = COMPONENT_RECORD | {"definition_entries", "definition_entries_digest"}
SUBFIELD_RECORD = ENTRY_RECORD | {"definition_subfields", "definition_subfields_digest"}
BOOL_DIGESTS = {digest(False), digest(True)}
INPUT = {"name", "syntax", "classification", "origin", "resolved", "value", "value_digest"}
CLASSES = {"likely-external-input", "declared-variable", "task-runtime-variable", "explicit-external-input", "declared-environment", "ambiguous-environment", "possible-shell-local"}


def fail():
    raise AuditError("malformed or incompatible snapshot")


def is_hash(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def validate_findings(findings):
    if not isinstance(findings, list):
        fail()
    for entry in findings:
        if not isinstance(entry, dict) or not {"code", "severity", "message"} <= entry.keys() or entry.keys() - {"code", "severity", "message", "field", "name", "reference"}:
            fail()
        if entry["severity"] not in ("info", "warning", "unassessed") or any(not isinstance(v, str) for v in entry.values()):
            fail()


def validate_snapshot(data):
    if not isinstance(data, dict) or data.keys() != TOP:
        fail()
    if data["kind"] != KIND or type(data["schema_version"]) is not int or data["schema_version"] != 1 or data["model"] != MODEL or data["values_redacted"] is not True or data["native_execution_decision"] != "unknown":
        fail()
    validate_findings(data["findings"])
    if not isinstance(data["tasks"], dict) or not data["tasks"]:
        fail()
    for name, record in data["tasks"].items():
        if not isinstance(name, str) or not isinstance(record, dict) or record.keys() not in (RECORD, COMPONENT_RECORD, ENTRY_RECORD, SUBFIELD_RECORD):
            fail()
        if record["assessment"] not in ("partial", "modeled-subset") or record["native_execution_decision"] != "unknown" or record["native_status"] != "unknown" or type(record["status_script_count"]) is not int or record["status_script_count"] < 0:
            fail()
        if not all(is_hash(record[k]) for k in ("definition_digest", "source_only_fingerprint", "advisory_fingerprint")):
            fail()
        if "definition_components" in record:
            components = record["definition_components"]
            if not isinstance(components, dict) or any(
                    not isinstance(field, str) or field in DEFINITION_EXCLUDED or not is_hash(sha)
                    for field, sha in components.items()) or not is_hash(record["definition_components_digest"]):
                fail()
            if record["definition_components_digest"] != definition_components_digest(record):
                raise AuditError("snapshot definition component integrity check failed")
        if "definition_entries" in record:
            entries = record["definition_entries"]
            if not isinstance(entries, dict) or entries.keys() != DEFINITION_LIST_FIELDS & components.keys():
                fail()
            for field, hashes in entries.items():
                if not isinstance(hashes, list) or any(not is_hash(sha) for sha in hashes):
                    fail()
                # Empty lists and the recorded status count can be cross-checked
                # without retaining any raw definition values.
                if (not hashes) != (components[field] == digest([])):
                    fail()
            if len(entries.get("status", [])) != record["status_script_count"]:
                fail()
            if not is_hash(record["definition_entries_digest"]):
                fail()
            if record["definition_entries_digest"] != definition_entries_digest(record):
                raise AuditError("snapshot definition entry integrity check failed")
        if "definition_subfields" in record:
            subfields = record["definition_subfields"]
            if not isinstance(subfields, dict) or subfields.keys() != {"cmds"} & entries.keys():
                fail()
            for field, children in subfields.items():
                if not isinstance(children, list) or len(children) != len(entries[field]):
                    fail()
                for child in children:
                    if child is None:
                        continue
                    if (not isinstance(child, dict) or "cmd" not in child or child.keys() - COMMAND_CHILD_KEYS
                            or any(not is_hash(sha) for sha in child.values())
                            or any(child[key] not in BOOL_DIGESTS for key in COMMAND_FLAGS & child.keys())):
                        fail()
            if not is_hash(record["definition_subfields_digest"]):
                fail()
            if record["definition_subfields_digest"] != definition_subfields_digest(record):
                raise AuditError("snapshot definition subfield integrity check failed")
        validate_findings(record["findings"])
        if not isinstance(record["source_hashes"], dict):
            fail()
        for path, sha in record["source_hashes"].items():
            if not isinstance(path, str) or not is_hash(sha):
                fail()
            if path.startswith("/") or "\x00" in path or any(part in ("", ".", "..") for part in path.split("/")):
                fail()
        if not isinstance(record["inputs"], dict):
            fail()
        for key, val in record["inputs"].items():
            if not isinstance(val, dict) or val.keys() != INPUT or val["syntax"] not in ("shell", "template") or not isinstance(val["name"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", val["name"]):
                fail()
            if key != f'{val["syntax"]}:{val["name"]}' or not isinstance(val["classification"], str) or val["classification"] not in CLASSES or type(val["resolved"]) is not bool:
                fail()
            if val["origin"] is not None and not isinstance(val["origin"], str):
                fail()
            if val["resolved"]:
                if not is_hash(val["value_digest"]) or val["value"] != "<redacted>" or not val["origin"]:
                    fail()
            elif val["value_digest"] is not None or val["value"] is not None:
                fail()
        if record["source_only_fingerprint"] != source_fingerprint(record) or record["advisory_fingerprint"] != advisory_fingerprint(record):
            raise AuditError("snapshot fingerprint integrity check failed")
    return data


def read_snapshot(path: Path):
    return validate_snapshot(load_json(path, 16 * 1024 * 1024))


def map_changes(before, after):
    return [{"name": key, "change": "added" if key not in before else "removed" if key not in after else "changed"}
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]


def explain_inputs(before, after, changes):
    details = []
    for change in changes:
        old, new = before.get(change["name"]), after.get(change["name"])
        # Membership is distinct from an unresolved value (whose digest is null).
        fields = ["presence"] if old is None or new is None else [
            field for field in ("classification", "origin", "resolved", "value_digest") if old[field] != new[field]]
        details.append({**change, "changed_fields": fields,
                        "before": old.copy() if old is not None else None,
                        "after": new.copy() if new is not None else None})
    return details


def comparison_context(snapshot, record):
    if record is None:
        return None
    return {"assessment": record["assessment"],
            "unresolved_inputs": {key: {field: value[field] for field in ("classification", "origin")}
                                  for key, value in sorted(record["inputs"].items()) if not value["resolved"]},
            "unassessed_findings": [{"scope": scope, **entry}
                                    for scope, findings in (("root", snapshot["findings"]), ("task", record["findings"]))
                                    for entry in findings if entry["severity"] == "unassessed"]}


def explain_definition(before, after):
    # An absent task has no fields; a legacy task has unknown component metadata.
    if any(record is not None and "definition_components" not in record for record in (before, after)):
        return None
    old = before["definition_components"] if before is not None else {}
    new = after["definition_components"] if after is not None else {}
    return [{"field": change["name"], "change": change["change"],
             "before_digest": old.get(change["name"]), "after_digest": new.get(change["name"])}
            for change in map_changes(old, new)]


def definition_entry_context(record):
    if record is None:
        return {"state": "task-absent", "fields": None}
    if "definition_entries" not in record:
        return {"state": "metadata-unavailable", "fields": None}
    return {"state": "available", "fields": sorted(record["definition_entries"])}


def explain_definition_entries(before, after):
    if any(record is not None and "definition_entries" not in record for record in (before, after)):
        return None
    old = before["definition_entries"] if before is not None else {}
    new = after["definition_entries"] if after is not None else {}
    changes = []
    for field in sorted(old.keys() | new.keys()):
        old_hashes, new_hashes = old.get(field, []), new.get(field, [])
        for index in range(max(len(old_hashes), len(new_hashes))):
            left = old_hashes[index] if index < len(old_hashes) else None
            right = new_hashes[index] if index < len(new_hashes) else None
            if left != right:
                changes.append({"location": [field, index],
                                "change": "added" if left is None else "removed" if right is None else "changed",
                                "before_digest": left, "after_digest": right})
    return changes


def definition_subfield_context(record):
    if record is None:
        return {"state": "task-absent", "fields": None}
    if "definition_subfields" not in record:
        return {"state": "metadata-unavailable", "fields": None}
    return {"state": "available", "fields": {
        field: [sorted(child) if child is not None else None for child in children]
        for field, children in sorted(record["definition_subfields"].items())}}


def explain_definition_subfields(before, after):
    if any(record is not None and "definition_subfields" not in record for record in (before, after)):
        return None
    old = before["definition_subfields"].get("cmds", []) if before is not None else []
    new = after["definition_subfields"].get("cmds", []) if after is not None else []
    changes = []
    for index in range(max(len(old), len(new))):
        # An opaque entry has no supported projection, not necessarily no raw keys.
        left = (old[index] or {}) if index < len(old) else {}
        right = (new[index] or {}) if index < len(new) else {}
        for change in map_changes(left, right):
            key = change["name"]
            changes.append({"location": ["cmds", index, key], "change": change["change"],
                            "before_digest": left.get(key), "after_digest": right.get(key)})
    return changes


def compare(before, after):
    validate_snapshot(before)
    validate_snapshot(after)
    result = {}
    for name in sorted(before["tasks"].keys() | after["tasks"].keys()):
        old = before["tasks"].get(name)
        new = after["tasks"].get(name)
        context_before, context_after = comparison_context(before, old), comparison_context(after, new)
        definition_changes = explain_definition(old, new)
        entry_changes = explain_definition_entries(old, new)
        entry_before, entry_after = definition_entry_context(old), definition_entry_context(new)
        subfield_changes = explain_definition_subfields(old, new)
        subfield_before, subfield_after = definition_subfield_context(old), definition_subfield_context(new)
        membership = "added" if old is None else "removed" if new is None else None
        old, new = old or {}, new or {}
        old_inputs, new_inputs = old.get("inputs", {}), new.get("inputs", {})
        variable_changes = map_changes(old_inputs, new_inputs)
        source_changes = map_changes(old.get("source_hashes", {}), new.get("source_hashes", {}))
        definition_changed = old.get("definition_digest") != new.get("definition_digest")
        assessment_changed = old.get("assessment") != new.get("assessment") or old.get("findings") != new.get("findings") or before["findings"] != after["findings"]
        reasons = ["task-membership"] if membership else []
        if source_changes:
            reasons.append("source-content-or-membership")
        if variable_changes:
            reasons.append("referenced-explicit-input-or-resolution")
        if definition_changed:
            reasons.append("task-definition")
        if assessment_changed:
            reasons.append("assessment-or-findings")
        result[name] = {"change": membership or ("changed" if reasons else "unchanged"), "reasons": reasons,
                        "source_only_changed": old.get("source_only_fingerprint") != new.get("source_only_fingerprint"),
                        "advisory_changed": old.get("advisory_fingerprint") != new.get("advisory_fingerprint"),
                        "source_changes": source_changes, "variable_changes": variable_changes,
                        "input_changes": explain_inputs(old_inputs, new_inputs, variable_changes),
                        "definition_changes": definition_changes,
                        "definition_changes_complete": definition_changes is not None,
                        "definition_entry_changes": entry_changes,
                        "definition_entry_changes_complete": entry_changes is not None,
                        "definition_entry_context_before": entry_before,
                        "definition_entry_context_after": entry_after,
                        "definition_subfield_changes": subfield_changes,
                        "definition_subfield_changes_complete": subfield_changes is not None,
                        "definition_subfield_context_before": subfield_before,
                        "definition_subfield_context_after": subfield_after,
                        "definition_changed": definition_changed, "assessment_changed": assessment_changed,
                        "assessment_before": old.get("assessment"), "assessment_after": new.get("assessment"),
                        "context_before": context_before, "context_after": context_after,
                        "native_execution_decision": "unknown"}
    return {"kind": "task-fingerprint-audit.comparison", "model": MODEL, "native_execution_decision": "unknown",
            "cache_reuse_authorized": False, "tasks": result}
