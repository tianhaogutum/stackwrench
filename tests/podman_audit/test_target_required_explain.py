"""DEP002 adds explicit requirement context without reclassifying healthy targets.

Existing condition/block/cause/absence order is preserved; only present required
keys are appended. Missing tests stay unknown; optional disabled targets remain
errors. Shared structural evidence is independent of selected provider targets.
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
CONSUMERS = ("api.default", "api.optional", "api.required")
TARGETS = ("disabled", "empty", "inherited", "none")
PHRASES = ("required defaults to true", "required is false", "required is true")
BOUND = "not runtime enforcement or a health result"


def locations(suffix, consumer, target):
    i, j = CONSUMERS.index(consumer), TARGETS.index(target)
    conditions, required, blocks, causes = (
        (((5, 7, 9, 11), (16, 19, 22, 25), (32, 35, 38, 41)),
         (None, (15, 18, 21, 24), (33, 36, 39, 42)), (49, 52, None, 56), (50, None, None, 57))
        if suffix == "yaml" else
        (((6, 9, 12, 15), (23, 27, 31, 35), (46, 50, 54, 58)),
         (None, (22, 26, 30, 34), (47, 51, 55, 59)), (72, 77, None, 83), (73, None, None, 84)))
    edge = '$.services["' + consumer + '"].depends_on.' + target
    block = "$.services." + target + ".healthcheck"
    def point(path, line, column):
        return {"path": path, "declaration": "present" if line else "absent", "line": line,
                "column": column if line else None}
    expected = [point(edge + ".condition", conditions[i][j], 9 if suffix == "yaml" else 11),
                point(block, blocks[j], 5 if suffix == "yaml" else 7)]
    if target != "inherited":
        expected.append(point(block + (".disable" if target == "disabled" else ".test"),
                              causes[j], 7 if suffix == "yaml" else 9))
    if required[i] is not None:
        expected.append(point(edge + ".required", required[i][j], 9 if suffix == "yaml" else 11))
    return expected


class TargetRequiredExplanationTests(unittest.TestCase):
    def run_cli(self, path, compare, *options):
        target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                   str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                  ["--provider", "podman-compose", "--provider-version", "1.6.0"])
        return subprocess.run([*CLI, str(path), *target, *options], text=True,
                              capture_output=True, timeout=15)

    def assert_targets(self, findings, suffix, compare=False, version="1.6.0"):
        items = [f for f in findings if f["ruleId"] == "DEP002"]
        self.assertEqual([(f["service"], f["details"]["target"]) for f in items],
                         [(c, t) for c in CONSUMERS for t in TARGETS])
        # Check all fields first so the red regression directly proves the omitted key.
        for item in items:
            expected = locations(suffix, item["service"], item["details"]["target"])
            self.assertEqual(item["explanation"]["fieldPaths"], [p["path"] for p in expected])
            self.assertEqual(item["explanation"]["fieldLocations"], expected)
        digest = hashlib.sha256(files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()).hexdigest()
        for item in items:
            explanation = item["explanation"]
            target = item["details"]["target"]
            disabled = target in ("disabled", "none")
            self.assertEqual(item["severity"], "error" if disabled else "warning")
            self.assertEqual(item["confidence"], "high" if disabled else "unverified")
            self.assertEqual(item["details"]["healthcheckState"], "disabled" if disabled else "image-healthcheck-unverified")
            index = CONSUMERS.index(item["service"])
            self.assertIn(PHRASES[index], explanation["reason"])
            self.assertIn("optional dependency" if index == 1 else "required dependency", explanation["reason"])
            self.assertIn(BOUND, explanation["reason"])
            self.assertIn({"disabled": "disable is true", "none": "test is [NONE]"}.get(
                target, "inherited image healthcheck remains unknown"), explanation["reason"])
            self.assertEqual(explanation["evidenceScope"], {
                "kind": "structural", "provider": None, "observedVersions": [], "versionMatch": "not-applicable"})
            self.assertEqual(explanation["localEvidence"], {
                "kind": "curated-catalog", "package": "stackwrench.podman_audit", "resource": "catalog.json",
                "jsonPointer": "/rules/DEP002", "sha256": digest})
            self.assertEqual(explanation["adviceScope"], {
                "runtimeVerificationRequired": True, "guaranteesRuntimeSuccess": False})
            if compare:
                self.assertNotIn("target", explanation)
                self.assertNotIn("1.6.0", json.dumps(explanation))
                self.assertNotIn("1.6.1", json.dumps(explanation))
            else:
                self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": version})

    def test_explicit_required_coordinates_defaults_and_additive_reports(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / "examples/podman_audit" / ("target-required." + suffix))
            original = copy.deepcopy(model)
            for compare, version in ((False, "1.6.0"), (False, "1.6.1"), (True, "1.6.0")):
                with self.subTest(suffix=suffix, compare=compare, version=version):
                    make = (lambda **kw: compare_profiles(model, LEFT, RIGHT, **kw)) if compare else (
                        lambda **kw: audit(model, provider="podman-compose", provider_version=version, **kw))
                    report = make(explain=True)
                    self.assert_targets(report["structuralFindings" if compare else "findings"], suffix, compare, version)
                    self.assertEqual(report, make(explain=True))
                    self.assertEqual(report["summary"], dict(services=9, capabilities=13, changed=13, error=6, warning=20)
                                     if compare else dict(services=9, findings=25 + (version == "1.6.1"),
                                                          error=6, warning=19 + (version == "1.6.1")))
                    self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
                    if compare:
                        for left, right in ((RIGHT, LEFT), (RIGHT, RIGHT), (LEFT | {"kind": "custom", "rules": {}}, RIGHT)):
                            self.assertEqual(report["structuralFindings"], compare_profiles(model, left, right, explain=True)["structuralFindings"])
                        for row in report["capabilities"]:
                            for side, selected, judgment in (("left", "1.6.0", "reported-risk"), ("right", "1.6.1", "unknown")):
                                self.assertEqual(row[side]["judgment"], judgment)
                                self.assertEqual(row[side]["explanation"]["target"]["version"], selected)
                                del row[side]["explanation"]
                        for findings in report["profileFindings"].values():
                            for finding in findings:
                                del finding["explanation"]
                    for finding in report["structuralFindings" if compare else "findings"]:
                        del finding["explanation"]
                    self.assertEqual(report, make())
            self.assertEqual(model, original)

    def test_human_json_cli_coordinates_and_reasons(self):
        for suffix in ("yaml", "json"):
            for compare in (False, True):
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, compare=compare, json_mode=json_mode):
                        result = self.run_cli(ROOT / "examples/podman_audit" / ("target-required." + suffix), compare,
                                              "--explain", *(["--json"] if json_mode else []))
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            report = json.loads(result.stdout)
                            self.assert_targets(report["structuralFindings" if compare else "findings"], suffix, compare)
                        elif compare:
                            findings = [json.loads(line) for line in result.stdout.splitlines()
                                        if line.startswith('  {"category": "structural"')]
                            self.assert_targets(findings, suffix, True)
                        else:
                            for consumer in CONSUMERS:
                                for target in TARGETS:
                                    for field in locations(suffix, consumer, target):
                                        detail = (f"line {field['line']}, column {field['column']}" if field["declaration"] == "present" else "absent declaration")
                                        self.assertIn(f"Declaration: {field['path']} — {detail}", result.stdout)
                            for phrase in (*PHRASES, BOUND):
                                self.assertIn(phrase, result.stdout)

    def test_disabling_order_negative_edges_and_unavailable_source(self):
        for required in (None, False, True):
            options = {"condition": "service_healthy"} | ({} if required is None else {"required": required})
            for health, fields in (({"test": ["NONE"], "disable": True}, ["disable", "test"]),
                                   ({"test": ["NONE"], "disable": False}, ["test"]),
                                   ({"test": ["CMD", "SECRET_DO_NOT_ECHO"], "disable": True}, ["disable"]),
                                   ({"test": "NONE", "disable": False}, [])):
                with self.subTest(required=required, health=health):
                    model = {"services": {"api": {"depends_on": {"db": options}}, "db": {"healthcheck": health}}}
                    report = compare_profiles(model, LEFT, RIGHT, explain=True)
                    if not fields:
                        self.assertEqual(report["structuralFindings"], [])
                        continue
                    item = report["structuralFindings"][0]
                    self.assertEqual(item["severity"], "error")
                    expected = ["$.services.api.depends_on.db.condition", "$.services.db.healthcheck"]
                    expected += ["$.services.db.healthcheck." + key for key in fields]
                    expected += [] if required is None else ["$.services.api.depends_on.db.required"]
                    self.assertEqual(item["explanation"]["fieldPaths"], expected)
                    self.assertEqual(item["explanation"]["fieldLocations"], [
                        {"path": p, "declaration": "unavailable", "line": None, "column": None} for p in expected])
                    self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
            for condition in ("service_started", "service_completed_successfully"):
                model = load_document(json.dumps({"services": {"api": {"depends_on": {"db": options | {"condition": condition}}},
                                                              "db": {"healthcheck": {"disable": True}}}}), format="json")
                self.assertEqual(compare_profiles(model, LEFT, RIGHT, explain=True)["structuralFindings"], [])
        model = load_document('services: {api: {depends_on: [db]}, db: {healthcheck: {disable: true}}}')
        self.assertEqual(compare_profiles(model, LEFT, RIGHT, explain=True)["structuralFindings"], [])

    def test_thresholds_and_malformed_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.json"
            for compare in (False, True):
                for explain in ([], ["--explain"]):
                    for threshold, expected in (("error", 1), ("warning", 1), ("never", 0)):
                        result = self.run_cli(ROOT / "examples/podman_audit/target-required.yaml", compare, "--json", "--fail-on", threshold, *explain)
                        self.assertEqual(result.returncode, expected, result.stderr)
                    for key, value in (("required", "false"), ("required", 0), ("required", None),
                                       ("disable", "true"), ("test", ["NONE", "extra"]), ("healthcheck", [])):
                        options = {"condition": "service_healthy", "required": value if key == "required" else False}
                        health = {"disable": True}
                        if key not in ("required", "healthcheck"):
                            health[key] = value
                        bad.write_text(json.dumps({"services": {"api": {"depends_on": {"db": options}},
                                                                "db": {"healthcheck": value if key == "healthcheck" else health}}}))
                        result = self.run_cli(bad, compare, "--json", "--fail-on", "never", *explain)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        error = json.loads(result.stdout)
                        self.assertEqual(error["assessment"], "not-evaluated")
                        expected = "$.services.api.depends_on.db.required" if key == "required" else "$.services.db.healthcheck" + ("" if key == "healthcheck" else "." + key)
                        self.assertEqual(error["error"]["path"], expected)
                        self.assertNotIn("findings", error)
                        self.assertNotIn("structuralFindings", error)


if __name__ == "__main__":
    unittest.main()
