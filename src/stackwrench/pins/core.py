"""Exact-pin reports and inspection for selected runtime files and adapters."""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from dataclasses import dataclass, field

from .fs import InputError, Root, Snapshot, relative_path
from .locations import tool_lines

NUMBER = r"(?:0|[1-9][0-9]*)"
PIN = rf"{NUMBER}\.{NUMBER}\.{NUMBER}"
EXACT = re.compile(PIN + r"\Z", re.ASCII)
IMAGE = re.compile(rf"(?P<image>node|python|golang):(?P<pin>{PIN})(?P<suffix>-[A-Za-z0-9_][A-Za-z0-9_.-]*)?\Z", re.ASCII)
VERSION = re.compile(rf"[ \t]*(?P<prefix>v?)(?P<pin>{PIN})[ \t]*(?:\#[^\r\n]*)?(?:\r?\n)?\Z", re.ASCII)
FROM = re.compile(
    r"[ \t]*FROM[ \t]+(?:--platform=[^\s]+[ \t]+)?"
    r"(?P<image>[^\s#]+)(?:[ \t]+AS[ \t]+(?P<alias>[A-Za-z0-9][A-Za-z0-9_.-]*))?"
    r"(?:[ \t]+\#[^\r\n]*|[ \t]*)\Z", re.IGNORECASE | re.ASCII,
)
VERSION_TOOLS = {".nvmrc": "node", ".node-version": "node",
                 ".python-version": "python", ".go-version": "go"}
IMAGE_TOOLS = {"node": "node", "python": "python", "golang": "go"}


@dataclass
class Report:
    root: str
    mise: str
    declarations: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    updates: dict[str, bytes] = field(default_factory=dict)
    source_lines: dict[str, int] = field(default_factory=dict)

    def issue(self, path: str, code: str, message: str, line: int | None = None) -> None:
        finding = {"path": path, "code": code, "message": message}
        if line is not None:
            finding["line"] = line
        self.findings.append(finding)

    def error(self, exc: InputError) -> None:
        self.issue(exc.path, exc.code, str(exc))

    def declaration(self, *, path: str, line: int | None, tool: str | None,
                    kind: str, declaration: str, expected: str | None,
                    actual: str, evaluated: bool = True, reason: str | None = None) -> None:
        mismatch = evaluated and expected is not None and expected != actual
        if kind in ("docker-stage", "actions-other"):
            status = "skipped"
        elif not evaluated:
            status = "blocked"
        else:
            status = "drift" if mismatch else "match"
        if reason is None:
            reason = {"skipped": "named stage or scratch; no external runtime pin to compare",
                      "blocked": "no supported exact mise pin; manual review required",
                      "drift": "exact version differs from mise pin",
                      "match": "exact version matches mise pin"}[status]
        self.declarations.append({
            "path": path, "line": line, "tool": tool, "kind": kind,
            "declaration": declaration,
            "source": f"{self.mise}:[tools].{tool}" if tool else None,
            "expected": expected, "actual": actual,
            "mismatch": mismatch,
            "evaluated": evaluated,
            "status": status, "reason": reason,
            "sourcePath": self.mise if tool else None,
            "sourceLine": self.source_lines.get(tool),
            "oldValue": actual, "newValue": expected if evaluated else None,
        })

    @property
    def mismatch(self) -> bool:
        return any(item["mismatch"] for item in self.declarations)

    def as_dict(self) -> dict:
        return {"schemaVersion": 1, "root": self.root, "mise": self.mise,
                "status": "blocked" if self.findings else "drift" if self.mismatch else "clean",
                "mismatch": self.mismatch, "declarations": sorted(self.declarations, key=location_key),
                "findings": sorted(self.findings, key=location_key), "changedFiles": sorted(self.updates)}

    def plan_dict(self) -> dict:
        result = self.as_dict()
        paths = {s.path for s in self.snapshots} | {f["path"] for f in self.findings}
        diffs = self.file_diffs()
        result.update({
            "applicable": not self.findings,
            "changedFiles": [] if self.findings else sorted(self.updates),
            "diff": "".join(diffs.values()),
            "files": [{"path": path,
                       "diff": diffs.get(path, ""),
                       "declarations": [d for d in result["declarations"] if d["path"] == path],
                       "findings": [f for f in result["findings"] if f["path"] == path]}
                      for path in sorted(paths)],
        })
        return result

    def explanation(self) -> str:
        lines = []
        for file in self.plan_dict()["files"]:
            for item in file["declarations"]:
                source = (f" (source {item['sourcePath']}:{item['sourceLine'] or '?'} [tools].{item['tool']})"
                          if item["sourcePath"] else "")
                lines.append(f"{item['path']}:{item['line']}: {item['status']}: "
                             f"{json.dumps(item['oldValue'], ensure_ascii=True)} -> "
                             f"{json.dumps(item['newValue'], ensure_ascii=True)}{source}; {item['reason']}; "
                             f"declaration: {json.dumps(item['declaration'], ensure_ascii=True)}")
            for item in file["findings"]:
                location = item["path"] + (f":{item['line']}" if "line" in item else "")
                lines.append(f"{location}: {item['code']}: {item['message']}")
        lines.append("Batch blocked; no edits can be applied." if self.findings else
                     "Drift plan; no files written." if self.mismatch else "Clean plan; no files written.")
        return "\n".join(lines) + "\n"

    def diff(self) -> str:
        return "".join(self.file_diffs().values())

    def file_diffs(self) -> dict[str, str]:
        """Applicable fragments in path order; never expose partial blocked edits."""
        if self.findings:
            return {}
        diffs = {}
        for snapshot in sorted(self.snapshots, key=lambda item: item.path):
            if snapshot.path not in self.updates:
                continue
            chunks = []
            before = snapshot.data.decode("utf-8").splitlines(keepends=True)
            after = self.updates[snapshot.path].decode("utf-8").splitlines(keepends=True)
            # difflib omits the POSIX marker for missing final newlines.
            for line in difflib.unified_diff(before, after, fromfile=f"a/{snapshot.path}",
                                             tofile=f"b/{snapshot.path}", lineterm="\n"):
                if line.endswith("\n"):
                    chunks.append(line)
                else:
                    chunks.append(line + "\n\\ No newline at end of file\n")
            diffs[snapshot.path] = "".join(chunks)
        return diffs


