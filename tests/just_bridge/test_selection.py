"""Explicit partial migration; commands remain inert throughout these tests."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = [sys.executable, '-m', 'stackwrench', 'just']
SOURCE = 'prepare:\nbuild: prepare\ntest: prepare\nrelease: build\n'


class SelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-selection-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.source.write_text(SOURCE)

    def cli(self, *args):
        return subprocess.run([*CLI, *map(str, args)], cwd=self.directory,
                              capture_output=True, timeout=10)

    def test_multiple_roots_plan_and_convert_closure_without_default(self):
        args = ['--recipe', 'build', '--recipe', 'test', '--recipe', 'build']
        plan = self.cli('plan', self.source, '--json', *args)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        report = json.loads(plan.stdout)
        self.assertEqual(report['selection'], {
            'requestedRoots': ['build', 'test'],
            'includedRecipes': ['prepare', 'build', 'test'],
            'omittedRecipes': ['release'],
            'sourceDefaultRecipe': 'prepare', 'validationScope': 'full-source'})
        self.assertEqual(report['graph'], {'prepare': [], 'build': ['prepare'], 'test': ['prepare']})
        self.assertEqual([r['task'] for r in report['mappings']['recipes']], ['prepare', 'build', 'test'])
        self.assertIsNone(report['mappings']['default'])
        self.assertEqual(report['traces'], {'prepare': ['prepare'], 'build': ['prepare', 'build'],
                                           'test': ['prepare', 'test']})
        converted = self.cli('convert', self.source, *args, '--report', 'json')
        self.assertEqual(converted.returncode, 0, converted.stderr)
        self.assertEqual(converted.stderr, plan.stdout)
        doc = json.loads(converted.stdout)
        self.assertEqual(list(doc['tasks']), ['prepare', 'build', 'test'])
        self.assertEqual(doc['run'], 'once')
        self.assertEqual(doc['tasks']['build']['cmds'], [{'task': 'prepare'}])

    def test_shared_repeated_dependencies_and_forward_literals(self):
        from stackwrench.just_bridge import analyze, render_taskfile
        text = ('release: build\n  echo not-rendered\nbuild: left right left\n'
                '  echo {{late}}\ntest: right left\nright: shared\n'
                'left: shared shared\nshared:\nlate := "café🚀"\nunused := "kept"\n')
        analysis = analyze(text, recipes=['test', 'build', 'test'])
        self.assertTrue(analysis.supported, analysis.report())
        report = analysis.report()
        self.assertEqual(report['selection']['requestedRoots'], ['test', 'build'])
        included = ['build', 'test', 'right', 'left', 'shared']
        self.assertEqual(report['selection']['includedRecipes'], included)
        self.assertEqual(list(report['graph']), included)
        self.assertEqual(list(report['traces']), included)
        self.assertEqual(report['traces']['build'], ['shared', 'left', 'right', 'build'])
        self.assertEqual(report['traces']['test'], ['shared', 'right', 'left', 'test'])
        self.assertEqual([v['name'] for v in report['mappings']['variables']], ['late', 'unused'])
        doc = json.loads(render_taskfile(analysis))
        self.assertEqual(list(doc['tasks']), included)
        self.assertEqual(doc['tasks']['build']['cmds'], [
            {'task': 'left'}, {'task': 'right'}, {'task': 'left'}, "sh -cu 'echo café🚀'"])
        self.assertEqual(doc['tasks']['left']['cmds'], [{'task': 'shared'}, {'task': 'shared'}])
        self.assertNotIn('not-rendered', render_taskfile(analysis))

    def test_default_selected_excluded_dependency_and_leaf(self):
        from stackwrench.just_bridge import analyze, render_taskfile
        cases = [
            ('default: leaf\nleaf:\ntail:\n', ['default'], ['default', 'leaf'], True),
            ('default: leaf\nleaf:\ntail:\n', ['leaf'], ['leaf'], False),
            ('default:\nroot: default\n', ['root'], ['default', 'root'], True),
            ('first:\nleaf:\n', ['first'], ['first'], False),
            ('first:\nleaf:\n', ['leaf'], ['leaf'], False),
        ]
        for text, roots, names, has_default in cases:
            with self.subTest(text=text, roots=roots):
                analysis = analyze(text, recipes=roots)
                self.assertTrue(analysis.supported)
                self.assertEqual(list(json.loads(render_taskfile(analysis))['tasks']), names)
                expected = {'sourceRecipe': 'default', 'task': 'default', 'synthetic': False} if has_default else None
                self.assertEqual(analysis.report()['mappings']['default'], expected)
        self.source.write_text('default: leaf\nleaf:\n')
        for name, expected in [('leaf', 'Default: none'), ('default', 'Default: default -> task default')]:
            plan = self.cli('plan', self.source, '--recipe', name)
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertIn(expected.encode(), plan.stdout)

    def test_exact_names_and_invalid_requests_create_no_outputs(self):
        from stackwrench.just_bridge import analyze
        for name in ['', 'Build', ' build', 'build ', 'build test', 'missing', 'default']:
            with self.subTest(name=name):
                result = self.cli('plan', self.source, '--json', '--recipe', name)
                self.assertEqual(result.returncode, 2, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['supported'])
                self.assertIsNone(report['selection']['includedRecipes'])
                self.assertIsNone(report['selection']['omittedRecipes'])
                self.assertIsNone(report['renderedTaskfileSha256'])
                self.assertEqual(report['mappings']['recipes'], [])
                self.assertIsNone(report['mappings']['default'])
                self.assertEqual(report['traces'], {})
                self.assertIn('recipe-selection', [d['code'] for d in report['diagnostics']])
                result = self.cli('convert', self.source, '--recipe', name,
                                  '-o', self.directory/'new.yml', '--report-output', self.directory/'new.json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b'')
                self.assertFalse((self.directory/'new.yml').exists())
                self.assertFalse((self.directory/'new.json').exists())
        result = self.cli('plan', self.source, '--recipe')
        self.assertEqual(result.returncode, 2)
        for roots in ([], (), 'build', {'build'}, [1], [True]):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                analyze(SOURCE, recipes=roots)
        self.source.write_text('Build-me:\nbuild-me:\n')
        result = self.cli('convert', self.source, '--recipe', 'build-me')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(json.loads(result.stdout)['tasks']), ['build-me'])

    def test_unselected_invalid_source_still_fails_before_output_conflicts(self):
        cases = [
            ('good:\nbad: missing\n', 'missing-dependency'),
            ('good:\na: b\nb: a\n', 'dependency-cycle'),
            ('good:\nbad x:\n  echo unsupported\n', 'unsupported-syntax'),
            ('good:\nbad:\n  echo {{undefined}}\n', 'unsupported-interpolation'),
            ('good:\nunused := env_var("SECRET")\n', 'unsupported-variable'),
            ('good:\ndefault:\n', 'default-conflict'),
            ('good:\nbad:\n  @echo quiet\n', 'command-prefix'),
        ]
        output = self.directory/'exists.yml'
        sidecar = self.directory/'exists.json'
        output.write_bytes(b'keep taskfile'); sidecar.write_bytes(b'keep report')
        for text, error in cases:
            with self.subTest(error=error):
                self.source.write_text(text)
                plan = self.cli('plan', self.source, '--json', '--recipe', 'good')
                self.assertEqual(plan.returncode, 2, plan.stderr)
                report = json.loads(plan.stdout)
                self.assertIn(error, [d['code'] for d in report['diagnostics']])
                self.assertIsNone(report['selection']['includedRecipes'])
                self.assertIn('good', report['graph'])
                self.assertEqual(report['traces'], {})
                converted = self.cli('convert', self.source, '--recipe', 'good',
                                     '-o', output, '--report-output', sidecar, '--report', 'json')
                self.assertEqual(converted.returncode, 2)
                self.assertIn(error.encode(), converted.stderr)
                self.assertNotIn(b'output-error', converted.stderr)
                self.assertEqual(converted.stdout, b'')
        self.assertEqual(output.read_bytes(), b'keep taskfile')
        self.assertEqual(sidecar.read_bytes(), b'keep report')

    def test_source_io_utf8_and_input_limit_keep_selection_metadata(self):
        from stackwrench.just_bridge.core import MAX_INPUT_BYTES
        for data, code, diagnostic in [(b'good:\n\xff', 3, 'input-error'),
                                       (b'#' * (MAX_INPUT_BYTES + 1), 2, 'input-limit')]:
            with self.subTest(code=code):
                self.source.write_bytes(data)
                result = self.cli('plan', self.source, '--json', '--recipe', 'good')
                self.assertEqual(result.returncode, code)
                report = json.loads(result.stdout)
                self.assertIsNone(report['sourceSha256'])
                self.assertIsNone(report['renderedTaskfileSha256'])
                self.assertEqual(report['selection']['requestedRoots'], ['good'])
                self.assertIsNone(report['selection']['includedRecipes'])
                self.assertIn(diagnostic, [d['code'] for d in report['diagnostics']])
        self.source.unlink()
        result = self.cli('plan', self.source, '--json', '--recipe', 'unknown')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)['selection']['requestedRoots'], ['unknown'])

    def test_unicode_crlf_comments_sidecar_fingerprints_and_check_budgets(self):
        import hashlib
        raw = ('# source comment\r\n\r\nprepare:\r\n  echo {{word}}\r\n'
               '# Build café🚀\r\nbuild: prepare prepare\r\n  echo café🚀\r\n'
               'test: prepare\r\nrelease: build\r\n  echo OMITTED\r\n'
               'word := "café🚀"\r\n').encode('utf-8')
        self.source.write_bytes(raw)
        args = ['--recipe', 'test', '--recipe', 'build']
        plan = self.cli('plan', self.source, '--json', *args)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        taskfile, sidecar = self.directory/'Taskfile.yml', self.directory/'sidecar.json'
        converted = self.cli('convert', self.source, *args, '-o', taskfile,
                             '--report-output', sidecar, '--report', 'json')
        self.assertEqual(converted.returncode, 0, converted.stderr)
        self.assertEqual(converted.stdout, b'')
        self.assertEqual(converted.stderr, plan.stdout)
        self.assertEqual(sidecar.read_bytes(), plan.stdout)
        rendered = taskfile.read_bytes()
        report = json.loads(plan.stdout)
        self.assertEqual(report['sourceSha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(report['renderedTaskfileSha256'], hashlib.sha256(rendered).hexdigest())
        self.assertNotIn(b'OMITTED', rendered)
        self.assertEqual(json.loads(rendered)['tasks']['build']['desc'], 'Build café🚀')
        self.assertEqual(json.loads(rendered)['tasks']['build']['cmds'][:2],
                         [{'task': 'prepare'}, {'task': 'prepare'}])
        streamed = self.cli('convert', self.source, *args, '--output', '-', '--report', 'text')
        self.assertEqual(streamed.returncode, 0, streamed.stderr)
        self.assertEqual(streamed.stdout, rendered)
        for phrase in (b'full source', b'Selected roots: test, build', b'Included recipes: prepare, build, test',
                       b'Omitted recipes: release', b'Default: none'):
            self.assertIn(phrase, streamed.stderr)
        base = ['check-report', self.source, '--taskfile', taskfile, '--report', sidecar]
        size = len(sidecar.read_bytes())
        match = self.cli(*base, '--max-report-bytes', size, '--max-file-bytes', max(len(raw), len(rendered)))
        self.assertEqual(match.returncode, 0, match.stderr)
        self.assertEqual(json.loads(match.stdout)['status'], 'matched')
        bounded = self.cli(*base, '--max-report-bytes', size - 1)
        self.assertEqual(bounded.returncode, 2, bounded.stderr)
        self.assertIsNone(json.loads(bounded.stdout)['checks']['source']['expectedSha256'])
        self.source.write_bytes(raw.replace(b'OMITTED', b'CHANGED'))
        mismatch = self.cli(*base)
        self.assertEqual(mismatch.returncode, 1, mismatch.stderr)
        checked = json.loads(mismatch.stdout)
        self.assertFalse(checked['checks']['source']['matches'])
        self.assertTrue(checked['checks']['taskfile']['matches'])
        new_plan = self.cli('plan', self.source, '--json', *args)
        self.assertEqual(json.loads(new_plan.stdout)['renderedTaskfileSha256'], report['renderedTaskfileSha256'])

    def test_selection_publication_failures_retain_sidecar_and_no_success_report(self):
        import contextlib
        import io
        import os
        from unittest import mock
        from stackwrench.just_bridge.cli import main
        taskfile, sidecar = self.directory/'Taskfile.yml', self.directory/'sidecar.json'
        argv = ['convert', str(self.source), '--recipe', 'build', '-o', str(taskfile),
                '--report-output', str(sidecar), '--report', 'json']
        link = os.link
        def invoke():
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(argv)
            return code, out.getvalue(), err.getvalue()
        def fail_taskfile(source, destination):
            if destination == taskfile:
                taskfile.write_bytes(b'competitor')
            return link(source, destination)
        with mock.patch('stackwrench.just_bridge.cli.os.link', side_effect=fail_taskfile):
            code, out, err = invoke()
        self.assertEqual(code, 3)
        self.assertEqual(out, '')
        self.assertIn('report retained at', err)
        self.assertNotIn('"formatVersion"', err)
        self.assertEqual(json.loads(sidecar.read_bytes())['selection']['includedRecipes'], ['prepare', 'build'])
        self.assertEqual(taskfile.read_bytes(), b'competitor')
        sidecar.unlink(); taskfile.unlink()
        with mock.patch('stackwrench.just_bridge.cli.os.link', side_effect=OSError('report publish failed')):
            code, out, err = invoke()
        self.assertEqual(code, 3)
        self.assertIn('output-error', err)
        self.assertEqual(out, '')
        self.assertFalse(sidecar.exists())
        self.assertFalse(taskfile.exists())
        self.assertEqual(list(self.directory.glob('.stackwrench-just-*.tmp')), [])

    def test_exclusive_existing_destinations_and_unknown_name_precedence(self):
        taskfile, sidecar = self.directory/'Taskfile.yml', self.directory/'sidecar.json'
        taskfile.write_bytes(b'existing')
        args = ['convert', self.source, '--recipe', 'build', '-o', taskfile, '--report-output', sidecar]
        result = self.cli(*args)
        self.assertEqual(result.returncode, 3)
        self.assertIn(b'output-error', result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertFalse(sidecar.exists())
        self.assertEqual(taskfile.read_bytes(), b'existing')
        result = self.cli(*args, '--recipe', 'unknown')
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'recipe-selection', result.stderr)
        self.assertNotIn(b'output-error', result.stderr)

    def test_library_omission_keeps_existing_full_default_and_report(self):
        from stackwrench.just_bridge import analyze, render_taskfile
        implicit = analyze(SOURCE)
        explicit_none = analyze(SOURCE, recipes=None)
        self.assertEqual(render_taskfile(implicit), render_taskfile(explicit_none))
        self.assertEqual(implicit.report(), explicit_none.report())
        self.assertNotIn('selection', implicit.report())
        self.assertEqual(list(json.loads(render_taskfile(implicit))['tasks']),
                         ['default', 'prepare', 'build', 'test', 'release'])
