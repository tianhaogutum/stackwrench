"""Docker build checks: context-budget and cache-replay.

Both work on the build contexts found by discovery (Compose `build:` entries
and Dockerfiles). The context is listed once with `stackwrench context-budget`'s
`analyze` (which applies .dockerignore) and shared with cache-replay, whose
model then replays a one-file edit. Nothing is built and no daemon is called.
"""
from __future__ import annotations

import re

from .common import Context, isolated, not_analysed, plural, read_text, short, size
from .model import INFO, WARNING, Finding

BUDGET = "context-budget"
CACHE = "cache-replay"

# Directories that are almost never meant to be sent to the builder.
JUNK_DIRS = (".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".tox", ".nox", ".next", ".nuxt", ".terraform", ".gradle", "coverage")
SENSITIVE_NAMES = frozenset({".env", ".netrc", ".git-credentials", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa",
                             "id_ed25519"})
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
ENV_TEMPLATES = (".example", ".sample", ".template", ".dist", ".defaults")
MAX_SENSITIVE = 10


def is_sensitive(path: str) -> bool:
    name = path.rpartition("/")[2]
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(".env.") and not name.endswith(ENV_TEMPLATES):
        return True
    return name.endswith(SENSITIVE_SUFFIXES)


def context_analysis(ctx: Context, build):
    """("ok", report) or ("error", reason) for one build context; computed once per run."""
    key = ("context-budget", str(build.context), str(build.dockerfile))
    if key not in ctx.memo:
        from stackwrench.context_budget.core import analyze
        from stackwrench.context_budget.ignore import InputError

        dockerfile = build.dockerfile.relative_to(build.context).as_posix()
        try:
            ctx.memo[key] = ("ok", analyze(str(build.context), dockerfile=dockerfile,
                                           max_bytes=ctx.options.context_budget, largest=5, top=5))
        except (InputError, OSError, UnicodeError) as exc:
            ctx.memo[key] = ("error", ctx.scrub(exc))
    return ctx.memo[key]


# --------------------------------------------------------------------------- context-budget

def context_budget(ctx: Context):
    findings: list[Finding] = []
    for build in ctx.discovery.build_contexts:
        findings.extend(isolated(BUDGET, ctx.display(build.dockerfile), lambda build=build: _budget(ctx, build)))
    return findings


def _rules_name(ctx: Context, report: dict) -> str | None:
    source = report["baseline"]["rules_source"]
    return ctx.display(source) if source else None


def _budget(ctx: Context, build):
    from stackwrench.context_budget.ignore import InputError, decide, parse_rules

    file = ctx.display(build.dockerfile)
    where = ctx.display(build.context)
    status, report = context_analysis(ctx, build)
    if status == "error":
        return [not_analysed(BUDGET, file, f"build context {where} cannot be listed: {report}")]
    baseline = report["baseline"]
    rules = _rules_name(ctx, report)
    included = [row for row in baseline["entries"] if row["kind"] == "file" and row["included_by_rules"]]
    findings: list[Finding] = []

    groups: dict[str, list[dict]] = {}
    for row in included:
        parts = row["path"].split("/")
        for index, part in enumerate(parts[:-1]):
            if part in JUNK_DIRS:
                groups.setdefault("/".join(parts[:index + 1]), []).append(row)
                break
    for prefix, rows in sorted(groups.items()):
        total = sum(row["logical_bytes"] for row in rows)
        reincluded = [row["last_rule"] for row in rows if row["last_rule"]]
        if rules is None:
            why = "there is no .dockerignore"
        elif reincluded:
            rule = reincluded[0]
            why = f"{rules} line {rule.get('line')} re-includes it"
        else:
            why = f"{rules} has no rule for it"
        pattern = prefix if "/" not in prefix else "**/" + prefix.rpartition("/")[2]
        hint = f"add `{pattern}` to .dockerignore"
        try:
            excluded = [row for row in rows if not decide(row["path"], parse_rules(pattern, "<suggestion>"))[0]]
            if excluded:
                hint += f" (verified with the context-budget matcher: excludes {plural(len(excluded), 'file')})"
        except (InputError, UnicodeError):
            pass
        findings.append(Finding(
            BUDGET, "junk-included", WARNING,
            f"{prefix}/ is sent with build context {where} ({plural(len(rows), 'file')}, {size(total)}): {why}",
            file, subject=None, hint=hint))

    sensitive = [row for row in included if is_sensitive(row["path"])]
    for row in sensitive[:MAX_SENSITIVE]:
        why = "there is no .dockerignore" if rules is None else f"{rules} does not exclude it"
        findings.append(Finding(
            BUDGET, "sensitive-file", WARNING,
            f"{row['path']} is sent with build context {where} ({why}); a `COPY . .` would bake it into an image layer",
            file, hint=f"add `{row['path']}` to .dockerignore; pass secrets with `RUN --mount=type=secret` instead"))
    if len(sensitive) > MAX_SENSITIVE:
        findings.append(Finding(BUDGET, "sensitive-file", WARNING,
                                f"{len(sensitive) - MAX_SENSITIVE} more credential-like files are in build context {where}",
                                file))

    summary = baseline["summary"]
    budget = baseline["budget"]
    if budget.get("total_passed") is False:
        advice = [f"`{row['append_pattern']}` ({size(-row['logical_payload_bytes_delta'])})"
                  for row in baseline.get("ignore_suggestions", [])[:3]]
        findings.append(Finding(
            BUDGET, "over-budget", WARNING,
            f"build context {where} is {size(summary['included_payload_bytes'])}, over the "
            f"{size(budget['max_bytes'])} budget",
            file, hint=("largest savings if appended to .dockerignore: " + ", ".join(advice)) if advice else None))
    largest = baseline.get("largest_included_files") or []
    biggest = f"; largest: {largest[0]['path']} ({size(largest[0]['logical_bytes'])})" if largest else ""
    findings.append(Finding(
        BUDGET, "size", INFO,
        f"build context {where}: {plural(summary['included_payload_files'], 'file')}, "
        f"{size(summary['included_payload_bytes'])} ({f'rules: {rules}' if rules else 'no .dockerignore'}){biggest}",
        file, hint=f"`stackwrench context-budget {where} --largest 20` lists what is sent and why"))
    return findings


# --------------------------------------------------------------------------- cache-replay

# Instructions that do not take part in the linear COPY model. They are blanked
# (line numbers stay the same) before the Dockerfile is handed to the model.
METADATA = frozenset({"CMD", "ENTRYPOINT", "EXPOSE", "LABEL", "USER", "HEALTHCHECK", "STOPSIGNAL", "VOLUME",
                      "SHELL", "MAINTAINER", "ONBUILD"})
DIRECTIVE = re.compile(r"#\s*(?:syntax|check)\s*=", re.IGNORECASE)
INSTALL = re.compile(
    r"\b(?:npm\s+(?:ci|install|i)\b|yarn(?:\s+install)?\s*(?:$|&&|;)|pnpm\s+(?:install|i)\b|pip3?\s+install|"
    r"python3?\s+-m\s+pip\s+install|poetry\s+install|uv\s+(?:sync|pip\s+install)|pipenv\s+install|"
    r"bundle\s+install|go\s+mod\s+download|cargo\s+fetch|composer\s+install|apt-get\s+install|apk\s+add)")
CODE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".go", ".rb", ".rs", ".java", ".kt",
                 ".php", ".cs", ".ex", ".exs", ".swift", ".scala")


