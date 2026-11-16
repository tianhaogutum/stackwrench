"""Dev environment checks: devcontainer, pins (mise) and kind-images."""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

from .common import Context, isolated, names, not_analysed, plural
from .model import ERROR, INFO, WARNING, Finding

DEVCONTAINER = "devcontainer"
PINS = "pins"
KIND = "kind-images"


# --------------------------------------------------------------------------- devcontainer

# Diagnostics that say "not evaluated" or "unknown" rather than "broken".
DEV_INFO_CODES = frozenset({"implicit-workspace-unknown", "workspace-content-unknown", "run-args-unknown",
                            "build-options-unknown", "volume-runtime-unknown", "remote-bind-unknown",
                            "unsupported-platform"})
DEV_HINTS = {
    "missing-host-path": "paths in devcontainer.json are relative to the folder that contains it; "
                         "${localWorkspaceFolder} is the repository root",
    "wrong-host-kind": "build contexts and bind sources must be folders, Dockerfiles must be files",
    "missing-service": "`service` and `runServices` must name services of the referenced Compose file",
    "missing-volume": "declare the named volume under the Compose file's top-level `volumes:`",
    "path-escape": "the path leaves the repository; check the number of `../`",
}


def devcontainer_api():
    """The Python port's equivalent of devcontainer-wirecheck's `check(options)`.

    Looked up by name so that a different spelling in the port is a one-line fix here.
    """
    module = importlib.import_module("stackwrench.devcontainer")
    for name in ("check", "check_config", "run_check", "wirecheck"):
        function = getattr(module, name, None)
        if callable(function):
            return function
    for submodule in ("index", "checker", "core", "check"):
        try:
            candidate = importlib.import_module(f"stackwrench.devcontainer.{submodule}")
        except ModuleNotFoundError as exc:
            if exc.name != f"stackwrench.devcontainer.{submodule}":
                raise
            continue
        function = getattr(candidate, "check", None)
        if callable(function):
            return function
    raise ModuleNotFoundError("stackwrench.devcontainer has no check() entry point",
                              name="stackwrench.devcontainer.check")


def run_devcontainer_check(config: Path, workspace: Path) -> dict:
    function = devcontainer_api()
    options = {"config": str(config), "workspace": str(workspace), "daemon": "local"}
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "config" in parameters and "workspace" in parameters:
        report = function(**{key: value for key, value in options.items() if key in parameters})
    else:
        report = function(options)  # mirrors the JavaScript check(options)
    for method in ("to_dict", "as_dict"):
        if not isinstance(report, dict) and callable(getattr(report, method, None)):
            report = getattr(report, method)()
    return report


def devcontainer(ctx: Context):
    devcontainer_api()  # a missing port is reported once, not once per file
    findings: list[Finding] = []
    for source in ctx.discovery.of("devcontainer"):
        findings.extend(isolated(DEVCONTAINER, source.display, lambda source=source: _devcontainer_one(ctx, source)))
    return findings


def _devcontainer_one(ctx: Context, source):
    try:
        report = run_devcontainer_check(source.path, ctx.root)
    except (TypeError, ValueError) as exc:
        return [not_analysed(DEVCONTAINER, source.display, ctx.scrub(exc))]
    findings = []
    for item in report.get("diagnostics", []):
        code = str(item.get("code", "diagnostic"))
        severity = str(item.get("severity", "error"))
        if code in DEV_INFO_CODES or code.startswith("unsupported-") or severity not in (ERROR, WARNING, INFO):
            severity = INFO
        locations = item.get("locations") or []
        located = next((loc for loc in locations if loc.get("line")), locations[0] if locations else {})
        file = ctx.display(located["file"]) if located.get("file") else source.display
        pointer = located.get("pointer")
        message = ctx.scrub(item.get("message", code))
        if code.startswith("unsupported-"):
            message = f"not evaluated: {message}"
        findings.append(Finding(DEVCONTAINER, code, severity, message, file, line=located.get("line"),
                                pointer=pointer if pointer not in (None, "/") else None,
                                hint=DEV_HINTS.get(code)))
    return findings


# --------------------------------------------------------------------------- pins

PIN_TOOLS = ("node", "python", "golang")
TOOL_NAME = re.compile(r"\[tools\]\.([^:]+):")
PINS_HINT = "`stackwrench pins plan --root .` shows the edits as a diff; `stackwrench pins apply --root .` writes them"


