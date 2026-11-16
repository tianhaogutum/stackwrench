"""Independent structural checks and exact-version evidence lookup."""

import hashlib
import json
import re
from importlib.resources import files

from . import __version__
from .input import InputError, field_location, json_path

SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
DURATION = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?(?:us|ms|s|m|h))+")
OPAQUE_SERVICE = frozenset({
    "image", "command", "entrypoint", "environment", "labels", "ports", "expose",
    "volumes", "networks", "restart", "container_name", "user", "working_dir",
    "read_only", "init", "tty", "stdin_open", "stop_signal", "stop_grace_period",
    "hostname", "domainname", "shm_size", "secrets", "configs",
})
OPAQUE_TOP = frozenset({"name", "version", "networks", "volumes", "secrets", "configs"})
HEALTH_KEYS = frozenset({"test", "disable", "interval", "timeout", "start_period", "start_interval", "retries"})


def _catalog_bytes():
    return files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()


def catalog():
    return json.loads(_catalog_bytes())


def validate_target(provider, version):
    if provider != "podman-compose":
        raise InputError("unknown-provider", "Unsupported provider; compatibility is unverified. Only provider podman-compose is supported.")
    if not isinstance(version, str) or len(version) > 128 or not SEMVER.fullmatch(version):
        raise InputError("invalid-version", "Provider version must be exact SemVer, for example 1.6.0 (no v prefix or range).")


def _error(message, parts, code="invalid-input"):
    raise InputError(code, message, json_path(parts))


def _mapping(value, parts):
    if not isinstance(value, dict):
        _error("Expected a mapping.", parts)


def _healthcheck(value, parts):
    _mapping(value, parts)
    for key, child in value.items():
        path = (*parts, key)
        if key not in HEALTH_KEYS:
            _error("Unsupported healthcheck field.", path, "unsupported")
        if key == "disable" and type(child) is not bool:
            _error("healthcheck.disable must be a boolean.", path)
        if key == "retries" and (type(child) is not int or child < 0):
            _error("healthcheck.retries must be a nonnegative integer.", path)
        if key in {"interval", "timeout", "start_period", "start_interval"}:
            if not isinstance(child, str) or not (child == "0" or DURATION.fullmatch(child)):
                _error("Duration must be a nonnegative unit string (us, ms, s, m, h) or '0'.", path)
        if key == "test":
            if isinstance(child, str):
                if not child.strip():
                    _error("healthcheck.test must not be empty.", path)
            elif isinstance(child, list):
                if (not child or not all(isinstance(x, str) for x in child)
                        or child[0] not in {"NONE", "CMD", "CMD-SHELL"}):
                    _error("healthcheck.test must start with NONE, CMD or CMD-SHELL.", path)
                if child[0] == "NONE" and len(child) != 1:
                    _error("NONE must be the entire healthcheck test.", path)
                if child[0] == "CMD" and (len(child) < 2 or not child[1]):
                    _error("CMD requires a command.", path)
                if child[0] == "CMD-SHELL" and (len(child) != 2 or not child[1].strip()):
                    _error("CMD-SHELL requires one command string.", path)
            else:
                _error("healthcheck.test must be a string or list of strings.", path)


def _dependencies(value, parts):
    result = []
    if isinstance(value, list):
        seen = set()
        for index, name in enumerate(value):
            if not isinstance(name, str) or not NAME.fullmatch(name):
                _error("Dependency name must be a service name string.", (*parts, index))
            if name in seen:
                _error("Duplicate short dependency.", (*parts, index))
            seen.add(name)
            result.append({"target": name, "condition": "service_started", "required": True,
                           "parts": (*parts, index)})
    elif isinstance(value, dict):
        for name, options in value.items():
            path = (*parts, name)
            if not NAME.fullmatch(name):
                _error("Invalid dependency service name.", path)
            _mapping(options, path)
            for key in options:
                if key not in {"condition", "required", "restart"}:
                    _error("Unsupported dependency option.", (*path, key), "unsupported")
            condition = options.get("condition")
            if condition not in ("service_started", "service_healthy", "service_completed_successfully"):
                _error("Long dependency requires a supported condition.", (*path, "condition"))
            for key in ("required", "restart"):
                if key in options and type(options[key]) is not bool:
                    _error("Dependency required/restart must be a boolean.", (*path, key))
            result.append({"target": name, "condition": condition,
                           "required": options.get("required", True), "parts": path})
    else:
        _error("depends_on must be a list or mapping.", parts)
    return result


