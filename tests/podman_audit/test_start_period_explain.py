"""PC1500 adds declared start_period context last; it never creates a finding.

The option/disable/test prefix is unchanged. Zero strings count as declarations,
missing keys invent no location, and descriptions assert no runtime behavior.
"""

import copy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.podman_audit.audit import audit
from stackwrench.podman_audit.cli import render_human
from stackwrench.podman_audit.compare import compare_profiles, render_comparison
from stackwrench.podman_audit.input import load_document, read_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]
LEFT = {"schemaVersion": 1, "kind": "builtin", "provider": "podman-compose", "version": "1.6.0"}
RIGHT = LEFT | {"version": "1.6.1"}
FIELDS = {
    "active": ("start_interval", "start_period"),
    "both": ("start_interval", "disable", "test", "start_period"),
    "disabled": ("start_interval", "disable", "start_period"),
    "none": ("start_interval", "test", "start_period"),
    "omitted": ("start_interval",),
    "omitted_disabled": ("start_interval", "disable", "test"),
}
LINES = {
    "yaml": ((23, 22), (6, 7, 5, 4), (11, 12, 10), (16, 18, 19), (28,), (33, 34, 32)),
    "json": ((37, 36), (9, 10, 6, 5), (16, 17, 15), (26, 28, 31), (47,), (59, 60, 56)),
}


def expected_locations(suffix, name):
    return [{"path": f"$.services.{name}.healthcheck.{key}", "declaration": "present",
             "line": line, "column": 7 if suffix == "yaml" else 9}
            for key, line in zip(FIELDS[name], LINES[suffix][list(FIELDS).index(name)])]


def pc_findings(report):
    return [f for f in report["findings"] if f["ruleId"] == "PC1500"]


