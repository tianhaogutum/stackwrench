"""Port of test/mount-subpath-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench.devcontainer import InputError, check, compose_mount, to_sarif

from .group_fixture import ABSENT, GroupFixture, short_option_text
from .helpers import assert_valid_sarif, find, js_stringify, line_column, read, write

KINDS = ["yaml", "snapshot"]


def group_of(field: str, mount: dict[str, Any]) -> str:
    if field == "subpath" or (field == "unsupported" and field not in mount):
        return "volume/"
    return "bind/" if field in ("propagation", "create_host_path") else ""


def fixture(mkdtemp: Any, kind: str, subpath: Any, changes: dict[str, Any] | None = None) -> GroupFixture:
    mount = {"type": "volume", "source": "cache", "target": "/work", "volume": {} if subpath is ABSENT else {"subpath": subpath}, **(changes or {})}
    return GroupFixture(mkdtemp, "wirecheck-subpath-", kind, mount, True, group_of)


@pytest.mark.parametrize("kind", KINDS)
def test_every_defined_subpath_rejects_at_its_exact_nested_token(mkdtemp: Any, kind: str) -> None:
    for subpath in ["sub", "", "${UNRESOLVED}", "\u0000", True, False, 42, 0, None, {}, []]:
        f = fixture(mkdtemp, kind, subpath, {"source": "undeclared", "target": "relative", "volume": {"nocopy": 42, "subpath": subpath}})
        json_run = f.run("--json", "--explain")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        assert report == check(f.options)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        expected = f.at("subpath")
        assert diagnostic["message"] == "volume.subpath is not evaluated"
        assert diagnostic["locations"][0] == expected
        assert diagnostic["from"] == f"{f.document}#{expected['pointer']}"
        assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"])["location"] == expected
        assert not any(
            d["code"] in ("invalid-container-path", "missing-host-path", "missing-volume", "unsupported-substitution") for d in report["diagnostics"]
        )
        assert not any(n["kind"] == "host-path" and os.path.basename(n["label"]) == "undeclared" for n in report["nodes"])
        assert not any(e["relation"] == "uses-volume" for e in report["edges"])
        explanation = find(report["explanations"], lambda e: e["relation"] == "mount-target")
        assert explanation["status"] == "invalid"
        assert explanation["reasons"] == [{"code": "invalid-mount", "message": diagnostic["message"]}]
        assert explanation["location"]["pointer"] == f"{f.pointer}/target"
        mount = find(report["nodes"], lambda n: n["kind"] == "mount")
        assert mount["id"] == f"{f.document}#{f.pointer}"
        assert mount["location"]["pointer"] == f.pointer
        text = f.run("--explain")
        assert text.status == 1
        assert f"{expected['file']}:{expected['line']}:{expected['column']}#{expected['pointer']}" in text.stdout
        sarif = f.run("--sarif")
        assert sarif.status == 1
        converted = json.loads(sarif.stdout)
        assert_valid_sarif(converted)
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
def test_omitted_subpath_preserves_volume_identity_provenance_and_unknown_contents(mkdtemp: Any, kind: str) -> None:
    for volume in [ABSENT, {}, {"nocopy": True}, {"nocopy": False}]:
        for anonymous in [False, True]:
            f = fixture(mkdtemp, kind, ABSENT, {"volume": volume, **({"source": ABSENT} if anonymous else {})})
            os.mkdir(os.path.join(f.root, "cache"))
            report = check(f.options)
            assert report["status"] == "partial"
            assert [d["code"] for d in report["diagnostics"]] == ["workspace-content-unknown"]
            mount = find(report["nodes"], lambda n: n["kind"] == "mount")
            assert mount["id"] == f"{f.document}#{f.pointer}"
            assert mount["location"]["pointer"] == f.pointer
            assert mount["sourceLocation"] == (None if anonymous else f.at("source"))
            assert mount["targetLocation"] == f.at("target")
            assert any(e["relation"] == "uses-volume" for e in report["edges"])
            assert not any(n["kind"] == "host-path" and os.path.basename(n["label"]) == "cache" for n in report["nodes"])
            for args, exit in [([], 0), (["--strict"], 1)]:
                json_run = f.run("--json", *args)
                assert json_run.status == exit
                assert json.loads(json_run.stdout) == report
            assert read(f.document) == f.text


@pytest.mark.parametrize("kind", KINDS)
def test_subpath_keeps_earlier_option_and_structural_failures(mkdtemp: Any, kind: str) -> None:
    cases: list[tuple[dict[str, Any], str, str]] = [
        ({"unsupported": True}, "unsupported", "Unsupported mount option unsupported"),
        ({"read_only": 42}, "read_only", "read_only must be boolean"),
        ({"consistency": 42}, "consistency", "Unsupported consistency value"),
        ({"bind": {"propagation": "shared"}}, "bind", "bind options require type=bind and an object"),
        ({"type": "bind", "source": ".", "bind": {"propagation": "shared"}}, "propagation", "Only default rprivate bind propagation is supported"),
        ({"type": "bind", "source": ".", "bind": {"create_host_path": 42}}, "create_host_path", "create_host_path must be boolean"),
        ({"type": "bind", "source": "."}, "volume", "volume options require type=volume and an object"),
        *[({"volume": volume}, "volume", "volume options require type=volume and an object") for volume in [42, None, [], "invalid"]],
        ({"volume": {"subpath": "sub", "unsupported": True}}, "unsupported", "Unsupported mount option unsupported"),
    ]
    for changes, field, message in cases:
        f = fixture(mkdtemp, kind, "sub", changes)
        json_run = f.run("--json")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        assert diagnostic["message"] == message
        parent = find(report["nodes"], lambda n: n["kind"] == "mount")["location"]
        assert diagnostic["locations"][0] == (f.at(field) if field else parent)


@pytest.mark.parametrize("kind", KINDS)
def test_subpath_failure_retains_independent_declared_dependency_checks(mkdtemp: Any, kind: str) -> None:
    f = fixture(mkdtemp, kind, "sub")
    if kind == "yaml":
        text = f.text.replace("  web:\n", "  web:\n    depends_on: [db, missing, db]\n", 1).replace(
            "volumes:\n  cache: {}", "  db: {}\nvolumes:\n  cache: {}", 1
        )
    else:
        model = json.loads(f.text)
        model["services"]["web"]["depends_on"] = ["db", "missing", "db"]
        model["services"]["db"] = {}
        text = js_stringify(model, 2)
    write(f.document, text)
    json_run = f.run("--json")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert [d["code"] for d in report["diagnostics"][:2]] == ["invalid-mount", "missing-service"]
    assert report["diagnostics"][0]["locations"][0]["pointer"] == f"{f.pointer}/volume/subpath"
    assert report["diagnostics"][1]["locations"][0]["pointer"] == "/services/web/depends_on/1"
    assert [[e["to"].split("#")[1], e["status"]] for e in report["edges"] if e["relation"] == "depends-on"] == [
        ["/services/db", "resolved"],
        ["/services", "invalid"],
        ["/services/db", "resolved"],
    ]
    assert read(f.document) == text


def test_subpath_parser_without_matching_metadata_retains_enclosing_fallback() -> None:
    location = {"file": "synthetic.yaml", "pointer": "/services/web/volumes/0", "line": 4, "column": 9}
    for fields in [None, {"nocopy": {**location, "pointer": f"{location['pointer']}/volume/nocopy"}}]:
        with pytest.raises(InputError) as caught:
            compose_mount({"type": "volume", "source": "cache", "target": "/work", "volume": {"subpath": "sub", "nocopy": 42}}, location, fields)
        assert caught.value.code == "invalid-mount"
        assert caught.value.message == "volume.subpath is not evaluated"
        assert caught.value.location == location


def test_subpath_like_short_mount_options_retain_enclosing_scalar_policy(mkdtemp: Any) -> None:
    for kind in KINDS:
        for scalar in ["cache:/work:subpath", "cache:/work:subpath=sub", "cache:/work:volume-subpath=sub"]:
            f = fixture(mkdtemp, kind, ABSENT)
            text = short_option_text(kind, scalar, True)
            write(f.document, text)
            json_run = f.run("--json")
            assert json_run.status == 1
            report = json.loads(json_run.stdout)
            diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
            expected = {"file": f.document, "pointer": f.pointer, **line_column(text, text.find(js_stringify(scalar)))}
            assert diagnostic["message"] == "Unsupported or conflicting short mount options"
            assert diagnostic["locations"][0] == expected
            sarif = f.run("--sarif")
            assert sarif.status == 1
            assert json.loads(sarif.stdout) == to_sarif(report)
