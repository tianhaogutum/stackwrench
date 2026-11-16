"""Provider-independent queries over the validated declared dependency graph."""

from collections import deque
import json

from . import __version__
from .audit import _components, _dependency_graph, _validate
from .input import InputError, field_location, json_path


def _cycle(name, graph, membership, groups):
    group = groups[membership[name]]
    self_cycle = name in graph[name]
    participates = len(group) > 1 or self_cycle
    return {"participates": participates, "selfCycle": self_cycle,
            "services": list(group) if participates else []}


def _declaration(model, services, name, edge):
    parts = edge["parts"]
    short = isinstance(parts[-1], int)
    options = {} if short else services[name]["depends_on"][edge["target"]]
    fields = [parts, *[(*parts, key) for key in ("condition", "required", "restart") if key in options]]
    return {"service": name, "target": edge["target"], "form": "short" if short else "long",
            "condition": edge["condition"], "conditionDeclared": not short,
            "required": edge["required"], "requiredDeclared": "required" in options,
            "restart": options.get("restart"), "path": json_path(parts),
            "fieldLocations": [field_location(model, json_path(field)) for field in fields]}


def impact_query(model, service):
    """Return reverse reachability; input uses the same loader contract as audit."""
    services, dependencies, not_evaluated = _validate(model)
    if not isinstance(service, str) or service not in services:
        raise InputError("unknown-service", "Select one exact service name declared in this document.", "$.services")
    graph = _dependency_graph(services, dependencies)
    membership, groups = _components(graph)
    reverse = {name: set() for name in services}
    for name, targets in graph.items():
        for target in targets:
            reverse[target].add(name)

    # BFS computes only distances. Resolve witnesses separately so equal-length
    # ties compare in dependency direction, not reverse-BFS discovery order.
    distances, pending = {service: 0}, deque([service])
    while pending:
        target = pending.popleft()
        for name in sorted(reverse[target]):
            if name not in distances:
                distances[name] = distances[target] + 1
                pending.append(name)
    paths = {service: (service,)}
    for name in sorted(distances, key=lambda name: (distances[name], name)):
        if name != service:
            paths[name] = (name,) + min(paths[target] for target in graph[name]
                                       if distances.get(target) == distances[name] - 1)

    # Index once: a witness can share edges with many other witnesses.
    edges = {(name, edge["target"]): edge for name, declared in dependencies.items() for edge in declared}
    dependents = []
    for name in sorted(distances.keys() - {service}):
        path = paths[name]
        dependents.append({"service": name, "direct": distances[name] == 1, "distance": distances[name],
                           "witnessPath": list(path),
                           "witnessEdges": [_declaration(model, services, source, edges[source, target])
                                            for source, target in zip(path, path[1:])],
                           "cycle": _cycle(name, graph, membership, groups)})
    return {
        "schemaVersion": 1, "tool": "compose-podman-audit", "toolVersion": __version__,
        "mode": "impact", "assessment": "declared-dependency-impact", "selectedService": service,
        "selectedServiceLocation": field_location(model, json_path(("services", service))),
        "selectedCycle": _cycle(service, graph, membership, groups),
        "directDependents": [row["service"] for row in dependents if row["direct"]],
        "transitiveDependents": [row["service"] for row in dependents if not row["direct"]],
        "dependents": dependents,
        "scope": {
            "notice": "Declared dependency reachability only; not predicted startup order, health, completion, "
                      "restart behavior or provider compatibility. No commands, containers or probes are run. "
                      "Optional edges and all supported declared conditions count; absent targets are excluded.",
            "notEvaluated": not_evaluated,
        },
    }


def dependency_path_query(model, source, target):
    """One shortest declared path; equal endpoints use the zero-edge path."""
    services, dependencies, not_evaluated = _validate(model)
    for name in (source, target):
        if not isinstance(name, str) or name not in services:
            raise InputError("unknown-service", "Select two exact service names declared in this document.", "$.services")
    graph = _dependency_graph(services, dependencies)
    membership, groups = _components(graph)

    # Forward FIFO BFS with sorted neighbors visits shortest candidate sequences
    # in lexical order. First discovery fixes each node's canonical predecessor.
    parents, pending = {source: None}, deque([source])
    while pending and target not in parents:
        name = pending.popleft()
        for child in sorted(graph[name]):
            if child not in parents:
                parents[child] = name
                pending.append(child)
    path = []
    if target in parents:
        name = target
        while name is not None:
            path.append(name)
            name = parents[name]
        path.reverse()
    edges = {(name, edge["target"]): edge for name, declared in dependencies.items() for edge in declared}
    return {
        "schemaVersion": 1, "tool": "compose-podman-audit", "toolVersion": __version__,
        "mode": "dependency-path", "assessment": "declared-dependency-path",
        "sourceService": source, "targetService": target,
        "sourceServiceLocation": field_location(model, json_path(("services", source))),
        "targetServiceLocation": field_location(model, json_path(("services", target))),
        "sourceCycle": _cycle(source, graph, membership, groups),
        "targetCycle": _cycle(target, graph, membership, groups),
        "reachable": bool(path), "distance": len(path) - 1 if path else None,
        "witnessPath": path,
        "witnessEdges": [_declaration(model, services, name, edges[name, child])
                         for name, child in zip(path, path[1:])],
        "scope": {
            "notice": "Declared dependency reachability only; not predicted startup order, health, completion, "
                      "restart behavior or provider compatibility. No commands, containers or probes are run. "
                      "Optional edges and all supported declared conditions count; absent targets are excluded. "
                      "Equal endpoints use a zero-edge path; cycle membership is reported separately.",
            "notEvaluated": not_evaluated,
        },
    }


def render_dependency_path(report):
    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=True)
    lines = [f"stackwrench podman-audit {report['toolVersion']} | Declared dependency path",
             report["scope"]["notice"]]
    fields = (("Source service", "sourceService"), ("Target service", "targetService"),
              ("Source declaration", "sourceServiceLocation"), ("Target declaration", "targetServiceLocation"),
              ("Source cycle", "sourceCycle"), ("Target cycle", "targetCycle"),
              ("Reachable", "reachable"), ("Distance", "distance"), ("Witness path", "witnessPath"),
              ("Witness edges", "witnessEdges"))
    lines.extend(label + ": " + encode(report[key]) for label, key in fields)
    lines.extend("Not evaluated: " + encode(item) for item in report["scope"]["notEvaluated"])
    return "\n".join(lines)


def render_impact(report):
    """Escape all names as JSON, including in terminal output."""
    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=True)
    lines = [f"stackwrench podman-audit {report['toolVersion']} | Declared dependency impact",
             report["scope"]["notice"],
             "Selected service: " + encode(report["selectedService"]),
             "Selected declaration: " + encode(report["selectedServiceLocation"]),
             "Selected cycle: " + encode(report["selectedCycle"]),
             "Direct dependents: " + encode(report["directDependents"]),
             "Transitive dependents: " + encode(report["transitiveDependents"])]
    lines.extend("Dependent: " + encode(row) for row in report["dependents"])
    if not report["dependents"]:
        lines.append("No declared dependents.")
    lines.extend("Not evaluated: " + encode(item) for item in report["scope"]["notEvaluated"])
    return "\n".join(lines)