class StartPeriodExplanationTests(unittest.TestCase):
    def assert_context(self, explanation, name, suffix, version):
        expected = expected_locations(suffix, name)
        self.assertEqual(explanation["fieldPaths"], [p["path"] for p in expected])
        self.assertEqual(explanation["fieldLocations"], expected)
        self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": version})
        self.assertEqual(explanation["evidenceScope"], {"kind": "provider-report", "provider": "podman-compose",
                         "observedVersions": ["1.6.0"], "versionMatch": "exact" if version == "1.6.0" else "unknown"})
        self.assertEqual(explanation["localEvidence"], {"kind": "curated-catalog", "package": "stackwrench.podman_audit",
                         "resource": "catalog.json", "jsonPointer": "/rules/PC1500",
                         "sha256": hashlib.sha256(files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()).hexdigest()})
        self.assertEqual(explanation["adviceScope"], {"runtimeVerificationRequired": True, "guaranteesRuntimeSuccess": False})
        reason = explanation["reason"]
        self.assertTrue(reason.startswith("start_interval is declared;"))
        for key, phrase in (("disable", "disable is true"), ("test", "test is [NONE]"),
                            ("start_period", "start_period is declared")):
            self.assertEqual(phrase in reason, key in FIELDS[name])
        if "start_period" in FIELDS[name]:
            self.assertIn("static declaration context", reason)
            self.assertIn("not execution, timing, health or provider enforcement", reason)
        self.assertEqual("not a running probe" in reason, "disable" in FIELDS[name] or "test" in FIELDS[name])

    def test_yaml_json_coordinates_and_additive_reports(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / f"examples/podman_audit/start-period-context.{suffix}")
            original = copy.deepcopy(model)
            for version in ("1.6.0", "1.6.1"):
                with self.subTest(suffix=suffix, version=version):
                    plain = audit(model, provider="podman-compose", provider_version=version)
                    explained = audit(model, provider="podman-compose", provider_version=version, explain=True)
                    findings = pc_findings(explained)
                    self.assertEqual([f["service"] for f in findings], list(FIELDS))
                    for finding in findings:
                        name = finding["service"]
                        self.assertEqual(finding["details"], {"healthcheckState": "disabled" if name in
                                         ("both", "disabled", "none", "omitted_disabled") else "declared-option",
                                         "startPeriodDeclared": "start_period" in FIELDS[name],
                                         "versionMatch": "exact" if version == "1.6.0" else "unknown"})
                        self.assert_context(finding["explanation"], name, suffix, version)
                    for finding in explained["findings"]:
                        del finding["explanation"]
                    self.assertEqual(explained, plain)
            plain = compare_profiles(model, LEFT, RIGHT)
            explained = compare_profiles(model, LEFT, RIGHT, explain=True)
            rows = [r for r in explained["capabilities"] if r["ruleId"] == "PC1500"]
            self.assertEqual([r["service"] for r in rows], list(FIELDS))
            for row in rows:
                for side, version, judgment in (("left", "1.6.0", "reported-risk"), ("right", "1.6.1", "unknown")):
                    self.assert_context(row[side]["explanation"], row["service"], suffix, version)
                    self.assertEqual(row[side]["judgment"], judgment)
            self.assertEqual([f["ruleId"] for f in explained["structuralFindings"]], ["DEP002"])
            self.assertNotIn("target", explained["structuralFindings"][0]["explanation"])
            for row in explained["capabilities"]:
                for side in ("left", "right"):
                    del row[side]["explanation"]
            for group in [explained["structuralFindings"], *explained["profileFindings"].values()]:
                for finding in group:
                    del finding["explanation"]
            self.assertEqual(explained, plain)
            self.assertEqual(model, original)
            self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(plain))

    def test_human_and_json_cli_reports(self):
        for suffix in ("yaml", "json"):
            path = ROOT / f"examples/podman_audit/start-period-context.{suffix}"
            model = read_document(path)
            for mode in ("1.6.0", "1.6.1", "compare"):
                target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                           str(ROOT / "examples/podman_audit/profiles/unknown.json")] if mode == "compare" else
                          ["--provider", "podman-compose", "--provider-version", mode])
                expected = compare_profiles(model, LEFT, RIGHT, explain=True) if mode == "compare" else audit(
                    model, provider="podman-compose", provider_version=mode, explain=True)
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, mode=mode, json_mode=json_mode):
                        result = subprocess.run([*CLI, str(path), *target, "--explain", "--fail-on", "never",
                                                 *(["--json"] if json_mode else [])], text=True, capture_output=True, timeout=15)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            self.assertEqual(json.loads(result.stdout), expected)
                        else:
                            render = render_comparison if mode == "compare" else render_human
                            self.assertEqual(result.stdout, render(expected) + "\n")
                            if mode == "compare":
                                decoded = [json.loads(line.removeprefix("    Explanation: ")) for line in
                                           result.stdout.splitlines() if line.startswith("    Explanation: ")]
                                pc = [e for e in decoded if e["localEvidence"]["jsonPointer"] == "/rules/PC1500"]
                                self.assertEqual(len(pc), 12)
                                for i, name in enumerate(FIELDS):
                                    self.assert_context(pc[2*i], name, suffix, "1.6.0")
                                    self.assert_context(pc[2*i+1], name, suffix, "1.6.1")
                            else:
                                for name in FIELDS:
                                    for field in expected_locations(suffix, name):
                                        self.assertIn(f"Declaration: {field['path']} — line {field['line']}, column {field['column']}", result.stdout)

    def test_zero_omitted_negative_and_unavailable_source(self):
        for health in ({"start_period": "0"}, {"start_period": "0", "disable": True},
                       {"start_period": "0s", "test": ["NONE"]}):
            model = load_document(json.dumps({"services": {"db": {"healthcheck": health}}}), format="json")
            for version in ("1.6.0", "1.6.1"):
                self.assertEqual(pc_findings(audit(model, provider="podman-compose", provider_version=version, explain=True)), [])
            self.assertEqual(compare_profiles(model, LEFT, RIGHT, explain=True)["capabilities"], [])
        for period in (None, "0", "0s", "1m30s"):
            for loaded in (False, True):
                with self.subTest(period=period, loaded=loaded):
                    health = {"start_interval": "1s", "disable": False, "test": "NONE"}
                    if period is not None:
                        health["start_period"] = period
                    model = {"services": {"db.primary": {"healthcheck": health}}}
                    if loaded:
                        model = load_document(json.dumps(model), format="json")
                    item = pc_findings(audit(model, provider="podman-compose", provider_version="1.6.0", explain=True))[0]
                    fields = ["start_interval", *(["start_period"] if period is not None else [])]
                    self.assertEqual(item["explanation"]["fieldPaths"], ['$.services["db.primary"].healthcheck.' + k for k in fields])
                    self.assertIs(item["details"]["startPeriodDeclared"], period is not None)
                    self.assertEqual(item["details"]["healthcheckState"], "declared-option")
                    for loc in item["explanation"]["fieldLocations"]:
                        self.assertEqual(loc["declaration"], "present" if loaded else "unavailable")
                        self.assertEqual(loc["line"], 1 if loaded else None)
                        if not loaded:
                            self.assertIsNone(loc["column"])

    def test_thresholds_and_malformed_healthchecks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "compose.json"
            for compare in (False, True):
                target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                           str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                          ["--provider", "podman-compose", "--provider-version", "1.6.0"])
                for explain in ([], ["--explain"]):
                    path.write_text(json.dumps({"services": {"db": {"healthcheck": {
                        "start_interval": "1s", "start_period": "0", "disable": True}}}}))
                    for threshold, code in (("error", 0), ("warning", 1), ("never", 0)):
                        result = subprocess.run([*CLI, str(path), *target, "--json", *explain,
                                                 "--fail-on", threshold], capture_output=True, text=True, timeout=15)
                        self.assertEqual(result.returncode, code, result.stderr)
                    for key, value in (("start_period", 0), ("start_period", "-1s"), ("start_period", None),
                                       ("start_period", "1"), ("start_interval", 0), ("disable", "true"),
                                       ("test", ["NONE", "extra"]), (None, [])):
                        health = {"start_interval": "1s", "start_period": "0", key: value} if key else value
                        path.write_text(json.dumps({"services": {"db": {"healthcheck": health}}}))
                        result = subprocess.run([*CLI, str(path), *target, "--json", *explain, "--fail-on", "never"],
                                                capture_output=True, text=True, timeout=15)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        error = json.loads(result.stdout)
                        self.assertEqual(error["assessment"], "not-evaluated")
                        self.assertEqual(error["error"]["path"], "$.services.db.healthcheck" + ("." + key if key else ""))
                        self.assertNotIn("findings", error)
                        self.assertNotIn("capabilities", error)


if __name__ == "__main__":
    unittest.main()
