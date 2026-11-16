"""Port of test/named-volume-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench.devcontainer import check, to_sarif

from .helpers import TMP, Result, find, js_stringify, line_column, read, run_cli, write

MOUNT_POINTER = "/services/web/volumes/0"
KINDS = ["yaml", "snapshot"]


class Fixture:
    def __init__(
        self,
        mkdtemp: Any,
        kind: str,
        source: str = "absent",
        declared: bool = False,
        anonymous: bool = False,
        string: bool = False,
        omit_definitions: bool = False,
    ) -> None:
        self.root = mkdtemp("wirecheck-volume-locations-")
        self.config = os.path.join(self.root, "devcontainer.json")
        self.snapshot = snapshot = kind == "snapshot"
        self.document = os.path.join(self.root, "final.json" if snapshot else "compose.yaml")
        mount: Any = f"{source}:/work" if string else {"type": "volume", **({} if anonymous else {"source": source}), "target": "/work"}
        model: dict[str, Any] = {"services": {"web": {"volumes": [mount]}}}
        if not omit_definitions:
            model["volumes"] = {source: None} if declared else {}
        if string:
            yaml_mount = f'      - "{mount}"\n'
        else:
            yaml_mount = "      - type: volume\n" + ("" if anonymous else f'        source: "{source}"\n') + "        target: /work\n"
        definitions = "" if omit_definitions else f"volumes: {chr(10) + '  ' + source + ': null' if declared else '{}'}\n"
        self.text = js_stringify(model, 2) + "\n" if snapshot else f"services:\n  web:\n    volumes:\n{yaml_mount}{definitions}"
        refs: Any = ["base.yaml", "override.yaml"] if snapshot else "compose.yaml"
        if snapshot:
            for ref in refs:
                write(os.path.join(self.root, ref), "not parsed: [")
        write(self.config, js_stringify({"dockerComposeFile": refs, "service": "web", "workspaceFolder": "/work"}))
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if snapshot:
            self.options["composeJson"] = self.document
        self.source_location = (
            None
            if anonymous
            else self.at(MOUNT_POINTER if string else f"{MOUNT_POINTER}/source", js_stringify(mount if string else source))
        )

    # Use literal text offsets, independently of readDocument's parser locations.
    def at(self, pointer: str, token: str) -> dict[str, Any]:
        offset = self.text.find(token)
        assert offset >= 0, token
        return {"file": self.document, "pointer": pointer, **line_column(self.text, offset)}

    def run(self, *args: str) -> Result:
        result = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.snapshot else []), *args],
            cwd=TMP,
        )
        assert result.stderr == ""
        return result


def no_host_lookup(report: dict[str, Any], source: str) -> None:
    assert not any(n["kind"] == "host-path" and os.path.basename(n["label"]) == source for n in report["nodes"])
    assert not any(e["relation"].endswith("bind-source") for e in report["explanations"])
    assert not any(d["code"] in ("missing-host-path", "wrong-host-kind") for d in report["diagnostics"])


def missing_reference(report: dict[str, Any], source_location: dict[str, Any], pointer: str = MOUNT_POINTER) -> dict[str, Any]:
    diagnostic = find(report["diagnostics"], lambda d: d["code"] == "missing-volume")
    assert diagnostic, js_stringify(report["diagnostics"])
    assert diagnostic["locations"][0] == source_location
    source = find(report["nodes"], lambda n: n["id"] == diagnostic["from"])
    assert source["location"] == source_location
    related = find(report["nodes"], lambda n: n["id"] == diagnostic["to"])
    assert related["kind"] == "volume-definitions"
    assert diagnostic["locations"][1] == related["location"]
    assert any(
        e["from"] == source["id"] and e["to"] == related["id"] and e["relation"] == "missing-volume" and e["status"] == "invalid"
        for e in report["edges"]
    )
    mount = find(report["nodes"], lambda n: n["kind"] == "mount" and n["location"]["pointer"] == pointer)
    assert mount  # Preserve enclosing declaration identity and metadata.
    assert mount["sourceLocation"] == source_location
    explanation = find(report["explanations"], lambda e: e["relation"] == "mount-target" and e["location"]["pointer"] == f"{pointer}/target")
    if explanation:
        assert explanation["status"] == "invalid"
        assert any(r["code"] == "missing-volume" for r in explanation["reasons"])
    result = find(to_sarif(report)["runs"][0]["results"], lambda r: r["ruleId"] == "missing-volume")
    assert result["locations"][0]["properties"]["jsonPointer"] == source_location["pointer"]
    assert result["locations"][0]["physicalLocation"]["region"] == {"startLine": source_location["line"], "startColumn": source_location["column"]}
    assert result["relatedLocations"][0]["properties"]["jsonPointer"] == "/volumes"
    return result


@pytest.mark.parametrize("kind", KINDS)
def test_undeclared_named_volume_points_json_explain_and_sarif_to_its_source_token(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind)
    # A same-named host file must not make a missing volume declaration valid.
    write(os.path.join(f.root, "absent"), "synthetic file, not a volume")
    json_run = f.run("--json", "--explain")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    result = missing_reference(report, f.source_location)
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == os.path.basename(f.document)
    assert result["relatedLocations"][0]["physicalLocation"]["artifactLocation"]["uri"] == os.path.basename(f.document)
    assert find(report["diagnostics"], lambda d: d["code"] == "missing-volume")["locations"][1] == f.at("/volumes", "{}")
    explain = f.run("--explain")
    assert explain.status == 1
    loc = f.source_location
    assert f"{loc['file']}:{loc['line']}:{loc['column']}#{loc['pointer']}" in explain.stdout, explain.stdout
    sarif = f.run("--sarif", "--strict")
    assert sarif.status == 1
    assert json.loads(sarif.stdout) == to_sarif(report)
    if kind == "snapshot":
        assert report["scope"]["composeInput"]["format"] == "pre-rendered-json"
        assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
        assert "override.yaml" not in js_stringify(to_sarif(report)["runs"][0]["results"])
    no_host_lookup(report, "absent")
    assert read(f.document) == f.text


@pytest.mark.parametrize("kind", KINDS)
def test_short_named_volume_references_retain_the_enclosing_scalar(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, string=True)
    report = check(f.options)
    assert report["status"] == "error"
    missing_reference(report, f.source_location)
    no_host_lookup(report, "absent")


@pytest.mark.parametrize("kind", KINDS)
def test_declared_and_anonymous_volumes_retain_unknown_contents_without_host_path_checks(mkdtemp: Any, kind: str) -> None:
    for args in [{"declared": True}, {"anonymous": True}]:
        f = Fixture(mkdtemp, kind, **args)
        report = check(f.options)
        assert report["status"] == "partial"
        assert [d["code"] for d in report["diagnostics"]] == ["workspace-content-unknown"]
        mount = find(report["nodes"], lambda n: n["kind"] == "mount")
        assert mount["sourceLocation"] == f.source_location
        assert any(e["from"] == mount["id"] and e["relation"] == "uses-volume" for e in report["edges"])
        no_host_lookup(report, "absent")
        assert f.run("--json").status == 0
        assert f.run("--json", "--strict").status == 1


@pytest.mark.parametrize("kind", KINDS)
def test_absent_definitions_stay_inferred_while_an_invalid_volume_name_is_rejected_at_source(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, omit_definitions=True)
    result = missing_reference(check(f.options), f.source_location)
    assert result["relatedLocations"][0]["properties"]["inferred"] is True
    assert "region" not in result["relatedLocations"][0]["physicalLocation"]
    invalid = Fixture(mkdtemp, kind, source="./not-a-name")
    report = check(invalid.options)
    assert find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")["locations"][0] == invalid.source_location
    assert not any(d["code"] == "missing-volume" for d in report["diagnostics"])
    no_host_lookup(report, "not-a-name")


def test_unselected_service_named_volume_errors_still_identify_that_service_source(mkdtemp: Any) -> None:
    f = Fixture(mkdtemp, "yaml")
    write(f.document, f.text.replace("  web:", "  web: {}\n  worker:", 1))
    report = check(f.options)
    source_location = {**f.source_location, "pointer": "/services/worker/volumes/0/source", "line": f.source_location["line"] + 1}
    missing_reference(report, source_location, "/services/worker/volumes/0")
    no_host_lookup(report, "absent")
