"""Read-only CLI. All successful data goes to stdout; errors go to stderr."""
import argparse
import json
import os
import stat
import sys

from . import __version__
from .core import MAX_PATH_QUERIES, MergeError, _validate_paths, merge_files
from .compare import compare_files


MAX_PATH_JSON_BYTES = 8 * 1024 * 1024


def json_path(text):
    """Decode one JSON string so pointers containing NUL can travel in argv."""
    try:
        path = json.loads(text)
    except (ValueError, RecursionError):
        raise argparse.ArgumentTypeError("must encode a JSON string") from None
    if not isinstance(path, str):
        raise argparse.ArgumentTypeError("must encode a JSON string")
    return path


def _path_json_file_text(filename):
    """Read bounded strict UTF-8 from an explicitly named regular file."""
    try:
        # Nonblocking open lets us reject FIFOs before any read can wait on them.
        fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise argparse.ArgumentTypeError("path JSON input must be a regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_PATH_JSON_BYTES + 1)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"cannot read path JSON file: {exc}") from None
    if len(data) > MAX_PATH_JSON_BYTES:
        raise argparse.ArgumentTypeError(f"path JSON file exceeds {MAX_PATH_JSON_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise argparse.ArgumentTypeError("path JSON file must be valid UTF-8") from None
    return text


def json_path_file(filename):
    """Decode one JSON pointer string, retaining the singular file contract."""
    return json_path(_path_json_file_text(filename))


def json_paths_file(filename):
    """Decode a bounded pointer array from one explicitly named regular file."""
    return json_paths(_path_json_file_text(filename))


def _path_json_stdin_text():
    """Read bounded strict UTF-8 from explicitly requested stdin."""
    try:
        data = sys.stdin.buffer.read(MAX_PATH_JSON_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"cannot read path JSON stdin: {exc}") from None
    if len(data) > MAX_PATH_JSON_BYTES:
        raise argparse.ArgumentTypeError(f"path JSON stdin exceeds {MAX_PATH_JSON_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise argparse.ArgumentTypeError("path JSON stdin must be valid UTF-8") from None
    return text


def json_path_stdin():
    """Decode one JSON pointer string, retaining the singular transport contract."""
    return json_path(_path_json_stdin_text())


def json_paths_stdin():
    return json_paths(_path_json_stdin_text())


def json_paths_argv(text):
    """Bound inline UTF-8 bytes before sharing the batch shape/count decoder."""
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise argparse.ArgumentTypeError("path JSON argument must be valid UTF-8") from None
    if size > MAX_PATH_JSON_BYTES:
        raise argparse.ArgumentTypeError(f"path JSON argument exceeds {MAX_PATH_JSON_BYTES} bytes")
    return json_paths(text)


def json_paths(text):
    """Decode one bounded array; count every entry, including duplicates."""
    message = f"must encode a JSON array of 1..{MAX_PATH_QUERIES} strings"
    try:
        paths = json.loads(text)
        _validate_paths(paths)
    except (ValueError, RecursionError):
        raise argparse.ArgumentTypeError(message) from None
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stackwrench compose-merge", description="Offline merge/explain for a documented literal Compose subset; not Compose canonical output.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "explain", "history-paths", "trace"):
        help_text = {"history-paths": "discover distinct history pointers in first-event order",
                     "trace": "export all recorded events in chronological order"}.get(name)
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("-f", "--file", action="append", required=True, help="literal UTF-8 YAML, in merge order (repeatable)")
        if name == "explain":
            paths = cmd.add_mutually_exclusive_group(required=True)
            paths.add_argument("--path", help="literal RFC 6901 JSON pointer; empty string means root")
            paths.add_argument("--path-json", dest="path", type=json_path,
                               help=r'JSON-encoded RFC 6901 pointer, decoded once; e.g. "/x-data/\u0000"; "" means root')
            paths.add_argument("--paths-json", dest="paths", type=json_paths_argv, metavar="ARRAY",
                               help="inline JSON array of 1..64 pointer strings (max 8 MiB UTF-8; OS argv limits apply); emit ordered explanations")
            paths.add_argument("--path-json-file", dest="path", type=json_path_file,
                               help="UTF-8 regular file containing one JSON pointer string (max 8 MiB); '-' is a literal filename")
            paths.add_argument("--paths-json-file", dest="paths", type=json_paths_file,
                               help="UTF-8 regular file containing one JSON array of 1..64 pointer strings (max 8 MiB); '-' is literal; emit ordered explanations")
            paths.add_argument("--path-json-stdin", action="store_true",
                               help="read one UTF-8 JSON pointer string from stdin (max 8 MiB; waits for EOF or limit)")
            paths.add_argument("--paths-json-stdin", action="store_true",
                               help="read one UTF-8 JSON array of 1..64 pointer strings from stdin (max 8 MiB); emit ordered explanations")
    compare = commands.add_parser("compare", help="compare two explicit orders of the same files")
    for side in ("left", "right"):
        compare.add_argument("--" + side, action="append", required=True,
                             help="literal UTF-8 YAML in this order (repeat for each file)")
    args = parser.parse_args(argv)
    if args.command == "explain" and (args.path_json_stdin or args.paths_json_stdin):
        # Resolve conflicts/help before reading a pipe that may still be open.
        try:
            if args.paths_json_stdin:
                args.paths = json_paths_stdin()
            else:
                args.path = json_path_stdin()
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    try:
        if args.command == "compare":
            result = compare_files(args.left, args.right)
        else:
            project = merge_files(args.file)
            if args.command == "render":
                result = project.render()
            elif args.command == "history-paths":
                result = project.history_paths()
            elif args.command == "trace":
                result = {"events": project.events}
            elif args.paths is not None:
                result = project.explain_many(args.paths)
            else:
                result = project.explain(args.path)
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
        return 0
    except MergeError as e:
        print(f"stackwrench compose-merge: error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
