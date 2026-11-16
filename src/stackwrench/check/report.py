"""Text, Markdown, JSON and SARIF 2.1.0 renderings of one check run."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import PurePath, PurePosixPath
from urllib.parse import quote

from stackwrench import __version__

from .discover import KINDS, Discovery
from .model import ERROR, INFO, WARNING, Finding, at_or_above, counts
from .registry import AREAS, BY_NAME, Check

FORMAT = "stackwrench.check/v1"
REPO_URL = "https://github.com/tianhaogutum/stackwrench"
DOCS_URL = f"{REPO_URL}/blob/main/docs/check.md"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_LEVEL = {ERROR: "error", WARNING: "warning", INFO: "note"}
LABEL = {ERROR: "error", WARNING: "warn", INFO: "info"}
# GitHub rejects comments over 65,536 characters; keep well below it.
MARKDOWN_ROWS_PER_AREA = 40
MARKDOWN_CELL = 300

# Short rule titles for SARIF; unlisted rules fall back to their check summary.
RULE_TITLES = {
    "compose-env/env-file-not-interpolated": "Variable only in env_file is used for ${...} interpolation",
    "compose-env/unset-substituted-empty": "Unset variable is substituted as an empty string",
    "compose-env/inherited-unset": "Pass-through variable is not set",
    "compose-env/empty-overrides-value": "Empty string hides a value",
    "compose-env/required-unset": "Required variable is not set",
    "compose-merge/ports-merged": "Override adds ports instead of replacing them",
    "compose-merge/null-ignored": "null in an override removes nothing",
    "podman-audit/healthy-gate-hang": "service_healthy gate reported to hang on podman-compose",
    "podman-audit/start-interval": "healthcheck.start_interval differs on podman-compose",
    "podman-audit/healthy-gate-without-healthcheck": "Healthy gate on a service without a healthcheck",
    "podman-audit/missing-dependency": "depends_on names an undefined service",
    "podman-audit/dependency-cycle": "depends_on cycle",
    "context-budget/junk-included": "Build context includes a directory that should be ignored",
    "context-budget/sensitive-file": "Credential-like file in the build context",
    "context-budget/over-budget": "Build context over budget",
    "cache-replay/broad-copy-before-run": "Broad COPY before an expensive RUN",
    "devcontainer/missing-host-path": "Dev Container path does not exist",
    "devcontainer/missing-service": "Dev Container references an unknown Compose service",
    "pins/drift": "Version differs from the mise.toml pin",
    "kind-images/always-pull": "Image is pulled from a registry despite kind load",
    "kind-images/missing-local-image": "Image with policy Never is not loaded on a node",
    "just/missing-dependency": "Recipe depends on a missing recipe",
    "just/dependency-cycle": "Recipe dependency cycle",
    "task-audit/sources-match-nothing": "Task sources match no files",
    "task-audit/input-not-in-sources": "Task reads a value its sources do not cover",
    "task-audit/outputs-without-inputs": "Task generates outputs without declared inputs",
}


@dataclass
class Run:
    """Everything a renderer needs about one invocation."""

    repo: str
    discovery: Discovery
    checks: list[Check]
    findings: list[Finding]
    skipped: list[tuple[str, str]]
    fail_on: str | None  # "error", "warning" or None (never)

    @property
    def failed(self) -> bool:
        return at_or_above(self.findings, self.fail_on)

    @property
    def sources(self):
        return self.discovery.sources


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def summary_line(run: Run) -> str:
    total = counts(run.findings)
    return " · ".join([_plural(len(run.sources), "file"), _plural(total[ERROR], "error"),
                       _plural(total[WARNING], "warning"), f"{total[INFO]} info"])


def _area(finding: Finding) -> str:
    check = BY_NAME.get(finding.check)
    return check.area if check else AREAS[-1]


def _by_area(findings) -> dict[str, list[Finding]]:
    groups: dict[str, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(_area(finding), []).append(finding)
    return {area: groups[area] for area in AREAS if area in groups}


def mix(discovery: Discovery) -> str:
    kinds = Counter(source.kind for source in discovery.sources)
    return ", ".join(f"{kinds[kind]} {KINDS[kind] if kinds[kind] == 1 else KINDS[kind] + 's'}"
                     for kind in KINDS if kinds[kind])


def _skipped(run: Run) -> str:
    return "; ".join(f"{name} ({reason})" for name, reason in run.skipped)


# --------------------------------------------------------------------------- text

def _safe(value) -> str:
    """Untrusted labels must not move the cursor or recolour the terminal."""
    return "".join(f"\\x{ord(c):02x}" if ord(c) < 32 or ord(c) == 127 else c for c in str(value))


class _Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text


SEVERITY_COLOR = {ERROR: "1;31", WARNING: "33", INFO: "36"}


def render_text(run: Run, *, color: bool = False, verbose: bool = False) -> str:
    style = _Style(color)
    found = mix(run.discovery)
    lines = [style("stackwrench check", "1") + f"  {_safe(run.repo)}  "
             + style(f"({_plural(len(run.sources), 'file')}" + (f": {found}" if found else "") + ")", "2")]
    if run.discovery.truncated:
        lines.append(style("  (scan stopped early: the folder has more than 50,000 entries)", "2"))
    if not run.sources:
        lines += ["", "No dev-stack files found (Compose files, devcontainer.json, mise.toml, Dockerfiles, "
                      "justfile, Taskfile.yml, kind cluster configs).",
                  style("Point it at a repository root, e.g. `stackwrench check path/to/repo`.", "2")]
    labels = {source.display: source.label for source in run.sources}
    findings = run.findings
    width = min(max((len(f.rule_id) for f in findings), default=0), 38)
    for area, group in _by_area(findings).items():
        lines += ["", style(area, "1;4")]
        files: dict[str | None, list[Finding]] = {}
        for finding in group:
            files.setdefault(finding.file, []).append(finding)
        for file in sorted(files, key=lambda name: (name is None, name or "")):
            label = labels.get(file) if file is not None else None
            lines.append("  " + (style(_safe(file), "1") if file is not None else style("(whole repository)", "1"))
                         + (style(f"  {label}", "2") if label else ""))
            for finding in files[file]:
                severity = style(LABEL[finding.severity].ljust(5), SEVERITY_COLOR[finding.severity])
                subject = f"{_safe(finding.subject)}: " if finding.subject else ""
                where = (f"line {finding.line}" if finding.line is not None
                         else finding.pointer if verbose and finding.pointer else "")
                lines.append(f"    {severity}  {style(finding.rule_id.ljust(width), '2')}  {subject}"
                             f"{_safe(finding.message)}" + (style(f"  {_safe(where)}", "2") if where else ""))
                if verbose and finding.hint:
                    lines.append(" " * (13 + width) + style(f"-> {_safe(finding.hint)}", "2"))
    if run.skipped:
        lines += ["", style(f"Not applicable here: {_skipped(run)}", "2")]
    lines += ["", style(summary_line(run), "1")]
    if run.findings and not verbose:
        lines.append(style("-v adds a fix hint under each finding; nothing was run, built or written.", "2"))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- Markdown

def _cell(value) -> str:
    text = " ".join(str(value).split())
    if len(text) > MARKDOWN_CELL:
        text = text[:MARKDOWN_CELL - 1] + "…"
    # Escape what would end the cell, start HTML or turn into a mention or link.
    for char in ("\\", "|", "<", ">", "`", "*", "_", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text.replace("@", "@​")


def _code(value) -> str:
    text = " ".join(str(value).split()).replace("`", "'").replace("|", "\\|")
    return f"`{text}`"


MD_SEVERITY = {ERROR: "**error**", WARNING: "warning", INFO: "info"}


def render_markdown(run: Run) -> str:
    total = counts(run.findings)
    verdict = ("**Failed**" if run.failed else "**Passed**") + f" (fail on: {run.fail_on or 'never'})"
    lines = ["## stackwrench check", "",
             f"{verdict} · {total[ERROR]} errors · {total[WARNING]} warnings · {total[INFO]} info · "
             f"{_plural(len(run.sources), 'file')} checked", ""]
    found = mix(run.discovery)
    lines += [f"Files: {_cell(found)}" if found else "No dev-stack files found.", ""]
    for area, group in _by_area(run.findings).items():
        important = [f for f in group if f.severity != INFO]
        notes = [f for f in group if f.severity == INFO]
        area_counts = counts(group)
        lines += [f"### {area}", "",
                  f"{area_counts[ERROR]} errors · {area_counts[WARNING]} warnings · {area_counts[INFO]} info", ""]
        if important:
            lines += _md_table(important)
        if notes:
            lines += ["<details><summary>" + _plural(len(notes), "info note") + "</summary>", ""]
            lines += _md_table(notes)
            lines += ["</details>", ""]
    if run.skipped:
        lines += [f"Not applicable: {_cell(_skipped(run))}", ""]
    lines += [f"<sub>stackwrench {__version__} · read-only, nothing executed · [what each rule means]({DOCS_URL})</sub>",
              ""]
    return "\n".join(lines)


def _md_table(findings: list[Finding]) -> list[str]:
    rows = ["| Severity | Rule | File | Finding |", "| --- | --- | --- | --- |"]
    for finding in findings[:MARKDOWN_ROWS_PER_AREA]:
        where = finding.file or "(whole repository)"
        if finding.line is not None:
            where += f":{finding.line}"
        text = (f"{finding.subject}: " if finding.subject else "") + finding.message
        rows.append(f"| {MD_SEVERITY[finding.severity]} | {_code(finding.rule_id)} | {_code(where)} | {_cell(text)} |")
    if len(findings) > MARKDOWN_ROWS_PER_AREA:
        rows.append(f"| | | | … and {len(findings) - MARKDOWN_ROWS_PER_AREA} more (see `--format json`) |")
    return rows + [""]


# --------------------------------------------------------------------------- JSON

def render_json(run: Run) -> str:
    per_file = Counter(finding.file for finding in run.findings)
    document = {
        "format": FORMAT,
        "tool": {"name": "stackwrench", "version": __version__},
        "repo": run.repo,
        "checks": [check.name for check in run.checks],
        "skipped": [{"check": name, "reason": reason} for name, reason in run.skipped],
        "files": [{"path": source.display, "kind": source.kind, "label": source.label,
                   "findings": per_file[source.display]} for source in run.sources],
        "findings": [{**finding.to_dict(), "area": _area(finding)} for finding in run.findings],
        "summary": {"files": len(run.sources), "scanned": run.discovery.scanned, **counts(run.findings)},
        "failOn": run.fail_on or "never",
        "failed": run.failed,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- SARIF

def _uri(path: str) -> dict:
    pure = PurePath(path)
    if pure.is_absolute():
        return {"uri": "file://" + quote(pure.as_posix(), safe="/")}
    return {"uri": quote(PurePosixPath(*pure.parts).as_posix(), safe="/"), "uriBaseId": "%SRCROOT%"}


def _rule(rule_id: str, finding: Finding) -> dict:
    check = BY_NAME.get(finding.check)
    title = RULE_TITLES.get(rule_id) or (check.summary if check else rule_id)
    rule = {"id": rule_id, "name": rule_id.replace("/", "-"), "shortDescription": {"text": title},
            "defaultConfiguration": {"level": SARIF_LEVEL[finding.severity]},
            "helpUri": DOCS_URL,
            "properties": {"check": finding.check, "area": _area(finding)}}
    if check:
        rule["fullDescription"] = {"text": check.summary}
    return rule


def _result(finding: Finding, rule_index: int) -> dict:
    text = (f"{finding.subject}: " if finding.subject else "") + finding.message
    location: dict = {"physicalLocation": {"artifactLocation": _uri(finding.file),
                                           "region": {"startLine": finding.line or 1}}}
    if finding.pointer:
        location["logicalLocations"] = [{"fullyQualifiedName": finding.pointer, "kind": "member"}]
    result = {"ruleId": finding.rule_id, "ruleIndex": rule_index, "level": SARIF_LEVEL[finding.severity],
              "message": {"text": text}, "locations": [location],
              "properties": {"check": finding.check, "severity": finding.severity}}
    if finding.hint:
        result["properties"]["hint"] = finding.hint
    return result


def render_sarif(run: Run) -> str:
    rules: list[dict] = []
    index: dict[str, int] = {}
    results, notifications = [], []
    for finding in run.findings:
        if finding.file is None:
            # Run-level notices have no file; SARIF consumers expect results to have one.
            notifications.append({"level": SARIF_LEVEL[finding.severity],
                                  "message": {"text": f"{finding.rule_id}: {finding.message}"}})
            continue
        if finding.rule_id not in index:
            index[finding.rule_id] = len(rules)
            rules.append(_rule(finding.rule_id, finding))
        results.append(_result(finding, index[finding.rule_id]))
    crashed = any(finding.rule == "internal" for finding in run.findings)
    document = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "stackwrench", "version": __version__, "semanticVersion": __version__,
                                "informationUri": REPO_URL, "rules": rules}},
            "results": results,
            "invocations": [{"executionSuccessful": not crashed, "toolExecutionNotifications": notifications}],
            "columnKind": "unicodeCodePoints",
        }],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


RENDERERS = {"json": render_json, "sarif": render_sarif, "markdown": render_markdown}
