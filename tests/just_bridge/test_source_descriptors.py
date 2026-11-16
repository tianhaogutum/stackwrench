"""Only opened regular source descriptors may reach the bounded reader."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge import cli


def swapped_source(source, target, arguments):
    """Swap at the real open boundary (also works on the pre-fix loader)."""
    real_path_open, real_os_open = Path.open, os.open
    descriptors = []
    swapped = False

    def swap(path):
        nonlocal swapped
        if Path(path) == source and not swapped:
            source.unlink()
            source.symlink_to(target)
            swapped = True

    def path_open(path, *args, **kwargs):
        swap(path)
        return real_path_open(path, *args, **kwargs)

    def os_open(path, flags, *args, **kwargs):
        swap(path)
        fd = real_os_open(path, flags, *args, **kwargs)
        descriptors.append(fd)
        return fd

    with mock.patch.object(Path, 'open', path_open), \
         mock.patch.object(cli.os, 'open', os_open):
        code = cli.main(arguments)
    assert swapped, 'probe must swap the source before opening'
    for fd in descriptors:
        try:
            os.fstat(fd)
        except OSError:
            pass
        else:
            raise AssertionError('source descriptor leaked')
    return code


@unittest.skipUnless(os.name == 'posix', 'POSIX nonblocking descriptors')
class SourceDescriptorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-source-descriptor-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.raw = 'prepare:\r\nbuild: prepare\r\n  echo café🚀\r\nunused:\r\n'.encode()
        self.source.write_bytes(self.raw)
        self.output = self.directory / 'Taskfile.yml'
        self.sidecar = self.directory / 'report.json'

    def invoke(self, action='plan', *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([action, str(self.source), *map(str, arguments)])
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_input_error(self, report, explicit=True):
        self.assertFalse(report['supported'])
        self.assertEqual([d['code'] for d in report['diagnostics']], ['input-error'])
        self.assertIsNone(report['sourceSha256'])
        self.assertIsNone(report['renderedTaskfileSha256'])
        self.assertEqual(report['graph'], {})
        self.assertEqual(report['mappings']['recipes'], [])
        self.assertEqual(report['traces'], {})
        self.assertEqual(report['selection']['requestedRoots'], ['unknown'])
        for field in ('includedRecipes', 'omittedRecipes', 'sourceDefaultRecipe'):
            self.assertIsNone(report['selection'][field])
        if explicit:
            self.assertEqual(report['maxSourceBytes'], 1)
        else:
            self.assertNotIn('maxSourceBytes', report)

    def test_regular_to_device_swap_rejected_before_analysis(self):
        arguments = ['plan', str(self.source), '--json', '--recipe', 'unknown',
                     '--max-source-bytes', '1']
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
             mock.patch.object(cli, 'analyze', wraps=cli.analyze) as analysis, \
             mock.patch.object(cli.os, 'read', wraps=os.read) as reading:
            code = swapped_source(self.source, '/dev/null', arguments)
        self.assertEqual((code, stderr.getvalue()), (3, ''))
        self.assert_input_error(json.loads(stdout.getvalue()))
        analysis.assert_not_called()
        reading.assert_not_called()

    def test_swaps_to_fifo_and_device_fail_promptly_for_plan_and_convert(self):
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        for target in (fifo, Path('/dev/null')):
            for action in ('plan', 'convert'):
                with self.subTest(target=target, action=action):
                    self.source.unlink()
                    self.source.write_bytes(self.raw)
                    arguments = [action, str(self.source), '--recipe', 'unknown']
                    if action == 'plan':
                        arguments += ['--json']
                    else:
                        arguments += ['-o', str(self.output), '--report-output', str(self.sidecar)]
                    script = (
                        'import sys; from pathlib import Path; '
                        f'sys.path[:0] = {[str(Path(cli.__file__).parents[2]), str(Path(__file__).parent)]!r}; '
                        'from test_source_descriptors import swapped_source; '
                        f'sys.exit(swapped_source(Path({str(self.source)!r}), Path({str(target)!r}), {arguments!r}))')
                    result = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=3)
                    self.assertEqual(result.returncode, 3, result.stderr)
                    if action == 'plan':
                        self.assertEqual(result.stderr, b'')
                        self.assert_input_error(json.loads(result.stdout), explicit=False)
                    else:
                        self.assertEqual(result.stdout, b'')
                        self.assertIn(b'input-error', result.stderr)
                        self.assertNotIn(b'output-error', result.stderr)
                    self.assertFalse(self.output.exists())
                    self.assertFalse(self.sidecar.exists())

    def test_nonregular_missing_and_unreadable_precede_selection_and_output(self):
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        # AF_UNIX paths are limited to about 108 bytes; a deep TMPDIR would
        # overflow that, so bind a short relative name from the temporary directory.
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.directory)
        sock.bind('socket')
        for target in (self.directory, fifo, self.directory / 'socket',
                       Path('/dev/null'), self.directory / 'missing'):
            with self.subTest(target=target):
                self.source.unlink()
                self.source.symlink_to(target)
                with mock.patch.object(cli.os, 'read', side_effect=AssertionError('no reads')), \
                     mock.patch.object(cli, 'analyze', side_effect=AssertionError('no analysis')), \
                     mock.patch.object(cli, '_stage_new_file', side_effect=AssertionError('no writes')):
                    code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'unknown', '--max-source-bytes', 1)
                    self.assertEqual((code, stderr), (3, ''))
                    self.assert_input_error(json.loads(stdout))
                    code, stdout, stderr = self.invoke('convert', '--recipe', 'unknown', '--max-source-bytes', 1,
                                                       '-o', self.source, '--report-output', self.sidecar)
                    self.assertEqual((code, stdout), (3, ''))
                    self.assertIn('input-error', stderr)
                    self.assertNotIn('output-error', stderr)
                self.assertFalse(self.sidecar.exists())
        # Deterministic even when run with permission to bypass mode bits.
        with mock.patch.object(cli.os, 'open', side_effect=PermissionError('unreadable source')):
            code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'unknown', '--max-source-bytes', 1)
        self.assertEqual((code, stderr), (3, ''))
        self.assert_input_error(json.loads(stdout))

    def test_regular_symlink_retains_bytes_limits_and_selected_metadata(self):
        target = self.directory / 'target'
        self.source.rename(target)
        self.source.symlink_to(target)
        for selected in ([], ['--recipe', 'build']):
            for policy in ([], ['--max-source-bytes', len(self.raw)]):
                with self.subTest(selected=selected, policy=policy):
                    code, stdout, stderr = self.invoke('plan', '--json', *selected, *policy)
                    self.assertEqual((code, stderr), (0, ''))
                    report = json.loads(stdout)
                    self.assertEqual(report['sourceSha256'], hashlib.sha256(self.raw).hexdigest())
                    code, stdout, stderr = self.invoke('convert', *selected, *policy, '--report', 'json',
                                                       '-o', self.output, '--report-output', self.sidecar)
                    self.assertEqual((code, stdout), (0, ''))
                    self.assertEqual(json.loads(stderr), report)
                    self.assertEqual(json.loads(self.sidecar.read_bytes()), report)
                    self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), report['renderedTaskfileSha256'])
                    self.output.unlink()
                    self.sidecar.unlink()
        self.assertEqual(target.read_bytes(), self.raw)
        self.assertTrue(self.source.is_symlink())

    def test_descriptors_close_on_success_rejection_and_io_failures(self):
        real_open = os.open
        for failure in ('none', 'fstat', 'read', 'nonregular', 'utf8', 'overflow'):
            with self.subTest(failure=failure):
                self.source.unlink()
                self.source.write_bytes(b'build:\n\xff' if failure == 'utf8' else self.raw)
                if failure == 'nonregular':
                    self.source.unlink()
                    self.source.symlink_to('/dev/null')
                opened = []
                def opening(path, flags):
                    self.assertTrue(flags & os.O_NONBLOCK)
                    fd = real_open(path, flags)
                    opened.append(fd)
                    return fd
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(cli.os, 'open', side_effect=opening))
                    if failure in ('fstat', 'read'):
                        stack.enter_context(mock.patch.object(cli.os, failure, side_effect=OSError(failure + ' failed')))
                    args = ['--max-source-bytes', 1] if failure == 'overflow' else []
                    code, stdout, stderr = self.invoke('plan', '--json', *args)
                self.assertEqual(code, 0 if failure == 'none' else 2 if failure == 'overflow' else 3)
                self.assertEqual(len(opened), 1)
                for fd in opened:
                    with self.assertRaises(OSError):
                        os.fstat(fd)


if __name__ == '__main__':
    unittest.main()
