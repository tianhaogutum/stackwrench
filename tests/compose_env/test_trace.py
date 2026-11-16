"""Semantic and full installed-console E2E tests. Sequential, synthetic inputs only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench import __version__
from stackwrench.compose_env import TraceError, trace

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "compose-env"]


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def compose(self, env="      RESULT: '${VAR}'\n", fields="", top=""):
        return self.put("compose.yaml", "services:\n  demo:\n    image: synthetic:demo\n" + fields + "    environment:\n" + env + top)

    def run_cli(self, *argv, env=None, cwd=None):
        return subprocess.run([*CLI, *map(str, argv)], capture_output=True, text=True,
                              cwd=cwd or self.root, env=env, timeout=15)

    def result(self, compose, *envfiles, shell=None):
        return trace(compose, list(envfiles), shell, show_values=True)

    def values(self, report, service="demo"):
        return {name: item["winner"]["value"] for name, item in report["services"][service]["variables"].items()}

    def assert_cli_error(self, *args, forbidden=()):
        proc = self.run_cli(*args, "--json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("error", json.loads(proc.stderr))
        self.assertNotIn("Traceback", proc.stderr)
        for value in forbidden:
            self.assertNotIn(value, proc.stderr)
        return proc.stderr


class Semantics(Workspace):
    def test_operator_truth_table(self):
        # Independent, explicit expected results: unset, empty, set. None means required failure.
        cases = {
            "$VAR": ("", "", "value"), "${VAR}": ("", "", "value"),
            "${VAR:-word}": ("word", "word", "value"),
            "${VAR-word}": ("word", "", "value"),
            "${VAR:?message}": (None, None, "value"),
            "${VAR?message}": (None, "", "value"),
            "${VAR:+word}": ("", "", "word"),
            "${VAR+word}": ("", "word", "word"),
        }
        for expression, results in cases.items():
            for number, expected in enumerate(results):
                with self.subTest(expression=expression, state=number):
                    compose = self.compose(f"      RESULT: '{expression}'\n")
                    shell = self.put("shell.json", json.dumps([{}, {"VAR": ""}, {"VAR": "value"}][number]))
                    if expected is None:
                        with self.assertRaisesRegex(TraceError, "required variable VAR"):
                            self.result(compose, shell=shell)
                    else:
                        report = self.result(compose, shell=shell)
                        self.assertEqual(self.values(report)["RESULT"], expected)
                        use = report["interpolation"]["uses"][0]
                        self.assertEqual(use["input_status"], ["unset", "empty", "set"][number])
                        self.assertEqual(use["input_source"]["source"] if number else None,
                                         "shell-snapshot" if number else None)

    def test_escaped_dollar_nonrecursive_and_no_shell_execution(self):
        marker = self.root / "must-not-exist"
        compose = self.compose(f"      RESULT: '$$VAR|$VAR|${{VAR}}|$(touch {marker})|`touch {marker}`'\n")
        shell = self.put("shell.json", '{"VAR":"$SECOND"}')
        report = self.result(compose, shell=shell)
        self.assertEqual(self.values(report)["RESULT"], f"$VAR|$SECOND|$SECOND|$(touch {marker})|`touch {marker}`")
        self.assertFalse(marker.exists())

    def test_two_phases_sources_order_null_and_empty(self):
        compose = self.compose("      MODE: ''\n      INHERIT:\n      UNRESOLVED:\n      COPIED: '${PORT}'\n", fields="    env_file: [a.env, b.env]\n")
        self.put("a.env", "MODE=dev\nORDER=first\nUNRESOLVED=overridden\n")
        self.put("b.env", "ORDER=second\nPORT=container-only\n")
        first = self.put("first.env", "PORT=8080\nINHERIT=first\n")
        second = self.put("second.env", "PORT=9090\nINHERIT=second\n")
        shell = self.put("shell.json", '{"PORT":"8181","INHERIT":"shell"}')
        report = self.result(compose, first, second, shell=shell)
        self.assertEqual(self.values(report), {"MODE": "", "ORDER": "second", "PORT": "container-only", "INHERIT": "shell", "UNRESOLVED": None, "COPIED": "8181"})
        inputs = report["interpolation"]["variables"]
        self.assertEqual(len(inputs["PORT"]["shadowed"]), 2)
        variables = report["services"]["demo"]["variables"]
        self.assertEqual(variables["MODE"]["winner"]["status"], "empty")
        self.assertEqual(variables["MODE"]["shadowed"][0]["source"], "service-env-file")
        self.assertEqual(variables["INHERIT"]["winner"]["syntax"], "inherited-null")
        self.assertEqual(variables["INHERIT"]["winner"]["resolved_from"]["source"], "shell-snapshot")
        self.assertEqual(report["services"]["demo"]["image_env"], "not-modeled")
        use = next(u for u in report["interpolation"]["uses"] if u.get("variable") == "PORT")
        self.assertEqual(use["input_source"]["source"], "shell-snapshot")
        self.assertIn(":", use["location"])

    def test_list_syntax_interpolated_names_and_duplicate_history(self):
        compose = self.compose("      - INHERIT\n      - EMPTY=\n      - '${KEY}=first'\n      - '${KEY}=second'\n")
        shell = self.put("shell.json", '{"KEY":"RENAMED","INHERIT":"ok"}')
        report = self.result(compose, shell=shell)
        self.assertEqual(self.values(report), {"INHERIT": "ok", "EMPTY": "", "RENAMED": "second"})
        var = report["services"]["demo"]["variables"]["RENAMED"]
        self.assertEqual(var["shadowed"][0]["value"], "first")
        self.assertEqual(report["services"]["demo"]["variables"]["INHERIT"]["winner"]["syntax"], "inherited-key-only")

    def test_dotenv_quotes_comments_and_escapes(self):
        compose = self.compose("      SINGLE:\n      DOUBLE:\n      HASH:\n      COMMENT:\n      EMPTY:\n      SPACED:\n")
        dotenv = self.put("input.env", r'''# comment
SINGLE='Let\'s $OTHER # literal'
DOUBLE="a\tline\nnext\rreturn\\slash\"quote" # comment
HASH=left#right
COMMENT=value # ignored
EMPTY= # comment
 SPACED = spaced value
''')
        report = self.result(compose, dotenv)
        self.assertEqual(self.values(report), {"SINGLE": "Let's $OTHER # literal", "DOUBLE": 'a\tline\nnext\rreturn\\slash"quote', "HASH": "left#right", "COMMENT": "value", "EMPTY": "", "SPACED": "spaced value"})

    def test_same_file_and_previous_file_lookup(self):
        compose = self.compose("      A:\n      B:\n      C:\n      D:\n")
        first = self.put("first.env", "A=first\nB=old\n")
        second = self.put("second.env", "C=$A\nA=second\nD=$A\nB=$C\n")
        report = self.result(compose, first, second)
        self.assertEqual(self.values(report), {"A": "second", "B": "first", "C": "first", "D": "second"})
        shell = self.put("shell.json", '{"A":"shell"}')
        self.assertEqual(self.values(self.result(compose, first, second, shell=shell)), {"A": "shell", "B": "shell", "C": "shell", "D": "shell"})

    def test_service_dotenv_lookup_same_file_and_previous_file(self):
        compose = self.compose("      RESULT: '${A:-not-from-service}'\n", fields="    env_file: [a.env, b.env]\n")
        self.put("a.env", "A=first\nBASE=base\n")
        self.put("b.env", "BEFORE=$A\nA=second\nAFTER=$A\nB=$BASE\n")
        report = self.result(compose)
        self.assertEqual(self.values(report), {"A": "second", "BASE": "base", "BEFORE": "first", "AFTER": "second", "B": "base", "RESULT": "not-from-service"})
        shell = self.put("shell.json", '{"A":"shell"}')
        values = self.values(self.result(compose, shell=shell))
        self.assertEqual(values["BEFORE"], "shell")
        self.assertEqual(values["AFTER"], "shell")
        self.assertEqual(values["A"], "second")

    def test_no_host_or_implicit_dotenv_even_with_control_variables(self):
        compose = self.compose("      RESULT: '${PORT:-offline}'\n      PORT:\n")
        self.put(".env", "PORT=implicit\n")
        with patch.dict(os.environ, {"PORT": "host", "COMPOSE_FILE": "ignored.yaml", "COMPOSE_ENV_FILES": "ignored.env"}):
            report = self.result(compose)
        self.assertEqual(self.values(report), {"RESULT": "offline", "PORT": None})
        self.assertEqual(report["host_environment"], "not-read")

    def test_other_fields_interpolated_but_explicitly_not_evaluated(self):
        compose = self.compose("      RESULT: ok\n", fields="    command: ['echo', '${OTHER:-x}']\n    profiles: [inactive]\n", top="volumes:\n  data: {}\n")
        report = self.result(compose)
        self.assertEqual(len(report["interpolation"]["uses"]), 1)
        fields = {f["field"] for f in report["not_evaluated_fields"]}
        self.assertEqual(fields, {"image", "command", "profiles", "volumes"})

    def test_canonical_integer_environment(self):
        report = self.result(self.compose("      ZERO: 0\n      NEGATIVE: -12\n      PORT: 8080\n"))
        self.assertEqual(self.values(report), {"ZERO": "0", "NEGATIVE": "-12", "PORT": "8080"})

    def test_dynamic_service_envfile_path_and_relative_base(self):
        compose = self.compose("      RESULT: ok\n", fields="    env_file: '${INPUT_FILE}'\n")
        self.put("nested/file.env", "FILE_VAR=safe\n")
        shell = self.put("shell.json", '{"INPUT_FILE":"nested/file.env"}')
        report = trace(compose, shell_env=shell)
        self.assertNotIn("nested/file.env", json.dumps(report))
        winner = report["services"]["demo"]["variables"]["FILE_VAR"]["winner"]
        self.assertIn("env_file[0]:1", winner["location"])


class FailurePaths(Workspace):
    def test_required_error_is_redacted_even_show_values(self):
        compose = self.compose("      FIRST: '${KNOWN}'\n      RESULT: '${MISSING:?private-custom-message}'\n")
        env = self.put("input.env", "KNOWN=private-supplied-value\n")
        error = self.assert_cli_error("--compose", compose, "--env-file", env, "--show-values", forbidden=("private-custom-message", "private-supplied-value"))
        self.assertIn("MISSING", error)
        self.assertIn(":6:", error)

    def test_missing_compose_and_dotenv(self):
        self.assert_cli_error("--compose", self.root / "missing.yaml")
        self.assert_cli_error("--compose", ROOT / "examples/compose_env/required-error.yaml")
        self.assert_cli_error("--compose", ROOT / "examples/compose_env/compose.yaml", "--env-file", ROOT / "examples/compose_env/invalid.env")
        compose = self.compose()
        self.assert_cli_error("--compose", compose, "--env-file", self.root / "missing.env")
        compose = self.compose(fields="    env_file: missing.env\n")
        self.assert_cli_error("--compose", compose)

    def test_invalid_dotenv_rejected_without_values(self):
        compose = self.compose()
        for text in ['BAD="private-unclosed', "export BAD=private-export", "BAD: private-colon", 'BAD="private"suffix', 'BAD="private\\q"', "BAD", "BAD='private\nline'", 'BAD=private"quote']:
            with self.subTest(text=text):
                env = self.put("input.env", text)
                self.assert_cli_error("--compose", compose, "--env-file", env, forbidden=("private",))

    def test_invalid_json_snapshot_and_duplicate_keys(self):
        compose = self.compose()
        for text in ['{"VAR":"private-unclosed}', '{"VAR":"private-first","VAR":"private-second"}', '{"VAR":null}', '["private-array"]', '{"VAR":123}', '{"BAD-KEY":"private-value"}']:
            with self.subTest(text=text):
                shell = self.put("shell.json", text)
                self.assert_cli_error("--compose", compose, "--shell-env", shell, forbidden=("private",))

    def test_nested_and_unsupported_interpolation(self):
        for expression in ["${VAR:-${OTHER}}", "${VAR:-$OTHER}", "${VAR/foo/bar}", "${VAR:2}", "${VAR", "${}", "${VAR:=secret}"]:
            with self.subTest(expression=expression):
                compose = self.compose(f"      RESULT: '{expression}'\n")
                self.assert_cli_error("--compose", compose, forbidden=("secret",))

    def test_unsupported_envfile_shapes_and_merge(self):
        for fields in ["    env_file: [{path: data.env, format: unsupported}]\n", "    env_file: {path: data.env, format: raw}\n", "    env_file: [123]\n", "    extends: {file: other.yaml, service: demo}\n"]:
            with self.subTest(fields=fields):
                compose = self.compose(fields=fields)
                self.assert_cli_error("--compose", compose)
        for top in ["include: [other.yaml]\n", "env_file: input.env\n", "jobs: {}\n"]:
            self.assert_cli_error("--compose", self.compose(top=top))

    def test_invalid_yaml_duplicate_keys_anchors_tags_multidoc(self):
        cases = ["services: [private-broken", "services: {}\nservices: {}\n", "services: &private {}\n", "services: *private\n", "services: !private {}\n", "services: {}\n---\nservices: {}\n", "services: {demo: {<<: {environment: {A: private}}}}", "", "services: []", "private-root: {}", "services: {demo: []}"]
        for text in cases:
            with self.subTest(text=text):
                self.assert_cli_error("--compose", self.put("compose.yaml", text), forbidden=("private",))

    def test_invalid_environment_types_names_and_numbers(self):
        for env in ["      FLAG: true\n", "      FLAG: yes\n", "      FLOAT: 1.20\n", "      SCIENTIFIC: 1e3\n", "      OCTAL12: 0o12\n", "      OCTAL: 012\n", "      NEGZERO: -0\n", "      HUGE: 999999999999999999999\n", "      OBJECT: {}\n", "      - 123\n", "      - BAD-NAME=private\n", "      BAD-NAME: private\n", "      A: private\n      A: other\n", "      '${VAR}': private\n"]:
            with self.subTest(env=env):
                self.assert_cli_error("--compose", self.compose(env), forbidden=("private",))

    def test_file_size_encoding_and_nesting_limits(self):
        compose = self.put("compose.yaml", " " * (2 * 1024 * 1024 + 1))
        self.assert_cli_error("--compose", compose)
        compose.write_bytes(b"services: \xff")
        self.assert_cli_error("--compose", compose)
        compose = self.put("compose.yaml", "services: {demo: {image: safe, other: " + "[" * 70 + "x" + "]" * 70 + "}}")
        self.assert_cli_error("--compose", compose)

    def test_expansion_and_report_limits(self):
        compose = self.compose("      RESULT: '$VAR$VAR$VAR'\n")
        shell = self.put("shell.json", json.dumps({"VAR": "x" * (1024 * 1024)}))
        self.assert_cli_error("--compose", compose, "--shell-env", shell)
        compose = self.compose("      RESULT: '" + "$$" * 10001 + "'\n")
        self.assert_cli_error("--compose", compose)
        if hasattr(os, "mkfifo"):
            path = self.root / "pipe.env"
            os.mkfifo(path)
            self.assert_cli_error("--compose", self.compose(), "--env-file", path)

    def test_dynamic_filename_error_does_not_leak_value(self):
        compose = self.compose(fields="    env_file: '${PATH_VALUE}'\n")
        shell = self.put("shell.json", '{"PATH_VALUE":"private-path-value"}')
        self.assert_cli_error("--compose", compose, "--shell-env", shell, forbidden=("private-path-value",))


class ConsoleE2E(Workspace):
    def test_acceptance_example_redaction_and_values_opt_in(self):
        args = ["--compose", ROOT / "examples/compose_env/compose.yaml", "--env-file", ROOT / "examples/compose_env/.env", "--json"]
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["values_shown"])
        self.assertNotIn('"value":', result.stdout)
        for secret in ["8080", "safe literal", "from-explicit-input", "hello # literal", "fallback"]:
            self.assertNotIn(secret, result.stdout)
        shown = self.run_cli(*args, "--show-values", "--shell-env", ROOT / "examples/compose_env/shell.json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        report = json.loads(shown.stdout)
        self.assertEqual(self.values(report, "web")["PORT"], "8181")
        self.assertEqual(self.values(report, "web")["APP_MODE"], "")
        self.assertEqual(self.values(report, "web")["ESCAPED"], "$PORT")
        self.assertEqual(self.values(report, "worker")["INHERIT"], "from-shell-snapshot")

    def test_console_text_and_module_entrypoint(self):
        compose = self.compose("      RESULT: '${UNKNOWN}'\n")
        result = self.run_cli("--compose", compose)
        self.assertEqual(result.returncode, 0, result.stderr)
        for phrase in ["Interpolation inputs", "Interpolation uses", "Container environment", "Values hidden", "unresolved-substituted-empty", "Not evaluated"]:
            self.assertIn(phrase, result.stdout)
        proc = subprocess.run([sys.executable, "-m", "stackwrench", "compose-env", "--version"], capture_output=True, text=True, timeout=15)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, __version__))

    def test_cli_rejects_multiple_compose_and_unknown_arguments(self):
        compose = self.compose()
        proc = self.run_cli("--compose", compose, "--compose", compose)
        # Repeated --compose must never silently select the last file.
        self.assertEqual(proc.returncode, 2)
        self.assertIn("multi-file merge unsupported", proc.stderr)
        proc = self.run_cli("--compose", compose, "--private-flag", "private-argument")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("private", proc.stderr)

    def test_cli_host_env_is_not_input(self):
        compose = self.compose("      RESULT: '${VAR:-offline}'\n")
        env = dict(os.environ, VAR="host-private")
        result = self.run_cli("--compose", compose, "--json", "--show-values", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.values(json.loads(result.stdout)), {"RESULT": "offline"})


if __name__ == "__main__":
    unittest.main()
