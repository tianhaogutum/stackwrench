"""Redacted template contracts exercised through the installed console entry point."""

import json
import os
from unittest.mock import patch

from .test_trace import ROOT, Workspace


class Templates(Workspace):
    def fixture(self):
        compose = self.compose(
            "      TOKEN_VALUE: '${TOKEN:?synthetic-private-message}'\n"
            "      MODE_VALUE: '${MODE:-synthetic-private-default}'\n"
            "      EMPTY_VALUE: '${EMPTY?synthetic-private-empty-message}'\n"
            "      DUPLICATE: '$TOKEN'\n"
            "      PASS:\n"
            "      LITERAL: '$$ESCAPED'\n")
        inputs = self.put("input.env", "TOKEN=synthetic-private-token\nEMPTY=\nUNUSED=synthetic-private-unused\n")
        return compose, inputs

    def test_cli_minimal_template_and_source_fields(self):
        compose, inputs = self.fixture()
        output = self.root / "template.env"
        proc = self.run_cli("--compose", compose, "--env-file", inputs,
                            "--env-template", output, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        template = output.read_text(encoding="utf-8")
        from stackwrench.compose_env import env_template
        self.assertEqual(template, env_template(report))
        self.assertEqual([line for line in template.splitlines() if line.startswith("# variable:")], [
            "# variable: EMPTY; required: set (empty allowed)",
            "# variable: MODE; required: no",
            "# variable: PASS; required: no",
            "# variable: TOKEN; required: nonempty",
        ])
        for name in ["EMPTY", "MODE", "PASS", "TOKEN"]:
            self.assertEqual(template.splitlines().count(f"# {name}="), 1)
        for value in ["synthetic-private", "UNUSED", "TOKEN_VALUE=", "ESCAPED", "MODE_VALUE="]:
            self.assertNotIn(value, template)
        self.assertIn("default when unset or empty", template)
        self.assertIn("required when unset; empty allowed", template)
        self.assertIn("required when unset or empty", template)
        self.assertIn('["services", "demo", "environment", "TOKEN_VALUE"]', template)
        self.assertIn("inherited-null", template)
        self.assertFalse(any(line and not line.startswith("#") for line in template.splitlines()))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        # The untouched template declares nothing, and cannot turn unset into empty.
        plain = self.compose("      VALUE: '${PASS-fallback}'\n      PASS:\n")
        replay = self.result(plain, output)
        self.assertEqual(self.values(replay), {"VALUE": "fallback", "PASS": None})

    def test_existing_file_and_symlink_are_never_overwritten(self):
        compose, inputs = self.fixture()
        original = self.put("existing.env", "KEEP=synthetic-preserve\n")
        link = self.root / "link.env"
        link.symlink_to(original)
        dangling = self.root / "dangling.env"
        dangling.symlink_to(self.root / "absent.env")
        for output in [original, link, dangling, self.root]:
            with self.subTest(output=output):
                self.assert_cli_error("--compose", compose, "--env-file", inputs, "--env-template", output)
        self.assertEqual(original.read_text(), "KEEP=synthetic-preserve\n")
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.root / "absent.env").exists())

    def test_failure_writes_nothing_and_restricted_syntax_still_fails(self):
        for expression in ["${MISSING:?synthetic-private-message}", "${VAR:-${OTHER}}", "${VAR:=synthetic-private}"]:
            with self.subTest(expression=expression):
                compose = self.compose(f"      VALUE: '{expression}'\n")
                output = self.root / "template.env"
                self.assert_cli_error("--compose", compose, "--env-template", output,
                                      "--show-values", forbidden=("synthetic-private",))
                self.assertFalse(output.exists())

    def test_checked_in_example_and_show_values_cannot_fill_template(self):
        output = self.root / "template.env"
        proc = self.run_cli("--compose", ROOT / "examples/compose_env/template/compose.yaml",
                            "--env-file", ROOT / "examples/compose_env/template/inputs.env",
                            "--env-template", output, "--show-values", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("synthetic-token", proc.stdout)
        self.assertNotIn("synthetic-token", output.read_text())

    def test_no_host_environment_changes_or_implicit_loading(self):
        compose = self.compose("      VALUE: '${HOST_ONLY-default}'\n")
        self.put(".env", "HOST_ONLY=synthetic-implicit\n")
        output = self.root / "template.env"
        from stackwrench.compose_env import env_template, trace
        with patch.dict(os.environ, {"HOST_ONLY": "synthetic-host"}):
            before = dict(os.environ)
            template = env_template(trace(compose))
            self.assertEqual(dict(os.environ), before)
        self.assertIn("# HOST_ONLY=", template)
        self.assertNotIn("synthetic-host", template)
        self.assertFalse(output.exists())  # Library rendering is pure.

    def test_all_contexts_operator_constraints_and_stable_deduplication(self):
        compose = self.compose(
            "      - PASS\n"
            "      - PASS=overridden\n"
            "      - 'RESULT=${BOTH?private-message}|${BOTH:?private-message}|${BOTH-default}|${ALT+private-word}|${ALT:+private-word}|$COPY'\n",
            fields="    env_file: service.env\n")
        inputs = self.put("input.env", "BOTH=private-value\nCOPY=${BASE:-private-default}\nLITERAL='$NOT_A_REFERENCE'\nUNUSED=private-unused\n")
        self.put("service.env", "LOCAL=private-local\nDERIVED=$LOCAL\nRESULT=${FALLBACK-default}\nLITERAL='$NOT_A_REFERENCE_EITHER'\n")
        from stackwrench.compose_env import env_template, trace
        hidden = env_template(trace(compose, [inputs]))
        self.assertEqual(hidden, env_template(trace(compose, [inputs], show_values=True)))
        names = [line.removeprefix("# variable: ").split(";")[0]
                 for line in hidden.splitlines() if line.startswith("# variable:")]
        self.assertEqual(names, ["ALT", "BASE", "BOTH", "COPY", "FALLBACK", "LOCAL", "PASS"])
        self.assertIn("# variable: BOTH; required: nonempty", hidden)
        self.assertIn("default when unset; empty is preserved", hidden)
        self.assertIn("alternative when set, including empty", hidden)
        self.assertIn("alternative when nonempty", hidden)
        self.assertIn("inherited-key-only", hidden)  # Includes a shadowed inheritance.
        self.assertIn('interpolation-input; :-:', hidden)
        self.assertIn('field: ["COPY"]', hidden)
        self.assertIn('service-env-file; direct:', hidden)
        self.assertIn('field: ["DERIVED"]', hidden)
        self.assertNotIn("private-", hidden)
        self.assertNotIn("NOT_A_REFERENCE", hidden)

    def test_comment_metadata_cannot_inject_assignments(self):
        # An arbitrary extension field is valid interpolation metadata; environment
        # names remain restricted to identifiers, so use the extension instead.
        compose = self.put("compose\nINJECTED=bad.yaml", json.dumps({
            "services": {"demo": {"odd\nINJECTED_FIELD=bad": "${REF:-private-default}"}}
        }))
        from stackwrench.compose_env import env_template, trace
        template = env_template(trace(compose))
        self.assertIn(r"\nINJECTED=bad.yaml", template)
        self.assertTrue(all(not line or line.startswith("#") for line in template.splitlines()))
        self.assertNotIn("private-default", template)

    def test_invalid_output_and_scenario_combination_have_no_partial_stdout(self):
        compose, inputs = self.fixture()
        self.assert_cli_error("--compose", compose, "--env-file", inputs,
                              "--env-template", self.root / "missing-parent" / "private-name.env",
                              forbidden=("private-name",))
        manifest = self.put("scenarios.json", '{"one":{},"two":{}}')
        output = self.root / "template.env"
        proc = self.run_cli("--compose", compose, "--scenario-file", manifest,
                            "--env-template", output)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(output.exists())
