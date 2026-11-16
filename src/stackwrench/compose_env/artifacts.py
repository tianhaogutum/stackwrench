"""Strict, bounded ingestion of two saved single syntax-discovery reports."""

import json
import math
from pathlib import Path

from .checklist import compare_discovery_checklists, discovery_checklist
from .trace import MAX_DEPTH, TraceError, read_text


_ERROR = "artifact comparison requires two valid v1 single syntax-only discovery JSON files"


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _finite(number):
    value = float(number)
    if not math.isfinite(value):
        raise ValueError
    return value


def _constant(number):
    raise ValueError


def _check_depth(text: str) -> None:
    # Bound containers before json.loads, including unknown fields. JSON decoding
    # still checks grammar; escaped quotes/backslashes cannot change this count.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError
        elif char in "]}":
            depth -= 1


def _single(path: str | Path) -> dict:
    if not isinstance(path, (str, Path)):
        raise ValueError
    text = read_text(Path(path), "discovery artifact")
    _check_depth(text)
    report = json.loads(text, object_pairs_hook=_pairs, parse_float=_finite, parse_constant=_constant)
    if not isinstance(report, dict) or report.get("report_type") != "env-template-discovery":
        raise ValueError
    # Validate each side before pairing. Unknown fields are never projected.
    discovery_checklist(report)
    return report


def compare_discovery_artifacts(baseline_path: str | Path, candidate_path: str | Path) -> dict:
    """Compare two explicit local v1 single-discovery JSON artifacts, without rescanning.

    Each regular UTF-8 file is limited to 2 MiB and 64 JSON container levels;
    duplicate keys and nonfinite numbers fail even in ignored fields. Only the
    two supplied paths are read/statted. Recorded source labels are opaque metadata.
    Returns the existing named checklist-comparison schema with fixed baseline /
    candidate labels and independent scopes. No values, operands or messages are
    copied; requirements are syntax, never satisfaction or equivalent coverage.
    No files are written. Any read/JSON/schema failure raises a fixed TraceError.
    """
    try:
        baseline = _single(baseline_path)
        candidate = _single(candidate_path)
        return compare_discovery_checklists({
            "schema_version": 1, "report_type": "scenario-template-discovery",
            "evaluation": "syntax-only", "values_shown": False,
            "host_environment": "not-read", "implicit_dotenv": "not-read",
            "complete": baseline["complete"] and candidate["complete"],
            "scenarios": {"baseline": baseline, "candidate": candidate}})
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, RecursionError):
        raise TraceError(_ERROR) from None
