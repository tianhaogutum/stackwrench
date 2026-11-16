"""Comparison stdout failures remain handled through interpreter shutdown."""

import contextlib
import errno
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

from stackwrench.just_bridge import cli, report_check


class CheckReportStdoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-check-stdout-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.taskfile = self.directory / 'Taskfile.yml'
        self.report = self.directory / 'conversion.json'
        self.raw = 'build:\r\n  echo café🚀\r\n  touch forbidden\r\n'.encode()
        self.taskraw = b'opaque\xff\r\n'
        self.saved = dict(formatVersion=1, supported=True,
                          sourceSha256=hashlib.sha256(self.raw).hexdigest(),
                          renderedTaskfileSha256=hashlib.sha256(self.taskraw).hexdigest())
        self.env = dict(os.environ, PYTHONPATH=str(Path(cli.__file__).resolve().parents[2]))

    def prepare(self, case):
        self.source.write_bytes(self.raw)
        self.taskfile.write_bytes(self.taskraw)
        self.report.write_text(json.dumps(self.saved), encoding='utf-8')
        flags, status, code = [], 'matched', 0
        if case == 'mismatch':
            self.source.write_bytes(self.raw.replace(b'\r\n', b'\n'))
            status, code = 'mismatch', 1
        elif case == 'unavailable':
            self.report.write_text(json.dumps(dict(self.saved, sourceSha256=None)))
            status, code = 'unavailable', 2
        elif case == 'invalid-large':
            # A long duplicate-key diagnostic makes write exceed stdout's buffer.
            key = 'padding' * 2000
            self.report.write_text(json.dumps({key: 0})[:-1] + ', ' + json.dumps(key) + ': 1}')
            status, code = 'unavailable', 2
        elif case == 'file-limit':
            flags = ['--max-file-bytes', '1']
            status, code = 'unavailable', 2
        elif case == 'report-limit':
            flags = ['--max-report-bytes', '1']
            status, code = 'unavailable', 2
        elif case == 'error':
            self.taskfile.unlink()
            status, code = 'error', 3
        return flags, status, code

    def arguments(self, flags, dash=False):
        return ['check-report', str(self.source), '--taskfile', str(self.taskfile),
                '--report', str(self.report), *(['--output', '-'] if dash else []), *flags]

    def inputs(self):
        return {p: p.read_bytes() for p in self.directory.iterdir()}

    def assert_output_error(self, result, before):
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b'<stdout>: error [output-error]:', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertNotIn(b'Exception ignored', result.stderr)
        self.assertEqual(self.inputs(), before)

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full device')
    def test_full_device_default_and_dash_do_not_retry_at_shutdown(self):
        # Reproduces round16 next_probe.py default/dash buffered failure, plus
        # immediate writes (-u and a result larger than the stream buffer).
        for case in ('matched', 'mismatch', 'unavailable', 'invalid-large',
                     'file-limit', 'report-limit', 'error'):
            flags, _, _ = self.prepare(case)
            before = self.inputs()
            for unbuffered in (False, True):
                for dash in (False, True):
                    with self.subTest(case=case, unbuffered=unbuffered, dash=dash):
                        with open('/dev/full', 'wb', buffering=0) as sink:
                            result = subprocess.run([sys.executable, *(['-u'] if unbuffered else []),
                                                     '-m', 'stackwrench', 'just', *self.arguments(flags, dash)],
                                                    cwd=self.directory, env=self.env, stdout=sink,
                                                    stderr=subprocess.PIPE, timeout=5)
                        self.assert_output_error(result, before)

    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe')
    def test_closed_pipe_all_comparisons_do_not_retry_at_shutdown(self):
        for case in ('matched', 'mismatch', 'unavailable', 'invalid-large', 'error'):
            flags, _, _ = self.prepare(case)
            before = self.inputs()
            for dash in (False, True):
                with self.subTest(case=case, dash=dash):
                    read_fd, write_fd = os.pipe()
                    os.close(read_fd)
                    try:
                        result = subprocess.run([sys.executable, '-m', 'stackwrench', 'just',
                                                 *self.arguments(flags, dash)], cwd=self.directory,
                                                env=self.env, stdout=write_fd, stderr=subprocess.PIPE, timeout=5)
                    finally:
                        os.close(write_fd)
                    self.assert_output_error(result, before)

    def test_injected_delivery_failures_override_all_comparison_statuses(self):
        for case in ('matched', 'mismatch', 'unavailable', 'file-limit', 'report-limit', 'error'):
            flags, _, _ = self.prepare(case)
            before = self.inputs()
            for operation in ('reconfigure', 'write', 'flush'):
                for error in (OSError(errno.EIO, 'synthetic sink failure'), BrokenPipeError(),
                              UnicodeEncodeError('ascii', 'é', 0, 1, 'synthetic encoding failure'),
                              ValueError('closed sink'), RuntimeError('synthetic sink failure')):
                    for dash in (False, True):
                        with self.subTest(case=case, operation=operation, error=type(error), dash=dash):
                            sink = mock.Mock(spec=['reconfigure', 'write', 'flush'])
                            getattr(sink, operation).side_effect = error
                            stderr = io.StringIO()
                            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr), \
                                 mock.patch('stackwrench.just_bridge.cli._load', side_effect=AssertionError('no analysis')), \
                                 mock.patch('stackwrench.just_bridge.cli.render_taskfile', side_effect=AssertionError('no rendering')), \
                                 mock.patch('subprocess.run', side_effect=AssertionError('no recipes')):
                                code = cli.main(self.arguments(flags, dash))
                                self.assertIsNone(sys.stdout)
                            self.assert_output_error(subprocess.CompletedProcess(
                                [], code, stderr=stderr.getvalue().encode()), before)
                            getattr(sink, operation).assert_called_once()
                            if operation != 'flush':
                                sink.flush.assert_not_called()
                            if operation == 'reconfigure':
                                sink.write.assert_not_called()

    def test_success_preserves_json_bytes_limits_status_and_single_flush(self):
        for case in ('matched', 'mismatch', 'unavailable', 'invalid-large',
                     'file-limit', 'report-limit', 'error'):
            flags, status, expected_code = self.prepare(case)
            before = self.inputs()
            kwargs = {flags[0][2:].replace('-', '_'): int(flags[1])} if flags else {}
            report = report_check.check_report(self.source, self.taskfile, self.report, **kwargs)
            self.assertEqual(report['status'], status)
            expected = json.dumps(report, ensure_ascii=True, indent=2) + '\n'
            for dash in (False, True):
                with self.subTest(case=case, dash=dash):
                    capture, stderr = io.StringIO(), io.StringIO()
                    sink = mock.Mock(spec=['reconfigure', 'write', 'flush'], wraps=capture)
                    # StringIO has no reconfigure, so provide its successful no-op.
                    sink.reconfigure = mock.Mock()
                    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
                        code = cli.main(self.arguments(flags, dash))
                        self.assertIs(sys.stdout, sink)
                    self.assertEqual(code, expected_code, stderr.getvalue())
                    self.assertEqual(capture.getvalue(), expected)
                    self.assertEqual(stderr.getvalue(), '')
                    sink.reconfigure.assert_called_once_with(encoding='utf-8', errors='strict', newline='\n')
                    sink.flush.assert_called_once_with()
                    self.assertEqual(self.inputs(), before)
