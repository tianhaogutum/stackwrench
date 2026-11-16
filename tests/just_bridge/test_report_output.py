"""Conversion sidecar contracts, exercised through the installed package/CLI."""

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

from stackwrench.just_bridge.cli import main

CLI = [sys.executable, "-m", "stackwrench", "just"]


class ReportOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jtb-sidecar-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "justfile"
        self.source.write_text("root: left right left\nleft: base\nright: base\nbase:\n")
        self.taskfile = self.directory / "Taskfile.yml"
        # Different directories also exercise same-directory staging per file.
        self.reports = self.directory / "reports"
        self.reports.mkdir()
        self.report = self.reports / "conversion.json"

    def cli(self, *arguments, **kwargs):
        return subprocess.run([*CLI, *map(str, arguments)], cwd=self.directory,
                              text=True, capture_output=True, timeout=10, **kwargs)

    def convert(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["convert", str(self.source), "-o", str(self.taskfile),
                         "--report-output", str(self.report), *map(str, arguments)])
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_no_outputs(self):
        self.assertEqual(set(self.directory.iterdir()), {self.source, self.reports})
        self.assertEqual(list(self.reports.iterdir()), [])

    def test_installed_sidecar_matches_plan_and_attached_reports(self):
        before = self.source.read_bytes()
        expected = self.cli("plan", self.source, "--json")
        self.assertEqual(expected.returncode, 0, expected.stderr)
        for attached in (None, "text", "json"):
            with self.subTest(attached=attached):
                arguments = [] if attached is None else ["--report", attached]
                actual = self.cli("convert", self.source, "-o", self.taskfile,
                                  "--report-output", self.report, *arguments)
                self.assertEqual(actual.returncode, 0, actual.stderr)
                self.assertEqual(actual.stdout, "")
                self.assertEqual(self.report.read_text(), expected.stdout)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["formatVersion"], 1)
                self.assertEqual(report["traces"]["root"], ["base", "left", "right", "root"])
                self.assertEqual(json.loads(self.taskfile.read_text())["tasks"]["root"]["cmds"],
                                 [{"task": "left"}, {"task": "right"}, {"task": "left"}])
                if attached == "json":
                    self.assertEqual(actual.stderr, expected.stdout)
                elif attached == "text":
                    self.assertIn("Trace root: base -> left -> right -> root", actual.stderr)
                else:
                    self.assertIn("warning [runner-environment]", actual.stderr)
                self.assertEqual(self.report.stat().st_mode & 0o777, 0o600)
                self.assertEqual(set(self.directory.iterdir()), {self.source, self.reports, self.taskfile})
                self.assertEqual(list(self.reports.iterdir()), [self.report])
                self.report.unlink()
                self.taskfile.unlink()
        self.assertEqual(self.source.read_bytes(), before)

    def test_sidecar_with_stdout_and_dash_taskfile_output(self):
        expected = self.cli("convert", self.source).stdout
        for output in ([], ["--output", "-"]):
            with self.subTest(output=output):
                actual = self.cli("convert", self.source, *output, "--report-output", self.report)
                self.assertEqual(actual.returncode, 0, actual.stderr)
                self.assertEqual(actual.stdout, expected)
                self.assertTrue(json.loads(self.report.read_text())["supported"])
                self.report.unlink()
        self.assert_no_outputs()

    def test_real_report_write_failure_publishes_neither_artifact(self):
        taskfile_bytes = len(self.cli("convert", self.source).stdout.encode())
        report_bytes = len(self.cli("plan", self.source, "--json").stdout.encode())
        self.assertGreater(report_bytes, taskfile_bytes + 32)

        def limit_report_size():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (taskfile_bytes + 32,) * 2)

        for output in (["-o", self.taskfile], []):
            with self.subTest(output=output):
                actual = self.cli("convert", self.source, *output, "--report-output", self.report,
                                  "--report", "json", preexec_fn=limit_report_size)
                self.assertEqual(actual.returncode, 3, actual.stderr)
                self.assertIn(f"{self.report}: error [output-error]", actual.stderr)
                self.assertNotIn('"supported": true', actual.stderr)
                self.assertEqual(actual.stdout, "")
                self.assert_no_outputs()

    def test_report_write_flush_close_sync_and_publication_errors(self):
        real_temporary = tempfile.NamedTemporaryFile
        real_sync = os.fsync
        for operation in ("write", "flush", "close", "sync", "publish"):
            with self.subTest(operation=operation):
                error = OSError(errno.ENOSPC, "synthetic report failure")

                @contextlib.contextmanager
                def temporary(*args, **kwargs):
                    is_report = Path(kwargs["dir"]) == self.report.parent
                    with real_temporary(*args, **kwargs) as stream:
                        if is_report and operation in ("write", "flush"):
                            original = getattr(stream, operation)

                            def fail(*args, **kwargs):
                                if operation == "write":
                                    original(args[0][:8])
                                raise error

                            setattr(stream, operation, fail)
                        yield stream
                    if is_report and operation == "close":
                        raise error

                def sync(fd):
                    if operation == "sync":
                        raise error
                    return real_sync(fd)

                with mock.patch("tempfile.NamedTemporaryFile", side_effect=temporary), \
                     mock.patch("os.fsync", side_effect=sync), \
                     mock.patch("os.link", side_effect=error) as publish:
                    code, stdout, stderr = self.convert("--report", "json")
                self.assertEqual(code, 3, stderr)
                self.assertIn(str(self.report), stderr)
                self.assertIn("output-error", stderr)
                self.assertNotIn('"supported": true', stderr)
                self.assertEqual(stdout, "")
                if operation != "publish":
                    publish.assert_not_called()
                self.assert_no_outputs()

    def test_both_named_artifacts_are_complete_and_synced_before_publication(self):
        real_sync, real_link = os.fsync, os.link
        synced, published = [], []

        def sync(fd):
            self.assertFalse(self.report.exists())
            self.assertFalse(self.taskfile.exists())
            synced.append(os.fstat(fd).st_size)
            real_sync(fd)

        def publish(staged, destination):
            self.assertEqual(len(synced), 2)
            self.assertEqual(Path(staged).parent, destination.parent)
            content = json.loads(Path(staged).read_text())
            if destination == self.report:
                self.assertEqual(published, [])
                self.assertTrue(content["supported"])
                self.assertFalse(self.taskfile.exists())
            else:
                self.assertEqual(published, [self.report])
                self.assertEqual(content["version"], "3")
            published.append(destination)
            return real_link(staged, destination)

        with mock.patch("os.fsync", side_effect=sync), mock.patch("os.link", side_effect=publish):
            code, stdout, stderr = self.convert()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(published, [self.report, self.taskfile])

    def test_taskfile_staging_failure_publishes_neither_artifact(self):
        real_temporary = tempfile.NamedTemporaryFile

        def temporary(*args, **kwargs):
            if Path(kwargs["dir"]) == self.taskfile.parent:
                raise OSError(errno.EIO, "synthetic Taskfile staging failure")
            return real_temporary(*args, **kwargs)

        with mock.patch("tempfile.NamedTemporaryFile", side_effect=temporary), \
             mock.patch("os.link") as publish:
            code, stdout, stderr = self.convert()
        self.assertEqual(code, 3, stderr)
        self.assertIn(str(self.taskfile), stderr)
        self.assertEqual(stdout, "")
        publish.assert_not_called()
        self.assert_no_outputs()

    def test_report_destination_race_preserves_competitor_and_no_taskfile(self):
        real_link = os.link
        for kind in ("file", "dangling-symlink"):
            with self.subTest(kind=kind):
                def publish(staged, destination):
                    self.assertEqual(destination, self.report)
                    if kind == "file":
                        self.report.write_text("competitor")
                    else:
                        self.report.symlink_to(self.reports / "absent")
                    return real_link(staged, destination)

                with mock.patch("os.link", side_effect=publish):
                    code, stdout, stderr = self.convert()
                self.assertEqual(code, 3, stderr)
                self.assertEqual(stdout, "")
                self.assertFalse(self.taskfile.exists())
                if kind == "file":
                    self.assertEqual(self.report.read_text(), "competitor")
                else:
                    self.assertTrue(self.report.is_symlink())
                self.assertEqual(list(self.reports.iterdir()), [self.report])
                self.report.unlink()
                self.assert_no_outputs()

    def test_taskfile_publication_failure_retains_report_and_names_it(self):
        real_link = os.link
        for kind in ("io-error", "racing-file", "racing-symlink"):
            with self.subTest(kind=kind):
                def publish(staged, destination):
                    if destination == self.taskfile:
                        if kind == "io-error":
                            raise OSError(errno.EIO, "synthetic Taskfile publication failure")
                        if kind == "racing-file":
                            self.taskfile.write_text("competitor")
                        else:
                            self.taskfile.symlink_to(self.directory / "absent")
                    return real_link(staged, destination)

                with mock.patch("os.link", side_effect=publish):
                    code, stdout, stderr = self.convert("--report", "json")
                self.assertEqual(code, 3, stderr)
                self.assertEqual(stdout, "")
                self.assertIn("report retained", stderr)
                self.assertIn(str(self.report), stderr)
                self.assertNotIn('"supported": true', stderr)
                self.assertTrue(json.loads(self.report.read_text())["supported"])
                if kind == "io-error":
                    self.assertFalse(self.taskfile.exists())
                elif kind == "racing-file":
                    self.assertEqual(self.taskfile.read_text(), "competitor")
                else:
                    self.assertTrue(self.taskfile.is_symlink())
                self.taskfile.unlink(missing_ok=True)
                self.report.unlink()
                self.assert_no_outputs()

    def test_existing_and_invalid_report_paths_never_publish_taskfile(self):
        target = self.reports / "target"
        target.write_text("preserve")
        symlink = self.reports / "symlink"
        symlink.symlink_to(target)
        dangling = self.reports / "dangling"
        dangling.symlink_to(self.reports / "absent")
        hardlink = self.reports / "hardlink"
        os.link(self.source, hardlink)
        before = self.source.read_bytes()
        for report in (self.source, target, symlink, dangling, hardlink, self.reports,
                       self.reports / "missing" / "report.json"):
            with self.subTest(report=report):
                actual = self.cli("convert", self.source, "-o", self.taskfile,
                                  "--report-output", report)
                self.assertEqual(actual.returncode, 3, actual.stderr)
                self.assertIn("output-error", actual.stderr)
                self.assertEqual(actual.stdout, "")
                self.assertFalse(self.taskfile.exists())
                self.assertEqual(self.source.read_bytes(), before)
                self.assertEqual(target.read_text(), "preserve")
        self.assertEqual(set(self.reports.iterdir()), {target, symlink, dangling, hardlink})

    def test_stdout_failure_retains_published_report(self):
        stderr = io.StringIO()
        with mock.patch("sys.stdout.write", side_effect=OSError(errno.EPIPE, "closed stdout")), \
             contextlib.redirect_stderr(stderr):
            code = main(["convert", str(self.source), "--report-output", str(self.report)])
        self.assertEqual(code, 3, stderr.getvalue())
        self.assertIn("<stdout>: error [output-error]", stderr.getvalue())
        self.assertIn(f"report retained at {self.report}", stderr.getvalue())
        self.assertTrue(json.loads(self.report.read_text())["supported"])
        self.report.unlink()
        self.assert_no_outputs()

    def test_taskfile_conflicts_are_rejected_before_publishing_report(self):
        before = self.source.read_bytes()
        alias = self.directory / "alias"
        alias.symlink_to(self.reports, target_is_directory=True)
        self.taskfile.write_text("preserve")
        for output in (self.taskfile, self.source, self.report, alias / self.report.name):
            with self.subTest(output=output):
                actual = self.cli("convert", self.source, "-o", output, "--report-output", self.report)
                self.assertEqual(actual.returncode, 3, actual.stderr)
                self.assertEqual(actual.stdout, "")
                self.assertEqual(list(self.reports.iterdir()), [])
                self.assertEqual(self.source.read_bytes(), before)
                self.assertEqual(self.taskfile.read_text(), "preserve")

    def test_unsupported_or_unreadable_source_creates_neither_artifact(self):
        self.source.write_text("a:\nimport 'unsupported.just'\n")
        for source, expected in ((self.source, 2), (self.directory / "missing", 3)):
            actual = self.cli("convert", source, "-o", self.taskfile, "--report-output", self.report)
            self.assertEqual(actual.returncode, expected, actual.stderr)
            self.assertEqual(actual.stdout, "")
            self.assert_no_outputs()

    def test_dash_report_path_is_usage_error_not_a_stdout_report(self):
        actual = self.cli("convert", self.source, "-o", self.taskfile, "--report-output", "-")
        self.assertEqual(actual.returncode, 2, actual.stderr)
        self.assertIn("--report-output requires a named path", actual.stderr)
        self.assertEqual(actual.stdout, "")
        self.assert_no_outputs()
