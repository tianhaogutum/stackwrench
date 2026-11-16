"""Real CLI E2E tests against synthetic Taskfiles; no Task runtime required."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from stackwrench.task_audit.audit import digest

from .support import CLI, EXAMPLES, cli_env


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.taskfile = self.root / "Taskfile.yml"
        (self.root / "base.txt").write_text("synthetic source\n")
        self.write_fixture()

    def write_fixture(self, version="one", command='echo "{{.VERSION}}"', extra=""):
        self.taskfile.write_text(f'''version: '3'
vars:
  VERSION: '{version}'
tasks:
  build:
    sources: [base.txt]
    generates: [output.txt]
    cmds:
      - '{command}'
{extra}''')

    def run_cli(self, *args, code=0, env=None):
        result = subprocess.run([*CLI, *map(str, args)], cwd=self.root, env=cli_env(env),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def check(self, *args, code=0, env=None):
        return json.loads(self.run_cli("check", self.taskfile, "--root", self.root, "--json", *args, code=code, env=env).stdout)

    def snapshot(self, name="snapshot.json", *args, code=0):
        path = self.root / name
        self.run_cli("snapshot", self.taskfile, "--root", self.root, "--output", path, *args, code=code)
        return path

    def comparison(self, before, after):
        return json.loads(self.run_cli("compare", before, after, "--json").stdout)["tasks"]["build"]

    def codes(self, report):
        return {f["code"] for f in report["findings"] + report["tasks"]["build"]["findings"]}

    def test_stable_repeat_and_real_hash_mapping(self):
        first, second = self.check(), self.check()
        self.assertEqual(first, second)
        task = first["tasks"]["build"]
        self.assertEqual(task["source_hashes"], {"base.txt": hashlib.sha256(b"synthetic source\n").hexdigest()})
        self.assertEqual(task["inputs"]["template:VERSION"]["value_digest"], digest({"type": "str", "value": "one"}))
        self.assertEqual(task["native_execution_decision"], "unknown")
        before, after = self.snapshot("one.json"), self.snapshot("two.json")
        self.assertEqual(before.read_bytes(), after.read_bytes())
        self.assertEqual(self.comparison(before, after)["change"], "unchanged")

    def test_version_only_change_experiment(self):
        before = self.snapshot("before.json")
        self.write_fixture(version="two")
        after = self.snapshot("after.json")
        delta = self.comparison(before, after)
        self.assertFalse(delta["source_only_changed"])
        self.assertTrue(delta["advisory_changed"])
        self.assertEqual(delta["source_changes"], [])
        self.assertEqual(delta["variable_changes"], [{"name": "template:VERSION", "change": "changed"}])
        self.assertFalse(delta["definition_changed"])
        self.assertEqual(delta["reasons"], ["referenced-explicit-input-or-resolution"])
        self.assertEqual(delta["native_execution_decision"], "unknown")

    def test_source_only_change(self):
        before = self.snapshot("before.json")
        (self.root / "base.txt").write_text("changed source\n")
        delta = self.comparison(before, self.snapshot("after.json"))
        self.assertTrue(delta["source_only_changed"])
        self.assertTrue(delta["advisory_changed"])
        self.assertEqual(delta["source_changes"], [{"name": "base.txt", "change": "changed"}])
        self.assertEqual(delta["variable_changes"], [])

    def test_compare_explains_input_value_and_origin_changes(self):
        before = self.snapshot("before.json")
        self.write_fixture(version="two")
        value_changed = self.snapshot("value.json")
        delta = self.comparison(before, value_changed)
        detail, = delta["input_changes"]
        self.assertEqual(detail["name"], "template:VERSION")
        self.assertEqual(detail["change"], "changed")
        self.assertEqual(detail["changed_fields"], ["value_digest"])
        self.assertEqual(detail["before"]["value_digest"], digest({"type": "str", "value": "one"}))
        self.assertEqual(detail["after"]["value_digest"], digest({"type": "str", "value": "two"}))
        self.assertEqual(detail["before"]["value"], "<redacted>")
        self.assertEqual(detail["after"]["value"], "<redacted>")

        # Equal typed values in another scope change provenance, not value.
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    vars: {VERSION: two}\n    sources: [base.txt]\n    generates: [output.txt]\n    cmds: ['echo \"{{.VERSION}}\"']\n")
        task_scope = self.snapshot("scope.json")
        delta = self.comparison(value_changed, task_scope)
        detail, = delta["input_changes"]
        self.assertEqual(detail["changed_fields"], ["origin"])
        self.assertEqual(detail["before"]["origin"], "global.vars.VERSION")
        self.assertEqual(detail["after"]["origin"], "task.vars.VERSION")
        self.assertFalse(delta["definition_changed"])
        text = self.run_cli("compare", value_changed, task_scope).stdout
        self.assertIn("changed fields: origin", text)
        self.assertIn("global.vars.VERSION", text)
        self.assertIn("task.vars.VERSION", text)

    def test_compare_explains_resolution_transitions_and_redacts_env(self):
        self.write_fixture(command='echo "$MODE"')
        unknown = self.snapshot("unknown.json")
        envfile = self.root / "env.json"
        secret = "synthetic-private-mode"
        envfile.write_text(json.dumps({"MODE": secret}))
        known = self.snapshot("known.json", "--env-snapshot", envfile)
        for before, after, was_resolved in ((unknown, known, False), (known, unknown, True)):
            with self.subTest(was_resolved=was_resolved):
                delta = self.comparison(before, after)
                detail, = delta["input_changes"]
                self.assertEqual(detail["changed_fields"], ["classification", "origin", "resolved", "value_digest"])
                self.assertEqual(detail["before"]["resolved"], was_resolved)
                self.assertEqual(detail["after"]["resolved"], not was_resolved)
                unresolved = {"shell:MODE": {"classification": "likely-external-input", "origin": None}}
                self.assertEqual(delta["context_before"]["unresolved_inputs"], {} if was_resolved else unresolved)
                self.assertEqual(delta["context_after"]["unresolved_inputs"], unresolved if was_resolved else {})
                for flag in ([], ["--json"]):
                    output = self.run_cli("compare", before, after, *flag).stdout
                    self.assertNotIn(secret, output)
                    self.assertIn("snapshot.env.MODE", output)
                self.assertIn("unknown value", self.run_cli("compare", before, after).stdout)

    def test_compare_unchanged_retains_partial_and_unassessed_context(self):
        marker = self.root / "NEVER"
        self.taskfile.write_text(f'''version: '3'
includes: {{ignored: never-read.yml}}
vars: {{DYNAMIC: {{sh: 'touch {marker}'}}}}
tasks:
  build:
    sources: [base.txt]
    cmds: ['touch {marker}; echo $MODE']
    deps: [not-evaluated]
    status: ['touch {marker}']
''')
        before, after = self.snapshot("before.json"), self.snapshot("after.json")
        report = json.loads(self.run_cli("compare", before, after, "--json").stdout)
        self.assertFalse(report["cache_reuse_authorized"])
        delta = report["tasks"]["build"]
        self.assertEqual(delta["change"], "unchanged")
        self.assertEqual(delta["input_changes"], [])
        self.assertEqual(delta["context_before"], delta["context_after"])
        context = delta["context_before"]
        self.assertEqual(context["assessment"], "partial")
        self.assertEqual(context["unresolved_inputs"], {"shell:MODE": {"classification": "likely-external-input", "origin": None}})
        self.assertEqual([(f["scope"], f["code"]) for f in context["unassessed_findings"]],
                         [("root", "unsupported-field"), ("root", "nonliteral-value"), ("task", "unsupported-field")])
        text = self.run_cli("compare", before, after).stdout
        for expected in ("build: unchanged", "assessment: partial -> partial", "shell:MODE", "likely-external-input",
                         "root unassessed: unsupported-field includes", "task unassessed: unsupported-field deps",
                         "cache reuse authorized: false"):
            self.assertIn(expected, text)
        self.assertNotIn(str(marker), text)
        self.assertFalse(marker.exists())

    def test_compare_source_membership_content_and_stable_repeat(self):
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: ['*.txt']\n    cmds: ['cat base.txt']\n")
        (self.root / "removed.txt").write_text("removed")
        before = self.snapshot("before.json")
        (self.root / "removed.txt").unlink()
        (self.root / "added.txt").write_text("added")
        (self.root / "base.txt").write_text("edited")
        after = self.snapshot("after.json")
        delta = self.comparison(before, after)
        self.assertEqual(delta["source_changes"], [{"name": "added.txt", "change": "added"},
                                                  {"name": "base.txt", "change": "changed"},
                                                  {"name": "removed.txt", "change": "removed"}])
        self.assertEqual(delta["reasons"], ["source-content-or-membership"])
        self.assertEqual(delta["input_changes"], [])
        stable = self.snapshot("repeat.json")
        report = json.loads(self.run_cli("compare", after, stable, "--json").stdout)
        self.assertFalse(report["cache_reuse_authorized"])
        self.assertEqual(report["tasks"]["build"]["change"], "unchanged")
        self.assertEqual(report["tasks"]["build"]["context_after"],
                         {"assessment": "modeled-subset", "unresolved_inputs": {}, "unassessed_findings": []})
        self.assertEqual(self.run_cli("compare", before, after, "--json").stdout,
                         self.run_cli("compare", before, after, "--json").stdout)

    def test_compare_added_removed_tasks_keep_context_and_input_presence(self):
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: [base.txt]\n    cmds: ['echo $MODE']\n")
        before = self.snapshot("before.json")
        self.taskfile.write_text("version: '3'\ntasks:\n  added:\n    cmds: ['echo {{.CHECKSUM}}']\n")
        after = self.snapshot("after.json")
        report = json.loads(self.run_cli("compare", before, after, "--json").stdout)
        removed, added = report["tasks"]["build"], report["tasks"]["added"]
        self.assertIsNone(removed["context_after"])
        self.assertIsNone(added["context_before"])
        self.assertEqual(removed["context_before"]["assessment"], "partial")
        self.assertEqual(added["context_after"]["assessment"], "partial")
        self.assertEqual(added["context_after"]["unresolved_inputs"]["template:CHECKSUM"]["classification"], "task-runtime-variable")
        for delta, side, name in ((removed, "after", "shell:MODE"), (added, "before", "template:CHECKSUM")):
            detail, = delta["input_changes"]
            self.assertEqual(detail["name"], name)
            self.assertEqual(detail["changed_fields"], ["presence"])
            self.assertIsNone(detail[side])
        self.assertEqual(removed["source_changes"], [{"name": "base.txt", "change": "removed"}])
        text = self.run_cli("compare", before, after).stdout
        self.assertIn("assessment: absent -> partial", text)
        self.assertIn("assessment: partial -> absent", text)
        self.assertIn("task-runtime-variable", text)

    def test_explicit_env_change_and_no_host_inheritance(self):
        self.write_fixture(command='echo "$MODE" "${MODE}"')
        host = dict(os.environ, MODE="host-must-not-appear")
        unknown = self.check(env=host)["tasks"]["build"]
        self.assertFalse(unknown["inputs"]["shell:MODE"]["resolved"])
        self.assertEqual(unknown["assessment"], "partial")
        before_env, after_env = self.root / "before-env.json", self.root / "after-env.json"
        before_env.write_text('{"MODE":"before-synthetic"}')
        after_env.write_text('{"MODE":"after-synthetic"}')
        before = self.snapshot("before.json", "--env-snapshot", before_env)
        after = self.snapshot("after.json", "--env-snapshot", after_env)
        delta = self.comparison(before, after)
        self.assertTrue(delta["advisory_changed"])
        self.assertFalse(delta["source_only_changed"])
        self.assertEqual(delta["variable_changes"], [{"name": "shell:MODE", "change": "changed"}])
        self.assertNotIn("synthetic", after.read_text())

    def test_command_status_and_dynamic_vars_never_execute(self):
        marker = self.root / "NEVER"
        self.taskfile.write_text(f'''version: '3'
vars:
  UNSAFE:
    sh: touch {marker}
tasks:
  build:
    cmds: ['touch {marker}']
    status: ['touch {marker}', 'exit 0']
''')
        report = self.check()
        self.assertIn("nonliteral-value", self.codes(report))
        task = report["tasks"]["build"]
        self.assertEqual(task["native_status"], "unknown")
        self.assertEqual(task["status_script_count"], 2)
        self.snapshot()
        self.assertFalse(marker.exists())

    def test_unsupported_status_template_is_unassessed(self):
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    status: ['test {{.X | default 1}} = 1']\n")
        report = self.check("--strict", code=1)
        self.assertIn("unsupported-status-template", self.codes(report))
        self.assertEqual(report["tasks"]["build"]["native_status"], "unknown")

    def test_strict_is_nonzero_but_emits_report(self):
        report = self.check("--strict", code=1)
        self.assertIn("potential-fingerprint-gap", self.codes(report))
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: [base.txt]\n    cmds: ['cat base.txt']\n")
        self.check("--strict")

    def test_unsupported_includes_templates_unknown_fields(self):
        self.taskfile.write_text('''version: '3'
includes: {shared: /never/read.yml}
dotenv: [/never/read.env]
vars: {DYNAMIC: {sh: echo never}}
tasks:
  build:
    deps: [not-evaluated]
    cmds: ['echo {{.DYNAMIC | default "redacted"}}']
''')
        report = self.check("--strict", code=1)
        self.assertTrue({"unsupported-field", "nonliteral-value", "unsupported-template"} <= self.codes(report))
        self.assertEqual(report["tasks"]["build"]["assessment"], "partial")

    def test_missing_sources_and_unmatched_glob(self):
        for pattern in ("missing.txt", "*.absent"):
            with self.subTest(pattern=pattern):
                self.taskfile.write_text(f"version: '3'\ntasks:\n  build:\n    sources: ['{pattern}']\n")
                result = self.run_cli("check", self.taskfile, "--json", code=2)
                self.assertIn("matched no regular files", result.stderr)

    def test_glob_mapping_recursive_dotfiles_dedup_and_membership(self):
        (self.root / "src" / "nested").mkdir(parents=True)
        (self.root / "src" / "a.txt").write_text("a")
        (self.root / "src" / ".hidden.txt").write_text("hidden")
        (self.root / "src" / "nested" / "b.txt").write_text("b")
        (self.root / "src" / "literal[1].txt").write_text("brackets in matched file name")
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: ['src/**/*.txt', 'src/a.?xt']\n")
        task = self.check()["tasks"]["build"]
        self.assertEqual(set(task["source_hashes"]), {"src/a.txt", "src/.hidden.txt", "src/nested/b.txt", "src/literal[1].txt"})
        before = self.snapshot("before.json")
        (self.root / "src" / "a.txt").unlink()
        # Remove now-unmatched overlapping pattern.
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: ['src/**/*.txt']\n")
        delta = self.comparison(before, self.snapshot("after.json"))
        self.assertEqual(delta["source_changes"], [{"name": "src/a.txt", "change": "removed"}])

    def test_glob_escapes_and_unsupported_syntax(self):
        for pattern in ("../outside.txt", "/etc/passwd", "a/../../b", "./base.txt", "a//b", "a/**x.txt", "**", "[ab].txt", "{a,b}.txt", "C:/x", "a\\b"):
            with self.subTest(pattern=pattern):
                self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: [" + json.dumps(pattern) + "]\n")
                self.run_cli("check", self.taskfile, code=2)

    def test_symlink_file_and_directory_escape(self):
        (self.root / "link.txt").symlink_to(self.root / "base.txt")
        for pattern in ("link.txt", "*.txt"):
            self.taskfile.write_text(f"version: '3'\ntasks:\n  build:\n    sources: ['{pattern}']\n")
            self.assertIn("symlink", self.run_cli("check", self.taskfile, code=2).stderr)
        (self.root / "link.txt").unlink()
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: ['linkdir/base.txt']\n")
        self.run_cli("check", self.taskfile, code=2)

    def test_duplicate_yaml_keys_at_all_levels(self):
        docs = ["version: '3'\nversion: '3'\ntasks: {}", "version: '3'\nvars: {X: a, X: b}\ntasks: {build: {}}", "version: '3'\ntasks:\n  build:\n    sources: []\n    sources: [base.txt]"]
        for doc in docs:
            self.taskfile.write_text(doc)
            self.assertIn("duplicate", self.run_cli("check", self.taskfile, code=2).stderr)

    def test_yaml_graph_tags_merges_and_malformed_redacted(self):
        docs = ["version: '3'\nvars: &v {X: secret-value}\ntasks: {build: {vars: *v}}", "version: '3'\ntasks: {build: !!python/object:bad {}}", "version: '3'\ntasks: {build: {<<: {cmds: []}}}", "version: '3'\nvars: {SECRET: super-secret\ntasks: [", "version: '3'\ntasks: {build: {vars: {F: 1e999}}}"]
        for doc in docs:
            self.taskfile.write_text(doc)
            result = self.run_cli("check", self.taskfile, code=2)
            self.assertNotIn("super-secret", result.stderr)
            self.assertNotIn("secret-value", result.stderr)

    def test_values_redacted_in_json_text_snapshot_and_compare(self):
        secret = "synthetic-SECRET-72db"
        self.write_fixture(version=secret)
        result = self.run_cli("check", self.taskfile, "--json")
        self.assertNotIn(secret, result.stdout)
        text = self.run_cli("check", self.taskfile).stdout
        self.assertNotIn(secret, text)
        self.assertIn("<redacted>", text)
        before = self.snapshot("before.json")
        self.assertNotIn(secret, before.read_text())
        self.assertEqual(before.stat().st_mode & 0o777, 0o600)
        self.write_fixture(version="different-secret")
        after = self.snapshot("after.json")
        self.assertNotIn("different-secret", self.run_cli("compare", before, after).stdout)

    def test_no_output_overwrite(self):
        path = self.snapshot()
        contents = path.read_bytes()
        self.run_cli("snapshot", self.taskfile, "--output", path, code=2)
        self.assertEqual(path.read_bytes(), contents)
        source = self.root / "base.txt"
        self.run_cli("snapshot", self.taskfile, "--output", source, code=2)
        self.assertEqual(source.read_text(), "synthetic source\n")

    def test_malformed_snapshots_and_integrity_failure(self):
        valid_path = self.snapshot("valid.json")
        valid = json.loads(valid_path.read_text())
        bad_path = self.root / "bad.json"
        malformed = [None, {}, {"tasks": {}}, [], {**valid, "schema_version": 2}, {**valid, "model": "other"}]
        wrong_hash = copy.deepcopy(valid)
        wrong_hash["tasks"]["build"]["source_hashes"]["base.txt"] = "0" * 64
        malformed.append(wrong_hash)
        wrong_value = copy.deepcopy(valid)
        wrong_value["tasks"]["build"]["inputs"]["template:VERSION"]["value"] = "exposed-secret"
        malformed.append(wrong_value)
        wrong_class = copy.deepcopy(valid)
        wrong_class["tasks"]["build"]["inputs"]["template:VERSION"]["classification"] = []
        malformed.append(wrong_class)
        for value in malformed:
            bad_path.write_text(json.dumps(value))
            self.run_cli("compare", valid_path, bad_path, "--json", code=2)
        for raw in ('{"tasks":{},"tasks":{}}', '{"x":NaN}', '{'):
            bad_path.write_text(raw)
            self.run_cli("compare", valid_path, bad_path, code=2)

    def test_var_and_env_scopes_with_shadowing(self):
        self.taskfile.write_text('''version: '3'
vars: {V: global-var}
env: {E: root-env, SHADOW: root-value}
tasks:
  build:
    vars: {V: task-var}
    env: {SHADOW: task-value, LOCAL: local-env}
    cmds: ['echo {{.V}} {{.E}} {{.SHADOW}} $E $SHADOW $LOCAL $V']
''')
        task = self.check()["tasks"]["build"]
        inputs = task["inputs"]
        self.assertEqual(inputs["template:V"]["origin"], "task.vars.V")
        self.assertEqual(inputs["template:E"]["origin"], "global.env.E")
        self.assertEqual(inputs["template:SHADOW"]["origin"], "global.env.SHADOW")
        self.assertEqual(inputs["shell:SHADOW"]["origin"], "task.env.SHADOW")
        self.assertEqual(inputs["shell:LOCAL"]["origin"], "task.env.LOCAL")
        self.assertFalse(inputs["shell:V"]["resolved"])
        self.assertTrue(any("not automatically exported" in f["message"] for f in task["findings"]))

    def test_env_collision_is_unassessed_and_not_guessed(self):
        self.taskfile.write_text("version: '3'\nenv: {MODE: declared}\ntasks:\n  build:\n    cmds: ['echo $MODE']\n")
        envfile = self.root / "env.json"
        envfile.write_text('{"MODE":"snapshot"}')
        report = self.check("--env-snapshot", envfile, "--strict", code=1)
        self.assertIn("env-precedence-unknown", self.codes(report))
        self.assertFalse(report["tasks"]["build"]["inputs"]["shell:MODE"]["resolved"])

    def test_runtime_vars_and_possible_shell_locals_are_not_external(self):
        self.write_fixture(command='echo {{.CHECKSUM}} {{.GENERATES}}; TMP=value; echo $TMP')
        report = self.check("--strict", code=1)
        inputs = report["tasks"]["build"]["inputs"]
        self.assertEqual(inputs["template:CHECKSUM"]["classification"], "task-runtime-variable")
        self.assertEqual(inputs["shell:TMP"]["classification"], "possible-shell-local")
        self.assertNotIn("undefined-input", self.codes(report))

    def test_shell_unsupported_operators_and_lexical_reference_warning(self):
        self.write_fixture(command='echo $MODE ${ALT:-fallback} $(date) $1')
        report = self.check("--strict", code=1)
        self.assertIn("unsupported-shell-expansion", self.codes(report))
        self.assertIn("shell:MODE", report["tasks"]["build"]["inputs"])
        self.assertEqual(report["tasks"]["build"]["assessment"], "partial")

    def test_generates_without_inputs_and_templated_sources(self):
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    generates: [out.txt]\n    cmds: ['echo hi']\n")
        self.assertIn("outputs-without-inputs", self.codes(self.check("--strict", code=1)))
        self.taskfile.write_text("version: '3'\ntasks:\n  build:\n    sources: ['{{.DIR}}/*.txt']\n")
        report = self.check("--strict", code=1)
        self.assertIn("templated-path", self.codes(report))
        self.assertEqual(report["tasks"]["build"]["source_hashes"], {})

    def test_env_snapshot_validation(self):
        path = self.root / "env.json"
        for raw in ('[]', '{"MODE":true}', '{"MODE":"a","MODE":"b"}', '{"BAD-KEY":"x"}', 'null'):
            path.write_text(raw)
            self.run_cli("check", self.taskfile, "--env-snapshot", path, code=2)

    def test_bad_schema_types_and_version(self):
        for raw in ("[]", "version: 2\ntasks: {build: {}}", "version: '3'\ntasks: {}", "version: '3'\ntasks: {build: {sources: base.txt}}", "version: '3'\ntasks: {build: {vars: []}}", "version: '3'\ntasks: {build: {cmds: [false]}}", "version: '3'\ntasks: {build: {status: false}}"):
            self.taskfile.write_text(raw)
            self.run_cli("check", self.taskfile, code=2)

    def test_task_selection_shorthand_and_definition_changes(self):
        self.taskfile.write_text("version: '3'\ntasks:\n  build: ['echo one']\n  other: echo hi\n")
        report = self.check("--task", "build")
        self.assertEqual(set(report["tasks"]), {"build"})
        self.check("--task", "other")
        self.run_cli("check", self.taskfile, "--task", "absent", code=2)
        before = self.snapshot("before.json")
        self.taskfile.write_text("version: '3'\ntasks:\n  build: ['echo two']\n  added: echo hi\n")
        after = self.snapshot("after.json")
        compared = json.loads(self.run_cli("compare", before, after, "--json").stdout)
        self.assertTrue(compared["tasks"]["build"]["definition_changed"])
        self.assertTrue(compared["tasks"]["build"]["advisory_changed"])
        self.assertEqual(compared["tasks"]["other"]["change"], "removed")
        self.assertEqual(compared["tasks"]["added"]["change"], "added")

    def test_scalar_types_and_no_yaml11_boolean_coercion(self):
        self.taskfile.write_text("version: '3'\nvars: {A: yes, B: true, C: 7, D: 1.5, E: 2026-11-11}\ntasks:\n  build:\n    cmds: ['echo {{.A}} {{.B}} {{.C}} {{.D}} {{.E}}']\n")
        inputs = self.check()["tasks"]["build"]["inputs"]
        for name, value in {"A": "yes", "B": True, "C": 7, "D": 1.5, "E": "2026-11-11"}.items():
            self.assertEqual(inputs[f"template:{name}"]["value_digest"], digest({"type": type(value).__name__, "value": value}))

    def test_unused_literal_changes_and_mtime_do_not_change_fingerprints(self):
        before = self.snapshot("before.json")
        self.taskfile.write_text(self.taskfile.read_text().replace("VERSION: 'one'", "VERSION: 'one'\n  UNUSED: 'new-unused-value'"))
        os.utime(self.root / "base.txt", (1000000000, 1000000000))
        after = self.snapshot("after.json")
        self.assertEqual(self.comparison(before, after)["change"], "unchanged")

    def test_strict_snapshot_is_written_but_returns_one(self):
        path = self.snapshot("strict.json", "--strict", code=1)
        self.assertIn("build", json.loads(path.read_text())["tasks"])

    def test_regular_file_inputs_only(self):
        fifo = self.root / "pipe.yml"
        os.mkfifo(fifo)
        self.assertIn("regular file", self.run_cli("check", fifo, code=2).stderr)
        self.run_cli("check", self.root / "absent.yml", code=2)

    def test_terminal_control_characters_are_escaped(self):
        self.taskfile.write_text('version: "3"\ntasks:\n  "bad\\u001b[31m\\ninjected": "echo hi"\n')
        result = self.run_cli("check", self.taskfile)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("\\n", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_document_size_limit(self):
        self.taskfile.write_text("#" + "x" * (2 * 1024 * 1024))
        self.assertIn("exceeds", self.run_cli("check", self.taskfile, code=2).stderr)

    def test_repository_acceptance_examples(self):
        report = json.loads(self.run_cli("check", EXAMPLES / "Taskfile.yml", "--root", EXAMPLES, "--json").stdout)
        self.assertEqual(set(report["tasks"]), {"build", "status-demo", "external"})
        self.run_cli("snapshot", EXAMPLES / "Taskfile.yml", "--root", EXAMPLES, "--output", self.root / "acceptance.json")
        self.run_cli("check", EXAMPLES / "unsupported.yml", "--strict", code=1)
        self.run_cli("check", EXAMPLES / "missing-source.yml", code=2)


if __name__ == "__main__":
    unittest.main()
