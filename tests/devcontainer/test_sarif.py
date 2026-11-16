"""Port of test/sarif.test.js (node:test + Ajv) to pytest (+ jsonschema)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench import devcontainer as library

from .helpers import TMP, Result, assert_valid_sarif, decode_location, find, js_stringify, read, run_cli
from stackwrench.devcontainer.jsutil import utf16_len

pytest.importorskip("jsonschema")

SECRET = "SYNTHETIC_ENV_VALUE_MUST_STAY_PRIVATE"


class Fixture:
    def __init__(self, mkdtemp: Any) -> None:
        self.root = mkdtemp("wirecheck space # % ü-")
        self.config = os.path.join(self.root, "config space/devcontainer #1.json")

    def write(self, file: str, data: Any) -> str:
        target = os.path.join(self.root, file)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(data if isinstance(data, str) else js_stringify(data, 2))
        return target

    def run(self, *args: str) -> Result:
        return run_cli(
            ["--config", self.config, "--workspace", self.root, *args],
            cwd=TMP,
            env={"PATH": "/nonexistent", "HOME": self.root, "WIRECHECK_SECRET": SECRET},
        )


@pytest.fixture
def f(mkdtemp: Any) -> Fixture:
    return Fixture(mkdtemp)


def valid_log(r: Result, exit: int) -> tuple[dict[str, Any], dict[str, Any]]:
    assert r.status == exit, r.stdout + r.stderr
    assert r.stderr == ""
    log = json.loads(r.stdout)
    assert_valid_sarif(log)
    assert log["version"] == "2.1.0"
    assert len(log["runs"]) == 1
    run = log["runs"][0]
    for result in run["results"]:
        assert run["tool"]["driver"]["rules"][result["ruleIndex"]]["id"] == result["ruleId"]
        assert result["kind"] == "fail"
    return log, run


def test_sarif_maps_multiple_findings_across_files_with_encoded_paths_and_stable_rules_matching_terminal_json(f: Fixture) -> None:
    compose = f.write("compose space/compose # %.yaml", 'services:\n  web:\n    volumes:\n      - "./missing:/work"\n')
    f.write(
        "config space/devcontainer #1.json",
        {
            "dockerComposeFile": "../compose space/compose # %.yaml",
            "service": "absent",
            "runServices": ["ghost"],
            "workspaceFolder": "/work",
            "containerEnv": {"SECRET": SECRET},
            "initializeCommand": "touch SHOULD_NOT_EXIST",
        },
    )
    before = [read(file) for file in [f.config, compose]]
    json_run = f.run("--json")
    report = json.loads(json_run.stdout)
    terminal = f.run()
    log, run = valid_log(f.run("--sarif"), 1)
    assert len(run["results"]) == len(report["diagnostics"])
    assert len([line for line in terminal.stdout.split("\n") if re.match(r"(error|warning) \[", line)]) == len(run["results"])
    assert [[r["ruleId"], r["level"], r["message"]["text"]] for r in run["results"]] == [
        [d["code"], d["severity"], d["message"]] for d in report["diagnostics"]
    ]
    assert run["columnKind"] == "utf16CodeUnits"
    missing = [r for r in run["results"] if r["ruleId"] == "missing-service"]
    assert len(missing) == 2  # Do not collapse repeated instances of a rule.
    assert len(missing[0]["locations"]) == 1
    primary = missing[0]["locations"][0]
    assert decode_location(run, primary) == f.config
    assert re.search(r"config%20space/devcontainer%20%231.json", primary["physicalLocation"]["artifactLocation"]["uri"])
    assert primary["properties"]["jsonPointer"] == "/service"
    assert primary["physicalLocation"]["region"] == {"startLine": 3, "startColumn": 14}
    assert decode_location(run, missing[0]["relatedLocations"][0]) == compose
    assert missing[0]["relatedLocations"][0]["properties"]["jsonPointer"] == "/services"
    assert missing[0]["relatedLocations"][0]["physicalLocation"]["region"]["startLine"] == 2
    host = find(run["results"], lambda r: r["ruleId"] == "missing-host-path")
    assert host["locations"][0]["physicalLocation"]["region"]["startLine"] == 4
    assert "region" not in host["relatedLocations"][0]["physicalLocation"]
    assert library.to_sarif(report) == log
    repeated, _ = valid_log(f.run("--sarif", "--explain", "--strict"), 1)
    assert repeated == log
    for output in [terminal.stdout, json_run.stdout, js_stringify(log)]:
        assert SECRET not in output
    assert [read(file) for file in [f.config, compose]] == before
    assert not os.path.exists(os.path.join(f.root, "SHOULD_NOT_EXIST"))
    # Encounter order does not affect the rule table or the rule's identity.
    reversed_log = library.to_sarif({**report, "diagnostics": list(reversed(report["diagnostics"]))})
    assert reversed_log["runs"][0]["tool"]["driver"]["rules"] == run["tool"]["driver"]["rules"]


def test_sarif_preserves_warning_levels_and_normal_strict_exit_policies_including_an_empty_success_log(f: Fixture) -> None:
    f.write("config space/devcontainer #1.json", {"image": "synthetic:example", "workspaceFolder": "/image-only"})
    normal_log, normal_run = valid_log(f.run("--sarif"), 0)
    assert len(normal_run["results"]) == 1
    assert normal_run["results"][0]["level"] == "warning"
    assert valid_log(f.run("--sarif", "--strict"), 1)[0] == normal_log
    f.write(
        "config space/devcontainer #1.json",
        {
            "image": "synthetic:example",
            "workspaceMount": "type=bind,source=${localWorkspaceFolder},target=/work",
            "workspaceFolder": "/work",
        },
    )
    _, success = valid_log(f.run("--sarif", "--strict"), 0)
    assert success["results"] == []
    assert success["tool"]["driver"]["rules"] == []


def test_sarif_omits_regions_for_unread_documents_and_inferred_fields_and_locations_for_source_free_failures(f: Fixture) -> None:
    missing = json.loads(f.run("--json").stdout)
    assert missing["diagnostics"][0]["code"] == "read-error"
    assert missing["diagnostics"][0]["locations"][0]["line"] is None
    assert missing["diagnostics"][0]["locations"][0]["column"] is None
    _, unread = valid_log(f.run("--sarif"), 1)
    assert decode_location(unread, unread["results"][0]["locations"][0]) == f.config
    assert "region" not in unread["results"][0]["locations"][0]["physicalLocation"]
    f.write("config space/devcontainer #1.json", " " * (2 * 1024 * 1024 + 1))
    assert "region" not in valid_log(f.run("--sarif"), 1)[1]["results"][0]["locations"][0]["physicalLocation"]
    f.write("config space/devcontainer #1.json", {"image": "synthetic:example"})
    inferred = valid_log(f.run("--sarif"), 0)[1]["results"][0]["locations"][0]
    assert inferred["properties"]["inferred"] is True
    assert "region" not in inferred["physicalLocation"]
    _, no_workspace = valid_log(f.run("--workspace", os.path.join(f.root, "absent"), "--sarif"), 1)
    assert no_workspace["results"][0]["ruleId"] == "invalid-workspace"
    assert "locations" not in no_workspace["results"][0]


def test_sarif_parse_errors_retain_real_parser_coordinates_json_and_sarif_are_mutually_exclusive(f: Fixture) -> None:
    f.write("config space/devcontainer #1.json", '{\n  "image": ,\n}\n')
    parse = valid_log(f.run("--sarif"), 1)[1]["results"][0]
    assert parse["ruleId"] == "parse-error"
    assert parse["locations"][0]["physicalLocation"]["region"]["startLine"] == 2
    conflict = f.run("--json", "--sarif")
    assert conflict.status == 2
    assert conflict.stdout == ""
    assert re.search(r"--json and --sarif are mutually exclusive", conflict.stderr)


def test_sarif_ordered_compose_references_map_only_to_the_supplied_final_snapshot_without_invented_override_provenance(
    f: Fixture,
) -> None:
    f.write(
        "config space/devcontainer #1.json",
        {
            "dockerComposeFile": ["../raw space/base.yaml", "../raw space/override.yaml"],
            "service": "absent",
            "runServices": ["web"],
            "workspaceFolder": "/work",
        },
    )
    f.write("raw space/base.yaml", "not parsed: [")
    f.write("raw space/override.yaml", "not parsed: [")
    snapshot = f.write("snapshot space/final # %.json", {"services": {"web": {"volumes": ["./missing:/work"]}}})
    _, run = valid_log(f.run("--sarif", "--compose-json", snapshot), 1)
    ref = find(run["results"], lambda r: r["ruleId"] == "missing-service")
    assert decode_location(run, ref["relatedLocations"][0]) == snapshot
    host = find(run["results"], lambda r: r["ruleId"] == "missing-host-path")
    assert decode_location(run, host["locations"][0]) == snapshot
    assert re.search(r"raw space/missing", host["message"]["text"])  # Original Compose base, not snapshot base.
    assert "base.yaml" not in js_stringify(run["results"])
    assert "override.yaml" not in js_stringify(run["results"])


def test_sarif_columns_count_utf_16_code_units_from_the_real_json_token(f: Fixture) -> None:
    text = '{"image":"x", "name":"😀", "workspaceFolder":"relative"}'
    f.write("config space/devcontainer #1.json", text)
    _, run = valid_log(f.run("--sarif"), 1)
    region = find(run["results"], lambda r: r["ruleId"] == "invalid-container-path")["locations"][0]["physicalLocation"]["region"]
    assert region == {"startLine": 1, "startColumn": utf16_len(text[: text.index('"relative"')]) + 1}
