import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.podman_audit.audit import audit, catalog
from stackwrench.podman_audit.input import InputError, load_document, MAX_BYTES

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]


def check(text, version="1.6.0", format="yaml"):
    return audit(load_document(text, format=format), provider="podman-compose", provider_version=version)


def hits(report, rule):
    return [f for f in report["findings"] if f["ruleId"] == rule]


class MappingTests(unittest.TestCase):
    def test_real_compose_fixture(self):
        report = check((ROOT / "examples/podman_audit/compose.yaml").read_text())
        self.assertEqual(report["summary"], {"services": 4, "findings": 2, "error": 0, "warning": 2})
        self.assertEqual([(f["ruleId"], f["service"], f["path"]) for f in report["findings"]], [
            ("PC1541", "api", "$.services.api.depends_on.db"),
            ("PC1500", "db", "$.services.db.healthcheck.start_interval"),
        ])
        for finding in report["findings"]:
            self.assertEqual(finding["category"], "reported-risk")
            self.assertEqual(finding["confidence"], "reported")
            self.assertEqual(finding["evidence"]["observedVersions"], ["1.6.0"])
            self.assertEqual(finding["evidence"]["statusAtRetrieval"], "open")
            self.assertIn("4.9.3", finding["evidence"]["observationContext"])
            self.assertTrue(finding["suggestion"])

    def test_start_interval_on_disabled_check_is_labeled_as_declaration(self):
        report = check("services: {db: {healthcheck: {disable: true, start_interval: 1s}}}")
        finding = hits(report, "PC1500")[0]
        self.assertEqual(finding["details"]["healthcheckState"], "disabled")
        self.assertIs(finding["details"]["startPeriodDeclared"], False)

    def test_json_fixture_mapping(self):
        report = check((ROOT / "examples/podman_audit/compose.json").read_text(), format="json")
        self.assertEqual({f["ruleId"] for f in report["findings"]}, {"PC1500", "PC1541"})
        self.assertEqual(report["summary"]["services"], 2)

    def test_clean_means_limited_audit(self):
        report = check((ROOT / "examples/podman_audit/clean.yaml").read_text())
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["compatibility"], "not-established")
        self.assertEqual(report["assessment"], "limited-static-audit")
        self.assertEqual([x["path"] for x in report["scope"]["notEvaluated"]],
                         ["$.services.api.image", "$.services.db.image"])

    def test_unknown_versions_never_infer_range(self):
        fixture = (ROOT / "examples/podman_audit/compose.yaml").read_text()
        for version in ("0.0.0", "1.5.0", "1.6.1", "9.0.0", "1.6.0-rc.1", "1.6.0+vendor.1"):
            with self.subTest(version=version):
                report = check(fixture, version)
                self.assertEqual(report["versionCoverage"], "unverified")
                self.assertEqual({f["category"] for f in report["findings"]}, {"unverified-compatibility"})
                self.assertEqual({f["confidence"] for f in report["findings"]}, {"unverified"})
                self.assertEqual(len(report["findings"]), 3)

    def test_unknown_clean_file_has_coverage_warning(self):
        report = check("services: {a: {image: example.invalid/a:1}}", "2.0.0")
        finding = hits(report, "CAT001")[0]
        self.assertIsNone(finding["service"])
        self.assertEqual(finding["path"], "$")
        self.assertEqual(finding["category"], "unverified-compatibility")

    def test_missing_short_and_long_dependencies(self):
        report = check("""services:
  a:
    depends_on: [gone]
  b:
    depends_on:
      gone:
        condition: service_started
        required: false
""")
        findings = hits(report, "DEP001")
        self.assertEqual([(f["path"], f["severity"]) for f in findings], [
            ("$.services.a.depends_on[0]", "error"), ("$.services.b.depends_on.gone", "warning")])
        self.assertEqual([f["details"]["required"] for f in findings], [True, False])

    def test_missing_healthcheck_is_uncertain_not_missing_image_check(self):
        for health in ({}, {"healthcheck": {}}, {"healthcheck": {"interval": "1s"}}):
            with self.subTest(health=health):
                model = {"services": {"a": {"depends_on": {"db": {"condition": "service_healthy"}}}, "db": health}}
                finding = hits(check(json.dumps(model), format="json"), "DEP002")[0]
                self.assertEqual(finding["severity"], "warning")
                self.assertEqual(finding["confidence"], "unverified")
                self.assertEqual(finding["details"]["healthcheckState"], "image-healthcheck-unverified")

    def test_explicit_disable_has_structural_error(self):
        for health in ({"disable": True}, {"test": ["NONE"]}, {"disable": True, "test": ["CMD", "probe"]}):
            with self.subTest(health=health):
                model = {"services": {"a": {"depends_on": {"db": {"condition": "service_healthy"}}},
                                      "db": {"healthcheck": health}}}
                finding = hits(check(json.dumps(model), format="json"), "DEP002")[0]
                self.assertEqual(finding["severity"], "error")
                self.assertEqual(finding["confidence"], "high")
                self.assertEqual(finding["details"]["targetPath"], "$.services.db.healthcheck")

    def test_enabled_healthcheck_string_or_list(self):
        for test in ("synthetic-probe", ["CMD", "synthetic-probe"], ["CMD-SHELL", "synthetic-probe"]):
            with self.subTest(test=test):
                model = {"services": {"a": {"depends_on": {"db": {"condition": "service_healthy"}}},
                                      "db": {"healthcheck": {"test": test, "disable": False}}}}
                self.assertEqual(hits(check(json.dumps(model), format="json"), "DEP002"), [])

    def test_long_dependency_maps_do_not_create_false_health_gate(self):
        report = check("""services:
  a:
    depends_on:
      db: {condition: service_completed_successfully, required: true, restart: false}
  db: {}
""")
        self.assertEqual(report["findings"], [])

    def test_cycles_paths_and_incoming_noncyclic_edge(self):
        report = check("""services:
  a: {depends_on: [b]}
  b: {depends_on: [c]}
  c: {depends_on: [a]}
  incoming: {depends_on: [a]}
  isolated: {}
""")
        cycles = hits(report, "DEP003")
        self.assertEqual([f["service"] for f in cycles], ["a", "b", "c"])
        self.assertTrue(all(f["details"]["cycleServices"] == ["a", "b", "c"] for f in cycles))
        self.assertEqual(cycles[0]["path"], "$.services.a.depends_on[0]")

    def test_self_cycle_and_optional_cycle(self):
        report = check("services: {a: {depends_on: {a: {condition: service_started, required: false}}}}")
        self.assertEqual(hits(report, "DEP003")[0]["details"], {"cycleServices": ["a"], "required": False})

    def test_disjoint_cycles_and_tail(self):
        report = check("""services:
  a: {depends_on: [b, tail]}
  b: {depends_on: [a]}
  c: {depends_on: [d]}
  d: {depends_on: [c]}
  tail: {}
""")
        self.assertEqual([f["path"] for f in hits(report, "DEP003")], [
            "$.services.a.depends_on[0]", "$.services.b.depends_on[0]",
            "$.services.c.depends_on[0]", "$.services.d.depends_on[0]"])

    def test_path_escapes_dotted_and_hyphenated_names(self):
        report = check('services: {api-v1: {depends_on: ["db.primary"]}}')
        self.assertEqual(hits(report, "DEP001")[0]["path"], '$.services["api-v1"].depends_on[0]')

    def test_no_mutation_and_mapping_order_independent(self):
        model = load_document((ROOT / "examples/podman_audit/compose.yaml").read_text())
        old = copy.deepcopy(model)
        report = audit(model, provider="podman-compose", provider_version="1.6.0")
        self.assertEqual(model, old)
        model["services"] = dict(reversed(list(model["services"].items())))
        self.assertEqual(audit(model, provider="podman-compose", provider_version="1.6.0"), report)

    def test_yaml_booleans_are_not_yaml11_on_yes_service_names(self):
        self.assertEqual(check("services: {on: {}, yes: {depends_on: [on]}} ")["findings"], [])

    def test_escaped_dollars_remain_inert(self):
        report = check('services: {a: {command: "echo $$HOST $${TOKEN} $(touch /not-run)"}}')
        self.assertEqual(report["findings"], [])
        self.assertIn("$.services.a.command", [p["path"] for p in report["scope"]["notEvaluated"]])

    def test_extensions_and_opaque_fields_are_explicit(self):
        report = check("""x-note: {anything: true}
volumes: {data: {}}
services:
  a:
    image: example.invalid/a:1
    volumes: [data:/data]
    x-local: {custom: true}
""")
        self.assertEqual([p["path"] for p in report["scope"]["notEvaluated"]], [
            "$.services.a.image", "$.services.a.volumes", '$.services.a["x-local"]', "$.volumes", '$["x-note"]'])

    def test_catalog_minimum_evidence_fields(self):
        for name, rule in catalog()["rules"].items():
            with self.subTest(rule=name):
                for field in ("evidenceUrl", "observedVersions", "uncertainty", "statusAtRetrieval"):
                    self.assertIn(field, rule)
                self.assertTrue(rule["evidenceUrl"].startswith("https://"))

    def test_maximum_service_chain_does_not_recurse(self):
        services = {f"s{i}": {"depends_on": [f"s{i+1}"]} for i in range(499)}
        services["s499"] = {}
        self.assertEqual(check(json.dumps({"services": services}), format="json")["findings"], [])


