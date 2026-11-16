"""Discover or render referenced names without copying values into templates."""

import json
import os
from pathlib import Path

from .trace import TraceError, _trace, valid_name
from .scenarios import load_scenarios


RULES = {
    "direct": "direct reference; unset substitutes empty with a warning",
    "-": "default when unset; empty is preserved",
    ":-": "default when unset or empty",
    "?": "required when unset; empty allowed",
    ":?": "required when unset or empty",
    "+": "alternative when set, including empty; unset produces empty",
    ":+": "alternative when nonempty; unset or empty produces empty",
    "inherited-null": "optional inheritance; unset removes declaration; empty is preserved",
    "inherited-key-only": "optional inheritance; unset removes declaration; empty is preserved",
}


def _required_kind(operators) -> str:
    return "nonempty" if ":?" in operators else "set" if "?" in operators else "no"


def _validate_completeness_option(require_complete: bool) -> None:
    if not isinstance(require_complete, bool):
        raise TraceError("require_complete must be a boolean (value redacted)")


def _check_completeness(report: dict, require_complete: bool) -> dict:
    if require_complete and not report["complete"]:
        raise TraceError("syntax-only discovery incomplete: dynamic file contents or "
                         "environment declaration names remain unresolved")
    return report


def discover_template(compose: str | Path, env_files: list[str | Path] | None = None, *,
                      service_names: list[str] | None = None,
                      require_complete: bool = False) -> dict:
    """Discover syntax references without evaluating inputs or dynamic file paths.

    Literal service env_files and explicit dotenv inputs are read strictly. Every
    variable-dependent env_file is reported as unresolved, even with a fallback or
    supplied value. Variable-dependent environment list names retain references
    but are unresolved scopes, too; no assignment/inherited name is guessed.
    service_names selects unique literal services in Compose order; None scans all.
    Explicit dotenv/top-level references remain shared. Explicit selection validates
    the whole document before reading dotenv; unselected service files stay unopened.
    No shell snapshot, host environment or implicit .env is used.
    require_complete refuses unresolved scopes after strict input reads succeed;
    it never attempts to resolve them. No partial report is returned on failure.
    """
    _validate_completeness_option(require_complete)
    report, _ = _trace(compose, env_files, syntax_only=True, service_names=service_names)
    return _check_completeness(report, require_complete)


def discover_scenarios(compose: str | Path, scenario_file: str | Path, *,
                       scenario_names: list[str] | None = None,
                       service_names: list[str] | None = None,
                       require_complete: bool = False) -> dict:
    """Discover selected independent checklists, preserving manifest order.

    None selects all; otherwise require a nonempty list of unique known names.
    Validate the whole strict manifest, all shell restrictions and selection before
    reading Compose or selected inputs. Unselected inputs remain unopened. No
    reports escape if any selected scenario fails. Completeness covers selected
    children only; no comparison is performed. require_complete checks aggregate
    coverage after all selected strict reads succeed, before returning any report.
    service_names applies the same Compose service scope independently to each child.
    """
    _validate_completeness_option(require_complete)
    definitions = load_scenarios(Path(scenario_file))
    for name, options in definitions.items():
        if options["shell_env"] is not None:
            raise TraceError(f"scenario {name}: syntax-only discovery does not accept shell snapshots")
    if scenario_names is not None:
        if not isinstance(scenario_names, list) or not scenario_names:
            raise TraceError("scenario selection must be a nonempty list of unique known names (names redacted)")
        selected = set()
        for name in scenario_names:
            if not isinstance(name, str):
                raise TraceError("scenario selection names must be strings (names redacted)")
            valid_name(name, "scenario selection")
            if name not in definitions or name in selected:
                raise TraceError("scenario selection requires unique known names (names redacted)")
            selected.add(name)
    else:
        selected = set(definitions)
    reports = {}
    for name, options in definitions.items():
        if name not in selected:
            continue
        try:
            reports[name] = discover_template(compose, options["env_files"], service_names=service_names)
        except (TraceError, RecursionError) as error:
            message = str(error) if isinstance(error, TraceError) else "input nesting exceeds supported parser depth"
            raise TraceError(f"scenario {name}: {message}") from None
    report = {"schema_version": 1, "report_type": "scenario-template-discovery",
            "evaluation": "syntax-only", "values_shown": False,
            "host_environment": "not-read", "implicit_dotenv": "not-read",
            "complete": all(report["complete"] for report in reports.values()),
            "scenarios": reports}
    return _check_completeness(report, require_complete)


