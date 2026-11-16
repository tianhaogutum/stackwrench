"""Plan delivery failures must be handled before interpreter shutdown."""

import contextlib
import errno
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge import cli

CLI = [sys.executable, '-m', 'stackwrench', 'just']


class PlanOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-plan-output-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        # Execution would leave a marker; plan must remain inert even on failure.
        self.raw = b'build: clean\n  touch forbidden\nclean:\nunused:\n'
        self.source.write_bytes(self.raw)

    def arguments(self, json=False):
        return ['plan', str(self.source), '--recipe', 'build',
                '--max-source-bytes', str(len(self.raw)), *(['--json'] if json else [])]

    def assert_output_error(self, result):
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b'<stdout>: error [output-error]:', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertNotIn(b'Exception ignored', result.stderr)

    def assert_inputs_only(self):
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(set(self.directory.iterdir()), {self.source})

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full device')
    def test_full_device_handles_buffered_and_immediate_errors(self):
        for large in (False, True):
            if large:
                self.raw = b'build:\n' + b'  echo padding-padding-padding\n' * 300
                self.source.write_bytes(self.raw)
            for json in (False, True):
                with self.subTest(large=large, json=json):
                    with open('/dev/full', 'wb', buffering=0) as sink:
                        result = subprocess.run([*CLI, *self.arguments(json)], cwd=self.directory,
                                                stdout=sink, stderr=subprocess.PIPE, timeout=5)
                    self.assert_output_error(result)
                    self.assert_inputs_only()

    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe')
    def test_closed_pipe_returns_output_error_without_shutdown_retry(self):
        for json in (False, True):
            with self.subTest(json=json):
                read_fd, write_fd = os.pipe()
                os.close(read_fd)  # No reader exists before the child can write.
                try:
                    result = subprocess.run([*CLI, *self.arguments(json)], cwd=self.directory,
                                            stdout=write_fd, stderr=subprocess.PIPE, timeout=5)
                finally:
                    os.close(write_fd)
                self.assert_output_error(result)
                self.assert_inputs_only()

    def test_injected_write_and_flush_failures_for_all_report_paths(self):
        for status in ('supported', 'unsupported', 'input-error'):
            self.source.write_bytes(self.raw if status == 'supported' else
                                    b"import 'never-opened.just'\n" if status == 'unsupported' else b'\xff')
            for json in ((False, True) if status != 'input-error' else (True,)):
                for operation in ('write', 'flush'):
                    for error in (OSError(errno.EIO, 'synthetic sink failure'), BrokenPipeError(),
                                  UnicodeEncodeError('ascii', 'é', 0, 1, 'synthetic encoding failure'),
                                  ValueError('closed sink'), RuntimeError('synthetic sink failure')):
                        with self.subTest(status=status, json=json, operation=operation, error=type(error)):
                            sink = mock.Mock(spec=['write', 'flush'])
                            getattr(sink, operation).side_effect = error
                            stderr = io.StringIO()
                            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
                                code = cli.main(self.arguments(json))
                                # A stream without a descriptor must not be retried at shutdown.
                                self.assertIsNone(sys.stdout)
                            self.assertEqual(code, 3, stderr.getvalue())
                            self.assertIn('<stdout>: error [output-error]:', stderr.getvalue())
                            self.assertNotIn('Traceback', stderr.getvalue())
                            getattr(sink, operation).assert_called_once()
                            if operation == 'write':
                                sink.flush.assert_not_called()
        self.assertEqual(set(self.directory.iterdir()), {self.source})

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full device')
    def test_input_error_json_and_unsupported_plans_handle_failed_delivery(self):
        for status in ('unsupported', 'input-error'):
            self.source.write_bytes(b"import 'never-opened.just'\n" if status == 'unsupported' else b'\xff')
            for json in ((False, True) if status == 'unsupported' else (True,)):
                with self.subTest(status=status, json=json):
                    with open('/dev/full', 'wb', buffering=0) as sink:
                        result = subprocess.run([*CLI, *self.arguments(json)], cwd=self.directory,
                                                stdout=sink, stderr=subprocess.PIPE, timeout=5)
                    self.assert_output_error(result)
        self.assertEqual(set(self.directory.iterdir()), {self.source})

    def test_successful_report_delivery_flushes_once_and_preserves_status(self):
        for raw, expected in ((self.raw, 0), (b"import 'never-opened.just'\n", 2), (b'\xff', 3)):
            self.source.write_bytes(raw)
            for json in ((False, True) if expected != 3 else (True,)):
                with self.subTest(raw=raw, json=json):
                    capture = io.StringIO()
                    sink = mock.Mock(spec=['write', 'flush'], wraps=capture)
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
                        code = cli.main(self.arguments(json))
                        self.assertIs(sys.stdout, sink)
                    self.assertEqual(code, expected, stderr.getvalue())
                    self.assertEqual(stderr.getvalue(), '')
                    self.assertTrue(capture.getvalue().endswith('\n'))
                    sink.flush.assert_called_once_with()

    def test_text_input_errors_do_not_write_or_flush_stdout(self):
        self.source.write_bytes(b'\xff')
        sink = mock.Mock(spec=['write', 'flush'])
        sink.write.side_effect = sink.flush.side_effect = AssertionError('no stdout delivery')
        stderr = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
            code = cli.main(self.arguments())
        self.assertEqual(code, 3)
        self.assertIn('input-error', stderr.getvalue())
        sink.write.assert_not_called()
        sink.flush.assert_not_called()
