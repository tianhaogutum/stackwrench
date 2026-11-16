"""Exclusive delivery of comparison results, including negative comparisons."""

import contextlib
import hashlib
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

CLI = [sys.executable, '-m', 'stackwrench', 'just']


class CheckReportOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-check-output-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.taskfile = self.directory / 'Taskfile.yml'
        self.report = self.directory / 'analysis.json'
        self.output = self.directory / 'result.json'
        # Arbitrary bytes prove that comparisons need no parsing or rendering.
        self.source.write_bytes('default:\r\n  echo café🚀\r\n'.encode())
        self.taskfile.write_bytes(b'opaque\xff\r\n')
        self.saved = {'formatVersion': 1, 'supported': True,
                      'sourceSha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                      'renderedTaskfileSha256': hashlib.sha256(self.taskfile.read_bytes()).hexdigest()}
        self.report.write_text(json.dumps(self.saved), encoding='utf-8')

    def arguments(self, *extra):
        return ['check-report', str(self.source), '--taskfile', str(self.taskfile),
                '--report', str(self.report), *map(str, extra)]

    def cli(self, *extra, **kwargs):
        return subprocess.run([*CLI, *self.arguments(*extra)], cwd=self.directory,
                              capture_output=True, timeout=10, **kwargs)

    def invoke(self, destination=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(self.arguments('--output', destination or self.output))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def inputs(self):
        return {p: p.read_bytes() for p in (self.source, self.taskfile, self.report) if p.exists()}

    def test_named_bytes_and_exits_equal_stdout_for_every_comparison_status(self):
        initial = self.inputs()
        cases = [('matched', 0), ('mismatch', 1), ('unavailable', 2),
                 ('invalid', 2), ('error', 3)]
        for status, code in cases:
            with self.subTest(status=status):
                for path, raw in initial.items():
                    path.write_bytes(raw)
                if status == 'mismatch':
                    self.source.write_bytes(initial[self.source].replace(b'\r\n', b'\n'))
                elif status == 'unavailable':
                    self.report.write_text(json.dumps(dict(self.saved, sourceSha256=None)))
                elif status == 'invalid':
                    self.report.write_bytes(b'{')
                elif status == 'error':
                    self.taskfile.unlink()
                before = self.inputs()
                expected = self.cli()
                actual = self.cli('-o', self.output)
                self.assertEqual(actual.returncode, code, actual.stderr)
                self.assertEqual(actual.stdout, b'')
                self.assertEqual(actual.stderr, b'')
                self.assertEqual(self.output.read_bytes(), expected.stdout)
                data = json.loads(self.output.read_bytes())
                self.assertEqual(data['status'], 'unavailable' if status == 'invalid' else status)
                self.assertEqual(expected.returncode, code)
                self.assertEqual(self.inputs(), before)
                self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
                refused = self.cli('--output', self.output)
                self.assertEqual(refused.returncode, 3, refused.stderr)
                self.assertIn(b'output-error', refused.stderr)
                self.assertEqual(refused.stdout, b'')
                self.assertEqual(self.output.read_bytes(), expected.stdout)
                self.output.unlink()

    def test_stdout_dash_and_named_are_utf8_lf_even_under_other_python_io(self):
        expected = self.cli().stdout
        self.assertTrue(expected.endswith(b'\n'))
        self.assertNotIn(b'\r', expected)
        for encoding in ('ascii:backslashreplace', 'utf-16'):
            with self.subTest(encoding=encoding):
                env = dict(os.environ, PYTHONIOENCODING=encoding)
                for extra in ([], ['--output', '-']):
                    result = self.cli(*extra, env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.stderr, b'')
                result = self.cli('--output', self.output, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(self.output.read_bytes(), expected)
                self.output.unlink()
        self.assertFalse((self.directory / '-').exists())

    def test_existing_entries_and_input_aliases_are_refused_before_staging(self):
        self.output.write_bytes(b'preserve result')
        directory = self.directory / 'directory'
        directory.mkdir()
        symlink = self.directory / 'symlink'
        symlink.symlink_to(self.source)
        dangling = self.directory / 'dangling'
        dangling.symlink_to(self.directory / 'absent')
        hardlink = self.directory / 'hardlink'
        os.link(self.report, hardlink)
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        before = self.inputs()
        for destination in (self.output, self.source, self.taskfile, self.report,
                            directory, symlink, dangling, hardlink, fifo):
            with self.subTest(destination=destination.name), \
                 mock.patch('tempfile.NamedTemporaryFile', side_effect=AssertionError('must refuse before staging')):
                code, stdout, stderr = self.invoke(destination)
            self.assertEqual(code, 3, stderr)
            self.assertEqual(stdout, '')
            self.assertIn('output-error', stderr)
        self.assertEqual(self.inputs(), before)
        self.assertEqual(self.output.read_bytes(), b'preserve result')
        self.assertEqual(hardlink.read_bytes(), before[self.report])
        self.assertTrue(directory.is_dir())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(dangling.is_symlink())
        self.assertFalse((self.directory / 'absent').exists())

    def test_missing_input_destination_and_parent_alias_are_refused(self):
        alias = self.directory / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        for path in (self.source, self.taskfile, self.report):
            original = path.read_bytes()
            path.unlink()
            for destination in (path, alias / path.name):
                with self.subTest(input=path.name, destination=destination), \
                     mock.patch('tempfile.NamedTemporaryFile', side_effect=AssertionError('must refuse input alias')):
                    code, stdout, stderr = self.invoke(destination)
                self.assertEqual(code, 3, stderr)
                self.assertEqual(stdout, '')
                self.assertIn('output-error', stderr)
                self.assertFalse(path.exists())
            path.write_bytes(original)

    def test_real_partial_write_and_missing_parent_leave_no_result(self):
        def limit_file_size():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (64, 64))

        before = self.inputs()
        for extra, kwargs in [(['--output', self.output], {'preexec_fn': limit_file_size}),
                              (['--output', self.directory / 'absent' / 'result.json'], {})]:
            with self.subTest(extra=extra):
                result = self.cli(*extra, **kwargs)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, b'')
                self.assertIn(b'output-error', result.stderr)
                self.assertEqual(set(self.directory.iterdir()), set(before))
                self.assertEqual(self.inputs(), before)

    def test_write_flush_close_sync_and_publish_failures_do_not_publish(self):
        real_temporary, real_sync = tempfile.NamedTemporaryFile, os.fsync
        before = self.inputs()
        for operation in ('write', 'flush', 'close', 'sync', 'publish'):
            with self.subTest(operation=operation):
                error = OSError('synthetic result delivery failure')

                @contextlib.contextmanager
                def temporary(*args, **kwargs):
                    with real_temporary(*args, **kwargs) as stream:
                        if operation in ('write', 'flush'):
                            original = getattr(stream, operation)

                            def fail(*args, **kwargs):
                                if operation == 'write':
                                    original(args[0][:8])
                                raise error

                            setattr(stream, operation, fail)
                        yield stream
                    if operation == 'close':
                        raise error

                def sync(fd):
                    if operation == 'sync':
                        raise error
                    return real_sync(fd)

                with mock.patch('tempfile.NamedTemporaryFile', side_effect=temporary), \
                     mock.patch('os.fsync', side_effect=sync), \
                     mock.patch('os.link', side_effect=error) as publish:
                    code, stdout, stderr = self.invoke()
                self.assertEqual(code, 3, stderr)
                self.assertEqual(stdout, '')
                self.assertIn('output-error', stderr)
                self.assertIn(str(self.output), stderr)
                if operation != 'publish':
                    publish.assert_not_called()
                self.assertEqual(set(self.directory.iterdir()), set(before))
                self.assertEqual(self.inputs(), before)

    def test_racing_entries_are_never_overwritten(self):
        real_link = os.link
        before = self.inputs()
        for kind in ('file', 'directory', 'symlink', 'hardlink'):
            with self.subTest(kind=kind):
                def race(staged, destination):
                    self.assertEqual(destination, self.output)
                    if kind == 'file':
                        destination.write_bytes(b'competitor')
                    elif kind == 'directory':
                        destination.mkdir()
                    elif kind == 'symlink':
                        destination.symlink_to(self.directory / 'absent')
                    else:
                        real_link(self.source, destination)
                    return real_link(staged, destination)

                with mock.patch('os.link', side_effect=race) as publish:
                    code, stdout, stderr = self.invoke()
                self.assertEqual(code, 3, stderr)
                publish.assert_called_once()
                self.assertEqual(stdout, '')
                self.assertIn('output-error', stderr)
                self.assertEqual(self.inputs(), before)
                self.assertEqual(set(self.directory.iterdir()), {*before, self.output})
                if kind == 'directory':
                    self.assertTrue(self.output.is_dir())
                    self.output.rmdir()
                else:
                    if kind == 'symlink':
                        self.assertTrue(self.output.is_symlink())
                        self.assertFalse((self.directory / 'absent').exists())
                    else:
                        self.assertEqual(self.output.read_bytes(), b'competitor' if kind == 'file' else before[self.source])
                    self.output.unlink()

    def test_complete_synced_result_publishes_without_analysis_or_execution(self):
        real_sync, real_link = os.fsync, os.link
        expected = self.cli().stdout
        before = self.inputs()
        events = []

        def sync(fd):
            self.assertFalse(self.output.exists())
            self.assertEqual(os.fstat(fd).st_size, len(expected))
            events.append('synced')
            real_sync(fd)

        def publish(staged, destination):
            self.assertEqual(events, ['synced'])
            self.assertEqual(destination, self.output)
            self.assertEqual(staged.parent, destination.parent)
            self.assertEqual(staged.read_bytes(), expected)
            self.assertFalse(destination.exists())
            events.append('published')
            return real_link(staged, destination)

        with mock.patch('os.fsync', side_effect=sync), \
             mock.patch('os.link', side_effect=publish), \
             mock.patch('stackwrench.just_bridge.cli._load', side_effect=AssertionError('must not analyze')), \
             mock.patch('stackwrench.just_bridge.cli.render_taskfile', side_effect=AssertionError('must not render')), \
             mock.patch('subprocess.run', side_effect=AssertionError('must not execute')):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, '')
        self.assertEqual(stderr, '')
        self.assertEqual(events, ['synced', 'published'])
        self.assertEqual(self.inputs(), before)
        self.assertEqual(set(self.directory.iterdir()), {*before, self.output})
        self.assertEqual(self.output.read_bytes(), expected)
