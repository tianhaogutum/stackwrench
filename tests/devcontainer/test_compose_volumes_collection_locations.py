"""Port of test/compose-volumes-collection-locations.test.js (node:test) to pytest."""

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
KINDS = ["yaml", "snapshot"]
MESSAGE = "mounts/volumes must be an array"


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
        self.root = mkdtemp("wirecheck-volumes-collection-")
        self.config = os.path.join(self.root, "devcontainer.json")
        self.document = os.path.join(self.root, "compose.yaml" if kind == "yaml" else "final.json")
        service: dict[str, Any] = {} if value is OMITTED else {"volumes": value}
        service.update(extra or {})
        model = {"services": {name: service, **(later or {})}}
        self.text = yaml_stringify(model) if kind == "yaml" else js_stringify(model, 2) + "\n"
        if kind == "yaml" and flow:
            self.text = f"services:\n  {js_stringify(name)}:\n    volumes: {js_stringify(value)}\n"
        write(
            self.config,
            js_stringify({"dockerComposeFile": "compose.yaml", "service": name, "workspaceFolder": "/work", **(config_extra or {})}),
        )
        if kind == "snapshot":
            write(os.path.join(self.root, "compose.yaml"), "deliberately unparsed: [")
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if kind == "snapshot":
            self.options["composeJson"] = self.document
        self.pointer = f"/services/{escape(name)}/volumes"
        self.inputs = [self.config, self.document, *([os.path.join(self.root, "compose.yaml")] if kind == "snapshot" else [])]
        self.before = [Path(p).read_bytes() for p in self.inputs]

    def run(self, *flags: str) -> Result:
        result = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.kind == "snapshot" else []), *flags],
            cwd=TMP,
        )
        assert result.stderr == ""
        return result

    def at_volumes(self) -> dict[str, Any]:
        token = "volumes:" if self.kind == "yaml" else '"volumes":'
        end = self.text.find(token) + len(token)
        offset = end + len(re.match(r"\s*", self.text[end:]).group(0))
        return {"file": self.document, "pointer": self.pointer, **line_column(self.text, offset)}

    def unchanged(self) -> None:
        for path, before in zip(self.inputs, self.before):
            assert Path(path).read_bytes() == before


def assert_collection_failure(f: Fixture) -> dict[str, Any]:
    json_run = f.run("--json", "--explain")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
    assert diagnostic["message"] == MESSAGE
    assert diagnostic["severity"] == "error"
    expected = f.at_volumes()
    assert diagnostic["locations"] == [expected]
    assert diagnostic["from"] == f"{f.document}#{f.pointer}"
    assert diagnostic["to"] is None
    assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"])["location"] == expected
    assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"])["kind"] == "field"
    service_id = f"{f.document}#{f.pointer[: -len('/volumes')]}"
    assert any(n["kind"] == "service" and n["id"] == service_id for n in report["nodes"])
    assert not any(n["kind"] == "mount" for n in report["nodes"])
    assert not any(e["relation"] == "mount-target" for e in report["explanations"])
    assert not any(e["relation"] == "invalid-mount" for e in report["edges"])
    text = f.run("--explain")
    assert text.status == 1
    assert f"error [invalid-mount] {f.document}:{expected['line']}:{expected['column']}#{expected['pointer']}: {MESSAGE}" in text.stdout
    sarif_run = f.run("--sarif")
    assert sarif_run.status == 1
    sarif = json.loads(sarif_run.stdout)
    assert_valid_sarif(sarif)
    assert sarif == to_sarif(report)
    primary = find(sarif["runs"][0]["results"], lambda r: r["ruleId"] == "invalid-mount")["locations"][0]
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
def test_null_volumes_collection_points_at_its_supplied_value_token(mkdtemp: Any, kind: str) -> None:
    expected = assert_collection_failure(Fixture(mkdtemp, kind, None))
    assert [expected["line"], expected["column"]] == ([3, 14] if kind == "yaml" else [4, 18])


@pytest.mark.parametrize("kind", KINDS)
def test_scalar_volumes_collections_retain_rejection_without_expansion_or_mount_parsing(mkdtemp: Any, kind: str) -> None:
    for value in [False, True, 0, 42, "", ".:/work", "${HOST}"]:
        assert_collection_failure(Fixture(mkdtemp, kind, value, name="web/~"))


@pytest.mark.parametrize("kind", KINDS)
def test_object_volumes_collections_point_at_their_opening_token_not_child_fields(mkdtemp: Any, kind: str) -> None:
    for value in [{}, {"type": "bind", "source": ".", "target": "/work"}, {"mystery": "${HOST}"}]:
        assert_collection_failure(Fixture(mkdtemp, kind, value))
    if kind == "yaml":
        assert_collection_failure(Fixture(mkdtemp, kind, {"target": "/work"}, flow=True))


