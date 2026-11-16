"""DEP001 locates declared conditions without changing missing-target severity."""

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
TARGETS = ("short_missing", "implicit_missing", "optional_missing", "required_missing")
CONDITIONS = (None, "service_completed_successfully", "service_healthy", "service_started")
REQUIREMENTS = ("short-form", "required defaults to true", "required is false", "required is true")
BOUND = "not a runtime result or evidence of ordering, completion, health, recovery or provider enforcement"


def locations(suffix):
    base = '$.services["api-v1"].depends_on.'
    paths = ("$.services.worker.depends_on[0]", base + "implicit_missing",
             base + "optional_missing", base + "required_missing")
    points = (((20, 9),), ((11, 7), (12, 9)), ((5, 7), (7, 9), (6, 9)),
              ((8, 7), (9, 9), (10, 9))) if suffix == "yaml" else (
              ((28, 9),), ((14, 9), (15, 11)), ((6, 9), (8, 11), (7, 11)),
              ((10, 9), (11, 11), (12, 11)))
    keys = (("",), ("", ".condition"), ("", ".required", ".condition"), ("", ".required", ".condition"))
    return [[{"path": path + key, "declaration": "present", "line": line, "column": column}
             for key, (line, column) in zip(fields, coords)]
            for path, fields, coords in zip(paths, keys, points)]


