"""``stackwrench devcontainer``: command-line entry point.

Argument handling reproduces ``node:util`` ``parseArgs`` (strict, no
positionals) as used by the original ``devcontainer-wirecheck`` binary,
including its error messages; errors print ``stackwrench devcontainer: ...``
to stderr and exit 2.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from stackwrench import __version__

from .checks import check
from .jsutil import console_text, json_stringify
from .sarif import to_sarif

PROG = "stackwrench devcontainer"

HELP = f"""usage: {PROG} --config FILE --workspace ROOT [--json | --sarif] [--explain] [--strict]
  --json              Full wiring graph and diagnostics as JSON
  --sarif             SARIF 2.1.0 diagnostics for editors/CI (exclusive with --json)
  --compose-json FILE  Explicit pre-rendered Compose JSON (no merge is performed)
  --compose-base DIR   Base for relative Compose paths (default: first raw file dir)
  --daemon local|remote  Bind availability scope (default: local shared-filesystem assumption)
  --strict            Fail on warnings/unknowns as well as errors
  --explain           Show field locations, path bases, wiring targets and reasons
                      With --json, explanations remain in the single JSON report
                      With --sarif, stdout remains a single SARIF log
  --help              Show usage
  --version           Show version
Exit: 0 no errors (may be partial); 1 errors or strict warnings; 2 CLI/internal error.
No commands, hooks, containers, environment probes or network calls are executed."""

_OPTIONS: dict[str, str] = {
    "config": "string",
    "workspace": "string",
    "json": "boolean",
    "sarif": "boolean",
    "explain": "boolean",
    "compose-json": "string",
    "compose-base": "string",
    "daemon": "string",
    "strict": "boolean",
    "help": "boolean",
    "version": "boolean",
}
_SHORT = {"h": "help"}
_DEFAULTS = {"daemon": "local"}


class UsageError(Exception):
    pass


def _short_and_long(name: str) -> str:
    short = next((s for s, long in _SHORT.items() if long == name), None)
    return f"{'-' + short + ', ' if short else ''}--{name}"


def parse_args(argv: list[str]) -> dict[str, Any]:
    """``parseArgs({ options, allowPositionals: false })`` (strict mode)."""
    tokens: list[dict[str, Any]] = []
    remaining = list(argv)
    while remaining:
        arg = remaining.pop(0)
        if arg == "--":
            tokens.extend({"kind": "positional", "value": value} for value in remaining)
            break
        if len(arg) == 2 and arg[0] == "-" and arg[1] != "-":
            name = _SHORT.get(arg[1], arg[1])
            value = inline = None
            if _OPTIONS.get(name) == "string" and remaining:
                value, inline = remaining.pop(0), False
            tokens.append({"kind": "option", "name": name, "raw": arg, "value": value, "inline": inline})
            continue
        if len(arg) > 2 and arg[0] == "-" and arg[1] != "-":
            first = _SHORT.get(arg[1], arg[1])
            if _OPTIONS.get(first) != "string":
                # Short option group: expand -abc into -a -b -c.
                remaining[:0] = [f"-{ch}" for ch in arg[1:]]
                continue
            tokens.append({"kind": "option", "name": first, "raw": arg[:2], "value": arg[2:], "inline": True})
            continue
        if arg.startswith("--") and len(arg) > 2:
            if "=" in arg:
                name, value = arg[2:].split("=", 1)
                tokens.append({"kind": "option", "name": name, "raw": f"--{name}", "value": value, "inline": True})
            else:
                name = arg[2:]
                value = inline = None
                if _OPTIONS.get(name) == "string" and remaining:
                    value, inline = remaining.pop(0), False
                tokens.append({"kind": "option", "name": name, "raw": arg, "value": value, "inline": inline})
            continue
        tokens.append({"kind": "positional", "value": arg})

    values: dict[str, Any] = {}
    for token in tokens:
        if token["kind"] == "positional":
            raise UsageError(f"Unexpected argument '{token['value']}'. This command does not take positional arguments")
        name, raw, value = token["name"], token["raw"], token["value"]
        kind = _OPTIONS.get(name)
        if kind is None:
            raise UsageError(f"Unknown option '{raw}'")
        if kind == "string" and not isinstance(value, str):
            raise UsageError(f"Option '{_short_and_long(name)} <value>' argument missing")
        if kind == "boolean" and value is not None:
            raise UsageError(f"Option '{_short_and_long(name)}' does not take an argument")
        if not token["inline"] and isinstance(value, str) and len(value) > 1 and value[0] == "-":
            example = f"{raw}=-XYZ" if raw.startswith("--") else f"--{name}=-XYZ' or '{raw}-XYZ"
            raise UsageError(
                f"Option '{raw}' argument is ambiguous.\n"
                f"Did you forget to specify the option argument for '{raw}'?\n"
                f"To specify an option argument starting with a dash use '{example}'."
            )
        values[name] = True if kind == "boolean" else value
    for name, default in _DEFAULTS.items():
        values.setdefault(name, default)
    return values


def _emit(stream: TextIO, text: str) -> None:
    """``console.log``/``console.error``: one UTF-8 line, lone surrogates as U+FFFD."""
    data = console_text(text + "\n")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(data)
        buffer.flush()
    else:
        stream.write(data.decode("utf-8"))


def _location(loc: dict[str, Any], inferred: bool = False) -> str:
    text = f"{loc['file']}{':' + str(loc['line']) + ':' + str(loc['column']) if loc.get('line') else ''}#{loc['pointer']}"
    if inferred and loc.get("inferred"):
        text += " (inferred)"
    return text


def _text_report(report: dict[str, Any], explain: bool) -> list[str]:
    summary = report["summary"]
    lines = [f"{report['status']}: {summary['errors']} error(s), {summary['warnings']} warning(s)", str(report["scope"]["hostAssumption"])]
    for d in report["diagnostics"]:
        locations = " -> ".join(_location(loc) for loc in d["locations"])
        lines.append(f"{d['severity']} [{d['code']}] {locations}: {d['message']}")
    lines.append(
        f"{summary['unevaluatedFields']} field(s) outside wiring scope; use --explain for wiring details or --json for the graph."
    )
    if explain:
        lines.append("Wiring explanations: only reached checks are listed; input errors can stop later checks.")
        for e in report["explanations"]:
            service = f" (service {e['service']})" if e.get("service") else ""
            lines.append(f"{e['status']} [{e['relation']}] {_location(e['location'], True)}{service}")
            lines.append(f"  base: {e['base'] if e['base'] is not None else '(not applicable or unavailable)'}")
            lines.append(f"  target: {e['target'] if e['target'] is not None else '(unresolved)'}")
            if e.get("targetLocation"):
                lines.append(f"  target declaration: {_location(e['targetLocation'], True)}")
            for reason in e["reasons"]:
                lines.append(f"  reason [{reason['code']}]: {reason['message']}")
        for field in report["unevaluatedFields"]:
            lines.append(f"not-evaluated {_location(field['location'], True)}: {field['reason']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        values = parse_args(args)
        if values.get("help"):
            _emit(sys.stdout, HELP)
            return 0
        if values.get("version"):
            _emit(sys.stdout, __version__)
            return 0
        if not values.get("config") or not values.get("workspace"):
            raise UsageError("--config and --workspace are required")
        if values["daemon"] not in ("local", "remote"):
            raise UsageError("--daemon must be local or remote")
        if values.get("json") and values.get("sarif"):
            raise UsageError("--json and --sarif are mutually exclusive")
        report = check(
            {
                "config": values["config"],
                "workspace": values["workspace"],
                "composeJson": values.get("compose-json"),
                "composeBase": values.get("compose-base"),
                "daemon": values["daemon"],
            }
        )
        if values.get("sarif"):
            _emit(sys.stdout, json_stringify(to_sarif(report), 2))
        elif values.get("json"):
            _emit(sys.stdout, json_stringify(report, 2))
        else:
            for line in _text_report(report, bool(values.get("explain"))):
                _emit(sys.stdout, line)
        return 1 if report["status"] == "error" or (values.get("strict") and report["status"] == "partial") else 0
    except Exception as error:  # every failure is a CLI/internal error (exit 2)
        message = getattr(error, "message", None) or str(error)
        _emit(sys.stderr, f"{PROG}: {message}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
