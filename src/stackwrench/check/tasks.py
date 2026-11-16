"""Task runner checks: just (justfile) and task-audit (Taskfile v3)."""
from __future__ import annotations

import re

from .common import Context, isolated, names, not_analysed, plural, read_text
from .model import ERROR, INFO, WARNING, Finding

JUST = "just"
TASK = "task-audit"

JUST_RULES = {
    # just itself refuses to run any recipe of a justfile with these errors.
    "missing-dependency": ERROR,
    "dependency-cycle": ERROR,
    # just refuses these unless `set allow-duplicate-...` is used.
    "duplicate-recipe": WARNING,
    "duplicate-variable": WARNING,
}
MISSING_DEPENDENCY = re.compile(r"depends on missing recipe '([^']+)'")


# --------------------------------------------------------------------------- just

def just(ctx: Context):
    from stackwrench.just_bridge.core import analyze

    taskfile_dirs = {source.path.parent for source in ctx.discovery.of("taskfile")}
    findings: list[Finding] = []
    for source in ctx.discovery.of("justfile"):
        findings.extend(isolated(JUST, source.display,
                                 lambda source=source: _just_one(ctx, source, analyze, taskfile_dirs)))
    return findings


def _just_one(ctx: Context, source, analyze, taskfile_dirs):
    text = read_text(source.path)
    if text is None:
        return [not_analysed(JUST, source.display, "cannot read the file")]
    analysis = analyze(text, source.display)
    findings: list[Finding] = []
    outside = []
    for diagnostic in analysis.diagnostics:
        if diagnostic.severity != "error":
            continue  # translation notes (echo format, runner environment) are not repository problems
        code = diagnostic.code
        if code == "missing-dependency":
            match = MISSING_DEPENDENCY.search(diagnostic.message)
            # A recipe the bridge could not parse (parameters, attributes, `@quiet`) is not missing.
            if match and re.search(rf"(?m)^@?{re.escape(match[1])}\b", text):
                outside.append(diagnostic)
                continue
        if code in JUST_RULES:
            consequence = (": just refuses to run any recipe in this file" if JUST_RULES[code] == ERROR else "")
            findings.append(Finding(JUST, code, JUST_RULES[code], diagnostic.message + consequence,
                                    source.display, line=diagnostic.line))
        else:
            outside.append(diagnostic)
    if outside:
        first = outside[0]
        findings.append(Finding(
            JUST, "outside-subset", INFO,
            f"{plural(len(outside), 'line')} use just features the just→Task bridge does not translate "
            f"(first: line {first.line}, {first.code})", source.display, line=first.line,
            hint="`stackwrench just plan justfile` lists every one; the file itself may be fine for just"))
    elif analysis.supported and source.path.parent in taskfile_dirs:
        findings.append(Finding(
            JUST, "convertible", INFO,
            f"this folder has both a justfile and a Taskfile; all {plural(len(analysis.recipes), 'recipe')} "
            "translate to Taskfile v3", source.display,
            hint=f"`stackwrench just convert {source.display}` prints the Taskfile with a report of every known loss"))
    return findings


# --------------------------------------------------------------------------- task-audit

# Shell variables a command can read without that being a build input worth tracking.
AMBIENT = frozenset({"HOME", "PATH", "PWD", "USER", "SHELL", "TMPDIR", "LANG", "TERM", "OLDPWD"})
NO_MATCH = "source pattern matched no regular files"
TASK_HINT = "`stackwrench task-audit check Taskfile.yml --json` shows inputs, sources and fingerprints per task"


def task_audit(ctx: Context):
    from stackwrench.task_audit.audit import audit
    from stackwrench.task_audit.formats import AuditError, load_taskfile

    findings: list[Finding] = []
    for source in ctx.discovery.of("taskfile"):
        findings.extend(isolated(TASK, source.display,
                                 lambda source=source: _task_one(ctx, source, audit, AuditError, load_taskfile)))
    return findings


def _reference(reference: str) -> str:
    syntax, _, name = reference.partition(":")
    return f"{{{{.{name}}}}}" if syntax == "template" else f"${name}"


