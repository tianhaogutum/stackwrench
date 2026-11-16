"""A report byte budget cannot turn a truncated envelope into a match."""

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

from stackwrench.just_bridge import report_check
from stackwrench.just_bridge.cli import main

CLI = [sys.executable, '-m', 'stackwrench', 'just']


class CheckReportReportLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-report-limit-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.taskfile = self.directory / 'Taskfile.yml'
        self.report = self.directory / 'analysis.json'
        self.output = self.directory / 'result.json'
        self.source.write_bytes('café\r\n'.encode())
        self.taskfile.write_bytes(b'\xff\n')
        self.saved = {'formatVersion': 1, 'supported': True,
                      'source': '/never/read/embedded/path', 'note': '🚀 café',
                      'sourceSha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                      'renderedTaskfileSha256': hashlib.sha256(self.taskfile.read_bytes()).hexdigest()}
        self.raw = json.dumps(self.saved, ensure_ascii=False).encode() + b'\r\n'
        self.report.write_bytes(self.raw)

    def arguments(self, *extra):
        return ['check-report', str(self.source), '--taskfile', str(self.taskfile),
                '--report', str(self.report), *map(str, extra)]

    def cli(self, *extra):
        return subprocess.run([*CLI, *self.arguments(*extra)], cwd=self.directory,
                              env=dict(os.environ, PYTHONIOENCODING='utf-16'),
                              capture_output=True, timeout=10)

    def invoke(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(self.arguments(*extra))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_exact_report_bytes_and_one_byte_over_with_named_parity(self):
        limit = len(self.raw)
        self.assertGreater(limit, len(self.raw.decode()))
        for overflow in (False, True):
            with self.subTest(overflow=overflow):
                self.report.write_bytes(self.raw + (b' ' if overflow else b''))
                before = {p: p.read_bytes() for p in (self.source, self.taskfile, self.report)}
                result = self.cli('--max-report-bytes', f'00{limit}')
                self.assertEqual(result.returncode, 2 if overflow else 0, result.stderr)
                self.assertEqual(result.stderr, b'')
                data = json.loads(result.stdout)
                self.assertEqual(data['formatVersion'], 1)
                self.assertEqual(data['maxReportBytes'], limit)
                self.assertNotIn('maxFileBytes', data)
                self.assertEqual(data['status'], 'unavailable' if overflow else 'matched')
                for subject, path in [('source', self.source), ('taskfile', self.taskfile)]:
                    check = data['checks'][subject]
                    self.assertEqual(check['actualSha256'], hashlib.sha256(before[path]).hexdigest())
                    self.assertEqual(check['expectedSha256'], None if overflow else check['actualSha256'])
                    self.assertIs(check['matches'], None if overflow else True)
                self.assertEqual([(d['subject'], d['code']) for d in data['diagnostics']],
                                 [('report', 'invalid-report')] if overflow else [])
                if overflow:
                    self.assertIn(f'--max-report-bytes {limit}', data['diagnostics'][0]['message'])
                for output in ('-', self.output):
                    named = self.cli('--max-report-bytes', limit, '--output', output)
                    self.assertEqual(named.returncode, result.returncode, named.stderr)
                    self.assertEqual(named.stderr, b'')
                    if output == '-':
                        self.assertEqual(named.stdout, result.stdout)
                    else:
                        self.assertEqual(named.stdout, b'')
                        self.assertEqual(self.output.read_bytes(), result.stdout)
                        self.output.unlink()
                self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_default_ceiling_and_explicit_policy_are_independent(self):
        default = self.cli()
        self.assertEqual(default.returncode, 0, default.stderr)
        data = json.loads(default.stdout)
        self.assertNotIn('maxReportBytes', data)
        for value in ('67108864', '0' * 5000 + str(len(self.raw))):
            result = self.cli('--max-report-bytes', value)
            self.assertEqual(result.returncode, 0, result.stderr)
            explicit = json.loads(result.stdout)
            self.assertEqual(explicit.pop('maxReportBytes'), int(value.lstrip('0')))
            self.assertEqual(explicit, data)
        with mock.patch.object(report_check, 'MAX_REPORT_BYTES', len(self.raw)):
            self.report.write_bytes(self.raw + b' ')
            result = report_check.check_report(self.source, self.taskfile, self.report)
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('maxReportBytes', result)
        self.assertEqual(result['diagnostics'][0]['message'], 'report exceeds 64 MiB')

    def test_overflow_keeps_file_limits_and_input_error_precedence(self):
        for missing in (None, self.source, self.taskfile):
            with self.subTest(missing=missing):
                if missing is not None:
                    original = missing.read_bytes()
                    missing.unlink()
                result = self.cli('--max-report-bytes', len(self.raw) - 1, '--max-file-bytes', 2)
                self.assertEqual(result.returncode, 2 if missing is None else 3, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['status'], 'unavailable' if missing is None else 'error')
                self.assertEqual(data['maxFileBytes'], 2)
                self.assertEqual(data['maxReportBytes'], len(self.raw) - 1)
                self.assertEqual(data['diagnostics'][0]['code'], 'invalid-report')
                for subject, path in [('source', self.source), ('taskfile', self.taskfile)]:
                    check = data['checks'][subject]
                    self.assertIsNone(check['expectedSha256'])
                    self.assertIsNone(check['matches'])
                    self.assertEqual(check['actualSha256'],
                                     hashlib.sha256(b'\xff\n').hexdigest()
                                     if subject == 'taskfile' and path != missing else None)
                if missing is not None:
                    self.assertIn('input-error', [d['code'] for d in data['diagnostics']])
                    missing.write_bytes(original)
                if missing != self.source:
                    self.assertIn('file-limit', [d['code'] for d in data['diagnostics']])

    def test_small_budgets_do_not_bypass_strict_json_or_envelope_checks(self):
        invalid = [b'\xff', b'\xef\xbb\xbf' + self.raw, b'{', self.raw + b'x',
                   b'{"formatVersion":1,"formatVersion":1}',
                   self.raw.replace(b'"note": "', b'"note": NaN, "ignored": "'),
                   json.dumps(dict(self.saved, formatVersion=True)).encode(),
                   json.dumps(dict(self.saved, supported=1)).encode(),
                   json.dumps(dict(self.saved, sourceSha256='A' * 64)).encode(),
                   json.dumps(dict(self.saved, supported=False)).encode()]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.report.write_bytes(raw)
                result = report_check.check_report(self.source, self.taskfile, self.report,
                                                   max_report_bytes=len(raw))
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual([d['code'] for d in result['diagnostics']], ['invalid-report'])
                self.assertTrue(all(c['expectedSha256'] is None and c['matches'] is None
                                    for c in result['checks'].values()))
                self.assertTrue(all(c['actualSha256'] is not None for c in result['checks'].values()))

    def test_reads_are_bounded_without_trusting_initial_size_or_parsing_prefix(self):
        real_open = report_check._open_regular
        limit = len(self.raw)
        for raw in (self.raw, self.raw + b' ', self.raw + b'x' * 10000):
            with self.subTest(length=len(raw)):
                stream = io.BytesIO(raw)
                observed = []

                @contextlib.contextmanager
                def opening(path):
                    self.assertIn(path, (self.report, self.source, self.taskfile))
                    if path == self.report:
                        def read(size):
                            self.assertGreater(size, 0)
                            self.assertLessEqual(size, limit + 1 - stream.tell())
                            value = stream.read(size)
                            observed.append(len(value))
                            return value
                        yield mock.Mock(read=read)
                    else:
                        with real_open(path) as content:
                            yield content

                with mock.patch.object(report_check, '_open_regular', opening):
                    result = report_check.check_report(self.source, self.taskfile, self.report,
                                                       max_report_bytes=limit)
                self.assertEqual(sum(observed), min(len(raw), limit + 1))
                self.assertEqual(result['status'], 'matched' if len(raw) == limit else 'unavailable')
                if len(raw) > limit:
                    self.assertTrue(all(c['expectedSha256'] is None and c['matches'] is None
                                        for c in result['checks'].values()))

    def test_report_io_error_stays_error_with_independent_actual_hashes(self):
        self.report.unlink()
        code, stdout, stderr = self.invoke('--max-report-bytes', 1)
        self.assertEqual((code, stderr), (3, ''))
        data = json.loads(stdout)
        self.assertEqual(data['status'], 'error')
        self.assertEqual([d['code'] for d in data['diagnostics']], ['input-error'])
        self.assertTrue(all(c['expectedSha256'] is None and c['matches'] is None and
                            c['actualSha256'] is not None for c in data['checks'].values()))

    def test_short_reads_require_eof_or_overflow_and_preserve_io_errors(self):
        real_open = report_check._open_regular
        for kind, tail, extra_budget, chunk_size, status, diagnostic in (
                ('exact', b'', 0, 9, 'matched', None),
                ('overflow', b' ', 0, len(self.raw), 'unavailable', 'invalid-report'),
                ('trailing', b'x', 2, len(self.raw), 'unavailable', 'invalid-report'),
                ('read-error', b'', 0, len(self.raw), 'error', 'input-error')):
            with self.subTest(kind=kind):
                content = io.BytesIO(self.raw + tail)
                limit = len(self.raw) + extra_budget

                @contextlib.contextmanager
                def opening(path):
                    if path == self.report:
                        def read(size):
                            self.assertGreater(size, 0)
                            self.assertLessEqual(size, limit + 1 - content.tell())
                            if kind == 'read-error' and content.tell() == len(self.raw):
                                raise OSError('report read failed after a valid prefix')
                            return content.read(min(size, chunk_size))
                        yield mock.Mock(read=read)
                    else:
                        with real_open(path) as stream:
                            yield stream

                with mock.patch.object(report_check, '_open_regular', opening):
                    result = report_check.check_report(self.source, self.taskfile, self.report,
                                                       max_report_bytes=limit)
                self.assertEqual(result['status'], status)
                self.assertEqual([d['code'] for d in result['diagnostics']],
                                 [] if diagnostic is None else [diagnostic])
                self.assertTrue(all(c['actualSha256'] is not None for c in result['checks'].values()))
                if status != 'matched':
                    self.assertTrue(all(c['expectedSha256'] is None and c['matches'] is None
                                        for c in result['checks'].values()))

    def test_invalid_cli_budgets_are_usage_errors_before_reads_or_writes(self):
        for value in ('0', '-1', '+1', '1.0', ' 1', '1 ', '١', '1e3',
                      '67108865', '9' * 5000, ''):
            with self.subTest(value=value[:20]), \
                 mock.patch.object(report_check, '_open_regular', side_effect=AssertionError('no reads')), \
                 mock.patch('stackwrench.just_bridge.cli._write_new_file', side_effect=AssertionError('no writes')):
                code, stdout, stderr = self.invoke('--max-report-bytes', value, '-o', self.output)
                self.assertEqual((code, stdout), (2, ''))
                self.assertIn('ASCII decimal integer from 1 to 67108864', stderr)
                self.assertFalse(self.output.exists())

    def test_invalid_library_budgets_fail_before_reads(self):
        for value in (0, -1, True, False, 1.5, '1', 67108865):
            with self.subTest(value=value), \
                 mock.patch.object(report_check, '_open_regular', side_effect=AssertionError('no reads')):
                with self.assertRaises(ValueError):
                    report_check.check_report(self.source, self.taskfile, self.report,
                                              max_report_bytes=value)

    def test_output_error_overrides_unavailable_and_check_stays_inert(self):
        before = {p: p.read_bytes() for p in (self.source, self.taskfile, self.report)}
        with mock.patch('stackwrench.just_bridge.cli._load', side_effect=AssertionError('no analysis')), \
             mock.patch('stackwrench.just_bridge.cli.render_taskfile', side_effect=AssertionError('no rendering')), \
             mock.patch('subprocess.run', side_effect=AssertionError('no execution')):
            code, stdout, stderr = self.invoke('--max-report-bytes', 1, '-o', self.output)
        self.assertEqual((code, stdout, stderr), (2, '', ''))
        saved = self.output.read_bytes()
        code, stdout, stderr = self.invoke('--max-report-bytes', 1, '-o', self.output)
        self.assertEqual((code, stdout), (3, ''))
        self.assertIn('output-error', stderr)
        self.assertEqual(self.output.read_bytes(), saved)
        self.output.unlink()
        with mock.patch('os.fsync', side_effect=OSError('sync failed')):
            code, stdout, stderr = self.invoke('--max-report-bytes', 1, '-o', self.output)
        self.assertEqual((code, stdout), (3, ''))
        self.assertIn('output-error', stderr)
        self.assertFalse(self.output.exists())
        with contextlib.redirect_stderr(io.StringIO()), mock.patch('sys.stdout') as stdout:
            stdout.write.side_effect = OSError('stdout failed')
            self.assertEqual(main(self.arguments('--max-report-bytes', 1)), 3)
        self.assertEqual(set(self.directory.iterdir()), set(before))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