def location_key(item: dict) -> tuple:
    return (item["path"], item.get("line") or 0, item.get("declaration", ""), item.get("code", ""))


def text_of(snapshot: Snapshot) -> str:
    try:
        text = snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(snapshot.path, f"invalid UTF-8 at byte {exc.start}") from exc
    if (any(ord(char) < 32 and char not in "\t\r\n" for char in text)
            or any(char in text for char in "\x7f\x85\u2028\u2029\ufeff")
            or "\r" in text.replace("\r\n", "")):
        raise InputError(snapshot.path, "control characters, BOM, Unicode line separators and bare CR are unsupported")
    return text


def read_pins(snapshot: Snapshot, report: Report) -> dict[str, str]:
    try:
        text = text_of(snapshot)
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InputError(snapshot.path, f"invalid TOML: {exc}") from exc
    except RecursionError as exc:
        raise InputError(snapshot.path, "TOML nesting is too deep to evaluate safely") from exc
    tools = config.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise InputError(snapshot.path, "a nonempty [tools] table is required")
    report.source_lines = tool_lines(text)
    pins = {}
    for tool, value in tools.items():
        line = report.source_lines.get(tool)
        reason = None
        if tool not in IMAGE_TOOLS.values():
            reason = f"[tools].{tool}: only node, python and go are supported"
            report.issue(snapshot.path, "unsupported_tool", reason, line)
        elif not isinstance(value, str) or not EXACT.fullmatch(value):
            reason = f"[tools].{tool}: expected an exact major.minor.patch string; aliases, ranges, objects and arrays are refused"
            report.issue(snapshot.path, "unsupported_pin", reason, line)
        else:
            pins[tool] = value
        report.declaration(path=snapshot.path, line=line, tool=tool, kind="mise",
                           declaration=f"[tools].{tool}", expected=pins.get(tool),
                           actual=value if isinstance(value, str) else str(value),
                           evaluated=reason is None, reason=reason)
    return pins


def expected_pin(tool: str, pins: dict[str, str], report: Report, path: str, line: int) -> str | None:
    if tool not in pins:
        report.issue(path, "missing_pin", f"no supported exact [tools].{tool} pin in {report.mise}", line)
    return pins.get(tool)


def scan_version(snapshot: Snapshot, pins: dict[str, str], report: Report) -> None:
    text = text_of(snapshot)
    name = snapshot.path.rsplit("/", 1)[-1]
    tool = VERSION_TOOLS.get(name)
    if tool is None:
        raise InputError(snapshot.path, "unsupported version filename; use .nvmrc, .node-version, .python-version or .go-version",
                         "unsupported_file")
    match = VERSION.fullmatch(text)
    expected = expected_pin(tool, pins, report, snapshot.path, 1)
    if match is None:
        reason = "expected one exact major.minor.patch line (optional v prefix, whitespace and trailing # comment)"
        report.issue(snapshot.path, "unsupported_pin", reason, 1)
        report.declaration(path=snapshot.path, line=1, tool=tool, kind="version-file",
                           declaration=name, expected=expected, actual=text.rstrip("\r\n"),
                           evaluated=False, reason=reason)
        return
    actual = match["pin"]
    report.declaration(path=snapshot.path, line=1, tool=tool, kind="version-file",
                       declaration=name, expected=expected, actual=actual, evaluated=expected is not None)
    if expected is not None and actual != expected:
        report.updates[snapshot.path] = (text[:match.start("pin")] + expected + text[match.end("pin"):]).encode("utf-8")


