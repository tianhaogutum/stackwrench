"""Port of test/mount-create-host-path-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench.devcontainer import InputError, check, compose_mount, to_sarif

from .group_fixture import ABSENT, GroupFixture, short_option_text
from .helpers import find, js_stringify, line_column, read, write

KINDS = ["yaml", "snapshot"]


def group_of(field: str, mount: dict[str, Any]) -> str:
    if field in ("nocopy", "subpath"):
        return "volume/"
    return "bind/" if field in ("create_host_path", "propagation", "unsupported") else ""


def fixture(mkdtemp: Any, kind: str, create_host_path: Any, changes: dict[str, Any] | None = None) -> GroupFixture:
    mount = {
        "type": "bind",
        "source": ".",
        "target": "/work",
        "bind": {} if create_host_path is ABSENT else {"create_host_path": create_host_path},
        **(changes or {}),
    }
    return GroupFixture(mkdtemp, "wirecheck-create-host-path-", kind, mount, False, group_of)


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_create_host_path_identifies_its_nested_token_before_path_checks(mkdtemp: Any, kind: str) -> None:
    for create_host_path in ["true", "false", "invalid", 42, 0, None, {}, [], ""]:
        f = fixture(mkdtemp, kind, create_host_path, {"source": "./absent", "target": "relative"})
        json_run = f.run("--json", "--explain")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        assert report == check(f.options)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        expected = f.at("create_host_path")
        assert diagnostic["message"] == "create_host_path must be boolean"
        assert diagnostic["locations"][0] == expected
        assert diagnostic["from"] == f"{f.document}#{expected['pointer']}"
        assert not any(d["code"] in ("invalid-container-path", "missing-host-path") for d in report["diagnostics"])
        assert not any(n["kind"] == "host-path" and n["label"] == os.path.join(f.root, "absent") for n in report["nodes"])
        assert not any(e["relation"].endswith("bind-source") for e in report["explanations"])
        explanation = find(report["explanations"], lambda e: e["relation"] == "mount-target")
        assert explanation["status"] == "invalid"
        assert any(r["code"] == "invalid-mount" for r in explanation["reasons"])
        assert find(report["nodes"], lambda n: n["kind"] == "mount")["location"]["pointer"] == f.pointer
        text = f.run("--explain")
        assert text.status == 1
        assert f"{expected['file']}:{expected['line']}:{expected['column']}#{expected['pointer']}" in text.stdout
        sarif = f.run("--sarif")
        assert sarif.status == 1
        converted = json.loads(sarif.stdout)
        assert converted == to_sarif(report)
        primary = find(converted["runs"][0]["results"], lambda r: r["ruleId"] == "invalid-mount")["locations"][0]
        assert primary["properties"]["jsonPointer"] == expected["pointer"]
        assert not primary["properties"].get("inferred")
        assert primary["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
        if kind == "snapshot":
            assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
            assert "compose.yaml" not in js_stringify(converted["runs"][0]["results"])
        assert read(f.document) == f.text


@pytest.mark.parametrize("kind", KINDS)
def test_accepted_and_omitted_create_host_path_retain_strict_verdict_and_path_provenance(mkdtemp: Any, kind: str) -> None:
    for value, changes in [
        (True, {}),
        (False, {}),
        (ABSENT, {}),
        (ABSENT, {"bind": ABSENT}),
        (True, {"bind": {"propagation": "rprivate", "create_host_path": True}}),
    ]:
        f = fixture(mkdtemp, kind, value, changes)
        report = check(f.options)
        assert report["status"] == "ok"
        assert report["diagnostics"] == []
        mount = find(report["nodes"], lambda n: n["kind"] == "mount")
        assert mount["location"]["pointer"] == f.pointer
        assert mount["sourceLocation"] == f.at("source")
        assert mount["targetLocation"] == f.at("target")
        assert find(report["explanations"], lambda e: e["relation"] == "compose-bind-source")["location"] == f.at("source")
        json_run = f.run("--json", "--strict")
        assert json_run.status == 0
        assert json.loads(json_run.stdout) == report


@pytest.mark.parametrize("kind", KINDS)
def test_accepted_create_host_path_never_creates_a_missing_source(mkdtemp: Any, kind: str) -> None:
    for value in [True, False, ABSENT]:
        f = fixture(mkdtemp, kind, value, {"source": "./absent"})
        json_run = f.run("--json")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        assert report == check(f.options)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "missing-host-path")
        assert diagnostic["locations"][0] == f.at("source")
        assert not any(d["code"] == "invalid-mount" for d in report["diagnostics"])
        assert find(report["nodes"], lambda n: n["kind"] == "mount")["location"]["pointer"] == f.pointer
        assert not os.path.exists(os.path.join(f.root, "absent"))
        sarif = f.run("--sarif")
        assert sarif.status == 1
        assert json.loads(sarif.stdout) == to_sarif(report)


@pytest.mark.parametrize("kind", KINDS)
def test_create_host_path_keeps_earlier_option_errors_and_exact_structural_failures(mkdtemp: Any, kind: str) -> None:
    for changes, field, message in [
        ({"read_only": 42}, "read_only", "read_only must be boolean"),
        ({"consistency": 42}, "consistency", "Unsupported consistency value"),
        ({"type": "volume"}, "bind", "bind options require type=bind and an object"),
        ({"bind": 42}, "bind", "bind options require type=bind and an object"),
        ({"bind": {"create_host_path": "shared", "unsupported": True}}, "unsupported", "Unsupported mount option unsupported"),
        ({"bind": {"propagation": "shared", "create_host_path": 42}}, "propagation", "Only default rprivate bind propagation is supported"),
        ({"type": "volume", "bind": ABSENT, "volume": {"nocopy": 42}}, "nocopy", "nocopy must be boolean"),
        ({"type": "volume", "bind": ABSENT, "volume": {"subpath": "sub", "nocopy": 42}}, "subpath", "volume.subpath is not evaluated"),
    ]:
        f = fixture(mkdtemp, kind, "shared", changes)
        report = check(f.options)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        assert diagnostic["message"] == message
        parent = find(report["nodes"], lambda n: n["kind"] == "mount")["location"]
        assert diagnostic["locations"][0] == (f.at(field) if field else parent)
        assert not any(e["relation"].endswith("bind-source") for e in report["explanations"])


def test_create_host_path_parser_without_matching_metadata_retains_enclosing_fallback() -> None:
    location = {"file": "synthetic.yaml", "pointer": "/services/web/volumes/0", "line": 4, "column": 9}
    for fields in [None, {"target": {**location, "pointer": f"{location['pointer']}/target"}}]:
        with pytest.raises(InputError) as caught:
            compose_mount({"type": "bind", "source": ".", "target": "/work", "bind": {"create_host_path": "shared"}}, location, fields)
        assert caught.value.code == "invalid-mount"
        assert caught.value.message == "create_host_path must be boolean"
        assert caught.value.location == location


def test_create_host_path_like_short_mount_options_retain_enclosing_scalar_policy(mkdtemp: Any) -> None:
    for kind in KINDS:
        f = fixture(mkdtemp, kind, ABSENT)
        scalar = ".:/work:create_host_path"
        text = short_option_text(kind, scalar, False)
        write(f.document, text)
        report = check(f.options)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        expected = {"file": f.document, "pointer": f.pointer, **line_column(text, text.find(js_stringify(scalar)))}
        assert diagnostic["message"] == "Unsupported or conflicting short mount options"
        assert diagnostic["locations"][0] == expected
        sarif = f.run("--sarif")
        assert sarif.status == 1
        assert json.loads(sarif.stdout) == to_sarif(report)
