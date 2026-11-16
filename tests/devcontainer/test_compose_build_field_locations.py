"""Port of test/compose-build-field-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from stackwrench.devcontainer import check, to_sarif

from .helpers import TMP, Result, assert_valid_sarif, find, js_stringify, line_column, run_cli, write, yaml_stringify

KINDS = ["yaml", "json", "snapshot"]
MESSAGES = {
    "dockerfile_inline": "dockerfile_inline is not evaluated; use a local Dockerfile",
    "additional_contexts": "additional_contexts is not evaluated",
}


def escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


class Fixture:
    def __init__(
        self,
        mkdtemp: Any,
        kind: str,
        build: Any,
        name: str = "web",
        extra: dict[str, Any] | None = None,
        later: dict[str, Any] | None = None,
        yaml: str | None = None,
    ) -> None:
        self.kind = kind
        self.name = name
        self.root = mkdtemp("wirecheck-build-fields-")
        self.config = os.path.join(self.root, "devcontainer.json")
        self.document = os.path.join(self.root, {"yaml": "compose.yaml", "json": "compose.json"}.get(kind, "final.json"))
        model = {"services": {name: {"build": build, "volumes": [".:/work"], **(extra or {})}, **(later or {})}}
        if kind == "yaml":
            self.text = yaml if yaml is not None else yaml_stringify(model)
        else:
            self.text = js_stringify(model, 2) + "\n"
        write(
            self.config,
            js_stringify(
                {"dockerComposeFile": "compose.json" if kind == "json" else "compose.yaml", "service": name, "workspaceFolder": "/work"}
            ),
        )
        if kind == "snapshot":
            write(os.path.join(self.root, "compose.yaml"), "deliberately unparsed: [")
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if kind == "snapshot":
            self.options["composeJson"] = self.document
        self.inputs = [self.config, self.document, *([os.path.join(self.root, "compose.yaml")] if kind == "snapshot" else [])]
        self.before = [Path(p).read_bytes() for p in self.inputs]

    def run(self, *flags: str) -> Result:
        result = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.kind == "snapshot" else []), *flags],
            cwd=TMP,
        )
        assert result.stderr == ""
        return result

    # Locate the literal value in the input, independently of the production parser.
    def at_field(self, key: str) -> dict[str, Any]:
        token = f"{key}:" if self.kind == "yaml" else f'"{key}":'
        end = self.text.find(token) + len(token)
        offset = end + len(re.match(r"\s*", self.text[end:]).group(0))
        return {"file": self.document, "pointer": f"/services/{escape(self.name)}/build/{key}", **line_column(self.text, offset)}

    def unchanged(self) -> None:
        for path, before in zip(self.inputs, self.before):
            assert Path(path).read_bytes() == before


def assert_failure(f: Fixture, key: str, codes: list[str] | None = None) -> dict[str, Any]:
    codes = codes or ["unsupported-build"]
    json_run = f.run("--json", "--explain")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    assert [d["code"] for d in report["diagnostics"]] == codes
    diagnostic = find(report["diagnostics"], lambda d: d["code"] == "unsupported-build")
    assert diagnostic["message"] == MESSAGES[key]
    assert diagnostic["severity"] == "error"
    expected = f.at_field(key)
    assert diagnostic["locations"] == [expected]
    assert diagnostic["from"] == f"{f.document}#{expected['pointer']}"
    assert diagnostic["to"] is None
    assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"]) == {
        "id": diagnostic["from"],
        "kind": "field",
        "label": expected["pointer"],
        "location": expected,
    }
    assert any(n["kind"] == "service" and n["label"] == f.name for n in report["nodes"])
    assert any(n["kind"] == "build" and n["id"] == f"{f.document}#/services/{escape(f.name)}/build" for n in report["nodes"])
    assert any(e["relation"] == "workspace-covered" for e in report["edges"])
    assert any(e["relation"] == "compose-build-context" for e in report["explanations"])
    assert not any(e["relation"] == "compose-dockerfile" for e in report["explanations"])
    assert not any(e["relation"] == "unsupported-build" for e in report["edges"])
    # Rejected values stay outside the evaluated subset; their contents are not walked as paths.
    assert any(u["location"]["pointer"] == expected["pointer"] for u in report["unevaluatedFields"])
    text = f.run("--explain")
    assert text.status == 1
    assert f"error [unsupported-build] {f.document}:{expected['line']}:{expected['column']}#{expected['pointer']}: {MESSAGES[key]}" in text.stdout
    sarif_run = f.run("--sarif", "--explain")
    assert sarif_run.status == 1
    sarif = json.loads(sarif_run.stdout)
    assert_valid_sarif(sarif)
    assert sarif == to_sarif(report)
    result = find(sarif["runs"][0]["results"], lambda r: r["ruleId"] == "unsupported-build")
    assert len(result["locations"]) == 1
    assert result["locations"][0]["properties"]["jsonPointer"] == expected["pointer"]
    assert not result["locations"][0]["properties"].get("inferred")
    assert result["locations"][0]["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
    strict = f.run("--json", "--strict")
    assert strict.status == 1
    assert json.loads(strict.stdout) == report
    if f.document.endswith("final.json"):
        assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
        assert "compose.yaml" not in js_stringify(sarif["runs"][0]["results"])
    f.unchanged()
    return report


@pytest.mark.parametrize("key", list(MESSAGES))
@pytest.mark.parametrize("kind", KINDS)
def test_supplied_values_are_rejected_at_their_own_tokens(mkdtemp: Any, kind: str, key: str) -> None:
    for value in [None, False, True, 0, "", "FROM scratch", "${HOST}", [], ["../outside"], {}, {"arbitrary": "${HOST}"}]:
        f = Fixture(mkdtemp, kind, {"context": ".", key: value}, name="web/~")
        report = assert_failure(f, key)
        assert len([n for n in report["nodes"] if n["kind"] == "host-path"]) == 2  # raw Compose and context only


@pytest.mark.parametrize("kind", KINDS)
def test_context_errors_precede_inline_rejection_which_precedes_additional_contexts(mkdtemp: Any, kind: str) -> None:
    for context in [None, "${HOST}", "./missing"]:
        f = Fixture(
            mkdtemp,
            kind,
            {"context": context, "additional_contexts": {"extra": "../outside"}, "dockerfile_inline": "FROM scratch", "dockerfile": "missing"},
        )
        code = "invalid-field" if context is None else "unsupported-substitution" if context == "${HOST}" else "missing-host-path"
        report = assert_failure(f, "dockerfile_inline", [code, "unsupported-build"])
        assert report["diagnostics"][0]["locations"][0]["pointer"] == "/services/web/build/context"
        assert not any(n["location"]["pointer"] == "/services/web/build/additional_contexts" for n in report["nodes"])


@pytest.mark.parametrize("kind", KINDS)
def test_build_field_rejection_preserves_mounts_dependencies_and_later_service_traversal(mkdtemp: Any, kind: str) -> None:
    f = Fixture(
        mkdtemp,
        kind,
        {"additional_contexts": []},
        extra={
            "volumes": [None, {"type": "volume", "target": "/other", "volume": {"nocopy": 42}}, ".:/work"],
            "depends_on": ["missing", "later"],
        },
        later={
            "later": {"profiles": ["excluded"], "build": {"dockerfile_inline": False}, "volumes": [".:/later"], "depends_on": ["web"]},
            "last": {"build": False, "volumes": False, "depends_on": [42, "web"]},
        },
    )
    report = assert_failure(
        f,
        "additional_contexts",
        ["unsupported-build", "invalid-mount", "invalid-mount", "missing-service", "unsupported-build", "invalid-build", "invalid-mount", "invalid-field"],
    )
    assert [d["locations"][0]["pointer"] for d in report["diagnostics"]] == [
        "/services/web/build/additional_contexts", "/services/web/volumes/0", "/services/web/volumes/1/volume/nocopy",
        "/services/web/depends_on/0", "/services/later/build/dockerfile_inline", "/services/last/build",
        "/services/last/volumes", "/services/last/depends_on/0",
    ]
    assert len([e for e in report["edges"] if e["relation"] == "depends-on" and e["status"] == "resolved"]) == 3
    assert any(n["kind"] == "mount" and n["location"]["pointer"] == "/services/later/volumes/0" for n in report["nodes"])
    mount = find(report["nodes"], lambda n: (n.get("mount") or {}).get("target") == "/work")
    assert mount["sourceLocation"] == mount["location"]
    assert mount["targetLocation"] == mount["location"]


def test_yaml_quoted_inline_dockerfile_block_scalar_and_flow_collection_retain_literal_token_starts(mkdtemp: Any) -> None:
    for key, value, coords in [
        ("dockerfile_inline", '"FROM scratch"', [5, 26]),
        ("dockerfile_inline", "|-\n        FROM scratch", [5, 26]),
        ("additional_contexts", "[]", [5, 28]),
        ("additional_contexts", '{extra: "../outside"}', [5, 28]),
    ]:
        f = Fixture(
            mkdtemp,
            "yaml",
            {},
            yaml=f'services:\n  web:\n    build:\n      context: .\n      {key}: {value}\n    volumes: [".:/work"]\n',
        )
        assert_failure(f, key)
        assert [f.at_field(key)["line"], f.at_field(key)["column"]] == coords


def test_dev_container_unsupported_object_build_fields_retain_document_evidence_and_mounts(mkdtemp: Any) -> None:
    for key in MESSAGES:
        f = Fixture(mkdtemp, "yaml", {})
        write(f.config, js_stringify({"build": {key: None}, "workspaceFolder": "/work", "mounts": ["type=bind,source=.,target=/work"]}))
        run = f.run("--json")
        assert run.status == 1
        report = json.loads(run.stdout)
        assert report == check(f.options)
        assert [[d["code"], d["message"], [l["pointer"] for l in d["locations"]]] for d in report["diagnostics"]] == [
            ["unsupported-build", MESSAGES[key], ["/", f"/build/{key}"]]
        ]
        assert report["diagnostics"][0]["from"] == f"{f.config}#/"
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])
