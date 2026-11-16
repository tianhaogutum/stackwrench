"""Synthetic integration cases for opt-in, locally traceable explanations."""

import copy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys
import unittest

from stackwrench.podman_audit.audit import audit, catalog
from stackwrench.podman_audit.input import load_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]


def explain_fixture(name, version="1.6.0"):
    model = load_document((ROOT / "examples/podman_audit" / name).read_text())
    return audit(model, provider="podman-compose", provider_version=version, explain=True)


class ExplanationTests(unittest.TestCase):
    def assert_local_source(self, finding):
        source = finding["explanation"]["localEvidence"]
        self.assertEqual(source["package"], "stackwrench.podman_audit")
        self.assertEqual(source["resource"], "catalog.json")
        self.assertEqual(source["kind"], "curated-catalog")
        raw = files(source["package"]).joinpath(source["resource"]).read_bytes()
        self.assertEqual(source["sha256"], hashlib.sha256(raw).hexdigest())
        record = json.loads(raw)
        for part in source["jsonPointer"].split("/")[1:]:
            record = record[part]
        self.assertEqual(record, catalog()["rules"][finding["ruleId"]])
        self.assertEqual(record["observedVersions"], finding["evidence"]["observedVersions"])

    def test_health_fixtures_explain_exact_trigger_and_local_evidence(self):
        for fixture, rule, field in (
            ("health-dependency.yaml", "PC1541", "$.services.api.depends_on.db.condition"),
            ("healthcheck-options.yaml", "PC1500", "$.services.db.healthcheck.start_interval"),
        ):
            with self.subTest(fixture=fixture):
                report = explain_fixture(fixture)
                self.assertEqual(len(report["findings"]), 1)
                finding = report["findings"][0]
                self.assertEqual(finding["ruleId"], rule)
                explanation = finding["explanation"]
                expected_fields = [field]
                if rule == "PC1500":
                    expected_fields.append("$.services.db.healthcheck.start_period")
                self.assertEqual(explanation["fieldPaths"], expected_fields)
                self.assertEqual(explanation["target"], {"provider": "podman-compose", "version": "1.6.0"})
                self.assertEqual(explanation["evidenceScope"], {
                    "kind": "provider-report", "provider": "podman-compose",
                    "versionMatch": "exact", "observedVersions": ["1.6.0"],
                })
                self.assertIn("4.9.3", finding["evidence"]["observationContext"])
                self.assertTrue(explanation["reason"])
                self.assertTrue(explanation["adviceScope"]["runtimeVerificationRequired"])
                self.assertFalse(explanation["adviceScope"]["guaranteesRuntimeSuccess"])
                self.assert_local_source(finding)

    def test_unknown_versions_keep_selected_and_observed_versions_distinct(self):
        for version in ("1.6.1", "1.6.0-rc.1", "1.6.0+local"):
            with self.subTest(version=version):
                report = explain_fixture("health-dependency.yaml", version)
                self.assertEqual(report["versionCoverage"], "unverified")
                self.assertEqual({f["ruleId"] for f in report["findings"]}, {"PC1541", "CAT001"})
                for finding in report["findings"]:
                    explanation = finding["explanation"]
                    self.assertEqual(explanation["target"]["version"], version)
                    self.assertEqual(explanation["evidenceScope"]["observedVersions"], ["1.6.0"])
                    self.assertEqual(explanation["evidenceScope"]["versionMatch"], "unknown")
                    self.assertEqual(finding["confidence"], "unverified")
                    self.assertFalse(explanation["adviceScope"]["guaranteesRuntimeSuccess"])
                    self.assert_local_source(finding)
                coverage = next(f for f in report["findings"] if f["ruleId"] == "CAT001")
                self.assertEqual(coverage["explanation"]["fieldPaths"], [])

    def test_structural_rules_explain_graph_evidence_without_provider_match(self):
        report = explain_fixture("structural-errors.yaml", "9.0.0")
        structural = [f for f in report["findings"] if f["category"] == "structural"]
        self.assertEqual({f["ruleId"] for f in structural}, {"DEP001", "DEP002", "DEP003"})
        for finding in structural:
            explanation = finding["explanation"]
            self.assertEqual(explanation["evidenceScope"], {
                "kind": "structural", "provider": None,
                "versionMatch": "not-applicable", "observedVersions": [],
            })
            self.assertTrue(explanation["fieldPaths"])
            self.assert_local_source(finding)
        disabled = next(f for f in structural if f["ruleId"] == "DEP002")
        self.assertEqual(disabled["explanation"]["fieldPaths"], [
            "$.services.api.depends_on.db.condition", "$.services.db.healthcheck",
            "$.services.db.healthcheck.disable"])
        self.assertIn("disabled", disabled["explanation"]["reason"])

    def test_escaped_paths_inheritance_and_disabled_option_are_explained(self):
        model = load_document('''services:
  api-v1: {depends_on: {db.primary: {condition: service_healthy}}}
  db.primary: {healthcheck: {disable: true, start_interval: 1s}}
''')
        report = audit(model, provider="podman-compose", provider_version="1.6.0", explain=True)
        by_rule = {f["ruleId"]: f for f in report["findings"]}
        self.assertEqual(by_rule["PC1541"]["explanation"]["fieldPaths"], [
            '$.services["api-v1"].depends_on["db.primary"].condition'])
        self.assertIn("disabled", by_rule["PC1500"]["explanation"]["reason"])
        model["services"]["db.primary"] = {}
        inherited = audit(model, provider="podman-compose", provider_version="1.6.0", explain=True)
        finding = next(f for f in inherited["findings"] if f["ruleId"] == "DEP002")
        self.assertIn("image", finding["explanation"]["reason"])
        self.assertEqual(finding["confidence"], "unverified")

    def test_explaining_is_deterministic_additive_and_does_not_echo_commands(self):
        model = load_document((ROOT / "examples/podman_audit/compose.yaml").read_text())
        model["services"]["db"]["healthcheck"]["test"] = ["CMD-SHELL", "SECRET_DO_NOT_ECHO"]
        original = copy.deepcopy(model)
        plain = audit(model, provider="podman-compose", provider_version="1.6.0")
        explained = audit(model, provider="podman-compose", provider_version="1.6.0", explain=True)
        self.assertEqual(model, original)
        self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(explained))
        model["services"] = dict(reversed(list(model["services"].items())))
        self.assertEqual(explained, audit(model, provider="podman-compose", provider_version="1.6.0", explain=True))
        for finding in explained["findings"]:
            del finding["explanation"]
        self.assertEqual(explained, plain)