def model_text(text: str) -> str:
    """The Dockerfile without metadata-only instructions and parser directives."""
    lines = []
    skipping = False
    for raw in text.split("\n"):
        stripped = raw.strip()
        if skipping:
            skipping = stripped.endswith("\\")
            lines.append("")
            continue
        word = stripped.split(None, 1)[0].upper() if stripped else ""
        if word in METADATA:
            skipping = stripped.endswith("\\")
            lines.append("")
        elif DIRECTIVE.match(stripped):
            lines.append("")
        else:
            lines.append(raw)
    return "\n".join(lines)


def cache_replay(ctx: Context):
    from stackwrench.cache_replay import model

    findings: list[Finding] = []
    for build in ctx.discovery.build_contexts:
        findings.extend(isolated(CACHE, ctx.display(build.dockerfile),
                                 lambda build=build: _replay(ctx, build, model)))
    return findings


def _broad(instruction, inventory) -> bool:
    return any(source == "." or source in inventory.directories or any(c in source for c in "*?")
               for source in instruction.sources)


def _example(copied: list[str], dockerfile: str) -> str:
    plain = [path for path in copied if path != dockerfile
             and not any(part.startswith(".") or part in JUNK_DIRS for part in path.split("/"))]
    for path in plain:
        if path.endswith(CODE_SUFFIXES):
            return path
    return (plain or [path for path in copied if path != dockerfile] or copied)[0]


