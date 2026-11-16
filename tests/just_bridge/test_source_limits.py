"""Source byte budgets are enforced before decoding, analysis or publication."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge import analyze, render_taskfile
from stackwrench.just_bridge import cli
from stackwrench.just_bridge.core import MAX_INPUT_BYTES


class SourceLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-source-limit-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.output = self.directory / 'Taskfile.yml'
        self.sidecar = self.directory / 'report.json'
        self.raw = 'prepare:\r\nbuild: prepare\r\n  echo café🚀\r\nunused:\r\n'.encode()
        self.source.write_bytes(self.raw)

    def invoke(self, action='plan', *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = cli.main([action, str(self.source), *map(str, arguments)])
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_unavailable(self, report):
        self.assertFalse(report['supported'])
        for field in ('sourceSha256', 'renderedTaskfileSha256'):
            self.assertIsNone(report[field])
        self.assertEqual(report['graph'], {})
        self.assertEqual(report['mappings']['recipes'], [])
        self.assertEqual(report['traces'], {})
        self.assertEqual(report['selection']['requestedRoots'], ['build'])
        for field in ('includedRecipes', 'omittedRecipes', 'sourceDefaultRecipe'):
            self.assertIsNone(report['selection'][field])

    def test_exact_original_bytes_accepted_and_one_extra_rejected(self):
        limit = len(self.raw)
        code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'build',
                                           '--max-source-bytes', f'00{limit}')
        self.assertEqual((code, stderr), (0, ''))
        report = json.loads(stdout)
        self.assertEqual(report['maxSourceBytes'], limit)
        self.assertEqual(report['sourceSha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(report['selection']['includedRecipes'], ['prepare', 'build'])
        self.assertEqual(report['selection']['omittedRecipes'], ['unused'])
        code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'build',
                                           '--max-source-bytes', limit - 1)
        self.assertEqual((code, stderr), (2, ''))
        report = json.loads(stdout)
        self.assert_unavailable(report)
        self.assertEqual(report['maxSourceBytes'], limit - 1)
        self.assertEqual([d['code'] for d in report['diagnostics']], ['input-limit'])
        self.assertIn(f'--max-source-bytes {limit - 1}', report['diagnostics'][0]['message'])

    def test_conversion_parity_sidecar_attached_and_text_policy(self):
        for selected in ([], ['--recipe', 'build']):
            with self.subTest(selected=selected):
                baseline = self.invoke('convert', *selected, '--report', 'json')
                code, stdout, stderr = self.invoke('convert', *selected, '--max-source-bytes', len(self.raw),
                                                   '--report', 'json', '--report-output', self.sidecar)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(stdout, baseline[1])
                report = json.loads(stderr)
                self.assertEqual(self.sidecar.read_text(), stderr)
                self.assertEqual(report.pop('maxSourceBytes'), len(self.raw))
                self.assertEqual(report, json.loads(baseline[2]))
                self.assertEqual(hashlib.sha256(stdout.encode()).hexdigest(), report['renderedTaskfileSha256'])
                self.sidecar.unlink()
        code, stdout, stderr = self.invoke('plan', '--max-source-bytes', len(self.raw))
        self.assertEqual(code, 0)
        self.assertIn(f'Max source bytes: {len(self.raw)}', stdout)
        code, stdout, stderr = self.invoke('convert', '--max-source-bytes', len(self.raw), '--report', 'text')
        self.assertEqual(code, 0)
        self.assertIn(f'Max source bytes: {len(self.raw)}', stderr)

    def test_overflow_wins_over_decode_selection_and_output_and_writes_nothing(self):
        self.source.write_bytes(b'build:\n\xff')
        with mock.patch.object(cli, 'analyze', side_effect=AssertionError('no partial analysis')), \
             mock.patch.object(cli, '_stage_new_file', side_effect=AssertionError('no writes')):
            code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'build', '--max-source-bytes', 7)
            self.assertEqual((code, stderr), (2, ''))
            self.assert_unavailable(json.loads(stdout))
            for root in ('build', 'unknown', ''):
                code, stdout, stderr = self.invoke('convert', '--recipe', root, '--max-source-bytes', 7,
                                                   '-o', self.source, '--report-output', self.sidecar)
                self.assertEqual((code, stdout), (2, ''))
                self.assertIn('input-limit', stderr)
                self.assertNotIn('output-error', stderr)
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(list(self.directory.iterdir()), [self.source])

    def test_input_errors_preserve_policy_and_precede_output(self):
        for raw in (b'build:\n\xff', None):
            with self.subTest(raw=raw):
                if raw is None:
                    self.source.unlink()
                else:
                    self.source.write_bytes(raw)
                code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'build', '--max-source-bytes', 100)
                self.assertEqual((code, stderr), (3, ''))
                report = json.loads(stdout)
                self.assert_unavailable(report)
                self.assertEqual(report['maxSourceBytes'], 100)
                self.assertEqual([d['code'] for d in report['diagnostics']], ['input-error'])
                code, stdout, stderr = self.invoke('convert', '-o', self.source,
                                                   '--report-output', self.sidecar, '--max-source-bytes', 100)
                self.assertEqual((code, stdout), (3, ''))
                self.assertIn('input-error', stderr)
                self.assertNotIn('output-error', stderr)
                self.assertFalse(self.sidecar.exists())

    def test_full_source_validation_before_selection_within_budget(self):
        for suffix, diagnostic in ((b'bad:\n  echo {{missing}}\n', 'unsupported-interpolation'),
                                   (b'bad: absent\n', 'missing-dependency'),
                                   (b'bad: bad\n', 'dependency-cycle')):
            with self.subTest(diagnostic=diagnostic):
                raw = self.raw + suffix
                self.source.write_bytes(raw)
                code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'build',
                                                   '--max-source-bytes', len(raw))
                self.assertEqual((code, stderr), (2, ''))
                report = json.loads(stdout)
                self.assertEqual(report['sourceSha256'], hashlib.sha256(raw).hexdigest())
                self.assertIsNone(report['renderedTaskfileSha256'])
                self.assertIsNone(report['selection']['includedRecipes'])
                self.assertIn(diagnostic, [d['code'] for d in report['diagnostics']])

    def test_short_reads_continue_until_eof_or_overflow_including_default(self):
        for explicit in (False, True):
            for kind in ('exact', 'overflow', 'read-error'):
                with self.subTest(explicit=explicit, kind=kind):
                    limit = len(self.raw) if explicit else MAX_INPUT_BYTES
                    raw = self.raw if explicit else self.raw + b'#' + b'x' * (limit - len(self.raw) - 1)
                    content = io.BytesIO(raw + (b'x' if kind == 'overflow' else b''))
                    reads = []
                    def read(fd, size):
                        self.assertGreater(size, 0)
                        self.assertLessEqual(size, min(65536, limit + 1 - content.tell()))
                        reads.append(size)
                        if kind == 'read-error' and content.tell() == len(raw):
                            raise OSError('source read failed after valid prefix')
                        return content.read(min(size, 7 if explicit else 997))
                    with mock.patch.object(cli.os, 'read', side_effect=read):
                        args = ['--max-source-bytes', limit] if explicit else []
                        code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'build', *args)
                    self.assertEqual(stderr, '')
                    report = json.loads(stdout)
                    self.assertEqual(code, {'exact': 0, 'overflow': 2, 'read-error': 3}[kind])
                    self.assertGreater(len(reads), 1)
                    self.assertLessEqual(content.tell(), limit + 1)
                    if kind == 'exact':
                        self.assertEqual(report['sourceSha256'], hashlib.sha256(raw).hexdigest())
                    else:
                        self.assert_unavailable(report)

    def test_invalid_cli_limits_are_usage_before_reads_or_writes(self):
        for action in ('plan', 'convert'):
            for value in ('0', '-1', '+1', '1.0', ' 1', '1 ', '١', '１', '1e3', '1048577', '9' * 5000, ''):
                with self.subTest(action=action, value=value[:20]), \
                     mock.patch.object(cli, '_load', side_effect=AssertionError('no reads')), \
                     mock.patch.object(cli, '_stage_new_file', side_effect=AssertionError('no writes')):
                    code, stdout, stderr = self.invoke(action, '--max-source-bytes', value)
                    self.assertEqual((code, stdout), (2, ''))
                    self.assertIn('ASCII decimal integer from 1 to 1048576', stderr)
        self.assertEqual(list(self.directory.iterdir()), [self.source])

    def test_bounds_leading_zeros_empty_input_and_default_policy(self):
        self.source.write_bytes(b'x:\n' + b'#' * (MAX_INPUT_BYTES - 3))
        for value in (str(MAX_INPUT_BYTES), '0' * 5000 + str(MAX_INPUT_BYTES)):
            code, stdout, stderr = self.invoke('plan', '--json', '--max-source-bytes', value)
            self.assertEqual((code, stderr), (0, ''))
            self.assertEqual(json.loads(stdout)['maxSourceBytes'], MAX_INPUT_BYTES)
        baseline = self.invoke('plan', '--json')
        report = json.loads(baseline[1])
        self.assertNotIn('maxSourceBytes', report)
        self.source.write_bytes(self.source.read_bytes() + b'x')
        code, stdout, stderr = self.invoke('plan', '--json')
        self.assertEqual((code, stderr), (2, ''))
        self.assertEqual(json.loads(stdout)['diagnostics'][0]['message'], 'input exceeds 1 MiB')
        self.source.write_bytes(b'')
        code, stdout, stderr = self.invoke('plan', '--json', '--max-source-bytes', 1)
        self.assertEqual((code, stderr), (2, ''))
        report = json.loads(stdout)
        self.assertEqual(report['sourceSha256'], hashlib.sha256(b'').hexdigest())
        self.assertNotIn('input-limit', [d['code'] for d in report['diagnostics']])

    def test_output_failure_precedence_after_supported_analysis_and_exclusive_publication(self):
        self.output.write_bytes(b'existing')
        code, stdout, stderr = self.invoke('convert', '--max-source-bytes', len(self.raw),
                                           '-o', self.output, '--report-output', self.sidecar)
        self.assertEqual((code, stdout), (3, ''))
        self.assertIn('output-error', stderr)
        self.assertEqual(self.output.read_bytes(), b'existing')
        self.assertFalse(self.sidecar.exists())
        self.output.unlink()
        with mock.patch.object(cli.os, 'link', side_effect=OSError('publication failed')):
            code, stdout, stderr = self.invoke('convert', '--max-source-bytes', len(self.raw),
                                               '-o', self.output, '--report-output', self.sidecar)
        self.assertEqual((code, stdout), (3, ''))
        self.assertIn('output-error', stderr)
        self.assertEqual(list(self.directory.iterdir()), [self.source])

    def test_library_budget_matches_cli_and_rejects_invalid_values(self):
        for value in (True, False, 0, -1, MAX_INPUT_BYTES + 1, 1.5, '20'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                analyze('x:\n', max_source_bytes=value)
        text = self.raw.decode()
        analysis = analyze(text, str(self.source), recipes=['build'], max_source_bytes=len(self.raw))
        code, stdout, stderr = self.invoke('plan', '--json', '--recipe', 'build', '--max-source-bytes', len(self.raw))
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(analysis.report(), json.loads(stdout))
        self.assertEqual(render_taskfile(analysis), self.invoke('convert', '--recipe', 'build', '--max-source-bytes', len(self.raw))[1])
        overflow = analyze(text, recipes=['build'], max_source_bytes=len(self.raw) - 1)
        self.assert_unavailable(overflow.report())
        self.assertNotIn('maxSourceBytes', analyze(text).report())


if __name__ == '__main__':
    unittest.main()
