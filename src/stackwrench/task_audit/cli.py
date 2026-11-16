"""Command-line entry points. Inputs are data and are never executed."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .audit import audit, has_concerns
from .formats import AuditError, load_env
from .snapshots import compare, read_snapshot, validate_snapshot
from .suggestions import has_suggestion_concerns, render_suggestions, suggest


def parser():
    root = argparse.ArgumentParser(prog="stackwrench task-audit", description="Offline advisory Taskfile input audit; never executes commands.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("check", "snapshot", "suggest"):
        sub = commands.add_parser(command)
        sub.add_argument("taskfile", type=Path)
        sub.add_argument("--root", type=Path, help="source glob root (default: Taskfile parent)")
        sub.add_argument("--env-snapshot", type=Path, help="explicit JSON string-to-string environment map; host env is ignored")
        sub.add_argument("--task", action="append", help="audit a named task; repeat to select several")
        sub.add_argument("--strict", action="store_true", help="exit 1 for any warning, unassessed syntax or unresolved reference")
        if command in ("check", "suggest"):
            sub.add_argument("--json", action="store_true")
        else:
            sub.add_argument("--output", type=Path, required=True, help="new snapshot file; refuses overwrite, created mode 0600")
    sub = commands.add_parser("compare", help="compare snapshots and explain source/input/definition changes")
    sub.add_argument("before", type=Path)
    sub.add_argument("after", type=Path)
    sub.add_argument("--json", action="store_true")
    return root


def safe_lines(lines):
    return "\n".join(json.dumps(line, ensure_ascii=True)[1:-1] for line in lines)


def render_text(report):
    if report["kind"].endswith(".suggestions"):
        return render_suggestions(report, safe_lines)
    if report["kind"].endswith(".comparison"):
        lines = ["Advisory snapshot comparison; native Task execution decision: unknown; cache reuse authorized: false"]
        for name, task in report["tasks"].items():
            lines.append(f"{name}: {task['change']}")
            lines.append(f"  assessment: {task['assessment_before'] or 'absent'} -> {task['assessment_after'] or 'absent'}")
            for reason in task.get("reasons", []):
                lines.append(f"  reason: {reason}")
            for change in task["source_changes"]:
                lines.append(f"  source_changes: {change['name']} ({change['change']})")
            if not task["definition_changes_complete"]:
                lines.append("  definition fields: unavailable (legacy snapshot)")
            else:
                for change in task["definition_changes"]:
                    lines.append(f"  definition: {change['field']} ({change['change']})")
            for side in ("before", "after"):
                context = task[f"definition_entry_context_{side}"]
                description = (f"available; fields: {', '.join(context['fields']) or '(none)'}"
                               if context["state"] == "available" else context["state"])
                lines.append(f"  definition entries {side}: {description}")
            if not task["definition_entry_changes_complete"]:
                lines.append("  definition entries: unavailable (legacy snapshot)")
            else:
                for change in task["definition_entry_changes"]:
                    field, index = change["location"]
                    lines.append(f"  definition entry: [{field}, {index}] ({change['change']})")
                    lines.append(f"    before digest: {change['before_digest'] or 'absent entry'}")
                    lines.append(f"    after digest: {change['after_digest'] or 'absent entry'}")
            for side in ("before", "after"):
                context = task[f"definition_subfield_context_{side}"]
                lines.append(f"  definition subfields {side}: {context['state']}")
                if context["state"] == "available":
                    fields = context["fields"]
                    if "cmds" not in fields:
                        lines.append("    cmds: absent field")
                    elif not fields["cmds"]:
                        lines.append("    cmds: empty list")
                    else:
                        for index, keys in enumerate(fields["cmds"]):
                            description = ", ".join(keys) if keys is not None else "opaque entry (outside supported projection)"
                            lines.append(f"    [cmds, {index}]: {description}")
            if not task["definition_subfield_changes_complete"]:
                lines.append("  definition subfields: unavailable (legacy snapshot)")
            else:
                for change in task["definition_subfield_changes"]:
                    field, index, key = change["location"]
                    lines.append(f"  definition subfield: [{field}, {index}, {key}] ({change['change']})")
                    lines.append(f"    before digest: {change['before_digest'] or 'absent supported child'}")
                    lines.append(f"    after digest: {change['after_digest'] or 'absent supported child'}")
            for change in task["input_changes"]:
                lines.append(f"  input: {change['name']} ({change['change']}); changed fields: {', '.join(change['changed_fields'])}")
                for side in ("before", "after"):
                    value = change[side]
                    description = "absent" if value is None else (
                        f"{value['classification']}; {value['origin'] or 'unknown origin'}; "
                        f"{'<redacted>' if value['resolved'] else 'unknown value'}")
                    lines.append(f"    {side}: {description}")
            for side in ("before", "after"):
                context = task[f"context_{side}"]
                if context is None:
                    continue
                for key, value in context["unresolved_inputs"].items():
                    lines.append(f"  {side} unresolved: {key} ({value['classification']}; {value['origin'] or 'unknown origin'}; unknown value)")
                for item in context["unassessed_findings"]:
                    lines.append(f"  {side} {item['scope']} unassessed: {item['code']} {item.get('reference', item.get('field', item.get('name', '')))}: {item['message']}")
        return safe_lines(lines)
    lines = ["Offline advisory audit; native Task execution decision: unknown; values: redacted"]
    for item in report["findings"]:
        lines.append(f"root: {item['severity']} {item['code']} {item.get('field', item.get('name', ''))}: {item['message']}")
    for name, task in report["tasks"].items():
        lines.append(f"{name}: {task['assessment']}; {len(task['source_hashes'])} source file(s); advisory {task['advisory_fingerprint']}")
        for key, val in task["inputs"].items():
            lines.append(f"  {key}: {val['classification']}; {val['origin'] or 'unknown origin'}; {'<redacted>' if val['resolved'] else 'unknown value'}")
        for item in task["findings"]:
            lines.append(f"  {item['severity']} {item['code']} {item.get('reference', item.get('field', item.get('name', '')))}: {item['message']}")
    return safe_lines(lines)


def write_snapshot(path, report):
    # Exclusive creation prevents accidental changes to Taskfiles, sources or prior snapshots.
    import os
    payload = (json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > 16 * 1024 * 1024:
        raise AuditError("snapshot exceeds 16 MiB; select fewer tasks using --task")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
    except OSError:
        raise AuditError("cannot create snapshot output (must be a new writable file)") from None


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "compare":
            report = compare(read_snapshot(args.before), read_snapshot(args.after))
        elif args.command == "suggest":
            report = suggest(args.taskfile, args.root or args.taskfile.parent, load_env(args.env_snapshot), args.task)
        else:
            report = audit(args.taskfile, args.root or args.taskfile.parent, load_env(args.env_snapshot), args.task)
            validate_snapshot(report)
        if args.command == "snapshot":
            write_snapshot(args.output, report)
            print("Snapshot written; values redacted; native Task execution decision: unknown")
        elif args.json:
            print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False))
        else:
            # Escape control characters from user-controlled names in terminal output.
            print(render_text(report))
        concerns = has_suggestion_concerns if args.command == "suggest" else has_concerns
        return 1 if getattr(args, "strict", False) and concerns(report) else 0
    except (AuditError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, AuditError) else "input nesting exceeds supported depth"
        print(f"stackwrench task-audit: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
