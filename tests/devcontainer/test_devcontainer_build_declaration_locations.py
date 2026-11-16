"""Port of test/devcontainer-build-declaration-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench.devcontainer import check, to_sarif

from .helpers import TMP, UNDEFINED, Result, assert_valid_sarif, find, js_stringify, line_column, read, run_cli, write

MESSAGE = "build must be an object (or a Compose context string)"


class Fixture:
    def __init__(self, mkdtemp: Any, value: Any, extra: dict[str, Any] | None = None, text: str | None = None) -> None:
        self.root = mkdtemp("wirecheck-dev-build-")
        os.mkdir(os.path.join(self.root, "cfg"))
        os.mkdir(os.path.join(self.root, "src"))
        write(os.path.join(self.root, "cfg/Dockerfile"), "deliberately not parsed\n")
        self.config = os.path.join(self.root, "cfg/devcontainer.json")
        self.input = (
            text
            if text is not None
            else js_stringify(
                {"build": value, "workspaceFolder": "/work", "mounts": ["type=bind,source=.,target=/work"], **(extra or {})}, 2
            )
            + "\n"
        )
        write(self.config, self.input)
        self.options = {"config": self.config, "workspace": self.root}

    def run(self, *flags: str) -> Result:
        r = run_cli(["--config", self.config, "--workspace", self.root, *flags], cwd=TMP)
        assert r.stderr == ""
        return r

    # Match the literal token independently of production source mapping.
    def at_build(self) -> dict[str, Any]:
        end = self.input.find(":", self.input.find('"build"')) + 1
        skipped = re.match(r"(?:\s|/\*[\s\S]*?\*/|//[^\r\n]*(?:\r?\n|\Z))*", self.input[end:]).group(0)
        return {"file": self.config, "pointer": "/build", **line_column(self.input, end + len(skipped))}

    def unchanged(self) -> None:
        assert read(self.config) == self.input


def assert_failure(f: Fixture, codes: list[str] | None = None) -> dict[str, Any]:
    codes = codes or ["invalid-build"]
    json_run = f.run("--json", "--explain")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    assert [d["code"] for d in report["diagnostics"]] == codes
    diagnostic, expected = report["diagnostics"][0], f.at_build()
    assert diagnostic["message"] == MESSAGE
    assert diagnostic["severity"] == "error"
    assert diagnostic["locations"] == [expected]
    assert diagnostic["from"] == f"{f.config}#/build"
    assert diagnostic["to"] is None
    assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"]) == {
        "id": diagnostic["from"],
        "kind": "build",
        "label": "/build",
        "location": expected,
    }
    assert any(n["id"] == f"{f.config}#/" and n["kind"] == "document" for n in report["nodes"])
    assert not any(e["relation"] in ("build-context", "dockerfile") for e in report["explanations"])
    assert not any(n["location"]["pointer"].startswith("/build/") for n in report["nodes"])
    assert any(e["relation"] == "mount-target" for e in report["explanations"])
    assert any(e["relation"] == "workspace-coverage" for e in report["explanations"])
    assert not any(e["relation"] == "invalid-build" for e in report["edges"])
    text = f.run("--explain")
    assert text.status == 1
    assert f"error [invalid-build] {f.config}:{expected['line']}:{expected['column']}#/build: {MESSAGE}" in text.stdout
    sarif_run = f.run("--sarif", "--explain")
    assert sarif_run.status == 1
    sarif = json.loads(sarif_run.stdout)
    assert_valid_sarif(sarif)
    assert sarif == to_sarif(report)
    result = sarif["runs"][0]["results"][0]
    assert len(result["locations"]) == 1
    assert result["locations"][0]["properties"] == {"jsonPointer": "/build"}
    assert result["locations"][0]["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
    assert not result.get("relatedLocations")
    strict = f.run("--json", "--strict")
    assert strict.status == 1
    assert json.loads(strict.stdout) == report
    f.unchanged()
    return report


SHAPES = {
    "null": [None],
    "booleans": [True, False],
    "numbers": [0, 42, -1.5],
    "arrays": [[], ["${HOST}"], [{"context": "../outside", "dockerfile": "${HOST}"}]],
    "strings": ["", ".", "${HOST}", "\0"],
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_dev_container_rejected_builds_identify_their_supplied_value_tokens(mkdtemp: Any, shape: str) -> None:
    for value in SHAPES[shape]:
        f = Fixture(mkdtemp, value)
        assert [f.at_build()["line"], f.at_build()["column"]] == [2, 12]
        report = assert_failure(f)
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
        assert len([n for n in report["nodes"] if n["kind"] == "host-path"]) == 1  # only independent bind


def test_jsonc_comments_trailing_commas_crlf_and_multiline_values_retain_actual_build_token_coordinates(mkdtemp: Any) -> None:
    for text in [
        '{\n  "build": /* value follows */ null,\n  "workspaceFolder": "/work",\n  "mounts": ["type=bind,source=.,target=/work"],\n}\n',
        '{\r\n  "build": // comment\r\n    [\r\n      "${HOST}",\r\n    ],\r\n  "workspaceFolder": "/work",\r\n  "mounts": ["type=bind,source=.,target=/work"],\r\n}\r\n',
    ]:
        assert_failure(Fixture(mkdtemp, None, text=text))


def test_rejected_build_still_traverses_failed_mounts_later_mounts_and_mapped_workspace_paths(mkdtemp: Any) -> None:
    report = assert_failure(
        Fixture(
            mkdtemp,
            False,
            extra={
                "mounts": [None, {"type": "bind", "source": "./missing", "target": "/other"}, "type=bind,source=.,target=/work"],
                "workspaceFolder": "/work/absent",
            },
        ),
        ["invalid-build", "invalid-mount", "missing-host-path", "missing-host-path"],
    )
    assert [d["locations"][0]["pointer"] for d in report["diagnostics"]] == ["/build", "/mounts/0", "/mounts/1/source", "/workspaceFolder"]
    assert any(e["relation"] == "workspace-covered" for e in report["edges"])
    assert any(n["kind"] == "mount" and n["location"]["pointer"] == "/mounts/2" for n in report["nodes"])


def test_required_dockerfile_and_unsupported_object_build_fields_retain_document_primary_evidence_and_order(mkdtemp: Any) -> None:
    for value, code, detail, pointers in [
        ({}, "invalid-build", "build.dockerfile is required in Dev Container Dockerfile mode", ["/", "/build"]),
        (
            {"additional_contexts": [], "dockerfile_inline": None},
            "unsupported-build",
            "dockerfile_inline is not evaluated; use a local Dockerfile",
            ["/", "/build/dockerfile_inline"],
        ),
        ({"additional_contexts": False}, "unsupported-build", "additional_contexts is not evaluated", ["/", "/build/additional_contexts"]),
    ]:
        f = Fixture(mkdtemp, value)
        r = f.run("--json", "--strict")
        report = json.loads(r.stdout)
        assert r.status == 1
        assert [[d["code"], d["message"], [l["pointer"] for l in d["locations"]]] for d in report["diagnostics"]] == [[code, detail, pointers]]
        assert report["diagnostics"][0]["from"] == f"{f.config}#/"
        assert any(e["relation"] == "build-context" for e in report["explanations"])
        assert not any(e["relation"] == "dockerfile" for e in report["explanations"])
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
        f.unchanged()


def test_object_context_dockerfile_errors_keep_independent_host_field_evidence(mkdtemp: Any) -> None:
    for value, codes, pointers in [
        ({"context": None, "dockerfile": "missing"}, ["invalid-field", "missing-host-path"], ["/build/context", "/build/dockerfile"]),
        ({"context": "../missing", "dockerfile": "missing"}, ["missing-host-path", "missing-host-path"], ["/build/context", "/build/dockerfile"]),
    ]:
        f = Fixture(mkdtemp, value)
        r = f.run("--json")
        report = json.loads(r.stdout)
        assert r.status == 1
        assert [d["code"] for d in report["diagnostics"]] == codes
        assert [d["locations"][0]["pointer"] for d in report["diagnostics"]] == pointers
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
        f.unchanged()


def test_accepted_object_builds_image_without_build_and_legacy_dockerfile_retain_config_relative_paths(mkdtemp: Any) -> None:
    for extra in [
        {"build": {"dockerfile": "Dockerfile"}},
        {"build": {"context": "../src", "dockerfile": "Dockerfile"}},
        {"build": UNDEFINED, "image": "synthetic:example"},
        {"build": UNDEFINED, "dockerFile": "Dockerfile", "context": "../src"},
    ]:
        f = Fixture(mkdtemp, UNDEFINED, extra=extra)
        r = f.run("--json", "--strict")
        report = json.loads(r.stdout)
        assert r.status == 0
        assert report["diagnostics"] == []
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
        if "image" not in extra:
            target = f"host:{os.path.join(f.root, 'cfg/Dockerfile')}"
            assert any(e["relation"] == "dockerfile" and e["to"] == target for e in report["edges"])
        build = extra.get("build")
        if (isinstance(build, dict) and build.get("context")) or extra.get("context"):
            target = f"host:{os.path.join(f.root, 'src')}"
            assert any(e["relation"] == "build-context" and e["to"] == target for e in report["edges"])
        f.unchanged()


def test_mode_conflicts_and_misplaced_legacy_context_still_preempt_rejected_build_validation(mkdtemp: Any) -> None:
    for extra, code in [({"image": "synthetic:example"}, "invalid-mode"), ({"dockerFile": "Dockerfile"}, "invalid-mode"), ({"context": "."}, "invalid-mode-field")]:
        f = Fixture(mkdtemp, None, extra=extra)
        r = f.run("--json")
        report = json.loads(r.stdout)
        assert r.status == 1
        assert [d["code"] for d in report["diagnostics"]] == [code]
        assert not any(n["kind"] in ("build", "mount") for n in report["nodes"])
        f.unchanged()
