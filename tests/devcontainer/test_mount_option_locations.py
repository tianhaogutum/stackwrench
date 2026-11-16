"""Port of test/mount-option-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench.devcontainer import InputError, check, compose_mount, dev_mount, to_sarif

from .helpers import TMP, Result, entries, find, js_stringify, line_column, read, run_cli, write

ABSENT: Any = object()
KINDS = ["devcontainer", "compose", "snapshot"]


class Fixture:
    def __init__(self, mkdtemp: Any, kind: str, read_only: Any, changes: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.root = mkdtemp("wirecheck-mount-options-")
        self.config = os.path.join(self.root, "devcontainer.json")
        self.document = self.config if kind == "devcontainer" else os.path.join(self.root, "compose.yaml" if kind == "compose" else "final.json")
        self.pointer = "/mounts/0" if kind == "devcontainer" else "/services/web/volumes/0"
        mount: dict[str, Any] = {"type": "bind", "source": ".", "target": "/work"}
        if read_only is not ABSENT:
            mount["read_only"] = read_only
        mount.update(changes or {})
        if mount["type"] != "bind":
            del mount["source"]
        model = (
            {"image": "synthetic:example", "workspaceFolder": "/work", "mounts": [mount]}
            if kind == "devcontainer"
            else {"services": {"web": {"volumes": [mount]}}}
        )
        # JSONC comments also ensure coordinates refer to the actual source document.
        if kind == "compose":
            self.text = "services:\n  web:\n    volumes:\n      - " + "\n        ".join(f"{k}: {js_stringify(v)}" for k, v in entries(mount)) + "\n"
        else:
            self.text = ("// synthetic mount options\n" if kind == "devcontainer" else "") + js_stringify(model, 2) + "\n"
        if kind != "devcontainer":
            write(self.config, js_stringify({"dockerComposeFile": "compose.yaml", "service": "web", "workspaceFolder": "/work"}))
        if kind == "snapshot":
            write(os.path.join(self.root, "compose.yaml"), "not parsed: [")
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if kind == "snapshot":
            self.options["composeJson"] = self.document

    def at(self, field: str) -> dict[str, Any]:
        token = f"{field if self.kind == 'compose' else js_stringify(field)}: "
        assert token in self.text
        offset = self.text.find(token) + len(token)
        return {"file": self.document, "pointer": f"{self.pointer}/{field}", **line_column(self.text, offset)}

    def run(self, *args: str) -> Result:
        r = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.kind == "snapshot" else []), *args],
            cwd=TMP,
        )
        assert r.stderr == ""
        return r


def check_cli_failure(f: Fixture, kind: str, field: str, code: str) -> dict[str, Any]:
    json_run = f.run("--json", "--explain")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    diagnostic = find(report["diagnostics"], lambda d: d["code"] == code)
    assert diagnostic, js_stringify(report["diagnostics"])
    expected = f.at(field)
    assert diagnostic["locations"][0] == expected
    assert diagnostic["from"] == f"{f.document}#{expected['pointer']}"
    assert not any(e["relation"].endswith("bind-source") for e in report["explanations"])
    assert not any(n["kind"] == "host-path" and n["label"] == os.path.join(f.root, "absent") for n in report["nodes"])
    assert not any(d["code"] in ("invalid-container-path", "missing-host-path", "missing-volume") for d in report["diagnostics"])
    explanation = find(report["explanations"], lambda e: e["relation"] == "mount-target")
    assert explanation["status"] == "invalid"
    assert any(r["code"] == code for r in explanation["reasons"])
    assert find(report["nodes"], lambda n: n["kind"] == "mount")["location"]["pointer"] == f.pointer
    text = f.run("--explain")
    assert text.status == 1
    assert f"{expected['file']}:{expected['line']}:{expected['column']}#{expected['pointer']}" in text.stdout
    sarif = f.run("--sarif")
    assert sarif.status == 1
    converted = json.loads(sarif.stdout)
    assert converted == to_sarif(report)
    primary = find(converted["runs"][0]["results"], lambda r: r["ruleId"] == code)["locations"][0]
    assert primary["properties"]["jsonPointer"] == expected["pointer"]
    assert not primary["properties"].get("inferred")
    assert primary["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
    if kind == "snapshot":
        assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
        assert "compose.yaml" not in js_stringify(converted["runs"][0]["results"])
    assert read(f.document) == f.text
    return diagnostic


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_read_only_uses_the_exact_option_token_before_path_checks(mkdtemp: Any, kind: str) -> None:
    invalid = ["invalid", 42, None, {}, [], "", *([] if kind == "devcontainer" else ["true", "false"])]
    for read_only in invalid:
        f = Fixture(mkdtemp, kind, read_only, {"source": "./absent", "target": "relative"})
        code = "invalid-field" if read_only == "" else "invalid-mount"
        diagnostic = check_cli_failure(f, kind, "read_only", code)
        if code == "invalid-mount":
            assert diagnostic["message"] == ("Invalid read_only value" if kind == "devcontainer" else "read_only must be boolean")


@pytest.mark.parametrize("kind", KINDS)
def test_accepted_and_omitted_read_only_retain_mount_verdicts_and_path_provenance(mkdtemp: Any, kind: str) -> None:
    accepted = [ABSENT, True, False, *(["true", "false"] if kind == "devcontainer" else [])]
    for read_only in accepted:
        for mount_type in ["bind", "volume", "tmpfs"]:
            f = Fixture(mkdtemp, kind, read_only, {"type": mount_type})
            report = check(f.options)
            assert report["status"] == ("ok" if mount_type == "bind" else "partial")
            assert [d["code"] for d in report["diagnostics"]] == ([] if mount_type == "bind" else ["workspace-content-unknown"])
            mount = find(report["nodes"], lambda n: n["kind"] == "mount")
            assert mount["mount"]["type"] == mount_type
            assert mount["sourceLocation"] == (f.at("source") if mount_type == "bind" else None)
            assert mount["targetLocation"] == f.at("target")
            assert find(report["explanations"], lambda e: e["relation"] == "mount-target")["location"] == f.at("target")
            if mount_type == "bind":
                assert find(report["explanations"], lambda e: e["relation"].endswith("bind-source"))["location"] == f.at("source")
            json_run = f.run("--json")
            assert json_run.status == 0
            assert json.loads(json_run.stdout) == report
            assert f.run("--json", "--strict").status == (0 if mount_type == "bind" else 1)


def test_read_only_string_options_keep_scalar_provenance_aliases_and_format_specific_rules(mkdtemp: Any) -> None:
    for field in ["mounts", "workspaceMount"]:
        for option in ["read_only", "readonly"]:
            for value in ["invalid", "true", "false", None]:
                f = Fixture(mkdtemp, "devcontainer", ABSENT)
                scalar = f"type=bind,source=.,target=/work,{option}{'' if value is None else '=' + value}"
                text = js_stringify({"image": "synthetic:example", "workspaceFolder": "/work", field: [scalar] if field == "mounts" else scalar}, 2)
                write(f.config, text)
                report = check(f.options)
                assert report["status"] == ("error" if value == "invalid" else "ok")
                if value == "invalid":
                    expected = {
                        "file": f.config,
                        "pointer": "/mounts/0" if field == "mounts" else "/workspaceMount",
                        **line_column(text, text.find(js_stringify(scalar))),
                    }
                    assert find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")["locations"][0] == expected
                    primary = find(to_sarif(report)["runs"][0]["results"], lambda r: r["ruleId"] == "invalid-mount")["locations"][0]
                    assert primary["properties"]["jsonPointer"] == expected["pointer"]
                    assert primary["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
    for mode in ["invalid", "ro,rw", "ro", "rw"]:
        f = Fixture(mkdtemp, "compose", ABSENT)
        scalar = f".:/work:{mode}"
        write(f.document, f'services:\n  web:\n    volumes:\n      - "{scalar}"\n')
        report = check(f.options)
        assert report["status"] == ("ok" if mode in ("ro", "rw") else "error")
        if report["status"] == "error":
            assert find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")["locations"][0] == {
                "file": f.document,
                "pointer": f.pointer,
                "line": 4,
                "column": 9,
            }


def test_standalone_mount_parsers_retain_enclosing_location_fallback_without_field_metadata() -> None:
    location = {"file": "synthetic.json", "pointer": "/mounts/0", "line": 5, "column": 5}
    for parse in [dev_mount, compose_mount]:
        with pytest.raises(InputError) as caught:
            parse({"type": "bind", "source": ".", "target": "/work", "read_only": 42}, location)
        assert caught.value.code == "invalid-mount"
        assert caught.value.location == location


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_consistency_uses_its_exact_option_token_before_path_checks(mkdtemp: Any, kind: str) -> None:
    for consistency in ["invalid", 42, True, False, None, {}, [], ""]:
        f = Fixture(mkdtemp, kind, ABSENT, {"consistency": consistency, "source": "./absent", "target": "relative"})
        code = "invalid-field" if consistency == "" else "invalid-mount"
        diagnostic = check_cli_failure(f, kind, "consistency", code)
        if code == "invalid-mount":
            assert diagnostic["message"] == "Unsupported consistency value"


@pytest.mark.parametrize("kind", KINDS)
def test_accepted_and_omitted_consistency_retain_verdicts_and_path_provenance(mkdtemp: Any, kind: str) -> None:
    for consistency in [ABSENT, "consistent", "cached", "delegated"]:
        for mount_type in ["bind", "volume", "tmpfs"]:
            f = Fixture(mkdtemp, kind, ABSENT, {"type": mount_type, **({} if consistency is ABSENT else {"consistency": consistency})})
            report = check(f.options)
            assert report["status"] == ("ok" if mount_type == "bind" else "partial")
            assert [d["code"] for d in report["diagnostics"]] == ([] if mount_type == "bind" else ["workspace-content-unknown"])
            mount = find(report["nodes"], lambda n: n["kind"] == "mount")
            assert mount["mount"]["type"] == mount_type
            assert mount["sourceLocation"] == (f.at("source") if mount_type == "bind" else None)
            assert mount["targetLocation"] == f.at("target")
            assert find(report["explanations"], lambda e: e["relation"] == "mount-target")["location"] == f.at("target")
            if mount_type == "bind":
                assert find(report["explanations"], lambda e: e["relation"].endswith("bind-source"))["location"] == f.at("source")
            json_run = f.run("--json")
            assert json_run.status == 0
            assert json.loads(json_run.stdout) == report
            assert f.run("--json", "--strict").status == (0 if mount_type == "bind" else 1)


def test_consistency_string_options_retain_scalar_provenance_and_accepted_flags(mkdtemp: Any) -> None:
    for kind in KINDS:
        for field in ["mounts", "workspaceMount"] if kind == "devcontainer" else ["volumes"]:
            for consistency in ["invalid", "", "consistent", "cached", "delegated"]:
                f = Fixture(mkdtemp, kind, ABSENT)
                scalar = f"type=bind,source=.,target=/work,consistency={consistency}" if kind == "devcontainer" else f".:/work:{consistency}"
                model = (
                    {"image": "synthetic:example", "workspaceFolder": "/work", field: [scalar] if field == "mounts" else scalar}
                    if kind == "devcontainer"
                    else {"services": {"web": {"volumes": [scalar]}}}
                )
                text = f'services:\n  web:\n    volumes:\n      - "{scalar}"\n' if kind == "compose" else js_stringify(model, 2)
                write(f.document, text)
                report = check(f.options)
                invalid = consistency in ("invalid", "")
                assert report["status"] == ("error" if invalid else "ok")
                if invalid:
                    expected = {
                        "file": f.document,
                        "pointer": "/workspaceMount" if field == "workspaceMount" else f.pointer,
                        **line_column(text, text.find(js_stringify(scalar))),
                    }
                    assert find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")["locations"][0] == expected
                    primary = find(to_sarif(report)["runs"][0]["results"], lambda r: r["ruleId"] == "invalid-mount")["locations"][0]
                    assert primary["properties"]["jsonPointer"] == expected["pointer"]
                    assert primary["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
                    explained = f.run("--explain")
                    assert explained.status == 1
                    assert f"{expected['file']}:{expected['line']}:{expected['column']}#{expected['pointer']}" in explained.stdout
                    sarif = f.run("--sarif")
                    assert sarif.status == 1
                    assert json.loads(sarif.stdout) == to_sarif(report)
                else:
                    assert f.run("--json", "--strict").status == 0


def test_consistency_standalone_parsers_retain_enclosing_fallback_with_absent_or_unrelated_metadata() -> None:
    location = {"file": "synthetic.json", "pointer": "/mounts/0", "line": 5, "column": 5}
    for parse in [dev_mount, compose_mount]:
        for fields in [None, {"target": {**location, "pointer": "/mounts/0/target"}}]:
            with pytest.raises(InputError) as caught:
                parse({"type": "bind", "source": ".", "target": "/work", "consistency": 42}, location, fields)
            assert caught.value.code == "invalid-mount"
            assert caught.value.message == "Unsupported consistency value"
            assert caught.value.location == location


def test_consistency_validation_keeps_earlier_read_only_rejection_precedence(mkdtemp: Any) -> None:
    for kind in KINDS:
        f = Fixture(mkdtemp, kind, 42, {"consistency": 42})
        report = check(f.options)
        diagnostic = find(report["diagnostics"], lambda d: d["code"] == "invalid-mount")
        assert diagnostic["locations"][0] == f.at("read_only")
        assert diagnostic["message"] == ("Invalid read_only value" if kind == "devcontainer" else "read_only must be boolean")
        assert not any(e["relation"].endswith("bind-source") for e in report["explanations"])