class MissingConditionExplanationTests(unittest.TestCase):
    def run_cli(self, path, compare, *options):
        target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                   str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                  ["--provider", "podman-compose", "--provider-version", "1.6.0"])
        return subprocess.run([*CLI, str(path), *target, *options], text=True,
                              capture_output=True, timeout=15)

    def assert_missing(self, findings, suffix, compare):
        missing = [f for f in findings if f["ruleId"] == "DEP001"]
        self.assertEqual([f["details"]["target"] for f in missing], list(TARGETS))
        digest = hashlib.sha256(files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()).hexdigest()
        for item, expected in zip(missing, locations(suffix)):
            self.assertEqual(item["explanation"]["fieldPaths"], [p["path"] for p in expected])
            self.assertEqual(item["explanation"]["fieldLocations"], expected)
        for item, condition, requirement in zip(missing, CONDITIONS, REQUIREMENTS):
            explanation = item["explanation"]
            phrase = "the declared condition is " + condition if condition else "the short-form condition defaults to service_started"
            for phrase in (phrase, requirement, "target absent from this document's services map", BOUND):
                self.assertIn(phrase, explanation["reason"])
            optional = item["details"]["target"] == "optional_missing"
            self.assertEqual(item["details"], {"target": item["details"]["target"], "required": not optional})
            severity = "warning" if optional else "error"
            self.assertEqual((item["severity"], item["confidence"]), (severity, "high"))
            self.assertIn("so its severity is " + severity, explanation["reason"])
            self.assertEqual(explanation["evidenceScope"], {
                "kind": "structural", "provider": None, "observedVersions": [], "versionMatch": "not-applicable"})
            self.assertEqual(explanation["localEvidence"], {
                "kind": "curated-catalog", "package": "stackwrench.podman_audit", "resource": "catalog.json",
                "jsonPointer": "/rules/DEP001", "sha256": digest})
            self.assertEqual(explanation["adviceScope"], {
                "runtimeVerificationRequired": True, "guaranteesRuntimeSuccess": False})
            if compare:
                self.assertNotIn("target", explanation)
                self.assertNotIn("1.6.0", json.dumps(explanation))
                self.assertNotIn("1.6.1", json.dumps(explanation))
            else:
                self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": "1.6.0"})

    def test_declared_condition_coordinates_and_additive_reports(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / "examples/podman_audit" / ("missing-conditions." + suffix))
            original = copy.deepcopy(model)
            for compare in (False, True):
                with self.subTest(suffix=suffix, compare=compare):
                    make = (lambda **kw: compare_profiles(model, LEFT, RIGHT, **kw)) if compare else (
                        lambda **kw: audit(model, provider="podman-compose", provider_version="1.6.0", **kw))
                    plain, report = make(), make(explain=True)
                    findings = report["structuralFindings" if compare else "findings"]
                    self.assert_missing(findings, suffix, compare)
                    self.assertEqual([(f["service"], f["details"]["cycleServices"]) for f in findings
                                      if f["ruleId"] == "DEP003"], [("solo", ["solo"])])
                    self.assertEqual(report, make(explain=True))
                    self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
                    self.assertEqual(report["summary"], {"services": 5, "capabilities": 4, "changed": 4,
                                                       "error": 4, "warning": 7} if compare else
                                     {"services": 5, "findings": 10, "error": 4, "warning": 6})
                    if compare:
                        baseline = report["structuralFindings"]
                        for left, right in ((RIGHT, LEFT), (RIGHT, RIGHT),
                                            (LEFT | {"kind": "custom", "rules": {}}, RIGHT)):
                            self.assertEqual(compare_profiles(model, left, right, explain=True)["structuralFindings"], baseline)
                        for row in report["capabilities"]:
                            for side, version, judgment in (("left", "1.6.0", "reported-risk"), ("right", "1.6.1", "unknown")):
                                self.assertEqual(row[side]["judgment"], judgment)
                                self.assertEqual(row[side]["explanation"]["target"]["version"], version)
                                del row[side]["explanation"]
                        for items in report["profileFindings"].values():
                            for item in items:
                                del item["explanation"]
                    for item in findings:
                        del item["explanation"]
                    self.assertEqual(report, plain)
            self.assertEqual(model, original)

    def test_human_json_cli_coordinates_and_reasons(self):
        for suffix in ("yaml", "json"):
            path = ROOT / "examples/podman_audit" / ("missing-conditions." + suffix)
            for compare in (False, True):
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, compare=compare, json_mode=json_mode):
                        result = self.run_cli(path, compare, "--explain", *(["--json"] if json_mode else []))
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            report = json.loads(result.stdout)
                            self.assert_missing(report["structuralFindings" if compare else "findings"], suffix, compare)
                        elif compare:
                            shared = [json.loads(line) for line in result.stdout.splitlines()
                                      if line.startswith('  {"category": "structural"')]
                            self.assert_missing(shared, suffix, compare)
                        else:
                            for fields in locations(suffix):
                                for field in fields:
                                    self.assertIn(f"Declaration: {field['path']} — line {field['line']}, column {field['column']}", result.stdout)
                            for condition in CONDITIONS[1:]:
                                self.assertIn("the declared condition is " + condition, result.stdout)
                            self.assertIn("the short-form condition defaults to service_started", result.stdout)
                            self.assertIn(BOUND, result.stdout)

    def test_plain_mapping_short_default_and_present_target_negative(self):
        for dependencies, keys in (({"absent.db": {"condition": "service_completed_successfully", "required": False}},
                                    ["required", "condition"]),
                                   ({"absent.db": {"condition": "service_started"}}, ["condition"]),
                                   (["absent.db"], [])):
            for loaded in (False, True):
                for compare in (False, True):
                    with self.subTest(dependencies=dependencies, loaded=loaded, compare=compare):
                        model = {"services": {"api.v1": {"depends_on": dependencies}}}
                        if loaded:
                            model = load_document(json.dumps(model), format="json")
                        report = compare_profiles(model, LEFT, RIGHT, explain=True) if compare else audit(
                            model, provider="podman-compose", provider_version="1.6.0", explain=True)
                        item, = report["structuralFindings" if compare else "findings"]
                        base = '$.services["api.v1"].depends_on' + ('[0]' if isinstance(dependencies, list) else '["absent.db"]')
                        explanation = item["explanation"]
                        self.assertEqual(explanation["fieldPaths"], [base, *[base + "." + k for k in keys]])
                        self.assertTrue(all(p["declaration"] == ("present" if loaded else "unavailable")
                                            for p in explanation["fieldLocations"]))
                        if not loaded:
                            self.assertTrue(all(p["line"] is None and p["column"] is None for p in explanation["fieldLocations"]))
                        model["services"]["absent.db"] = {}
                        report = compare_profiles(model, LEFT, RIGHT, explain=True) if compare else audit(
                            model, provider="podman-compose", provider_version="1.6.0", explain=True)
                        self.assertEqual(report["structuralFindings" if compare else "findings"], [])

    def test_thresholds_and_malformed_condition_required_healthcheck(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "compose.json"
            for condition in CONDITIONS[1:]:
                for required in (False, True, None):
                    options = {"condition": condition}
                    if required is not None:
                        options["required"] = required
                    path.write_text(json.dumps({"services": {"api": {"depends_on": {"missing": options}}}}))
                    for compare in (False, True):
                        for explain in ([], ["--explain"]):
                            for threshold, expected in (("error", 0 if required is False else 1), ("warning", 1), ("never", 0)):
                                result = self.run_cli(path, compare, "--json", "--fail-on", threshold, *explain)
                                self.assertEqual(result.returncode, expected, result.stderr)
            for key, value in (("condition", "started"), ("condition", None), ("condition", []),
                               ("condition", False), ("missing-condition", None),
                               ("required", "false"), ("required", 0), ("required", None),
                               ("disable", "true"), ("test", ["NONE", "extra"])):
                service = {"depends_on": {"missing": {"condition": "service_started", "required": False}}}
                if key in ("disable", "test"):
                    service["healthcheck"] = {key: value}
                elif key == "missing-condition":
                    del service["depends_on"]["missing"]["condition"]
                else:
                    service["depends_on"]["missing"][key] = value
                path.write_text(json.dumps({"services": {"api": service}}))
                for compare in (False, True):
                    for explain in ([], ["--explain"]):
                        result = self.run_cli(path, compare, "--json", "--fail-on", "never", *explain)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        error = json.loads(result.stdout)
                        self.assertEqual(error["assessment"], "not-evaluated")
                        self.assertEqual(error["error"]["path"], "$.services.api." + (
                            "healthcheck." + key if key in ("disable", "test") else
                            "depends_on.missing." + ("condition" if key == "missing-condition" else key)))
                        self.assertNotIn("findings", error)
                        self.assertNotIn("capabilities", error)


if __name__ == "__main__":
    unittest.main()
