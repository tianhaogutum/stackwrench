"""PC1541 locates declared requirements without asserting runtime enforcement.

Condition stays first, including when required appears earlier in the source.
Explicit false/true and implicit defaults preserve provider risk/unknown judgments;
started/completed/short dependencies gain no healthy-gate finding. Both source and
report formats retain coordinates, selected targets and bounded static reasons.
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
from stackwrench.podman_audit.compare import compare_profiles
from stackwrench.podman_audit.input import load_document, read_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]
LEFT = {"schemaVersion": 1, "kind": "builtin", "provider": "podman-compose", "version": "1.6.0"}
RIGHT = LEFT | {"version": "1.6.1"}
NAMES = ("db.default", "db.optional", "db.required")
PHRASES = ("required defaults to true", "required is false", "required is true")
BOUND = "not runtime enforcement or a health result"


def locations(suffix):
    points = (((11, 9),), ((6, 9), (5, 9)), ((8, 9), (9, 9))) if suffix == "yaml" else (
        ((14, 11),), ((7, 11), (6, 11)), ((10, 11), (11, 11)))
    return [[{"path": '$.services["api-v1"].depends_on["' + name + '"].' + key,
              "declaration": "present", "line": line, "column": column}
             for key, (line, column) in zip(("condition", "required"), positions)]
            for name, positions in zip(NAMES, points)]


class HealthyRequiredExplanationTests(unittest.TestCase):
    def run_cli(self, path, compare, *options):
        target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                   str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                  ["--provider", "podman-compose", "--provider-version", "1.6.0"])
        return subprocess.run([*CLI, str(path), *target, *options], text=True,
                              capture_output=True, timeout=15)

    def assert_explanations(self, explanations, suffix, version):
        self.assertEqual(len(explanations), 3)
        digest = hashlib.sha256(files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()).hexdigest()
        for explanation, expected in zip(explanations, locations(suffix)):
            self.assertEqual(explanation["fieldPaths"], [p["path"] for p in expected])
            self.assertEqual(explanation["fieldLocations"], expected)
        for explanation, phrase in zip(explanations, PHRASES):
            self.assertIn(phrase, explanation["reason"])
            self.assertIn("optional dependency" if "false" in phrase else "required dependency", explanation["reason"])
            self.assertIn("service_healthy", explanation["reason"])
            self.assertIn("healthy-gate provider report", explanation["reason"])
            self.assertIn(BOUND, explanation["reason"])
            self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": version})
            self.assertEqual(explanation["evidenceScope"], {
                "kind": "provider-report", "provider": "podman-compose", "observedVersions": ["1.6.0"],
                "versionMatch": "exact" if version == "1.6.0" else "unknown"})
            self.assertEqual(explanation["localEvidence"], {
                "kind": "curated-catalog", "package": "stackwrench.podman_audit", "resource": "catalog.json",
                "jsonPointer": "/rules/PC1541", "sha256": digest})
            self.assertEqual(explanation["adviceScope"], {
                "runtimeVerificationRequired": True, "guaranteesRuntimeSuccess": False})

    def test_required_coordinates_defaults_targets_and_additive_reports(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / "examples/podman_audit" / ("healthy-required." + suffix))
            original = copy.deepcopy(model)
            audits = {}
            for version in ("1.6.0", "1.6.1"):
                with self.subTest(suffix=suffix, version=version):
                    report = audit(model, provider="podman-compose", provider_version=version, explain=True)
                    audits[version] = report
                    gates = [f for f in report["findings"] if f["ruleId"] == "PC1541"]
                    self.assertEqual([f["path"] for f in gates], [p[0]["path"].removesuffix(".condition") for p in locations(suffix)])
                    self.assert_explanations([f["explanation"] for f in gates], suffix, version)
                    self.assertTrue(all(f["details"] == {"versionMatch": "exact" if version == "1.6.0" else "unknown"} for f in gates))
                    self.assertTrue(all(f["severity"] == "warning" for f in gates))
                    self.assertEqual(report["summary"], {"services": 5, "findings": 3 if version == "1.6.0" else 4,
                                                        "error": 0, "warning": 3 if version == "1.6.0" else 4})
                    stripped = copy.deepcopy(report)
                    for item in stripped["findings"]:
                        del item["explanation"]
                    self.assertEqual(stripped, audit(model, provider="podman-compose", provider_version=version))
            for left, right in ((LEFT, RIGHT), (RIGHT, LEFT)):
                with self.subTest(suffix=suffix, left=left["version"]):
                    report = compare_profiles(model, left, right, explain=True)
                    self.assertEqual(report, compare_profiles(model, left, right, explain=True))
                    self.assertEqual(report["structuralFindings"], [])
                    self.assertEqual(report["summary"], {"services": 5, "capabilities": 3, "changed": 3, "error": 0, "warning": 4})
                    self.assertEqual([r["path"] for r in report["capabilities"]], [p[0]["path"] for p in locations(suffix)])
                    for side, profile in (("left", left), ("right", right)):
                        version = profile["version"]
                        self.assert_explanations([r[side]["explanation"] for r in report["capabilities"]], suffix, version)
                        gates = [f for f in audits[version]["findings"] if f["ruleId"] == "PC1541"]
                        for row, finding in zip(report["capabilities"], gates):
                            self.assertEqual(row[side]["explanation"], finding["explanation"])
                            self.assertEqual(row[side]["judgment"], "reported-risk" if version == "1.6.0" else "unknown")
                            del row[side]["explanation"]
                        for item in report["profileFindings"][side]:
                            self.assertEqual(item["explanation"]["target"]["version"], version)
                            del item["explanation"]
                    self.assertEqual(report, compare_profiles(model, left, right))
            self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(audits))
            self.assertEqual(model, original)

    def test_human_json_cli_coordinates_and_reasons(self):
        for suffix in ("yaml", "json"):
            path = ROOT / "examples/podman_audit" / ("healthy-required." + suffix)
            for compare in (False, True):
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, compare=compare, json_mode=json_mode):
                        result = self.run_cli(path, compare, "--explain", *(["--json"] if json_mode else []))
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            report = json.loads(result.stdout)
                            if compare:
                                for side, version in (("left", "1.6.0"), ("right", "1.6.1")):
                                    self.assert_explanations([r[side]["explanation"] for r in report["capabilities"]], suffix, version)
                            else:
                                self.assert_explanations([f["explanation"] for f in report["findings"]], suffix, "1.6.0")
                        elif compare:
                            explanations = [json.loads(line.split("Explanation: ", 1)[1]) for line in result.stdout.splitlines()
                                            if line.startswith("    Explanation: ")]
                            self.assert_explanations(explanations[::2], suffix, "1.6.0")
                            self.assert_explanations(explanations[1::2], suffix, "1.6.1")
                        else:
                            for fields in locations(suffix):
                                for field in fields:
                                    self.assertIn(f"Declaration: {field['path']} — line {field['line']}, column {field['column']}", result.stdout)
                            for phrase in (*PHRASES, BOUND, "Selected target: podman-compose 1.6.0"):
                                self.assertIn(phrase, result.stdout)

    def test_nonhealthy_conditions_and_unavailable_source(self):
        for required in (False, True, None):
            for condition in ("service_started", "service_completed_successfully", "service_healthy"):
                with self.subTest(required=required, condition=condition):
                    options = {"condition": condition}
                    if required is not None:
                        options["required"] = required
                    model = {"services": {"api": {"depends_on": {"db": options}},
                                          "db": {"healthcheck": {"test": ["CMD", "SECRET_DO_NOT_ECHO"]}}}}
                    report = audit(model, provider="podman-compose", provider_version="1.6.0", explain=True)
                    self.assertEqual(len(report["findings"]), int(condition == "service_healthy"))
                    if condition == "service_healthy":
                        explanation = report["findings"][0]["explanation"]
                        paths = ["$.services.api.depends_on.db." + k for k in ("condition", "required")[:1 if required is None else 2]]
                        self.assertEqual(explanation["fieldPaths"], paths)
                        self.assertEqual(explanation["fieldLocations"], [{"path": p, "declaration": "unavailable",
                                                                          "line": None, "column": None} for p in paths])
        model = load_document('services: {api: {depends_on: [db]}, db: {}}')
        self.assertEqual(audit(model, provider="podman-compose", provider_version="1.6.0", explain=True)["findings"], [])

    def test_thresholds_and_malformed_fields(self):
        path = ROOT / "examples/podman_audit/healthy-required.yaml"
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.json"
            for compare in (False, True):
                for explain in ([], ["--explain"]):
                    for threshold, expected in (("error", 0), ("warning", 1), ("never", 0)):
                        result = self.run_cli(path, compare, "--json", "--fail-on", threshold, *explain)
                        self.assertEqual(result.returncode, expected, result.stderr)
                    for key, value in (("required", "false"), ("required", 0), ("required", None),
                                       ("disable", "true"), ("test", ["NONE", "extra"]), ("start_interval", 1)):
                        options = {"condition": "service_healthy", "required": value if key == "required" else False}
                        health = {"test": ["CMD", "SECRET_DO_NOT_ECHO"]}
                        if key != "required":
                            health[key] = value
                        bad.write_text(json.dumps({"services": {"api": {"depends_on": {"db": options}}, "db": {"healthcheck": health}}}))
                        result = self.run_cli(bad, compare, "--json", "--fail-on", "never", *explain)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        error = json.loads(result.stdout)
                        self.assertEqual(error["assessment"], "not-evaluated")
                        self.assertEqual(error["error"]["path"], "$.services.api.depends_on.db.required" if key == "required" else
                                         "$.services.db.healthcheck." + key)
                        self.assertNotIn("findings", error)
                        self.assertNotIn("capabilities", error)


if __name__ == "__main__":
    unittest.main()
