"""Named, explicit environment scenarios using local synthetic integration fixtures."""

import json
import os
from unittest.mock import patch

from .test_trace import ROOT, Workspace


class Scenarios(Workspace):
    def manifest(self, data):
        return self.put("inputs/scenarios.json", json.dumps(data))

    def fixture(self):
        compose = self.compose(
            "      OPTIONAL:\n"
            "      DEFAULTED: '${MODE:-synthetic-default}'\n"
            "      CHANGED: '${CHANGED}'\n"
            "      SAME:\n")
        self.put("inputs/dev.env", "SAME=synthetic-shared\nCHANGED=synthetic-development\n")
        self.put("inputs/ci.env", "SAME=synthetic-shared\nCHANGED=synthetic-ci\n")
        self.put("inputs/ci.json", '{"OPTIONAL":"","MODE":"synthetic-default"}')
        manifest = self.manifest({
            "development": {"env_files": ["dev.env"]},
            "ci": {"env_files": ["ci.env"], "shell_env": "ci.json"},
        })
        return compose, manifest

    def compare(self, compose, manifest, **kwargs):
        from stackwrench.compose_env import compare_scenarios
        return compare_scenarios(compose, manifest, **kwargs)

    def test_cli_named_scenarios_states_and_independent_changes(self):
        compose, manifest = self.fixture()
        proc = self.run_cli("--compose", compose, "--scenario-file", manifest, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report, self.compare(compose, manifest))
        self.assertEqual(report["baseline"], "development")
        self.assertFalse(report["values_shown"])
        delta = report["comparisons"]["ci"]
        inputs = delta["interpolation"]["variables"]
        self.assertEqual((inputs["OPTIONAL"]["baseline_status"], inputs["OPTIONAL"]["scenario_status"]), ("unset", "empty"))
        self.assertTrue(inputs["SAME"]["source_changed"])
        self.assertFalse(inputs["SAME"]["value_changed"])
        variables = delta["services"]["demo"]["variables"]
        self.assertEqual(variables["DEFAULTED"], {
            "baseline_status": "set", "scenario_status": "set", "source_changed": False,
            "resolution_changed": True, "value_changed": False, "path_selection_changed": False, "dependency_path_selection_changed": False,
        })
        self.assertFalse(variables["CHANGED"]["source_changed"])
        self.assertTrue(variables["CHANGED"]["value_changed"])
        for name, expected in [("development", ("unset", "default")), ("ci", ("set", "source"))]:
            use = next(u for u in report["scenarios"][name]["interpolation"]["uses"] if u.get("variable") == "MODE")
            self.assertEqual((use["input_status"], use["branch"]), expected)
        for forbidden in ["synthetic-default", "synthetic-shared", "synthetic-ci", "synthetic-development", '"value":', '"result":']:
            self.assertNotIn(forbidden, proc.stdout)
        shown = self.run_cli("--compose", compose, "--scenario-file", manifest, "--json", "--show-values")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        shown_report = json.loads(shown.stdout)
        self.assertEqual(report["comparisons"], shown_report["comparisons"])
        self.assertEqual(self.values(shown_report["scenarios"]["ci"])["OPTIONAL"], "")
        self.assertIsNone(self.values(shown_report["scenarios"]["development"])["OPTIONAL"])

    def test_text_comparison_and_default_branches_visible(self):
        compose, manifest = self.fixture()
        proc = self.run_cli("--compose", compose, "--scenario-file", manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for expected in ["Scenario development (baseline)", "Scenario ci", "Comparison ci against development", "unset -> empty", "source_changed=", "value_changed=", "default -> set", "Values hidden"]:
            self.assertIn(expected, proc.stdout)
        self.assertNotIn("synthetic-default", proc.stdout)

    def test_selected_service_files_have_private_physical_identity(self):
        from stackwrench.compose_env import trace

        compose = ROOT / "examples/compose_env/file-scenarios/compose.yaml"
        manifest = ROOT / "examples/compose_env/file-scenarios/scenarios.json"
        proc = self.run_cli("--compose", compose, "--scenario-file", manifest, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report, self.compare(compose, manifest))
        variables = report["comparisons"]["right"]["services"]["demo"]["variables"]
        for name in ("PLAIN", "EMPTY", "LOCAL", "COPY"):
            with self.subTest(name=name):
                self.assertTrue(variables[name]["source_changed"])
                self.assertFalse(variables[name]["value_changed"])
        self.assertFalse(variables["PLAIN"]["resolution_changed"])
        self.assertTrue(variables["COPY"]["resolution_changed"])
        # A stable winning file still depends on the newly selected upstream file.
        self.assertEqual(variables["FOLLOW"], {
            "baseline_status": "set", "scenario_status": "set", "source_changed": False,
            "resolution_changed": True, "value_changed": False, "path_selection_changed": False, "dependency_path_selection_changed": True,
        })
        # Changing only a shadowed file is outside winning-declaration comparisons.
        self.assertEqual(variables["OVERRIDDEN"], {
            "baseline_status": "set", "scenario_status": "set", "source_changed": False,
            "resolution_changed": False, "value_changed": False, "path_selection_changed": False, "dependency_path_selection_changed": False,
        })
        for forbidden in ("selected-left.env", "selected-right.env", "synthetic-equal",
                          '"value":', '"result":'):
            self.assertNotIn(forbidden, proc.stdout)
        self.assertEqual(report["comparison_scope"]["service_env_file_identity"], "private-device-inode")
        for shown in (False, True):
            comparison = self.compare(compose, manifest, show_values=shown)
            self.assertEqual(report["comparisons"], comparison["comparisons"])
            for private_key in ("physical_source", "service_file_identities", "st_dev", "st_ino"):
                self.assertNotIn(f'"{private_key}":', json.dumps(comparison))
            for side in ("left", "right"):
                # The internal identity sidecar must never enter either public API report.
                self.assertEqual(comparison["scenarios"][side], trace(
                    compose, shell_env=manifest.parent / f"{side}.json", show_values=shown))
        text = self.run_cli("--compose", compose, "--scenario-file", manifest)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("service demo PLAIN: set -> set; source_changed=true", text.stdout)
        self.assertNotIn("selected-left.env", text.stdout)
        self.assertNotIn("selected-right.env", text.stdout)

    def test_selected_service_file_aliases_preserve_physical_identity(self):
        compose = self.compose("      OVERRIDDEN: synthetic-equal\n",
                               fields="    env_file: '${SELECTED}'\n")
        original = self.put("selected-original.env", "PLAIN=synthetic-equal\nCOPY=${PLAIN}\n")
        (self.root / "symlink.env").symlink_to(original)
        os.link(original, self.root / "hardlink.env")
        options = {}
        for name, selected in (("original", "selected-original.env"),
                               ("relative", "./selected-original.env"),
                               ("absolute", str(original)), ("symlink", "symlink.env"),
                               ("hardlink", "hardlink.env")):
            self.put(f"inputs/{name}.json", json.dumps({"SELECTED": selected}))
            options[name] = {"shell_env": f"{name}.json"}
        report = self.compare(compose, self.manifest(options))
        for name, comparison in report["comparisons"].items():
            for variable, change in comparison["services"]["demo"]["variables"].items():
                with self.subTest(scenario=name, variable=variable):
                    for flag in ("source_changed", "resolution_changed", "value_changed"):
                        self.assertFalse(change[flag])

    def test_same_file_selection_branch_has_distinct_redacted_signal(self):
        from stackwrench.compose_env import trace

        compose = ROOT / "examples/compose_env/path-selection/compose.yaml"
        manifest = ROOT / "examples/compose_env/path-selection/scenarios.json"
        hidden = self.compare(compose, manifest)
        shown = self.compare(compose, manifest, show_values=True)
        self.assertEqual(hidden["comparisons"], shown["comparisons"])
        variables = hidden["comparisons"]["explicit"]["services"]["demo"]["variables"]
        self.assertEqual(variables["PLAIN"], {
            "baseline_status": "set", "scenario_status": "set", "source_changed": False,
            "resolution_changed": False, "value_changed": False, "path_selection_changed": True, "dependency_path_selection_changed": False,
        })
        self.assertEqual(hidden["comparison_scope"]["path_selection"],
                         "winning-service-env-file-selector-provenance")
        for name, comparison in hidden["comparisons"].items():
            for variable, change in comparison["services"]["demo"]["variables"].items():
                self.assertEqual(change["path_selection_changed"], name == "explicit" and variable == "PLAIN")
            for change in comparison["interpolation"]["variables"].values():
                self.assertFalse(change["path_selection_changed"])
        for name, options in (("baseline", {}), ("identical", {}),
                              ("explicit", {"shell_env": manifest.parent / "explicit.json"})):
            for report, shown_values in ((hidden, False), (shown, True)):
                self.assertEqual(report["scenarios"][name], trace(compose, **options, show_values=shown_values))
        for flag in ((), ("--json",)):
            proc = self.run_cli("--compose", compose, "--scenario-file", manifest, *flag)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if flag:
                self.assertEqual(json.loads(proc.stdout), hidden)
            else:
                self.assertIn("path_selection_changed=true", proc.stdout)
                self.assertIn("path_selection_changed=false", proc.stdout)
            for forbidden in ("selected.env", "synthetic-equal", '"value":', '"result":'):
                self.assertNotIn(forbidden, proc.stdout)
        for report in (hidden, shown):
            for private_key in ("physical_source", "service_file_identities", "service_file_path_uses",
                                "st_dev", "st_ino", "fingerprint"):
                self.assertNotIn(f'"{private_key}":', json.dumps(report))

    def test_path_selection_follows_upstream_provenance_not_unrelated_use_indexes(self):
        compose = self.compose("      OVERRIDDEN: synthetic-equal\n",
                               fields="    env_file: '${FILE}'\n")
        self.put("selected.env", "PLAIN=synthetic-equal\nOVERRIDDEN=synthetic-equal\n")
        self.put("inputs/base.env", "FILE=${MODE:-selected.env}\n")
        self.put("inputs/explicit.json", '{"MODE":"selected.env"}')
        self.put("inputs/noise.env", "UNUSED=${MISSING:-synthetic-unrelated}\n")
        manifest = self.manifest({
            "baseline": {"env_files": ["base.env"]},
            "explicit": {"env_files": ["base.env"], "shell_env": "explicit.json"},
            "noise": {"env_files": ["noise.env", "base.env"]},
        })
        report = self.compare(compose, manifest)
        for name, comparison in report["comparisons"].items():
            change = comparison["services"]["demo"]["variables"]["PLAIN"]
            self.assertEqual(change, {
                "baseline_status": "set", "scenario_status": "set", "source_changed": False,
                "resolution_changed": False, "value_changed": False,
                "path_selection_changed": name == "explicit", "dependency_path_selection_changed": False,
            })
            self.assertFalse(comparison["services"]["demo"]["variables"]["OVERRIDDEN"]["path_selection_changed"])

    def test_shadowed_selectors_and_value_dependencies_do_not_select_winning_file(self):
        compose = self.compose("      OVERRIDDEN: synthetic-equal\n",
            fields="    env_file: ['${SHADOW:-shadow.env}', '${FILE:-selected.env}', stable.env]\n")
        self.put("shadow.env", "SHADOWED=synthetic-equal\nOVERRIDDEN=synthetic-equal\n")
        self.put("selected.env", "PLAIN=synthetic-equal\n")
        self.put("stable.env", "SHADOWED=synthetic-equal\nFOLLOW=${PLAIN}\n")
        self.put("inputs/shadow.json", '{"SHADOW":"shadow.env"}')
        self.put("inputs/selected.json", '{"FILE":"selected.env"}')
        self.put("inputs/noise.env", "UNUSED=${MISSING:-synthetic-unrelated}\n")
        manifest = self.manifest({"baseline": {}, "identical": {},
                                  "noise": {"env_files": ["noise.env"]}, "shadow": {"shell_env": "shadow.json"},
                                  "selected": {"shell_env": "selected.json"}})
        report = self.compare(compose, manifest)
        self.assertIs(report["comparisons"]["selected"]["services"]["demo"]["variables"]["FOLLOW"].get(
            "dependency_path_selection_changed"), True)
        for name, comparison in report["comparisons"].items():
            for variable, change in comparison["services"]["demo"]["variables"].items():
                self.assertEqual(change["path_selection_changed"], name == "selected" and variable == "PLAIN")
                self.assertEqual(change["dependency_path_selection_changed"],
                                 name == "selected" and variable == "FOLLOW")
                for flag in ("source_changed", "resolution_changed", "value_changed"):
                    self.assertFalse(change[flag])

    def test_dependency_path_selection_transitive_dotenv_and_shared_sources(self):
        compose = self.compose("      OVERRIDDEN: synthetic-equal\n",
            fields="    env_file: ['${FILE}', middle.env, stable.env]\n")
        self.put("inputs/base.env", "SELECTOR=${MODE:-selected.env}\nFILE=${SELECTOR}\n")
        self.put("inputs/explicit.json", '{"MODE":"selected.env"}')
        self.put("inputs/noise.env", "UNUSED=${MISSING:-synthetic-unrelated}\n")
        self.put("selected.env", "PLAIN=synthetic-equal\nOVERRIDDEN=synthetic-equal\n")
        self.put("middle.env", "COPY=${PLAIN}\n")
        self.put("stable.env", "FOLLOW=${COPY}${COPY}${PLAIN}\nALTERNATIVE=${COPY:+synthetic-equal}\n")
        manifest = self.manifest({
            "baseline": {"env_files": ["base.env"]},
            "explicit": {"env_files": ["base.env"], "shell_env": "explicit.json"},
            "noise": {"env_files": ["noise.env", "base.env"]},
        })
        report = self.compare(compose, manifest)
        for name, comparison in report["comparisons"].items():
            for variable, change in comparison["services"]["demo"]["variables"].items():
                self.assertEqual(change, {
                    "baseline_status": "set", "scenario_status": "set", "source_changed": False,
                    "resolution_changed": False, "value_changed": False,
                    "path_selection_changed": name == "explicit" and variable == "PLAIN",
                    "dependency_path_selection_changed": name == "explicit" and variable in
                        {"COPY", "FOLLOW", "ALTERNATIVE"},
                })
            for change in comparison["interpolation"]["variables"].values():
                self.assertFalse(change["dependency_path_selection_changed"])

    def test_dependency_path_selection_stops_cycles_and_deduplicates(self):
        # A synthetic graph exercises the traversal contract beyond the parser's
        # chronological DAG: inheritance back-edge, shared input and duplicate use.
        from stackwrench.compose_env.scenarios import dependency_path_selection
        from stackwrench.compose_env.trace import _TraceMetadata

        winner = {"source": "service-env-file", "location": "winner",
                  "interpolation_uses": [0, 0, 1]}
        dependency = {"source": "service-env-file", "location": "dependency",
                      "interpolation_uses": [0], "resolved_from": winner}
        winner["resolved_from"] = dependency
        selector = {"phase": "compose", "location": "selector", "offset": 0,
                    "variable": "FILE", "operator": ":-", "branch": "default"}
        report = {"interpolation": {"uses": [
            {"input_source": dependency}, {"input_source": dependency}, selector,
            {**selector, "location": "direct-selector"}]}}
        metadata = _TraceMetadata(service_file_path_uses={"dependency": [2, 2], "winner": [3]})
        signature = dependency_path_selection(winner, report, metadata)
        self.assertEqual(len(signature), 1)
        self.assertEqual(json.loads(signature[0])["location"], "selector")
        # The direct winner must not become its own dependency on a back-edge.
        dependency["resolved_from"] = None
        dependency["interpolation_uses"] = []
        self.assertEqual(dependency_path_selection(dependency, report, metadata), ())

    def test_dependency_path_selection_example_redaction_and_trace_parity(self):
        from stackwrench.compose_env import trace

        compose = ROOT / "examples/compose_env/dependency-path-selection/compose.yaml"
        manifest = ROOT / "examples/compose_env/dependency-path-selection/scenarios.json"
        hidden = self.compare(compose, manifest)
        shown = self.compare(compose, manifest, show_values=True)
        self.assertEqual(hidden["comparisons"], shown["comparisons"])
        self.assertEqual(hidden["comparison_scope"]["dependency_path_selection"],
                         "recorded-value-dependency-service-env-file-selector-provenance")
        for name, comparison in hidden["comparisons"].items():
            for variable, change in comparison["services"]["demo"]["variables"].items():
                self.assertEqual(change["path_selection_changed"], name == "explicit" and variable == "PLAIN")
                self.assertEqual(change["dependency_path_selection_changed"],
                                 name == "explicit" and variable == "FOLLOW")
                for flag in ("source_changed", "resolution_changed", "value_changed"):
                    self.assertFalse(change[flag])
            for change in comparison["interpolation"]["variables"].values():
                self.assertFalse(change["dependency_path_selection_changed"])
        for name, options in json.loads(manifest.read_text()).items():
            shell = manifest.parent / options["shell_env"] if "shell_env" in options else None
            for comparison, show_values in ((hidden, False), (shown, True)):
                self.assertEqual(comparison["scenarios"][name],
                                 trace(compose, shell_env=shell, show_values=show_values))
        for flags in ((), ("--json",)):
            proc = self.run_cli("--compose", compose, "--scenario-file", manifest, *flags)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if flags:
                self.assertEqual(json.loads(proc.stdout), hidden)
            else:
                self.assertIn("dependency_path_selection_changed=true", proc.stdout)
                self.assertIn("dependency_path_selection_changed=false", proc.stdout)
            for forbidden in ("selected.env", "shadow.env", "stable.env", "synthetic-equal",
                              '"value":', '"result":'):
                self.assertNotIn(forbidden, proc.stdout)
        for report in (hidden, shown):
            for private_key in ("physical_source", "service_file_identities", "service_file_path_uses",
                                "st_dev", "st_ino", "fingerprint"):
                self.assertNotIn(f'"{private_key}":', json.dumps(report))

    def test_selector_required_failure_remains_atomic_and_redacted(self):
        from stackwrench.compose_env import TraceError

        compose = self.compose("      RESULT: ok\n",
                               fields="    env_file: '${FILE:?synthetic-private-message}'\n")
        self.put("selected.env", "PLAIN=synthetic-private-value\n")
        self.put("inputs/good.json", '{"FILE":"selected.env"}')
        manifest = self.manifest({"good": {"shell_env": "good.json"}, "bad": {}})
        for shown in (False, True):
            with self.assertRaises(TraceError) as error:
                self.compare(compose, manifest, show_values=shown)
            self.assertNotIn("synthetic-private", str(error.exception))
            self.assertNotIn("selected.env", str(error.exception))
            self.assert_cli_error("--compose", compose, "--scenario-file", manifest,
                                  *(("--show-values",) if shown else ()),
                                  forbidden=("synthetic-private", "selected.env", "st_ino", "st_dev"))

    def test_selected_service_file_failure_remains_atomic_and_redacted(self):
        compose = self.compose("      RESULT: ok\n", fields="    env_file: '${SELECTED}'\n")
        self.put("selected-good.env", "PLAIN=synthetic-private-value\n")
        self.put("inputs/good.json", '{"SELECTED":"selected-good.env"}')
        self.put("inputs/bad.json", '{"SELECTED":"synthetic-private-missing.env"}')
        manifest = self.manifest({"good": {"shell_env": "good.json"}, "bad": {"shell_env": "bad.json"}})
        for flags in ((), ("--show-values",)):
            self.assert_cli_error("--compose", compose, "--scenario-file", manifest, *flags,
                                  forbidden=("selected-good.env", "synthetic-private", "st_ino", "st_dev"))

    def test_redaction_preserves_variable_names_and_shadow_histories(self):
        compose = self.compose("      value: '${result}'\n      result:\n")
        self.put("inputs/base.env", "result=synthetic-private-one\n")
        self.put("inputs/override.env", "result=synthetic-private-two\n")
        manifest = self.manifest({"development": {"env_files": ["base.env", "override.env"]}, "ci": {}})
        hidden = self.compare(compose, manifest)
        shown = self.compare(compose, manifest, show_values=True)
        variables = hidden["scenarios"]["development"]["services"]["demo"]["variables"]
        self.assertEqual(set(variables), {"value", "result"})
        self.assertNotIn("value", variables["value"]["winner"])
        inputs = hidden["scenarios"]["development"]["interpolation"]["variables"]
        self.assertEqual(len(inputs["result"]["shadowed"]), 1)
        self.assertNotIn("value", inputs["result"]["shadowed"][0])
        self.assertNotIn("synthetic-private", json.dumps(hidden))
        self.assertIn("synthetic-private-two", json.dumps(shown))

    def test_transitive_resolution_and_unrelated_use_indexes(self):
        compose = self.compose("      RESULT: '${COPY}'\n")
        self.put("inputs/base.env", "COPY=${MODE:-synthetic-default}\n")
        self.put("inputs/ci.json", '{"MODE":"synthetic-default"}')
        manifest = self.manifest({"development": {"env_files": ["base.env"]}, "ci": {"env_files": ["base.env"], "shell_env": "ci.json"}})
        change = self.compare(compose, manifest)["comparisons"]["ci"]["services"]["demo"]["variables"]["RESULT"]
        self.assertFalse(change["source_changed"])
        self.assertFalse(change["value_changed"])
        self.assertTrue(change["resolution_changed"])
        self.put("inputs/unused.env", "UNUSED=${MISSING:-synthetic-unrelated}\n")
        manifest = self.manifest({"development": {"env_files": ["base.env"]}, "ci": {"env_files": ["unused.env", "base.env"]}})
        change = self.compare(compose, manifest)["comparisons"]["ci"]["services"]["demo"]["variables"]["RESULT"]
        self.assertEqual(change, {"baseline_status": "set", "scenario_status": "set", "source_changed": False, "resolution_changed": False, "value_changed": False, "path_selection_changed": False, "dependency_path_selection_changed": False})

    def test_each_scenario_has_isolated_inputs_and_container_variable_union(self):
        compose = self.compose("      - '${NAME:-DEVELOPMENT}=synthetic'\n      - REMOVED\n")
        self.put("inputs/dev.env", "REMOVED=\n")
        self.put("inputs/ci.json", '{"NAME":"CI"}')
        manifest = self.manifest({"development": {"env_files": ["dev.env"]}, "ci": {"shell_env": "ci.json"}, "empty": {}})
        report = self.compare(compose, manifest)
        self.assertEqual(list(report["comparisons"]), ["ci", "empty"])
        variables = report["comparisons"]["ci"]["services"]["demo"]["variables"]
        self.assertEqual(set(variables), {"CI", "DEVELOPMENT", "REMOVED"})
        self.assertEqual((variables["CI"]["baseline_status"], variables["CI"]["scenario_status"]), ("unset", "set"))
        self.assertEqual((variables["REMOVED"]["baseline_status"], variables["REMOVED"]["scenario_status"]), ("empty", "unset"))
        self.assertTrue(variables["REMOVED"]["value_changed"])
        self.assertNotIn("REMOVED", report["scenarios"]["ci"]["interpolation"]["variables"])

    def test_host_isolation_and_no_execution(self):
        marker = self.root / "must-not-exist"
        compose = self.compose(f"      RESULT: '${{HOST_ONLY:-offline}}|$(touch {marker})|`touch {marker}`'\n")
        self.put(".env", "HOST_ONLY=synthetic-implicit\n")
        manifest = self.manifest({"development": {}, "ci": {}})
        with patch.dict(os.environ, {"HOST_ONLY": "synthetic-host-private", "COMPOSE_ENV_FILES": "ignored.env"}):
            report = self.compare(compose, manifest, show_values=True)
        self.assertFalse(marker.exists())
        for scenario in report["scenarios"].values():
            self.assertEqual(self.values(scenario)["RESULT"], f"offline|$(touch {marker})|`touch {marker}`")
            self.assertEqual(scenario["host_environment"], "not-read")
            self.assertEqual(scenario["implicit_dotenv"], "not-read")

    def test_invalid_manifests_and_duplicates_are_redacted(self):
        compose = self.compose()
        invalid = ["{private-malformed", "[]", "null", "{}", '{"dev":{}}',
                   '{"dev":{},"dev":{},"ci":{}}', '{"dev":{"env_files":[],"env_files":[]},"ci":{}}']
        invalid += [json.dumps(value) for value in [
            {"dev": [], "ci": {}}, {"bad-name": {}, "ci": {}},
            {"dev": {"private-unknown": "private-value"}, "ci": {}},
            {"dev": {"env_files": "private-path"}, "ci": {}},
            {"dev": {"env_files": [123]}, "ci": {}},
            {"dev": {"env_files": [""]}, "ci": {}},
            {"dev": {"shell_env": None}, "ci": {}},
            {"dev": {"shell_env": {}}, "ci": {}},
            {"dev": {"shell_env": ""}, "ci": {}},
            {"dev": {"env_files": ["private\x00path"]}, "ci": {}},
            {f"scenario_{i}": {} for i in range(17)},
        ]]
        for value in invalid:
            with self.subTest(value=value):
                manifest = self.put("scenarios.json", value)
                self.assert_cli_error("--compose", compose, "--scenario-file", manifest, forbidden=("private",))

    def test_scenario_failure_is_atomic_even_show_values(self):
        compose = self.compose("      RESULT: '${REQUIRED:?synthetic-private-message}'\n")
        self.put("inputs/good.json", '{"REQUIRED":"synthetic-private-value"}')
        manifest = self.manifest({"development": {"shell_env": "good.json"}, "ci": {}})
        message = self.assert_cli_error("--compose", compose, "--scenario-file", manifest, "--show-values", forbidden=("synthetic-private",))
        self.assertIn("ci", message)
        self.assertIn("required variable REQUIRED", message)
        manifest = self.manifest({"development": {}, "ci": {"env_files": ["missing.env"]}})
        self.assert_cli_error("--compose", self.compose("      RESULT: ok\n"), "--scenario-file", manifest)

    def test_cli_rejects_shared_inputs_with_scenarios(self):
        compose, manifest = self.fixture()
        for args in [("--env-file", "private-path"), ("--shell-env", "private-path")]:
            proc = self.run_cli("--compose", compose, "--scenario-file", manifest, *args)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertNotIn("private", proc.stderr)

    def test_checked_in_example_from_other_working_directory(self):
        proc = self.run_cli("--compose", ROOT / "examples/compose_env/scenarios/compose.yaml", "--scenario-file", ROOT / "examples/compose_env/scenarios/scenarios.json", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["baseline"], "development")
        self.assertTrue(report["comparisons"]["ci"]["services"]["demo"]["variables"]["DEFAULTED"]["resolution_changed"])