def _task_one(ctx: Context, source, audit, AuditError, load_taskfile):
    path, display = source.path, source.display
    try:
        data = load_taskfile(path)
    except (AuditError, RecursionError) as exc:
        return [not_analysed(TASK, display, ctx.scrub(exc) if isinstance(exc, AuditError) else "nesting too deep")]
    tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    findings: list[Finding] = []
    results: dict[str, dict] = {}
    global_findings: list[dict] = []
    try:
        report = audit(path, path.parent)
        results, global_findings = report["tasks"], report["findings"]
    except (AuditError, RecursionError) as whole:
        # One bad task (a sources glob that matches nothing) fails the whole audit:
        # audit task by task to say which one, and keep the others.
        failures: dict[str, str] = {}
        for name in tasks:
            try:
                single = audit(path, path.parent, None, [name])
            except (AuditError, RecursionError) as exc:
                failures[name] = str(exc) if isinstance(exc, AuditError) else "nesting too deep"
                continue
            results.update(single["tasks"])
            global_findings = single["findings"]
        if not results and len(set(failures.values())) <= 1 and NO_MATCH not in failures.values():
            return [not_analysed(TASK, display, ctx.scrub(whole), TASK_HINT)]
        for name, message in failures.items():
            line = ctx.yaml_line(path, ["tasks", name])
            if message == NO_MATCH:
                findings.append(Finding(
                    TASK, "sources-match-nothing", WARNING,
                    "a `sources` pattern matches no files (moved or misspelled?), so there is nothing to fingerprint",
                    display, line=line, subject=name,
                    hint="fix the glob so it matches the files the task reads"))
            else:
                findings.append(Finding(TASK, "not-analysed", INFO, f"not analysed: {ctx.scrub(message)}",
                                        display, line=line, subject=name, hint=TASK_HINT))
    partial = 0
    for name, record in sorted(results.items()):
        task = tasks.get(name)
        task = task if isinstance(task, dict) else {}
        line = ctx.yaml_line(path, ["tasks", name])
        codes = {item["code"] for item in record["findings"]}
        if any(item["severity"] == "unassessed" for item in record["findings"]):
            partial += 1
        if "outputs-without-inputs" in codes:
            outputs = task.get("generates") or []
            findings.append(Finding(
                TASK, "outputs-without-inputs", WARNING,
                f"generates {names(str(item) for item in outputs)} but declares no sources or status: Task runs it "
                "every time and cannot tell when the output is stale",
                display, line=line, subject=name, hint="declare the inputs under `sources:` (or a `status:` check)"))
        elif "no-modeled-source-inputs" in codes:
            findings.append(Finding(
                TASK, "no-source-inputs", WARNING,
                "generates outputs, but no literal `sources` describe what they are built from",
                display, line=line, subject=name, hint="list the input files under `sources:`"))
        if task.get("sources"):
            references = [item["reference"] for item in record["findings"]
                          if item["code"] in ("potential-fingerprint-gap", "undefined-input") and "reference" in item
                          and not (item["reference"].startswith("shell:")
                                   and item["reference"].partition(":")[2] in AMBIENT)]
            references = list(dict.fromkeys(references))
            if references:
                shown = names(_reference(reference) for reference in references)
                findings.append(Finding(
                    TASK, "input-not-in-sources", WARNING,
                    f"runs only when its sources change, but its commands also use {shown}, which no source "
                    "covers: check that changing it re-runs the task",
                    display, line=line, subject=name,
                    hint="Task's up-to-date check follows `sources` (or `status`); add a status check or a file "
                         "that carries the value to `sources`"))
    unassessed_root = [item for item in global_findings if item.get("severity") == "unassessed"]
    if partial or unassessed_root:
        fields = names(sorted({item.get("field", item.get("name", "")) for item in unassessed_root} - {""}))
        detail = f" (top-level: {fields})" if fields else ""
        findings.append(Finding(
            TASK, "partial", INFO,
            f"{plural(partial, 'task')} use Taskfile features outside the audited subset{detail}; those parts "
            "were not assessed", display, hint=TASK_HINT))
    return findings
