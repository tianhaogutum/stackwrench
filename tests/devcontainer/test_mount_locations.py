"""Port of test/mount-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench.devcontainer import check, to_sarif

from .helpers import FIXTURES, TMP, UNDEFINED, Result, find, js_stringify, position, read, run_cli, search, write

KINDS = ["devcontainer", "compose", "snapshot"]


def fixture_text(name: str) -> str:
    return read(FIXTURES / "mount-locations" / name)


class Fixture:
    def __init__(self, mkdtemp: Any, kind: str, changes: dict[str, Any] | None = None) -> None:
        changes = changes or {}
        self.changes = changes
        self.root = mkdtemp("wirecheck-mount-locations-")
        self.config = os.path.join(self.root, "devcontainer.json")
        compose = kind != "devcontainer"
        self.snapshot = snapshot = kind == "snapshot"
        self.document = os.path.join(self.root, "final.json" if snapshot else "compose.yaml") if compose else self.config
        self.pointer = "/services/web/volumes/0" if compose else "/mounts/0"
        if snapshot:
            text = js_stringify({"services": {"web": {"volumes": [{"type": "bind", "source": ".", "target": "/work"}]}}}, 2) + "\n"
        else:
            text = fixture_text("compose.yaml" if compose else "devcontainer.jsonc")
        for field, value in changes.items():
            if value is UNDEFINED:
                kept: list[str] = []
                for line in text.split("\n"):
                    if not re.search(rf'\b{field}["\s]*:', line):
                        kept.append(line)
                    # Removing the first YAML mapping field must preserve the sequence item.
                    elif compose and not snapshot and re.match(r"\s*- ", line):
                        kept.append(re.sub(r"- .*", "-", line, count=1))
                text = "\n".join(kept)
            else:
                text = re.sub(
                    rf'({field}["\s]*:\s*)"[^"]*"', lambda m, value=value: m.group(1) + js_stringify(value), text, count=1
                )
        text = re.sub(r",(\s*})", r"\1", text)  # Removing the last field must leave strict JSON valid.
        self.text = text
        if compose:
            write(self.config, js_stringify({"dockerComposeFile": "compose.yaml", "service": "web", "workspaceFolder": "/work"}))
            if snapshot:
                write(os.path.join(self.root, "compose.yaml"), "not parsed: [")
        write(self.document, text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if snapshot:
            self.options["composeJson"] = self.document
        self.source_relation = "compose-bind-source" if compose else "bind-source"

    def at(self, field: str) -> dict[str, Any]:
        value = self.changes.get(field)
        # changes[field] ?? default (undefined and null both fall back)
        token = js_stringify(value if value is not None and value is not UNDEFINED else ("." if field == "source" else "/work"))
        return {
            "file": self.document,
            "pointer": f"{self.pointer}/{field}",
            **position(self.text, token, search(self.text, rf'\b{field}["\s]*:')),
        }

    def run(self, *args: str) -> Result:
        result = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.snapshot else []), *args],
            cwd=TMP,
        )
        assert result.stderr == ""
        return result


def primary(report: dict[str, Any], code: str, expected: dict[str, Any]) -> dict[str, Any]:
    diagnostic = find(report["diagnostics"], lambda d: d["code"] == code)
    assert diagnostic, js_stringify(report["diagnostics"])
    assert diagnostic["locations"][0] == expected
    result = find(to_sarif(report)["runs"][0]["results"], lambda r: r["ruleId"] == code)
    assert result["locations"][0]["properties"]["jsonPointer"] == expected["pointer"]
    assert result["locations"][0]["physicalLocation"].get("region") == (
        None if expected.get("inferred") else {"startLine": expected["line"], "startColumn": expected["column"]}
    )
    return diagnostic


@pytest.mark.parametrize("kind", KINDS)
def test_object_mount_keeps_separate_source_target_explanations_nodes_and_coverage_locations(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind)
    json_run = f.run("--json", "--explain", "--strict")
    assert json_run.status == 0, json_run.stdout
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    source = find(report["explanations"], lambda e: e["relation"] == f.source_relation)
    target = find(report["explanations"], lambda e: e["relation"] == "mount-target")
    assert source["location"] == f.at("source")
    assert target["location"] == f.at("target")
    assert source["base"] == f.root
    assert source["target"] == f.root
    assert target["target"] == "bind . -> /work"
    assert target["status"] == "resolved"
    mount = find(report["nodes"], lambda n: n["kind"] == "mount")
    assert mount["location"]["pointer"] == f.pointer  # Preserve declaration identity.
    assert mount["sourceLocation"] == f.at("source")
    assert mount["targetLocation"] == f.at("target")
    assert find(report["explanations"], lambda e: e["relation"] == "workspace-coverage")["targetLocation"] == f.at("target")
    text = f.run("--explain", "--strict")
    assert text.status == 0
    for field in ["source", "target"]:
        loc = f.at(field)
        assert f"{loc['file']}:{loc['line']}:{loc['column']}#{loc['pointer']}" in text.stdout, text.stdout
    assert read(f.document) == f.text


@pytest.mark.parametrize("kind", KINDS)
def test_missing_object_bind_source_is_primary_at_source_in_json_explain_and_cli_sarif(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, {"source": "./absent"})
    report = check(f.options)
    d = primary(report, "missing-host-path", f.at("source"))
    assert d["locations"][1] == {"file": os.path.join(f.root, "absent"), "pointer": "/", "line": None, "column": None}
    source = find(report["explanations"], lambda e: e["relation"] == f.source_relation)
    assert source["location"] == f.at("source")
    assert source["status"] == "invalid"
    sarif = f.run("--sarif", "--strict")
    assert sarif.status == 1
    assert json.loads(sarif.stdout) == to_sarif(report)


@pytest.mark.parametrize("kind", KINDS)
def test_object_path_escapes_and_substitution_failures_keep_source_primary(mkdtemp: Any, kind: str) -> None:
    for source, code in [("../outside", "path-escape"), ("~unsupported", "unsupported-path"), ("${env:WIRECHECK_UNREAD}", "unsupported-substitution")]:
        f = Fixture(mkdtemp, kind, {"source": source})
        report = check(f.options)
        assert report["status"] == "error"
        primary(report, code, f.at("source"))


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_object_targets_keep_target_primary_and_never_evaluate_bind_sources(mkdtemp: Any, kind: str) -> None:
    for target, code in [("relative", "invalid-container-path"), ("", "invalid-field"), (42, "invalid-mount"), ("${env:WIRECHECK_UNREAD}", "unsupported-substitution")]:
        f = Fixture(mkdtemp, kind, {"target": target})
        report = check(f.options)
        assert report["status"] == "error"
        primary(report, code, f.at("target"))
        assert not any(e["relation"] == f.source_relation for e in report["explanations"])


@pytest.mark.parametrize("kind", KINDS)
def test_absent_object_mount_fields_are_inferred_and_sarif_does_not_fabricate_token_regions(mkdtemp: Any, kind: str) -> None:
    for field in ["source", "target"]:
        f = Fixture(mkdtemp, kind, {field: UNDEFINED})
        report = check(f.options)
        mount = find(report["nodes"], lambda n: n["kind"] == "mount")
        primary(report, "invalid-mount", {**mount["location"], "pointer": f"{f.pointer}/{field}", "inferred": True})


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_object_mount_type_uses_its_exact_token_without_evaluating_paths(mkdtemp: Any, kind: str) -> None:
    for mount_type in ["unsupported", 42, None, False, {}, [], ""]:
        f = Fixture(mkdtemp, kind, {"type": mount_type, "source": "./absent", "target": "relative"})
        json_run = f.run("--json", "--explain")
        assert json_run.status == 1
        report = json.loads(json_run.stdout)
        assert report == check(f.options)
        expected = {
            "file": f.document,
            "pointer": f"{f.pointer}/type",
            **position(f.text, js_stringify(mount_type), search(f.text, r'\btype["\s]*:')),
        }
        code = "invalid-field" if mount_type == "" else "invalid-mount"
        diagnostic = primary(report, code, expected)
        assert diagnostic["from"] == f"{f.document}#{expected['pointer']}"
        assert any(n["kind"] == "mount" and n["location"]["pointer"] == f.pointer for n in report["nodes"])
        assert not any(n["kind"] == "host-path" and n["label"] == os.path.join(f.root, "absent") for n in report["nodes"])
        assert not any(e["relation"] == f.source_relation for e in report["explanations"])
        assert not any(d["code"] in ("invalid-container-path", "missing-host-path", "missing-volume") for d in report["diagnostics"])
        target = find(report["explanations"], lambda e: e["relation"] == "mount-target")
        assert target["status"] == "invalid"
        assert any(r["code"] == code for r in target["reasons"])
        text = f.run("--explain")
        assert text.status == 1
        assert f"{expected['file']}:{expected['line']}:{expected['column']}#{expected['pointer']}" in text.stdout
        sarif = f.run("--sarif")
        assert sarif.status == 1
        assert json.loads(sarif.stdout) == to_sarif(report)
        if kind == "snapshot":
            assert report["scope"]["composeInput"]["format"] == "pre-rendered-json"
            assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
            assert "compose.yaml" not in js_stringify(to_sarif(report)["runs"][0]["results"])
        assert read(f.document) == f.text


@pytest.mark.parametrize("kind", KINDS)
def test_missing_object_mount_type_has_inferred_child_provenance_without_a_sarif_region(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, {"type": UNDEFINED, "source": "./absent"})
    json_run = f.run("--json")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    mount = find(report["nodes"], lambda n: n["kind"] == "mount")
    expected = {**mount["location"], "pointer": f"{f.pointer}/type", "inferred": True}
    primary(report, "invalid-mount", expected)
    assert not any(n["kind"] == "host-path" and n["label"] == os.path.join(f.root, "absent") for n in report["nodes"])
    assert not any(e["relation"] == f.source_relation for e in report["explanations"])
    text = f.run("--explain")
    assert text.status == 1
    assert f"#{expected['pointer']}" in text.stdout
    sarif = f.run("--sarif")
    assert sarif.status == 1
    assert json.loads(sarif.stdout) == to_sarif(report)
    loc = find(json.loads(sarif.stdout)["runs"][0]["results"], lambda r: r["ruleId"] == "invalid-mount")["locations"][0]
    assert loc["properties"]["inferred"] is True
    assert "region" not in loc["physicalLocation"]


@pytest.mark.parametrize("kind", KINDS)
def test_supported_object_mount_types_retain_verdicts_and_source_target_provenance(mkdtemp: Any, kind: str) -> None:
    for mount_type in ["bind", "volume", "tmpfs"]:
        f = Fixture(mkdtemp, kind, {"type": mount_type, **({} if mount_type == "bind" else {"source": UNDEFINED})})
        report = check(f.options)
        assert report["status"] == ("ok" if mount_type == "bind" else "partial")
        assert [d["code"] for d in report["diagnostics"]] == ([] if mount_type == "bind" else ["workspace-content-unknown"])
        mount = find(report["nodes"], lambda n: n["kind"] == "mount")
        assert mount["mount"]["type"] == mount_type
        assert mount["sourceLocation"] == (f.at("source") if mount_type == "bind" else None)
        assert mount["targetLocation"] == f.at("target")
        assert f.run("--json").status == 0
        assert f.run("--json", "--strict").status == (0 if mount_type == "bind" else 1)


def test_invalid_string_mount_types_and_malformed_short_strings_retain_scalar_provenance(mkdtemp: Any) -> None:
    for kind in ["devcontainer", "compose"]:
        values = ["type=unsupported,source=./absent,target=/work", "source=./absent,target=/work"] if kind == "devcontainer" else ["a:b:c:d"]
        for value in values:
            f = Fixture(mkdtemp, kind)
            for field in ["mounts", "workspaceMount"] if kind == "devcontainer" else ["volumes"]:
                model = (
                    {"services": {"web": {"volumes": [value]}}}
                    if kind == "compose"
                    else {"image": "synthetic:example", "workspaceFolder": "/work", field: [value] if field == "mounts" else value}
                )
                text = js_stringify(model, 2)
                write(f.document, text)
                report = check(f.options)
                primary(
                    report,
                    "invalid-mount",
                    {"file": f.document, "pointer": "/workspaceMount" if field == "workspaceMount" else f.pointer, **position(text, js_stringify(value))},
                )
                assert not any(n["kind"] == "host-path" and n["label"] == os.path.join(f.root, "absent") for n in report["nodes"])
                assert not any(e["relation"] == f.source_relation for e in report["explanations"])


def test_string_mounts_retain_enclosing_scalar_locations_including_aliases_and_workspace_mount(mkdtemp: Any) -> None:
    for kind in ["devcontainer", "compose"]:
        f = Fixture(mkdtemp, kind)
        mount_string = "./absent:/work" if kind == "compose" else "type=bind,src=./absent,dst=/work"
        value: dict[str, Any] = (
            {"services": {"web": {"volumes": [mount_string]}}}
            if kind == "compose"
            else {"image": "synthetic:example", "workspaceFolder": "/work", "mounts": [mount_string]}
        )
        write(f.document, js_stringify(value, 2))
        text = read(f.document)
        expected = {"file": f.document, "pointer": f.pointer, **position(text, js_stringify(mount_string))}
        report = check(f.options)
        primary(report, "missing-host-path", expected)
        for relation in [f.source_relation, "mount-target"]:
            assert find(report["explanations"], lambda e: e["relation"] == relation)["location"] == expected
        if kind == "devcontainer":
            del value["mounts"]
            value["workspaceMount"] = mount_string
            write(f.document, js_stringify(value, 2))
            primary(
                check(f.options),
                "missing-host-path",
                {**expected, "pointer": "/workspaceMount", **position(read(f.document), js_stringify(mount_string))},
            )


def test_duplicate_object_targets_identify_the_second_target_including_cross_compose_config_mounts(mkdtemp: Any) -> None:
    for compose in [False, True]:
        f = Fixture(mkdtemp, "compose" if compose else "devcontainer")
        mount = {"type": "bind", "source": ".", "target": "/work/"}
        value = (
            {"dockerComposeFile": "compose.yaml", "service": "web", "workspaceFolder": "/work", "mounts": [mount]}
            if compose
            else {"image": "synthetic:example", "workspaceFolder": "/work", "mounts": [{**mount, "target": "/work"}, mount]}
        )
        text = js_stringify(value, 2)
        write(f.config, text)
        primary(
            check(f.options),
            "duplicate-mount-target",
            {"file": f.config, "pointer": "/mounts/0/target" if compose else "/mounts/1/target", **position(text, '"/work/"')},
        )