class RejectionTests(unittest.TestCase):
    def assert_bad(self, text, code="invalid-input", format="yaml"):
        with self.assertRaises(InputError) as caught:
            check(text, format=format)
        self.assertEqual(caught.exception.detail["code"], code)
        return caught.exception.detail

    def test_invalid_yaml_and_multiple_documents(self):
        for text in ("services: [", "services:\n\tbad: {}", "services: {a: {}}\n---\nservices: {b: {}}"):
            with self.subTest(text=text):
                self.assert_bad(text)

    def test_duplicate_yaml_keys_nested_and_root(self):
        for text in ("services: {}\nservices: {}", "services: {a: {image: a, image: b}}",
                     "services: {a: {healthcheck: {test: [CMD, a], test: [CMD, b]}}}"):
            with self.subTest(text=text):
                self.assert_bad(text, "duplicate-key")

    def test_duplicate_and_invalid_json(self):
        for text in ('{"services":{"a":{"image":"a","image":"b"}}}', '{"services":{},"services":{}}'):
            with self.subTest(text=text):
                self.assert_bad(text, "duplicate-key", format="json")
        for text in ('{"services": NaN}', '{"services":{"a":{}}} trailing', '{unquoted:1}', '{"services":{"a":{"x-value":1e999}}}'):
            with self.subTest(text=text):
                self.assert_bad(text, format="json")

    def test_unresolved_interpolation_even_with_host_env(self):
        with patch.dict(os.environ, {"IMAGE": "resolved-host-value"}):
            for expression in ("$IMAGE", "${IMAGE}", "${IMAGE:-fallback}", "$$$IMAGE"):
                with self.subTest(expression=expression):
                    detail = self.assert_bad(json.dumps({"services": {"a": {"image": expression}}}),
                                             "unresolved-interpolation", format="json")
                    self.assertEqual(detail["path"], "$.services.a.image")

    def test_yaml_aliases_anchors_merge_and_tags(self):
        for text in ("services: &s {a: {}}", "services: {a: *missing}",
                     "services: {a: {<<: {image: a}}}", "services: !reset {}",
                     "!!python/object/apply:os.system ['touch /not-run']"):
            with self.subTest(text=text):
                self.assert_bad(text, "unsupported")

    def test_unsupported_directives_never_pass(self):
        for key in ("extends", "profiles", "build", "links", "network_mode", "volumes_from", "provider", "develop", "deploy", "pre_start", "post_start", "typo"):
            with self.subTest(key=key):
                detail = self.assert_bad(json.dumps({"services": {"a": {key: {}}}}), "unsupported", format="json")
                self.assertEqual(detail["path"], "$.services.a." + key)
        for key in ("include", "jobs", "unknown"):
            with self.subTest(key=key):
                self.assert_bad(json.dumps({"services": {"a": {}}, key: []}), "unsupported", format="json")

    def test_structure_shapes_and_keys(self):
        for text in ("[]", "null", "{}", "services: {}", "services: []", "services: {a: null}",
                     "services: {a: {depends_on: db}}", "services: {a: {depends_on: [db, db]}}",
                     "services: {a: {depends_on: [true]}}", "services: {true: {}}",
                     "services: {'bad service': {}}", "services: {a: {depends_on: {'bad name': {}}}}"):
            with self.subTest(text=text):
                self.assert_bad(text)

    def test_invalid_long_dependency_options(self):
        for options in ({}, None, "service_started", {"condition": "typo"}, {"condition": []},
                        {"condition": "service_started", "required": "false"},
                        {"condition": "service_started", "restart": 1}):
            with self.subTest(options=options):
                self.assert_bad(json.dumps({"services": {"a": {"depends_on": {"b": options}}}}), format="json")
        self.assert_bad('services: {a: {depends_on: {b: {condition: service_started, typo: true}}}}', "unsupported")

    def test_invalid_healthcheck_shapes(self):
        for value in (None, [], "probe", {"disable": "true"}, {"retries": True}, {"retries": -1},
                      {"test": []}, {"test": ["BOGUS"]}, {"test": ["CMD"]}, {"test": ["CMD", 1]},
                      {"test": ["NONE", "extra"]}, {"test": ["CMD-SHELL", ""]}, {"test": ""},
                      {"interval": -1}, {"start_interval": "-1s"}, {"timeout": "later"}):
            with self.subTest(value=value):
                self.assert_bad(json.dumps({"services": {"a": {"healthcheck": value}}}), format="json")
        self.assert_bad('services: {a: {healthcheck: {start_interva: 1s}}}', "unsupported")

    def test_invalid_provider_and_versions(self):
        model = load_document("services: {a: {}}")
        for provider in ("docker-compose", "podman", "unknown"):
            with self.subTest(provider=provider), self.assertRaises(InputError) as caught:
                audit(model, provider=provider, provider_version="1.6.0")
            self.assertEqual(caught.exception.detail["code"], "unknown-provider")
        for version in ("", "v1.6.0", "1.6", "1.6.x", ">=1.6.0", "01.6.0", "1.6.0-01", "latest", "1.6.0+", "1.6.0-"):
            with self.subTest(version=version), self.assertRaises(InputError) as caught:
                audit(model, provider="podman-compose", provider_version=version)
            self.assertEqual(caught.exception.detail["code"], "invalid-version")

    def test_size_depth_nodes_and_service_limits(self):
        self.assert_bad("x: " + "a" * MAX_BYTES, "input-limit")
        self.assert_bad("[" * 70 + "0" + "]" * 70, "input-limit")
        self.assert_bad("[" + ",".join("0" for _ in range(50_001)) + "]", "input-limit")
        self.assert_bad(json.dumps({"services": {f"a{i}": {} for i in range(501)}}), "input-limit", format="json")


