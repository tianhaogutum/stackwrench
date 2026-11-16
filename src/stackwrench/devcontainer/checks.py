"""Offline, source-located Dev Container and Compose wiring checks.

Port of the Node.js ``lib/index.js``. :func:`check` is synchronous and
read-only: paths are resolved against the current directory and confined to
the declared workspace; nothing is executed and no environment is read.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Callable, Mapping

from .documents import Document, InputError, Location, pointer, read_document
from .jsutil import FsError, errno_code, fs_error, js_basename, js_dirname, js_join, js_keys, js_relative, json_stringify
from .mounts import compose_mount, dev_mount
from .paths import canonical, container_path, host_path, stat

HOST_ASSUMPTIONS = {
    "local": "Local Linux filesystem snapshot; bind checks assume the daemon shares these paths. No Docker context or daemon was inspected.",
    "remote": "Remote daemon declared: local files do not establish remote bind availability. No remote filesystem was inspected.",
}
RENDER_HELP = (
    "Supply --compose-json FILE containing explicitly pre-rendered Compose JSON; render ordered files yourself with "
    "docker compose -f FIRST -f SECOND config --format json (parsing only; review interpolation inputs). "
    "This tool never invokes Docker."
)
_FS_CODES = ("EACCES", "EPERM", "ELOOP", "ENAMETOOLONG")
_SUBSTITUTION = re.compile(r"\$\{([^}]+)\}")
_MISSING: Any = object()  # JavaScript ``undefined`` for absent keys


def _object(value: Any) -> bool:
    return isinstance(value, dict)


def _error_message(error: BaseException) -> str:
    return getattr(error, "message", None) or str(error)


class Checker:
    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options = options
        self.nodes: dict[str, dict[str, Any]] = {}
        self.docs: list[Document] = []
        self.used: dict[int, set[str]] = {}
        self.root = ""
        self.selected_service: Any = None
        daemon = options.get("daemon")
        daemon = "local" if daemon is None else daemon
        self.report: dict[str, Any] = {
            "schemaVersion": 1,
            "status": "ok",
            "scope": {
                "platform": "linux",
                "daemon": daemon,
                "hostAssumption": HOST_ASSUMPTIONS.get(daemon),
                "workspaceRoot": None,
                "composeInput": None,
                "evaluatedServices": "all definitions, regardless of runServices/profiles",
                "limits": "2 MiB per document; 100 nesting levels; no image, image metadata, Dockerfile content or lifecycle inspection",
            },
            "nodes": [],
            "edges": [],
            "diagnostics": [],
            "explanations": [],
            "unevaluatedFields": [],
        }

    # --- graph primitives -------------------------------------------------

    def _used(self, doc: Document) -> set[str]:
        return self.used[id(doc)]

    def use(self, doc: Document, parts: list[Any]) -> None:
        used = self.used.get(id(doc))
        if used is not None:
            used.add(pointer(parts))

    def node(self, doc: Document, parts: list[Any], kind: str = "field", label: str | None = None) -> str:
        self.use(doc, parts)
        return self.add_node(
            {
                "id": f"{doc.file}#{pointer(parts)}",
                "kind": kind,
                "label": label if label is not None else pointer(parts),
                "location": doc.at(parts),
            }
        )

    def add_node(self, node: dict[str, Any]) -> str:
        if node["id"] not in self.nodes:
            self.nodes[node["id"]] = node
        return node["id"]

    def edge(
        self,
        source: str,
        target: str,
        relation: str,
        severity: str = "info",
        status: str = "resolved",
        location: Location | None = None,
    ) -> None:
        entry: dict[str, Any] = {"from": source, "to": target, "relation": relation, "severity": severity, "status": status}
        if location:
            entry["location"] = location
        self.report["edges"].append(entry)

    def diagnostic(
        self,
        code: str,
        severity: str,
        message: str,
        source: str | None = None,
        target: str | None = None,
        location: Location | None = None,
    ) -> None:
        candidates = [
            (self.nodes.get(source) or {}).get("location") if source else None,
            (self.nodes.get(target) or {}).get("location") if target else None,
            location,
        ]
        unique: dict[str, Location] = {}
        for candidate in candidates:
            if candidate:
                unique.setdefault(json_stringify(candidate), candidate)
        self.report["diagnostics"].append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "from": source if source else None,
                "to": target if target else None,
                "locations": list(unique.values()),
                "hostAssumption": self.report["scope"]["hostAssumption"],
            }
        )
        if source and target:
            self.edge(source, target, code, severity, "invalid" if severity == "error" else "unknown")

    def attempt(self, fn: Callable[[], Any], source: str | None, precise_location: bool = False) -> Any:
        try:
            return fn()
        except InputError as error:
            # Validation may fail at a child token (or an inferred absent field).
            # Keep that field primary instead of prepending the enclosing declaration.
            if precise_location and error.location:
                source = self.add_node(
                    {
                        "id": f"{error.location['file']}#{error.location['pointer']}",
                        "kind": "field",
                        "label": error.location["pointer"],
                        "location": error.location,
                    }
                )
            self.diagnostic(error.code, "error", error.message, source, None, error.location)
            return None

    def explain(
        self, doc: Document, parts: list[Any], relation: str, base: str | None, evaluate: Callable[[dict[str, Any]], Any]
    ) -> Any:
        entry: dict[str, Any] = {
            "location": doc.at(parts),
            "relation": relation,
            "base": base,
            "service": parts[1] if len(parts) > 1 and parts[0] == "services" else None,
            "target": None,
            "targetLocation": None,
            "status": "resolved",
            "reasons": [],
        }
        self.report["explanations"].append(entry)
        start = len(self.report["diagnostics"])
        try:
            return evaluate(entry)
        except InputError as error:
            entry["status"] = "invalid"
            entry["reasons"].append({"code": error.code, "message": error.message})
            raise
        except Exception as error:
            entry["status"] = "not-evaluated"
            entry["reasons"].append({"code": "evaluation-interrupted", "message": f"Check interrupted: {_error_message(error)}"})
            raise
        finally:
            # Only diagnostics from this evaluation belong to this outcome. A Compose
            # string build uses one source field for two independently checked paths.
            diagnostics = self.report["diagnostics"][start:]
            for d in diagnostics:
                if not any(r["code"] == d["code"] and r["message"] == d["message"] for r in entry["reasons"]):
                    entry["reasons"].append({"code": d["code"], "message": d["message"]})
            if any(d["severity"] == "error" for d in diagnostics):
                entry["status"] = "invalid"
            elif entry["status"] == "resolved" and any(d["severity"] == "warning" for d in diagnostics):
                entry["status"] = "unknown"

    def load(self, file: str, format: str) -> Document:
        doc = read_document(file, format)
        self.docs.append(doc)
        self.used[id(doc)] = set()
        self.add_node({"id": f"{doc.file}#/", "kind": "document", "label": js_basename(doc.file), "location": doc.at([])})
        return doc

    def expand(self, value: Any, doc: Document, parts: list[Any], compose: bool = False) -> str:
        location = doc.at(parts)
        if not isinstance(value, str) or not value or "\0" in value:
            raise InputError("invalid-field", "Expected a nonempty string without NUL", location)
        if compose:
            result = value
        else:

            def substitute(match: re.Match[str]) -> str:
                name = match.group(1)
                if name == "localWorkspaceFolder":
                    return self.root
                if name == "localWorkspaceFolderBasename":
                    return js_basename(self.root)
                raise InputError(
                    "unsupported-substitution", f"Unsupported substitution {match.group(0)}; no host environment is read", location
                )

            result = _SUBSTITUTION.sub(substitute, value)
        if "$" in result:
            raise InputError(
                "unsupported-substitution",
                "Unresolved dollar expansion in a wiring field; Compose variables require pre-rendered JSON",
                location,
            )
        return result

    def resolve(
        self, value: Any, base: str, doc: Document, parts: list[Any], compose: bool = False, relation: str = "host-path"
    ) -> str:
        def evaluate(entry: dict[str, Any]) -> str:
            file = host_path(self.expand(value, doc, parts, compose), base, self.root, doc.at(parts))
            entry["target"] = file
            return file

        return self.explain(doc, parts, relation, base, evaluate)

    def host(
        self,
        value: Any,
        base: str,
        doc: Document,
        parts: list[Any],
        expected: str | None,
        relation: str,
        bind: bool = False,
    ) -> dict[str, Any] | None:
        source = self.node(doc, parts)

        def evaluate(entry: dict[str, Any]) -> dict[str, Any]:
            file = host_path(self.expand(value, doc, parts, relation.startswith("compose-")), base, self.root, doc.at(parts))
            entry["target"] = file
            observation = stat(file)
            target = self.add_node(
                {
                    "id": f"host:{file}",
                    "kind": "host-path",
                    "label": file,
                    "location": {"file": file, "pointer": "/", "line": None, "column": None},
                    "localObservation": observation,
                }
            )
            if bind and self.report["scope"]["daemon"] == "remote":
                self.diagnostic(
                    "remote-bind-unknown",
                    "warning",
                    f"Remote bind availability is unknown for {file}; local snapshot: {observation['kind']}",
                    source,
                    target,
                )
            elif not observation["exists"]:
                self.diagnostic(
                    "missing-host-path", "error", f"Missing {'bind source' if bind else expected}: {file}", source, target
                )
            elif expected and observation["kind"] != expected:
                self.diagnostic(
                    "wrong-host-kind", "error", f"Expected {expected}, found {observation['kind']}: {file}", source, target
                )
            else:
                self.edge(source, target, relation)
            return {"file": file, "to": target, "observation": observation}

        return self.attempt(lambda: self.explain(doc, parts, relation, base, evaluate), source)

    # --- wiring checks ----------------------------------------------------

    def build(self, doc: Document, parts: list[Any], value: Any, base: str, compose: bool) -> None:
        source = self.node(doc, parts, "build")
        if not _object(value) and not (compose and isinstance(value, str)):
            raise InputError("invalid-build", "build must be an object (or a Compose context string)", doc.at(parts))
        string_form = isinstance(value, str)
        context_parts = parts if string_form else [*parts, "context"]
        # Do not mark the whole object consumed: report every unevaluated build option.
        if not string_form:
            self._used(doc).discard(pointer(parts))
        context_value = value if string_form else value.get("context", ".")
        context = self.host(
            context_value, base, doc, context_parts, "directory", "compose-build-context" if compose else "build-context"
        )
        if not string_form and "dockerfile_inline" in value:
            raise InputError(
                "unsupported-build",
                "dockerfile_inline is not evaluated; use a local Dockerfile",
                doc.at([*parts, "dockerfile_inline"]),
            )
        if not string_form and "additional_contexts" in value:
            raise InputError("unsupported-build", "additional_contexts is not evaluated", doc.at([*parts, "additional_contexts"]))
        dockerfile = "Dockerfile" if string_form else value.get("dockerfile", _MISSING)
        if not compose and dockerfile is _MISSING:
            raise InputError("invalid-build", "build.dockerfile is required in Dev Container Dockerfile mode", doc.at(parts))
        docker_parts = parts if string_form else [*parts, "dockerfile"]
        if context or not compose:
            self.host(
                "Dockerfile" if dockerfile is _MISSING else dockerfile,
                context["file"] if compose and context else base,
                doc,
                docker_parts,
                "file",
                "compose-dockerfile" if compose else "dockerfile",
            )
        else:

            def unresolved(entry: dict[str, Any]) -> None:
                entry["status"] = "not-evaluated"
                entry["reasons"].append(
                    {
                        "code": "unresolved-build-context",
                        "message": "Dockerfile base is unavailable because the build context could not be resolved",
                    }
                )

            self.explain(doc, docker_parts, "compose-dockerfile", None, unresolved)
        if not string_form and "options" in value:
            self.diagnostic(
                "build-options-unknown", "warning", "build.options may change path semantics and is not evaluated", source
            )

    def mounts(
        self,
        doc: Document,
        parts: list[Any],
        values: Any,
        base: str,
        compose: bool,
        top_volumes: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            raise InputError("invalid-mount", "mounts/volumes must be an array", doc.at(parts))
        self.use(doc, parts)
        result: list[dict[str, Any]] = []
        for i, raw in enumerate(values):
            p = [*parts, i]
            source = self.node(doc, p, "mount")

            def run(p: list[Any] = p, raw: Any = raw) -> None:
                result.append(self.mount(doc, p, raw, base, compose, top_volumes, result))

            self.attempt(run, source, True)
        return result

    def mount(
        self,
        doc: Document,
        parts: list[Any],
        raw: Any,
        base: str,
        compose: bool,
        top_volumes: dict[str, Any] | None = None,
        existing: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        existing = existing if existing is not None else []
        # Strings encode both paths in one scalar; only objects have child tokens.
        source_parts = [*parts, "source"] if _object(raw) else parts
        target_parts = [*parts, "target"] if _object(raw) else parts

        def evaluate(entry: dict[str, Any]) -> dict[str, Any]:
            value = raw
            # The Dev Container workspaceMount field supports string form only.
            if parts[0] == "workspaceMount" and not isinstance(value, str):
                self.expand(value, doc, parts)
            # Expand before parsing: ${VAR:-default} contains mount delimiters.
            if isinstance(value, str):
                value = self.expand(value, doc, parts, compose)
            elif _object(value):
                value = {
                    k: (self.expand(v, doc, [*parts, k], compose) if isinstance(v, str) else v) for k, v in value.items()
                }
            is_object = _object(value)

            def at(*extra: Any) -> Location:
                return doc.at([*parts, *extra] if is_object else parts)

            fields: dict[str, Any] = {
                "type": at("type"),
                "read_only": at("read_only"),
                "consistency": at("consistency"),
                "propagation": at("bind", "propagation"),
                "create_host_path": at("bind", "create_host_path"),
                "nocopy": at("volume", "nocopy"),
                "subpath": at("volume", "subpath"),
                "source": doc.at(source_parts),
                "target": doc.at(target_parts),
            }
            # Resolve arbitrary option keys as path parts, including escaped pointer keys.
            if is_object:
                fields["optionLocation"] = lambda option_parts: doc.at([*parts, *option_parts])
            mount = (compose_mount if compose else dev_mount)(value, doc.at(parts), fields)
            target = container_path(mount["target"], fields["target"])
            entry["target"] = f"{mount['type']} {mount['source'] if mount['source'] is not None else '(anonymous)'} -> {target}"
            item = self.record_mount(
                doc, parts, {**mount, "target": target}, base, compose, top_volumes, source_parts, target_parts
            )
            if any(m["target"] == item["target"] for m in existing):
                raise InputError(
                    "duplicate-mount-target",
                    f"Multiple mounts target {item['target']}; precedence is not inferred",
                    fields["target"],
                )
            return item

        return self.explain(doc, target_parts, "mount-target", base, evaluate)

    def record_mount(
        self,
        doc: Document,
        parts: list[Any],
        mount: dict[str, Any],
        base: str,
        compose: bool,
        top_volumes: dict[str, Any] | None = None,
        source_parts: list[Any] | None = None,
        target_parts: list[Any] | None = None,
    ) -> dict[str, Any]:
        top_volumes = top_volumes if top_volumes is not None else {}
        source_parts = source_parts if source_parts is not None else parts
        target_parts = target_parts if target_parts is not None else parts
        source = self.node(doc, parts, "mount")
        target = mount["target"]
        source_location = None if mount["source"] is None else doc.at(source_parts)
        target_location = doc.at(target_parts)
        self.nodes[source].update(
            {
                "mount": {"type": mount["type"], "source": mount["source"], "target": target},
                "sourceLocation": source_location,
                "targetLocation": target_location,
            }
        )
        item = {**mount, "from": source, "targetLocation": target_location}
        if mount["type"] == "bind":
            host = self.host(
                mount["source"], base, doc, source_parts, None, "compose-bind-source" if compose else "bind-source", True
            )
            if host:
                item["host"] = host
        elif mount["type"] == "volume":
            if compose and mount["source"] and mount["source"] not in top_volumes:
                self.diagnostic(
                    "missing-volume",
                    "error",
                    f"Named volume {mount['source']} is not declared in top-level volumes",
                    self.node(doc, source_parts),
                    self.node(doc, ["volumes"], "volume-definitions"),
                )
            else:
                if compose and mount["source"]:
                    to = self.node(doc, ["volumes", mount["source"]], "named-volume", mount["source"])
                else:
                    to = self.add_node(
                        {
                            "id": f"{source}:volume",
                            "kind": "named-volume",
                            "label": mount["source"] if mount["source"] is not None else "(anonymous)",
                            "location": doc.at(parts),
                        }
                    )
                self.edge(source, to, "uses-volume")
        return item

    def workspace(self, doc: Document, mounts: list[dict[str, Any]], compose: bool) -> Any:
        parts: list[Any] = ["workspaceFolder"]

        def evaluate(entry: dict[str, Any]) -> None:
            entry["service"] = self.selected_service if compose else None
            source = self.node(doc, parts, "workspace-folder")
            if "workspaceFolder" not in doc.data and not compose:
                self.diagnostic(
                    "implicit-workspace-unknown",
                    "warning",
                    "Implicit workspace/Git-root mounting is not inferred. Declare workspaceMount and workspaceFolder for an explicit coverage check.",
                    source,
                )
                return
            folder = container_path(self.expand(doc.data.get("workspaceFolder", "/"), doc, parts), doc.at(parts))
            entry["target"] = folder
            self.nodes[source]["containerPath"] = folder
            targets: set[str] = set()
            for mount in mounts:
                if mount["target"] in targets:
                    raise InputError(
                        "duplicate-mount-target",
                        f"Multiple mounts target {mount['target']}; precedence is not inferred",
                        mount["targetLocation"],
                    )
                targets.add(mount["target"])
            candidates = sorted(
                (
                    m
                    for m in mounts
                    if folder == m["target"] or folder.startswith(m["target"] if m["target"].endswith("/") else m["target"] + "/")
                ),
                key=lambda m: -len(m["target"]),
            )
            covering = candidates[0] if candidates else None
            entry["targetLocation"] = covering["targetLocation"] if covering else None
            if covering is None:
                self.diagnostic(
                    "workspace-uncovered",
                    "warning",
                    f"No declared mount covers {folder}. Image/Dockerfile filesystem content and implicit mounts are unknown; this does not prove the directory is absent.",
                    source,
                    mounts[0]["from"] if mounts else None,
                )
            elif covering["type"] != "bind":
                self.diagnostic(
                    "workspace-content-unknown",
                    "warning",
                    f"{folder} is covered by {covering['type']}, whose contents are not inspected",
                    source,
                    covering["from"],
                )
            elif covering.get("host"):
                entry["base"] = covering["host"]["file"]
                self.edge(source, covering["from"], "workspace-covered")
                # Check the mapped directory as well, respecting nested mounts and the root boundary.
                suffix = js_relative(covering["target"], folder)
                self.host(
                    js_join(covering["host"]["file"], suffix),
                    covering["host"]["file"],
                    doc,
                    parts,
                    "directory",
                    "workspace-bind-directory",
                    True,
                )
            else:
                entry["status"] = "not-evaluated"
                entry["reasons"].append(
                    {
                        "code": "unresolved-bind-source",
                        "message": "Workspace mapping cannot be evaluated because the covering bind source could not be resolved",
                    }
                )

        return self.explain(doc, parts, "workspace-coverage", None, evaluate)

    def dependencies(self, doc: Document, name: str, value: Any) -> None:
        parts: list[Any] = ["services", name, "depends_on"]
        if _object(value):
            # Leave the whole long-form subtree visible to the existing scope scan.
            def long_form(entry: dict[str, Any]) -> None:
                entry["status"] = "not-evaluated"
                entry["reasons"].append(
                    {
                        "code": "unsupported-depends-on-form",
                        "message": "Long-form depends_on is outside the evaluated wiring subset; only a short string list is checked",
                    }
                )

            self.explain(doc, parts, "depends-on", None, long_form)
            return
        self.use(doc, parts)
        if not isinstance(value, list):

            def invalid(entry: dict[str, Any]) -> None:
                raise InputError(
                    "invalid-depends-on",
                    "depends_on must be a short string list; long-form objects are not evaluated",
                    doc.at(parts),
                )

            self.attempt(lambda: self.explain(doc, parts, "depends-on", None, invalid), self.node(doc, parts))
            return
        if not value:

            def empty(entry: dict[str, Any]) -> None:
                entry["target"] = "No declared service dependencies"

            self.explain(doc, parts, "depends-on", None, empty)
        services = doc.data["services"]
        for i, dependency in enumerate(value):
            p = [*parts, i]
            source = self.node(doc, p, "service-reference")
            self.nodes[source]["service"] = name

            def evaluate(entry: dict[str, Any], p: list[Any] = p, dependency: Any = dependency, source: str = source) -> None:
                target = self.expand(dependency, doc, p, True)
                self.nodes[source]["dependency"] = target
                exists = target in services
                target_parts = ["services", target] if exists else ["services"]
                to = self.node(doc, target_parts, "service" if exists else "service-definitions", target if exists else None)
                entry["target"] = f"service {target} in {doc.file}"
                entry["targetLocation"] = doc.at(target_parts)
                # Direct declared service edges retain each occurrence's source evidence.
                self.edge(
                    self.node(doc, ["services", name], "service", name),
                    to,
                    "depends-on",
                    "info" if exists else "error",
                    "resolved" if exists else "invalid",
                    doc.at(p),
                )
                if not exists:
                    self.diagnostic("missing-service", "error", f"Service {target} is absent from {doc.file}", source, to)

            self.attempt(lambda evaluate=evaluate, p=p: self.explain(doc, p, "depends-on", None, evaluate), source)

    def compose(self, config: Document, base: str) -> None:
        raw = config.data.get("dockerComposeFile", _MISSING)
        parts: list[Any] = ["dockerComposeFile"]
        source = self.node(config, parts, "compose-reference")
        is_list = isinstance(raw, list)
        refs = raw if is_list else [raw]
        if not refs:
            raise InputError("invalid-compose-reference", "dockerComposeFile cannot be empty", config.at(parts))
        paths = [
            self.resolve(v, base, config, [*parts, i] if is_list else parts, False, "compose-reference")
            for i, v in enumerate(refs)
        ]
        compose_json = self.options.get("composeJson")
        compose_base_option = self.options.get("composeBase")
        if len(paths) > 1 and not compose_json:
            raise InputError("unsupported-compose-merge", RENDER_HELP, config.at(parts))
        for i, file in enumerate(paths):
            ref = self.host(file, base, config, [*parts, i] if is_list else parts, "file", "compose-file")
            if not ref or not ref["observation"]["exists"]:
                return
        file = host_path(compose_json, os.getcwd(), self.root) if compose_json else paths[0]
        compose_base = host_path(compose_base_option, os.getcwd(), self.root) if compose_base_option else js_dirname(paths[0])
        if stat(compose_base)["kind"] != "directory":
            raise InputError("invalid-compose-base", "Compose base must be an existing directory", config.at(parts))
        doc = self.load(file, "json" if compose_json or file.endswith(".json") else "yaml")
        self.report["scope"]["composeInput"] = {
            "format": "pre-rendered-json" if compose_json else "raw-single-file",
            "file": file,
            "base": compose_base,
            "orderedFiles": paths,
            "provenance": (
                "Locations refer to the supplied JSON; original override provenance is unavailable. Caller is responsible for rendering order and interpolation."
                if compose_json
                else "Locations refer to the raw Compose file."
            ),
        }
        self.edge(source, f"{doc.file}#/", "compose-document")
        if "include" in doc.data:
            raise InputError("unsupported-compose-include", RENDER_HELP, doc.at(["include"]))
        services = doc.data.get("services", _MISSING)
        if not _object(services) or not services:
            raise InputError("invalid-services", "Compose services must be a nonempty object", doc.at(["services"]))
        self.node(doc, ["services"], "service-definitions")
        self._used(doc).discard("/services")
        top_volumes = doc.data.get("volumes", {})
        if not _object(top_volumes):
            raise InputError("invalid-volumes", "Top-level volumes must be an object", doc.at(["volumes"]))
        for name in js_keys(top_volumes):
            definition = top_volumes[name]
            if definition is not None and not _object(definition):
                raise InputError("invalid-volume", "Named volume definition must be an object or null", doc.at(["volumes", name]))
            volume = self.node(doc, ["volumes", name], "named-volume", name)
            if definition is not None and any(k not in ("name", "labels") for k in definition):
                self.diagnostic(
                    "volume-runtime-unknown",
                    "warning",
                    f"Named volume {name} has runtime options; driver/external availability and contents are not evaluated",
                    volume,
                )

        def reference(name: Any, p: list[Any]) -> Any:
            n = self.node(config, p, "service-reference")

            def evaluate(entry: dict[str, Any]) -> str:
                expanded = self.expand(name, config, p)
                entry["service"] = expanded
                entry["target"] = f"service {expanded} in {doc.file}"
                entry["targetLocation"] = doc.at(["services", expanded] if expanded in services else ["services"])
                self.nodes[n]["service"] = expanded
                if expanded not in services:
                    self.diagnostic(
                        "missing-service",
                        "error",
                        f"Service {expanded} is absent from {doc.file}",
                        n,
                        self.node(doc, ["services"], "service-definitions"),
                    )
                else:
                    self.edge(n, self.node(doc, ["services", expanded], "service", expanded), "references-service")
                return expanded

            return self.explain(config, p, "references-service", None, evaluate)

        selected_service = reference(config.data.get("service", _MISSING), ["service"])
        self.selected_service = selected_service
        if "runServices" in config.data:
            self.use(config, ["runServices"])
            run_services = config.data["runServices"]
            if not isinstance(run_services, list):
                raise InputError("invalid-run-services", "runServices must be an array", config.at(["runServices"]))
            for i, name in enumerate(run_services):
                reference(name, ["runServices", i])
        selected: list[dict[str, Any]] = []
        for name in js_keys(services):
            service = services[name]
            p: list[Any] = ["services", name]
            node = self.node(doc, p, "service", name)
            self._used(doc).discard(pointer(p))
            if not _object(service):
                raise InputError("invalid-service", f"Service {name} must be an object", doc.at(p))

            def evaluate_service(name: str = name, service: dict[str, Any] = service, p: list[Any] = p, node: str = node) -> None:
                nonlocal selected
                for key in ("extends", "volumes_from", "develop", "tmpfs", "configs", "secrets"):
                    if key in service:
                        raise InputError(
                            "unsupported-compose-wiring",
                            f"{key} can alter the wiring graph and is not evaluated; use explicit supported volumes or remove this field from a reviewed snapshot. include/extends can first be resolved with pre-rendered JSON.",
                            doc.at([*p, key]),
                        )
                # Rejected build declarations and fields belong to their supplied value tokens.
                if "build" in service:
                    self.attempt(lambda: self.build(doc, [*p, "build"], service["build"], compose_base, True), node, True)
                # Collection errors belong to the volumes value, not its owning service.
                mounts = self.attempt(
                    lambda: self.mounts(doc, [*p, "volumes"], service.get("volumes", []), compose_base, True, top_volumes),
                    node,
                    True,
                )
                if name == selected_service:
                    selected = mounts if mounts is not None else []

            self.attempt(evaluate_service, node)
            if "depends_on" in service:
                self.dependencies(doc, name, service["depends_on"])
        self._used(doc).discard("/services")
        for name in js_keys(services):
            self._used(doc).discard(pointer(["services", name]))
        if "workspaceMount" in config.data:
            raise InputError(
                "unsupported-workspace-mount",
                "workspaceMount is only supported in image/Dockerfile mode; use Compose service volumes",
                config.at(["workspaceMount"]),
            )
        selected.extend(self.mounts(config, ["mounts"], config.data.get("mounts", []), self.root, False))
        if isinstance(selected_service, str) and selected_service in services:
            self.workspace(config, selected, True)
        else:

            def missing(entry: dict[str, Any]) -> None:
                entry["service"] = selected_service
                entry["status"] = "not-evaluated"
                entry["reasons"].append(
                    {
                        "code": "missing-service",
                        "message": f"Workspace coverage cannot be evaluated because service {selected_service} is missing",
                    }
                )

            self.explain(config, ["workspaceFolder"], "workspace-coverage", None, missing)

    def run(self) -> None:
        if sys.platform != "linux":
            raise InputError("unsupported-platform", "This version supports Linux hosts only")
        self.root = canonical(self.options["workspace"])
        self.report["scope"]["workspaceRoot"] = self.root
        if stat(self.root)["kind"] != "directory":
            raise InputError("invalid-workspace", "Declared workspace must be an existing directory")
        file = host_path(self.options["config"], os.getcwd(), self.root)
        config = self.load(file, "jsonc")
        data, base = config.data, js_dirname(file)
        modes = [k for k in ("image", "build", "dockerFile", "dockerComposeFile") if k in data]
        if len(modes) != 1:
            raise InputError(
                "invalid-mode", "Specify exactly one of image, build, dockerFile (legacy), dockerComposeFile", config.at([])
            )
        if "context" in data and modes[0] != "dockerFile":
            raise InputError(
                "invalid-mode-field", "Legacy context is supported only with dockerFile; use build.context otherwise", config.at(["context"])
            )
        if "runArgs" in data:
            self.diagnostic(
                "run-args-unknown", "warning", "runArgs can alter mounts and paths and is not evaluated", self.node(config, ["runArgs"])
            )
        if modes[0] == "dockerComposeFile":
            self.compose(config, base)
            return
        if self.options.get("composeJson") or self.options.get("composeBase"):
            raise InputError("invalid-option", "Compose options require dockerComposeFile mode", config.at([]))
        for key in ("service", "runServices"):
            if key in data:
                raise InputError("invalid-mode-field", f"{key} requires Compose mode", config.at([key]))
        if "image" in data:
            self.expand(data["image"], config, ["image"])
            self.node(config, ["image"], "image")
        # Non-object declarations fail at /build; object errors keep their existing evidence.
        if "build" in data:
            document_node = self.node(config, [], "document")
            self.attempt(lambda: self.build(config, ["build"], data["build"], base, False), document_node, not _object(data["build"]))
        if "dockerFile" in data:
            self.host(data.get("context", "."), base, config, ["context"], "directory", "build-context")
            self.host(data["dockerFile"], base, config, ["dockerFile"], "file", "dockerfile")
        mounts: list[dict[str, Any]] = []
        if "workspaceMount" in data:
            self.node(config, ["workspaceMount"], "mount")
            if "workspaceFolder" not in data:
                raise InputError("invalid-workspace", "workspaceMount requires workspaceFolder", config.at(["workspaceMount"]))
            # Keep the original field location, rather than creating synthetic document offsets.
            mounts.append(self.mount(config, ["workspaceMount"], data["workspaceMount"], self.root, False))
        mounts.extend(self.mounts(config, ["mounts"], data.get("mounts", []), self.root, False))
        self.workspace(config, mounts, False)

    def finish(self) -> dict[str, Any]:
        for doc in self.docs:
            used = self._used(doc)

            def scan(value: Any, parts: list[Any], doc: Document = doc, used: set[str] = used) -> None:
                ptr = pointer(parts)
                if ptr in used and parts:
                    return
                prefix = ("" if ptr == "/" else ptr) + "/"
                descendants = any(p.startswith(prefix) for p in used)
                if parts and not descendants:
                    self.report["unevaluatedFields"].append(
                        {"location": doc.at(parts), "reason": "Outside the evaluated wiring subset; not executed or validated"}
                    )
                    return
                if isinstance(value, dict):
                    for key in js_keys(value):
                        scan(value[key], [*parts, key])
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        scan(item, [*parts, index])

            scan(doc.data, [])
        self.report["nodes"] = list(self.nodes.values())
        ds = self.report["diagnostics"]
        errors = sum(1 for d in ds if d["severity"] == "error")
        warnings = sum(1 for d in ds if d["severity"] == "warning")
        self.report["status"] = "error" if errors else "partial" if warnings else "ok"
        self.report["summary"] = {
            "errors": errors,
            "warnings": warnings,
            "nodes": len(self.report["nodes"]),
            "edges": len(self.report["edges"]),
            "unevaluatedFields": len(self.report["unevaluatedFields"]),
        }
        return self.report


def check(options: Mapping[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
    """Synchronous, read-only check; returns the JSON-compatible report.

    Options (camelCase as in the JSON API; snake_case keyword aliases accepted):
    ``config``, ``workspace``, ``composeJson``, ``composeBase``, ``daemon``.
    """
    merged: dict[str, Any] = dict(options or {})
    for key, value in kwargs.items():
        merged[{"compose_json": "composeJson", "compose_base": "composeBase"}.get(key, key)] = value
    for key in ("config", "workspace", "composeJson", "composeBase"):
        if isinstance(merged.get(key), os.PathLike):
            merged[key] = os.fspath(merged[key])
    if not merged.get("config") or not merged.get("workspace"):
        raise TypeError("config and workspace are required")
    if merged.get("daemon") and merged["daemon"] not in HOST_ASSUMPTIONS:
        raise TypeError("daemon must be local or remote")
    checker = Checker(merged)
    try:
        checker.run()
    except InputError as error:
        checker.diagnostic(error.code, "error", error.message, None, None, error.location)
    except FsError as error:
        if error.code not in _FS_CODES:
            raise
        checker.diagnostic("filesystem-error", "error", error.message)
    except OSError as error:
        code = errno_code(error)
        if code not in _FS_CODES:
            raise
        checker.diagnostic("filesystem-error", "error", fs_error(error, "stat").message)
    return checker.finish()
