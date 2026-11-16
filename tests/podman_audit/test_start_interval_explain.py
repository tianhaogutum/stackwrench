"""PC1500 locates only active disabling declarations after start_interval.

The checked option remains first. Both disabling keys use disable/test order,
independent of input order. Active checks (including string NONE) gain no fields.
Exact source coordinates, selected targets and catalog provenance survive both
report formats/modes; commands stay private. Explanation is additive only.
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
SERVICES = ("active", "both", "disabled", "none")
FIELDS = (("start_interval",), ("start_interval", "disable", "test"),
          ("start_interval", "disable"), ("start_interval", "test"))


def locations(suffix):
    lines = ((19,), (5, 6, 4), (9, 10), (14, 16)) if suffix == "yaml" else (
        (33,), (8, 9, 5), (14, 15), (24, 26))
    return [[{"path": f"$.services.{name}.healthcheck.{key}", "declaration": "present",
              "line": line, "column": 7 if suffix == "yaml" else 9}
             for key, line in zip(fields, points)]
            for name, fields, points in zip(SERVICES, FIELDS, lines)]


class StartIntervalExplanationTests(unittest.TestCase):
    def assert_explanations(self, explanations, suffix, version):
        self.assertEqual(len(explanations), 4)
        digest = hashlib.sha256(files("stackwrench.podman_audit").joinpath("catalog.json").read_bytes()).hexdigest()
        for item, expected, fields in zip(explanations, locations(suffix), FIELDS):
            self.assertEqual(item["fieldPaths"], [p["path"] for p in expected])
            self.assertEqual(item["fieldLocations"], expected)
            self.assertEqual(item["target"], {"provider": "podman-compose", "version": version})
            self.assertEqual(item["evidenceScope"], {"kind": "provider-report", "provider": "podman-compose",
                             "observedVersions": ["1.6.0"], "versionMatch": "exact" if version == "1.6.0" else "unknown"})
            self.assertEqual(item["localEvidence"], {"kind": "curated-catalog", "package": "stackwrench.podman_audit",
                             "resource": "catalog.json", "jsonPointer": "/rules/PC1500", "sha256": digest})
            self.assertFalse(item["adviceScope"]["guaranteesRuntimeSuccess"])
            self.assertTrue(item["adviceScope"]["runtimeVerificationRequired"])
            for field, phrase in (("disable", "disable is true"), ("test", "test is [NONE]")):
                self.assertEqual(phrase in item["reason"], field in fields)
            if len(fields) > 1:
                self.assertIn("not a running probe", item["reason"])
            else:
                self.assertNotIn("disabled", item["reason"])

    def test_yaml_json_coordinates_and_additive_reports(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / "examples/podman_audit" / ("disabled-start-interval." + suffix))
            original = copy.deepcopy(model)
            for compare in (False, True):
                with self.subTest(suffix=suffix, compare=compare):
                    make = (lambda **kw: compare_profiles(model, LEFT, RIGHT, **kw)) if compare else (
                        lambda **kw: audit(model, provider="podman-compose", provider_version="1.6.0", **kw))
                    plain, explained = make(), make(explain=True)
                    self.assertEqual(explained["summary"], {"services": 4, "capabilities": 4, "changed": 4,
                                                          "error": 0, "warning": 5} if compare else
                                     {"services": 4, "findings": 4, "error": 0, "warning": 4})
                    if compare:
                        rows = explained["capabilities"]
                        self.assertEqual([r["service"] for r in rows], list(SERVICES))
                        for side, version, judgment in (("left", "1.6.0", "reported-risk"), ("right", "1.6.1", "unknown")):
                            self.assert_explanations([r[side]["explanation"] for r in rows], suffix, version)
                            self.assertTrue(all(r[side]["judgment"] == judgment for r in rows))
                            for row in rows:
                                del row[side]["explanation"]
                        self.assertEqual(explained["structuralFindings"], [])
                        for items in explained["profileFindings"].values():
                            for item in items:
                                del item["explanation"]
                    else:
                        findings = explained["findings"]
                        self.assertEqual([f["service"] for f in findings], list(SERVICES))
                        self.assertTrue(all(f["ruleId"] == "PC1500" and f["severity"] == "warning" for f in findings))
                        self.assert_explanations([f["explanation"] for f in findings], suffix, "1.6.0")
                        for item in findings:
                            del item["explanation"]
                    self.assertEqual(explained, plain)
            self.assertEqual(model, original)

    def test_human_and_json_cli_reports(self):
        for suffix in ("yaml", "json"):
            path = ROOT / "examples/podman_audit" / ("disabled-start-interval." + suffix)
            model = read_document(path)
            for compare in (False, True):
                target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                           str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                          ["--provider", "podman-compose", "--provider-version", "1.6.0"])
                expected = compare_profiles(model, LEFT, RIGHT, explain=True) if compare else audit(
                    model, provider="podman-compose", provider_version="1.6.0", explain=True)
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, compare=compare, json_mode=json_mode):
                        result = subprocess.run([*CLI, str(path), *target, "--explain", *(["--json"] if json_mode else [])],
                                                text=True, capture_output=True, timeout=15)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            self.assertEqual(json.loads(result.stdout), expected)
                            explanations = ([r["left"]["explanation"] for r in expected["capabilities"]] if compare else
                                            [f["explanation"] for f in expected["findings"]])
                        elif compare:
                            self.assertEqual(result.stdout, render_comparison(expected) + "\n")
                            explanations = [json.loads(line.removeprefix("    Explanation: ")) for line in
                                            result.stdout.splitlines() if line.startswith("    Explanation: ")][::2]
                        else:
                            self.assertEqual(result.stdout, render_human(expected) + "\n")
                            for fields in locations(suffix):
                                for field in fields:
                                    self.assertIn(f"Declaration: {field['path']} — line {field['line']}, column {field['column']}", result.stdout)
                            explanations = [f["explanation"] for f in expected["findings"]]
                        self.assert_explanations(explanations, suffix, "1.6.0")

    def test_active_boundaries_escaped_paths_and_unavailable_source(self):
        for health, fields in (({"test": "NONE", "disable": False}, []),
                               ({"disable": False}, []),
                               ({"test": ["CMD", "SECRET_DO_NOT_ECHO"]}, []),
                               ({"disable": True, "test": "SECRET_DO_NOT_ECHO"}, ["disable"]),
                               ({"test": ["NONE"], "disable": True}, ["disable", "test"])):
            for loaded in (False, True):
                with self.subTest(health=health, loaded=loaded):
                    model = {"services": {"db.primary": {"healthcheck": health | {"start_interval": "1s"}}}}
                    if loaded:
                        model = load_document(json.dumps(model), format="json")
                    report = compare_profiles(model, LEFT, RIGHT, explain=True)
                    item = report["capabilities"][0]["left"]["explanation"]
                    self.assertEqual(item["fieldPaths"], ['$.services["db.primary"].healthcheck.' + key
                                                         for key in ["start_interval", *fields]])
                    self.assertTrue(all(p["declaration"] == ("present" if loaded else "unavailable")
                                        for p in item["fieldLocations"]))
                    if not loaded:
                        self.assertTrue(all(p["line"] is None and p["column"] is None for p in item["fieldLocations"]))
                    self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))

    def test_thresholds_and_invalid_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "compose.json"
            for compare in (False, True):
                target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                           str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                          ["--provider", "podman-compose", "--provider-version", "1.6.0"])
                for explain in ([], ["--explain"]):
                    path.write_text(json.dumps({"services": {"db": {"healthcheck": {"start_interval": "1s", "disable": True}}}}))
                    for threshold, expected in (("error", 0), ("warning", 1), ("never", 0)):
                        result = subprocess.run([*CLI, str(path), *target, "--json", *explain, "--fail-on", threshold],
                                                text=True, capture_output=True, timeout=15)
                        self.assertEqual(result.returncode, expected, result.stderr)
                    for key, value in (("disable", "true"), ("disable", 1), ("test", None), ("test", ["NONE", "extra"]),
                                       ("start_interval", 1)):
                        path.write_text(json.dumps({"services": {"db": {"healthcheck": {"start_interval": "1s", key: value}}}}))
                        result = subprocess.run([*CLI, str(path), *target, "--json", *explain, "--fail-on", "never"],
                                                text=True, capture_output=True, timeout=15)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        error = json.loads(result.stdout)
                        self.assertEqual(error["assessment"], "not-evaluated")
                        self.assertEqual(error["error"]["path"], "$.services.db.healthcheck." + key)
                        self.assertNotIn("findings", error)
                        self.assertNotIn("capabilities", error)


if __name__ == "__main__":
    unittest.main()
