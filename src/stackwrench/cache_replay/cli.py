"""Command-line interface. Inputs are data; RUN text is never executed."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .model import MODES, InputError, compare


def render_text(report: dict) -> str:
    lines = ["stackwrench cache-replay: static linear COPY model (not a build)",
             f"Mode: {report['mode']}",
             "Units: model units; measured build seconds: not measured",
             "Semantic equivalence of candidate: human assumption, not proven",
             "Actual cache hits: unknown; unaffected steps and synthetic hits are not build evidence"]
    selection = report.get("selection")
    if selection:
        lines.append(f"Report filter: scenario {selection['scenarioId']} "
                     f"({selection['displayedScenarioCount']} displayed / {selection['evaluatedScenarioCount']} evaluated)")
    selected_scenarios = report.get("selectedScenarios")
    if selected_scenarios:
        lines.append(f"Report filter: scenarios {', '.join(selected_scenarios['scenarioIds'])} "
                     f"({selected_scenarios['displayedScenarioCount']} displayed / "
                     f"{selected_scenarios['evaluatedScenarioCount']} evaluated)")
    if "initialRevisionIds" in report:
        declarations = report["initialRevisionIds"]
        lines.append(f"Initial revision declarations: {len(declarations)}")
        lines.extend(f"  {path} revisionId={revision}" for path, revision in sorted(declarations.items()))
    candidate = report["candidateComparison"]
    lines.extend(["\nCaller-supplied candidate diff:",
                  candidate["diff"].rstrip("\n") or "  (no textual changes)",
                  candidate["notice"]])
    for scenario in report["scenarios"]:
        lines.append(f"\nScenario {scenario['id']}")
        lines.append(f"  inventory files: {scenario['inventoryBeforeFileCount']} -> {scenario['inventoryAfterFileCount']}")
        if "previousBuild" in scenario:
            previous = scenario["previousBuild"]
            label = ("none (cold start)" if previous is None else "initial-warm"
                     if previous["kind"] == "initial-warm" else f"scenario {previous['scenarioId']}")
            lines.append(f"  previous modeled build: {label}")
        for change in scenario["changes"]:
            if "revisionId" in change:
                lines.append(f"  declared revision: {change['path']} ({change['kind']}) revisionId={change['revisionId']}")
        for side in ("before", "after"):
            result = scenario[side]
            ids = ", ".join(result["invalidatedInstructionIds"]) or "none"
            runs = ", ".join(result["invalidatedRunIds"]) or "none"
            lines.append(f"  {side}: invalidated [{ids}]; RUN [{runs}]; {result['invalidatedModelUnits']:g} model units")
            first = result["firstAffectedCopy"]
            for trigger in result["triggers"]:
                lines.append(f"    {trigger['path']} ({trigger['kind']}) -> {trigger['copyId']} COPY {trigger['source']}")
            if first:
                lines.append(f"    first affected COPY: {first}")
            lines.append(f"    first invalidated COPY: {result['firstInvalidatedCopy'] or 'none'}")
            if result["invalidatedInstructionIds"]:
                lines.append("    chain: " + " -> ".join(result["invalidatedInstructionIds"]))
            if "cache" in result:
                cache = result["cache"]
                if report["mode"] == "independent-cold":
                    lines.append(f"    synthetic cold cache: 0 hits, {len(cache['missInstructionIds'])} misses; "
                                 f"entries 0 -> {cache['entryCountAfter']} (discarded after scenario)")
                else:
                    lines.append(f"    synthetic cache: {len(cache['hitInstructionIds'])} hits, {len(cache['missInstructionIds'])} misses; "
                                 f"retained entries {cache['entryCountBefore']} -> {cache['entryCountAfter']}")
            if "syntheticReuseCounts" in result:
                counts = result["syntheticReuseCounts"]
                lines.append(f"    synthetic reuse: previous-build={counts['previousBuild']}, "
                             f"retained-history={counts['retainedHistory']}, misses={counts['misses']}")
                units = result["syntheticReuseModelUnits"]
                lines.append(f"    synthetic reuse model units: previous-build={units['previousBuild']:g}, "
                             f"retained-history={units['retainedHistory']:g}, misses={units['misses']:g}")
            if result["ignoredMtimePaths"]:
                lines.append("    mtime-only ignored: " + ", ".join(result["ignoredMtimePaths"]))
            if result["uncopiedChangePaths"]:
                lines.append("    uncopied: " + ", ".join(result["uncopiedChangePaths"]))
            for step in result["steps"]:
                lines.append(f"    {step['instructionId']} {step['op']} {step['argument']}: {step['decision']}")
                lines.append(f"      why: {step['why']}")
                if "syntheticStateOrigin" in step:
                    origin = step["syntheticStateOrigin"]
                    label = ("none (miss)" if origin is None else "initial-warm"
                             if origin["kind"] == "initial-warm" else f"scenario {origin['scenarioId']}")
                    lines.append(f"      synthetic state origin: {label}")
                if "syntheticStateReuse" in step:
                    lines.append(f"      synthetic state reuse: {step['syntheticStateReuse'] or 'none (miss)'}")
                for trigger in step["matchedChanges"]:
                    lines.append(f"      matched: {trigger['path']} ({trigger['kind']}) via COPY {trigger['source']}")
        lines.append(f"  saved: {scenario['savedModelUnits']:g} model units")
    total = report["totals"]
    total_label = "Total (displayed scenario)" if selection else "Total"
    if selected_scenarios:
        total_label = "Total (displayed scenarios)"
    lines.append(f"\n{total_label}: before={total['beforeModelUnits']:g}, after={total['afterModelUnits']:g}, saved={total['savedModelUnits']:g} model units")
    if "syntheticReuseCounts" in total:
        for side in ("before", "after"):
            counts = total["syntheticReuseCounts"][side]
            units = total["syntheticReuseModelUnits"][side]
            lines.append(f"  {side} synthetic reuse: previous-build={counts['previousBuild']}, "
                         f"retained-history={counts['retainedHistory']}, misses={counts['misses']}")
            lines.append(f"  {side} synthetic reuse model units: previous-build={units['previousBuild']:g}, "
                         f"retained-history={units['retainedHistory']:g}, misses={units['misses']:g}")
    if "syntheticMissesByInstruction" in total:
        for side in ("before", "after"):
            for record in total["syntheticMissesByInstruction"][side]:
                lines.append(f"  {side} synthetic misses {record['instructionId']}: "
                             f"count={record['missCount']}, missed={record['missedModelUnits']:g} model units; "
                             f"scenarios=[{', '.join(record['missedScenarioIds'])}]")
    if "syntheticHistoryReusesByInstruction" in total:
        for side in ("before", "after"):
            for record in total["syntheticHistoryReusesByInstruction"][side]:
                lines.append(f"  {side} synthetic retained-history reuses {record['instructionId']}: "
                             f"count={record['reuseCount']}, reused={record['reusedModelUnits']:g} model units; "
                             f"scenarios=[{', '.join(record['reusedScenarioIds'])}]")
    if "syntheticHistoryReusesByOrigin" in total:
        for side in ("before", "after"):
            for record in total["syntheticHistoryReusesByOrigin"][side]:
                origin = record["origin"]
                label = "initial-warm" if origin["kind"] == "initial-warm" else f"scenario {origin['scenarioId']}"
                lines.append(f"  {side} synthetic retained-history reuses from {label}: "
                             f"count={record['reuseCount']}, reused={record['reusedModelUnits']:g} model units")
    lines.append("\nAssumptions:")
    lines.extend("  - " + value for value in report["assumptions"])
    lines.append("Unmodeled:")
    lines.extend("  - " + value for value in report["unmodeled"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stackwrench cache-replay", description="Offline baseline/candidate COPY cache dependency replay; never builds containers.")
    parser.add_argument("--version", action="version", version=__version__)
    for option in ("before", "after", "changes", "context"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--weights", type=Path, help="JSON per-side instruction weights; default RUN=1, others=0")
    parser.add_argument("--mode", choices=MODES, default="independent-warm",
                        help="independent warm-cache scenarios (default), independent empty-cache scenarios, "
                             "or ordered warm/cold-start history with retained synthetic cache states")
    selectors = parser.add_mutually_exclusive_group()
    selectors.add_argument("--scenario", metavar="ID", help="display one scenario by exact ID; still evaluate the full input")
    selectors.add_argument("--select-scenarios", metavar="ID", nargs="+", action="extend",
                           help="display unique exact IDs in input order; still evaluate the full input")
    parser.add_argument("--json", action="store_true", help="emit a JSON report on success")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="after reporting, exit 1 if any displayed scenario has negative saved model units")
    args = parser.parse_args(argv)
    try:
        report = compare(args.before, args.after, args.changes, args.context, args.weights,
                         mode=args.mode, scenario=args.scenario, select_scenarios=args.select_scenarios)
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        print(f"stackwrench cache-replay: input not evaluated: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) if args.json else render_text(report))
    if args.fail_on_regression and any(item["savedModelUnits"] < 0 for item in report["scenarios"]):
        return 1
    return 0
