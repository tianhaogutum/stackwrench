"""SARIF 2.1.0 conversion of a :func:`check` report."""

from __future__ import annotations

from typing import Any

from stackwrench import __version__

from .jsutil import js_resolve
from .paths import inside

SCHEMA = "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
TOOL_NAME = "devcontainer-wirecheck"

# WHATWG URL path percent-encode set (C0 controls, > U+007E and these).
_PATH_ENCODE = set(' "#<>?`{}')


def path_to_file_url(path: str) -> str:
    """``url.pathToFileURL(path).href`` on POSIX."""
    resolved = js_resolve(path)
    if path.endswith("/") and not resolved.endswith("/"):
        resolved += "/"
    resolved = (
        resolved.replace("%", "%25").replace("\\", "%5C").replace("\n", "%0A").replace("\r", "%0D").replace("\t", "%09")
    )
    out: list[str] = []
    for ch in resolved:
        code = ord(ch)
        if code < 0x20 or code > 0x7E or ch in _PATH_ENCODE:
            if 0xD800 <= code <= 0xDFFF:
                ch = "�"
            out.append("".join(f"%{byte:02X}" for byte in ch.encode("utf-8")))
        else:
            out.append(ch)
    return "file://" + "".join(out)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def to_sarif(report: dict[str, Any]) -> dict[str, Any]:
    """Convert a report without rerunning checks, reading files or consulting env."""
    root = report["scope"]["workspaceRoot"]
    root_uri = path_to_file_url(root if root.endswith("/") else root + "/") if root else None

    def location(source: dict[str, Any]) -> dict[str, Any]:
        uri = path_to_file_url(source["file"])
        if root and root_uri is not None and inside(root, source["file"]):
            artifact = {"uri": uri[len(root_uri) :], "uriBaseId": "%SRCROOT%"}
        else:
            artifact = {"uri": uri}
        physical: dict[str, Any] = {"artifactLocation": artifact}
        # Inferred fields point at a parent, not the absent token. Host observations
        # and unread documents have no coordinates. Never invent a region for them.
        if not source.get("inferred") and _is_positive_int(source.get("line")):
            physical["region"] = {"startLine": source["line"]}
            if _is_positive_int(source.get("column")):
                physical["region"]["startColumn"] = source["column"]
        properties: dict[str, Any] = {"jsonPointer": source["pointer"]}
        if source.get("inferred"):
            properties["inferred"] = True
        return {"physicalLocation": physical, "properties": properties}

    # IDs are the existing diagnostic codes, independent of paths or encounter order.
    ids = sorted({d["code"] for d in report["diagnostics"]})
    indices = {rule_id: index for index, rule_id in enumerate(ids)}
    results = []
    for d in report["diagnostics"]:
        primary, related = (d["locations"][0], d["locations"][1:]) if d["locations"] else (None, [])
        result: dict[str, Any] = {
            "ruleId": d["code"],
            "ruleIndex": indices[d["code"]],
            "kind": "fail",
            "level": "note" if d["severity"] == "info" else d["severity"],
            "message": {"text": d["message"]},
        }
        if primary:
            result["locations"] = [location(primary)]
        if related:
            result["relatedLocations"] = [
                {**location(source), "id": i + 1, "message": {"text": f"Related wiring location: {source['pointer']}"}}
                for i, source in enumerate(related)
            ]
        results.append(result)
    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": TOOL_NAME,
                "version": __version__,
                "rules": [{"id": rule_id, "shortDescription": {"text": rule_id.replace("-", " ")}} for rule_id in ids],
            }
        },
        "columnKind": "utf16CodeUnits",
    }
    if root_uri:
        run["originalUriBaseIds"] = {"%SRCROOT%": {"uri": root_uri}}
    run["results"] = results
    run["properties"] = {
        "status": report["status"],
        "summary": report["summary"],
        "hostAssumption": report["scope"]["hostAssumption"],
    }
    return {"$schema": SCHEMA, "version": "2.1.0", "runs": [run]}


toSarif = to_sarif