def _validate(model):
    _mapping(model, ())
    not_evaluated = []

    def opaque(parts):
        not_evaluated.append({"path": json_path(parts), "reason": "Not checked for schema or provider compatibility."})

    for key in model:
        if key == "services":
            continue
        if key in OPAQUE_TOP or key.startswith("x-"):
            opaque((key,))
        else:
            _error("Unsupported top-level Compose directive.", (key,), "unsupported")
    services = model.get("services")
    _mapping(services, ("services",))
    if not services:
        _error("At least one service is required.", ("services",))
    if len(services) > 500:
        _error("At most 500 services are supported.", ("services",), "input-limit")
    dependencies = {}
    for name, service in services.items():
        parts = ("services", name)
        if not NAME.fullmatch(name):
            _error("Invalid service name.", parts)
        _mapping(service, parts)
        dependencies[name] = []
        for key, value in service.items():
            path = (*parts, key)
            if key == "depends_on":
                dependencies[name] = _dependencies(value, path)
            elif key == "healthcheck":
                _healthcheck(value, path)
            elif key in OPAQUE_SERVICE or key.startswith("x-"):
                opaque(path)
            else:
                _error("Unsupported service directive; supply a model within the documented subset.", path, "unsupported")
    if sum(map(len, dependencies.values())) > 10_000:
        _error("At most 10,000 dependency edges are supported.", ("services",), "input-limit")
    return services, dependencies, sorted(not_evaluated, key=lambda x: x["path"])


def _dependency_graph(services, dependencies):
    """Declared edges between present services, including optional dependencies."""
    return {name: {edge["target"] for edge in edges if edge["target"] in services}
            for name, edges in dependencies.items()}


def _components(graph):
    """Iterative Kosaraju SCC: bounded stack, no cycle enumeration explosion."""
    seen, order = set(), []
    for root in sorted(graph):
        if root in seen:
            continue
        stack = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in sorted(graph[node], reverse=True) if child not in seen)
    reverse = {node: set() for node in graph}
    for node, children in graph.items():
        for child in children:
            reverse[child].add(node)
    membership, groups = {}, []
    for root in reversed(order):
        if root in membership:
            continue
        index = len(groups)
        group, stack = [], [root]
        while stack:
            node = stack.pop()
            if node in membership:
                continue
            membership[node] = index
            group.append(node)
            stack.extend(reverse[node] - membership.keys())
        groups.append(sorted(group))
    return membership, groups


