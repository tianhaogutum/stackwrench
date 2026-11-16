"""Console entry point; local reads plus explicitly requested template export."""

import argparse
import json
import sys

from . import __version__
from .trace import TraceError, trace
from .scenarios import compare_scenarios
from .checklist import compare_discovery_checklists, discovery_checklist
from .artifacts import compare_discovery_artifacts
from .template import discover_scenarios, discover_template, env_template, write_env_template


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "stackwrench compose-env: invalid arguments; use --help (argument values redacted)\n")


def text_report(report: dict) -> str:
    lines = ["Interpolation inputs (host environment and implicit .env not read):"]

    def declaration(name: str, item: dict, prefix: str = "  ") -> None:
        winner = item["winner"]
        shown = f" value={json.dumps(winner['value'], ensure_ascii=True)}" if report["values_shown"] else ""
        lines.append(f"{prefix}{name}: {winner['status']} <- {winner['source']} ({winner['syntax']}) @ {winner['location']}{shown}")
        if "resolved_from" in winner:
            origin = winner["resolved_from"]
            lines.append(f"{prefix}  inherited from {origin['source']} @ {origin['location']}")
        for entry in item["shadowed"]:
            lines.append(f"{prefix}  shadowed: {entry['status']} <- {entry['source']} @ {entry['location']}")

    for name, item in report["interpolation"]["variables"].items():
        declaration(name, item)
    lines.append("Interpolation uses:")
    for index, use in enumerate(report["interpolation"]["uses"]):
        origin = use.get("input_source")
        source = origin["source"] if origin else "none"
        lines.append(f"  #{index} {use.get('variable', '$$')} [{use['operator']}] "
                     f"{use.get('input_status', 'literal')} <- {source}; {use['branch']} "
                     f"-> {use['result_status']} @ {use['location']}, offset {use['offset']}")
    lines.append("Service env_files:")
    for item in report["service_env_files"]:
        lines.append(f"  {item['service']}: {item['status']}; format={item['format']}; "
                     f"required={str(item['required']).lower()} @ {item['location']}")
    lines.append("Container environment:")
    for service, details in report["services"].items():
        lines.append(f"  service {service}: image ENV not modeled")
        for name, item in details["variables"].items():
            declaration(name, item, "    ")
    for item in report["not_evaluated_fields"]:
        lines.append(f"Not evaluated: {item['scope']} field {item['field']} @ {item['location']}")
    for warning in report["warnings"]:
        name = f" ({warning['variable']})" if "variable" in warning else ""
        lines.append(f"Warning: {warning['code']}{name} @ {warning['location']}")
    if not report["values_shown"]:
        lines.append("Values hidden. Use --show-values only with safe inputs.")
    return "\n".join(lines)


def text_comparison(report: dict) -> str:
    lines = []
    for name, scenario in report["scenarios"].items():
        suffix = " (baseline)" if name == report["baseline"] else ""
        lines.extend([f"Scenario {name}{suffix}", text_report(scenario), ""])
    for name, comparison in report["comparisons"].items():
        lines.append(f"Comparison {name} against {report['baseline']}:")

        def changes(scope, variables):
            for variable, change in variables.items():
                flags = ", ".join(f"{key}={str(change[key]).lower()}" for key in
                                  ("source_changed", "resolution_changed", "value_changed", "path_selection_changed",
                                   "dependency_path_selection_changed"))
                lines.append(f"  {scope} {variable}: {change['baseline_status']} -> "
                             f"{change['scenario_status']}; {flags}")

        changes("interpolation", comparison["interpolation"]["variables"])
        for service, details in comparison["services"].items():
            changes(f"service {service}", details["variables"])
    lines.append("Comparison covers winning declarations and recorded value dependencies; "
                 "direct winning and value-dependency service env_file selectors are compared separately; "
                 "physical identity is compared privately, image ENV is not modeled.")
    return "\n".join(lines)


def text_discovery(report: dict) -> str:
    lines = ["Syntax-only template discovery (no operators evaluated; values omitted):"]
    if "scope" in report:
        lines.append("Selected services: " + json.dumps(report["scope"]["services"], ensure_ascii=True))
        lines.append("Shared scope: explicit dotenv inputs and top-level Compose values; other services excluded.")
    for use in report["references"]:
        lines.append(f"  {use['variable']} [{use['operator']}] {use['phase']} @ "
                     f"{json.dumps(use['location'], ensure_ascii=True)}; field: "
                     f"{json.dumps(use['field_path'], ensure_ascii=True)}")
    for item in report["service_env_files"]:
        lines.append(f"  env_file {item['status']}; format={item['format']}; required={str(item['required']).lower()} @ "
                     f"{json.dumps(item['location'], ensure_ascii=True)}; field: "
                     f"{json.dumps(item['field_path'], ensure_ascii=True)}")
    for item in report["unresolved_environment"]:
        lines.append("  environment unresolved-dynamic; assignment/inheritance name unknown @ " +
                     json.dumps(item["location"], ensure_ascii=True) + "; field: " +
                     json.dumps(item["field_path"], ensure_ascii=True))
    lines.append("Discovery complete for explicit inputs, literal service env_files and statically named inheritance."
                 if report["complete"] else
                 "Discovery incomplete: dynamic file contents or environment declaration names remain unresolved.")
    return "\n".join(lines)


def text_scenario_discovery(report: dict) -> str:
    return "\n\n".join(f"Scenario {name}\n{text_discovery(child)}"
                       for name, child in report["scenarios"].items())


