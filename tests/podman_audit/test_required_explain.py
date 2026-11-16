"""DEP001 retains entry locations and locates only explicit .required keys.

True/false determine error/warning; omitted long and short forms default true
without inventing a .required declaration. JSON/human audits and comparisons
retain source coordinates and bounded static reasons. Shared explanations have
no profile target. Plain bytes, findings, judgments and thresholds are unchanged;
non-boolean required values still exit 2 even with --fail-on never.
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
from stackwrench.podman_audit.input import read_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]
LEFT = {"schemaVersion": 1, "kind": "builtin", "provider": "podman-compose", "version": "1.6.0"}
RIGHT = LEFT | {"version": "1.6.1"}


def locations(suffix):
    points = ((11, 7), (5, 7), (7, 9), (8, 7), (10, 9), (15, 9)) if suffix == "yaml" else (
        (14, 9), (6, 9), (8, 11), (10, 9), (12, 11), (21, 9))
    base = '$.services["api-v1"].depends_on.'
    paths = [base + "implicit_missing", base + "optional_missing", base + "optional_missing.required",
             base + "required_missing", base + "required_missing.required", "$.services.worker.depends_on[0]"]
    items = [{"path": path, "declaration": "present", "line": line, "column": column}
             for path, (line, column) in zip(paths, points)]
    groups = [items[5:], items[:1], items[1:3], items[3:5]]
    # The edge/required order is retained before the declared condition.
    for group, (line, column) in zip(groups[1:], ((12, 9), (6, 9), (9, 9)) if suffix == "yaml" else
                                               ((15, 11), (7, 11), (11, 11))):
        group.append({"path": group[0]["path"] + ".condition", "declaration": "present",
                      "line": line, "column": column})
    return groups


class RequiredExplanationTests(unittest.TestCase):
    def run_cli(self, path, compare, *options):
        target = (["--compare-profiles", str(ROOT / "examples/podman_audit/profiles/observed.json"),
                   str(ROOT / "examples/podman_audit/profiles/unknown.json")] if compare else
                  ["--provider", "podman-compose", "--provider-version", "1.6.0"])
        return subprocess.run([*CLI, str(path), *target, *options], text=True,
                              capture_output=True, timeout=15)

    def assert_required(self, findings, suffix, compare):
        missing = [f for f in findings if f["ruleId"] == "DEP001"]
        self.assertEqual([f["details"]["target"] for f in missing],
                         ["short_missing", "implicit_missing", "optional_missing", "required_missing"])
        for item, expected in zip(missing, locations(suffix)):
            explanation = item["explanation"]
            self.assertEqual(explanation["fieldPaths"], [p["path"] for p in expected])
            self.assertEqual(explanation["fieldLocations"], expected)
        phrases = ["short-form", "required defaults to true", "required is false", "required is true"]
        for item, phrase, severity in zip(missing, phrases,
                                                    ("error", "error", "warning", "error")):
            explanation = item["explanation"]
            self.assertIn(phrase, explanation["reason"])
            self.assertIn(severity, explanation["reason"])
            self.assertIn("not a runtime result", explanation["reason"])
            self.assertEqual(item["severity"], severity)
            self.assertEqual(item["confidence"], "high")
            self.assertEqual(explanation["evidenceScope"], {
                "kind": "structural", "provider": None, "observedVersions": [], "versionMatch": "not-applicable"})
            self.assertEqual(explanation["localEvidence"]["jsonPointer"], "/rules/DEP001")
            if compare:
                self.assertNotIn("target", explanation)
                self.assertNotIn("1.6.0", json.dumps(explanation))
                self.assertNotIn("1.6.1", json.dumps(explanation))
            else:
                self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": "1.6.0"})

    def test_required_key_coordinates_defaults_and_additive_reports(self):
        for suffix in ("yaml", "json"):
            model = read_document(ROOT / "examples/podman_audit" / ("missing-required." + suffix))
            original = copy.deepcopy(model)
            for compare in (False, True):
                with self.subTest(suffix=suffix, compare=compare):
                    make_report = (lambda **kw: compare_profiles(model, LEFT, RIGHT, **kw)) if compare else (
                        lambda **kw: audit(model, provider="podman-compose", provider_version="1.6.0", **kw))
                    plain = make_report()
                    report = make_report(explain=True)
                    self.assert_required(report["structuralFindings" if compare else "findings"], suffix, compare)
                    self.assertEqual(report, make_report(explain=True))
                    self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
                    self.assertEqual(report["summary"], {"services": 2, "capabilities": 1, "changed": 1,
                                                       "error": 3, "warning": 3} if compare else
                                     {"services": 2, "findings": 5, "error": 3, "warning": 2})
                    if compare:
                        self.assertEqual(report["structuralFindings"],
                                         compare_profiles(model, RIGHT, LEFT, explain=True)["structuralFindings"])
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

    def test_cli_yaml_json_audit_comparison_and_human_coordinates(self):
        for suffix in ("yaml", "json"):
            path = ROOT / "examples/podman_audit" / ("missing-required." + suffix)
            for compare in (False, True):
                for json_mode in (False, True):
                    with self.subTest(suffix=suffix, compare=compare, json_mode=json_mode):
                        result = self.run_cli(path, compare, "--explain", *(["--json"] if json_mode else []))
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout)
                        if json_mode:
                            report = json.loads(result.stdout)
                            self.assert_required(report["structuralFindings" if compare else "findings"], suffix, compare)
                        elif compare:
                            findings = [json.loads(line) for line in result.stdout.splitlines()
                                        if line.startswith('  {"category": "structural"')]
                            self.assert_required(findings, suffix, compare)
                        else:
                            for fields in locations(suffix):
                                for field in fields:
                                    self.assertIn(f"Declaration: {field['path']} — line {field['line']}, column {field['column']}", result.stdout)
                            for phrase in ("required is false", "required is true", "required defaults to true", "short-form"):
                                self.assertIn(phrase, result.stdout)

    def test_optional_and_default_thresholds_and_unavailable_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "compose.json"
            for options in ({"condition": "service_started", "required": False},
                            {"condition": "service_started", "required": True},
                            {"condition": "service_started"}, None):
                dependencies = {"missing": options} if options is not None else ["missing"]
                model = {"services": {"api": {"depends_on": dependencies}}}
                path.write_text(json.dumps(model))
                optional = options is not None and options.get("required") is False
                item = audit(model, provider="podman-compose", provider_version="1.6.0", explain=True)["findings"][0]
                self.assertTrue(all(p["declaration"] == "unavailable" and p["line"] is None and p["column"] is None
                                    for p in item["explanation"]["fieldLocations"]))
                for compare in (False, True):
                    for explain in ([], ["--explain"]):
                        for threshold, expected in (("error", 0 if optional else 1), ("warning", 1), ("never", 0)):
                            result = self.run_cli(path, compare, "--json", "--fail-on", threshold, *explain)
                            self.assertEqual(result.returncode, expected, result.stderr)

    def test_non_boolean_required_is_an_input_error(self):
        with tempfile.TemporaryDirectory() as temp:
            for suffix in ("yaml", "json"):
                path = Path(temp) / ("bad." + suffix)
                for value in ("false", 0, None, [], {}):
                    # JSON text is also valid restricted YAML, preserving quoted strings.
                    path.write_text(json.dumps({"services": {"api": {"depends_on": {"missing": {
                        "condition": "service_started", "required": value}}}}}))
                    for compare in (False, True):
                        for json_mode in (False, True):
                            result = self.run_cli(path, compare, "--explain", "--fail-on", "never",
                                                  *(["--json"] if json_mode else []))
                            self.assertEqual(result.returncode, 2, result.stderr)
                            if json_mode:
                                error = json.loads(result.stdout)
                                self.assertEqual(error["assessment"], "not-evaluated")
                                self.assertEqual(error["error"]["path"], "$.services.api.depends_on.missing.required")
                                self.assertNotIn("findings", error)
                                self.assertNotIn("structuralFindings", error)
                            else:
                                self.assertEqual(result.stdout, "")
                                self.assertIn("$.services.api.depends_on.missing.required", result.stderr)


if __name__ == "__main__":
    unittest.main()