def _replay(ctx: Context, build, model):
    file = ctx.display(build.dockerfile)
    text = read_text(build.dockerfile)
    if text is None:
        return [not_analysed(CACHE, file, "cannot read the Dockerfile")]
    parse = getattr(model, "_parse_dockerfile", None)
    try:
        if parse is not None:
            instructions = parse(build.dockerfile, model_text(text))
        else:
            instructions = model.parse_dockerfile(build.dockerfile)
    except model.InputError as exc:
        return [not_analysed(CACHE, file, f"outside the cache-replay model ({ctx.scrub(exc)})",
                             "the model covers single-stage FROM/WORKDIR/COPY/RUN/ENV/ARG Dockerfiles; "
                             "`stackwrench cache-replay --help` describes it")]
    status, report = context_analysis(ctx, build)
    if status == "error":
        return [not_analysed(CACHE, file, f"build context {ctx.display(build.context)} cannot be listed: {report}")]
    files = [row["path"] for row in report["baseline"]["entries"] if row["kind"] == "file" and row["included_by_rules"]]
    try:
        inventory = model.inventory_from_files(files)
    except model.InputError as exc:
        return [not_analysed(CACHE, file, ctx.scrub(exc))]
    dockerfile = build.dockerfile.relative_to(build.context).as_posix()
    weights = {instruction.id: 1 if instruction.op == "RUN" else 0 for instruction in instructions}
    for index, instruction in enumerate(instructions):
        if instruction.op != "COPY" or not _broad(instruction, inventory):
            continue
        if not any(later.op == "RUN" for later in instructions[index + 1:]):
            continue
        try:
            selected = model.copy_files(instruction, inventory)
        except model.InputError:
            continue
        copied = sorted({path for paths in selected.values() for path in paths})
        if len(copied) < 2:
            continue
        example = _example(copied, dockerfile)
        scenario = {"id": "edit-one-file", "changes": [{"path": example, "kind": "content"}]}
        try:
            result = model.replay(instructions, inventory, scenario, weights)
        except model.InputError as exc:
            return [not_analysed(CACHE, file, ctx.scrub(exc))]
        rerun = [step for step in instructions if step.id in result["invalidatedRunIds"]]
        if not rerun:
            continue
        installs = [step for step in rerun if INSTALL.search(step.argument)]
        first = (installs or rerun)[0]
        more = len(rerun) - 1
        message = (f"COPY {short(instruction.argument, 40)} copies {plural(len(copied), 'file')} before "
                   f"RUN {short(first.argument, 40)} (line {first.line}): editing any one of them, such as "
                   f"{example}, re-runs it" + (f" and {plural(more, 'more RUN step')}" if more else ""))
        manifests = [path for path in copied if path.rpartition("/")[2] in (
            "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "requirements.txt", "pyproject.toml",
            "poetry.lock", "uv.lock", "Pipfile.lock", "go.mod", "go.sum", "Gemfile.lock", "composer.lock",
            "Cargo.lock")]
        first_copy = f"COPY {' '.join(manifests[:2])} ./" if manifests else "COPY the dependency manifest"
        return [Finding(
            CACHE, "broad-copy-before-run", WARNING if installs else INFO, message, file, line=instruction.line,
            hint=f"{first_copy}, RUN the install, then COPY the rest; compare both orders with "
                 "`stackwrench cache-replay --before Dockerfile --after Dockerfile.new --changes changes.json "
                 "--context DIR` (model units, not build seconds)")]
    return []

