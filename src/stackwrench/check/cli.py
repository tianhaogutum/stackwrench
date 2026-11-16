"""``stackwrench check [REPO]``: a read-only doctor for a repository's local dev stack.

Exit status: 0 no finding at the --fail-on threshold, 1 findings at or above
it, 2 usage or input error (unknown option or check, REPO is not a folder).
Read-only: no file is written, nothing is executed, docker and podman are never called.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from .discover import DiscoveryError, discover
from .registry import AREAS, CHECKS, CheckOptions, run_checks, select
from .report import RENDERERS, Run, render_text

PROG = "stackwrench check"
FAIL_ON = {"error": "error", "warning": "warning", "never": None}
SIZE = re.compile(r"(?i)\s*([0-9]+(?:\.[0-9]+)?)\s*(b|kb|k|mb|m|gb|g|kib|mib|gib)?\s*")
UNITS = {None: 1, "b": 1, "k": 10 ** 3, "kb": 10 ** 3, "m": 10 ** 6, "mb": 10 ** 6, "g": 10 ** 9, "gb": 10 ** 9,
         "kib": 2 ** 10, "mib": 2 ** 20, "gib": 2 ** 30}
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")

EPILOG = """\
examples:
  stackwrench check                              check the current directory
  stackwrench check path/to/repo -v              with a fix hint under each finding
  stackwrench check . --format markdown >> "$GITHUB_STEP_SUMMARY"
  stackwrench check . --format sarif > stackwrench.sarif
  stackwrench check . --only compose-env,pins --fail-on warning
  stackwrench check . --context-budget 50MB
  stackwrench check --list-checks

exit status: 0 nothing at the --fail-on level, 1 findings at or above it,
2 usage or input error. Nothing is executed or written; docker, podman,
kind and Task are never called.
Docs: https://github.com/tianhaogutum/stackwrench/blob/main/docs/check.md
"""


def _names(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("expected one or more check names, comma separated")
    return names


def parse_size(value: str) -> int:
    """``500000``, ``500k``, ``50MB`` (decimal) or ``48MiB`` (binary) to bytes."""
    match = SIZE.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError("expected a byte count such as 500000, 500kB, 50MB or 48MiB")
    number, unit = match.groups()
    return int(float(number) * UNITS[unit.lower() if unit else None])


def _version(value: str) -> str:
    if not VERSION.fullmatch(value):
        raise argparse.ArgumentTypeError("expected an exact version such as 1.6.0")
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Find the Compose files, .env, devcontainer.json, mise.toml and version files, Dockerfiles "
                    "and build contexts, justfile, Taskfile and kind configs in REPO and report what will break "
                    "the local dev stack: environment mix-ups, override surprises, Podman differences, broken "
                    "Dev Container paths, version drift, oversized build contexts, cache-busting COPYs and "
                    "task-runner gaps.")
    p.add_argument("repo", nargs="?", default=".", metavar="REPO", help="repository folder to check (default: .)")
    p.add_argument("--format", choices=("text", "json", "sarif", "markdown"), default="text",
                   help="report format (default: text); markdown suits a PR comment or $GITHUB_STEP_SUMMARY, "
                        "sarif is SARIF 2.1.0 for code scanning")
    p.add_argument("--fail-on", choices=tuple(FAIL_ON), default="error",
                   help="exit 1 when a finding is at least this severe (default: error)")
    p.add_argument("--only", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="run only these checks (repeatable)")
    p.add_argument("--skip", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="do not run these checks (repeatable)")
    p.add_argument("--list-checks", action="store_true", help="list the available checks and exit")
    p.add_argument("--context-budget", type=parse_size, metavar="SIZE",
                   help="warn when a build context sends more than SIZE, e.g. 50MB (default: report sizes only)")
    p.add_argument("--podman-compose-version", type=_version, default="1.6.0", metavar="X.Y.Z",
                   help="podman-compose version to audit against (default: 1.6.0, the version the bundled "
                        "evidence covers)")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="colour text output (default: auto; honours NO_COLOR)")
    p.add_argument("-v", "--verbose", action="store_true", help="text: show a fix hint under each finding")
    return p


def list_checks() -> str:
    width = max(len(check.name) for check in CHECKS) + 2
    lines = ["Checks (select with --only/--skip):"]
    for area in AREAS:
        group = [check for check in CHECKS if check.area == area]
        if not group:
            continue
        lines += ["", f"{area}:"]
        for check in group:
            lines.append(f"  {check.name.ljust(width)}{check.summary}")
            lines.append(f"  {''.ljust(width)}uses: stackwrench {check.uses}; rules: {', '.join(check.rules)}")
    return "\n".join(lines) + "\n"


def _use_color(choice: str) -> bool:
    if choice != "auto":
        return choice == "always"
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.list_checks:
        sys.stdout.write(list_checks())
        return 0
    try:
        checks = select([name for group in args.only for name in group],
                        [name for group in args.skip for name in group])
        if not checks:
            raise ValueError("--only/--skip leave no check to run")
        discovery = discover(args.repo)
    except (ValueError, DiscoveryError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 2
    options = CheckOptions(context_budget=args.context_budget, podman_compose_version=args.podman_compose_version)
    outcome = run_checks(discovery, checks, options)
    run = Run(args.repo, discovery, checks, outcome.findings, outcome.skipped, FAIL_ON[args.fail_on])
    if args.format == "text":
        output = render_text(run, color=_use_color(args.color), verbose=args.verbose)
    else:
        output = RENDERERS[args.format](run)
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