def pins(ctx: Context):
    from stackwrench.pins.core import inspect as inspect_pins
    from stackwrench.pins.fs import InputError, Root

    mise = ctx.discovery.of("mise")
    if not mise:
        versions = ctx.discovery.of("tool-versions")
        if versions:
            return [Finding(PINS, "tool-versions-not-read", INFO,
                            ".tool-versions is not compared: pins reconciles exact versions from mise.toml only",
                            versions[0].display,
                            hint="mise reads .tool-versions too; a [tools] table in mise.toml makes the pins comparable")]
        return []
    return isolated(PINS, mise[0].display, lambda: _pins(ctx, mise[0], inspect_pins, InputError, Root))


def _pins(ctx: Context, mise, inspect_pins, InputError, Root):
    dockerfiles = [source.display for source in ctx.discovery.of("dockerfile")]
    version_files = [source.display for source in ctx.discovery.of("version-file")]
    workflows = [source.display for source in ctx.discovery.of("workflow")]
    root = None
    try:
        root = Root(str(ctx.root))
        report = inspect_pins(root, mise.display, dockerfiles, version_files, workflows)
    except (InputError, OSError) as exc:
        return [not_analysed(PINS, mise.display, ctx.scrub(exc))]
    finally:
        if root is not None:
            root.close()
    findings: list[Finding] = []
    compared = matched = drifted = 0
    for item in report.declarations:
        if item["kind"] == "mise" or item["status"] not in ("match", "drift"):
            continue
        compared += 1
        if item["status"] == "match":
            matched += 1
            continue
        drifted += 1
        if item["kind"] == "docker-image":
            what = f"`{item['declaration']}` uses {item['tool']} {item['actual']}"
        elif item["kind"] == "version-file":
            what = f"{item['declaration']} says {item['actual']}"
        else:
            what = f"{item['declaration']} uses {item['tool']} {item['actual']}"
        source_line = f":{item['sourceLine']}" if item.get("sourceLine") else ""
        findings.append(Finding(
            PINS, "drift", WARNING, f"{what}; {item['sourcePath']}{source_line} pins {item['expected']}",
            item["path"], line=item["line"], subject=item["tool"], hint=PINS_HINT))

    blocked = {(item["path"], item["line"]): item for item in report.declarations if item["status"] == "blocked"}
    unsupported_tools = []
    for item in report.findings:
        code, path, line = item["code"], item["path"], item.get("line")
        declaration = blocked.get((path, line), {})
        if code == "unsupported_tool":
            match = TOOL_NAME.search(item["message"])
            unsupported_tools.append(match[1] if match else "?")
        elif code == "unsupported_image":
            image = str(declaration.get("actual", ""))
            repository = re.split(r"[:@]", image, maxsplit=1)[0]
            if repository in PIN_TOOLS:
                pin = declaration.get("expected")
                versus = f" ({declaration.get('tool')} {pin} in {report.mise})" if pin else ""
                findings.append(Finding(PINS, "floating-tag", INFO,
                                        f"{image} has no exact major.minor.patch tag, so it cannot be compared "
                                        f"with mise.toml{versus}", path, line=line, subject=declaration.get("tool")))
            # Other images (postgres, nginx, ...) have no mise pin to compare: nothing to say.
        elif code == "missing_pin":
            findings.append(Finding(PINS, "missing-pin", INFO, item["message"], path, line=line,
                                    hint="add the tool to [tools] in mise.toml to keep both in step"))
        elif code in ("unsupported_pin", "unsupported_action_pin"):
            findings.append(Finding(PINS, "not-exact", INFO, f"{item['message']}; not compared", path, line=line))
        elif code in ("digest_pinned", "dynamic_image"):
            findings.append(Finding(PINS, "not-comparable", INFO, item["message"], path, line=line))
        else:
            findings.append(not_analysed(PINS, path, ctx.scrub(item["message"]), line=line))
    if unsupported_tools:
        findings.append(Finding(PINS, "tools-not-compared", INFO,
                                f"{names(unsupported_tools)} in {report.mise} not compared: pins reconciles "
                                "node, python and go", report.mise))
    if compared:
        findings.append(Finding(PINS, "summary", INFO,
                                f"{plural(compared, 'pin')} compared with {report.mise}: {matched} match, {drifted} drift",
                                report.mise))
    return findings


# --------------------------------------------------------------------------- kind-images

KIND_HINT = ("node availability needs per-node `crictl images -o json` snapshots from the running cluster; "
             "check never contacts a cluster: `stackwrench kind-images --manifests ... --inventory nodes.json`")
