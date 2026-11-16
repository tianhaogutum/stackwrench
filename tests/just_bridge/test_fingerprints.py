"""Exact-byte analysis fingerprints, using independent hashlib oracles."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge import analyze, render_taskfile
from stackwrench.just_bridge.cli import main

CLI = [sys.executable, "-m", "stackwrench", "just"]
SOURCE = "default:\n  echo café🚀\u2028fin\n"
# Independent expected rendering: includes the YAML-safe U+2028 escape and LF.
RENDERED = (
    '{\n  "version": "3",\n  "run": "once",\n  "tasks": {\n'
    '    "default": {\n      "cmds": [\n'
    '        "sh -cu \'echo café🚀\\u2028fin\'"\n'
    '      ]\n    }\n  }\n}\n'
).encode("utf-8")


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jtb-fingerprints-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "justfile"
        self.taskfile = self.directory / "Taskfile.yml"
        self.sidecar = self.directory / "conversion.json"

    def cli(self, *arguments, env=None):
        return subprocess.run([*CLI, *map(str, arguments)], cwd=self.directory,
                              capture_output=True, timeout=10, env=env)

    def assert_hashes(self, report, source, rendered):
        for field, content in (("sourceSha256", source), ("renderedTaskfileSha256", rendered)):
            self.assertIn(field, report)
            expected = None if content is None else hashlib.sha256(content).hexdigest()
            self.assertEqual(report[field], expected, field)

    def test_core_fingerprints_preserve_unicode_and_original_newlines(self):
        variants = [SOURCE, SOURCE.replace("\n", "\r\n"),
                    SOURCE.replace("\n", "\r\n", 1), SOURCE.rstrip("\n")]
        hashes = set()
        for text in variants:
            with self.subTest(text=text):
                analysis = analyze(text)
                self.assertTrue(analysis.supported)
                self.assertEqual(render_taskfile(analysis).encode("utf-8"), RENDERED)
                report = analysis.report()
                self.assert_hashes(report, text.encode("utf-8"), RENDERED)
                hashes.add(report["sourceSha256"])
        self.assertEqual(len(hashes), len(variants))

    def test_source_only_and_generated_byte_changes(self):
        texts = [SOURCE, "# discarded comment\n\n" + SOURCE,
                 SOURCE.replace("café", "thé")]
        reports = [analyze(text).report() for text in texts]
        for text, report in zip(texts, reports):
            self.assert_hashes(report, text.encode("utf-8"),
                               render_taskfile(analyze(text)).encode("utf-8"))
        self.assertEqual(len({r["sourceSha256"] for r in reports}), 3)
        self.assertEqual(reports[0]["renderedTaskfileSha256"], reports[1]["renderedTaskfileSha256"])
        self.assertNotEqual(reports[0]["renderedTaskfileSha256"], reports[2]["renderedTaskfileSha256"])

    def test_installed_plan_named_stdout_sidecar_and_attached_json_agree(self):
        for raw in (SOURCE.encode("utf-8"), SOURCE.replace("\n", "\r\n").encode("utf-8")):
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                plan = self.cli("plan", self.source, "--json")
                self.assertEqual(plan.returncode, 0, plan.stderr)
                self.assert_hashes(json.loads(plan.stdout), raw, RENDERED)
                named = self.cli("convert", self.source, "-o", self.taskfile,
                                 "--report-output", self.sidecar, "--report", "json")
                self.assertEqual(named.returncode, 0, named.stderr)
                self.assertEqual(named.stdout, b"")
                self.assertEqual(self.taskfile.read_bytes(), RENDERED)
                self.assertEqual(self.sidecar.read_bytes(), plan.stdout)
                self.assertEqual(named.stderr, plan.stdout)
                self.sidecar.unlink()
                for output in ([], ["--output", "-"]):
                    streamed = self.cli("convert", self.source, *output,
                                        "--report-output", self.sidecar, "--report", "json")
                    self.assertEqual(streamed.returncode, 0, streamed.stderr)
                    self.assertEqual(streamed.stdout, RENDERED)
                    self.assertEqual(self.sidecar.read_bytes(), plan.stdout)
                    self.assertEqual(streamed.stderr, plan.stdout)
                    self.sidecar.unlink()
                self.assertEqual(self.source.read_bytes(), raw)
                self.taskfile.unlink()

    def test_stdout_uses_hashed_utf8_bytes_even_with_non_utf8_python_io(self):
        self.source.write_bytes(SOURCE.encode("utf-8"))
        for encoding in ("ascii:backslashreplace", "utf-16"):
            with self.subTest(encoding=encoding):
                env = dict(os.environ, PYTHONIOENCODING=encoding)
                result = self.cli("convert", self.source, "--report-output", self.sidecar, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, RENDERED)
                self.assert_hashes(json.loads(self.sidecar.read_bytes()),
                                   self.source.read_bytes(), result.stdout)
                self.sidecar.unlink()

    def test_unsupported_sources_have_source_hash_but_no_rendered_hash(self):
        for text in ("", "default: missing\n", "a: b\nb: a\n",
                     "import 'other.just'\n", "\ufeffdefault:\n", "default:\r"):
            with self.subTest(text=text):
                raw = text.encode("utf-8")
                self.source.write_bytes(raw)
                result = self.cli("plan", self.source, "--json")
                self.assertEqual(result.returncode, 2, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["supported"])
                self.assert_hashes(report, raw, None)
                converted = self.cli("convert", self.source, "-o", self.taskfile,
                                     "--report-output", self.sidecar, "--report", "json")
                self.assertEqual(converted.returncode, 2, converted.stderr)
                self.assertEqual(converted.stdout, b"")
                self.assertFalse(self.taskfile.exists())
                self.assertFalse(self.sidecar.exists())

    def test_input_errors_and_limits_have_no_fingerprints(self):
        # Exactly 1 MiB is accepted and hashed; never fingerprint a partial read.
        boundary = b"default:\n#" + b"x" * (1_048_576 - len(b"default:\n#"))
        self.source.write_bytes(boundary)
        result = self.cli("plan", self.source, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assert_hashes(report, boundary, render_taskfile(analyze(boundary.decode())).encode())
        for raw, expected_code in ((b"\xff", 3), (b"x" * 1_048_577, 2)):
            with self.subTest(raw_length=len(raw)):
                self.source.write_bytes(raw)
                result = self.cli("plan", self.source, "--json")
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assert_hashes(json.loads(result.stdout), None, None)
        for path in (self.directory / "missing", self.directory):
            with self.subTest(path=path):
                result = self.cli("plan", path, "--json")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assert_hashes(json.loads(result.stdout), None, None)
        self.assert_hashes(analyze("x" * 1_048_577).report(), None, None)

    def test_retained_report_fingerprints_expected_bytes_without_claiming_delivery(self):
        self.source.write_bytes(SOURCE.encode("utf-8"))
        real_link = os.link

        def publish(staged, destination):
            if destination == self.taskfile:
                raise OSError("synthetic Taskfile publication failure")
            return real_link(staged, destination)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("os.link", side_effect=publish), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["convert", str(self.source), "-o", str(self.taskfile),
                         "--report-output", str(self.sidecar), "--report", "json"])
        self.assertEqual(code, 3, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("report retained", stderr.getvalue())
        self.assertNotIn('"supported": true', stderr.getvalue())
        self.assertFalse(self.taskfile.exists())
        report = json.loads(self.sidecar.read_bytes())
        self.assertTrue(report["supported"])
        self.assert_hashes(report, self.source.read_bytes(), RENDERED)
        self.assertEqual(set(self.directory.iterdir()), {self.source, self.sidecar})