@pytest.mark.parametrize("kind", KINDS)
def test_omitted_and_empty_volumes_remain_accepted_while_other_mounts_cover_workspace(mkdtemp: Any, kind: str) -> None:
    for value in [OMITTED, []]:
        f = Fixture(mkdtemp, kind, value, config_extra={"mounts": [{"type": "bind", "source": ".", "target": "/work"}]})
        json_run = f.run("--json", "--strict")
        assert json_run.status == 0
        report = json.loads(json_run.stdout)
        assert report == check(f.options)
        assert report["status"] == "ok"
        assert report["diagnostics"] == []
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
        assert not any((n.get("location") or {}).get("pointer") == f.pointer for n in report["nodes"])
        f.unchanged()


@pytest.mark.parametrize("kind", KINDS)
def test_collection_failure_preserves_earlier_build_evidence_dependencies_and_later_services(mkdtemp: Any, kind: str) -> None:
    f = Fixture(
        mkdtemp,
        kind,
        None,
        extra={"build": 42, "depends_on": ["missing", "later"]},
        later={
            "later": {
                "profiles": ["excluded"],
                "volumes": [None, {"type": "volume", "source": "cache", "target": "/other", "volume": {"nocopy": 42}}, ".:/work"],
                "depends_on": [42, "web"],
            },
            "last": {"volumes": False, "depends_on": ["missing"]},
        },
    )
    run = f.run("--json")
    assert run.status == 1
    report = json.loads(run.stdout)
    assert report == check(f.options)
    assert [d["code"] for d in report["diagnostics"]] == [
        "invalid-build", "invalid-mount", "missing-service", "invalid-mount", "invalid-mount", "invalid-field", "invalid-mount", "missing-service", "workspace-uncovered",
    ]
    assert [l["pointer"] for l in report["diagnostics"][0]["locations"]] == ["/services/web/build"]
    assert report["diagnostics"][1]["locations"][0] == f.at_volumes()
    assert [d["locations"][0]["pointer"] for d in report["diagnostics"][3:5]] == ["/services/later/volumes/0", "/services/later/volumes/1/volume/nocopy"]
    assert report["diagnostics"][6]["locations"][0]["pointer"] == "/services/last/volumes"
    assert any(e["relation"] == "depends-on" and e["status"] == "resolved" and e["to"] == f"{f.document}#/services/later" for e in report["edges"])
    assert any(
        e["relation"] == "depends-on" and e["status"] == "resolved" and e["location"]["pointer"] == "/services/later/depends_on/1"
        for e in report["edges"]
    )
    mount = find(report["nodes"], lambda n: (n.get("mount") or {}).get("target") == "/work")
    assert mount["location"]["pointer"] == "/services/later/volumes/2"
    assert mount["sourceLocation"]["pointer"] == mount["location"]["pointer"]
    assert mount["targetLocation"]["pointer"] == mount["location"]["pointer"]
    f.unchanged()


@pytest.mark.parametrize("kind", KINDS)
def test_earlier_unsupported_service_checks_still_skip_mounts_but_preserve_dependencies(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, None, extra={"extends": {}, "build": 42, "depends_on": ["missing"]}, later={"last": {"volumes": False}})
    run = f.run("--json")
    assert run.status == 1
    report = json.loads(run.stdout)
    assert report == check(f.options)
    assert [d["code"] for d in report["diagnostics"]] == ["unsupported-compose-wiring", "missing-service", "invalid-mount", "workspace-uncovered"]
    assert [l["pointer"] for l in report["diagnostics"][0]["locations"]] == ["/services/web", "/services/web/extends"]
    assert report["diagnostics"][2]["locations"][0]["pointer"] == "/services/last/volumes"
    assert not any(n["id"] == f"{f.document}#{f.pointer}" for n in report["nodes"])
    f.unchanged()


def test_dev_container_non_array_mounts_keep_existing_rejection_and_endpoint_behavior(mkdtemp: Any) -> None:
    for compose in [False, True]:
        f = Fixture(mkdtemp, "yaml", [], config_extra={"mounts": None})
        if not compose:
            write(f.config, js_stringify({"image": "synthetic:example", "mounts": None}))
        json_run = f.run("--json")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        assert [[d["code"], d["message"], d["from"], d["to"], d["locations"][0]["pointer"]] for d in report["diagnostics"]] == [
            ["invalid-mount", MESSAGE, None, None, "/mounts"]
        ]