def _explanation(finding, parts, provider, version, data, digest, model):
    """Explain checked fields, without copying commands or other source values."""
    rule_id = finding["ruleId"]
    rule = data["rules"][rule_id]
    field_paths = [finding["path"]]
    if rule_id == "PC1541":
        field_paths = [json_path((*parts, "condition"))]
        options = model["services"][finding["service"]]["depends_on"][parts[-1]]
        if "required" in options:
            field_paths.append(json_path((*parts, "required")))
            requirement = ("required is true, declaring a required dependency" if options["required"] else
                           "required is false, declaring an optional dependency")
        else:
            requirement = "required defaults to true, declaring a required dependency"
        reason = ("The long dependency condition is service_healthy; this selects the healthy-gate provider report. "
                  + "Here, " + requirement + ". This describes declarations, not runtime enforcement or a health result.")
    elif rule_id == "PC1500":
        reason = "start_interval is declared; this selects the startup-cadence provider report."
        health = model["services"][finding["service"]]["healthcheck"]
        if finding["details"]["healthcheckState"] == "disabled":
            causes = []
            if health.get("disable") is True:
                field_paths.append(json_path((*parts[:-1], "disable")))
                causes.append("disable is true")
            if health.get("test") == ["NONE"]:
                field_paths.append(json_path((*parts[:-1], "test")))
                causes.append("test is [NONE]")
            reason += (" The check is explicitly disabled: " + "; ".join(causes)
                       + ". This reviews an option declaration, not a running probe.")
        if "start_period" in health:
            field_paths.append(json_path((*parts[:-1], "start_period")))
            reason += (" start_period is declared; this is static declaration context, "
                       "not execution, timing, health or provider enforcement.")
    elif rule_id == "DEP001":
        if isinstance(parts[-1], int):
            requirement = "the short-form dependency is required by default"
        elif "required" in model["services"][finding["service"]]["depends_on"][parts[-1]]:
            field_paths.append(json_path((*parts, "required")))
            requirement = "required is true" if finding["details"]["required"] else "required is false"
        else:
            requirement = "required defaults to true"
        if isinstance(parts[-1], int):
            condition = "the short-form condition defaults to service_started"
        else:
            options = model["services"][finding["service"]]["depends_on"][parts[-1]]
            field_paths.append(json_path((*parts, "condition")))
            condition = "the declared condition is " + options["condition"]
        reason = ("The dependency names a target absent from this document's services map; "
                  + requirement + ", so its severity is " + finding["severity"]
                  + ". Here, " + condition + ". This describes the declared document, "
                  + "not a runtime result or evidence of ordering, completion, health, recovery or provider enforcement.")
    elif rule_id == "DEP002":
        field_paths = [json_path((*parts, "condition")), finding["details"]["targetPath"]]
        target = model["services"][finding["details"]["target"]]
        if finding["details"]["healthcheckState"] == "disabled":
            health = target["healthcheck"]
            causes = []
            # Both declarations can disable a check independently. Keep a stable
            # order and do not expose the contents of an unrelated test command.
            if health.get("disable") is True:
                field_paths.append(finding["details"]["targetPath"] + ".disable")
                causes.append("disable is true")
            if health.get("test") == ["NONE"]:
                field_paths.append(finding["details"]["targetPath"] + ".test")
                causes.append("test is [NONE]")
            reason = ("The service_healthy target has an explicitly disabled healthcheck: "
                      + "; ".join(causes) + ". This describes declarations, not a runtime health result.")
        else:
            if "healthcheck" in target:
                # The block exists but its test does not. Make that absence explicit
                # rather than suggesting the block proves a declared probe exists.
                field_paths.append(finding["details"]["targetPath"] + ".test")
            reason = "The service_healthy target has no declared test; an inherited image healthcheck remains unknown."
        options = model["services"][finding["service"]]["depends_on"][parts[-1]]
        if "required" in options:
            field_paths.append(json_path((*parts, "required")))
            requirement = ("required is true, declaring a required dependency" if options["required"] else
                           "required is false, declaring an optional dependency")
        else:
            requirement = "required defaults to true, declaring a required dependency"
        reason += (" Here, " + requirement
                   + ". This describes declarations, not runtime enforcement or a health result.")
    elif rule_id == "DEP003":
        if isinstance(parts[-1], int):
            requirement = "the short-form dependency is required by default"
        elif "required" in model["services"][finding["service"]]["depends_on"][parts[-1]]:
            field_paths.append(json_path((*parts, "required")))
            requirement = "required is true" if finding["details"]["required"] else "required is false"
        else:
            requirement = "required defaults to true"
        if isinstance(parts[-1], int):
            condition = "the short-form condition defaults to service_started"
        else:
            options = model["services"][finding["service"]]["depends_on"][parts[-1]]
            field_paths.append(json_path((*parts, "condition")))
            condition = "the declared condition is " + options["condition"]
        reason = ("The declared dependency edge is inside a strongly connected component; "
                  + requirement + "; optional edges also count. "
                  + "Here, " + condition + ". All declared edge conditions count. "
                  + "This describes the declared graph, not a runtime result or evidence of "
                  + "ordering, completion, health, recovery or provider enforcement.")
    else:  # CAT001 is target-wide, with no triggering Compose field.
        field_paths = []
        reason = "The selected provider version has no exact observation in the local catalog; compatibility is unverified."
    provider_scoped = rule["kind"] != "structural"
    return {
        "fieldPaths": field_paths,
        "fieldLocations": [field_location(model, path) for path in field_paths],
        "reason": reason,
        "target": {"provider": provider, "version": version},
        "evidenceScope": {
            "kind": rule["kind"],
            "provider": data["provider"] if provider_scoped else None,
            "observedVersions": rule["observedVersions"],
            "versionMatch": ("exact" if version in rule["observedVersions"] else "unknown")
                            if provider_scoped else "not-applicable",
        },
        "localEvidence": {
            "kind": "curated-catalog", "package": "stackwrench.podman_audit", "resource": "catalog.json",
            "jsonPointer": "/rules/" + rule_id, "sha256": digest,
        },
        "adviceScope": {"runtimeVerificationRequired": True, "guaranteesRuntimeSuccess": False},
    }


