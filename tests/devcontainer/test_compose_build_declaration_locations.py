"""Port of test/compose-build-declaration-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from stackwrench.devcontainer import check, to_sarif

from .helpers import TMP, Result, assert_valid_sarif, find, js_stringify, line_column, run_cli, write, yaml_stringify

OMITTED: Any = object()
KINDS = ["yaml", "json", "snapshot"]
MESSAGE = "build must be an object (or a Compose context string)"


def escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


class Fixture:
    def __init__(
        self,
        mkdtemp: Any,
        kind: str,
        value: Any,
        name: str = "web",
        extra: dict[str, Any] | None = None,
        later: dict[str, Any] | None = None,
        flow: bool = False,
        config_extra: dict[str, Any] | None = None,
    ) -> None:
        self.kind = kind
        self.root = mkdtemp("wirecheck-build-declaration-")
        self.config = os.path.join(self.root, "devcontainer.json")
        self.document = os.path.join(self.root, {"yaml": "compose.yaml", "json": "compose.json"}.get(kind, "final.json"))
        service: dict[str, Any] = {} if value is OMITTED else {"build": value}
        service.update({"volumes": [".:/work"]})
        service.update(extra or {})
        model = {"services": {name: service, **(later or {})}}
        self.text = yaml_stringify(model) if kind == "yaml" else js_stringify(model, 2) + "\n"
        if kind == "yaml" and flow:
            self.text = f'services:\n  {js_stringify(name)}:\n    build: {js_stringify(value)}\n    volumes: [".:/work"]\n'
        write(
            self.config,
            js_stringify(
                {
                    "dockerComposeFile": "compose.json" if kind == "json" else "compose.yaml",
                    "service": name,
                    "workspaceFolder": "/work",
                    **(config_extra or {}),
                }
            ),
        )
        if kind == "snapshot":
            write(os.path.join(self.root, "compose.yaml"), "deliberately unparsed: [")
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if kind == "snapshot":
            self.options["composeJson"] = self.document
        self.pointer = f"/services/{escape(name)}/build"
        self.inputs = [self.config, self.document, *([os.path.join(self.root, "compose.yaml")] if kind == "snapshot" else [])]
        self.before = [Path(p).read_bytes() for p in self.inputs]

    def run(self, *flags: str) -> Result:
        result = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.kind == "snapshot" else []), *flags],
            cwd=TMP,
        )
        assert result.stderr == ""
        return result

    def at_build(self) -> dict[str, Any]:
        token = "build:" if self.kind == "yaml" else '"build":'
        end = self.text.find(token) + len(token)
        offset = end + len(re.match(r"\s*", self.text[end:]).group(0))
        return {"file": self.document, "pointer": self.pointer, **line_column(self.text, offset)}

    def unchanged(self) -> None:
        for path, before in zip(self.inputs, self.before):
            assert Path(path).read_bytes() == before


def assert_build_failure(f: Fixture) -> dict[str, Any]:
    json_run = f.run("--json", "--explain")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-build")
    assert diagnostic["message"] == MESSAGE
    assert diagnostic["severity"] == "error"
    expected = f.at_build()
    assert diagnostic["locations"] == [expected]
    assert diagnostic["from"] == f"{f.document}#{f.pointer}"
    assert diagnostic["to"] is None
    assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"])["location"] == expected
    assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"])["kind"] == "build"
    service_id = f"{f.document}#{f.pointer[: -len('/build')]}"
    assert any(n["kind"] == "service" and n["id"] == service_id for n in report["nodes"])
    assert any(n["kind"] == "mount" for n in report["nodes"])
    assert any(e["relation"] == "workspace-covered" for e in report["edges"])
    assert not any(e["relation"] in ("compose-build-context", "compose-dockerfile") for e in report["explanations"])
    assert any(e["relation"] == "mount-target" for e in report["explanations"])
    assert not any(e["relation"] == "invalid-build" for e in report["edges"])
    text = f.run("--explain")
    assert text.status == 1
    assert f"error [invalid-build] {f.document}:{expected['line']}:{expected['column']}#{expected['pointer']}: {MESSAGE}" in text.stdout
    sarif_run = f.run("--sarif")
    assert sarif_run.status == 1
    sarif = json.loads(sarif_run.stdout)
    assert_valid_sarif(sarif)
    assert sarif == to_sarif(report)
    primary = find(sarif["runs"][0]["results"], lambda r: r["ruleId"] == "invalid-build")["locations"][0]
    assert primary["properties"]["jsonPointer"] == expected["pointer"]
    assert not primary["properties"].get("inferred")
    assert primary["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
    strict = f.run("--json", "--strict")
    assert strict.status == 1
    assert json.loads(strict.stdout) == report
    if f.document.endswith("final.json"):
        assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
        assert "compose.yaml" not in js_stringify(sarif["runs"][0]["results"])
    f.unchanged()
    return expected


@pytest.mark.parametrize("kind", KINDS)
def test_null_build_points_at_its_supplied_value_token(mkdtemp: Any, kind: str) -> None:
    expected = assert_build_failure(Fixture(mkdtemp, kind, None))
    assert [expected["line"], expected["column"]] == ([3, 12] if kind == "yaml" else [4, 16])


@pytest.mark.parametrize("kind", KINDS)
def test_boolean_and_numeric_build_declarations_retain_structural_rejection(mkdtemp: Any, kind: str) -> None:
    for value in [False, True, 0, 42, -1.5]:
        assert_build_failure(Fixture(mkdtemp, kind, value, name="web/~"))


@pytest.mark.parametrize("kind", KINDS)
def test_array_build_declarations_point_at_the_opening_token_without_traversing_children(mkdtemp: Any, kind: str) -> None:
    for value in [[], ["${HOST}"], [{"context": "../outside", "dockerfile_inline": "ignored"}]]:
        assert_build_failure(Fixture(mkdtemp, kind, value))
    if kind == "yaml":
        assert_build_failure(Fixture(mkdtemp, kind, ["."], flow=True))


@pytest.mark.parametrize("kind", KINDS)
def test_omitted_and_accepted_builds_retain_default_dockerfile_and_workspace_coverage(mkdtemp: Any, kind: str) -> None:
    for value in [OMITTED, ".", {}, {"context": "."}]:
        f = Fixture(mkdtemp, kind, value)
        write(os.path.join(f.root, "Dockerfile"), "FROM scratch\n")
        run = f.run("--json", "--strict")
        assert run.status == 0
        report = json.loads(run.stdout)
        assert report == check(f.options)
        assert report["diagnostics"] == []
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
        build_edges = [e for e in report["edges"] if e["relation"] in ("compose-build-context", "compose-dockerfile")]
        assert len(build_edges) == (0 if value is OMITTED else 2)
        if value is not OMITTED:
            assert any(e["to"] == f"host:{os.path.join(f.root, 'Dockerfile')}" for e in build_edges)
        f.unchanged()


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_build_preserves_mount_traversal_independent_dependencies_and_later_services(mkdtemp: Any, kind: str) -> None:
    f = Fixture(
        mkdtemp,
        kind,
        None,
        extra={
            "depends_on": ["missing", "later"],
            "volumes": [None, {"type": "volume", "source": "cache", "target": "/other", "volume": {"nocopy": 42}}, ".:/work"],
        },
        later={
            "later": {"profiles": ["excluded"], "build": False, "volumes": False, "depends_on": ["web"]},
            "last": {"build": 42, "volumes": [".:/later"], "depends_on": [42, "web"]},
        },
    )
    run = f.run("--json")
    assert run.status == 1
    report = json.loads(run.stdout)
    assert report == check(f.options)
    assert [d["code"] for d in report["diagnostics"]] == [
        "invalid-build", "invalid-mount", "invalid-mount", "missing-service", "invalid-build", "invalid-mount", "invalid-build", "invalid-field",
    ]
    assert report["diagnostics"][0]["locations"] == [f.at_build()]
    assert [d["locations"][0]["pointer"] for d in report["diagnostics"]] == [
        "/services/web/build", "/services/web/volumes/0", "/services/web/volumes/1/volume/nocopy", "/services/web/depends_on/0",
        "/services/later/build", "/services/later/volumes", "/services/last/build", "/services/last/depends_on/0",
    ]
    assert len([e for e in report["edges"] if e["relation"] == "depends-on" and e["status"] == "resolved"]) == 3
    assert any(e["relation"] == "workspace-covered" for e in report["edges"])
    mount = find(report["nodes"], lambda n: (n.get("mount") or {}).get("target") == "/work")
    assert mount["location"]["pointer"] == "/services/web/volumes/2"
    assert mount["sourceLocation"] == mount["location"]
    assert mount["targetLocation"] == mount["location"]
    f.unchanged()


@pytest.mark.parametrize("kind", KINDS)
def test_earlier_unsupported_service_wiring_still_skips_builds_mounts_and_preserves_dependencies(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, None, extra={"extends": {}, "depends_on": ["missing"]}, later={"last": {"build": False, "volumes": [".:/later"]}})
    run = f.run("--json")
    assert run.status == 1
    report = json.loads(run.stdout)
    assert [d["code"] for d in report["diagnostics"]] == ["unsupported-compose-wiring", "missing-service", "invalid-build", "workspace-uncovered"]
    assert [l["pointer"] for l in report["diagnostics"][0]["locations"]] == ["/services/web", "/services/web/extends"]
    assert not any(n["id"] == f"{f.document}#{f.pointer}" for n in report["nodes"])
    assert any(n["kind"] == "mount" and n["location"]["pointer"] == "/services/last/volumes/0" for n in report["nodes"])
    f.unchanged()


@pytest.mark.parametrize("kind", KINDS)
def test_object_build_failures_keep_earlier_context_unsupported_field_and_host_path_evidence(mkdtemp: Any, kind: str) -> None:
    for value in [{"context": None}, {"dockerfile_inline": "FROM scratch"}, {"additional_contexts": []}, {"context": "./missing", "dockerfile": "absent"}]:
        f = Fixture(mkdtemp, kind, value)
        report = json.loads(f.run("--json").stdout)
        assert report == check(f.options)
        if "context" in value and value["context"] is None:
            assert [d["code"] for d in report["diagnostics"]] == ["invalid-field"]
            assert [l["pointer"] for l in report["diagnostics"][0]["locations"]] == ["/services/web/build/context"]
        elif value.get("context") == "./missing":
            assert [d["code"] for d in report["diagnostics"]] == ["missing-host-path", "missing-host-path"]
            assert [d["locations"][0]["pointer"] for d in report["diagnostics"]] == ["/services/web/build/context", "/services/web/build/dockerfile"]
        else:
            assert [d["code"] for d in report["diagnostics"]] == ["unsupported-build"]
            key = next(iter(value))
            assert [l["pointer"] for l in report["diagnostics"][0]["locations"]] == [f"/services/web/build/{key}"]
            assert report["diagnostics"][0]["from"] == f"{f.document}#/services/web/build/{key}"
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
        f.unchanged()


def test_dev_container_rejected_build_shapes_retain_build_primary_evidence_and_continued_mounts(mkdtemp: Any) -> None:
    for value in [None, False, 42, [], "."]:
        f = Fixture(mkdtemp, "yaml", OMITTED)
        write(f.config, js_stringify({"build": value, "workspaceFolder": "/work", "mounts": ["type=bind,source=.,target=/work"]}))
        run = f.run("--json")
        assert run.status == 1
        report = json.loads(run.stdout)
        assert [[d["code"], d["message"], [l["pointer"] for l in d["locations"]]] for d in report["diagnostics"]] == [
            ["invalid-build", MESSAGE, ["/build"]]
        ]
        assert report["diagnostics"][0]["from"] == f"{f.config}#/build"
        assert any(n["kind"] == "mount" for n in report["nodes"])
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
