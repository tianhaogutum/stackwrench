"""Port of test/mount-nocopy-locations.test.js (node:test) to pytest."""

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
    if field in ("nocopy", "subpath", "unsupported"):
        return "volume/"
    return "bind/" if field in ("propagation", "create_host_path") else ""


def fixture(mkdtemp: Any, kind: str, nocopy: Any, changes: dict[str, Any] | None = None) -> GroupFixture:
    mount = {"type": "volume", "source": "cache", "target": "/work", "volume": {} if nocopy is ABSENT else {"nocopy": nocopy}, **(changes or {})}
    return GroupFixture(mkdtemp, "wirecheck-nocopy-", kind, mount, True, group_of)


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_nocopy_identifies_its_nested_token_before_target_path_reference_checks(mkdtemp: Any, kind: str) -> None:
    for nocopy in ["true", "false", "invalid", 42, 0, None, {}, [], "", "${UNRESOLVED}"]:
        f = fixture(mkdtemp, kind, nocopy, {"source": "undeclared", "target": "relative"})
        json_run = f.run("--json", "--explain")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        assert report == check(f.options)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        expected = f.at("nocopy")
        assert diagnostic["message"] == "nocopy must be boolean"
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
        assert any(r["code"] == "invalid-mount" for r in explanation["reasons"])
        assert find(report["nodes"], lambda n: n["kind"] == "mount")["location"]["pointer"] == f.pointer
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
def test_accepted_and_omitted_nocopy_keep_declared_anonymous_contents_unknown(mkdtemp: Any, kind: str) -> None:
    for value in [True, False, ABSENT]:
        for anonymous in [False, True]:
            for changes in [{}, *([{"volume": ABSENT}] if value is ABSENT else [])]:
                f = fixture(mkdtemp, kind, value, {**changes, **({"source": ABSENT} if anonymous else {})})
                # An existing same-name directory is irrelevant to volume contents.
                os.mkdir(os.path.join(f.root, "cache"))
                report = check(f.options)
                assert report["status"] == "partial"
                assert [d["code"] for d in report["diagnostics"]] == ["workspace-content-unknown"]
                assert find(report["explanations"], lambda e: e["relation"] == "workspace-coverage")["status"] == "unknown"
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
def test_nocopy_keeps_earlier_option_errors_and_exact_structural_failures(mkdtemp: Any, kind: str) -> None:
    cases: list[tuple[dict[str, Any], str, str]] = [
        ({"read_only": 42}, "read_only", "read_only must be boolean"),
        ({"consistency": 42}, "consistency", "Unsupported consistency value"),
        ({"bind": {"propagation": "shared", "create_host_path": 42}}, "bind", "bind options require type=bind and an object"),
        ({"type": "bind", "source": ".", "bind": {"propagation": "shared", "create_host_path": 42}}, "propagation", "Only default rprivate bind propagation is supported"),
        ({"type": "bind", "source": ".", "bind": {"propagation": "rprivate", "create_host_path": 42}}, "create_host_path", "create_host_path must be boolean"),
        ({"type": "bind", "source": "."}, "volume", "volume options require type=volume and an object"),
        *[({"volume": volume}, "volume", "volume options require type=volume and an object") for volume in [42, None, [], "invalid"]],
        ({"volume": {"nocopy": 42, "unsupported": True}}, "unsupported", "Unsupported mount option unsupported"),
        ({"volume": {"nocopy": 42, "subpath": "sub"}}, "subpath", "volume.subpath is not evaluated"),
    ]
    for changes, field, message in cases:
        f = fixture(mkdtemp, kind, 42, changes)
        json_run = f.run("--json")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        assert diagnostic["message"] == message
        parent = find(report["nodes"], lambda n: n["kind"] == "mount")["location"]
        assert diagnostic["locations"][0] == (f.at(field) if field else parent)
        assert not any(e["relation"] == "uses-volume" for e in report["edges"])


@pytest.mark.parametrize("kind", KINDS)
def test_accepted_nocopy_leaves_later_target_source_reference_policies_and_inference_intact(mkdtemp: Any, kind: str) -> None:
    for changes, code, field, message in [
        ({"target": "relative"}, "invalid-container-path", "target", None),
        ({"target": ABSENT}, "invalid-mount", "target", "Mount target is required"),
        ({"source": "./cache"}, "invalid-mount", "source", "Named volume source must be a name, not a host path"),
        ({"source": "undeclared"}, "missing-volume", "source", None),
    ]:
        f = fixture(mkdtemp, kind, True, changes)
        json_run = f.run("--json")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == code)
        if message:
            assert diagnostic["message"] == message
        parent = find(report["nodes"], lambda n: n["kind"] == "mount")["location"]
        expected = {**parent, "pointer": f"{f.pointer}/target", "inferred": True} if changes.get("target") is ABSENT else f.at(field)
        assert diagnostic["locations"][0] == expected
        assert not any(n["kind"] == "host-path" and re.search(r"cache|undeclared", n["label"]) for n in report["nodes"])
        sarif = json.loads(f.run("--sarif").stdout)
        assert_valid_sarif(sarif)
        assert sarif == to_sarif(report)
        primary = find(sarif["runs"][0]["results"], lambda r: r["ruleId"] == code)["locations"][0]
        if changes.get("target") is ABSENT:
            assert primary["properties"]["inferred"] is True
            assert "region" not in primary["physicalLocation"]


def test_nocopy_parser_without_matching_metadata_retains_enclosing_fallback() -> None:
    location = {"file": "synthetic.yaml", "pointer": "/services/web/volumes/0", "line": 4, "column": 9}
    for fields in [None, {"target": {**location, "pointer": f"{location['pointer']}/target"}}]:
        with pytest.raises(InputError) as caught:
            compose_mount({"type": "volume", "source": "cache", "target": "/work", "volume": {"nocopy": 42}}, location, fields)
        assert caught.value.code == "invalid-mount"
        assert caught.value.message == "nocopy must be boolean"
        assert caught.value.location == location


def test_nocopy_like_short_mount_options_retain_enclosing_scalar_policy(mkdtemp: Any) -> None:
    for kind in KINDS:
        for scalar in ["cache:/work:nocopy", "cache:/work:nocopy=true", "cache:/work:volume-nocopy"]:
            f = fixture(mkdtemp, kind, ABSENT)
            text = short_option_text(kind, scalar, True)
            write(f.document, text)
            report = check(f.options)
            diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
            expected = {"file": f.document, "pointer": f.pointer, **line_column(text, text.find(js_stringify(scalar)))}
            assert diagnostic["message"] == "Unsupported or conflicting short mount options"
            assert diagnostic["locations"][0] == expected
            sarif = f.run("--sarif")
            assert sarif.status == 1
            assert json.loads(sarif.stdout) == to_sarif(report)
