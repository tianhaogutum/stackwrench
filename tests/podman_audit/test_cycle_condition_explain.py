"""DEP003 locates declared conditions while preserving the entire declared graph."""

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
SERVICES = ("job", "optional", "required", "short", "a.b")
CONDITIONS = ("service_completed_successfully", "service_started", "service_healthy", None,
              "service_completed_successfully")
REQUIREMENTS = ("required is false", "required is false", "required is true", "short-form",
                "required defaults to true")
BOUND = "not a runtime result or evidence of ordering, completion, health, recovery or provider enforcement"


def locations(suffix):
    paths = ("$.services.job.depends_on.job", "$.services.optional.depends_on.required",
             "$.services.required.depends_on.short", "$.services.short.depends_on[0]",
             '$.services["a.b"].depends_on.optional')
    points = (((29, 7), (30, 9), (31, 9)), ((8, 7), (9, 9), (10, 9)),
              ((13, 7), (15, 9), (14, 9)), ((20, 9),), ((4, 7), (5, 9))) if suffix == "yaml" else (
              ((45, 9), (46, 11), (47, 11)), ((12, 9), (13, 11), (14, 11)),
              ((20, 9), (22, 11), (21, 11)), ((31, 9),), ((5, 9), (6, 11)))
    keys = (("", ".required", ".condition"),) * 3 + (("",), ("", ".condition"))
    return [[{"path": path + key, "declaration": "present", "line": line, "column": column}
             for key, (line, column) in zip(fields, coords)]
            for path, fields, coords in zip(paths, keys, points)]


