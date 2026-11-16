"""Compare named, explicit inputs through the existing two-phase trace model."""

from pathlib import Path
import json

from .trace import TraceError, _trace, read_text, valid_name

MAX_SCENARIOS = 16


def load_scenarios(path: Path) -> dict:
    label = str(path)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TraceError(f"{label}: duplicate scenario JSON key (name redacted)")
            result[key] = value
        return result

    try:
        data = json.loads(read_text(path, label), object_pairs_hook=pairs)
    except (ValueError, RecursionError) as error:
        if isinstance(error, TraceError):
            raise
        raise TraceError(f"{label}: invalid scenario JSON (details redacted)") from None
    if not isinstance(data, dict) or not 2 <= len(data) <= MAX_SCENARIOS:
        raise TraceError(f"{label}: expected 2 to {MAX_SCENARIOS} named scenarios")

    def input_path(value):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise TraceError(f"{label}: scenario input paths must be nonempty strings without NUL")
        return path.parent / value

    result = {}
    # Validate the whole manifest before reading any scenario input.
    for name, options in data.items():
        valid_name(name, label)
        if not isinstance(options, dict) or options.keys() - {"env_files", "shell_env"}:
            raise TraceError(f"{label}: each scenario must be an object with only env_files and shell_env")
        files = options.get("env_files", [])
        if not isinstance(files, list):
            raise TraceError(f"{label}: scenario env_files must be a list of paths")
        result[name] = {
            "env_files": [input_path(value) for value in files],
            "shell_env": input_path(options["shell_env"]) if "shell_env" in options else None,
        }
    return result


def origin(entry: dict | None, identities: dict) -> tuple | None:
    """Declaration identity; states, values and report-local use indexes are not identity."""
    if entry is None:
        return None
    physical = identities[entry["location"]] if entry["source"] == "service-env-file" else None
    return (entry["source"], entry["location"], entry["syntax"], physical,
            origin(entry.get("resolved_from"), identities))


def resolution(entry: dict | None, report: dict, identities: dict, *, uses=()) -> tuple:
    """Compare reachable expression metadata, without recursive walks or index identity.

    Following upstream entries also detects defaults in sequential dotenv inputs.
    Input/output states are compared as values separately, not as provenance.
    """
    pending = list(uses)

    def enqueue(item):
        while item is not None:
            pending.extend(item["interpolation_uses"])
            item = item.get("resolved_from")

    enqueue(entry)
    seen = set()
    events = []
    uses = report["interpolation"]["uses"]
    while pending:
        index = pending.pop()
        if index in seen:
            continue
        seen.add(index)
        use = uses[index]
        source = use.get("input_source")
        events.append(json.dumps({
            "phase": use["phase"], "location": use["location"], "offset": use["offset"],
            "variable": use.get("variable"), "operator": use["operator"],
            "branch": use["branch"], "input_source": origin(source, identities),
            "physical_source": identities[use["location"]] if use["phase"] == "service-env-file" else None,
        }, sort_keys=True, ensure_ascii=True))
        enqueue(source)
    return tuple(sorted(events))


def path_selection(entry: dict | None, report: dict, metadata) -> tuple:
    """Only this winning file's selector, not selectors of value dependencies."""
    if entry is None or entry["source"] != "service-env-file":
        return ()
    return resolution(None, report, metadata.service_file_identities,
                      uses=metadata.service_file_path_uses[entry["location"]])


def dependency_path_selection(entry: dict | None, report: dict, metadata) -> tuple:
    """Selectors strictly below the winner in its recorded value-dependency graph.

    Follow inheritance and recorded input sources, even unused-by-branch sources,
    never shadowed histories or selector edges. Visit each entry/use once to stop
    cycles and deduplicate shared dependencies. Local indexes only locate events;
    resolution() compares normalized provenance without values, states or indexes.
    """
    if entry is None:
        return ()
    pending = [entry]
    seen_entries = set()
    seen_uses = set()
    selectors = set()
    uses = report["interpolation"]["uses"]
    while pending:
        item = pending.pop()
        if id(item) in seen_entries:
            continue
        seen_entries.add(id(item))
        if item is not entry and item["source"] == "service-env-file":
            selectors.update(metadata.service_file_path_uses[item["location"]])
        if item.get("resolved_from") is not None:
            pending.append(item["resolved_from"])
        for index in item["interpolation_uses"]:
            if index in seen_uses:
                continue
            seen_uses.add(index)
            source = uses[index].get("input_source")
            if source is not None:
                pending.append(source)
    return resolution(None, report, metadata.service_file_identities, uses=selectors)