def env_template(report: dict) -> str:
    """Return comment-only checklists for trace or single/named discovery reports.

    All evaluated (trace) or syntax-only (discovery) references count, including
    shadowed declarations, explicit
    interpolation-file dependencies and service-local dotenv references. Supplied
    but unreferenced names, literal dollars and container output assignments do not.
    Values, operand text and custom error messages are never consulted.
    """
    if report.get("schema_version") == 1 and report.get("report_type") == "scenario-template-discovery":
        sections = ["# compose-env-trace: named scenario discovery (values omitted)",
                    "# Each scenario is an independent checklist; nothing is loaded automatically."]
        for name, child in report["scenarios"].items():
            valid_name(name, "env template scenario")
            if child.get("report_type") != "env-template-discovery":
                raise TraceError("env template requires syntax-only scenario discovery reports")
            sections.extend(["", f"# Scenario {name}; complete: {str(child['complete']).lower()}",
                             env_template(child).rstrip("\n")])
        return "\n".join(sections) + "\n"
    discovery = report.get("report_type") == "env-template-discovery"
    if report.get("schema_version") != 1 or ("report_type" in report and not discovery):
        raise TraceError("env template requires a single trace or template discovery report")
    references: dict[str, dict] = {}

    def add(name, operator, phase, item):
        valid_name(name, "env template")
        row = references.setdefault(name, {"operators": set(), "sources": {}})
        row["operators"].add(operator)
        # JSON quoting keeps caller-supplied metadata on one comment line, even
        # when a field name or input filename contains newline/control characters.
        source = (f"# use: {phase}; {operator}: {RULES[operator]}; field: "
                  f"{json.dumps(item['field_path'], ensure_ascii=True)}; at: "
                  f"{json.dumps(item['location'], ensure_ascii=True)}")
        row["sources"][source] = None

    if discovery:
        for use in report["references"]:
            add(use["variable"], use["operator"], use["phase"], use)
    else:
        for use in report["interpolation"]["uses"]:
            if "variable" in use:
                add(use["variable"], use["operator"], use["phase"], use)
        for service in report["services"].values():
            for name, history in service["variables"].items():
                for entry in [*history["shadowed"], history["winner"]]:
                    if entry["syntax"] in ("inherited-null", "inherited-key-only"):
                        add(name, entry["syntax"], "compose", entry)

    lines = [
        "# compose-env-trace: referenced-variable template (values omitted)",
        "# All assignments are commented: this file initially leaves every name UNSET.",
        "# Uncomment NAME= only for intentional EMPTY; provide a value for NONEMPTY.",
        "# Defaults, alternatives, supplied values and required messages are omitted.",
        "# Required describes expression constraints, not the current input state.",
        "# Uses include evaluated/shadowed inputs and service-local dotenv dependencies.",
        "# Review scopes: a global input can override a service-local lookup.",
        "# No host environment is read or changed; nothing is loaded automatically.",
    ]
    if discovery:
        lines[5] = "# Uses are syntax-only; no operators or required-input checks were evaluated."
        if "scope" in report:
            lines.append("# Selected services: " + json.dumps(report["scope"]["services"], ensure_ascii=True))
            lines.append("# Shared scope: explicit dotenv inputs and top-level Compose values; other services excluded.")
        lines.append("# Dynamic env_file paths are not opened; their contents are outside this checklist.")
        for item in report["service_env_files"]:
            if item["status"] == "unresolved-dynamic":
                lines.append("# Unresolved dynamic env_file; field: " +
                             json.dumps(item["field_path"], ensure_ascii=True) + "; at: " +
                             json.dumps(item["location"], ensure_ascii=True))
        for item in report.get("unresolved_environment", []):
            lines.append("# Unresolved dynamic environment declaration; field: " +
                         json.dumps(item["field_path"], ensure_ascii=True) + "; at: " +
                         json.dumps(item["location"], ensure_ascii=True) +
                         "; assignment/inheritance name unknown.")
    for name, row in sorted(references.items()):
        operators = row["operators"]
        required = _required_kind(operators)
        if required == "set":
            required = "set (empty allowed)"
        lines.extend(["", f"# variable: {name}; required: {required}",
                      *row["sources"], f"# {name}="])
    return "\n".join(lines) + "\n"


def write_env_template(path: str | Path, template: str) -> None:
    """Create a private file exclusively, refusing existing files and symlinks."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except (OSError, ValueError):
        raise TraceError("env template: cannot create new output file; existing paths are never overwritten") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(template)
    except (OSError, UnicodeError):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        raise TraceError("env template: cannot write output file") from None
