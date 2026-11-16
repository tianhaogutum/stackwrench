"""Acceptance-tool regressions; mocked processes are not runner equivalence evidence."""

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge.cli import main

CLI = [sys.executable, "-m", "stackwrench", "just"]


class AcceptanceCliTests(unittest.TestCase):
    def test_installed_verify_missing_runners_is_structured_skip_not_pass(self):
        with tempfile.TemporaryDirectory(prefix="jtb-acceptance-cli-") as temp:
            directory = Path(temp)
            # A tempting local recipe must never be read or executed by verify.
            (directory / "justfile").write_text("root:\n  touch USER_COMMAND_RAN\n")
            environment = dict(os.environ, PATH="")
            actual = subprocess.run([*CLI, "verify"], cwd=directory,
                                    env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(actual.returncode, 4, actual.stderr)
            report = json.loads(actual.stdout)
            self.assertEqual(report["formatVersion"], 1)
            self.assertEqual(report["suiteVersion"], 2)
            self.assertEqual([case["name"] for case in report["cases"]],
                             ["ordered-diamond", "dependency-failure", "unset-variable-failure"])
            unset = report["cases"][2]
            self.assertEqual(unset["expected"], {"stdout": "shared\nstop\n", "exitCode": None,
                                                "exitCodePolicy": "nonzero", "order": ["shared", "stop"]})
            self.assertEqual(report["status"], "skipped")
            self.assertIn("just", report["reason"])
            self.assertIn("task", report["reason"])
            self.assertTrue(all(case["status"] == "skipped" for case in report["cases"]))
            self.assertFalse((directory / "USER_COMMAND_RAN").exists())
            self.assertEqual(set(p.name for p in directory.iterdir()), {"justfile"})
            self.assertEqual(actual.stderr, "")

    def test_report_output_is_exclusive_and_user_recipe_argument_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="jtb-acceptance-report-") as temp:
            directory = Path(temp)
            destination = directory / "report.json"
            environment = dict(os.environ, PATH="")
            def run(*args):
                return subprocess.run([*CLI, "verify", *map(str, args)], cwd=directory,
                                      env=environment, capture_output=True, text=True, timeout=10)
            actual = run("--output", destination)
            self.assertEqual(actual.returncode, 4, actual.stderr)
            self.assertEqual(actual.stdout, "")
            before = destination.read_bytes()
            self.assertEqual(json.loads(before)["status"], "skipped")
            again = run("--output", destination)
            self.assertEqual(again.returncode, 3, again.stderr)
            self.assertIn("output-error", again.stderr)
            self.assertEqual(destination.read_bytes(), before)
            self.assertEqual(set(directory.iterdir()), {destination})
            rejected = run("user.justfile")
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")


