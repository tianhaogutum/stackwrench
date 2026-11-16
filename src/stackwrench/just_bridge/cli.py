"""Offline translation, plus opt-in execution of built-in acceptance fixtures."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import json
import os
import stat
from pathlib import Path
import sys
import tempfile

from . import __version__
from .core import Analysis, Diagnostic, MAX_INPUT_BYTES, analyze, render_dot, render_taskfile
from .core import _source_byte_limit, _source_limit_message


def _load(path: Path, recipes: list[str] | None = None,
          max_source_bytes: int | None = None) -> Analysis:
    limit = _source_byte_limit(max_source_bytes)
    content = bytearray()
    # A pathname check can describe a different file from the one we open.
    # Nonblocking open also handles a FIFO swapped in just before this call.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("source must be a regular UTF-8 file")
        # Raw reads prevent read-ahead beyond the N+1 overflow probe.
        # A short read is not EOF, and initial file size is not a snapshot.
        while len(content) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
    finally:
        os.close(fd)
    if len(content) > limit:
        report = Analysis(str(path), requested_roots=(
            list(dict.fromkeys(recipes)) if recipes is not None else None),
            max_source_bytes=max_source_bytes)
        report.diagnostics.append(Diagnostic("error", "input-limit", 1,
                                              _source_limit_message(max_source_bytes)))
        return report
    return analyze(content.decode("utf-8"), str(path), recipes=recipes,
                   max_source_bytes=max_source_bytes)


def _print_diagnostics(analysis: Analysis) -> None:
    for diagnostic in analysis.diagnostics:
        print(f"{analysis.source}:{diagnostic.line}: {diagnostic.severity} "
              f"[{diagnostic.code}]: {diagnostic.message}", file=sys.stderr)


def _text_plan(analysis: Analysis) -> str:
    report = analysis.report()
    lines = [f"Source: {analysis.source}",
             f"Supported: {'yes' if analysis.supported else 'no'}"]
    if analysis.max_source_bytes is not None:
        lines.append(f"Max source bytes: {analysis.max_source_bytes}")
    if "selection" in report:
        selection = report["selection"]
        lines.append("Validation scope: full source and dependency graph (before selection)")
        lines.append(f"Selected roots: {', '.join(selection['requestedRoots'])}")
        for label, field in (("Included recipes", "includedRecipes"), ("Omitted recipes", "omittedRecipes")):
            names = selection[field]
            names_text = "(unavailable)" if names is None else ", ".join(names) or "(none)"
            lines.append(f"{label}: {names_text}")
    for name, dependencies in report["graph"].items():
        lines.append(f"  {name} -> {', '.join(dependencies) or '(none)'}")
    if analysis.supported:
        default = report['mappings']['default']
        lines.append(f"Default: {default['sourceRecipe']} -> task default" if default else
                     "Default: none; invoke included tasks by name (task NAME)")
        for recipe in report["mappings"]["recipes"]:
            lines.append(f"Mapping: {recipe['sourceRecipe']} -> tasks.{recipe['task']}")
            for command in recipe["commands"]:
                lines.append(f"  line {command['line']}: {command['taskCommand']!r}")
        for root, order in report["traces"].items():
            lines.append(f"Trace {root}: {' -> '.join(order)}")
    for diagnostic in analysis.diagnostics:
        lines.append(f"{analysis.source}:{diagnostic.line}: {diagnostic.severity} "
                     f"[{diagnostic.code}]: {diagnostic.message}")
    return "\n".join(lines) + "\n"


def _write_stdout(content: str, *, utf8: bool = False,
                  retained_report: Path | None = None) -> bool:
    stream = sys.stdout
    try:
        if utf8 and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict", newline="\n")
        stream.write(content)
        # Small output may still be buffered; delivery is not complete until
        # flush succeeds, even when the analysis itself failed.
        stream.flush()
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        # Do not retry this sink at interpreter shutdown (which can change the
        # handled exit code to 120). Embedders must restore/replace failed stdout.
        sys.stdout = None
        try:
            descriptor = stream.fileno()
            with open(os.devnull, "wb") as sink:
                # Discard pending bytes if the original wrapper is later closed
                # or finalized, including the reference held by sys.__stdout__.
                os.dup2(sink.fileno(), descriptor)
        except (AttributeError, OSError, ValueError, RuntimeError):
            # In-memory/custom streams need not expose a usable descriptor.
            pass
        retained = f"; report retained at {retained_report}" if retained_report is not None else ""
        print(f"<stdout>: error [output-error]: {exc}{retained}", file=sys.stderr)
        return False
    return True


@contextmanager
def _stage_new_file(destination: Path, content: str):
    """Stage a complete, synced and closed file; remove it on context exit."""
    staged = None
    try:
        # Same directory ensures publication stays on one filesystem. The
        # private temporary file is never exposed under the requested name
        # until write, flush, fsync and close have all succeeded.
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=destination.parent, prefix=".stackwrench-just-", suffix=".tmp") as stream:
            staged = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        yield staged
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _write_new_file(destination: Path, content: str) -> None:
    """Publish a fully written file atomically, without replacing any entry."""
    with _stage_new_file(destination, content) as staged:
        # Unlike rename/replace, link fails if *any* destination entry exists,
        # even a dangling symlink or a file created during staging.
        os.link(staged, destination)


def _byte_limit(value: str, maximum: int) -> int:
    try:
        if not value.isascii() or not value.isdecimal():
            raise ValueError
        digits = value.lstrip('0')
        if len(digits) > len(str(maximum)):
            raise ValueError
        limit = int(digits or '0')
        if not 1 <= limit <= maximum:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(
            f'must be an ASCII decimal integer from 1 to {maximum}') from None
    return limit


def _file_byte_limit(value: str) -> int:
    from .report_check import MAX_FILE_BYTE_LIMIT
    return _byte_limit(value, MAX_FILE_BYTE_LIMIT)


def _report_byte_limit(value: str) -> int:
    from .report_check import MAX_REPORT_BYTES
    return _byte_limit(value, MAX_REPORT_BYTES)


def _source_byte_argument(value: str) -> int:
    return _byte_limit(value, MAX_INPUT_BYTES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stackwrench just",
        description="Offline just → Taskfile v3 translation; opt-in verification runs built-in fixtures only.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="action", required=True)
    plan = commands.add_parser("plan", help="inspect graph, mappings, dependency traces and losses")
    plan.add_argument("source", type=Path, help="UTF-8 justfile (no imports are read)")
    plan.add_argument("--json", action="store_true", help="machine-readable report, including errors")
    graph = commands.add_parser("graph", help="export validated recipe graph as Graphviz DOT (prerequisite -> dependent)")
    graph.add_argument("source", type=Path, help="UTF-8 justfile (no imports are read)")
    convert = commands.add_parser("convert", help="validate everything, then write a new Taskfile")
    convert.add_argument("source", type=Path)
    convert.add_argument("--output", "-o", type=Path,
                         help="new destination only; omit or use '-' for stdout")
    convert.add_argument("--report", choices=("text", "json"),
                         help="after successful conversion, emit mappings, traces and diagnostics to stderr")
    convert.add_argument("--report-output", type=Path, metavar="NEW-PATH",
                         help="new JSON sidecar; published before Taskfile output (never overwritten)")
    for command in (plan, convert, graph):
        command.add_argument("--recipe", action="append", metavar="NAME",
                             help="include this exact recipe and its dependencies; repeat for multiple roots; validates entire source")
        command.add_argument("--max-source-bytes", type=_source_byte_argument, metavar="N",
                             help="optional inclusive original source byte limit (1..1048576; default 1048576)")
    check = commands.add_parser("check-report", help="compare saved analysis fingerprints with explicit file bytes")
    check.add_argument("source", type=Path)
    check.add_argument("--taskfile", type=Path, required=True)
    check.add_argument("--report", type=Path, required=True, help="saved analysis JSON (read only)")
    check.add_argument("--output", "-o", type=Path,
                       help="new JSON result destination only; omit or use '-' for stdout")
    check.add_argument("--max-file-bytes", type=_file_byte_limit, metavar="N",
                       help="optional inclusive byte limit for each source/Taskfile (1..2^63-1)")
    check.add_argument("--max-report-bytes", type=_report_byte_limit, metavar="N",
                       help="optional inclusive byte limit for saved analysis JSON (1..67108864)")
    verify = commands.add_parser("verify", help="compare built-in synthetic fixtures using installed just and Task only")
    verify.add_argument("--output", "-o", type=Path,
                        help="new JSON report destination only; omit or use '-' for stdout")
    args = parser.parse_args(argv)
    if args.action == "convert" and args.report_output is not None and str(args.report_output) == "-":
        parser.error("--report-output requires a named path; '-' is not supported")
    if args.action == "check-report":
        from .report_check import EXIT_CODES, check_report
        report = check_report(args.source, args.taskfile, args.report,
                              max_file_bytes=args.max_file_bytes,
                              max_report_bytes=args.max_report_bytes)
        try:
            content = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
            if args.output is None or str(args.output) == "-":
                if not _write_stdout(content, utf8=True):
                    return 3
            else:
                if os.path.lexists(args.output):
                    raise FileExistsError("result destination already exists; never overwritten")
                if any(args.output.resolve() == path.resolve()
                       for path in (args.source, args.taskfile, args.report)):
                    raise FileExistsError("result destination must be distinct from all input paths")
                _write_new_file(args.output, content)
        except (OSError, RuntimeError) as exc:
            destination = args.output if args.output is not None and str(args.output) != "-" else "<stdout>"
            print(f"{destination}: error [output-error]: {exc}", file=sys.stderr)
            return 3
        return EXIT_CODES[report["status"]]
    if args.action == "verify":
        from .acceptance import EXIT_CODES, verify as verify_fixtures
        try:
            report = verify_fixtures()
            content = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
            if args.output is None or str(args.output) == "-":
                if not _write_stdout(content, utf8=True):
                    return 3
            else:
                _write_new_file(args.output, content)
        except OSError as exc:
            print(f"{args.output or '<stdout>'}: error [output-error]: {exc}", file=sys.stderr)
            return 3
        return EXIT_CODES[report["status"]]
    try:
        analysis = _load(args.source, args.recipe, args.max_source_bytes)
    except (OSError, UnicodeError) as exc:
        analysis = Analysis(str(args.source), requested_roots=(
            list(dict.fromkeys(args.recipe)) if args.recipe is not None else None),
            max_source_bytes=args.max_source_bytes)
        analysis.diagnostics.append(Diagnostic("error", "input-error", 1, str(exc)))
        if args.action == "plan" and args.json:
            _write_stdout(json.dumps(analysis.report(), ensure_ascii=True, indent=2) + "\n")
        else:
            _print_diagnostics(analysis)
        return 3
    if args.action == "plan":
        content = (json.dumps(analysis.report(), ensure_ascii=True, indent=2) + "\n"
                   if args.json else _text_plan(analysis))
        if not _write_stdout(content):
            return 3
        return 0 if analysis.supported else 2
    if args.action == "graph":
        if not analysis.supported:
            _print_diagnostics(analysis)
            return 2
        return 0 if _write_stdout(render_dot(analysis), utf8=True) else 3
    if not analysis.supported or args.report is None:
        _print_diagnostics(analysis)
    if not analysis.supported:
        return 2
    content = render_taskfile(analysis)
    output = args.output if args.output is not None and str(args.output) != "-" else None
    destination = output or "<stdout>"
    report_published = False
    try:
        with ExitStack() as staging:
            report_stage = None
            if args.report_output is not None:
                destination = args.report_output
                if os.path.lexists(destination):
                    raise FileExistsError("report destination already exists; never overwritten")
                if output is not None and destination.resolve() == output.resolve():
                    raise FileExistsError("report and Taskfile destinations must be distinct")
                # Fail before publishing a report if a named Taskfile already
                # exists. Publication still uses link to close the race.
                if output is not None and os.path.lexists(output):
                    destination = output
                    raise FileExistsError("Taskfile destination already exists; never overwritten")
                report_stage = staging.enter_context(_stage_new_file(
                    args.report_output, json.dumps(analysis.report(), ensure_ascii=True, indent=2) + "\n"))
            destination = output or "<stdout>"
            if output is not None:
                if output.resolve() == args.source.resolve():
                    raise FileExistsError("destination is the source file; never overwritten")
                taskfile_stage = staging.enter_context(_stage_new_file(output, content))
            if report_stage is not None:
                destination = args.report_output
                os.link(report_stage, args.report_output)
                report_published = True
            destination = output or "<stdout>"
            if output is not None:
                os.link(taskfile_stage, output)
            else:
                # Match the fingerprint and named output even under an explicit
                # non-UTF-8 PYTHONIOENCODING or platform newline translation.
                # In-process text captures (e.g. StringIO) already keep text as-is.
                if not _write_stdout(content, utf8=True, retained_report=(
                        args.report_output if report_published else None)):
                    return 3
    except (OSError, RuntimeError) as exc:
        retained = f"; report retained at {args.report_output}" if report_published else ""
        print(f"{destination}: error [output-error]: {exc}{retained}", file=sys.stderr)
        return 3
    if args.report == "json":
        print(json.dumps(analysis.report(), ensure_ascii=True, indent=2), file=sys.stderr)
    elif args.report == "text":
        sys.stderr.write(_text_plan(analysis))
    return 0
