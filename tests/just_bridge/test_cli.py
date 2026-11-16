import contextlib
import errno
import io
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench import __version__
from stackwrench.just_bridge.cli import main

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "just"]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jtb-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "justfile"
        self.destination = self.directory / "Taskfile.yml"
        self.source.write_text("build: clean\n    echo building\nclean:\n    echo cleaning\n")

    def run_cli(self, *arguments):
        return subprocess.run([*CLI, *map(str, arguments)], cwd=self.directory,
                              text=True, capture_output=True, timeout=10)

    def test_console_entry_point_and_module(self):
        actual = self.run_cli("--version")
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout, f"{__version__}\n")
        actual = subprocess.run([sys.executable, "-m", "stackwrench.just_bridge", "--version"],
                                cwd=self.directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout, f"{__version__}\n")

    def test_full_plan_json_and_text(self):
        actual = self.run_cli("plan", self.source, "--json")
        self.assertEqual(actual.returncode, 0, actual.stderr)
        report = json.loads(actual.stdout)
        self.assertTrue(report["supported"])
        self.assertEqual(report["graph"], {"build": ["clean"], "clean": []})
        self.assertEqual(report["traces"]["build"], ["clean", "build"])
        self.assertEqual(report["mappings"]["recipes"][0]["commands"][0]["taskCommand"], "sh -cu 'echo building'")
        actual = self.run_cli("plan", self.source)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        for text in ["build -> clean", "Trace build: clean -> build", "Mapping: build -> tasks.build", "warning [echo-format]"]:
            self.assertIn(text, actual.stdout)
        self.assertFalse(self.destination.exists())

    def test_convert_new_destination_exact_taskfile(self):
        before = self.source.read_bytes()
        actual = self.run_cli("convert", self.source, "--output", self.destination)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout, "")
        self.assertEqual(json.loads(self.destination.read_text()), {
            "version": "3", "run": "once", "tasks": {
                "default": {"cmds": [{"task": "build"}]},
                "build": {"cmds": [{"task": "clean"}, "sh -cu 'echo building'"]},
                "clean": {"cmds": ["sh -cu 'echo cleaning'"]}}})
        self.assertEqual(self.source.read_bytes(), before)
        self.assertIn("warning [runner-environment]", actual.stderr)

    def test_stdout_matches_file_and_dash_output(self):
        actual = self.run_cli("convert", self.source)
        dash = self.run_cli("convert", self.source, "--output", "-")
        file = self.run_cli("convert", self.source, "--output", self.destination)
        for result in (actual, dash, file):
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual.stdout, self.destination.read_text())
        self.assertEqual(dash.stdout, actual.stdout)
        self.assertFalse((self.directory / "-").exists())

    def test_disk_write_failure_leaves_no_partial_destination(self):
        # A real Linux/POSIX file-size limit forces a partial write/flush failure.
        def limit_file_size():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (64, 64))

        before = self.source.read_bytes()
        actual = subprocess.run(
            [*CLI, "convert", str(self.source), "-o", str(self.destination)],
            cwd=self.directory, text=True, capture_output=True, timeout=10,
            preexec_fn=limit_file_size)
        self.assertEqual(actual.returncode, 3, actual.stderr)
        self.assertIn("output-error", actual.stderr)
        self.assertEqual(actual.stdout, "")
        self.assertFalse(self.destination.exists(), "a failed write must not publish a partial Taskfile")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(set(self.directory.iterdir()), {self.source})

    def invoke_convert(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["convert", str(self.source), "-o", str(self.destination), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_destination_is_invisible_until_complete_and_synced(self):
        real_sync, real_link = os.fsync, os.link
        expected = self.run_cli("convert", self.source).stdout

        def sync(fd):
            self.assertFalse(self.destination.exists())
            self.assertEqual(os.fstat(fd).st_size, len(expected.encode("utf-8")))
            real_sync(fd)

        def publish(staged, destination):
            self.assertFalse(self.destination.exists())
            self.assertEqual(Path(staged).read_text(), expected)
            self.assertEqual(Path(staged).parent, self.destination.parent)
            return real_link(staged, destination)

        with mock.patch("os.fsync", side_effect=sync) as synced, \
             mock.patch("os.link", side_effect=publish) as published:
            code, stdout, stderr = self.invoke_convert()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "")
        synced.assert_called_once()
        published.assert_called_once()
        self.assertEqual(self.destination.read_text(), expected)
        self.assertEqual(set(self.directory.iterdir()), {self.source, self.destination})

    def test_sync_or_publish_failure_cleans_staging_and_creates_no_destination(self):
        for operation in ("os.fsync", "os.link"):
            with self.subTest(operation=operation):
                with mock.patch(operation, side_effect=OSError(errno.EIO, "synthetic I/O failure")):
                    code, stdout, stderr = self.invoke_convert("--report", "json")
                self.assertEqual(code, 3, stderr)
                self.assertIn("output-error", stderr)
                self.assertNotIn('"supported": true', stderr)
                self.assertEqual(stdout, "")
                self.assertEqual(set(self.directory.iterdir()), {self.source})

    def test_destination_race_never_overwrites_file_or_dangling_symlink(self):
        real_link = os.link
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind):
                def competitor_wins(staged, destination):
                    if kind == "file":
                        self.destination.write_text("competitor data")
                    else:
                        self.destination.symlink_to(self.directory / "absent")
                    return real_link(staged, destination)

                with mock.patch("os.link", side_effect=competitor_wins):
                    code, stdout, stderr = self.invoke_convert()
                self.assertEqual(code, 3, stderr)
                self.assertIn("output-error", stderr)
                self.assertEqual(stdout, "")
                if kind == "file":
                    self.assertEqual(self.destination.read_text(), "competitor data")
                else:
                    self.assertTrue(self.destination.is_symlink())
                    self.assertFalse((self.directory / "absent").exists())
                self.assertEqual(set(self.directory.iterdir()), {self.source, self.destination})
                self.destination.unlink()

    def test_convert_attached_report_preserves_chain_and_duplicate_calls(self):
        self.source.write_text("root: left right left\nleft: base\nright: base\nbase:\n")
        actual = self.run_cli("convert", self.source, "-o", self.destination, "--report", "json")
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout, "")
        report = json.loads(actual.stderr)
        expected = json.loads(self.run_cli("plan", self.source, "--json").stdout)
        self.assertEqual(report, expected)
        self.assertEqual(report["traces"]["root"], ["base", "left", "right", "root"])
        self.assertEqual(report["mappings"]["recipes"][0]["dependencies"], ["left", "right", "left"])
        self.assertEqual([(r["sourceRecipe"], r["task"]) for r in report["mappings"]["recipes"]],
                         [(name, name) for name in ("root", "left", "right", "base")])
        self.assertEqual(report["mappings"]["default"],
                         {"sourceRecipe": "root", "task": "default", "synthetic": True})
        self.assertEqual(json.loads(self.destination.read_text())["tasks"]["root"]["cmds"],
                         [{"task": "left"}, {"task": "right"}, {"task": "left"}])
        actual = self.run_cli("convert", self.source, "--report", "text")
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout, self.destination.read_text())
        for text in ("Mapping: root -> tasks.root", "Trace root: base -> left -> right -> root",
                     "Default: root -> task default", "warning [runner-environment]"):
            self.assertIn(text, actual.stderr)

    def test_unsupported_conversion_has_no_success_report(self):
        self.source.write_text("a:\n  echo safe\nimport 'unsupported.just'\n")
        actual = self.run_cli("convert", self.source, "-o", self.destination, "--report", "json")
        self.assertEqual(actual.returncode, 2)
        self.assertIn(f"{self.source}:3: error [unsupported-syntax]", actual.stderr)
        self.assertNotIn('"supported": true', actual.stderr)
        self.assertEqual(actual.stdout, "")
        self.assertEqual(set(self.directory.iterdir()), {self.source})

    def test_source_and_existing_destination_never_overwritten(self):
        before = self.source.read_bytes()
        for destination in (self.source, self.destination):
            if destination != self.source:
                destination.write_text("preserve")
            actual = self.run_cli("convert", self.source, "-o", destination)
            self.assertEqual(actual.returncode, 3)
            self.assertIn("output-error", actual.stderr)
            self.assertEqual(actual.stdout, "")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.destination.read_text(), "preserve")

    def test_symlink_hardlink_and_dangling_link_never_followed(self):
        target = self.directory / "target"
        target.write_text("preserve")
        paths = [self.directory / name for name in ("symlink", "hardlink", "dangling")]
        paths[0].symlink_to(target)
        os.link(self.source, paths[1])
        paths[2].symlink_to(self.directory / "absent")
        before = self.source.read_bytes()
        for destination in paths:
            actual = self.run_cli("convert", self.source, "-o", destination)
            self.assertEqual(actual.returncode, 3, actual.stderr)
        self.assertEqual(target.read_text(), "preserve")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse((self.directory / "absent").exists())

    def test_destination_directory_and_missing_parent_fail(self):
        for path in [self.directory, self.directory / "absent" / "Taskfile.yml"]:
            actual = self.run_cli("convert", self.source, "-o", path)
            self.assertEqual(actual.returncode, 3)
            self.assertEqual(actual.stdout, "")
        self.assertFalse((self.directory / "absent").exists())

    def test_unknown_syntax_line_number_no_partial_file_or_stdout(self):
        self.source.write_text("good:\n  echo good\nimport 'secret.just'\n")
        actual = self.run_cli("convert", self.source, "-o", self.destination)
        self.assertEqual(actual.returncode, 2)
        self.assertIn(f"{self.source}:3: error [unsupported-syntax]", actual.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(actual.stdout, "")
        stdout = self.run_cli("convert", self.source)
        self.assertEqual(stdout.stdout, "")
        self.assertEqual(stdout.returncode, 2)

    def test_invalid_plan_json_and_text_have_nonzero_status(self):
        self.source.write_text("a: b\nb: a\n")
        actual = self.run_cli("plan", self.source, "--json")
        self.assertEqual(actual.returncode, 2)
        report = json.loads(actual.stdout)
        self.assertFalse(report["supported"])
        self.assertEqual(report["mappings"]["recipes"], [])
        self.assertEqual(report["traces"], {})
        self.assertIn("dependency-cycle", [d["code"] for d in report["diagnostics"]])
        actual = self.run_cli("plan", self.source)
        self.assertEqual(actual.returncode, 2)
        self.assertIn("dependency cycle: a -> b -> a", actual.stdout)

    def test_duplicate_missing_and_expression_fail_e2e(self):
        for source, code in [("a:\na:\n", "duplicate-recipe"),
                             ("a: absent\n", "missing-dependency"),
                             ("x := env_var('HOME')\na:\n", "unsupported-variable")]:
            with self.subTest(code=code):
                self.source.write_text(source)
                actual = self.run_cli("convert", self.source, "-o", self.destination)
                self.assertEqual(actual.returncode, 2)
                self.assertIn(code, actual.stderr)
                self.assertFalse(self.destination.exists())

    def test_missing_non_file_and_invalid_utf8_input(self):
        invalid = self.directory / "invalid"
        invalid.write_bytes(b"a:\n  echo \xff\n")
        for path in [self.directory / "missing", self.directory, invalid]:
            with self.subTest(path=path.name):
                actual = self.run_cli("convert", path, "-o", self.destination)
                self.assertEqual(actual.returncode, 3)
                self.assertEqual(actual.stdout, "")
                self.assertFalse(self.destination.exists())
                actual = self.run_cli("plan", path, "--json")
                self.assertEqual(actual.returncode, 3)
                self.assertFalse(json.loads(actual.stdout)["supported"])

    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.directory / "fifo"
        os.mkfifo(fifo)
        actual = self.run_cli("plan", fifo, "--json")
        self.assertEqual(actual.returncode, 3)
        self.assertIn("regular UTF-8 file", json.loads(actual.stdout)["diagnostics"][0]["message"])

    def test_input_size_limit_e2e(self):
        self.source.write_bytes(b"#" * 1_048_577)
        actual = self.run_cli("convert", self.source, "-o", self.destination)
        self.assertEqual(actual.returncode, 2)
        self.assertIn("input-limit", actual.stderr)
        self.assertFalse(self.destination.exists())

    def test_injection_strings_are_inert_during_plan_and_conversion(self):
        self.source.write_text("payload := '$(touch assignment-marker)'\na:\n  touch direct-marker\n  printf '%s' '{{payload}}'\n  printf '%s' \"$(touch substitution-marker)\"\n")
        actual = self.run_cli("plan", self.source, "--json")
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertTrue(json.loads(actual.stdout)["supported"])
        actual = self.run_cli("convert", self.source, "-o", self.destination)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertIn("$(touch assignment-marker)", self.destination.read_text())
        for marker in ["assignment-marker", "direct-marker", "substitution-marker"]:
            self.assertFalse((self.directory / marker).exists())
        self.source.write_text("x := `touch backtick-marker`\na:\n  touch partial-marker\n")
        actual = self.run_cli("convert", self.source, "-o", self.directory / "rejected.yml")
        self.assertEqual(actual.returncode, 2)
        for marker in ["backtick-marker", "partial-marker", "rejected.yml"]:
            self.assertFalse((self.directory / marker).exists())

    def test_synthetic_examples_through_installed_cli(self):
        actual = self.run_cli("plan", ROOT / "examples/just_bridge/justfile", "--json")
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(json.loads(actual.stdout)["traces"]["build"], ["prepare", "clean", "check", "build"])
        actual = self.run_cli("convert", ROOT / "examples/just_bridge/justfile", "-o", self.destination)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        actual = self.run_cli("convert", ROOT / "examples/just_bridge/rejected.justfile", "-o", self.directory / "bad.yml")
        self.assertEqual(actual.returncode, 2, actual.stderr)
        self.assertFalse((self.directory / "bad.yml").exists())

    def test_argument_errors(self):
        for arguments in [[], ["unknown"], ["plan"], ["convert", str(self.source), "--force"]]:
            with self.subTest(arguments=arguments):
                actual = self.run_cli(*arguments)
                self.assertEqual(actual.returncode, 2)
                self.assertEqual(actual.stdout, "")
