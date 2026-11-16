"""Opt-in byte budgets never expose a partial hash as a comparison."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge import report_check
from stackwrench.just_bridge.cli import main

CLI = [sys.executable, '-m', 'stackwrench', 'just']


class CheckReportLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-check-limit-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.taskfile = self.directory / 'Taskfile.yml'
        self.report = self.directory / 'analysis.json'
        self.output = self.directory / 'result.json'
        self.source.write_bytes('café\r\n'.encode())  # Seven bytes, six characters.
        self.taskfile.write_bytes(b'\xff\n')
        self.save_hashes()

    def save_hashes(self):
        self.saved = {'formatVersion': 1, 'supported': True,
                      'sourceSha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                      'renderedTaskfileSha256': hashlib.sha256(self.taskfile.read_bytes()).hexdigest()}
        self.report.write_text(json.dumps(self.saved), encoding='utf-8')

    def arguments(self, *extra):
        return ['check-report', str(self.source), '--taskfile', str(self.taskfile),
                '--report', str(self.report), *map(str, extra)]

    def cli(self, *extra):
        return subprocess.run([*CLI, *self.arguments(*extra)], cwd=self.directory,
                              capture_output=True, timeout=10)

    def invoke(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(self.arguments(*extra))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_inclusive_boundary_and_named_bytes_match_stdout(self):
        for raw, limit in [(b'', 1), (b'x', 1), ('café\r\n'.encode(), 7),
                           ('🚀\r\n'.encode(), 6)]:
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                self.taskfile.write_bytes(b'')
                self.save_hashes()
                for overflow in (False, True):
                    if overflow:
                        # Ensure even an expected hash of the entire oversized
                        # input cannot bypass the limit.
                        self.source.write_bytes(raw.ljust(limit, b'x') + b'!')
                        self.save_hashes()
                    before = {p: p.read_bytes() for p in (self.source, self.taskfile, self.report)}
                    result = self.cli('--max-file-bytes', f'00{limit}')
                    self.assertEqual(result.returncode, 2 if overflow else 0, result.stderr)
                    self.assertEqual(result.stderr, b'')
                    data = json.loads(result.stdout)
                    self.assertEqual(data['formatVersion'], 1)
                    self.assertEqual(data['maxFileBytes'], limit)
                    self.assertEqual(data['status'], 'unavailable' if overflow else 'matched')
                    check = data['checks']['source']
                    self.assertEqual(check['expectedSha256'], self.saved['sourceSha256'])
                    self.assertIs(check['matches'], None if overflow else True)
                    self.assertEqual(check['actualSha256'], None if overflow else
                                     hashlib.sha256(raw).hexdigest())
                    self.assertIs(data['checks']['taskfile']['matches'], True)
                    self.assertEqual([(d['subject'], d['code']) for d in data['diagnostics']],
                                     [('source', 'file-limit')] if overflow else [])
                    named = self.cli('--max-file-bytes', limit, '-o', self.output)
                    self.assertEqual(named.returncode, result.returncode, named.stderr)
                    self.assertEqual((named.stdout, named.stderr), (b'', b''))
                    self.assertEqual(self.output.read_bytes(), result.stdout)
                    self.output.unlink()
                    self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_limit_is_per_file_and_default_is_unchanged(self):
        self.taskfile.write_bytes(b'12345678')
        self.save_hashes()
        data = report_check.check_report(self.source, self.taskfile, self.report,
                                         max_file_bytes=7)
        self.assertEqual(data['status'], 'unavailable')
        self.assertIs(data['checks']['source']['matches'], True)
        self.assertIsNone(data['checks']['taskfile']['actualSha256'])
        self.assertIsNone(data['checks']['taskfile']['matches'])
        self.assertEqual(data['diagnostics'][0]['subject'], 'taskfile')
        self.source.write_bytes(b'12345678')
        data = report_check.check_report(self.source, self.taskfile, self.report,
                                         max_file_bytes=7)
        self.assertEqual([d['subject'] for d in data['diagnostics']], ['source', 'taskfile'])
        self.assertTrue(all(c['actualSha256'] is None and c['matches'] is None
                            for c in data['checks'].values()))
        self.save_hashes()
        default = self.cli()
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertNotIn('maxFileBytes', json.loads(default.stdout))
        unlimited = self.cli('--max-file-bytes', '9223372036854775807')
        self.assertEqual(unlimited.returncode, 0, unlimited.stderr)
        leading_zeros = self.cli('--max-file-bytes', '0' * 5000 + '8')
        self.assertEqual(leading_zeros.returncode, 0, leading_zeros.stderr)

    def test_limit_precedence_keeps_independent_negative_comparisons(self):
        for kind, code, status in [('mismatch', 2, 'unavailable'), ('null', 2, 'unavailable'),
                                   ('invalid', 2, 'unavailable'), ('missing', 3, 'error')]:
            with self.subTest(kind=kind):
                self.taskfile.write_bytes(b'\xff\n')
                self.save_hashes()
                if kind == 'mismatch':
                    self.taskfile.write_bytes(b'x')
                elif kind == 'null':
                    self.report.write_text(json.dumps(dict(self.saved, sourceSha256=None)))
                elif kind == 'invalid':
                    self.report.write_bytes(b'{')
                else:
                    self.taskfile.unlink()
                result = self.cli('--max-file-bytes', 6)
                self.assertEqual(result.returncode, code, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['status'], status)
                self.assertIsNone(data['checks']['source']['actualSha256'])
                self.assertIsNone(data['checks']['source']['matches'])
                self.assertIn({'subject': 'source', 'code': 'file-limit'},
                              [{k: d[k] for k in ('subject', 'code')} for d in data['diagnostics']])
                self.assertIs(data['checks']['taskfile']['matches'],
                              False if kind == 'mismatch' else True if kind == 'null' else None)
                if kind == 'null':
                    self.assertIn('fingerprint-unavailable', [d['code'] for d in data['diagnostics']])
                if kind == 'invalid':
                    self.assertTrue(all(c['expectedSha256'] is None for c in data['checks'].values()))

    def test_reads_are_bounded_even_when_opened_content_exceeds_stat_size(self):
        # The real file is seven bytes. A growing/replaced input's opened stream
        # can contain more; enforce the budget while reading, not via stat size.
        real_open = report_check._open_regular
        limit = 1024 * 1024 + 2
        for length in (limit, limit + 1, limit + 100):
            with self.subTest(length=length):
                observed = []
                raw = b'x' * length
                content = io.BytesIO(raw)

                @contextlib.contextmanager
                def opening(path):
                    if path == self.source:
                        def read(size):
                            self.assertGreater(size, 0)
                            self.assertLessEqual(size, 1024 * 1024)
                            chunk = content.read(size)
                            observed.append((size, len(chunk)))
                            return chunk
                        yield mock.Mock(read=read)
                    else:
                        with real_open(path) as stream:
                            yield stream

                with mock.patch.object(report_check, '_open_regular', opening):
                    data = report_check.check_report(self.source, self.taskfile, self.report,
                                                     max_file_bytes=limit)
                self.assertEqual(data['status'], 'mismatch' if length == limit else 'unavailable')
                self.assertEqual(sum(actual for _, actual in observed), min(length, limit + 1))
                self.assertEqual(data['checks']['source']['actualSha256'],
                                 hashlib.sha256(raw).hexdigest() if length == limit else None)
                self.assertIs(data['checks']['taskfile']['matches'], True)

    def test_partial_read_error_within_budget_still_has_io_precedence(self):
        real_open = report_check._open_regular

        @contextlib.contextmanager
        def opening(path):
            if path == self.taskfile:
                yield mock.Mock(read=mock.Mock(side_effect=[b'x', OSError('read failed')]))
            else:
                with real_open(path) as stream:
                    yield stream

        with mock.patch.object(report_check, '_open_regular', opening):
            data = report_check.check_report(self.source, self.taskfile, self.report,
                                             max_file_bytes=6)
        self.assertEqual(data['status'], 'error')
        self.assertTrue(all(c['actualSha256'] is None for c in data['checks'].values()))
        self.assertEqual([d['code'] for d in data['diagnostics']], ['file-limit', 'input-error'])

    def test_invalid_cli_budgets_fail_before_reading_or_creating_output(self):
        for value in ('0', '-1', '+1', '1.0', ' 1', '1 ', '١', '1e3',
                      '9223372036854775808', '9' * 5000, ''):
            with self.subTest(value=value[:25]), \
                 mock.patch.object(report_check, '_open_regular', side_effect=AssertionError('no reads')), \
                 mock.patch('stackwrench.just_bridge.cli._write_new_file', side_effect=AssertionError('no writes')):
                code, stdout, stderr = self.invoke('--max-file-bytes', value, '-o', self.output)
            self.assertEqual(code, 2, stderr)
            self.assertEqual(stdout, '')
            self.assertIn('--max-file-bytes', stderr)
            self.assertFalse(self.output.exists())

    def test_invalid_library_budgets_fail_before_reading(self):
        for value in (0, -1, True, False, 1.5, '1', 2**63):
            with self.subTest(value=value), \
                 mock.patch.object(report_check, '_open_regular', side_effect=AssertionError('no reads')):
                with self.assertRaises(ValueError):
                    report_check.check_report(self.source, self.taskfile, self.report,
                                              max_file_bytes=value)

    def test_limited_named_output_is_inert_and_output_failure_overrides(self):
        before = {p: p.read_bytes() for p in (self.source, self.taskfile, self.report)}
        with mock.patch('stackwrench.just_bridge.cli._load', side_effect=AssertionError('no analysis')), \
             mock.patch('stackwrench.just_bridge.cli.render_taskfile', side_effect=AssertionError('no rendering')), \
             mock.patch('subprocess.run', side_effect=AssertionError('no execution')):
            code, stdout, stderr = self.invoke('--max-file-bytes', 6, '-o', self.output)
        self.assertEqual((code, stdout, stderr), (2, '', ''))
        result = self.output.read_bytes()
        code, stdout, stderr = self.invoke('--max-file-bytes', 6, '-o', self.output)
        self.assertEqual(code, 3)
        self.assertEqual(stdout, '')
        self.assertIn('output-error', stderr)
        self.assertEqual(self.output.read_bytes(), result)
        self.output.unlink()
        with mock.patch('os.fsync', side_effect=OSError('sync failed')):
            code, stdout, stderr = self.invoke('--max-file-bytes', 6, '-o', self.output)
        self.assertEqual(code, 3)
        self.assertEqual(stdout, '')
        self.assertIn('output-error', stderr)
        self.assertEqual(set(self.directory.iterdir()), set(before))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