def scan_dockerfile(snapshot: Snapshot, pins: dict[str, str], report: Report) -> None:
    text = text_of(snapshot)
    lines = text.splitlines(keepends=True)
    output = list(lines)
    aliases: set[str] = set()
    continued = False
    seen_from = False
    for number, raw in enumerate(lines, 1):
        line = raw.removesuffix("\n").removesuffix("\r")
        stripped = line.lstrip(" \t")
        if stripped.startswith("#"):
            if re.match(r"#\s*escape\s*=", stripped, re.IGNORECASE):
                if not re.fullmatch(r"#\s*escape\s*=\s*\\\s*", stripped, re.IGNORECASE):
                    report.issue(snapshot.path, "unsupported_escape", "only the default backslash Dockerfile escape is supported", number)
            continue
        if not stripped:
            continue
        if "<<" in line:
            report.issue(snapshot.path, "unsupported_heredoc", "Dockerfile heredocs / '<<' are refused to avoid interpreting their bodies as instructions", number)
            # No further FROM-looking text is trusted after a possible heredoc.
            return
        was_continued = continued
        continued = line.rstrip(" \t").endswith("\\")
        if was_continued:
            continue
        if not re.match(r"FROM(?:[ \t]|$)", stripped, re.IGNORECASE):
            continue
        seen_from = True
        if continued:
            report.issue(snapshot.path, "unsupported_from", "continued FROM instructions are unsupported", number)
            continue
        match = FROM.fullmatch(line)
        if match is None:
            report.issue(snapshot.path, "unsupported_from", "expected FROM [--platform=VALUE] IMAGE [AS ALIAS] [# comment]", number)
            continue
        image = match["image"]
        alias = match["alias"]
        reason = code = None
        if "@" in image:
            code, reason = "digest_pinned", "digest-pinned images cannot be rewritten automatically"
        elif "$" in image:
            code, reason = "dynamic_image", "dynamic image references are unsupported; no ARG or environment expansion is performed"
        elif image.lower() in aliases or image == "scratch":
            report.declaration(path=snapshot.path, line=number, tool=None, kind="docker-stage",
                               declaration=line.strip(), expected=None, actual=image, evaluated=False)
        else:
            tag = IMAGE.fullmatch(image)
            if tag is None:
                code = "unsupported_image"
                reason = f"{image!r}: only unqualified node/python/golang with exact major.minor.patch[-suffix] tags are supported; no version is inferred"
            else:
                tool = IMAGE_TOOLS[tag["image"]]
                actual = tag["pin"]
                expected = expected_pin(tool, pins, report, snapshot.path, number)
                report.declaration(path=snapshot.path, line=number, tool=tool, kind="docker-image",
                                   declaration=line.strip(), expected=expected, actual=actual, evaluated=expected is not None)
                if expected is not None and actual != expected:
                    start = match.start("image") + tag.start("pin")
                    end = match.start("image") + tag.end("pin")
                    output[number - 1] = raw[:start] + expected + raw[end:]
        if reason is not None:
            # Only a literal supported repository name establishes the runtime.
            # Never resolve a floating tag, ARG, digest or registry-qualified name.
            tool = IMAGE_TOOLS.get(re.split(r"[:@]", image, maxsplit=1)[0])
            report.issue(snapshot.path, code, reason, number)
            report.declaration(path=snapshot.path, line=number, tool=tool, kind="docker-image",
                               declaration=line.strip(), expected=pins.get(tool), actual=image,
                               evaluated=False, reason=reason)
        if alias:
            if alias.lower() in aliases:
                report.issue(snapshot.path, "duplicate_stage", f"duplicate stage alias {alias!r}", number)
            aliases.add(alias.lower())
    if not seen_from:
        report.issue(snapshot.path, "missing_from", "no FROM instruction found")
    updated = "".join(output).encode("utf-8")
    if updated != snapshot.data:
        report.updates[snapshot.path] = updated


def inspect(root: Root, mise: str, dockerfiles: list[str] | None = None,
            version_files: list[str] | None = None, workflows: list[str] | None = None) -> Report:
    report = Report(root.path, mise)
    # Normalize the complete selection before reading any file.
    try:
        mise = relative_path(mise)
        report.mise = mise
        selection = [(mise, "mise", False)]
        selection += [(relative_path(path), "dockerfile", dockerfiles is None)
                      for path in (dockerfiles if dockerfiles is not None else ["Dockerfile"])]
        selection += [(relative_path(path), "version", version_files is None)
                      for path in (version_files if version_files is not None else VERSION_TOOLS)]
        selection += [(relative_path(path), "workflow", False) for path in (workflows or [])]
        paths = [path for path, _, _ in selection]
        if len(paths) != len(set(paths)):
            raise InputError("<selection>", "duplicate paths or conflicting file roles are refused", "duplicate_path")
    except InputError as exc:
        report.error(exc)
        return report
    pins = {}
    for path, kind, optional in selection:
        try:
            snapshot = root.read(path, optional=optional)
            if snapshot is None:
                continue
            report.snapshots.append(snapshot)
            if kind == "mise":
                pins = read_pins(snapshot, report)
            elif kind == "dockerfile":
                scan_dockerfile(snapshot, pins, report)
            elif kind == "workflow":
                from .actions import scan_workflow
                scan_workflow(snapshot, pins, report)
            else:
                scan_version(snapshot, pins, report)
        except InputError as exc:
            report.error(exc)
    return report
