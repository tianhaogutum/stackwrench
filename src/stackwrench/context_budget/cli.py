"""Command-line interface; diagnostics never masquerade as a budget pass."""

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .core import analyze
from .dimensions import contribution_changes, summarize, validate_intersections, validate_limits
from .ignore import InputError
from .snapshot import (
    MODEL, compare_snapshots, from_report, inspect_snapshot, read_snapshot, snapshot_path, write_snapshot,
)


def nonnegative(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a nonnegative integer") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return result


def _rule_reason(rule: dict | None) -> str:
    if rule is None:
        return "default include"
    location = f"{rule['source']}:{rule['line']}"
    return f"{location!r} {rule['raw']!r} via {rule['matched_path']!r}"


def dimension_limits(values: list[str]) -> dict[str, int]:
    limits = {}
    for value in values:
        selector, separator, amount = value.rpartition("=")
        if not separator or not selector or not amount.isascii() or not amount.isdecimal():
            raise InputError("dimension budget must be SELECTOR=NONNEGATIVE_INTEGER")
        if selector in limits:
            raise InputError(f"duplicate dimension budget: {selector!r}")
        try:
            limits[selector] = int(amount)
        except ValueError as exc:
            raise InputError("dimension budget integer is too long") from exc
    return limits


def intersection_limits(values: list[list[str]]) -> dict[tuple[str, str], int]:
    limits = {}
    for directory, value in values:
        suffix, amount = next(iter(dimension_limits([value]).items()))
        pair = (directory, suffix)
        if pair in limits:
            raise InputError(f"duplicate intersection budget: {pair!r}")
        limits[pair] = amount
    return limits


def render(report: dict) -> str:
    lines = [f"Context: {report['context']!r}",
             f"Ignore selection: {report['ignore_selection']}",
             "Logical regular-file payload only; not compressed transfer or image size.",
             "Dockerfile/active ignore controls are separate; symlinks count as 0 payload bytes."]
    labels = ["baseline"] + (["candidate"] if report["candidate"] is not None else [])
    for label in labels:
        scenario = report[label]
        summary = scenario["summary"]
        lines.extend([f"\n{label}: {summary['included_payload_bytes']} bytes, {summary['included_payload_files']} files",
                      f"Rules: {scenario['rules_source']!r}",
                      f"Controls: {summary['control_files']} files / {summary['control_bytes_on_disk']} on-disk bytes (separate)",
                      f"Symlinks: {summary['included_symlinks']} included, {summary['excluded_symlinks']} excluded",
                      "Entries (last matching rule, including parent matches):"])
        for row in scenario["entries"]:
            rule = row["last_rule"]
            why = _rule_reason(rule)
            decision = "include" if row["included_by_rules"] else "exclude"
            # repr prevents filenames or link text injecting terminal control codes.
            lines.append(f"  {decision:7} {row['kind']:9} {row['logical_bytes']:>10} {row['path']!r} [{row['role']}] {why}")
            if "link_target" in row:
                lines.append(f"    link target (not followed): {row['link_target']!r}")
        lines.append("Largest included payload files:")
        for row in scenario["largest_included_files"]:
            lines.append(f"  {row['logical_bytes']:>10} {row['path']!r}")
        lines.append("Top directory subtrees (overlap) and final extensions (case-sensitive):")
        for dimension, key in (("directories", "path"), ("extensions", "extension")):
            for row in scenario["contributions"][dimension]:
                lines.append(f"  {dimension}: {row[key]!r}: {row['included_payload_bytes']} bytes / {row['included_payload_files']} files")
            for row in scenario["budget"][dimension]:
                lines.append(f"  Budget {dimension} {row[key]!r}: {row['included_payload_bytes']} / {row['max_bytes']} bytes; " +
                             ("PASS" if row["passed"] else "EXCEEDED"))
        for row in scenario["budget"]["directory_extensions"]:
            lines.append(f"  Budget directory_extensions {row['path']!r} extension {row['extension']!r}: "
                         f"{row['included_payload_bytes']} / {row['max_bytes']} bytes; " +
                         ("PASS" if row["passed"] else "EXCEEDED"))
        if scenario["ignore_suggestions"]:
            lines.append("Candidate ignore advice: append ONE pattern after existing rules, then review build inputs.")
            lines.append("Each estimate is independent, not additive; patterns may also exclude future matching paths/descendants.")
            for row in scenario["ignore_suggestions"]:
                suffix = f" extension {row['scope']['extension']!r}" if "extension" in row["scope"] else ""
                lines.append(f"  Append {row['append_pattern']!r}: {row['logical_payload_bytes_delta']:+d} logical payload bytes, "
                             f"{row['included_payload_files_delta']:+d} files; scope {row['scope']['kind']} {row['scope']['selector']!r}{suffix}; "
                             f"all budgets after: {'PASS' if row['budget_after']['passed'] else 'EXCEEDED'}")
                for changed in row["changed_entries"]:
                    lines.append(f"    would exclude {changed['path']!r} [{changed['kind']}, {changed['role']}]; "
                                 f"{changed['logical_payload_bytes_delta']:+d} logical payload bytes")
    if report["comparison"] is not None:
        comparison = report["comparison"]
        lines.append(f"\nCandidate minus baseline: {comparison['included_payload_bytes_delta']:+d} bytes, {comparison['included_payload_files_delta']:+d} files")
    if "snapshot_written" in report:
        lines.append(f"\nSnapshot created: {report['snapshot_written']!r}")
    if "snapshot_comparison" in report:
        comparison = report["snapshot_comparison"]
        lines.extend(render_snapshot_comparison(comparison))
    selected = report[report["budget_applies_to"]]
    if selected["budget"]["max_bytes"] is not None:
        lines.append(f"Budget ({report['budget_applies_to']}): {selected['budget']['max_bytes']} bytes; " +
                     ("PASS" if selected["budget"]["total_passed"] else "EXCEEDED"))
    return "\n".join(lines)


def render_snapshot_comparison(comparison: dict) -> list[str]:
    saved_current = "current_snapshot" in comparison
    lines = [f"\nSaved snapshot: {comparison['baseline_snapshot']!r}"]
    if saved_current:
        lines.append(f"Current snapshot: {comparison['current_snapshot']!r}")
    direction = "Current minus baseline snapshot" if saved_current else "Current minus saved snapshot"
    lines.append(f"{direction}: {comparison['included_payload_bytes_delta']:+d} logical payload bytes, {comparison['included_payload_files_delta']:+d} files")
    for label in ("baseline", "current"):
        metadata = comparison[label]
        lines.append(f"  {label} rules: {metadata['rules_source']!r} ({metadata['ignore_selection']})")
    if not comparison["changes"]:
        lines.append("No inventory differences.")
    for change in comparison["changes"]:
        lines.append(f"  {change['path']!r}: {', '.join(change['reasons'])}; {change['included_payload_bytes_delta']:+d} logical payload bytes")
        for label in ("before", "after"):
            row = change[label]
            if row is None:
                lines.append(f"    {label}: absent")
                continue
            why = _rule_reason(row["last_rule"])
            decision = "include" if row["included_by_rules"] else "exclude"
            lines.append(f"    {label}: {decision} {row['kind']} {row['logical_bytes']} bytes [{row['role']}] {why}")
    return lines


def compare_saved(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="stackwrench context-budget compare-snapshots",
        description="Compare CURRENT minus BASELINE saved metadata without accessing the recorded context or rules.")
    parser.add_argument("baseline", help="saved baseline snapshot (cwd-relative)")
    parser.add_argument("current", help="saved current snapshot (cwd-relative)")
    parser.add_argument("--max-bytes", type=nonnegative, metavar="NONNEGATIVE_INTEGER",
                        help="logical payload byte budget for CURRENT saved metadata only")
    parser.add_argument("--max-dir-bytes", action="append", default=[], metavar="DIR=BYTES",
                        help="repeatable subtree budget; DIR must be a CURRENT saved directory")
    parser.add_argument("--max-extension-bytes", action="append", default=[], metavar="EXT=BYTES",
                        help="repeatable final suffix budget for CURRENT saved payload, e.g. .bin=1000 or '<none>=1000'")
    parser.add_argument("--max-dir-extension-bytes", action="append", nargs=2, default=[],
                        metavar=("DIR", "EXT=BYTES"),
                        help="repeatable intersection budget; DIR must be a CURRENT saved directory")
    parser.add_argument("--top", type=nonnegative, metavar="NONNEGATIVE_INTEGER",
                        help="explicitly add up to N CURRENT saved directory and extension contributions per list")
    parser.add_argument("--top-changes", type=nonnegative, metavar="NONNEGATIVE_INTEGER",
                        help="explicitly add up to N changed saved directory and extension groups per list")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    options = parser.parse_args(args)
    try:
        baseline_path = Path(os.path.abspath(options.baseline))
        current_path = Path(os.path.abspath(options.current))
        baseline = read_snapshot(baseline_path)
        current = read_snapshot(current_path)
        comparison = compare_snapshots(baseline, current)
        directories = validate_limits(dimension_limits(options.max_dir_bytes), directory=True)
        extensions = validate_limits(dimension_limits(options.max_extension_bytes), directory=False)
        pairs = validate_intersections(intersection_limits(options.max_dir_extension_bytes))
        if directories or pairs:
            saved_directories = {row["path"] for row in current["entries"] if row["kind"] == "directory"}
            for directory in sorted(directories.keys() | {pair[0] for pair in pairs}):
                if directory not in saved_directories:
                    kind = "directory" if directory in directories else "intersection"
                    raise InputError(f"{directory!r}: {kind} budget must name a CURRENT saved directory")
        comparison.update(baseline_snapshot=str(baseline_path), current_snapshot=str(current_path))
        report = {"schema_version": 1, "status": "ok", "model": MODEL,
                  "snapshot_comparison": comparison}
        if options.max_bytes is not None:
            observed = comparison["current"]["included_payload_bytes"]
            passed = observed <= options.max_bytes
            report["current_saved_budget"] = {"max_bytes": options.max_bytes,
                                              "included_payload_bytes": observed, "passed": passed}
            if not passed:
                report["status"] = "budget_exceeded"
        if directories or extensions or pairs or options.top is not None:
            contributions, budget = summarize(current["entries"], options.max_bytes, directories, extensions,
                                              options.top if options.top is not None else 0, pairs,
                                              budget_file_counts=True)
            if options.top is not None:
                report["current_saved_contributions"] = contributions
        if directories or extensions or pairs:
            if directories:
                report["current_saved_directory_budgets"] = budget["directories"]
            if extensions:
                report["current_saved_extension_budgets"] = budget["extensions"]
            if pairs:
                report["current_saved_directory_extension_budgets"] = budget["directory_extensions"]
            report["current_saved_budgets_passed"] = budget["passed"]
            if not budget["passed"]:
                report["status"] = "budget_exceeded"
        if options.top_changes is not None:
            report["saved_contribution_changes"] = contribution_changes(
                baseline["entries"], current["entries"], options.top_changes)
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        message = str(exc) if not isinstance(exc, RecursionError) else "snapshot nesting exceeds supported Python recursion depth"
        if options.json:
            print(json.dumps({"schema_version": 1, "status": "error", "error": message}, ensure_ascii=True))
        else:
            print(f"stackwrench context-budget: {message!r}", file=sys.stderr)
        return 2
    if options.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        metadata = comparison["current"]
        lines = [f"Recorded context: {metadata['context']!r}",
                 f"Recorded Dockerfile: {metadata['dockerfile']!r}",
                 "Saved metadata only; no live context scan.",
                 "Logical regular-file payload only; not compressed transfer or image size."]
        lines.extend(render_snapshot_comparison(comparison))
        if "current_saved_budget" in report:
            budget = report["current_saved_budget"]
            lines.append(f"Current saved budget: {budget['included_payload_bytes']} / {budget['max_bytes']} bytes; " +
                         ("PASS" if budget["passed"] else "EXCEEDED"))
        if "current_saved_directory_budgets" in report:
            for row in report["current_saved_directory_budgets"]:
                lines.append(f"Current saved directory budget {row['path']!r}: "
                             f"{row['included_payload_bytes']} / {row['max_bytes']} bytes; "
                             f"{row['included_payload_files']} files; " + ("PASS" if row["passed"] else "EXCEEDED"))
        if "current_saved_extension_budgets" in report:
            for row in report["current_saved_extension_budgets"]:
                lines.append(f"Current saved extension budget {row['extension']!r}: "
                             f"{row['included_payload_bytes']} / {row['max_bytes']} bytes; "
                             f"{row['included_payload_files']} files; " + ("PASS" if row["passed"] else "EXCEEDED"))
        if "current_saved_directory_extension_budgets" in report:
            for row in report["current_saved_directory_extension_budgets"]:
                lines.append(f"Current saved intersection budget {row['path']!r} extension {row['extension']!r}: "
                             f"{row['included_payload_bytes']} / {row['max_bytes']} bytes; "
                             f"{row['included_payload_files']} files; " + ("PASS" if row["passed"] else "EXCEEDED"))
        if "current_saved_budgets_passed" in report:
            lines.append("Current saved budgets: " + ("PASS" if report["current_saved_budgets_passed"] else "EXCEEDED"))
        if "current_saved_contributions" in report:
            lines.append("Current saved contributions: directory subtrees overlap; final extensions are case-sensitive.")
            for dimension, key in (("directories", "path"), ("extensions", "extension")):
                for row in report["current_saved_contributions"][dimension]:
                    lines.append(f"  {dimension}: {row[key]!r}: {row['included_payload_bytes']} bytes / "
                                 f"{row['included_payload_files']} files")
        if "saved_contribution_changes" in report:
            lines.append("Saved contribution changes: CURRENT minus BASELINE; directory subtrees overlap; "
                         "final extensions are case-sensitive.")
            for dimension, key in (("directories", "path"), ("extensions", "extension")):
                for row in report["saved_contribution_changes"][dimension]:
                    lines.append(f"  {dimension}: {row[key]!r}: "
                                 f"baseline {row['baseline_included_payload_bytes']} bytes / "
                                 f"{row['baseline_included_payload_files']} files; "
                                 f"current {row['current_included_payload_bytes']} bytes / "
                                 f"{row['current_included_payload_files']} files; "
                                 f"delta {row['included_payload_bytes_delta']:+d} bytes / "
                                 f"{row['included_payload_files_delta']:+d} files")
        print("\n".join(lines))
    return 1 if report["status"] == "budget_exceeded" else 0


def _render_saved_entry(row: dict) -> list[str]:
    lines = [f"  {key}: {row[key]!r}"
             for key in ("path", "kind", "logical_bytes", "role", "included_by_rules")]
    if row["last_rule"] is None:
        lines.append("  last_rule: null (default include)")
    else:
        lines.append("  last_rule:")
        for key in ("source", "line", "raw", "pattern", "negated", "matched_path"):
            lines.append(f"    {key}: {row['last_rule'][key]!r}")
    return lines


def inspect_saved(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="stackwrench context-budget inspect-snapshot",
        description="Inspect one saved inventory without accessing the recorded context or rules.")
    parser.add_argument("snapshot", help="saved inventory snapshot (cwd-relative)")
    parser.add_argument("--path", action="append", default=[], metavar="EXACT_RELATIVE_PATH",
                        help="select one exact recorded entry; no normalization, globs or live lookup")
    parser.add_argument("--subtree", action="append", default=[], metavar="EXACT_RECORDED_DIRECTORY",
                        help="list one exact recorded directory and all descendants; exclusive with --path")
    parser.add_argument("--extension", action="append", default=[], metavar="EXACT_FINAL_SUFFIX",
                        help="list regular-file metadata with this exact final suffix or <none>; exclusive with --path/--subtree")
    parser.add_argument("--subtree-extension", action="append", nargs=2, default=[],
                        metavar=("EXACT_RECORDED_DIRECTORY", "EXACT_FINAL_SUFFIX"),
                        help="list regular-file metadata for one exact saved directory/suffix pair; exclusive with other selectors")
    parser.add_argument("--decision", action="append", default=[], choices=("included", "excluded"),
                        metavar="included|excluded",
                        help="list every recorded entry with this stored inclusion decision; exclusive with other selectors")
    parser.add_argument("--limit", action="append", default=[], type=nonnegative, metavar="NONNEGATIVE_INTEGER",
                        help="maximum displayed rows (default 100); requires --subtree, --extension, --subtree-extension or --decision")
    parser.add_argument("--json", action="count", default=0, help="machine-readable report")
    options = parser.parse_args(args)
    # Some argparse versions consume a following option as operand two of
    # nargs=2. A final suffix can never start with '-', so fail as a missing
    # operand before opening the artifact or rendering a partial result.
    if any(suffix.startswith("-") for _, suffix in options.subtree_extension):
        parser.error("--subtree-extension requires EXACT_RECORDED_DIRECTORY EXACT_FINAL_SUFFIX")
    try:
        path = Path(os.path.abspath(options.snapshot))
        saved = read_snapshot(path)
        for flag in ("path", "subtree", "extension", "subtree_extension", "decision", "limit"):
            if len(getattr(options, flag)) > 1:
                raise InputError(f"--{flag.replace('_', '-')} may be specified only once")
        if options.json > 1:
            raise InputError("--json may be specified only once")
        inspection = inspect_snapshot(saved, options.path[0] if options.path else None,
                                      subtree=options.subtree[0] if options.subtree else None,
                                      extension=options.extension[0] if options.extension else None,
                                      subtree_extension=tuple(options.subtree_extension[0]) if options.subtree_extension else None,
                                      decision=options.decision[0] if options.decision else None,
                                      limit=options.limit[0] if options.limit else None)
        inspection["snapshot"] = str(path)
        report = {"schema_version": 1, "status": "ok", "model": MODEL,
                  "snapshot_inspection": inspection}
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        message = str(exc) if not isinstance(exc, RecursionError) else "snapshot nesting exceeds supported Python recursion depth"
        if options.json:
            print(json.dumps({"schema_version": 1, "status": "error", "error": message}, ensure_ascii=True))
        else:
            print(f"stackwrench context-budget: {message!r}", file=sys.stderr)
        return 2
    if options.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        summary = inspection["summary"]
        lines = [f"Saved snapshot: {inspection['snapshot']!r}",
                 f"Inventory format: {inspection['format']!r}; schema version {inspection['schema_version']}",
                 f"Recorded model: {MODEL!r}",
                 f"Recorded context: {summary['context']!r}",
                 f"Recorded Dockerfile: {summary['dockerfile']!r}",
                 f"Recorded ignore selection: {summary['ignore_selection']!r}",
                 f"Recorded rules: {summary['rules_source']!r}",
                 "Saved metadata only; no live context scan, content or freshness verification.",
                 "Logical regular-file payload only; controls separate; symlinks count as 0 payload bytes.",
                 f"Saved entries: {inspection['entry_count']}",
                 f"Saved payload: {summary['included_payload_bytes']} bytes, {summary['included_payload_files']} files"]
        if "selected_entry" in inspection:
            lines.append("Selected recorded entry:")
            lines.extend(_render_saved_entry(inspection["selected_entry"]))
        if "selected_subtree" in inspection:
            selected = inspection["selected_subtree"]
            lines.extend([f"Selected recorded subtree: {selected['path']!r}",
                          f"Subtree entries: {selected['returned_entry_count']} returned / "
                          f"{selected['matching_entry_count']} matching; truncated: {selected['truncated']}",
                          f"Subtree payload: {selected['included_payload_bytes']} bytes, "
                          f"{selected['included_payload_files']} files"])
            for row in selected["entries"]:
                lines.append("Recorded subtree entry:")
                lines.extend(_render_saved_entry(row))
        if "selected_extension" in inspection:
            selected = inspection["selected_extension"]
            lines.extend([f"Selected recorded extension: {selected['extension']!r}",
                          f"Extension entries: {selected['returned_entry_count']} returned / "
                          f"{selected['matching_entry_count']} matching; truncated: {selected['truncated']}",
                          f"Extension payload: {selected['included_payload_bytes']} bytes, "
                          f"{selected['included_payload_files']} files"])
            for row in selected["entries"]:
                lines.append("Recorded extension entry:")
                lines.extend(_render_saved_entry(row))
        if "selected_subtree_extension" in inspection:
            selected = inspection["selected_subtree_extension"]
            lines.extend([f"Selected recorded subtree extension: {selected['path']!r} extension {selected['extension']!r}",
                          f"Subtree extension entries: {selected['returned_entry_count']} returned / "
                          f"{selected['matching_entry_count']} matching; truncated: {selected['truncated']}",
                          f"Subtree extension payload: {selected['included_payload_bytes']} bytes, "
                          f"{selected['included_payload_files']} files"])
            for row in selected["entries"]:
                lines.append("Recorded subtree extension entry:")
                lines.extend(_render_saved_entry(row))
        if "selected_decision" in inspection:
            selected = inspection["selected_decision"]
            lines.extend([f"Selected recorded decision: {selected['decision']!r}",
                          f"Decision entries: {selected['returned_entry_count']} returned / "
                          f"{selected['matching_entry_count']} matching; truncated: {selected['truncated']}",
                          f"Decision payload: {selected['included_payload_bytes']} bytes, "
                          f"{selected['included_payload_files']} files"])
            for row in selected["entries"]:
                lines.append("Recorded decision entry:")
                lines.extend(_render_saved_entry(row))
        print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "inspect-snapshot":
        return inspect_saved(args_list[1:])
    if args_list and args_list[0] == "compare-snapshots":
        return compare_saved(args_list[1:])
    compare = bool(args_list and args_list[0] == "compare")
    if compare:
        args_list.pop(0)
    parser = argparse.ArgumentParser(
        prog="stackwrench context-budget",
        description="Offline Docker-ignore provenance and logical payload budget. Optional alias: compare CONTEXT --candidate-ignore FILE.",
        epilog="Saved-only inspection: stackwrench context-budget inspect-snapshot SNAPSHOT [--path EXACT_RELATIVE_PATH] [--json]. Saved-only comparison: stackwrench context-budget compare-snapshots BASELINE CURRENT [--max-bytes NONNEGATIVE_INTEGER] [--max-dir-bytes DIR=BYTES ...] [--max-extension-bytes EXT=BYTES ...] [--max-dir-extension-bytes DIR EXT=BYTES ...] [--top NONNEGATIVE_INTEGER] [--top-changes NONNEGATIVE_INTEGER] [--json].")
    parser.add_argument("context", help="one local directory (no symlink path components)")
    parser.add_argument("--dockerfile", default="Dockerfile", help="context-relative Dockerfile path")
    parser.add_argument("--ignore", help="explicit baseline rule file (cwd-relative); overrides automatic selection")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--candidate-ignore", help="candidate rule file (cwd-relative); compare without mutation")
    modes.add_argument("--save-snapshot", help="explicitly create a new metadata snapshot outside context (cwd-relative)")
    modes.add_argument("--baseline-snapshot", help="compare current inventory to an existing snapshot outside context (cwd-relative)")
    parser.add_argument("--max-bytes", type=nonnegative, help="included payload budget; applies to candidate when given")
    parser.add_argument("--largest", default=10, type=nonnegative, help="number of largest included files (default 10)")
    parser.add_argument("--max-dir-bytes", action="append", default=[], metavar="DIR=BYTES",
                        help="repeatable directory subtree budget (context-relative existing directory)")
    parser.add_argument("--max-extension-bytes", action="append", default=[], metavar="EXT=BYTES",
                        help="repeatable final suffix budget, e.g. .bin=1000 or '<none>=1000'")
    parser.add_argument("--max-dir-extension-bytes", action="append", nargs=2, default=[],
                        metavar=("DIR", "EXT=BYTES"),
                        help="repeatable intersection budget, e.g. build .bin=1000")
    parser.add_argument("--top", default=10, type=nonnegative,
                        help="maximum rows per contribution dimension and ignore suggestions (default 10)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--version", action="version", version=__version__)
    options = parser.parse_args(args_list)
    if compare and options.candidate_ignore is None:
        parser.error("compare requires --candidate-ignore")
    try:
        saved = (read_snapshot(snapshot_path(options.baseline_snapshot, options.context))
                 if options.baseline_snapshot is not None else None)
        output = (snapshot_path(options.save_snapshot, options.context)
                  if options.save_snapshot is not None else None)
        report = analyze(options.context, dockerfile=options.dockerfile, ignore=options.ignore,
                         candidate_ignore=options.candidate_ignore, max_bytes=options.max_bytes,
                         max_dir_bytes=dimension_limits(options.max_dir_bytes),
                         max_extension_bytes=dimension_limits(options.max_extension_bytes), top=options.top,
                         max_dir_extension_bytes=intersection_limits(options.max_dir_extension_bytes),
                         largest=options.largest,
                         include_link_targets=not (saved is not None or output is not None))
        if output is not None:
            write_snapshot(output, from_report(report))
            report["snapshot_written"] = str(output)
        if saved is not None:
            comparison = compare_snapshots(saved, from_report(report))
            comparison["baseline_snapshot"] = str(snapshot_path(options.baseline_snapshot, options.context))
            report["snapshot_comparison"] = comparison
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        message = str(exc) if not isinstance(exc, RecursionError) else "context nesting exceeds supported Python recursion depth"
        if options.json:
            print(json.dumps({"schema_version": 1, "status": "error", "error": message}, ensure_ascii=True))
        else:
            print(f"stackwrench context-budget: {message!r}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True) if options.json else render(report))
    return 1 if report["status"] == "budget_exceeded" else 0