def main(argv: list[str] | None = None) -> int:
    parser = Parser(prog="stackwrench compose-env", description="Offline two-phase Compose environment provenance; redacts values by default.",
                    allow_abbrev=False)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--compose", action="append", help="one literal YAML/JSON Compose file (no merge); required except for saved artifact comparison")
    parser.add_argument("--compare-discovery-artifacts", nargs=2, action="append", metavar=("BASELINE", "CANDIDATE"),
                        help="compare two saved single-discovery JSON files, always JSON; excludes all live-input, discovery and export options")
    parser.add_argument("--env-file", action="append", default=[], help="explicit interpolation dotenv; repeat in precedence order")
    parser.add_argument("--shell-env", help="JSON string-to-string shell snapshot, highest interpolation priority")
    parser.add_argument("--scenario-file", help="JSON object of 2-16 named explicit input sets; first is comparison baseline, or discover each independently")
    parser.add_argument("--scenario", action="append", help="select a named discovery scenario; repeat as needed, output stays in manifest order; requires --discover-template --scenario-file")
    parser.add_argument("--service", action="append", help="select a literal service for syntax-only discovery; repeat as needed, Compose order retained; shared explicit dotenv/top-level references included")
    parser.add_argument("--env-template", help="create a new comment-only dotenv checklist from a single trace or single/named discovery; never overwrite")
    parser.add_argument("--discover-template", action="store_true",
                        help="syntax-only references without required-input evaluation; never open dynamic env_file paths")
    parser.add_argument("--require-complete", action="store_true",
                        help="require complete selected syntax-only discovery coverage before output/export; requires --discover-template")
    parser.add_argument("--checklist-json", action="store_true",
                        help="structured per-variable syntax checklist as JSON; requires --discover-template; keeps scenario sections independent")
    parser.add_argument("--compare-checklists-json", action="store_true",
                        help="baseline-relative syntax checklist differences as JSON; requires named discovery with at least two selected scenarios; excludes --checklist-json")
    parser.add_argument("--json", action="store_true", help="machine-readable report or error")
    parser.add_argument("--show-values", action="store_true", help="explicitly reveal values in successful reports")
    args = parser.parse_args(argv)
    artifact_mode = args.compare_discovery_artifacts is not None
    if artifact_mode and (len(args.compare_discovery_artifacts) != 1 or args.compose is not None
                          or args.env_file or args.shell_env is not None or args.scenario_file is not None
                          or args.scenario is not None or args.service is not None or args.env_template is not None
                          or args.discover_template or args.require_complete or args.checklist_json
                          or args.compare_checklists_json or args.show_values):
        parser.error("saved artifact comparison excludes live input, discovery and export options")
    if not artifact_mode and args.compose is None:
        parser.error("live modes require a Compose file")
    if not artifact_mode and len(args.compose) != 1:
        parser.exit(2, "stackwrench compose-env: exactly one Compose file supported; multi-file merge unsupported\n")
    if args.checklist_json and not args.discover_template:
        parser.error("structured checklist requires syntax-only discovery")
    if args.compare_checklists_json and (not args.discover_template or args.scenario_file is None or args.checklist_json):
        parser.error("checklist comparison requires named syntax-only discovery and excludes checklist projection")
    if args.service is not None and not args.discover_template:
        parser.error("service selection requires syntax-only discovery")
    if args.require_complete and not args.discover_template:
        parser.error("completeness gate requires syntax-only discovery")
    if args.scenario is not None and (not args.discover_template or args.scenario_file is None):
        parser.error("scenario selection requires named discovery")
    if args.scenario_file is not None and (args.env_file or args.shell_env is not None):
        parser.error("scenario inputs cannot be combined with shared inputs")
    if args.scenario_file is not None and args.env_template is not None and not args.discover_template:
        parser.error("template export requires a single trace")
    if args.discover_template and (args.shell_env is not None or args.show_values):
        parser.error("syntax-only discovery cannot combine with shell snapshot or value disclosure")
    try:
        if artifact_mode:
            result = compare_discovery_artifacts(*args.compare_discovery_artifacts[0])
        elif args.discover_template and args.scenario_file is not None:
            result = discover_scenarios(args.compose[0], args.scenario_file, scenario_names=args.scenario, service_names=args.service,
                                        require_complete=args.require_complete)
        elif args.discover_template:
            result = discover_template(args.compose[0], args.env_file, service_names=args.service,
                                       require_complete=args.require_complete)
        elif args.scenario_file is not None:
            result = compare_scenarios(args.compose[0], args.scenario_file, show_values=args.show_values)
        else:
            result = trace(args.compose[0], args.env_file, args.shell_env, show_values=args.show_values)
        if args.compare_checklists_json:
            output = compare_discovery_checklists(result)
        else:
            output = discovery_checklist(result) if args.checklist_json else result
        if args.env_template is not None:
            write_env_template(args.env_template, env_template(result))
    except (TraceError, RecursionError) as error:
        message = str(error) if isinstance(error, TraceError) else "input nesting exceeds supported parser depth"
        if artifact_mode or args.json or args.checklist_json or args.compare_checklists_json:
            print(json.dumps({"error": {"code": "invalid-or-unsupported-input", "message": message}}, ensure_ascii=True), file=sys.stderr)
        else:
            print(f"stackwrench compose-env: {message}", file=sys.stderr)
        return 2
    if args.discover_template:
        render = text_scenario_discovery if args.scenario_file is not None else text_discovery
    else:
        render = text_comparison if args.scenario_file is not None else text_report
    print(json.dumps(output, indent=2, ensure_ascii=True) if artifact_mode or args.json or args.checklist_json or args.compare_checklists_json else render(result))
    return 0
