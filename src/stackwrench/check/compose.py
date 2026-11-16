"""Compose checks: compose-env, compose-merge and podman-audit.

Each one calls the Python API of the matching stackwrench command and turns
its report into findings. Nothing here starts Compose, Podman or a container.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

import yaml

from .common import Context, isolated, names, not_analysed, plural, read_text, short, split_location
from .model import INFO, WARNING, Finding

ENV = "compose-env"
MERGE = "compose-merge"
PODMAN = "podman-audit"

REQUIRED = re.compile(r"(?P<loc>.+?), offset [0-9]+: required variable (?P<name>[A-Za-z_][A-Za-z0-9_]*) "
                      r"\((?P<op>:?\?)\)")
LEADING_LOCATION = re.compile(r"(?P<loc>.+?:[0-9]+(?::[0-9]+)?)(?:, offset [0-9]+)?: (?P<rest>.*)\Z", re.S)
ENV_HINT = ("compose-env reads one Compose file at a time with a strict YAML subset (no anchors, aliases, "
            "extends or include); `stackwrench compose-env --compose FILE --env-file .env` shows the full trace")


# --------------------------------------------------------------------------- compose-env

def compose_env(ctx: Context):
    from stackwrench.compose_env.trace import TraceError, trace

    findings: list[Finding] = []
    for group in ctx.discovery.compose_groups:
        findings.extend(isolated(ENV, ctx.display(group.files[0]),
                                 lambda group=group: _env_group(ctx, group, trace, TraceError)))
    return findings


def _env_group(ctx: Context, group, trace, TraceError):
    env_files = [str(group.env_file)] if group.env_file else []
    findings: list[Finding] = []
    reports = []
    errors: set[str] = set()
    for path in group.files:
        try:
            report = trace(str(path), env_files)
        except TraceError as exc:
            finding = _trace_error(ctx, path, str(exc))
            if finding.message not in errors:  # a broken .env fails every file the same way
                errors.add(finding.message)
                findings.append(finding)
            continue
        except RecursionError:
            findings.append(not_analysed(ENV, ctx.display(path), "YAML nesting exceeds the parser depth"))
            continue
        reports.append(report)
        findings.extend(_env_findings(ctx, path, report))
    if group.env_file and reports:
        findings.extend(_dotenv_unused(ctx, group, reports))
    return findings


def _where(ctx: Context, location: str, fallback: Path) -> tuple[str, int | None]:
    file, line = split_location(location)
    return ctx.display(file or fallback), line


def _trace_error(ctx: Context, path: Path, message: str) -> Finding:
    required = REQUIRED.match(message)
    if required:
        file, line = _where(ctx, required["loc"], path)
        name = required["name"]
        return Finding(ENV, "required-unset", WARNING,
                       f"${{{name}{required['op']}…}} requires {name}, and .env does not set it: "
                       f"`docker compose` stops here unless the shell exports {name}",
                       file, line=line, subject=name,
                       hint=f"set {name} in .env, or give it a default with ${{{name}:-value}}; "
                            "check does not read your shell environment")
    leading = LEADING_LOCATION.fullmatch(message)
    file, line = (_where(ctx, leading["loc"], path) if leading else (ctx.display(path), None))
    reason = leading["rest"] if leading else message
    return not_analysed(ENV, file, ctx.scrub(reason), ENV_HINT, line=line)


def _env_file_names(path: Path) -> dict[str, list[str]]:
    """service -> literal env_file paths as written, to name them in messages."""
    try:
        data = yaml.safe_load(read_text(path) or "")
    except (yaml.YAMLError, RecursionError, ValueError):
        return {}
    services = data.get("services") if isinstance(data, dict) else None
    result: dict[str, list[str]] = {}
    for name, service in (services or {}).items() if isinstance(services, dict) else ():
        value = service.get("env_file") if isinstance(service, dict) else None
        items = value if isinstance(value, list) else [value] if value is not None else []
        result[str(name)] = [item.get("path") if isinstance(item, dict) else item for item in items]
    return result


ENV_FILE_INDEX = re.compile(r"->env_file\[([0-9]+)\]:([0-9]+)\Z")


def _env_file_label(declaration: dict, service: str, env_files: dict[str, list[str]]) -> str:
    match = ENV_FILE_INDEX.search(declaration.get("location", ""))
    if not match:
        return "env_file"
    index, line = int(match[1]), match[2]
    declared = env_files.get(service, [])
    name = declared[index] if index < len(declared) else None
    if not isinstance(name, str) or "$" in name:
        name = f"env_file[{index}]"
    return f"{name} line {line}"


def _env_findings(ctx: Context, path: Path, report: dict) -> list[Finding]:
    findings: list[Finding] = []
    services = report["services"]
    env_files = _env_file_names(path)
    uses = [use for use in report["interpolation"]["uses"]
            if use.get("phase") == "compose" and "variable" in use]
    # Variables that some service gets from an env_file: name -> (service, declaration).
    from_env_file: dict[str, tuple[str, dict]] = {}
    for service, details in services.items():
        for name, item in details["variables"].items():
            for declaration in [*item["shadowed"], item["winner"]]:
                if declaration["source"] == "service-env-file" and declaration["status"] == "set":
                    from_env_file.setdefault(name, (service, declaration))
                    break
    explained: set[str] = set()  # interpolation variables already reported
    covered: set[tuple[str, str]] = set()  # (service, variable) container values already reported

    # ${VAR} is unset for interpolation, but VAR sits in an env_file: the classic mix-up.
    for use in uses:
        name = use["variable"]
        if use.get("input_status") != "unset" or name in explained or name not in from_env_file:
            continue
        explained.add(name)
        service, declaration = from_env_file[name]
        label = _env_file_label(declaration, service, env_files)
        item = services[service]["variables"].get(name, {})
        winner = item.get("winner", {})
        hides = winner.get("source") == "environment" and winner.get("status") == "empty"
        if hides:
            covered.add((service, name))
            effect = f"so `environment: {name}` becomes \"\" and hides {name} from {label}"
        else:
            effect = f"{name} in {label} reaches the container but is never used for ${{...}}"
        file, line = _where(ctx, use["location"], path)
        findings.append(Finding(
            ENV, "env-file-not-interpolated", WARNING,
            f"${{{name}}} is not set for interpolation (.env does not define it), {effect}",
            file, line=line, subject=name,
            hint=f"env_file values go to the container only; put {name} in .env for ${{{name}}}, "
                 f"or drop the `environment:` entry and let {label.rsplit(' line ', 1)[0]} supply it"))

    warnings = report["warnings"]
    substituted: dict[str, list[dict]] = {}
    for warning in warnings:
        if warning["code"] == "unresolved-substituted-empty" and warning.get("variable") not in explained:
            substituted.setdefault(warning["variable"], []).append(warning)
    for name, items in substituted.items():
        file, line = _where(ctx, items[0]["location"], path)
        more = f" ({plural(len(items), 'use')})" if len(items) > 1 else ""
        findings.append(Finding(
            ENV, "unset-substituted-empty", WARNING,
            f"${{{name}}} is not set in .env, so Compose substitutes an empty string{more}",
            file, line=line, subject=name,
            hint=f"set {name} in .env, use ${{{name}:-default}}, or fail fast with ${{{name}:?message}}; "
                 "check does not read your shell environment"))

    for warning in warnings:
        if warning["code"] != "unresolved-inherited-removed":
            continue
        name = warning.get("variable", "?")
        syntax = "`- NAME`"
        for service, details in services.items():
            winner = details["variables"].get(name, {}).get("winner", {})
            if winner.get("location") == warning["location"]:
                syntax = f"`{name}:`" if winner.get("syntax") == "inherited-null" else f"`- {name}`"
                covered.add((service, name))
        file, line = _where(ctx, warning["location"], path)
        findings.append(Finding(
            ENV, "inherited-unset", WARNING,
            f"{syntax} passes {name} through from the environment, but .env does not set it: the container "
            f"gets no {name} unless the shell running Compose exports it",
            file, line=line, subject=name,
            hint=f"give it a value (`{name}: value`), set it in .env, or document that the shell must export it"))

    for service, details in services.items():
        for name, item in details["variables"].items():
            winner = item["winner"]
            if (service, name) in covered or winner["status"] != "empty":
                continue
            hidden = [entry for entry in item["shadowed"] if entry["status"] == "set"]
            if not hidden:
                continue
            loser = (_env_file_label(hidden[-1], service, env_files) if hidden[-1]["source"] == "service-env-file"
                     else "an earlier declaration")
            if winner["source"] == "environment":
                where = "`environment:`"
            else:
                where = _env_file_label(winner, service, env_files)
            file, line = _where(ctx, winner["location"], path)
            findings.append(Finding(
                ENV, "empty-overrides-value", WARNING,
                f"{where} sets {name} to an empty string (empty is not unset), which wins over {name} from {loser}",
                file, line=line, subject=name,
                hint=f"delete the empty entry to keep the value from {loser}; an explicit empty string always wins"))
    return findings


def _dotenv_unused(ctx: Context, group, reports: list[dict]) -> list[Finding]:
    used: set[str] = set()
    for report in reports:
        used.update(use["variable"] for use in report["interpolation"]["uses"]
                    if use.get("phase") == "compose" and "variable" in use)
        for details in report["services"].values():
            for name, item in details["variables"].items():
                if any(entry.get("syntax", "").startswith("inherited") for entry in [*item["shadowed"], item["winner"]]):
                    used.add(name)
    unused = []
    for name, item in reports[0]["interpolation"]["variables"].items():
        winner = item["winner"]
        if winner["source"] == "interpolation-env-file" and name not in used:
            unused.append((split_location(winner["location"])[1] or 0, name))
    if not unused:
        return []
    unused.sort()
    listed = [name for _, name in unused]
    files = ", ".join(path.name for path in group.files)
    verb = "is" if len(listed) == 1 else "are"
    return [Finding(
        ENV, "dotenv-unused", INFO,
        f"{names(listed)} {verb} set in .env but not used by {files}: .env feeds ${{...}} interpolation, "
        "it is not passed to containers",
        ctx.display(group.env_file), line=unused[0][0] or None,
        hint="to pass a value into a container, reference it (`NAME: ${NAME}`) or list the file under env_file")]


# --------------------------------------------------------------------------- compose-merge

COMMAND_PATH = re.compile(r"/services/[^/]+/(?:command|entrypoint|healthcheck/test)\Z")
RESOURCE_PATH = re.compile(r"/services/(?P<service>[^/]+)/(?P<kind>ports|volumes|secrets|configs)\Z")
MERGE_HINT = ("compose-merge reads a literal subset of Compose (no ${...} interpolation, build, depends_on, "
              "env_file, extends); `stackwrench compose-merge explain -f BASE -f OVERRIDE --path /services/...` "
              "traces one field")


def _pointer_label(pointer: str) -> str:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    return ".".join(parts) or "(root)"


def _value_label(value) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return json.dumps(value) if not isinstance(value, str) else value


def _port_label(port) -> str:
    if not isinstance(port, dict):
        return str(port)
    label = f"{port['published']}:{port['target']}" if port.get("published") else str(port.get("target"))
    if port.get("host_ip"):
        label = f"{port['host_ip']}:{label}"
    if port.get("protocol", "tcp") != "tcp":
        label += f"/{port['protocol']}"
    return label


def _mount_label(mount) -> str:
    if not isinstance(mount, dict):
        return str(mount)
    target = mount.get("target") or mount.get("source")
    return f"{mount['source']}:{target}" if mount.get("source") and mount.get("target") else str(target)


def compose_merge(ctx: Context):
    from stackwrench.compose_merge.core import MergeError, merge_files

    findings: list[Finding] = []
    for group in ctx.discovery.compose_groups:
        if len(group.default) < 2:
            continue
        findings.extend(isolated(MERGE, ctx.display(group.default[-1]),
                                 lambda group=group: _merge_group(ctx, group, merge_files, MergeError)))
    return findings


def _merge_group(ctx: Context, group, merge_files, MergeError):
    files = [str(path) for path in group.default]
    try:
        project = merge_files(files)
    except MergeError as exc:
        message = str(exc)
        leading = LEADING_LOCATION.fullmatch(message)
        file, line = (_where(ctx, leading["loc"], group.default[0]) if leading
                      else (ctx.display(group.default[-1]), None))
        reason = leading["rest"] if leading else message
        return [not_analysed(MERGE, file, ctx.scrub(reason), MERGE_HINT, line=line)]
    findings: list[Finding] = []
    tagged: dict[str, list[dict]] = {"reset": [], "override": []}
    for event in project.events:
        source = event["source"]
        if source.get("input", 0) < 1:
            continue  # only what the override files change
        path, action = event["path"], event["action"]
        file, line = ctx.display(source["file"]), source.get("line")
        previous = event.get("previousSources") or []
        base = (f"{ctx.display(previous[0]['file'])}:{previous[0]['line']}" if previous else "the base file")
        label = _pointer_label(path)
        if action == "retain":
            findings.append(Finding(
                MERGE, "null-ignored", WARNING,
                f"null in {file} does not remove the value from {base}; Compose keeps it",
                file, line=line, subject=label,
                hint="write `!reset null` to remove a key in an override file"))
        elif action == "replace" and COMMAND_PATH.fullmatch(path) and event["previousPresent"]:
            findings.append(Finding(
                MERGE, "command-replaced", INFO,
                f"the value from {base} is replaced, not appended: {short(_value_label(event['value']))}",
                file, line=line, subject=label))
        elif action == "merge" and RESOURCE_PATH.fullmatch(path) and event["previousPresent"]:
            kind = RESOURCE_PATH.fullmatch(path)["kind"]
            before, after = event.get("previous") or [], event.get("value") or []
            added = [item for item in after if item not in before]
            if not added or not before:
                continue
            render = _port_label if kind == "ports" else _mount_label
            kept = [render(item) for item in before]
            if kind == "ports":
                findings.append(Finding(
                    MERGE, "ports-merged", WARNING,
                    f"{file} adds {', '.join(render(item) for item in added)}, and "
                    f"{', '.join(kept)} from {base} stays published: Compose merges ports, it does not replace them",
                    file, line=line, subject=label,
                    hint="use `ports: !override [...]` in the override file to replace the list"))
            else:
                findings.append(Finding(
                    MERGE, "list-merged", INFO,
                    f"{file} adds {', '.join(render(item) for item in added)}; "
                    f"{', '.join(kept)} from {base} is kept ({kind} merge by target)",
                    file, line=line, subject=label))
        elif action in tagged:
            tagged[action].append(event)
    for action, events in tagged.items():
        paths = [event["path"] for event in events]
        for event in events:
            # Report only the outermost tagged path; its children carry the same action.
            if any(event["path"].startswith(other + "/") for other in paths if other != event["path"]):
                continue
            label = _pointer_label(event["path"])
            effect = "removes it" if action == "reset" else "replaces it instead of merging"
            findings.append(Finding(
                MERGE, action, INFO, f"!{action} in {ctx.display(event['source']['file'])} {effect}",
                ctx.display(event["source"]["file"]), line=event["source"].get("line"), subject=label))
    return findings


# --------------------------------------------------------------------------- podman-audit

PODMAN_RULES = {
    "PC1541": "healthy-gate-hang",
    "PC1500": "start-interval",
    "DEP001": "missing-dependency",
    "DEP002": "healthy-gate-without-healthcheck",
    "DEP003": "dependency-cycle",
    "CAT001": "unverified-version",
}
JSON_PATH_TOKEN = re.compile(r'\.([A-Za-z_][A-Za-z_0-9]*)|\[([0-9]+)\]|\[("(?:[^"\\]|\\.)*")\]')
PODMAN_HINT = ("podman-audit checks depends_on and healthcheck only; resolve ${...} in those fields "
               "(`docker compose config`) and run `stackwrench podman-audit FILE --provider podman-compose "
               "--provider-version X.Y.Z --explain`")


def json_path_parts(path: str) -> list:
    parts: list = []
    position = 1 if path.startswith("$") else 0
    while position < len(path):
        match = JSON_PATH_TOKEN.match(path, position)
        if not match:
            break
        if match[1] is not None:
            parts.append(match[1])
        elif match[2] is not None:
            parts.append(int(match[2]))
        else:
            parts.append(json.loads(match[3]))
        position = match.end()
    return parts


def podman_audit(ctx: Context):
    from stackwrench.podman_audit.audit import audit
    from stackwrench.podman_audit.input import InputError, StrictLoader, load_document

    version = ctx.options.podman_compose_version
    findings: list[Finding] = []
    for group in ctx.discovery.compose_groups:
        for path in group.standalone:
            findings.extend(isolated(PODMAN, ctx.display(path), lambda path=path: _podman_file(
                ctx, path, version, audit, InputError, StrictLoader, load_document)))
    return findings


def _podman_file(ctx: Context, path: Path, version: str, audit, InputError, StrictLoader, load_document):
    display = ctx.display(path)
    text = read_text(path)
    if text is None:
        return [not_analysed(PODMAN, display, "cannot read the file")]
    try:
        data = yaml.load(text, Loader=StrictLoader)
    except (yaml.YAMLError, InputError, RecursionError, ValueError):
        return [not_analysed(PODMAN, display, "the YAML cannot be read", PODMAN_HINT)]
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        return []
    # podman-audit evaluates depends_on and healthcheck only and refuses fields outside its
    # subset (build, env_file, ...), so it gets exactly those two fields of every service.
    projection = {"services": {str(name): {key: service[key] for key in ("depends_on", "healthcheck")
                                           if isinstance(service, dict) and key in service}
                               for name, service in services.items()}}
    if not any(projection["services"].values()):
        return []
    try:
        model = load_document(json.dumps(projection), format="json")
        report = audit(model, provider="podman-compose", provider_version=version)
    except InputError as exc:
        parts = json_path_parts(exc.detail.get("path", "$"))
        return [not_analysed(PODMAN, display, f"{exc} ({exc.detail.get('path', '$')})", PODMAN_HINT,
                             line=ctx.yaml_line(path, parts) if parts else None)]
    findings = []
    for item in report["findings"]:
        rule = PODMAN_RULES.get(item["ruleId"], item["ruleId"].lower())
        parts = json_path_parts(item["path"])
        message = _podman_message(item, parts, version)
        evidence = item.get("evidence", {}).get("evidenceUrl")
        hint = item.get("suggestion", "")
        if evidence:
            hint = f"{hint} Evidence: {evidence}".strip()
        findings.append(Finding(PODMAN, rule, item["severity"], message, display,
                                line=ctx.yaml_line(path, parts) if parts else None,
                                subject=item.get("service"), hint=hint or None))
    return findings


def _podman_message(item: dict, parts: list, version: str) -> str:
    details = item.get("details", {})
    service = item.get("service")
    target = details.get("target") or (parts[3] if len(parts) > 3 and parts[2] == "depends_on" else None)
    reported = details.get("versionMatch") == "exact"
    scope = (f"podman-compose {version} was reported to" if reported
             else f"podman-compose 1.6.0 was reported to (unverified for {version})")
    rule = item["ruleId"]
    if rule == "PC1541":
        return (f"waits for {target} with condition: service_healthy; {scope} hang a detached `up` "
                "on this gate even after it became healthy")
    if rule == "PC1500":
        return (f"healthcheck.start_interval: {scope} map it to Podman's separate startup healthcheck, "
                "with different timing")
    if rule == "DEP001":
        optional = "" if details.get("required", True) else " (required: false)"
        return f"depends on {target}, which this file does not define{optional}"
    if rule == "DEP002":
        if details.get("healthcheckState") == "disabled":
            return f"waits for {target} to be healthy, but {target} disables its healthcheck"
        return (f"waits for {target} to be healthy, but {target} declares no healthcheck "
                "(an image HEALTHCHECK may exist; images are not inspected)")
    if rule == "DEP003":
        cycle = details.get("cycleServices") or []
        return f"depends_on {target} is part of a cycle: {' -> '.join(cycle)}"
    if rule == "CAT001":
        return (f"podman-compose {version} is not in the bundled evidence catalog (observed: 1.6.0); "
                "compatibility is unverified")
    return item.get("reportedBehavior", rule)
