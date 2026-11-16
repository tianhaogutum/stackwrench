"""Offline profile comparisons using local synthetic assertions, never runtime claims."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]


def profile(name):
    return Path(name) if Path(name).is_absolute() else ROOT / "examples/podman_audit/profiles" / (name + ".json")


class CompareCLITests(unittest.TestCase):
    def run_cli(self, left="observed", right="unknown", *extra, fixture="compose.yaml", env=None):
        return subprocess.run([*CLI, str(ROOT / "examples/podman_audit" / fixture), "--compare-profiles",
                               str(profile(left)), str(profile(right)), *extra],
                              text=True, capture_output=True, timeout=15, env=env)

    def test_observed_to_unknown_never_implies_support(self):
        result = self.run_cli("observed", "unknown", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["compatibility"], "not-established")
        self.assertEqual(report["assessment"], "limited-static-profile-comparison")
        self.assertEqual([(r["path"], r["ruleId"]) for r in report["capabilities"]], [
            ("$.services.api.depends_on.db.condition", "PC1541"),
            ("$.services.db.healthcheck.start_interval", "PC1500")])
        self.assertEqual(report["summary"]["changed"], 2)
        for row in report["capabilities"]:
            self.assertTrue(row["changed"])
            self.assertEqual(row["left"]["judgment"], "reported-risk")
            self.assertEqual(row["left"]["basis"], "builtin-observation")
            self.assertEqual(row["right"]["judgment"], "unknown")
            self.assertEqual(row["right"]["basis"], "builtin-missing-evidence")
            self.assertEqual(row["right"]["evidence"]["observedVersions"], ["1.6.0"])
            self.assertEqual(row["right"]["versionMatch"], "unknown")
            self.assertEqual(row["right"]["localEvidence"]["resource"], "catalog.json")
        self.assertEqual([f["ruleId"] for f in report["profileFindings"]["right"]], ["CAT001"])
        self.assertEqual(report["structuralFindings"], [])
        self.assertEqual(result.stdout, self.run_cli("observed", "unknown", "--json").stdout)

    def test_custom_assertions_are_separate_and_omissions_remain_unknown(self):
        result = self.run_cli("observed", "custom", "--json", "--explain")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        healthy, interval = report["capabilities"]
        self.assertEqual(healthy["right"]["judgment"], "unknown")
        self.assertEqual(healthy["right"]["basis"], "custom-unspecified")
        self.assertEqual(interval["right"]["judgment"], "supported")
        self.assertEqual(interval["right"]["basis"], "custom-assertion")
        self.assertIsNone(interval["right"]["evidence"])
        self.assertEqual(interval["right"]["localEvidence"]["kind"], "custom-profile")
        self.assertEqual(interval["right"]["localEvidence"]["jsonPointer"], "/rules/PC1500")
        self.assertEqual(report["profiles"]["right"]["kind"], "custom")
        canonical = json.dumps(json.loads(profile("custom").read_text()), sort_keys=True,
                               ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(interval["right"]["localEvidence"]["sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(interval["left"]["explanation"]["evidenceScope"]["versionMatch"], "exact")
        self.assertNotIn("explanation", json.loads(self.run_cli("observed", "custom", "--json").stdout)["capabilities"][0]["left"])

    def test_equal_judgments_do_not_hide_provenance_and_structural_errors(self):
        same = self.run_cli("observed", "observed", "--json")
        self.assertEqual(same.returncode, 0, same.stderr)
        report = json.loads(same.stdout)
        self.assertEqual(report["summary"]["changed"], 0)
        self.assertEqual(len(report["capabilities"]), 2)
        bad = self.run_cli("custom", "custom", "--json", fixture="structural-errors.yaml")
        self.assertEqual(bad.returncode, 1, bad.stderr)
        self.assertEqual(json.loads(bad.stdout)["summary"]["error"], 4)
        self.assertEqual(len(json.loads(bad.stdout)["structuralFindings"]), 4)
        self.assertEqual(self.run_cli("custom", "custom", "--json", "--fail-on", "never",
                                      fixture="structural-errors.yaml").returncode, 0)
        self.assertEqual(self.run_cli("observed", "unknown", "--json", "--fail-on", "warning").returncode, 1)
        clean = self.run_cli("observed", "unknown", "--json", "--fail-on", "warning", fixture="clean.yaml")
        self.assertEqual(clean.returncode, 1, clean.stderr)  # target-wide unknown coverage
        self.assertEqual(json.loads(clean.stdout)["capabilities"], [])

    def test_human_output_shows_changed_and_unchanged_judgments_and_basis(self):
        result = self.run_cli("observed", "custom")
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ("left: builtin podman-compose 1.6.0", "right: custom podman-compose 1.6.1",
                     "reported-risk -> supported", "custom-assertion", "custom-unspecified",
                     "$.services.api.depends_on.db.condition", "not established", "issues/1500"):
            self.assertIn(text, result.stdout)

    def test_invalid_profile_shapes_and_conflicting_modes_fail_closed(self):
        good = json.loads(profile("custom").read_text())
        invalid = [{}, [], {**good, "schemaVersion": True}, {**good, "schemaVersion": 2},
                   {**good, "kind": "observed"}, {**good, "provider": "docker-compose"},
                   {**good, "version": "latest"}, {**good, "typo": True},
                   {**good, "rules": []}, {**good, "rules": {"DEP001": "supported"}},
                   {**good, "rules": {"PC1500": True}}, {**good, "rules": {"PC1500": "pass"}},
                   {**good, "rules": {"PC1500": []}}, {**good, "kind": "builtin"},
                   {**good, "kind": []}, {**good, "version": None},
                   {**good, "rules": {"CAT001": "supported"}}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            for data in invalid:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    result = self.run_cli(str(path), "unknown", "--json", "--fail-on", "never")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report["assessment"], "not-evaluated")
                    self.assertNotIn("capabilities", report)
                    self.assertIn(report["error"]["code"], ("invalid-profile", "invalid-input",
                                                          "unknown-provider", "invalid-version"))
            for data in ('{"schemaVersion":1,"schemaVersion":1}', '{bad', '{"kind":NaN}'):
                path.write_text(data)
                result = self.run_cli(str(path), "unknown", "--json")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(json.loads(result.stdout)["error"]["code"], ("invalid-input", "duplicate-key"))
            path = Path(temp) / "bad.yaml"
            for data in ('kind: &k builtin', 'kind: !reset builtin', 'kind: builtin\nkind: custom',
                         'kind: builtin\n---\nkind: custom'):
                path.write_text(data)
                result = self.run_cli(str(path), "unknown", "--json")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(json.loads(result.stdout)["error"]["code"], ("invalid-input", "unsupported", "duplicate-key"))
        for extra in (("--provider", "podman-compose"), ("--provider-version", "1.6.0")):
            result = self.run_cli("observed", "unknown", "--json", *extra)
            self.assertEqual(result.returncode, 2, result.stderr)
        result = self.run_cli("observed", "unknown", "--json", fixture="unsupported.yaml")
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_python_api_is_deterministic_nonmutating_and_handles_escaped_fields(self):
        # Import here so the CLI regressions also run against the pre-feature package.
        from stackwrench.podman_audit.compare import compare_profiles
        from stackwrench.podman_audit.input import load_document
        model = load_document('''services:
  api-v1: {depends_on: {db.primary: {condition: service_healthy}}}
  db.primary: {healthcheck: {test: [CMD, SECRET_DO_NOT_ECHO], start_interval: 1s}}
''')
        left, right = (json.loads(profile(n).read_text()) for n in ("observed", "custom"))
        original = copy.deepcopy((model, left, right))
        report = compare_profiles(model, left, right)
        self.assertEqual((model, left, right), original)
        model["services"] = dict(reversed(list(model["services"].items())))
        self.assertEqual(report, compare_profiles(model, dict(reversed(list(left.items()))), right))
        self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
        self.assertEqual(report["capabilities"][0]["path"], '$.services["api-v1"].depends_on["db.primary"].condition')

    def test_yaml_profiles_custom_defaults_and_exact_version_boundaries(self):
        from stackwrench.podman_audit.compare import compare_profiles
        from stackwrench.podman_audit.input import InputError, load_document
        model = load_document((ROOT / "examples/podman_audit/compose.yaml").read_text())
        observed = json.loads(profile("observed").read_text())
        for version in ("1.6.1", "1.6.0-rc.1", "1.6.0+local"):
            report = compare_profiles(model, observed, observed | {"version": version})
            self.assertEqual({row["right"]["judgment"] for row in report["capabilities"]}, {"unknown"})
        custom = observed | {"kind": "custom", "rules": {}}
        report = compare_profiles(model, observed, custom)
        self.assertEqual({row["right"]["judgment"] for row in report["capabilities"]}, {"unknown"})
        self.assertEqual({row["right"]["basis"] for row in report["capabilities"]}, {"custom-unspecified"})
        custom["rules"] = {"PC1541": "supported", "PC1500": "supported"}
        report = compare_profiles(model, custom, custom)
        self.assertEqual(report["summary"]["warning"], 0)
        self.assertEqual(report["summary"]["changed"], 0)
        self.assertEqual(report["compatibility"], "not-established")
        same_judgments = compare_profiles(model, observed | {"version": "1.6.1"},
                                         custom | {"rules": {"PC1500": "unknown", "PC1541": "unknown"}})
        self.assertEqual(same_judgments["summary"]["changed"], 0)
        self.assertNotEqual(same_judgments["capabilities"][0]["left"]["basis"],
                            same_judgments["capabilities"][0]["right"]["basis"])
        with self.assertRaises(InputError) as caught:
            compare_profiles(model, observed, custom | {"rules": {"PC1500": "pass"}})
        self.assertEqual(caught.exception.detail["path"], "$.rules.PC1500")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "profile.yaml"
            path.write_text('schemaVersion: 1\nkind: builtin\nprovider: podman-compose\nversion: 1.6.1\n')
            result = self.run_cli("observed", str(path), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), compare_profiles(model, observed, observed | {"version": "1.6.1"}))

    def test_comparison_process_never_runs_commands_or_connects(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "sitecustomize.py").write_text('''import sys
def guard(event, args):
    if event.startswith(("subprocess.", "socket.", "os.system", "os.exec", "os.spawn")):
        raise RuntimeError("Forbidden runtime action: " + event)
    if event == "open" and str(args[0]).endswith((".env", "Dockerfile")):
        raise RuntimeError("Forbidden implicit read")
sys.addaudithook(guard)
''')
            result = self.run_cli("observed", "custom", "--json", "--explain",
                                  env=dict(os.environ, PYTHONPATH=str(path), COMPOSE_FILE="not-read.yaml"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["capabilities"]), 2)


if __name__ == "__main__":
    unittest.main()