class AcceptanceEngineTests(unittest.TestCase):
    def engine(self):
        from stackwrench.just_bridge import acceptance
        return acceptance

    def test_missing_one_runner_never_executes_available_runner(self):
        engine = self.engine()
        for missing in ("just", "task"):
            with self.subTest(missing=missing), \
                 mock.patch.object(engine.shutil, "which", side_effect=lambda name, **kwargs: None if name == missing else "/bin/" + name), \
                 mock.patch.object(engine.subprocess, "run") as process:
                report = engine.verify()
            self.assertEqual(report["status"], "skipped")
            self.assertIn(missing, report["reason"])
            process.assert_not_called()

    def run_engine(self, mutate=None):
        engine = self.engine()
        calls, directories = [], []
        def process(argv, **kwargs):
            directory = Path(kwargs["cwd"])
            calls.append((argv, kwargs))
            directories.append(directory)
            self.assertTrue(directory.is_dir())
            self.assertNotEqual(directory, Path.cwd())
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["env"]["PATH"], os.defpath)
            self.assertNotIn("JUST_JUSTFILE", kwargs["env"])
            self.assertNotIn("TASK_X_ENV_PRECEDENCE", kwargs["env"])
            self.assertEqual(kwargs["timeout"], 15)
            runner = "just" if Path(argv[0]).name == "just" else "task"
            if argv[1:] == ["--version"]:
                return subprocess.CompletedProcess(argv, 0, (runner + " pinned-test-version\n").encode(), b"")
            fixture = next(f for f in engine.FIXTURES if directory.parent.name == f.name)
            if runner == "just":
                self.assertEqual(argv[1:4], ["--no-dotenv", "--justfile", "justfile"])
                self.assertEqual((directory / "justfile").read_text(), fixture.source)
            else:
                self.assertEqual(argv[1:5], ["--silent", "--exit-code", "--taskfile", "Taskfile.yml"])
                taskfile = json.loads((directory / "Taskfile.yml").read_text())
                self.assertEqual(taskfile["version"], "3")
                self.assertEqual(taskfile["run"], "once")
            code = fixture.exit_code if fixture.exit_code is not None else 2
            stdout, trace = fixture.stdout.encode(), "".join(n + "\n" for n in fixture.order).encode()
            if mutate:
                code, stdout, trace = mutate(runner, fixture, code, stdout, trace)
            if trace is not None:
                (directory / "trace.txt").write_bytes(trace)
            return subprocess.CompletedProcess(argv, code, stdout, b"runner diagnostics differ")
        with mock.patch.object(engine.shutil, "which", side_effect=lambda name, **kwargs: "/bin/" + name), \
             mock.patch.object(engine.subprocess, "run", side_effect=process), \
             mock.patch.dict(os.environ, {"JUST_JUSTFILE": "user.justfile", "TASK_X_ENV_PRECEDENCE": "1"}):
            report = engine.verify()
        self.assertTrue(all(not path.exists() for path in directories), "synthetic directories must be cleaned")
        self.assertTrue(all(call[1]["env"] == calls[0][1]["env"] for call in calls))
        return report

    def test_completed_report_checks_expected_values_not_only_runner_agreement(self):
        report = self.run_engine()
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(case["status"] == "passed" and all(case["checks"].values()) for case in report["cases"]))
        failure = next(c for c in report["cases"] if c["name"] == "dependency-failure")
        self.assertEqual(failure["expected"]["exitCode"], 7)
        self.assertEqual(failure["expected"]["order"], ["shared", "stop"])
        self.assertEqual(failure["observations"]["task"]["exitCode"], 7)
        self.assertIn("pinned-test-version", report["runners"]["just"]["version"]["stdout"])
        # Identical wrong output from both runners must fail against the oracle.
        wrong = self.run_engine(lambda runner, fixture, code, stdout, trace: (code, b"wrong\n", trace))
        self.assertEqual(wrong["status"], "failed")
        self.assertTrue(wrong["cases"][0]["checks"]["stdoutEqual"])
        self.assertFalse(wrong["cases"][0]["checks"]["expectedStdout"])

    def test_stdout_exit_and_dependency_order_mismatches_are_independent(self):
        for field in ("stdout", "exit", "order", "missing-trace", "newline"):
            with self.subTest(field=field):
                def change(runner, fixture, code, stdout, trace):
                    if runner == "task":
                        if field == "stdout": stdout += b"\xff"
                        if field == "exit": code = 99
                        if field == "order": trace = b"root\nshared\n"
                        if field == "missing-trace": trace = None
                        if field == "newline": trace = trace.replace(b"\n", b"\r\n")
                    return code, stdout, trace
                report = self.run_engine(change)
                self.assertEqual(report["status"], "failed")
                case = report["cases"][0]
                check = {"stdout": "stdoutEqual", "exit": "exitCodeEqual"}.get(field, "orderEqual")
                self.assertFalse(case["checks"][check])
                if field == "stdout":
                    raw = base64.b64decode(case["observations"]["task"]["stdoutBase64"])
                    self.assertTrue(raw.endswith(b"\xff"))

    def test_unset_failure_requires_positive_equal_codes_and_exact_prefix(self):
        report = self.run_engine()
        case = next((c for c in report["cases"] if c["name"] == "unset-variable-failure"), None)
        self.assertIsNotNone(case, "verify must exercise unset-variable failure")
        self.assertEqual(report["suiteVersion"], 2)
        self.assertEqual(case["expected"], {"stdout": "shared\nstop\n", "exitCode": None,
                                           "exitCodePolicy": "nonzero", "order": ["shared", "stop"]})
        for code in (1, 2, 127, 0, -9):
            with self.subTest(code=code):
                def change(runner, fixture, original, stdout, trace):
                    return (code if fixture.name == "unset-variable-failure" else original), stdout, trace
                actual = self.run_engine(change)
                unset = actual["cases"][2]
                self.assertTrue(unset["checks"]["exitCodeEqual"])
                self.assertEqual(unset["checks"]["expectedExitCode"], code > 0)
                self.assertEqual(actual["status"], "passed" if code > 0 else "failed")
        for field in ("code", "stdout", "trace"):
            with self.subTest(field=field):
                def change(runner, fixture, code, stdout, trace):
                    if fixture.name == "unset-variable-failure":
                        if field == "code" and runner == "task": code = 9
                        if field == "stdout": stdout += b"after-expansion\nlater-line\ntail\nroot\n"
                        if field == "trace": trace += b"after-expansion\nlater-line\ntail\nroot\n"
                    return code, stdout, trace
                actual = self.run_engine(change)
                self.assertEqual(actual["status"], "failed")
                check = {"code": "exitCodeEqual", "stdout": "expectedStdout", "trace": "expectedOrder"}[field]
                self.assertFalse(actual["cases"][2]["checks"][check])

    def test_unset_fixture_generated_shells_stop_and_detect_missing_u(self):
        engine = self.engine()
        fixture = next((f for f in engine.FIXTURES if f.name == "unset-variable-failure"), None)
        self.assertIsNotNone(fixture, "verify must include an unset-variable fixture")
        from stackwrench.just_bridge import analyze, render_taskfile
        document = json.loads(render_taskfile(analyze(fixture.source)))
        # This small interpreter exercises only generated sequential task calls
        # and real shell commands. It does not stand in for a live Task runner.
        for remove_u in (False, True):
            with self.subTest(remove_u=remove_u), tempfile.TemporaryDirectory(prefix="jtb-unset-") as temp:
                visited, outputs = set(), []
                def run(name):
                    if name in visited:
                        return 0
                    visited.add(name)
                    for command in document["tasks"][name]["cmds"]:
                        if isinstance(command, dict):
                            code = run(command["task"])
                        else:
                            if remove_u:
                                self.assertTrue(command.startswith("sh -cu "))
                                command = command.replace("sh -cu ", "sh -c ", 1)
                            result = subprocess.run(["sh", "-c", command], cwd=temp,
                                                    env={"PATH": os.defpath, "JTB_UNSET_055": "inherited"},
                                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
                            outputs.append(result.stdout)
                            code = result.returncode
                        if code:
                            return code
                    return 0
                code = run("root")
                stdout = b"".join(outputs)
                trace = (Path(temp) / "trace.txt").read_bytes()
                if remove_u:
                    self.assertEqual(code, 0)
                    self.assertEqual(stdout, b"shared\nstop\n\nafter-expansion\nlater-line\ntail\nroot\n")
                    self.assertEqual(trace, b"shared\nstop\nafter-expansion\nlater-line\ntail\nroot\n")
                else:
                    self.assertGreater(code, 0)
                    self.assertEqual(stdout, b"shared\nstop\n")
                    self.assertEqual(trace, b"shared\nstop\n")

    def test_timeout_launch_and_version_errors_are_unverifiable(self):
        engine = self.engine()
        for error in (subprocess.TimeoutExpired(["just"], 15, output=b"partial"), OSError("cannot launch")):
            with self.subTest(error=error), \
                 mock.patch.object(engine.shutil, "which", side_effect=lambda name, **kwargs: "/bin/" + name), \
                 mock.patch.object(engine.subprocess, "run", side_effect=error):
                report = engine.verify()
            self.assertEqual(report["status"], "error")
            self.assertTrue(all(c["status"] == "error" for c in report["cases"]))
            self.assertIsNone(report["cases"][0]["checks"])
        with mock.patch.object(engine.shutil, "which", side_effect=lambda name, **kwargs: "/bin/" + name), \
             mock.patch.object(engine.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, b"", b"unsupported")):
            report = engine.verify()
        self.assertEqual(report["status"], "error")
        self.assertIn("version", report["reason"])

    def test_fixture_timeout_retains_partial_output_and_never_passes(self):
        def timeout(runner, fixture, code, stdout, trace):
            if runner == "task":
                raise subprocess.TimeoutExpired([runner], 15, output=b"partial")
            return code, stdout, trace
        report = self.run_engine(timeout)
        self.assertEqual(report["status"], "error")
        for case in report["cases"]:
            self.assertEqual(case["status"], "error")
            self.assertIsNone(case["checks"])
            self.assertEqual(case["observations"]["task"]["stdout"], "partial")

    def test_fixture_io_failure_is_a_versioned_error_report(self):
        engine = self.engine()
        with mock.patch.object(engine.shutil, "which", side_effect=lambda name, **kwargs: "/bin/" + name), \
             mock.patch.object(engine.tempfile, "TemporaryDirectory", side_effect=OSError("disk full")):
            report = engine.verify()
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["formatVersion"], 1)
        self.assertIn("disk full", report["reason"])
        self.assertTrue(all(case["status"] == "error" for case in report["cases"]))

    def test_builtin_fixture_shell_contracts(self):
        # Run only literal fixture commands in independently specified recipe
        # order. This validates the shell oracle, not either runner's graph.
        engine = self.engine()
        from stackwrench.just_bridge import analyze
        for fixture in engine.FIXTURES:
            with self.subTest(fixture=fixture.name), tempfile.TemporaryDirectory(prefix="jtb-oracle-") as temp:
                analysis = analyze(fixture.source)
                self.assertTrue(analysis.supported, analysis.report())
                outputs, code = [], 0
                for name in fixture.order:
                    for command in analysis.recipes[name].commands:
                        actual = subprocess.run(["sh", "-cu", command.expanded], cwd=temp,
                                                env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
                                                capture_output=True, timeout=5)
                        outputs.append(actual.stdout)
                        code = actual.returncode
                        if code:
                            break
                    if code:
                        break
                self.assertEqual(b"".join(outputs), fixture.stdout.encode())
                if fixture.exit_code is None:
                    self.assertGreater(code, 0)
                else:
                    self.assertEqual(code, fixture.exit_code)
                self.assertEqual((Path(temp) / "trace.txt").read_text().splitlines(), list(fixture.order))

    def test_cli_propagates_failed_and_error_report_status(self):
        engine = self.engine()
        for status, expected_code in (("failed", 1), ("error", 3), ("passed", 0)):
            stdout = io.StringIO()
            with mock.patch.object(engine, "verify", return_value={"status": status}), contextlib.redirect_stdout(stdout):
                self.assertEqual(main(["verify"]), expected_code)
            self.assertEqual(json.loads(stdout.getvalue())["status"], status)
