"""Port of test/depends-on.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench.devcontainer import check, to_sarif

from .helpers import TMP, Result, assert_valid_sarif, find, js_stringify, line_column, read, run_cli, write, yaml_stringify

ABSENT: Any = object()
KINDS = ["yaml", "snapshot"]


class Fixture:
    def __init__(self, mkdtemp: Any, kind: str, dependencies: Any, extra: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.root = mkdtemp("wirecheck-depends-")
        self.config = os.path.join(self.root, "devcontainer.json")
        self.document = os.path.join(self.root, "compose.yaml" if kind == "yaml" else "final.json")
        web: dict[str, Any] = {"volumes": [".:/work"]}
        if dependencies is not ABSENT:
            web["depends_on"] = dependencies
        self.model = {"services": {"web": web, "db": {}, **(extra or {})}}
        self.text = yaml_stringify(self.model) if kind == "yaml" else js_stringify(self.model, 2) + "\n"
        write(
            self.config,
            js_stringify({"dockerComposeFile": "compose.yaml", "service": "web", "runServices": ["web"], "workspaceFolder": "/work"}),
        )
        if kind == "snapshot":
            write(os.path.join(self.root, "compose.yaml"), "not parsed: [")
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if kind == "snapshot":
            self.options["composeJson"] = self.document

    def run(self, *args: str) -> Result:
        result = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.kind == "snapshot" else []), *args],
            cwd=TMP,
        )
        assert result.stderr == ""
        return result

    # Expected coordinates come from literal fixture tokens, independently of parsers.
    def at(self, pointer: str, token: str, occurrence: int = 0) -> dict[str, Any]:
        offset = -1
        for _ in range(occurrence + 1):
            offset = self.text.find(token, offset + 1)
        assert offset >= 0, token
        return {"file": self.document, "pointer": pointer, **line_column(self.text, offset)}


@pytest.mark.parametrize("kind", KINDS)
def test_declared_dependency_edges_and_missing_references_share_exact_cli_evidence(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, ["db", "missing"])
    r = f.run("--json", "--explain")
    assert r.status == 1
    report = json.loads(r.stdout)
    assert report == check(f.options)
    db = f.at("/services/web/depends_on/0", "db\n" if kind == "yaml" else '"db"')
    missing = f.at("/services/web/depends_on/1", "missing\n" if kind == "yaml" else '"missing"')
    services = f.at("/services", "web:" if kind == "yaml" else "{", 0 if kind == "yaml" else 1)
    assert [d["code"] for d in report["diagnostics"]] == ["missing-service"]
    diagnostic = report["diagnostics"][0]
    assert diagnostic["locations"] == [missing, services]
    assert diagnostic["from"] == f"{f.document}#{missing['pointer']}"
    assert diagnostic["to"] == f"{f.document}#/services"
    edges = [e for e in report["edges"] if e["relation"] == "depends-on"]
    assert edges == [
        {"from": f"{f.document}#/services/web", "to": f"{f.document}#/services/db", "relation": "depends-on", "severity": "info", "status": "resolved", "location": db},
        {"from": f"{f.document}#/services/web", "to": f"{f.document}#/services", "relation": "depends-on", "severity": "error", "status": "invalid", "location": missing},
    ]
    explanations = [e for e in report["explanations"] if e["relation"] == "depends-on"]
    assert [[e["location"], e["service"], e["status"], e["base"]] for e in explanations] == [
        [db, "web", "resolved", None],
        [missing, "web", "invalid", None],
    ]
    assert explanations[0]["targetLocation"]["pointer"] == "/services/db"
    assert explanations[1]["targetLocation"] == services
    assert explanations[1]["reasons"][0]["code"] == "missing-service"
    assert not any("depends_on" in u["location"]["pointer"] for u in report["unevaluatedFields"])
    explained = f.run("--explain")
    assert explained.status == 1
    for location in [db, missing, services]:
        assert f"{location['file']}:{location['line']}:{location['column']}#{location['pointer']}" in explained.stdout
    assert re.search(r"invalid \[depends-on\]", explained.stdout)
    for strict in [[], ["--strict"]]:
        sarif_run = f.run("--sarif", *strict)
        assert sarif_run.status == 1
        sarif = json.loads(sarif_run.stdout)
        assert_valid_sarif(sarif)
        assert sarif == to_sarif(report)
        result = sarif["runs"][0]["results"][0]
        assert result["locations"][0]["physicalLocation"]["region"] == {"startLine": missing["line"], "startColumn": missing["column"]}
        assert result["locations"][0]["properties"]["jsonPointer"] == missing["pointer"]
        assert result["relatedLocations"][0]["physicalLocation"]["region"] == {"startLine": services["line"], "startColumn": services["column"]}
    if kind == "snapshot":
        assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
        assert "compose.yaml" not in js_stringify(report["diagnostics"])
    assert read(f.document) == f.text


@pytest.mark.parametrize("kind", KINDS)
def test_empty_absent_repeated_self_and_cyclic_declarations_do_not_imply_activation(mkdtemp: Any, kind: str) -> None:
    for dependencies in [ABSENT, [], ["db"], ["db", "db", "web"]]:
        f = Fixture(mkdtemp, kind, dependencies, {"db": {"profiles": ["excluded"], "depends_on": ["web"]}, "worker": {"depends_on": ["db"]}})
        report = check(f.options)
        assert report["status"] == "ok"
        assert report["diagnostics"] == []
        edges = [e for e in report["edges"] if e["relation"] == "depends-on"]
        expected = [["/services/web", f"/services/{name}", f"/services/web/depends_on/{i}"] for i, name in enumerate(dependencies)] if isinstance(dependencies, list) else []
        assert [[e["from"].split("#")[1], e["to"].split("#")[1], e["location"]["pointer"]] for e in edges] == [
            *expected,
            ["/services/db", "/services/web", "/services/db/depends_on/0"],
            ["/services/worker", "/services/db", "/services/worker/depends_on/0"],
        ]
        assert all(e["status"] == "resolved" for e in edges)
        if isinstance(dependencies, list) and not dependencies:
            assert find(report["explanations"], lambda e: e["location"]["pointer"] == "/services/web/depends_on")["status"] == "resolved"
        cli_report = f.run("--json", "--strict")
        assert cli_report.status == 0
        assert json.loads(cli_report.stdout) == report


@pytest.mark.parametrize("kind", KINDS)
def test_malformed_entries_fail_at_their_own_tokens_and_later_entries_still_resolve(mkdtemp: Any, kind: str) -> None:
    values = ["", None, 42, False, {}, [], "db ", "${HOST}", "bad\0name", "db"]
    f = Fixture(mkdtemp, kind, values)
    report = check(f.options)
    assert report["status"] == "error"
    assert [d["code"] for d in report["diagnostics"]] == [
        "invalid-field", "invalid-field", "invalid-field", "invalid-field", "invalid-field", "invalid-field",
        "missing-service", "unsupported-substitution", "invalid-field",
    ]
    for i in range(len(values) - 1):
        # Quoted strings retain their opening quote as the token location.
        yaml_tokens = ['""', "null", "42", "false", "{}", "[]", '"db "', "${HOST}", '"bad\\0name"']
        expected = f.at(f"/services/web/depends_on/{i}", yaml_tokens[i] if kind == "yaml" else js_stringify(values[i]))
        assert report["diagnostics"][i]["locations"][0] == expected
        explanation = find(report["explanations"], lambda e: e["location"]["pointer"] == expected["pointer"])
        assert explanation["status"] == "invalid"
    assert any(
        e["relation"] == "depends-on" and e["status"] == "resolved" and e["location"]["pointer"] == "/services/web/depends_on/9"
        for e in report["edges"]
    )
    r = f.run("--sarif")
    assert r.status == 1
    sarif = json.loads(r.stdout)
    assert_valid_sarif(sarif)
    assert sarif == to_sarif(report)


@pytest.mark.parametrize("kind", KINDS)
def test_repeated_missing_names_keep_distinct_evidence_across_services_and_exact_escaped_names_resolve(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, ["ghost", "ghost", "db/~", "Db"], {"db/~": {}, "Db": {}, "worker": {"profiles": ["excluded"], "depends_on": ["ghost"]}})
    report = check(f.options)
    diagnostics = [d for d in report["diagnostics"] if d["code"] == "missing-service"]
    assert len(diagnostics) == 3
    pointers = ["/services/web/depends_on/0", "/services/web/depends_on/1", "/services/worker/depends_on/0"]
    for i, d in enumerate(diagnostics):
        assert d["locations"][0] == f.at(pointers[i], "ghost" if kind == "yaml" else '"ghost"', i)
    resolved = [e for e in report["edges"] if e["relation"] == "depends-on" and e["status"] == "resolved"]
    assert [e["to"] for e in resolved] == [f"{f.document}#/services/db~1~0", f"{f.document}#/services/Db"]
    sarif = to_sarif(report)
    assert_valid_sarif(sarif)
    assert [r["locations"][0]["properties"]["jsonPointer"] for r in sarif["runs"][0]["results"]] == pointers


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_list_shapes_reject_while_long_form_stays_explicitly_unevaluated(mkdtemp: Any, kind: str) -> None:
    for value in [None, 42, True, "", "db", {}, {"db": {"condition": "service_healthy", "required": False}}]:
        f = Fixture(mkdtemp, kind, value)
        report = check(f.options)
        entry = find(report["explanations"], lambda e: e["location"]["pointer"] == "/services/web/depends_on")
        long_form = isinstance(value, (dict, list))
        assert report["status"] == ("ok" if long_form else "error")
        assert (entry or {}).get("status") == ("not-evaluated" if long_form else "invalid")
        assert not any(e["relation"] == "depends-on" for e in report["edges"])
        if long_form:
            assert entry["reasons"][0]["code"] == "unsupported-depends-on-form"
            assert any(u["location"]["pointer"] == "/services/web/depends_on" for u in report["unevaluatedFields"])
        else:
            assert report["diagnostics"][0]["code"] == "invalid-depends-on"
            assert report["diagnostics"][0]["locations"][0]["pointer"] == "/services/web/depends_on"
        assert any(e["relation"] == "workspace-covered" for e in report["edges"])


@pytest.mark.parametrize("kind", KINDS)
def test_dependency_failures_preserve_build_mount_ordering_and_queued_nocopy_precision(mkdtemp: Any, kind: str) -> None:
    f = Fixture(
        mkdtemp,
        kind,
        ABSENT,
        {
            "web": {
                "build": {"context": "absent"},
                "volumes": [
                    {"type": "volume", "source": "missing", "target": "relative", "read_only": 42, "volume": {"nocopy": 42}},
                    {"type": "volume", "source": "missing", "target": "relative", "volume": {"nocopy": 42}},
                ],
                "depends_on": [42, "db", "missing"],
            },
            "inactive": {"profiles": ["excluded"], "volumes": ["./absent:/data"], "depends_on": ["missing"]},
        },
    )
    report = check(f.options)
    assert [d["code"] for d in report["diagnostics"][:6]] == [
        "missing-host-path", "missing-host-path", "invalid-mount", "invalid-mount", "invalid-field", "missing-service",
    ]
    assert report["diagnostics"][2]["locations"][0]["pointer"] == "/services/web/volumes/0/read_only"
    nocopy = report["diagnostics"][3]
    assert nocopy["locations"][0] == f.at("/services/web/volumes/1/volume/nocopy", "42\n" if kind == "yaml" else "42", 2)
    assert nocopy["message"] == "nocopy must be boolean"
    assert any(d["code"] == "missing-host-path" and d["locations"][0]["pointer"] == "/services/inactive/volumes/0" for d in report["diagnostics"])
    assert any(d["code"] == "missing-service" and d["locations"][0]["pointer"] == "/services/inactive/depends_on/0" for d in report["diagnostics"])
    assert not any(d["code"] in ("invalid-container-path", "missing-volume") for d in report["diagnostics"])


@pytest.mark.parametrize("kind", KINDS)
def test_missing_services_container_retains_inferred_source_and_region_free_sarif_evidence(mkdtemp: Any, kind: str) -> None:
    f = Fixture(mkdtemp, kind, ABSENT)
    write(f.document, "{}\n")
    report = check(f.options)
    assert report["diagnostics"][0]["code"] == "invalid-services"
    assert report["diagnostics"][0]["locations"][0] == {"file": f.document, "pointer": "/services", "line": 1, "column": 1, "inferred": True}
    sarif = to_sarif(report)
    assert_valid_sarif(sarif)
    assert "region" not in sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
