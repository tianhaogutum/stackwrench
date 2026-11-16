"""Pure, allowlisted structured projections of syntax-only discovery reports."""

from .template import RULES, _required_kind
from .trace import TraceError, valid_name


_SHARED = ["explicit-dotenv-inputs", "top-level-compose-values"]
_ERROR = "checklist requires a valid syntax-only discovery report"


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError


def _string(value) -> str:
    _require(isinstance(value, str))
    return value


def _array(value) -> list:
    _require(isinstance(value, list))
    return value


def _source(item: dict) -> dict:
    _require(isinstance(item, dict))
    path = _array(item["field_path"])
    _require(all(isinstance(part, str) or type(part) is int and part >= 0 for part in path))
    return {"field_path": list(path), "location": _string(item["location"])}


def _common(report: dict, report_type: str) -> dict:
    _require(isinstance(report, dict))
    _require(type(report["schema_version"]) is int and report["schema_version"] == 1)
    _require(report["report_type"] == report_type and report["evaluation"] == "syntax-only")
    _require(report["values_shown"] is False and report["host_environment"] == "not-read"
             and report["implicit_dotenv"] == "not-read" and type(report["complete"]) is bool)
    return {"schema_version": 1, "report_type": report_type.replace("discovery", "checklist"),
            "evaluation": "syntax-only", "values_shown": False,
            "host_environment": "not-read", "implicit_dotenv": "not-read",
            "complete": report["complete"]}


def _single(report: dict) -> dict:
    result = _common(report, "env-template-discovery")
    scope = {"service_selection": "all", "services": None, "shared": list(_SHARED)}
    if "scope" in report:
        selected = report["scope"]
        services = [_string(name) for name in _array(selected["services"])]
        _require(bool(services) and all(name and "\0" not in name for name in services)
                 and len(set(services)) == len(services) and selected["shared"] == _SHARED)
        scope.update(service_selection="selected", services=services)
    rows = {}
    for use in _array(report["references"]):
        source = _source(use)
        name = use["variable"]
        valid_name(name, "checklist")
        operator, phase = use["operator"], use["phase"]
        _require(operator in RULES and phase in ("compose", "interpolation-input", "service-env-file"))
        source.update(operator=operator, phase=phase)
        if "offset" in use:
            _require(type(use["offset"]) is int and use["offset"] >= 0)
            source["offset"] = use["offset"]
        row = rows.setdefault(name, {"operators": set(), "sources": []})
        row["operators"].add(operator)
        row["sources"].append(source)
    files = []
    for item in _array(report["service_env_files"]):
        source = _source(item)
        _require(type(item["required"]) is bool and item["format"] in ("compose", "raw")
                 and item["status"] in ("scanned", "skipped-optional-missing", "unresolved-dynamic"))
        source.update(service=_string(item["service"]), required=item["required"],
                      format=item["format"], status=item["status"])
        if item["status"] == "skipped-optional-missing":
            _require(item["required"] is False and item["reason"] == "missing")
            source["reason"] = "missing"
        files.append(source)
    unresolved = []
    for item in _array(report["unresolved_environment"]):
        source = _source(item)
        _require(item["status"] == "unresolved-dynamic")
        source.update(service=_string(item["service"]), status="unresolved-dynamic")
        unresolved.append(source)
    _require(result["complete"] == (not unresolved and all(f["status"] != "unresolved-dynamic" for f in files)))
    return {**result, "scope": scope,
            "variables": [{"variable": name, "operators": sorted(row["operators"]),
                           "required": _required_kind(row["operators"]), "sources": row["sources"]}
                          for name, row in sorted(rows.items())],
            "service_env_files": files, "unresolved_environment": unresolved}


def discovery_checklist(report: dict) -> dict:
    """Summarize a v1 single/named discovery report without I/O or evaluation.

    Variables sort by name; operators are sorted/distinct; every original source
    stays in discovery order with optional offset. Required is nonempty for :?,
    set (empty allowed) for ?, and no otherwise. Named scenarios stay independent
    in manifest order. Coverage describes selected/shared or all-service syntax,
    never runnability. Only documented redacted metadata is copied; extra keys
    (including values) are ignored. Returns detached lists/dicts; does not mutate
    the report. Wrong/malformed discovery reports raise a fixed redacted TraceError.
    """
    try:
        _require(isinstance(report, dict))
        if report.get("report_type") != "scenario-template-discovery":
            return _single(report)
        result = _common(report, "scenario-template-discovery")
        scenarios = report["scenarios"]
        _require(isinstance(scenarios, dict) and 1 <= len(scenarios) <= 16)
        children = {}
        for name, child in scenarios.items():
            valid_name(name, "checklist")
            children[name] = _single(child)
        _require(result["complete"] == all(child["complete"] for child in children.values()))
        return {**result, "scenarios": children}
    except (KeyError, TypeError, ValueError, TraceError, RecursionError):
        raise TraceError(_ERROR) from None


def compare_discovery_checklists(report: dict) -> dict:
    """Compare v1 named discovery syntax, using the first selected child as baseline.

    Requires 2-16 children in manifest order. Each nonbaseline child gets sorted
    rows for its own name union with the baseline, including unchanged names.
    Presence changes are separate from common-name operator/requirement/source
    changes. Sources compare ordered metadata excluding operator; multiplicity,
    offsets and literal labels matter, physical identity is never consulted.
    Independent checklist sections preserve coverage without equating it with
    input satisfaction. No I/O, evaluation, inference or source mutation occurs.
    Wrong/malformed inputs raise a fixed redacted TraceError.
    """
    try:
        _require(isinstance(report, dict) and report.get("report_type") == "scenario-template-discovery")
        result = discovery_checklist(report)
        children = result["scenarios"]
        _require(len(children) >= 2)
    except (KeyError, TypeError, ValueError, TraceError, RecursionError):
        raise TraceError("checklist comparison requires a valid named syntax-only discovery report "
                         "with at least two scenarios") from None

    def variables(child):
        return {row["variable"]: row for row in child["variables"]}

    def sources(row):
        return [{key: value for key, value in source.items() if key != "operator"}
                for source in row["sources"]]

    baseline = next(iter(children))
    before = variables(children[baseline])
    comparisons = {}
    for name, child in children.items():
        if name == baseline:
            continue
        after = variables(child)
        rows = []
        for variable in sorted(before.keys() | after.keys()):
            a, b = before.get(variable), after.get(variable)
            both = a is not None and b is not None
            rows.append({"variable": variable, "baseline_present": a is not None,
                         "scenario_present": b is not None,
                         "presence_change": "added" if a is None else "removed" if b is None else "unchanged",
                         "baseline_operators": list(a["operators"]) if a else [],
                         "scenario_operators": list(b["operators"]) if b else [],
                         "baseline_required": a["required"] if a else None,
                         "scenario_required": b["required"] if b else None,
                         "operators_changed": both and a["operators"] != b["operators"],
                         "requirement_changed": both and a["required"] != b["required"],
                         "source_changed": both and sources(a) != sources(b)})
        comparisons[name] = {"variables": rows}
    return {**result, "report_type": "scenario-template-checklist-comparison",
            "baseline": baseline, "comparisons": comparisons}
