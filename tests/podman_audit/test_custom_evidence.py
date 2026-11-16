"""Custom rule metadata contract, established before implementation.

String judgments stay valid. An object requires judgment and permits only optional
evidenceReferences (nonempty list of nonblank strings) and observationNotes
(nonblank string). Both outputs preserve supplied metadata without catalog
promotion, fetching references or changing judgment-only comparisons/thresholds.
Malformed objects fail closed at exact paths, including with --fail-on never.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.podman_audit.compare import compare_profiles, render_comparison, validate_profile
from stackwrench.podman_audit.input import InputError, load_document, read_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]
BUILTIN = {"schemaVersion": 1, "kind": "builtin", "provider": "podman-compose", "version": "1.6.0"}
MODEL = """services:
  api: {depends_on: {db: {condition: service_healthy}}}
  db: {healthcheck: {test: [CMD, SECRET_DO_NOT_ECHO], start_interval: 1s}}
"""


def custom(rules, version="1.6.0"):
    return BUILTIN | {"kind": "custom", "version": version, "rules": rules}


def digest(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class CustomEvidenceTests(unittest.TestCase):
    def compare(self, left, right, **kwargs):
        return compare_profiles(load_document(MODEL), left, right, **kwargs)

    def run_cli(self, left, right, *extra, fixture="healthcheck-options.yaml", env=None):
        return subprocess.run([*CLI, str(ROOT / "examples/podman_audit" / fixture), "--compare-profiles",
                               str(left), str(right), *extra],
                              text=True, capture_output=True, timeout=15, env=env)

    def test_per_rule_metadata_on_both_sides_never_becomes_builtin_evidence(self):
        left_rules = {
            "PC1541": {"judgment": "reported-risk", "observationNotes": "Synthetic healthy gate note."},
            "PC1500": {"judgment": "supported", "evidenceReferences": ["local-run-17", "missing.txt"]},
        }
        right_rules = {
            "PC1541": {"judgment": "unknown", "evidenceReferences": ["https://example.invalid/gate"]},
            "PC1500": {"judgment": "reported-risk", "observationNotes": "Synthetic interval note."},
        }
        left, right = custom(left_rules), custom(right_rules, "1.6.1")
        originals = copy.deepcopy((left, right))
        for explain in (False, True):
            report = self.compare(left, right, explain=explain)
            self.assertEqual(report["compatibility"], "not-established")
            self.assertEqual(report["profileFindings"], {"left": [], "right": []})
            self.assertEqual(report["summary"]["changed"], 2)
            self.assertEqual(report["summary"]["warning"], 2)
            for row in report["capabilities"]:
                for side, source in (("left", left), ("right", right)):
                    assertion = source["rules"][row["ruleId"]]
                    item = row[side]
                    self.assertEqual(item["judgment"], assertion["judgment"])
                    self.assertEqual(item["basis"], "custom-assertion")
                    self.assertIsNone(item["evidence"])
                    self.assertEqual(item["localEvidence"], {
                        "kind": "custom-profile", "jsonPointer": "/rules/" + row["ruleId"],
                        "sha256": digest(source)})
                    self.assertNotIn("versionMatch", item)
                    self.assertNotIn("explanation", item)
                    for key in ("evidenceReferences", "observationNotes"):
                        if key in assertion:
                            self.assertEqual(item[key], assertion[key])
                        else:
                            self.assertNotIn(key, item)
            self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
        self.assertEqual((left, right), originals)
        report["capabilities"][1]["left"]["evidenceReferences"].append("output-only")
        self.assertEqual((left, right), originals)

    def test_minimal_objects_legacy_strings_and_omissions(self):
        for judgment in ("reported-risk", "supported", "unknown"):
            with self.subTest(judgment=judgment):
                legacy = custom({"PC1500": judgment})
                modern = custom({"PC1500": {"judgment": judgment}})
                report = self.compare(legacy, modern)
                healthy, interval = report["capabilities"]
                self.assertEqual(report["summary"]["changed"], 0)
                for item in (healthy["left"], healthy["right"]):
                    self.assertEqual(item, {"judgment": "unknown", "basis": "custom-unspecified",
                                            "evidence": None, "localEvidence": None})
                self.assertEqual(set(interval["right"]), {"judgment", "basis", "evidence", "localEvidence"})
                self.assertEqual(interval["right"]["judgment"], judgment)

    def test_metadata_changes_hash_but_not_judgments_summary_or_builtin_assessment(self):
        plain = custom({"PC1500": "supported"})
        contextual = custom({"PC1500": {"judgment": "supported", "observationNotes": "Local assertion."}})
        before, after = self.compare(BUILTIN, plain, explain=True), self.compare(BUILTIN, contextual, explain=True)
        self.assertEqual(before["summary"], after["summary"])
        self.assertEqual([r["left"] for r in before["capabilities"]], [r["left"] for r in after["capabilities"]])
        self.assertEqual(self.compare(plain, contextual)["summary"]["changed"], 0)
        updated = custom({"PC1500": {"judgment": "supported", "observationNotes": "Changed note."}})
        self.assertNotEqual(after["profiles"]["right"]["sha256"], self.compare(BUILTIN, updated)["profiles"]["right"]["sha256"])
        supported = custom({rule: {"judgment": "supported", "observationNotes": "Synthetic."}
                            for rule in ("PC1541", "PC1500")})
        self.assertEqual(self.compare(supported, supported)["summary"]["warning"], 0)

    def test_human_metadata_is_labeled_and_escaped(self):
        references = ["https://example.invalid/é", "  local-run-17  "]
        notes = '  Synthetic é🙂\nleft: builtin-observation\t\x1b[31m  '
        source = custom({"PC1500": {"judgment": "supported", "evidenceReferences": references,
                                     "observationNotes": notes}})
        report = self.compare(BUILTIN, source)
        item = report["capabilities"][1]["right"]
        self.assertEqual(item["observationNotes"], notes)
        self.assertEqual(item["evidenceReferences"], references)
        output = render_comparison(report)
        self.assertIn("right: custom-assertion", output)
        self.assertIn("Custom evidence references: " + json.dumps(references, ensure_ascii=True), output)
        self.assertIn("Custom observation notes: " + json.dumps(notes, ensure_ascii=True), output)
        self.assertNotIn("\nleft: builtin-observation", output)
        self.assertNotIn("\x1b", output)

    def test_invalid_objects_fail_at_exact_profile_paths(self):
        cases = [
            ({}, "judgment"), ({"observationNotes": "Note"}, "judgment"),
            *[({"judgment": value}, "judgment") for value in (None, True, 1, [], {}, "pass")],
            ({"judgment": "supported", "evidence": []}, "evidence"),
            ({"judgment": "supported", "basis": "builtin-observation"}, "basis"),
            *[({"judgment": "supported", "evidenceReferences": value}, "evidenceReferences")
              for value in (None, True, "url", {}, [])],
            *[({"judgment": "supported", "evidenceReferences": ["ok", value]}, "evidenceReferences[1]")
              for value in (None, True, 1, {}, [], "", " \n\t")],
            *[({"judgment": "supported", "observationNotes": value}, "observationNotes")
              for value in (None, True, 1, {}, [], "", " \n\t")],
        ]
        with tempfile.TemporaryDirectory() as temp:
            left = Path(temp) / "left.json"
            right = ROOT / "examples/podman_audit/profiles/observed.json"
            for value, suffix in cases:
                with self.subTest(value=value):
                    source = custom({"PC1500": value})
                    expected = "$.rules.PC1500." + suffix
                    with self.assertRaises(InputError) as caught:
                        validate_profile(source)
                    self.assertEqual(caught.exception.detail["code"], "invalid-profile")
                    self.assertEqual(caught.exception.detail["path"], expected)
                    left.write_text(json.dumps(source), encoding="utf-8")
                    result = self.run_cli(left, right, "--json", "--explain", "--fail-on", "never")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    error = json.loads(result.stdout)
                    self.assertEqual(error["assessment"], "not-evaluated")
                    self.assertEqual(error["error"]["path"], expected)
                    self.assertNotIn("capabilities", error)
        for source in (BUILTIN | {"rules": {"PC1500": {"judgment": "supported"}}},
                       custom({"DEP001": {"judgment": "supported", "observationNotes": "Cannot override."}})):
            with self.assertRaises(InputError):
                validate_profile(source)

    def test_json_yaml_metadata_parity_determinism_and_exit_thresholds(self):
        left = ROOT / "examples/podman_audit/profiles/observed.json"
        right = ROOT / "examples/podman_audit/profiles/custom-evidence.json"
        source = read_document(right)
        first = self.run_cli(left, right, "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, self.run_cli(left, right, "--json").stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report["summary"], {"services": 1, "capabilities": 1, "changed": 1, "error": 0, "warning": 1})
        self.assertEqual(report["capabilities"][0]["right"]["observationNotes"], source["rules"]["PC1500"]["observationNotes"])
        self.assertEqual(self.run_cli(left, right, "--fail-on", "warning").returncode, 1)
        self.assertEqual(self.run_cli(right, right, "--fail-on", "warning").returncode, 0)
        human = self.run_cli(left, right)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("Custom evidence references:", human.stdout)
        with tempfile.TemporaryDirectory() as temp:
            yaml_path = Path(temp) / "profile.yaml"
            yaml_path.write_text('''version: 1.6.1
provider: podman-compose
kind: custom
schemaVersion: 1
rules:
  PC1500:
    observationNotes: Synthetic example only; no runtime verification was performed.
    evidenceReferences:
      - https://example.invalid/observations/start-interval
      - local-run-17
    judgment: supported
''', encoding="utf-8")
            self.assertEqual(source, read_document(yaml_path))
            self.assertEqual(first.stdout, self.run_cli(left, yaml_path, "--json").stdout)
        structural = self.run_cli(right, right, "--json", fixture="structural-errors.yaml")
        self.assertEqual(structural.returncode, 1, structural.stderr)
        self.assertEqual(json.loads(structural.stdout)["summary"]["error"], 4)

    def test_metadata_uses_restricted_loader(self):
        prefix = '{"schemaVersion":1,"kind":"custom","provider":"podman-compose","version":"1.6.0","rules":{"PC1500":'
        for text, code in (
            (prefix + '{"judgment":"supported","judgment":"unknown"}}}', "duplicate-key"),
            (prefix + '{"judgment":"supported","evidenceReferences":[NaN]}}}', "invalid-input"),
            (prefix + '{"judgment":"supported","observationNotes":"${SECRET}"}}}', "unresolved-interpolation"),
        ):
            with self.subTest(code=code), self.assertRaises(InputError) as caught:
                load_document(text, format="json")
            self.assertEqual(caught.exception.detail["code"], code)
        for text in ("notes: &note value", "notes: !reset value", "notes: value\nnotes: duplicate"):
            with self.assertRaises(InputError):
                load_document(text)

    def test_references_and_notes_are_never_resolved_or_executed(self):
        source = custom({"PC1500": {"judgment": "unknown", "evidenceReferences": [
            "https://example.invalid/observation", "/must-not-read/evidence.txt", "$(touch SHOULD_NOT_EXIST)"],
            "observationNotes": "$(echo inert)"}})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "custom.json").write_text(json.dumps(source), encoding="utf-8")
            (path / "sitecustomize.py").write_text('''import sys
def guard(event, args):
    if event.startswith(("subprocess.", "socket.", "os.system", "os.exec", "os.spawn")):
        raise RuntimeError("Forbidden runtime action")
    if event == "open" and str(args[0]).endswith(("evidence.txt", ".env", "Dockerfile")):
        raise RuntimeError("Forbidden reference or implicit read")
sys.addaudithook(guard)
''', encoding="utf-8")
            result = self.run_cli(ROOT / "examples/podman_audit/profiles/observed.json", path / "custom.json", "--json",
                                  env=dict(os.environ, PYTHONPATH=str(path)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["capabilities"][0]["right"]["evidenceReferences"],
                             source["rules"]["PC1500"]["evidenceReferences"])


if __name__ == "__main__":
    unittest.main()
