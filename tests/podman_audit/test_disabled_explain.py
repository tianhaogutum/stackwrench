"""DEP002 locates only declarations that actually disable a healthy target.

Both source formats retain the condition and block, then .disable for true and
.test for [NONE] (both in that order if both apply). String 'NONE' is a command,
not the disabling list. No command values are echoed. Audits retain their target;
shared comparisons omit it. Plain output/judgments/thresholds stay unchanged.
Malformed healthcheck types still exit 2 even with --fail-on never.
"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.podman_audit.audit import audit
from stackwrench.podman_audit.compare import compare_profiles
from stackwrench.podman_audit.input import read_document, load_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]
LEFT = {"schemaVersion": 1, "kind": "builtin", "provider": "podman-compose", "version": "1.6.0"}
RIGHT = LEFT | {"version": "1.6.1"}


def locations(suffix, target):
    points = {"yaml": {"cache": ((5, 9), (9, 5), (10, 7)),
                       "db.primary": ((7, 9), (12, 5), (13, 7))},
              "json": {"cache": ((6, 11), (14, 7), (15, 9)),
                       "db.primary": ((9, 11), (21, 7), (22, 9))}}
    base = "$.services.cache.healthcheck" if target == "cache" else '$.services["db.primary"].healthcheck'
    condition = "$.services.api.depends_on.cache.condition" if target == "cache" else '$.services.api.depends_on["db.primary"].condition'
    return [{"path": path, "declaration": "present", "line": line, "column": column}
            for path, (line, column) in zip((condition, base, base + (".test" if target == "cache" else ".disable")),
                                           points[suffix][target])]


class DisabledExplanationTests(unittest.TestCase):
    def run_cli(self, path, compare, *options):
        target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                   str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                  ["--provider", "podman-compose", "--provider-version", "1.6.0"])
        return subprocess.run([*CLI, str(path), *target, *options], text=True,
                              capture_output=True, timeout=15)

    def assert_disabled(self, findings, suffix, compare):
        disabled = [f for f in findings if f["ruleId"] == "DEP002"]
        self.assertEqual([f["details"]["target"] for f in disabled], ["cache", "db.primary"])
        for item in disabled:
            explanation = item["explanation"]
            expected = locations(suffix, item["details"]["target"])
            self.assertEqual(explanation["fieldPaths"], [p["path"] for p in expected])
            self.assertEqual(explanation["fieldLocations"], expected)
            self.assertIn("test is [NONE]" if item["details"]["target"] == "cache" else "disable is true",
                          explanation["reason"])
            self.assertEqual(item["severity"], "error")
            self.assertEqual(item["confidence"], "high")
            self.assertEqual(explanation["evidenceScope"]["versionMatch"], "not-applicable")
            self.assertEqual(explanation["localEvidence"]["jsonPointer"], "/rules/DEP002")
            if compare:
                self.assertNotIn("target", explanation)
                self.assertNotIn("1.6.0", json.dumps(explanation))
                self.assertNotIn("1.6.1", json.dumps(explanation))
            else:
                self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": "1.6.0"})

    def test_exact_disabling_declarations_are_additive_in_audit_and_comparison(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / "examples/podman_audit" / ("disabled-healthchecks." + suffix))
            original = copy.deepcopy(model)
            for compare in (False, True):
                with self.subTest(suffix=suffix, compare=compare):
                    make_report = (lambda **kw: compare_profiles(model, LEFT, RIGHT, **kw)) if compare else (
                        lambda **kw: audit(model, provider="podman-compose", provider_version="1.6.0", **kw))
                    plain = make_report()
                    report = make_report(explain=True)
                    self.assert_disabled(report["structuralFindings" if compare else "findings"], suffix, compare)
                    self.assertEqual(report, make_report(explain=True))
                    self.assertEqual(report["summary"], {"services": 3, "capabilities": 2, "changed": 2,
                                                       "error": 2, "warning": 3} if compare else
                                     {"services": 3, "findings": 4, "error": 2, "warning": 2})
                    self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
                    if compare:
                        self.assertEqual(report["structuralFindings"],
                                         compare_profiles(model, RIGHT, LEFT, explain=True)["structuralFindings"])
                        for row in report["capabilities"]:
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

    def test_cli_yaml_json_audit_comparison_and_human_coordinates(self):
        for suffix in ("yaml", "json"):
            path = ROOT / "examples/podman_audit" / ("disabled-healthchecks." + suffix)
            for compare in (False, True):
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, compare=compare, json_mode=json_mode):
                        result = self.run_cli(path, compare, "--explain", *(["--json"] if json_mode else []))
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            report = json.loads(result.stdout)
                            self.assert_disabled(report["structuralFindings" if compare else "findings"], suffix, compare)
                        elif compare:
                            findings = [json.loads(line) for line in result.stdout.splitlines()
                                        if line.startswith('  {"category": "structural"')]
                            self.assert_disabled(findings, suffix, compare)
                        else:
                            for target in ("cache", "db.primary"):
                                for item in locations(suffix, target):
                                    self.assertIn(f"Declaration: {item['path']} — line {item['line']}, column {item['column']}", result.stdout)
                            self.assertIn("disable is true", result.stdout)
                            self.assertIn("test is [NONE]", result.stdout)

    def test_only_active_disabling_fields_are_located_in_stable_order(self):
        for health, fields, phrases in (
            ({"disable": True}, ["disable"], ["disable is true"]),
            ({"disable": False, "test": ["NONE"]}, ["test"], ["test is [NONE]"]),
            ({"test": ["NONE"], "disable": True}, ["disable", "test"], ["disable is true", "test is [NONE]"]),
            ({"disable": True, "test": ["CMD", "SECRET_DO_NOT_ECHO"]}, ["disable"], ["disable is true"]),
        ):
            with self.subTest(health=health):
                model = load_document(json.dumps({"services": {"api": {"depends_on": {"db": {"condition": "service_healthy"}}},
                                                              "db": {"healthcheck": health}}}), format="json")
                report = compare_profiles(model, LEFT, RIGHT, explain=True)
                item = report["structuralFindings"][0]["explanation"]
                self.assertEqual(item["fieldPaths"][2:], ["$.services.db.healthcheck." + key for key in fields])
                self.assertEqual([p["path"] for p in item["fieldLocations"]], item["fieldPaths"])
                self.assertTrue(all(p["declaration"] == "present" for p in item["fieldLocations"]))
                for phrase in phrases:
                    self.assertIn(phrase, item["reason"])
                self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
        for health in ({"disable": False, "test": "NONE"}, {"test": ["CMD", "probe"]}):
            model = load_document(json.dumps({"services": {"api": {"depends_on": {"db": {"condition": "service_healthy"}}},
                                                          "db": {"healthcheck": health}}}), format="json")
            self.assertEqual(compare_profiles(model, LEFT, RIGHT, explain=True)["structuralFindings"], [])

    def test_thresholds_and_invalid_healthcheck_types(self):
        path = ROOT / "examples/podman_audit/disabled-healthchecks.yaml"
        for compare in (False, True):
            for threshold, expected in (("error", 1), ("warning", 1), ("never", 0)):
                result = self.run_cli(path, compare, "--explain", "--json", "--fail-on", threshold)
                self.assertEqual(result.returncode, expected, result.stderr)
            with tempfile.TemporaryDirectory() as temp:
                bad = Path(temp) / "bad.json"
                for key, value in (("disable", "true"), ("disable", 1), ("test", None), ("test", ["NONE", "extra"])):
                    bad.write_text(json.dumps({"services": {"db": {"healthcheck": {key: value}}}}))
                    result = self.run_cli(bad, compare, "--explain", "--json", "--fail-on", "never")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    error = json.loads(result.stdout)
                    self.assertEqual(error["assessment"], "not-evaluated")
                    self.assertEqual(error["error"]["path"], "$.services.db.healthcheck." + key)
                    self.assertNotIn("findings", error)
                    self.assertNotIn("structuralFindings", error)


if __name__ == "__main__":
    unittest.main()