def audit(model, *, provider, provider_version, explain=False):
    """Audit a parsed model. Input must have been loaded by load_document/read_document."""
    validate_target(provider, provider_version)
    services, dependencies, not_evaluated = _validate(model)
    raw_catalog = _catalog_bytes()
    data = json.loads(raw_catalog)
    digest = hashlib.sha256(raw_catalog).hexdigest() if explain else None
    findings = []

    def emit(rule_id, service, parts, category, severity, confidence, **details):
        rule = data["rules"][rule_id]
        finding = {"ruleId": rule_id, "service": service, "path": json_path(parts),
                         "category": category, "severity": severity, "confidence": confidence,
                         "reportedBehavior": rule["reportedBehavior"], "suggestion": rule["suggestion"],
                         "evidence": {k: rule[k] for k in ("evidenceUrl", "observedVersions", "statusAtRetrieval", "observationContext", "uncertainty")},
                         "details": details}
        if explain:
            finding["explanation"] = _explanation(finding, parts, provider, provider_version, data, digest, model)
        findings.append(finding)

    def provider_finding(rule_id, service, parts, **details):
        matched = provider_version in data["rules"][rule_id]["observedVersions"]
        emit(rule_id, service, parts,
             "reported-risk" if matched else "unverified-compatibility", "warning",
             "reported" if matched else "unverified", versionMatch="exact" if matched else "unknown", **details)

    known = sorted({v for rule in data["rules"].values() if rule["kind"] == "provider-report"
                    for v in rule["observedVersions"]})
    if provider_version not in known:
        emit("CAT001", None, (), "unverified-compatibility", "warning", "unverified",
             requestedVersion=provider_version)
    graph = _dependency_graph(services, dependencies)
    membership, groups = _components(graph)
    for name in sorted(services):
        health = services[name].get("healthcheck", {})
        if "start_interval" in health:
            # Even a disabled check is a declared option worth flagging; record it.
            disabled = health.get("disable") is True or health.get("test") == ["NONE"]
            provider_finding("PC1500", name, ("services", name, "healthcheck", "start_interval"),
                             healthcheckState="disabled" if disabled else "declared-option",
                             startPeriodDeclared="start_period" in health)
        for edge in dependencies[name]:
            target, path = edge["target"], edge["parts"]
            if edge["condition"] == "service_healthy":
                provider_finding("PC1541", name, path)
            if target not in services:
                emit("DEP001", name, path, "structural", "error" if edge["required"] else "warning",
                     "high", target=target, required=edge["required"])
                continue
            if edge["condition"] == "service_healthy":
                target_health = services[target].get("healthcheck", {})
                disabled = target_health.get("disable") is True or target_health.get("test") == ["NONE"]
                if disabled or "test" not in target_health:
                    emit("DEP002", name, path, "structural", "error" if disabled else "warning",
                         "high" if disabled else "unverified", target=target,
                         healthcheckState="disabled" if disabled else "image-healthcheck-unverified",
                         targetPath=json_path(("services", target, "healthcheck")))
            if membership[name] == membership[target]:
                group = groups[membership[name]]
                if len(group) > 1 or name == target:
                    emit("DEP003", name, path, "structural", "error", "high", cycleServices=group,
                         required=edge["required"])
    findings.sort(key=lambda f: (f["path"], f["ruleId"]))
    counts = {severity: sum(f["severity"] == severity for f in findings) for severity in ("error", "warning")}
    return {
        "schemaVersion": 1, "tool": "compose-podman-audit", "toolVersion": __version__,
        "provider": provider, "providerVersion": provider_version,
        "catalogVersion": data["catalogVersion"], "evidenceRetrievedAt": data["retrievedAt"],
        "versionCoverage": "observed-reports-only" if provider_version in known else "unverified",
        "assessment": "limited-static-audit", "compatibility": "not-established",
        "summary": {"services": len(services), "findings": len(findings), **counts},
        "scope": {
            "evaluatedRules": sorted(data["rules"]),
            "notice": "No runtime reproduction or compatibility pass. Only declared dependencies and healthcheck fields are inspected. Image healthchecks and all other runtime semantics are not evaluated.",
            "notEvaluated": not_evaluated,
        },
        "findings": findings,
    }
