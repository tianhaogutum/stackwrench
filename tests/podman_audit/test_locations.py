"""Location contract: additive fieldLocations ordered like fieldPaths.

Mapping paths point to the opening key token; list paths to the item token.
Positions are 1-based Unicode character coordinates in the original document,
not UTF-8 byte offsets. Absent declarations have null coordinates, never an
invented position on their parent. CAT001 has no source fields. Loading remains
strict and reports never copy source values or filenames.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.podman_audit.audit import audit
from stackwrench.podman_audit.compare import compare_profiles, render_comparison
from stackwrench.podman_audit.input import InputError, load_document, read_document
from stackwrench.podman_audit.cli import render_human

CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]
ROOT = Path(__file__).resolve().parents[2]


def explained(text, format="yaml", version="1.6.0"):
    return audit(load_document(text, format=format), provider="podman-compose",
                 provider_version=version, explain=True)


def finding(report, rule):
    return next(f for f in report["findings"] if f["ruleId"] == rule)


def location(path, line=None, column=None, declaration="present"):
    return {"path": path, "declaration": declaration, "line": line, "column": column}


class SourceLocationTests(unittest.TestCase):
    def test_yaml_block_keys_have_exact_locations_without_values(self):
        text = '''# synthetic
services:
  api:
    depends_on:
      db:
        condition: service_healthy
  db:
    healthcheck:
      test: [CMD, SECRET_DO_NOT_ECHO]
      start_interval: 1s
'''
        report = explained(text)
        for rule, path, line, column in (
            ("PC1541", "$.services.api.depends_on.db.condition", 6, 9),
            ("PC1500", "$.services.db.healthcheck.start_interval", 10, 7),
        ):
            self.assertEqual(finding(report, rule)["explanation"]["fieldLocations"],
                             [location(path, line, column)])
        self.assertNotIn("SECRET_DO_NOT_ECHO", json.dumps(report))
        self.assertIn("line 6, column 9", render_human(report))
        plain = audit(load_document(text), provider="podman-compose", provider_version="1.6.0")
        for item in report["findings"]:
            del item["explanation"]
        self.assertEqual(report, plain)

    def test_json_escaped_keys_unicode_and_crlf_use_key_tokens(self):
        # The escaped spelling must map to the decoded path; an earlier opaque
        # string with the same words must not be mistaken for a declaration.
        text = ('{\r\n  "x-note": "é🙂 condition start_interval",\r\n  "services": {\r\n'
                '    "api-v1": {"depends_on": {"db.primary": {"condit\\u0069on": "service_healthy"}}},\r\n'
                '    "db.primary": {"healthcheck": {"test": ["CMD", "é🙂"], "start_interval": "1s"}}\r\n  }\r\n}')
        report = explained(text, "json")
        condition = '$.services["api-v1"].depends_on["db.primary"].condition'
        interval = '$.services["db.primary"].healthcheck.start_interval'
        self.assertEqual(finding(report, "PC1541")["explanation"]["fieldLocations"],
                         [location(condition, 4, 46)])
        self.assertEqual(finding(report, "PC1500")["explanation"]["fieldLocations"],
                         [location(interval, 5, 59)])
        self.assertEqual(report, explained(text, "json"))

    def test_yaml_flow_quoted_keys_and_multiline_scalars(self):
        text = '''x-note: |
  condition: service_healthy
  start_interval: secret
services:
  'api-v1': {depends_on: {'db.primary': {'condition': service_healthy}}}
  db.primary: {healthcheck: {test: [CMD, probe], start_interval: 1s}}
'''
        report = explained(text)
        self.assertEqual(finding(report, "PC1541")["explanation"]["fieldLocations"], [
            location('$.services["api-v1"].depends_on["db.primary"].condition', 5, 42)])
        self.assertEqual(finding(report, "PC1500")["explanation"]["fieldLocations"], [
            location('$.services["db.primary"].healthcheck.start_interval', 6, 50)])

    def test_inherited_healthchecks_distinguish_absent_block_and_test(self):
        for health in ("{}", "{healthcheck: {}}", "{healthcheck: {interval: 1s}}"):
            with self.subTest(health=health):
                text = ('services:\n  api: {depends_on: {db: {condition: service_healthy}}}\n'
                        f'  db: {health}\n')
                item = finding(explained(text), "DEP002")
                fields = item["explanation"]["fieldLocations"]
                self.assertEqual([field["path"] for field in fields], item["explanation"]["fieldPaths"])
                block = location("$.services.db.healthcheck", declaration="absent")
                if health != "{}":
                    block = location("$.services.db.healthcheck", 3, 8)
                self.assertEqual(fields[1], block)
                if health != "{}":
                    self.assertEqual(fields[2], location("$.services.db.healthcheck.test", declaration="absent"))
                else:
                    self.assertEqual(len(fields), 2)
                self.assertEqual(item["confidence"], "unverified")
                self.assertIn("absent declaration", render_human(explained(text)))
        # JSON must mark absence as explicitly as YAML does.
        text = '{"services":{"api":{"depends_on":{"db":{"condition":"service_healthy"}}},"db":{}}}'
        item = finding(explained(text, "json"), "DEP002")
        self.assertEqual(item["explanation"]["fieldLocations"][1],
                         location("$.services.db.healthcheck", declaration="absent"))

    def test_disabled_healthcheck_points_to_present_block_and_disabling_key(self):
        for health, key in (("{disable: true}", "disable"), ("{test: [NONE]}", "test")):
            report = explained('services:\n  api: {depends_on: {db: {condition: service_healthy}}}\n'
                               f'  db: {{healthcheck: {health}}}\n')
            item = finding(report, "DEP002")
            self.assertEqual(item["explanation"]["fieldLocations"][1:],
                             [location("$.services.db.healthcheck", 3, 8),
                              location("$.services.db.healthcheck." + key, 3, 22)])
            self.assertEqual(item["severity"], "error")

    def test_short_list_items_and_long_dependency_keys(self):
        for text, format, rule, expected in (
            ('services:\n  api:\n    depends_on:\n      - missing\n', "yaml", "DEP001",
             location("$.services.api.depends_on[0]", 4, 9)),
            ('services: {api: {depends_on: [api]}}', "yaml", "DEP003",
             location("$.services.api.depends_on[0]", 1, 31)),
            ('{"services":{"api":{"depends_on":["missing"]}}}', "json", "DEP001",
             location("$.services.api.depends_on[0]", 1, 35)),
            ('services:\n  api:\n    depends_on:\n      missing: {condition: service_started}\n', "yaml", "DEP001",
             location("$.services.api.depends_on.missing", 4, 7)),
        ):
            with self.subTest(text=text):
                fields = [expected]
                if "condition:" in text:
                    fields.append(location(expected["path"] + ".condition", 4, 17))
                self.assertEqual(finding(explained(text, format), rule)["explanation"]["fieldLocations"],
                                 fields)

    def test_target_wide_finding_has_no_fabricated_location(self):
        item = finding(explained("services: {api: {}}", version="1.6.1"), "CAT001")
        self.assertEqual(item["explanation"]["fieldPaths"], [])
        self.assertEqual(item["explanation"]["fieldLocations"], [])

    def test_plain_mapping_has_unavailable_location_not_absent_declaration(self):
        model = {"services": {"api": {"depends_on": ["missing"]}}}
        item = finding(audit(model, provider="podman-compose", provider_version="1.6.0", explain=True), "DEP001")
        self.assertEqual(item["explanation"]["fieldLocations"], [
            location("$.services.api.depends_on[0]", declaration="unavailable")])

    def test_source_metadata_is_outside_model_and_tracks_formatting(self):
        compact = 'services: {db: {healthcheck: {start_interval: 1s}}}'
        multiline = 'services:\n  db:\n    healthcheck:\n      start_interval: 1s\n'
        models = [load_document(text) for text in (compact, multiline)]
        self.assertEqual(models[0], models[1])
        self.assertEqual(json.dumps(models[0]), json.dumps(models[1]))
        self.assertEqual(audit(models[0], provider="podman-compose", provider_version="1.6.0"),
                         audit(models[1], provider="podman-compose", provider_version="1.6.0"))
        for text, line in ((compact, 1), (multiline, 4)):
            self.assertEqual(finding(explained(text), "PC1500")["explanation"]["fieldLocations"][0]["line"], line)

    def test_comparison_locations_refer_to_compose_not_profile(self):
        text = (ROOT / "examples/podman_audit/healthcheck-options.yaml").read_text()
        model = load_document(text)
        profile = load_document('{"schemaVersion":1,"kind":"builtin","provider":"podman-compose","version":"1.6.0"}', format="json")
        report = compare_profiles(model, profile, profile, explain=True)
        row = report["capabilities"][0]
        expected = [location("$.services.db.healthcheck.start_interval", 8, 7),
                    location("$.services.db.healthcheck.start_period", 7, 7)]
        for side in ("left", "right"):
            self.assertEqual(row[side]["explanation"]["fieldLocations"], expected)
        self.assertIn('"line": 8', render_comparison(report))

    def test_installed_cli_bom_json_yaml_absence_and_rejections(self):
        with tempfile.TemporaryDirectory() as temp:
            for suffix, text in (("json", '{\n  "services": {"db": {"healthcheck": {"start_interval": "1s"}}}\n}'),
                                 ("yaml", 'services:\n  db: {healthcheck: {start_interval: 1s}}\n')):
                path = Path(temp) / ("compose." + suffix)
                path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
                result = subprocess.run([*CLI, str(path), "--provider", "podman-compose",
                                         "--provider-version", "1.6.0", "--explain", "--json"],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), audit(read_document(path), provider="podman-compose",
                                                                 provider_version="1.6.0", explain=True))
                fields = finding(json.loads(result.stdout), "PC1500")["explanation"]["fieldLocations"]
                self.assertEqual(fields, [location("$.services.db.healthcheck.start_interval", 2,
                                                   39 if suffix == "json" else 22)])
            inherited = ROOT / "examples/podman_audit/inherited-healthcheck.yaml"
            result = subprocess.run([*CLI, str(inherited), "--provider", "podman-compose",
                                     "--provider-version", "1.6.0", "--explain", "--json"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            item = finding(json.loads(result.stdout), "DEP002")
            self.assertEqual(item["explanation"]["fieldLocations"][-1],
                             location("$.services.db.healthcheck.test", declaration="absent"))
            path = Path(temp) / "bad.json"
            path.write_text('{"services":{"db":{}},"services":{}}')
            result = subprocess.run([*CLI, str(path), "--provider", "podman-compose",
                                     "--provider-version", "1.6.0", "--explain", "--json", "--fail-on", "never"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["error"]["code"], "duplicate-key")
            self.assertNotIn("findings", report)
        for text, format in (("services: &s {db: {}}", "yaml"), ('{"services":NaN}', "json")):
            with self.assertRaises(InputError):
                explained(text, format)


if __name__ == "__main__":
    unittest.main()
