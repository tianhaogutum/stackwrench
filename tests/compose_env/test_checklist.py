"""Structured syntax checklists: independent scopes, requirements and pure redaction."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from stackwrench.compose_env import TraceError, discover_scenarios, discover_template, env_template
from .test_trace import Workspace


class Checklists(Workspace):
    def fixture(self):
        compose = self.put('compose.yaml', json.dumps({
            'name': '${PROJECT?private-message}',
            'x-meta\n# NAME=quoted"': '$META',
            'services': {
                'z.api': {'env_file': ['local.env', '${FILE:-private-unopened.env}',
                                     {'path': 'absent.env', 'required': False},
                                     {'path': 'raw.env', 'format': 'raw'}],
                          'environment': ['A=${TOKEN?private-message}',
                                          'A=${TOKEN:?private-message}',
                                          'B=$TOKEN $TOKEN ${TOKEN:-private-default}',
                                          'C=${OPTION+private-alt}${OPTION:+private-alt}${OPTION-private-default}',
                                          'PASS', 'PASS=private-shadow', 'PASS',
                                          '${KEY:?private-message}=private-value', 'ESCAPE=$$ESCAPED']},
                'a-worker': {'environment': {'PASS': None, 'LOCAL': '${LOCAL?private-message}'}},
                'excluded': {'env_file': 'private-missing.env'}}}))
        self.put('local.env', 'OUT=${TOKEN?private-local}\nOUT=$LOCAL\nLITERAL=\'$EXCLUDED\'\n')
        self.put('raw.env', 'OUT=${RAW:?private-raw}\n')
        self.put('private-unopened.env', 'MALFORMED private-dynamic\n')
        inputs = self.put('input\n# INJECT=value".env', 'OUT=$SHARED $SHARED\nOUT=${TOKEN:?private-input}\n'
                          'UNUSED=private-supplied\nFILE=private-unopened.env\n')
        return compose, inputs

    def test_cli_structured_summary_is_opt_in_and_preserves_export(self):
        compose, inputs = self.fixture()
        flags = ['--compose', compose, '--env-file', inputs, '--discover-template',
                 '--service', 'a-worker', '--service', 'z.api']
        default = self.run_cli(*flags, '--json')
        self.assertEqual(default.returncode, 0, default.stderr)
        original = json.loads(default.stdout)
        self.assertNotIn('variables', original)
        output = self.root / 'checklist.template'
        proc = self.run_cli(*flags, '--checklist-json', '--env-template', output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        from stackwrench.compose_env import discovery_checklist
        summary = json.loads(proc.stdout)
        self.assertEqual(summary, discovery_checklist(original))
        self.assertEqual(summary['report_type'], 'env-template-checklist')
        self.assertEqual([row['variable'] for row in summary['variables']],
                         ['FILE', 'KEY', 'LOCAL', 'META', 'OPTION', 'PASS', 'PROJECT', 'SHARED', 'TOKEN'])
        self.assertEqual(output.read_text(), env_template(original))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(not line or line.startswith('#') for line in output.read_text().splitlines()))
        for forbidden in ['private-', 'RAW', 'EXCLUDED', 'UNUSED', 'ESCAPED', '"value":', '"result":', '"branch":']:
            self.assertNotIn(forbidden, proc.stdout + output.read_text())
        self.assertIn('\\n# INJECT=value\\"', proc.stdout)

    def test_requirements_sources_and_scope_are_preserved_without_io(self):
        from stackwrench.compose_env import discovery_checklist
        compose, inputs = self.fixture()
        report = discover_template(compose, [inputs], service_names=['a-worker', 'z.api'])
        before = copy.deepcopy(report)
        with patch.object(Path, 'open', side_effect=AssertionError('read')), \
                patch.object(Path, 'stat', side_effect=AssertionError('stat')), \
                patch.dict(os.environ, {'TOKEN': 'private-host'}):
            summary = discovery_checklist(report)
        self.assertEqual(report, before)
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['scope'], {'service_selection': 'selected', 'services': ['z.api', 'a-worker'],
                         'shared': ['explicit-dotenv-inputs', 'top-level-compose-values']})
        rows = {r['variable']: r for r in summary['variables']}
        self.assertEqual(rows['TOKEN']['operators'], [':-', ':?', '?', 'direct'])
        self.assertEqual(rows['TOKEN']['required'], 'nonempty')
        for name in ['LOCAL', 'PROJECT']:
            self.assertEqual(rows[name]['required'], 'set')
        for name in ['FILE', 'META', 'OPTION', 'PASS', 'SHARED']:
            self.assertEqual(rows[name]['required'], 'no')
        self.assertEqual(rows['PASS']['operators'], ['inherited-key-only', 'inherited-null'])
        for name, row in rows.items():
            expected = [{k: v for k, v in use.items() if k != 'variable'}
                        for use in report['references'] if use['variable'] == name]
            self.assertEqual(row['sources'], expected)
        self.assertEqual(len(rows['SHARED']['sources']), 2)  # distinct offsets in the same scalar
        self.assertEqual({s['phase'] for s in rows['TOKEN']['sources']},
                         {'compose', 'interpolation-input', 'service-env-file'})
        self.assertEqual(summary['service_env_files'], report['service_env_files'])
        self.assertEqual(summary['unresolved_environment'], report['unresolved_environment'])
        summary['variables'][0]['sources'][0]['field_path'].append('changed')
        summary['service_env_files'][0]['field_path'].append('changed')
        summary['unresolved_environment'][0]['field_path'].append('changed')
        summary['scope']['services'].append('changed')
        self.assertEqual(report, before)

    def test_named_sections_remain_independent_with_selected_read_scope(self):
        from stackwrench.compose_env import discovery_checklist
        from stackwrench.compose_env.trace import _read_text
        compose, inputs = self.fixture()
        later = self.put('second.env', 'OUT=${ONLY_SECOND:?private-message}\n')
        manifest = self.put('scenarios.json', json.dumps({'z_first': {'env_files': [inputs.name]},
                            'a_second': {'env_files': [later.name]}, 'broken': {'env_files': ['private-missing.env']}}))
        allowed = {manifest, compose, inputs, later, self.root / 'local.env', self.root / 'raw.env', self.root / 'absent.env'}
        original_stat = Path.stat
        def read(path, label):
            self.assertIn(path, allowed)
            return _read_text(path, label)
        def stat(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return original_stat(path, *args, **kwargs)
        with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                patch.object(Path, 'stat', autospec=True, side_effect=stat):
            original = discover_scenarios(compose, manifest, scenario_names=['a_second', 'z_first'],
                                          service_names=['z.api'])
            summary = discovery_checklist(original)
        self.assertEqual(summary['report_type'], 'scenario-template-checklist')
        self.assertEqual(list(summary['scenarios']), ['z_first', 'a_second'])
        self.assertNotIn('variables', summary)
        first, second = summary['scenarios'].values()
        self.assertIn('SHARED', [r['variable'] for r in first['variables']])
        self.assertNotIn('ONLY_SECOND', [r['variable'] for r in first['variables']])
        self.assertNotIn('SHARED', [r['variable'] for r in second['variables']])
        self.assertIn('ONLY_SECOND', [r['variable'] for r in second['variables']])
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest, '--discover-template',
                            '--scenario', 'a_second', '--scenario', 'z_first', '--service', 'z.api',
                            '--checklist-json', '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), summary)

    def test_all_scope_empty_and_complete_controls(self):
        from stackwrench.compose_env import discovery_checklist
        compose = self.put('compose.yaml', 'services: {demo: {environment: {EMPTY: literal}}}\n')
        report = discover_template(compose, require_complete=True)
        summary = discovery_checklist(report)
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['variables'], [])
        self.assertEqual(summary['scope'], {'service_selection': 'all', 'services': None,
                         'shared': ['explicit-dotenv-inputs', 'top-level-compose-values']})
        proc = self.run_cli('--compose', compose, '--discover-template', '--checklist-json', '--require-complete')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), summary)

    def test_api_rejects_wrong_reports_and_projects_only_public_fields(self):
        from stackwrench.compose_env import discovery_checklist, trace
        compose = self.put('compose.yaml', 'services: {demo: {environment: ["A=$TOKEN"]}}\n')
        original = discover_template(compose, service_names=['demo'])
        invalid = [None, [], 'private-report', {}, trace(compose), discovery_checklist(original)]
        patches = [{'schema_version': 2}, {'schema_version': True}, {'report_type': 'scenario-comparison'},
                   {'values_shown': True}, {'evaluation': 'evaluated'}, {'complete': 1},
                   {'references': None}, {'references': [{'variable': 'BAD-private'}]},
                   {'scope': {'services': [], 'shared': []}}]
        for update in patches:
            bad = copy.deepcopy(original); bad.update(update); invalid.append(bad)
        for field, value in [('operator', 'private-operator'), ('phase', 'private-phase'),
                             ('field_path', [True]), ('location', None), ('offset', -1)]:
            bad = copy.deepcopy(original); bad['references'][0][field] = value; invalid.append(bad)
        invalid.append({'schema_version': 1, 'report_type': 'scenario-template-discovery',
                        'evaluation': 'syntax-only', 'values_shown': False, 'host_environment': 'not-read',
                        'implicit_dotenv': 'not-read', 'complete': True, 'scenarios': {'private-name': original}})
        for field, value in [('status', 'private-status'), ('required', 'private-boolean'),
                             ('format', 'private-format'), ('field_path', [None])]:
            bad = copy.deepcopy(original)
            bad['service_env_files'] = [{'service': 'demo', 'field_path': ['services', 'demo', 'env_file'],
                                        'location': 'source:1:1', 'required': True, 'format': 'compose',
                                        'status': 'scanned', field: value}]
            invalid.append(bad)
        for report in invalid:
            with self.subTest(report=report):
                with self.assertRaises(TraceError) as caught:
                    discovery_checklist(report)
                self.assertEqual(str(caught.exception), 'checklist requires a valid syntax-only discovery report')
        polluted = copy.deepcopy(original)
        polluted['value'] = 'private-value'
        polluted['references'][0].update(value='private-value', operand='private-operand', message='private-message')
        self.assertEqual(discovery_checklist(polluted), discovery_checklist(original))

    def test_cli_invalid_modes_precede_reads_and_runtime_errors_are_atomic(self):
        from stackwrench.compose_env.cli import main
        for flags in [[], ['--scenario-file', 'private-manifest'],
                      ['--discover-template', '--show-values'], ['--discover-template', '--shell-env', 'private-shell']]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('stackwrench.compose_env.trace._read_text', side_effect=AssertionError('input read')), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    main(['--compose', 'private-compose', '--checklist-json', *flags])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertNotIn('private-', stderr.getvalue())
        compose, inputs = self.fixture()
        manifest = self.put('scenarios.json', '{"first": {}, "later": {"env_files": ["missing.env"]}}')
        output = self.root / 'blocked.template'
        for flags in [[], ['--service', 'z.api', '--require-complete'],
                      ['--service', 'z.api', '--scenario-file', manifest]]:
            proc = self.run_cli('--compose', compose, '--discover-template', '--checklist-json',
                                '--env-template', output, *flags)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, '')
            self.assertIn('error', json.loads(proc.stderr))
            self.assertNotIn('private-', proc.stderr)
            self.assertFalse(output.exists())
        existing = self.put('existing.template', 'KEEP=private-value\n')
        link = self.root / 'link.template'; link.symlink_to(existing)
        for target in [existing, link]:
            proc = self.run_cli('--compose', compose, '--discover-template', '--checklist-json',
                                '--service', 'z.api', '--env-template', target)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, '')
            self.assertIn('error', json.loads(proc.stderr))
        self.assertEqual(existing.read_text(), 'KEEP=private-value\n')
        self.assertTrue(link.is_symlink())

    def test_literal_service_metadata_and_projection_failure_before_export(self):
        from stackwrench.compose_env import discovery_checklist
        from stackwrench.compose_env.cli import main
        names = ['', '\0\n# FIELD=literal"']
        compose = self.put('compose.yaml', json.dumps({'services': {
            name: {'env_file': '${FILE:-private-target}', 'environment': ['${KEY}=private-value']}
            for name in names}}))
        original = discover_template(compose)
        summary = discovery_checklist(original)
        self.assertEqual([f['service'] for f in summary['service_env_files']], names)
        self.assertEqual([e['service'] for e in summary['unresolved_environment']], names)
        self.assertEqual(summary['variables'][0]['sources'], [
            {k: v for k, v in use.items() if k != 'variable'}
            for use in original['references'] if use['variable'] == 'FILE'])
        output = self.root / 'must-not-exist.template'
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('stackwrench.compose_env.cli.discovery_checklist', side_effect=TraceError('redacted projection failure')), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--compose', str(compose), '--discover-template', '--checklist-json',
                         '--env-template', str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('error', json.loads(stderr.getvalue()))
        self.assertFalse(output.exists())
