"""Verify delivery tests mock the fixture engine; no runner equivalence claim."""

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge import cli


STATUSES = (('passed', 0), ('failed', 1), ('error', 3), ('skipped', 4))


class VerifyStdoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-verify-stdout-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        # This input must never be read or executed by delivery tests.
        self.source = self.directory / 'justfile'
        self.source.write_bytes(b'root:\n  touch USER_COMMAND_RAN\n')
        self.env = dict(os.environ, PYTHONPATH=str(Path(cli.__file__).resolve().parents[2]))

    def arguments(self, dash=False):
        return ['verify', *(['--output', '-'] if dash else [])]

    def report(self, status, large=False):
        return dict(status=status, reason='mock-only café🚀\r\n' + ('x' * 20000 if large else ''))

    def command(self, status, dash=False, unbuffered=False, large=False):
        script = (
            'import sys; from unittest.mock import patch; from stackwrench.just_bridge.cli import main; '
            f"engine = patch('stackwrench.just_bridge.acceptance.verify', return_value={self.report(status, large)!r}).start(); "
            "patch('subprocess.run', side_effect=AssertionError('no recipes or runner probes')).start(); "
            f'code = main({self.arguments(dash)!r}); engine.assert_called_once_with(); sys.exit(code)')
        return [sys.executable, *(['-u'] if unbuffered else []), '-c', script]

    def assert_untouched(self):
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'justfile'})
        self.assertEqual(self.source.read_bytes(), b'root:\n  touch USER_COMMAND_RAN\n')

    def assert_output_error(self, code, stderr):
        self.assertEqual(code, 3, stderr)
        self.assertIn('<stdout>: error [output-error]:', stderr)
        self.assertNotIn('Traceback', stderr)
        self.assertNotIn('Exception ignored', stderr)
        self.assert_untouched()

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full device')
    def test_full_device_default_and_dash_do_not_retry_at_shutdown(self):
        # Small buffered skipped reports reproduce round17 next_probe.py.
        # Also cover every status, -u and writes exceeding the stdout buffer.
        for status, _ in STATUSES:
            for large in (False, True):
                for unbuffered in (False, True):
                    for dash in (False, True):
                        with self.subTest(status=status, large=large, unbuffered=unbuffered, dash=dash):
                            with open('/dev/full', 'wb', buffering=0) as sink:
                                result = subprocess.run(self.command(status, dash, unbuffered, large),
                                                        cwd=self.directory, env=self.env, stdout=sink,
                                                        stderr=subprocess.PIPE, text=True, timeout=5)
                            self.assert_output_error(result.returncode, result.stderr)

    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe')
    def test_closed_pipe_all_statuses_do_not_retry_at_shutdown(self):
        for status, _ in STATUSES:
            for large in (False, True):
                for unbuffered in (False, True):
                    for dash in (False, True):
                        with self.subTest(status=status, large=large, unbuffered=unbuffered, dash=dash):
                            read_fd, write_fd = os.pipe()
                            os.close(read_fd)
                            try:
                                result = subprocess.run(self.command(status, dash, unbuffered, large),
                                                        cwd=self.directory, env=self.env, stdout=write_fd,
                                                        stderr=subprocess.PIPE, text=True, timeout=5)
                            finally:
                                os.close(write_fd)
                            self.assert_output_error(result.returncode, result.stderr)

    def test_injected_failures_disable_sink_and_override_every_status(self):
        for status, _ in STATUSES:
            for operation in ('reconfigure', 'write', 'flush'):
                for error in (OSError(errno.EIO, 'synthetic sink failure'), BrokenPipeError(),
                              UnicodeEncodeError('ascii', 'é', 0, 1, 'synthetic encoding failure'),
                              ValueError('closed sink'), RuntimeError('synthetic sink failure')):
                    for dash in (False, True):
                        with self.subTest(status=status, operation=operation, error=type(error), dash=dash):
                            sink = mock.Mock(spec=['reconfigure', 'write', 'flush'])
                            getattr(sink, operation).side_effect = error
                            stderr = io.StringIO()
                            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr), \
                                 mock.patch('stackwrench.just_bridge.acceptance.verify', return_value=self.report(status)) as engine, \
                                 mock.patch('subprocess.run', side_effect=AssertionError('no recipes')):
                                code = cli.main(self.arguments(dash))
                                self.assertIsNone(sys.stdout)
                            engine.assert_called_once_with()
                            self.assert_output_error(code, stderr.getvalue())
                            getattr(sink, operation).assert_called_once()
                            if operation != 'flush':
                                sink.flush.assert_not_called()
                            if operation == 'reconfigure':
                                sink.write.assert_not_called()

    def test_success_preserves_json_bytes_status_and_single_flush(self):
        for status, expected_code in STATUSES:
            report = self.report(status)
            expected = json.dumps(report, ensure_ascii=True, indent=2) + '\n'
            for dash in (False, True):
                with self.subTest(status=status, dash=dash):
                    capture, stderr = io.StringIO(), io.StringIO()
                    sink = mock.Mock(spec=['reconfigure', 'write', 'flush'], wraps=capture)
                    sink.reconfigure = mock.Mock()
                    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr), \
                         mock.patch('stackwrench.just_bridge.acceptance.verify', return_value=report) as engine, \
                         mock.patch('subprocess.run', side_effect=AssertionError('no recipes')):
                        code = cli.main(self.arguments(dash))
                        self.assertIs(sys.stdout, sink)
                    engine.assert_called_once_with()
                    self.assertEqual(code, expected_code)
                    self.assertEqual(capture.getvalue(), expected)
                    self.assertEqual(stderr.getvalue(), '')
                    sink.reconfigure.assert_called_once_with(encoding='utf-8', errors='strict', newline='\n')
                    sink.write.assert_called_once_with(expected)
                    sink.flush.assert_called_once_with()
                    self.assert_untouched()

    def test_named_publication_preserves_json_and_refuses_existing_entries(self):
        for status, expected_code in STATUSES:
            with self.subTest(status=status):
                destination = self.directory / 'report.json'
                report = self.report(status)
                expected = (json.dumps(report, ensure_ascii=True, indent=2) + '\n').encode()
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                     mock.patch('stackwrench.just_bridge.acceptance.verify', return_value=report), \
                     mock.patch('subprocess.run', side_effect=AssertionError('no recipes')):
                    self.assertEqual(cli.main(['verify', '--output', str(destination)]), expected_code)
                self.assertEqual(destination.read_bytes(), expected)
                self.assertEqual(stdout.getvalue(), '')
                self.assertEqual(stderr.getvalue(), '')
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                for entry in ('file', 'dangling-symlink', 'directory'):
                    if entry != 'file':
                        destination.unlink()
                        if entry == 'directory':
                            destination.mkdir()
                        else:
                            destination.symlink_to(self.directory / 'missing')
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                         mock.patch('stackwrench.just_bridge.acceptance.verify', return_value=report), \
                         mock.patch('subprocess.run', side_effect=AssertionError('no recipes')):
                        self.assertEqual(cli.main(['verify', '--output', str(destination)]), 3)
                    self.assertIn('error [output-error]', stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), '')
                    if entry == 'file':
                        self.assertEqual(destination.read_bytes(), expected)
                    elif entry == 'dangling-symlink':
                        self.assertTrue(destination.is_symlink())
                    else:
                        self.assertTrue(destination.is_dir())
                destination.rmdir()
                self.assert_untouched()

    def test_named_staging_failure_and_publication_race_remain_exclusive(self):
        for status, _ in STATUSES:
            for failure in ('fsync', 'race'):
                with self.subTest(status=status, failure=failure):
                    destination = self.directory / 'report.json'
                    stdout, stderr = io.StringIO(), io.StringIO()
                    original_link = os.link
                    def race(staged, target):
                        Path(target).write_bytes(b'concurrent writer')
                        original_link(staged, target)
                    operation = (mock.patch('os.fsync', side_effect=OSError('sync failure'))
                                 if failure == 'fsync' else mock.patch('os.link', side_effect=race))
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), operation, \
                         mock.patch('stackwrench.just_bridge.acceptance.verify', return_value=self.report(status)), \
                         mock.patch('subprocess.run', side_effect=AssertionError('no recipes')):
                        self.assertEqual(cli.main(['verify', '--output', str(destination)]), 3)
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertIn('error [output-error]', stderr.getvalue())
                    if failure == 'race':
                        self.assertEqual(destination.read_bytes(), b'concurrent writer')
                        destination.unlink()
                    else:
                        self.assertFalse(destination.exists())
                    self.assert_untouched()