class CLITests(unittest.TestCase):
    def run_cli(self, file="examples/podman_audit/compose.yaml", *extra, version="1.6.0", json_mode=True, env=None):
        args = [*CLI, str(ROOT / file), "--provider", "podman-compose", "--provider-version", version]
        if json_mode:
            args.append("--json")
        return subprocess.run([*args, *extra], cwd=ROOT, text=True, capture_output=True, timeout=15, env=env)

    def test_acceptance_e2e_deterministic(self):
        first, second = self.run_cli(), self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report["summary"]["findings"], 2)
        self.assertEqual(report["findings"][0]["path"], "$.services.api.depends_on.db")

    def test_json_fixture_e2e(self):
        result = self.run_cli("examples/podman_audit/compose.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({f["ruleId"] for f in json.loads(result.stdout)["findings"]}, {"PC1541", "PC1500"})

    def test_human_report_contains_evidence_scope_and_suggestions(self):
        result = self.run_cli(json_mode=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ("PC1541", "$.services.api.depends_on.db", "confidence=reported", "issues/1541", "Suggestion:", "not established", "Fields not evaluated"):
            self.assertIn(text, result.stdout)

    def test_clean_cli(self):
        result = self.run_cli("examples/podman_audit/clean.yaml")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["findings"], [])
        result = self.run_cli("examples/podman_audit/clean.yaml", json_mode=False)
        self.assertIn("not a compatibility guarantee", result.stdout)

    def test_structural_error_exit_and_fail_on(self):
        result = self.run_cli("examples/podman_audit/structural-errors.yaml")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["error"], 4)
        self.assertEqual({f["ruleId"] for f in report["findings"]}, {"DEP001", "DEP002", "DEP003", "PC1541"})
        self.assertEqual(self.run_cli("examples/podman_audit/structural-errors.yaml", "--fail-on", "never").returncode, 0)
        self.assertEqual(self.run_cli("examples/podman_audit/compose.yaml", "--fail-on", "warning").returncode, 1)
        self.assertEqual(self.run_cli("examples/podman_audit/clean.yaml", "--fail-on", "warning", version="9.0.0").returncode, 1)

    def test_unsupported_is_not_clean_and_cannot_suppress_input_errors(self):
        result = self.run_cli("examples/podman_audit/unsupported.yaml", "--fail-on", "never")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["assessment"], "not-evaluated")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "unsupported")
        self.assertEqual(self.run_cli("examples/podman_audit/unsupported.yaml", json_mode=False).returncode, 2)

    def test_bad_file_utf8_duplicates_and_syntax_e2e(self):
        with tempfile.TemporaryDirectory() as temp:
            for data, code in ((b'\xff', "input-io"), (b'services: [', "invalid-input"),
                               (b'services: {a: {image: a, image: b}}', "duplicate-key")):
                with self.subTest(code=code):
                    path = Path(temp) / "compose.yaml"
                    path.write_bytes(data)
                    result = self.run_cli(str(path))
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(json.loads(result.stdout)["error"]["code"], code)
        self.assertEqual(self.run_cli("no-such-file.yaml").returncode, 2)
        self.assertEqual(self.run_cli("examples/podman_audit").returncode, 2)

    def test_invalid_provider_version_and_extra_files_e2e(self):
        for options, code in ((["--provider", "docker"], "unknown-provider"),
                              (["--provider-version", "1.6"], "invalid-version"),
                              (["extra-compose.yaml"], "invalid-arguments"),
                              (["-f", "extra.yaml"], "invalid-arguments")):
            with self.subTest(options=options):
                result = self.run_cli("examples/podman_audit/clean.yaml", *options)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], code)

    def test_help_version_module_entrypoint_and_missing_args(self):
        for options in (["--help"], ["--version"]):
            result = subprocess.run([*CLI, *options], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout)
            if options == ["--help"]:
                self.assertTrue(result.stdout.startswith("usage: stackwrench podman-audit"))
            else:
                from stackwrench import __version__
                self.assertEqual(result.stdout.strip(), __version__)
        result = subprocess.run([*CLI, "--json"], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid-arguments")
        result = subprocess.run([sys.executable, "-m", "stackwrench", "podman-audit", str(ROOT / "examples/podman_audit/clean.yaml"),
                                 "--provider", "podman-compose", "--provider-version", "1.6.0"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_e2e_no_shell_network_daemon_or_dotenv_and_no_input_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            marker = temp / "MUST_NOT_EXIST"
            # Reject subprocess/network actions inside the *actual CLI process*.
            (temp / "sitecustomize.py").write_text('''import sys

def guard(event, args):
    if event.startswith(("subprocess.", "socket.", "os.system", "os.exec", "os.spawn")):
        raise RuntimeError("Forbidden runtime action: " + event)
    if event == "open" and str(args[0]).endswith((".env", "Dockerfile")):
        raise RuntimeError("Forbidden implicit input read")

sys.addaudithook(guard)
''')
            fixture = temp / "compose.yaml"
            text = f'''services:
  a:
    command: "touch {marker}"
    entrypoint: [sh, -c]
    environment: [INHERITED_SECRET]
    healthcheck:
      test: [CMD-SHELL, "touch {marker}"]
      start_interval: 1s
'''
            fixture.write_text(text)
            (temp / ".env").write_text("INHERITED_SECRET=secret-do-not-read\n")
            environment = dict(os.environ, PYTHONPATH=str(temp), INHERITED_SECRET="secret-do-not-report",
                               COMPOSE_FILE="not-read.yaml", DOCKER_HOST="unix:///not-a-daemon.sock")
            result = self.run_cli(str(fixture), "--explain", env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["findings"][0]["ruleId"], "PC1500")
            self.assertNotIn("secret-do-not", result.stdout)
            self.assertFalse(marker.exists())
            self.assertEqual(fixture.read_text(), text)

    def test_parser_error_does_not_echo_secret_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.yaml"
            path.write_text('services: [SECRET_DO_NOT_ECHO')
            result = self.run_cli(str(path))
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("SECRET_DO_NOT_ECHO", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
