"""Shared explanations describe source/graph evidence, never a selected profile.

--explain is additive in comparison JSON and human output. Exact YAML/JSON
coordinates, absent whole healthcheck/test, and catalog provenance are retained.
Provider targets stay per assessment. Counts, order, plain reports and failures
are unchanged: required missing dependency -> 1; malformed input/profile -> 2,
even with --fail-on never. No image healthcheck or runtime support is inferred.
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

from stackwrench.podman_audit.compare import compare_profiles
from stackwrench.podman_audit.input import load_document, read_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]
LEFT = {"schemaVersion": 1, "kind": "builtin", "provider": "podman-compose", "version": "1.6.0"}
RIGHT = LEFT | {"version": "1.6.1"}


def position(path, line=None, column=None):
    return {"path": path, "declaration": "present" if line else "absent",
            "line": line, "column": column}


def expected_locations(suffix):
    missing, cache, db, health = ((4, 9), (8, 9), (10, 9), (13, 5)) if suffix == "yaml" else (
        (5, 9), (11, 11), (14, 11), (20, 7))
    return [
        [position("$.services.api.depends_on[0]", *missing)],
        [position("$.services.worker.depends_on.cache.condition", *cache),
         position("$.services.cache.healthcheck")],
        [position("$.services.worker.depends_on.db.condition", *db),
         position("$.services.db.healthcheck", *health), position("$.services.db.healthcheck.test")],
    ]


class SharedStructuralTests(unittest.TestCase):
    def run_cli(self, path, *extra, left=None, right=None):
        return subprocess.run([*CLI, str(path), "--compare-profiles",
                               str(left or ROOT / "examples/podman_audit/profiles/observed.json"),
                               str(right or ROOT / "examples/podman_audit/profiles/unknown.json"), *extra],
                              capture_output=True, text=True, timeout=15)

    def assert_shared(self, report, suffix):
        shared = report["structuralFindings"]
        self.assertEqual([f["ruleId"] for f in shared], ["DEP001", "DEP002", "DEP002"])
        digest = hashlib.sha256(files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()).hexdigest()
        for finding, expected in zip(shared, expected_locations(suffix)):
            self.assertIn("explanation", finding, "comparison strips shared structural explanations")
            explanation = finding["explanation"]
            self.assertEqual(explanation["fieldLocations"], expected)
            self.assertEqual(explanation["fieldPaths"], [item["path"] for item in expected])
            self.assertNotIn("target", explanation)
            self.assertEqual(explanation["evidenceScope"], {
                "kind": "structural", "provider": None, "observedVersions": [], "versionMatch": "not-applicable"})
            self.assertEqual(explanation["localEvidence"], {
                "kind": "curated-catalog", "package": "stackwrench.podman_audit", "resource": "catalog.json",
                "jsonPointer": "/rules/" + finding["ruleId"], "sha256": digest})
            self.assertEqual(explanation["adviceScope"], {
                "runtimeVerificationRequired": True, "guaranteesRuntimeSuccess": False})
            self.assertNotIn("1.6.0", json.dumps(explanation))
            self.assertNotIn("1.6.1", json.dumps(explanation))
        self.assertIn("absent", shared[0]["explanation"]["reason"])
        for finding in shared[1:]:
            self.assertIn("inherited image healthcheck remains unknown", finding["explanation"]["reason"])
            self.assertEqual(finding["confidence"], "unverified")

    def test_shared_explanations_have_exact_yaml_and_json_locations(self):
        for suffix in ("yaml", "json"):
            with self.subTest(suffix=suffix):
                model = read_document(ROOT / "examples/podman_audit" / ("comparison-structural." + suffix))
                self.assert_shared(compare_profiles(model, LEFT, RIGHT, explain=True), suffix)

    def test_both_cli_formats_retain_absence_coordinates_and_no_shared_target(self):
        for suffix in ("yaml", "json"):
            path = ROOT / "examples/podman_audit" / ("comparison-structural." + suffix)
            for json_mode in (True, False):
                with self.subTest(suffix=suffix, json_mode=json_mode):
                    result = self.run_cli(path, "--explain", *(["--json"] if json_mode else []))
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stderr, "")
                    if json_mode:
                        self.assert_shared(json.loads(result.stdout), suffix)
                    else:
                        # Human comparison renders each finding as a JSON line.
                        shared = [json.loads(line) for line in result.stdout.splitlines()
                                  if line.startswith('  {"category": "structural"')]
                        self.assert_shared({"structuralFindings": shared}, suffix)
                        self.assertEqual(result.stdout.count("shared: WARNING DEP002"), 2)
                        self.assertIn("shared: ERROR DEP001 $.services.api.depends_on[0]", result.stdout)

    def test_additive_deterministic_nonmutating_with_unchanged_provider_targets_and_counts(self):
        model = read_document(ROOT / "examples/podman_audit/comparison-structural.yaml")
        original = copy.deepcopy((model, LEFT, RIGHT))
        plain = compare_profiles(model, LEFT, RIGHT)
        report = compare_profiles(model, LEFT, RIGHT, explain=True)
        self.assertEqual((model, LEFT, RIGHT), original)
        self.assertEqual(report, compare_profiles(model, LEFT, RIGHT, explain=True))
        self.assertEqual(report["summary"], {"services": 4, "capabilities": 2, "changed": 2, "error": 1, "warning": 5})
        self.assertEqual(report["compatibility"], "not-established")
        self.assertEqual([row["path"] for row in report["capabilities"]], [
            "$.services.worker.depends_on.cache.condition", "$.services.worker.depends_on.db.condition"])
        for row in report["capabilities"]:
            for side, profile, judgment, match in (("left", LEFT, "reported-risk", "exact"),
                                                    ("right", RIGHT, "unknown", "unknown")):
                item = row[side]
                self.assertEqual(item["judgment"], judgment)
                self.assertEqual(item["explanation"]["target"], {"provider": profile["provider"], "version": profile["version"]})
                self.assertEqual(item["explanation"]["evidenceScope"]["versionMatch"], match)
        coverage = report["profileFindings"]["right"][0]
        self.assertEqual(coverage["ruleId"], "CAT001")
        self.assertEqual(coverage["explanation"]["target"]["version"], "1.6.1")
        self.assertEqual(coverage["explanation"]["fieldLocations"], [])
        for item in [*report["structuralFindings"], coverage,
                     *(row[side] for row in report["capabilities"] for side in ("left", "right"))]:
            del item["explanation"]
        self.assertEqual(report, plain)
        self.assertNotIn('"explanation"', json.dumps(plain))

    def test_shared_evidence_is_identical_when_profiles_swap_or_use_custom_assertions(self):
        model = read_document(ROOT / "examples/podman_audit/structural-errors.yaml")
        original = copy.deepcopy(model)
        baseline = compare_profiles(model, LEFT, RIGHT, explain=True)["structuralFindings"]
        self.assertEqual({f["ruleId"] for f in baseline}, {"DEP001", "DEP002", "DEP003"})
        for left, right in ((RIGHT, LEFT), (RIGHT, RIGHT),
                            (LEFT | {"kind": "custom", "rules": {}}, RIGHT)):
            self.assertEqual(compare_profiles(model, left, right, explain=True)["structuralFindings"], baseline)
        for finding in baseline:
            self.assertIn("explanation", finding)
            self.assertNotIn("target", finding["explanation"])
        disabled = next(f for f in baseline if f["ruleId"] == "DEP002")
        self.assertIn("disabled", disabled["explanation"]["reason"])
        self.assertEqual(disabled["explanation"]["fieldLocations"][-1]["declaration"], "present")
        self.assertEqual(model, original)
        # Programmatic callers without source metadata must not invent absence.
        report = compare_profiles(dict(load_document('services: {api: {depends_on: [missing]}}')), LEFT, RIGHT, explain=True)
        self.assertEqual(report["structuralFindings"][0]["explanation"]["fieldLocations"], [{
            "path": "$.services.api.depends_on[0]", "declaration": "unavailable", "line": None, "column": None}])

    def test_thresholds_are_unchanged_and_input_errors_cannot_be_suppressed(self):
        path = ROOT / "examples/podman_audit/comparison-structural.yaml"
        for explain in ([], ["--explain"]):
            for threshold, expected in (("error", 1), ("warning", 1), ("never", 0)):
                result = self.run_cli(path, "--json", "--fail-on", threshold, *explain)
                self.assertEqual(result.returncode, expected, result.stderr)
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.json"
            bad.write_text('{"schemaVersion":1,"kind":"builtin","provider":"podman-compose","version":"latest"}')
            result = self.run_cli(path, "--explain", "--json", "--fail-on", "never", right=bad)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid-version")
            for text in ('{bad', '{"services":[]}', '{"services":{"api":{"depends_on":42}}}'):
                bad.write_text(text)
                result = self.run_cli(bad, "--explain", "--json", "--fail-on", "never")
                self.assertEqual(result.returncode, 2, result.stderr)
                error = json.loads(result.stdout)
                self.assertEqual(error["assessment"], "not-evaluated")
                self.assertNotIn("structuralFindings", error)
            warning = Path(temp) / "warning.yaml"
            warning.write_text('services:\n  api: {depends_on: {db: {condition: service_healthy}}}\n  db: {}\n')
            for threshold, expected in (("error", 0), ("warning", 1), ("never", 0)):
                result = self.run_cli(warning, "--explain", "--json", "--fail-on", threshold)
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
