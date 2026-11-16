"""Static lexical audit and explicit-input fingerprint model, never a Task runtime."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .formats import AuditError, canonical, literal, load_taskfile
from .sources import hash_sources, validate_pattern

MODEL = "task-fingerprint-audit/explicit-inputs-v1"
KIND = "task-fingerprint-audit.snapshot"
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
TEMPLATE = re.compile(r"{{\s*\.(" + IDENTIFIER + r")\s*}}")
SHELL = re.compile(r"(?<![\\$])\$(?:\{(" + IDENTIFIER + r")\}|(" + IDENTIFIER + r"))")
RUNTIME = {"CHECKSUM", "TIMESTAMP", "SOURCES", "GENERATES", "TASK", "ROOT_DIR", "TASKFILE_DIR", "TASK_VERSION", "USER_WORKING_DIR"}
ROOT_FIELDS = {"version", "vars", "env", "tasks"}
TASK_FIELDS = {"cmds", "vars", "env", "sources", "generates", "status", "desc", "summary"}
DEFINITION_EXCLUDED = {"vars", "env", "desc", "summary"}
DEFINITION_LIST_FIELDS = {"cmds", "status"}
COMMAND_FLAGS = {"silent", "ignore_error", "defer"}
COMMAND_CHILD_KEYS = {"cmd"} | COMMAND_FLAGS


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def finding(code, message, severity="warning", **context):
    return {"code": code, "severity": severity, "message": message, **context}


def unknown_fields(mapping, supported, where):
    return [finding("unsupported-field", f"{where} field is not evaluated", "unassessed", field=key)
            for key in sorted(mapping.keys() - supported)]


def declarations(data, label, findings):
    if not isinstance(data, dict):
        raise AuditError(f"{label} must be a mapping")
    result = {}
    for name, value in sorted(data.items()):
        if not re.fullmatch(IDENTIFIER, name):
            findings.append(finding("unsupported-variable-name", f"{label} name is outside the identifier subset", "unassessed", name=name))
        supported = literal(value) and not (isinstance(value, str) and ("{{" in value or "}}" in value))
        if not supported:
            findings.append(finding("nonliteral-value", f"{label} value is dynamic, templated or non-scalar; not evaluated", "unassessed", name=name))
        result[name] = (supported, value, f"{label}.{name}")
    return result


def string_list(task, field):
    value = task.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AuditError(f"{field} must be a list of strings in this subset")
    return value


def literal_paths(task, field, findings):
    patterns = []
    for pattern in string_list(task, field):
        if "{{" in pattern or "}}" in pattern:
            findings.append(finding("templated-path", f"{field} contains an unevaluated template", "unassessed"))
        else:
            validate_pattern(pattern)
            patterns.append(pattern)
    return patterns


def references(commands, findings):
    refs = set()
    assignments = set()
    for command in commands:
        for match in TEMPLATE.finditer(command):
            refs.add(("template", match.group(1)))
        without_templates = TEMPLATE.sub("", command)
        if "{{" in without_templates or "}}" in without_templates:
            findings.append(finding("unsupported-template", "command has Go-template syntax beyond literal {{.VAR}}", "unassessed"))
        for match in SHELL.finditer(without_templates):
            refs.add(("shell", match.group(1) or match.group(2)))
        # Shell-local bindings are hints only; scope/order/quoting are not interpreted.
        assignments.update(re.findall(r"(?:^|[\s;|&])(" + IDENTIFIER + r")=", without_templates))
        if re.search(r"\$\{(?!" + IDENTIFIER + r"\})", without_templates) or re.search(r"\$(?:[0-9@*#?!$-]|\()|`", without_templates):
            findings.append(finding("unsupported-shell-expansion", "command uses parameter operators, substitution or special shell parameters; not interpreted", "unassessed"))
    return sorted(refs), assignments


def audit(taskfile: Path, root: Path, env: dict | None = None, selected: list[str] | None = None) -> dict:
    return audit_document(load_taskfile(taskfile), root, env, selected)


def audit_document(data: dict, root: Path, env: dict | None = None, selected: list[str] | None = None) -> dict:
    """Audit one parsed document, also shared by the read-only suggestion report."""
    version = data.get("version")
    if type(version) not in (str, int, float) or not re.fullmatch(r"3(?:\.[0-9]+){0,2}", str(version)):
        raise AuditError("Taskfile version must be v3 (for example '3' or '3.54')")
    try:
        root = root.resolve()
    except (OSError, RuntimeError):
        raise AuditError("cannot resolve project root") from None
    if not root.is_dir():
        raise AuditError("project root must be a directory")
    tasks = data.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise AuditError("tasks must be a nonempty mapping")
    global_findings = unknown_fields(data, ROOT_FIELDS, "root")
    global_vars = declarations(data.get("vars", {}), "global.vars", global_findings)
    global_env = declarations(data.get("env", {}), "global.env", global_findings)
    env = env or {}
    if selected and any(name not in tasks for name in selected):
        raise AuditError("requested task does not exist")
    results = {}
    for name in sorted(set(selected) if selected else tasks):
        raw = tasks[name]
        if isinstance(raw, str):
            task = {"cmds": [raw]}
        elif isinstance(raw, list):
            task = {"cmds": raw}
        elif isinstance(raw, dict):
            task = raw
        else:
            raise AuditError("task must be a command string, command list or mapping")
        findings = unknown_fields(task, TASK_FIELDS, "task")
        task_vars = declarations(task.get("vars", {}), "task.vars", findings)
        task_env = declarations(task.get("env", {}), "task.env", findings)
        # Task vars are not automatically exported to shell commands.
        variables = {**global_vars, **task_vars}
        environment = {**global_env, **task_env}
        commands = []
        cmds = task.get("cmds", [])
        if not isinstance(cmds, list):
            raise AuditError("cmds must be a list")
        for cmd in cmds:
            if isinstance(cmd, str):
                commands.append(cmd)
            elif isinstance(cmd, dict):
                findings.append(finding("unsupported-command", "structured command, task call or loop is not evaluated", "unassessed"))
                # Still surface candidates in a literal cmd, without interpreting options.
                if isinstance(cmd.get("cmd"), str):
                    commands.append(cmd["cmd"])
            else:
                raise AuditError("command must be a string or mapping")
        sources = literal_paths(task, "sources", findings)
        outputs = literal_paths(task, "generates", findings)
        status = string_list(task, "status")
        for script in status:
            remainder = TEMPLATE.sub("", script)
            if "{{" in remainder or "}}" in remainder:
                findings.append(finding("unsupported-status-template", "status script has a template beyond literal {{.VAR}}; script remains unevaluated", "unassessed"))
        if "status" in task:
            findings.append(finding("native-status-unknown", "status scripts are present and were not executed; native status remains unknown", "info"))
        if outputs and not task.get("sources") and not task.get("status"):
            findings.append(finding("outputs-without-inputs", "generates has no sources or status declaration; review reproducibility inputs and a suitable native status strategy"))
        if outputs and not sources:
            findings.append(finding("no-modeled-source-inputs", "generated outputs have no modeled source content; review the input declaration"))
        refs, assignments = references(commands, findings)
        inputs = {}
        for syntax, variable in refs:
            key = f"{syntax}:{variable}"
            origin = None
            value = None
            known = False
            classification = "likely-external-input"
            if syntax == "template":
                if variable in variables:
                    known, value, origin = variables[variable]
                    classification = "declared-variable"
                elif variable in global_env:
                    known, value, origin = global_env[variable]
                    classification = "declared-environment"
                elif variable in RUNTIME:
                    classification = "task-runtime-variable"
                    findings.append(finding("runtime-variable", "known Task runtime/derived variable is not an external input; value is not evaluated", "unassessed", reference=key))
                elif variable in env:
                    known, value, origin = True, env[variable], f"snapshot.env.{variable}"
                    classification = "explicit-external-input"
            else:
                if variable in assignments:
                    classification = "possible-shell-local"
                    findings.append(finding("possible-shell-local", "lexical assignment may define this variable; shell scope/order is not evaluated", "unassessed", reference=key))
                elif variable in environment and variable in env:
                    classification = "ambiguous-environment"
                    findings.append(finding("env-precedence-unknown", "both env declaration and snapshot define this name; Task env precedence experiments are not modeled", "unassessed", reference=key))
                elif variable in environment:
                    known, value, origin = environment[variable]
                    classification = "declared-environment"
                elif variable in env:
                    known, value, origin = True, env[variable], f"snapshot.env.{variable}"
                    classification = "explicit-external-input"
            if not known and classification == "likely-external-input":
                explanation = "reference has no resolved explicit value; host environment is never read"
                if syntax == "shell" and variable in variables:
                    explanation += "; vars entries are not automatically exported to shell"
                findings.append(finding("undefined-input", explanation, reference=key))
            if known:
                findings.append(finding("potential-fingerprint-gap", "referenced value is modeled explicitly here; review whether native sources/status cover changes to this input; this is a structural heuristic, not a Task bug claim", reference=key))
            inputs[key] = {"name": variable, "syntax": syntax, "classification": classification,
                           "origin": origin, "resolved": known, "value": "<redacted>" if known else None,
                           "value_digest": digest({"type": type(value).__name__, "value": value}) if known else None}
        source_hashes = hash_sources(root, sources)
        # Declarations of values are excluded, so variable-only changes have their own explanation.
        definition = {key: val for key, val in task.items() if key not in DEFINITION_EXCLUDED}
        partial = bool(global_findings) or any(f["severity"] == "unassessed" for f in findings) or any(not v["resolved"] for v in inputs.values())
        record = {"assessment": "partial" if partial else "modeled-subset",
                  "native_execution_decision": "unknown", "native_status": "unknown",
                  "status_script_count": len(status), "source_hashes": source_hashes, "inputs": inputs,
                  "definition_digest": digest(definition), "findings": findings,
                  "definition_components": {key: digest(val) for key, val in sorted(definition.items())}}
        record["definition_components_digest"] = definition_components_digest(record)
        record["definition_entries"] = {field: [digest(entry) for entry in definition[field]]
                                        for field in sorted(DEFINITION_LIST_FIELDS & definition.keys())}
        record["definition_entries_digest"] = definition_entries_digest(record)
        record["definition_subfields"] = ({"cmds": [command_subfields(entry) for entry in cmds]}
                                          if "cmds" in definition else {})
        record["definition_subfields_digest"] = definition_subfields_digest(record)
        record["source_only_fingerprint"] = source_fingerprint(record)
        record["advisory_fingerprint"] = advisory_fingerprint(record)
        results[name] = record
    return {"kind": KIND, "schema_version": 1, "model": MODEL, "values_redacted": True,
            "native_execution_decision": "unknown", "findings": global_findings, "tasks": results}


def source_fingerprint(record):
    return digest({"model": MODEL + "/sources-only", "sources": record["source_hashes"]})


def definition_components_digest(record):
    # Supplemental explanation checksum: leave the v1 fingerprint formulas intact.
    return digest({"definition_digest": record["definition_digest"],
                   "components": record["definition_components"]})


def definition_entries_digest(record):
    return digest({"definition_components_digest": record["definition_components_digest"],
                   "entries": record["definition_entries"]})


def command_subfields(entry):
    """Project only immediate literal command/boolean keys; all else is opaque."""
    if (not isinstance(entry, dict) or not isinstance(entry.get("cmd"), str)
            or entry.keys() - COMMAND_CHILD_KEYS
            or any(type(entry[key]) is not bool for key in COMMAND_FLAGS & entry.keys())):
        return None
    return {key: digest(value) for key, value in sorted(entry.items())}


def definition_subfields_digest(record):
    return digest({"definition_entries_digest": record["definition_entries_digest"],
                   "subfields": record["definition_subfields"]})


def advisory_fingerprint(record):
    return digest({"model": MODEL, "sources": record["source_hashes"], "definition": record["definition_digest"],
                   "inputs": {key: {k: value[k] for k in ("origin", "resolved", "classification", "value_digest")}
                              for key, value in record["inputs"].items()}})


def has_concerns(report):
    return any(f["severity"] != "info" for f in report["findings"]) or any(
        t["assessment"] == "partial" or any(f["severity"] != "info" for f in t["findings"]) for t in report["tasks"].values())