VERDICTS = {
    "missing-required-local": ("missing-local-image", ERROR,
                               "imagePullPolicy Never, but the image is not loaded on {nodes}"),
    "will-request-registry": ("always-pull", WARNING,
                              "imagePullPolicy {policy} pulls from a registry on {nodes} even when the image is "
                              "loaded; `kind load` does not change that"),
    "would-pull-if-scheduled": ("would-pull", WARNING,
                                "not loaded on {nodes}, so IfNotPresent would pull it from a registry there"),
}


def kind_images(ctx: Context):
    from stackwrench.kind_images.inputs import InputError
    from stackwrench.kind_images.manifests import read_manifests
    from stackwrench.kind_images.planner import ALWAYS_RISK, build_report

    configs = ctx.discovery.of("kind-config")
    inventories = ctx.discovery.of("kind-inventory")
    if not configs and not inventories:
        return []
    anchor = (configs or inventories)[0].display
    return isolated(KIND, anchor, lambda: _kind(ctx, configs, inventories, read_manifests, build_report,
                                                ALWAYS_RISK, InputError))


def _kind(ctx, configs, inventories, read_manifests, build_report, always_risk, InputError):
    findings: list[Finding] = []
    manifests = []
    containers = []
    for source in ctx.discovery.of("k8s-manifest"):
        try:
            found, _ = read_manifests([source.path])
        except InputError as exc:
            findings.append(not_analysed(KIND, source.display, ctx.scrub(exc)))
            continue
        manifests.append(source)
        containers.extend(found)
    anchor = (configs or inventories)[0].display
    if not manifests:
        findings.append(Finding(KIND, "needs-input", INFO,
                                "kind cluster found, but no rendered Kubernetes manifests to plan images for",
                                anchor, hint=KIND_HINT))
        return findings
    if not inventories:
        for container in containers:
            if container["effectivePullPolicy"] != "Always":
                continue
            source = "default for :latest" if container["policySource"] != "explicit" else "explicit"
            findings.append(Finding(
                KIND, "always-pull", WARNING,
                f"{container['container']}: {container['image']} has imagePullPolicy Always ({source}): {always_risk}",
                ctx.display(container["source"]), pointer=f"document {container['document']}, {container['path']}",
                subject=f"{container['kind']}/{container['name']}",
                hint="pin a tag other than :latest or set imagePullPolicy: IfNotPresent for images you `kind load`"))
        findings.append(Finding(KIND, "needs-snapshots", INFO,
                                f"{plural(len(containers), 'container')} in {plural(len(manifests), 'manifest')}; "
                                "per-node image availability was not assessed", anchor, hint=KIND_HINT))
        return findings
    for inventory in inventories:
        try:
            report = build_report([source.path for source in manifests], inventory.path)
        except InputError as exc:
            findings.append(not_analysed(KIND, inventory.display, ctx.scrub(exc)))
            continue
        plans = {item["image"]: item for item in report["loadPlan"]}
        for container in report["assessments"]:
            by_verdict: dict[str, list[str]] = {}
            for node in container["nodes"]:
                by_verdict.setdefault(node["verdict"], []).append(node["node"])
            for verdict, nodes in by_verdict.items():
                if verdict not in VERDICTS:
                    continue
                rule, severity, text = VERDICTS[verdict]
                plan = plans.get(container["canonicalImage"])
                hint = ("proposed, not run: " + " ".join(plan["argv"])) if plan and verdict != "will-request-registry" else None
                findings.append(Finding(
                    KIND, rule, severity,
                    f"{container['container']}: {container['image']} "
                    + text.format(nodes=", ".join(nodes), policy=container["effectivePullPolicy"]),
                    ctx.display(container["source"]), pointer=f"document {container['document']}, {container['path']}",
                    subject=f"{container['kind']}/{container['name']}", hint=hint))
        unassessed = report["unassessedNodes"]
        if unassessed:
            findings.append(Finding(KIND, "unassessed-nodes", INFO,
                                    f"no snapshot for {names(node['node'] for node in unassessed)}; their image "
                                    "availability is unknown", inventory.display))
        if report["unassessed"]:
            findings.append(Finding(KIND, "unassessed-objects", INFO,
                                    f"{plural(len(report['unassessed']), 'object')} outside the supported kinds "
                                    "(Pod, Deployment, StatefulSet, DaemonSet, Job, CronJob) were not assessed",
                                    inventory.display))
    return findings