class CycleConditionExplanationTests(unittest.TestCase):
    def run_cli(self, path, compare, *options):
        target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                   str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                  ["--provider", "podman-compose", "--provider-version", "1.6.0"])
        return subprocess.run([*CLI, str(path), *target, *options], text=True,
                              capture_output=True, timeout=15)

    def assert_cycles(self, findings, suffix, compare):
        cycles = [f for f in findings if f["ruleId"] == "DEP003"]
        self.assertEqual([f["service"] for f in cycles], list(SERVICES))
        digest = hashlib.sha256(files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()).hexdigest()
        for item, expected, condition, requirement in zip(cycles, locations(suffix), CONDITIONS, REQUIREMENTS):
            explanation = item["explanation"]
            self.assertEqual(explanation["fieldPaths"], [p["path"] for p in expected])
            self.assertEqual(explanation["fieldLocations"], expected)
            phrase = "the declared condition is " + condition if condition else "the short-form condition defaults to service_started"
            self.assertIn(phrase, explanation["reason"])
            for phrase in (requirement, "optional edges also count", "All declared edge conditions count", BOUND):
                self.assertIn(phrase, explanation["reason"])
            self.assertEqual(item["details"], {
                "cycleServices": ["job"] if item["service"] == "job" else ["a.b", "optional", "required", "short"],
                "required": item["service"] not in ("job", "optional")})
            self.assertEqual((item["severity"], item["confidence"]), ("error", "high"))
            self.assertEqual(explanation["evidenceScope"], {
                "kind": "structural", "provider": None, "observedVersions": [], "versionMatch": "not-applicable"})
            self.assertEqual(explanation["localEvidence"], {
                "kind": "curated-catalog", "package": "stackwrench.podman_audit", "resource": "catalog.json",
                "jsonPointer": "/rules/DEP003", "sha256": digest})
            self.assertEqual(explanation["adviceScope"], {
                "runtimeVerificationRequired": True, "guaranteesRuntimeSuccess": False})
            if compare:
                self.assertNotIn("target", explanation)
                self.assertNotIn("1.6.0", json.dumps(explanation))
                self.assertNotIn("1.6.1", json.dumps(explanation))
            else:
                self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": "1.6.0"})

    def test_declared_condition_coordinates_graph_and_additive_reports(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / "examples/podman_audit" / ("cycle-conditions." + suffix))
            original = copy.deepcopy(model)
            for compare in (False, True):
                with self.subTest(suffix=suffix, compare=compare):
                    make = (lambda **kw: compare_profiles(model, LEFT, RIGHT, **kw)) if compare else (
                        lambda **kw: audit(model, provider="podman-compose", provider_version="1.6.0", **kw))
                    plain, report = make(), make(explain=True)
                    self.assert_cycles(report["structuralFindings" if compare else "findings"], suffix, compare)
                    self.assertEqual(report, make(explain=True))
                    self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
                    self.assertEqual(report["summary"], {"services": 6, "capabilities": 1, "changed": 1,
                                                       "error": 5, "warning": 2} if compare else
                                     {"services": 6, "findings": 6, "error": 5, "warning": 1})
                    if compare:
                        baseline = report["structuralFindings"]
                        for left, right in ((RIGHT, LEFT), (RIGHT, RIGHT),
                                            (LEFT | {"kind": "custom", "rules": {}}, RIGHT)):
                            self.assertEqual(compare_profiles(model, left, right, explain=True)["structuralFindings"], baseline)
                        row = report["capabilities"][0]
                        for side, version, judgment in (("left", "1.6.0", "reported-risk"), ("right", "1.6.1", "unknown")):
                            self.assertEqual(row[side]["judgment"], judgment)
                            self.assertEqual(row[side]["explanation"]["target"]["version"], version)
                            del row[side]["explanation"]
                        for items in report["profileFindings"].values():
                            for item in items:
                                del item["explanation"]
                    for item in report["structuralFindings" if compare else "findings"]:
                        del item["explanation"]
                    self.assertEqual(report, plain)
            self.assertEqual(model, original)

    def test_human_json_cli_coordinates_and_reasons(self):
        for suffix in ("yaml", "json"):
            path = ROOT / "examples/podman_audit" / ("cycle-conditions." + suffix)
            for compare in (False, True):
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, compare=compare, json_mode=json_mode):
                        result = self.run_cli(path, compare, "--explain", *(["--json"] if json_mode else []))
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            report = json.loads(result.stdout)
                            self.assert_cycles(report["structuralFindings" if compare else "findings"], suffix, compare)
                        elif compare:
                            shared = [json.loads(line) for line in result.stdout.splitlines()
                                      if line.startswith('  {"category": "structural"')]
                            self.assert_cycles(shared, suffix, compare)
                        else:
                            for fields in locations(suffix):
                                for field in fields:
                                    self.assertIn(f"Declaration: {field['path']} — line {field['line']}, column {field['column']}", result.stdout)
                            for condition in set(CONDITIONS) - {None}:
                                self.assertIn("the declared condition is " + condition, result.stdout)
                            self.assertIn("the short-form condition defaults to service_started", result.stdout)
                            self.assertIn(BOUND, result.stdout)
                            self.assertNotIn("Declaration: $.services.outside.depends_on", result.stdout)

    def test_plain_mapping_and_short_default_never_invent_a_condition_key(self):
        for dependencies, keys in (({"db.primary": {"condition": "service_completed_successfully", "required": False}},
                                    ["required", "condition"]), (["db.primary"], [])):
            for loaded in (False, True):
                with self.subTest(dependencies=dependencies, loaded=loaded):
                    model = {"services": {"db.primary": {"depends_on": dependencies},
                                          "outside": {"depends_on": ["db.primary"]}}}
                    if loaded:
                        model = load_document(json.dumps(model), format="json")
                    item, = compare_profiles(model, LEFT, RIGHT, explain=True)["structuralFindings"]
                    base = '$.services["db.primary"].depends_on' + ('[0]' if isinstance(dependencies, list) else '["db.primary"]')
                    explanation = item["explanation"]
                    self.assertEqual(explanation["fieldPaths"], [base, *[base + "." + k for k in keys]])
                    self.assertTrue(all(p["declaration"] == ("present" if loaded else "unavailable")
                                        for p in explanation["fieldLocations"]))
                    if not loaded:
                        self.assertTrue(all(p["line"] is None and p["column"] is None for p in explanation["fieldLocations"]))

    def test_thresholds_and_malformed_condition_required_healthcheck(self):
        path = ROOT / "examples/podman_audit/cycle-conditions.yaml"
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.json"
            for compare in (False, True):
                for explain in ([], ["--explain"]):
                    for threshold, expected in (("error", 1), ("warning", 1), ("never", 0)):
                        result = self.run_cli(path, compare, "--json", "--fail-on", threshold, *explain)
                        self.assertEqual(result.returncode, expected, result.stderr)
                    for key, value in (("condition", "started"), ("condition", None), ("condition", []),
                                       ("condition", False), ("missing-condition", None),
                                       ("required", "false"), ("required", 0), ("required", None),
                                       ("disable", "true"), ("test", ["NONE", "extra"])):
                        service = {"depends_on": {"db": {"condition": "service_started", "required": False}}}
                        if key in ("disable", "test"):
                            service["healthcheck"] = {key: value}
                        elif key == "missing-condition":
                            del service["depends_on"]["db"]["condition"]
                        else:
                            service["depends_on"]["db"][key] = value
                        bad.write_text(json.dumps({"services": {"db": service}}))
                        result = self.run_cli(bad, compare, "--json", "--fail-on", "never", *explain)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        error = json.loads(result.stdout)
                        self.assertEqual(error["assessment"], "not-evaluated")
                        self.assertEqual(error["error"]["path"], "$.services.db." + (
                            "healthcheck." + key if key in ("disable", "test") else
                            "depends_on.db." + ("condition" if key == "missing-condition" else key)))
                        self.assertNotIn("findings", error)
                        self.assertNotIn("capabilities", error)


if __name__ == "__main__":
    unittest.main()