def compare_variables(before: dict, after: dict, baseline: dict, scenario: dict,
                      baseline_metadata, scenario_metadata, extra_names=()) -> dict:
    baseline_identities = baseline_metadata.service_file_identities
    scenario_identities = scenario_metadata.service_file_identities
    result = {}
    for name in sorted(before.keys() | after.keys() | set(extra_names)):
        a = before.get(name, {}).get("winner")
        b = after.get(name, {}).get("winner")
        result[name] = {
            "baseline_status": a["status"] if a else "unset",
            "scenario_status": b["status"] if b else "unset",
            "source_changed": origin(a, baseline_identities) != origin(b, scenario_identities),
            "resolution_changed": resolution(a, baseline, baseline_identities) != resolution(b, scenario, scenario_identities),
            "value_changed": (a["value"] if a else None) != (b["value"] if b else None),
            "path_selection_changed": path_selection(a, baseline, baseline_metadata) != path_selection(b, scenario, scenario_metadata),
            "dependency_path_selection_changed": dependency_path_selection(a, baseline, baseline_metadata) != dependency_path_selection(b, scenario, scenario_metadata),
        }
    return result


def redact(report: dict) -> None:
    """Strip only schema value fields; variable names may themselves be value/result."""
    def entry(item):
        item.pop("value", None)
        if "resolved_from" in item:
            entry(item["resolved_from"])

    def variables(items):
        for item in items.values():
            entry(item["winner"])
            for shadow in item["shadowed"]:
                entry(shadow)

    variables(report["interpolation"]["variables"])
    for use in report["interpolation"]["uses"]:
        use.pop("result", None)
    for service in report["services"].values():
        variables(service["variables"])
    report["values_shown"] = False


def compare_scenarios(compose: str | Path, scenario_file: str | Path, *,
                      show_values: bool = False) -> dict:
    """Compare every named scenario with the first, using only explicit local files.

    Manifest-relative input paths are the only difference from trace's path rules.
    Exact equality is computed privately; no hashes or values escape by default.
    A failing scenario raises TraceError, so no partial comparison is returned.
    """
    definitions = load_scenarios(Path(scenario_file))
    reports = {}
    metadata = {}
    for name, options in definitions.items():
        try:
            reports[name], metadata[name] = _trace(compose, **options, show_values=True)
        except (TraceError, RecursionError) as error:
            message = str(error) if isinstance(error, TraceError) else "input nesting exceeds supported parser depth"
            raise TraceError(f"scenario {name}: {message}") from None
    baseline_name = next(iter(reports))
    baseline = reports[baseline_name]
    comparisons = {}
    for name, report in reports.items():
        if name == baseline_name:
            continue
        # Also list referenced-but-unsupplied Compose inputs explicitly as unset.
        referenced = {use["variable"] for item in (baseline, report)
                      for use in item["interpolation"]["uses"]
                      if use["phase"] == "compose" and "variable" in use}
        comparisons[name] = {
            "interpolation": {"variables": compare_variables(
                baseline["interpolation"]["variables"], report["interpolation"]["variables"],
                baseline, report, metadata[baseline_name], metadata[name], referenced)},
            "services": {service: {"variables": compare_variables(
                baseline["services"][service]["variables"], report["services"][service]["variables"],
                baseline, report, metadata[baseline_name], metadata[name])} for service in baseline["services"]},
        }
    if not show_values:
        for report in reports.values():
            redact(report)
    return {
        "schema_version": 1, "report_type": "scenario-comparison", "values_shown": show_values,
        "baseline": baseline_name, "host_environment": "not-read", "implicit_dotenv": "not-read",
        "comparison_scope": {"declarations": "winning-only", "resolution": "recorded-value-dependencies",
                             "path_selection": "winning-service-env-file-selector-provenance",
                             "dependency_path_selection": "recorded-value-dependency-service-env-file-selector-provenance",
                             "service_env_file_identity": "private-device-inode", "image_env": "not-modeled"},
        "scenarios": reports, "comparisons": comparisons,
    }