class ExplanationCLITests(unittest.TestCase):
    def run_cli(self, fixture="health-dependency.yaml", *extra):
        return subprocess.run([*CLI, str(ROOT / "examples/podman_audit" / fixture), "--provider", "podman-compose",
                               "--provider-version", "1.6.0", "--explain", *extra],
                              text=True, capture_output=True, timeout=15)

    def test_installed_json_and_human_explanations(self):
        first = self.run_cli("health-dependency.yaml", "--json")
        second = self.run_cli("health-dependency.yaml", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout), explain_fixture("health-dependency.yaml"))
        human = self.run_cli("healthcheck-options.yaml")
        self.assertEqual(human.returncode, 0, human.stderr)
        for text in ("$.services.db.healthcheck.start_interval", "Selected target: podman-compose 1.6.0",
                     "Evidence match: exact", "catalog.json#/rules/PC1500", "sha256=",
                     "not a runtime success guarantee"):
            self.assertIn(text, human.stdout)

    def test_explanation_does_not_change_failure_thresholds_or_hide_errors(self):
        warning = self.run_cli("healthcheck-options.yaml", "--json", "--fail-on", "warning")
        self.assertEqual(warning.returncode, 1, warning.stderr)
        structural = self.run_cli("structural-errors.yaml", "--json")
        self.assertEqual(structural.returncode, 1, structural.stderr)
        for extra, code in ((["--provider", "unknown"], "unknown-provider"),
                            (["--provider-version", "latest"], "invalid-version")):
            with self.subTest(extra=extra):
                result = self.run_cli("health-dependency.yaml", "--json", "--fail-on", "never", *extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                error = json.loads(result.stdout)
                self.assertEqual(error["assessment"], "not-evaluated")
                self.assertEqual(error["error"]["code"], code)
                self.assertNotIn("findings", error)
                if code == "unknown-provider":
                    self.assertIn("unverified", error["error"]["message"])
        bad = self.run_cli("unsupported.yaml", "--json", "--fail-on", "never")
        self.assertEqual(bad.returncode, 2, bad.stderr)
        self.assertEqual(json.loads(bad.stdout)["error"]["code"], "unsupported")

    def test_unknown_version_json_is_explicitly_unverified(self):
        result = self.run_cli("healthcheck-options.yaml", "--provider-version", "1.6.1", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["compatibility"], "not-established")
        self.assertEqual(report["versionCoverage"], "unverified")
        self.assertTrue(all(f["explanation"]["evidenceScope"]["versionMatch"] == "unknown"
                            for f in report["findings"]))

    def test_json_input_and_clean_explanations(self):
        result = self.run_cli("compose.json", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        findings = json.loads(result.stdout)["findings"]
        self.assertEqual({f["ruleId"] for f in findings}, {"PC1541", "PC1500"})
        self.assertTrue(all(f["explanation"]["evidenceScope"]["versionMatch"] == "exact" for f in findings))
        clean = self.run_cli("clean.yaml", "--json", "--fail-on", "warning")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        report = json.loads(clean.stdout)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["compatibility"], "not-established")


if __name__ == "__main__":
    unittest.main()
