"""Taskfile stdout failures remain handled through interpreter shutdown."""

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

from stackwrench.just_bridge import cli
from stackwrench.just_bridge.core import analyze, render_taskfile


class ConvertOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-convert-output-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.sidecar = self.directory / 'conversion.json'
        self.raw = 'build: clean\r\n  echo café🚀\r\n  touch forbidden\r\nclean:\r\nunused:\r\n'.encode()
        self.source.write_bytes(self.raw)
        self.env = dict(os.environ, PYTHONPATH=str(Path(cli.__file__).resolve().parents[2]))

    def arguments(self, dash=False, attached=None, sidecar=False):
        return ['convert', str(self.source), *(['--output', '-'] if dash else []),
                '--recipe', 'build', '--max-source-bytes', str(len(self.raw)),
                *(['--report', attached] if attached else []),
                *(['--report-output', str(self.sidecar)] if sidecar else [])]

    def expected_report(self):
        return analyze(self.raw.decode(), str(self.source), recipes=['build'],
                       max_source_bytes=len(self.raw)).report()

    def assert_retained_report(self):
        expected = json.dumps(self.expected_report(), ensure_ascii=True, indent=2) + '\n'
        self.assertEqual(self.sidecar.read_bytes(), expected.encode())
        self.assertEqual(self.sidecar.stat().st_mode & 0o777, 0o600)

    def assert_inputs_and_sidecar_only(self, sidecar):
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(set(self.directory.iterdir()),
                         {self.source, self.sidecar} if sidecar else {self.source})

    def assert_delivery_failure(self, result, sidecar):
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b'<stdout>: error [output-error]:', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertNotIn(b'Exception ignored', result.stderr)
        self.assertNotIn(b'"supported": true', result.stderr)
        self.assertNotIn(b'Source: ', result.stderr)
        if sidecar:
            self.assertIn(f'; report retained at {self.sidecar}'.encode(), result.stderr)
            self.assert_retained_report()
        self.assert_inputs_and_sidecar_only(sidecar)

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full device')
    def test_full_device_keeps_handled_exit_after_shutdown(self):
        # Small buffered output reproduces round15 next_probe.py; large output
        # also fails immediately during write instead of only at flush.
        for large in (False, True):
            if large:
                self.raw = b'build: clean\n' + b'  echo padding-padding-padding\n' * 300 + b'clean:\n'
                self.source.write_bytes(self.raw)
            for dash, attached, sidecar in ((False, None, False), (True, 'json', False),
                                             (False, 'text', True), (True, 'json', True)):
                with self.subTest(large=large, dash=dash, attached=attached, sidecar=sidecar):
                    self.sidecar.unlink(missing_ok=True)
                    with open('/dev/full', 'wb', buffering=0) as sink:
                        result = subprocess.run([sys.executable, '-m', 'stackwrench', 'just',
                                                 *self.arguments(dash, attached, sidecar)],
                                                cwd=self.directory, env=self.env, stdout=sink,
                                                stderr=subprocess.PIPE, timeout=5)
                    self.assert_delivery_failure(result, sidecar)
                    self.sidecar.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe')
    def test_closed_pipe_keeps_handled_exit_after_shutdown(self):
        for large in (False, True):
            if large:
                self.raw = b'build: clean\n' + b'  echo padding-padding-padding\n' * 300 + b'clean:\n'
                self.source.write_bytes(self.raw)
            for dash, attached, sidecar in ((False, 'json', False), (True, 'text', True)):
                with self.subTest(large=large, dash=dash, sidecar=sidecar):
                    read_fd, write_fd = os.pipe()
                    os.close(read_fd)
                    try:
                        result = subprocess.run([sys.executable, '-m', 'stackwrench', 'just',
                                                 *self.arguments(dash, attached, sidecar)],
                                                cwd=self.directory, env=self.env, stdout=write_fd,
                                                stderr=subprocess.PIPE, timeout=5)
                    finally:
                        os.close(write_fd)
                    self.assert_delivery_failure(result, sidecar)
                    self.sidecar.unlink(missing_ok=True)

    def test_injected_delivery_errors_retain_report_and_disable_sink(self):
        for operation in ('reconfigure', 'write', 'flush'):
            for error in (OSError(errno.EIO, 'synthetic sink failure'), BrokenPipeError(),
                          UnicodeEncodeError('ascii', 'é', 0, 1, 'synthetic encoding failure'),
                          ValueError('closed sink'), RuntimeError('synthetic sink failure')):
                for sidecar in (False, True):
                    with self.subTest(operation=operation, error=type(error), sidecar=sidecar):
                        sink = mock.Mock(spec=['reconfigure', 'write', 'flush'])

                        def fail(*args, **kwargs):
                            if sidecar:
                                self.assert_retained_report()  # Publication precedes even reconfigure.
                            raise error

                        getattr(sink, operation).side_effect = fail
                        stderr = io.StringIO()
                        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
                            code = cli.main(self.arguments(True, 'json', sidecar))
                            self.assertIsNone(sys.stdout)
                        result = subprocess.CompletedProcess([], code, stderr=stderr.getvalue().encode())
                        self.assert_delivery_failure(result, sidecar)
                        getattr(sink, operation).assert_called_once()
                        if operation != 'flush':
                            sink.flush.assert_not_called()
                        if operation == 'reconfigure':
                            sink.write.assert_not_called()
                        self.sidecar.unlink(missing_ok=True)

    def test_report_publication_failure_never_touches_stdout(self):
        sink = mock.Mock(spec=['reconfigure', 'write', 'flush'])
        stderr = io.StringIO()
        with mock.patch('os.link', side_effect=OSError('synthetic publication failure')), \
             contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
            code = cli.main(self.arguments(sidecar=True, attached='json'))
            self.assertIs(sys.stdout, sink)
        self.assertEqual(code, 3)
        self.assertIn(f'{self.sidecar}: error [output-error]:', stderr.getvalue())
        self.assertNotIn('report retained', stderr.getvalue())
        self.assertNotIn('"supported": true', stderr.getvalue())
        self.assertEqual(sink.mock_calls, [])
        self.assert_inputs_and_sidecar_only(False)

    def test_analysis_and_input_errors_precede_stdout_and_sidecar(self):
        cases = [(b"import 'never-read.just'\n", [], 2, 'unsupported'),
                 (self.raw, ['--recipe', 'missing'], 2, 'selection'),
                 (self.raw, ['--max-source-bytes', '1'], 2, 'input-limit'),
                 (b'\xff', [], 3, 'input-error'), (None, [], 3, 'input-error')]
        for raw, flags, expected, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic, raw=raw):
                if raw is None:
                    self.source.unlink()
                else:
                    self.source.write_bytes(raw)
                sink = mock.Mock(spec=['reconfigure', 'write', 'flush'])
                stderr = io.StringIO()
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
                    code = cli.main(self.arguments(sidecar=True, attached='json') + flags)
                    self.assertIs(sys.stdout, sink)
                self.assertEqual(code, expected, stderr.getvalue())
                self.assertIn(diagnostic, stderr.getvalue())
                self.assertNotIn('output-error', stderr.getvalue())
                self.assertEqual(sink.mock_calls, [])
                self.assertFalse(self.sidecar.exists())
                self.assertEqual(set(self.directory.iterdir()), {self.source} if raw is not None else set())

    def test_success_preserves_bytes_metadata_and_single_flush(self):
        expected = render_taskfile(analyze(self.raw.decode(), recipes=['build'])).encode()
        for attached in (None, 'text', 'json'):
            with self.subTest(attached=attached):
                capture, stderr = io.StringIO(), io.StringIO()
                sink = mock.Mock(spec=['write', 'flush'], wraps=capture)
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
                    code = cli.main(self.arguments(True, attached, True))
                    self.assertIs(sys.stdout, sink)
                self.assertEqual(code, 0, stderr.getvalue())
                self.assertEqual(capture.getvalue().encode(), expected)
                sink.flush.assert_called_once_with()
                self.assert_retained_report()
                report = json.loads(self.sidecar.read_bytes())
                self.assertEqual(report['sourceSha256'], hashlib.sha256(self.raw).hexdigest())
                self.assertEqual(report['renderedTaskfileSha256'], hashlib.sha256(expected).hexdigest())
                if attached == 'json':
                    self.assertEqual(stderr.getvalue().encode(), self.sidecar.read_bytes())
                elif attached == 'text':
                    self.assertIn('Source: ', stderr.getvalue())
                self.assert_inputs_and_sidecar_only(True)
                self.sidecar.unlink()
