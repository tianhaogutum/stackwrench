"""Saved analysis checks compare explicit files, without rerendering recipes."""
import contextlib
import copy
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

from stackwrench.just_bridge.cli import main

CLI = [sys.executable, '-m', 'stackwrench', 'just']
SOURCE = "default:\r\n  echo café🚀\r\n".encode('utf-8')
# Deliberately arbitrary output: checking hashes must not parse or regenerate it.
TASKFILE = b'opaque output\xff\r\n'


class CheckReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-check-report-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.taskfile = self.directory / 'Taskfile.yml'
        self.report = self.directory / 'conversion.json'
        self.source.write_bytes(SOURCE)
        self.taskfile.write_bytes(TASKFILE)
        self.saved = {'formatVersion': 1, 'supported': True,
                      'source': '/never/open/this/path',
                      'sourceSha256': hashlib.sha256(SOURCE).hexdigest(),
                      'renderedTaskfileSha256': hashlib.sha256(TASKFILE).hexdigest()}
        self.save(self.saved)

    def save(self, data):
        self.report.write_text(json.dumps(data), encoding='utf-8')

    def arguments(self):
        return ['check-report', str(self.source), '--taskfile', str(self.taskfile),
                '--report', str(self.report)]

    def check(self, code, status):
        result = subprocess.run([*CLI, *self.arguments()], cwd=self.directory,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stderr, b'')
        data = json.loads(result.stdout)
        self.assertEqual(data['formatVersion'], 1)
        self.assertEqual(data['status'], status)
        self.assertEqual(data['report'], str(self.report))
        return data

    def test_exact_match_and_independent_byte_changes(self):
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        data = self.check(0, 'matched')
        for subject, path in [('source', self.source), ('taskfile', self.taskfile)]:
            check = data['checks'][subject]
            self.assertTrue(check['matches'])
            self.assertEqual(check['path'], str(path))
            self.assertEqual(check['expectedSha256'], hashlib.sha256(before[path]).hexdigest())
            self.assertEqual(check['actualSha256'], check['expectedSha256'])
        self.assertEqual(data['diagnostics'], [])
        self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)
        for subject, path, changed in [('source', self.source, SOURCE.replace(b'\r\n', b'\n')),
                                       ('source', self.source, SOURCE.replace('café'.encode(), b'tea')),
                                       ('taskfile', self.taskfile, TASKFILE.rstrip(b'\r\n'))]:
            with self.subTest(subject=subject, changed=changed):
                path.write_bytes(changed)
                data = self.check(1, 'mismatch')
                self.assertIs(data['checks'][subject]['matches'], False)
                other = 'taskfile' if subject == 'source' else 'source'
                self.assertIs(data['checks'][other]['matches'], True)
                self.assertEqual(data['checks'][subject]['actualSha256'], hashlib.sha256(changed).hexdigest())
                path.write_bytes(before[path])

    def test_plan_sidecar_and_attached_reports_are_checkable(self):
        plan = subprocess.run([*CLI, 'plan', str(self.source), '--json'],
                              capture_output=True, timeout=10, check=True)
        self.taskfile.unlink()
        self.report.unlink()
        converted = subprocess.run([*CLI, 'convert', str(self.source), '-o', str(self.taskfile),
                                    '--report-output', str(self.report), '--report', 'json'],
                                   capture_output=True, timeout=10, check=True)
        self.assertEqual(plan.stdout, converted.stderr)
        self.assertEqual(plan.stdout, self.report.read_bytes())
        for saved in [plan.stdout, self.report.read_bytes(), converted.stderr]:
            self.report.write_bytes(saved)
            self.check(0, 'matched')

    def test_null_hashes_are_unavailable_and_other_check_is_independent(self):
        for field, supported in [('renderedTaskfileSha256', False), ('sourceSha256', True)]:
            with self.subTest(field=field):
                saved = dict(self.saved, supported=supported)
                saved[field] = None
                self.save(saved)
                data = self.check(2, 'unavailable')
                subject = 'source' if field == 'sourceSha256' else 'taskfile'
                self.assertIsNone(data['checks'][subject]['matches'])
                other = 'taskfile' if subject == 'source' else 'source'
                self.assertIs(data['checks'][other]['matches'], True)
                self.assertIn('fingerprint-unavailable', [d['code'] for d in data['diagnostics']])
        self.save(dict(self.saved, sourceSha256=None, renderedTaskfileSha256=None, supported=False))
        data = self.check(2, 'unavailable')
        self.assertTrue(all(c['matches'] is None for c in data['checks'].values()))

    def test_invalid_report_never_produces_matches(self):
        invalid = [[], None, {}, dict(self.saved, formatVersion=2),
                   dict(self.saved, formatVersion=True), dict(self.saved, supported=1),
                   dict(self.saved, supported=False)]
        for field in ['sourceSha256', 'renderedTaskfileSha256']:
            for value in ['a'*63, 'A'*64, 'g'*64, '', 123, [], {}, True]:
                invalid.append(dict(self.saved, **{field: value}))
            missing = copy.deepcopy(self.saved)
            del missing[field]
            invalid.append(missing)
        raw_invalid = [b'{', b'\xff', b'\xef\xbb\xbf{}', b'{} trailing',
                       b'{"formatVersion": 1, "formatVersion": 1}',
                       json.dumps(dict(self.saved, extra=float('nan'))).encode(),
                       b'[' * 2000 + b']' * 2000]
        for raw in [json.dumps(d).encode() for d in invalid] + raw_invalid:
            with self.subTest(raw=raw[:160]):
                self.report.write_bytes(raw)
                data = self.check(2, 'unavailable')
                self.assertTrue(all(c['matches'] is None and c['expectedSha256'] is None
                                    for c in data['checks'].values()))
                self.assertEqual([d['code'] for d in data['diagnostics']], ['invalid-report'])

    def test_missing_nonregular_and_retained_sidecar_without_output(self):
        for path in [self.source, self.taskfile, self.report]:
            original = path.read_bytes()
            for kind in ['missing', 'directory', 'fifo']:
                with self.subTest(path=path.name, kind=kind):
                    path.unlink()
                    if kind == 'directory':
                        path.mkdir()
                    elif kind == 'fifo':
                        os.mkfifo(path)
                    data = self.check(3, 'error')
                    if path != self.report:
                        subject = 'source' if path == self.source else 'taskfile'
                        self.assertIsNone(data['checks'][subject]['matches'])
                        self.assertIsNone(data['checks'][subject]['actualSha256'])
                    else:
                        self.assertTrue(all(c['matches'] is None for c in data['checks'].values()))
                    self.assertIn('input-error', [d['code'] for d in data['diagnostics']])
                    if kind == 'directory':
                        path.rmdir()
                    elif kind == 'fifo':
                        path.unlink()
                    path.write_bytes(original)

    def test_in_process_does_not_load_render_execute_or_write(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('stackwrench.just_bridge.cli._load', side_effect=AssertionError('must not analyze')), \
             mock.patch('stackwrench.just_bridge.cli.render_taskfile', side_effect=AssertionError('must not render')), \
             mock.patch('subprocess.run', side_effect=AssertionError('must not execute')), \
             mock.patch('os.link', side_effect=AssertionError('must not publish')), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(self.arguments())
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'matched')

    def test_io_failures_and_report_limit_never_claim_matches(self):
        # Inject read failure after bytes were supplied: a partial hash is unusable.
        from stackwrench.just_bridge import report_check
        real_open = report_check._open_regular
        for failure in [PermissionError('denied'), OSError('partial read failed')]:
            @contextlib.contextmanager
            def opening(path):
                if path == self.taskfile:
                    stream = mock.Mock()
                    stream.read.side_effect = [b'partial', failure]
                    yield stream
                else:
                    with real_open(path) as stream:
                        yield stream
            with self.subTest(failure=failure), mock.patch.object(report_check, '_open_regular', opening):
                data = report_check.check_report(self.source, self.taskfile, self.report)
            self.assertEqual(data['status'], 'error')
            self.assertIsNone(data['checks']['taskfile']['matches'])
            self.assertIsNone(data['checks']['taskfile']['actualSha256'])
            self.assertIs(data['checks']['source']['matches'], True)
        with mock.patch.object(report_check, 'MAX_REPORT_BYTES', 4):
            data = report_check.check_report(self.source, self.taskfile, self.report)
        self.assertEqual(data['status'], 'unavailable')
        self.assertTrue(all(c['matches'] is None for c in data['checks'].values()))

    def test_symlinks_to_regular_files_and_status_precedence(self):
        original = self.taskfile.read_bytes()
        target = self.directory / 'real-taskfile'
        self.taskfile.rename(target)
        self.taskfile.symlink_to(target)
        self.check(0, 'matched')
        self.save(dict(self.saved, renderedTaskfileSha256=None, supported=False))
        self.source.write_bytes(SOURCE + b'# changed')
        data = self.check(2, 'unavailable')
        self.assertIs(data['checks']['source']['matches'], False)
        self.taskfile.unlink()
        data = self.check(3, 'error')
        self.assertIsNone(data['checks']['taskfile']['actualSha256'])
        self.assertEqual(target.read_bytes(), original)

    def test_report_byte_limit_is_inclusive(self):
        from stackwrench.just_bridge import report_check
        raw = self.report.read_bytes()
        with mock.patch.object(report_check, 'MAX_REPORT_BYTES', len(raw)):
            data = report_check.check_report(self.source, self.taskfile, self.report)
            self.assertEqual(data['status'], 'matched')
            self.report.write_bytes(raw + b' ')
            data = report_check.check_report(self.source, self.taskfile, self.report)
        self.assertEqual(data['status'], 'unavailable')
        self.assertEqual(data['diagnostics'][0]['code'], 'invalid-report')

    def test_open_and_stdout_errors_and_required_arguments(self):
        from stackwrench.just_bridge import report_check
        real_open = report_check._open_regular

        def opening(path):
            if path == self.source:
                raise PermissionError('synthetic access denied')
            return real_open(path)

        with mock.patch.object(report_check, '_open_regular', opening):
            data = report_check.check_report(self.source, self.taskfile, self.report)
        self.assertEqual(data['status'], 'error')
        self.assertIsNone(data['checks']['source']['matches'])
        self.assertIs(data['checks']['taskfile']['matches'], True)
        stderr = io.StringIO()
        with mock.patch('sys.stdout', new=mock.Mock(spec=['write', 'flush'])) as stdout, \
             contextlib.redirect_stderr(stderr):
            stdout.write.side_effect = OSError('synthetic stdout error')
            code = main(self.arguments())
        self.assertEqual(code, 3)
        self.assertIn('output-error', stderr.getvalue())
        result = subprocess.run([*CLI, 'check-report', str(self.source)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'required', result.stderr)
