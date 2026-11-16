"""Command-line interface. Exit 0: success, 1: check drift, 2: blocked/error."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import Report, inspect
from .fs import InputError, Root


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="stackwrench pins", description="Reconcile exact mise pins offline; never installs runtimes or runs containers.")
    result.add_argument("--version", action="version", version=__version__)
    subcommands = result.add_subparsers(dest="command", required=True)
    for name in ("check", "plan", "apply"):
        sub = subcommands.add_parser(name)
        sub.add_argument("--root", required=True, help="project directory (no symlinks)")
        sub.add_argument("--mise", default="mise.toml", help="root-relative source (default: mise.toml)")
        sub.add_argument("--dockerfile", action="append", help="root-relative Dockerfile; repeatable, replaces default Dockerfile selection")
        sub.add_argument("--version-file", action="append", help="root-relative runtime file; repeatable, replaces the four default selections")
        sub.add_argument("--workflow", action="append", help="root-relative Actions workflow; repeatable, explicit only (no discovery)")
        if name == "check":
            sub.add_argument("--json", action="store_true", help="emit schemaVersion 1 JSON, including input errors")
        elif name == "plan":
            output = sub.add_mutually_exclusive_group()
            output.add_argument("--json", action="store_true", help="emit a read-only per-file explanation and diff as JSON")
            output.add_argument("--explain", action="store_true", help="explain every comparison and unsafe declaration, including blocked batches")
    return result


def show_findings(report: Report, *, declaration_identities: bool = False) -> None:
    pin_kinds = {
        "unsupported_action_pin": ("actions-version",),
        "unsupported_pin": ("mise", "version-file"),
        "missing_pin": ("actions-version", "version-file", "docker-image"),
        "digest_pinned": ("docker-image",),
        "dynamic_image": ("docker-image",),
        "unsupported_image": ("docker-image",),
    }
    blocked_pins: dict[tuple[str, int], list[dict]] = {}
    if declaration_identities:
        for item in report.declarations:
            if item["kind"] in ("mise", "actions-version", "version-file", "docker-image") and item["status"] == "blocked" and item["line"] is not None:
                blocked_pins.setdefault((item["path"], item["line"]), []).append(item)
    for item in report.findings:
        location = item["path"] + (f":{item['line']}" if "line" in item else "")
        suffix = ""
        if item["code"] in pin_kinds and item.get("line") is not None:
            candidates = [row for row in blocked_pins.get((item["path"], item["line"]), [])
                          if row["kind"] in pin_kinds[item["code"]]]
            if len(candidates) == 1:
                declaration = candidates[0]
                if declaration["kind"] != "mise":
                    if declaration["source"] is not None and declaration["sourcePath"] is not None:
                        source_line = declaration["sourceLine"] if declaration["sourceLine"] is not None else "?"
                        suffix = f" (source {declaration['source']}; location {declaration['sourcePath']}:{source_line})"
                    suffix += f"; declaration: {json.dumps(declaration['declaration'], ensure_ascii=True)}"
                old_value = declaration.get("oldValue")
                if item["code"] in ("unsupported_pin", "unsupported_action_pin") and isinstance(old_value, str):
                    suffix += f"; value: {json.dumps(old_value, ensure_ascii=True)}"
        print(f"{location}: {item['code']}: {item['message']}{suffix}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = None
    try:
        root = Root(args.root)
        report = inspect(root, args.mise, args.dockerfile, args.version_file, args.workflow)
        if args.command == "apply" and not report.findings:
            try:
                root.apply(report.snapshots, report.updates)
            except InputError as exc:
                report.error(exc)
    except (InputError, OSError) as exc:
        report = Report(args.root, args.mise)
        report.error(exc if isinstance(exc, InputError) else InputError(args.root, str(exc), "io_error"))
    finally:
        if root is not None:
            root.close()

    if args.command in ("check", "plan") and args.json:
        print(json.dumps(report.plan_dict() if args.command == "plan" else report.as_dict(), ensure_ascii=True, indent=2))
    elif args.command == "plan" and args.explain:
        sys.stdout.write(report.explanation())
    elif report.findings:
        show_findings(report, declaration_identities=args.command == "check")
    elif args.command == "plan":
        sys.stdout.write(report.diff())
    elif args.command == "apply":
        print(f"Updated {len(report.updates)} file(s)." if report.updates else "No changes.")
    else:
        for item in report.declarations:
            if item["mismatch"]:
                source_line = item["sourceLine"] if item["sourceLine"] is not None else "?"
                print(f"{item['path']}:{item['line']}: {item['tool']}: {item['actual']} -> {item['expected']} "
                      f"(source {item['source']}; location {item['sourcePath']}:{source_line}); "
                      f"declaration: {json.dumps(item['declaration'], ensure_ascii=True)}")
        print("Drift detected." if report.mismatch else "All evaluated declarations match.")
    if report.findings:
        return 2
    return 1 if args.command == "check" and report.mismatch else 0


if __name__ == "__main__":
    raise SystemExit(main())
