"""Baseline-relative syntax checklists, separate presence/requirements and coverage."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from stackwrench.compose_env import TraceError, discover_scenarios, discover_template, discovery_checklist, env_template
from .test_trace import Workspace


ERROR = 'checklist comparison requires a valid named syntax-only discovery report with at least two scenarios'


class ChecklistComparisons(Workspace):
    def fixture(self):
        compose = self.put('compose.yaml', json.dumps({
            'x-meta\n# FIELD=quoted"': '$META',
            'services': {
                'z.api': {'env_file': ['local.env', '${FILE:-private-target.env}'],
                          'environment': ['A=$SHARED $SHARED', 'A=${SHARED:-private-default}',
                                          'PASS', 'PASS=private-shadow', 'PASS',
                                          '${KEY:?private-message}=private-value', 'ESC=$$ESCAPED']},
                'a-worker': {'environment': {'PASS': None, 'A': '${SHARED?private-message}'}},
                'excluded': {'env_file': 'missing.env'}}}))
        local = self.put('local.env', 'OUT=${LOCAL:?private-message}\n')
        self.put('private-target.env', 'MALFORMED private-target\n')
        before = self.put('base\n# INJECT=value".env',
                          'OUT=$REMOVE\nOUT=$OP\nOUT=${REQ?private-message}\nOUT=${RELAX?private-message}\n'
                          'OUT=$REPEAT $REPEAT\nOUT=\'$IGNORED\'\nFILE=private-target.env\n')
        after = self.put('later.env',
                         'OUT=${ADD:?private-message}\nOUT=${OP:-private-default}\nOUT=${REQ:?private-message}\n'
                         'OUT=$RELAX\nOUT=$REPEAT\nUNUSED=private-supplied\n')
        manifest = self.put('scenarios.json', json.dumps({
            'z_base': {'env_files': [before.name]}, 'a_later': {'env_files': [after.name]},
            'm_identical': {'env_files': [before.name]}, 'broken': {'env_files': ['missing.env']}}))
        return compose, manifest, before, after, local

    def flags(self, compose, manifest):
        return ['--compose', compose, '--scenario-file', manifest, '--discover-template',
                '--scenario', 'm_identical', '--scenario', 'a_later', '--scenario', 'z_base',
                '--service', 'a-worker', '--service', 'z.api']

    def test_cli_comparison_is_opt_in_and_baseline_follows_manifest(self):
        compose, manifest, *_ = self.fixture()
        flags = self.flags(compose, manifest)
        original_proc = self.run_cli(*flags, '--json')
        self.assertEqual(original_proc.returncode, 0, original_proc.stderr)
        original = json.loads(original_proc.stdout)
        self.assertNotIn('comparisons', original)
        output = self.root / 'new.template'
        proc = self.run_cli(*flags, '--compare-checklists-json', '--env-template', output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        from stackwrench.compose_env import compare_discovery_checklists
        result = json.loads(proc.stdout)
        self.assertEqual(result, compare_discovery_checklists(original))
        self.assertEqual(result['report_type'], 'scenario-template-checklist-comparison')
        self.assertEqual(result['baseline'], 'z_base')
        self.assertEqual(list(result['scenarios']), ['z_base', 'a_later', 'm_identical'])
        self.assertEqual(list(result['comparisons']), ['a_later', 'm_identical'])
        self.assertEqual(result['scenarios'], discovery_checklist(original)['scenarios'])
        self.assertEqual(output.read_text(), env_template(original))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(not line or line.startswith('#') for line in output.read_text().splitlines()))
        self.assertIn('\\n# INJECT=value\\"', proc.stdout)
        self.assertIn('\\n# FIELD=quoted\\"', proc.stdout)
        for forbidden in ['private-', 'IGNORED', 'UNUSED', 'ESCAPED', '"value":', '"branch":', '"result_status":']:
            self.assertNotIn(forbidden, proc.stdout + output.read_text())
        # Both existing representations retain their exact prior shapes.
        proc = self.run_cli(*flags, '--checklist-json')
        self.assertEqual(json.loads(proc.stdout), discovery_checklist(original))

    def test_presence_operators_requirements_and_per_pair_unions(self):
        from stackwrench.compose_env import compare_discovery_checklists
        compose, manifest, *_ = self.fixture()
        original = discover_scenarios(compose, manifest, scenario_names=['m_identical', 'a_later', 'z_base'],
                                      service_names=['a-worker', 'z.api'])
        result = compare_discovery_checklists(original)
        self.assertFalse(result['complete'])
        rows = result['comparisons']['a_later']['variables']
        self.assertEqual([r['variable'] for r in rows],
                         ['ADD', 'FILE', 'KEY', 'LOCAL', 'META', 'OP', 'PASS', 'RELAX', 'REMOVE', 'REPEAT', 'REQ', 'SHARED'])
        rows = {r['variable']: r for r in rows}
        self.assertEqual(rows['ADD'], dict(variable='ADD', baseline_present=False, scenario_present=True,
                         presence_change='added', baseline_operators=[], scenario_operators=[':?'],
                         baseline_required=None, scenario_required='nonempty', operators_changed=False,
                         requirement_changed=False, source_changed=False))
        self.assertEqual(rows['REMOVE']['presence_change'], 'removed')
        self.assertEqual(rows['REMOVE']['baseline_required'], 'no')
        self.assertIsNone(rows['REMOVE']['scenario_required'])
        for name, before, after, requirement_changed in [('OP', 'no', 'no', False), ('REQ', 'set', 'nonempty', True),
                                                       ('RELAX', 'set', 'no', True)]:
            self.assertEqual(rows[name]['presence_change'], 'unchanged')
            self.assertTrue(rows[name]['operators_changed'])
            self.assertEqual(rows[name]['requirement_changed'], requirement_changed)
            self.assertEqual((rows[name]['baseline_required'], rows[name]['scenario_required']), (before, after))
        self.assertFalse(rows['REPEAT']['operators_changed'])
        self.assertFalse(rows['REPEAT']['requirement_changed'])
        self.assertTrue(rows['REPEAT']['source_changed'])
        self.assertEqual(rows['PASS']['baseline_operators'], ['inherited-key-only', 'inherited-null'])
        self.assertFalse(rows['PASS']['source_changed'])
        identical = result['comparisons']['m_identical']['variables']
        self.assertNotIn('ADD', [r['variable'] for r in identical])
        for row in identical:
            self.assertEqual(row['presence_change'], 'unchanged')
            self.assertFalse(any(row[k] for k in ['operators_changed', 'requirement_changed', 'source_changed']))
        for name, child in result['scenarios'].items():
            self.assertEqual(child['scope']['services'], ['z.api', 'a-worker'])
            for key in ['service_env_files', 'unresolved_environment', 'complete']:
                self.assertEqual(child[key], original['scenarios'][name][key])
        subset = discover_scenarios(compose, manifest, scenario_names=['m_identical', 'a_later'],
                                    service_names=['z.api'])
        selected = compare_discovery_checklists(subset)
        self.assertEqual(selected['baseline'], 'a_later')  # earlier manifest name is unselected
        self.assertEqual(list(selected['comparisons']), ['m_identical'])

    def test_source_change_is_ordered_metadata_not_operator_or_coverage(self):
        from stackwrench.compose_env import compare_discovery_checklists
        compose = self.put('simple.yaml', 'services: {demo: {environment: ["A=$TOKEN", "B=$TOKEN"]}}\n')
        child = discover_template(compose)
        common = {k: child[k] for k in ['schema_version', 'evaluation', 'values_shown', 'host_environment',
                                       'implicit_dotenv', 'complete']}
        report = {**common, 'report_type': 'scenario-template-discovery',
                  'scenarios': {'first': child, 'second': copy.deepcopy(child)}}
        second = report['scenarios']['second']
        second['references'][0]['operator'] = ':?'
        second['scope'] = {'services': ['demo'], 'shared': ['explicit-dotenv-inputs', 'top-level-compose-values']}
        def row():
            return compare_discovery_checklists(report)['comparisons']['second']['variables'][0]
        self.assertEqual(row(), dict(variable='TOKEN', baseline_present=True, scenario_present=True,
                         presence_change='unchanged', baseline_operators=['direct'], scenario_operators=[':?', 'direct'],
                         baseline_required='no', scenario_required='nonempty', operators_changed=True,
                         requirement_changed=True, source_changed=False))
        for mutation in ['location', 'phase', 'field_path', 'offset', 'order', 'repeat']:
            saved = copy.deepcopy(second['references'])
            if mutation == 'order': second['references'].reverse()
            elif mutation == 'repeat': second['references'].append(copy.deepcopy(second['references'][0]))
            elif mutation == 'phase': second['references'][0]['phase'] = 'interpolation-input'
            elif mutation == 'offset': second['references'][0]['offset'] += 1
            elif mutation == 'field_path': second['references'][0]['field_path'].append('literal')
            else: second['references'][0]['location'] += 'literal-alias'
            self.assertTrue(row()['source_changed'], mutation)
            second['references'] = saved
        empty = discover_template(self.put('empty.yaml', 'services: {}\n'))
        report['scenarios'] = {'first': empty, 'second': copy.deepcopy(empty)}
        self.assertEqual(compare_discovery_checklists(report)['comparisons'], {'second': {'variables': []}})
        # Different coverage never invents a variable, requirement or source difference.
        report['scenarios'] = {'first': child, 'second': copy.deepcopy(child)}
        report['scenarios']['second']['complete'] = report['complete'] = False
        report['scenarios']['second']['unresolved_environment'] = [
            {'service': 'demo', 'field_path': ['services', 'demo', 'environment', 2],
             'location': 'literal:3:1', 'status': 'unresolved-dynamic'}]
        result = compare_discovery_checklists(report)
        self.assertTrue(result['scenarios']['first']['complete'])
        self.assertFalse(result['scenarios']['second']['complete'])
        self.assertFalse(result['complete'])
        self.assertFalse(any(result['comparisons']['second']['variables'][0][key] for key in
                             ['operators_changed', 'requirement_changed', 'source_changed']))

    def test_pure_no_additional_io_and_selected_read_scope(self):
        from stackwrench.compose_env import compare_discovery_checklists
        from stackwrench.compose_env.trace import _read_text
        compose, manifest, before, after, local = self.fixture()
        allowed = {compose, manifest, before, after, local}
        actual_stat = Path.stat
        reads = []
        def read(path, label):
            self.assertIn(path, allowed); reads.append(path)
            return _read_text(path, label)
        def stat(path, *args, **kwargs):
            self.assertIn(path, allowed)
            return actual_stat(path, *args, **kwargs)
        with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                patch.object(Path, 'stat', autospec=True, side_effect=stat):
            original = discover_scenarios(compose, manifest, scenario_names=['a_later', 'z_base'],
                                          service_names=['a-worker', 'z.api'])
        self.assertEqual([p for p in reads if p in (before, after)], [before, after])
        original['scenarios']['z_base']['references'][0]['value'] = 'private-value'
        saved = copy.deepcopy(original)
        with patch.object(Path, 'open', side_effect=AssertionError('read')), \
                patch.object(Path, 'stat', side_effect=AssertionError('stat')), \
                patch('builtins.open', side_effect=AssertionError('open')), \
                patch.object(type(os.environ), '__getitem__', side_effect=AssertionError('host')):
            result = compare_discovery_checklists(original)
        self.assertEqual(original, saved)
        self.assertNotIn('private-value', json.dumps(result))
        result['scenarios']['z_base']['variables'][0]['sources'][0]['field_path'].append('mutation')
        result['scenarios']['z_base']['scope']['services'].append('mutation')
        result['comparisons']['a_later']['variables'][0]['scenario_operators'].append('mutation')
        self.assertEqual(original, saved)
        self.assertEqual(compare_discovery_checklists(json.loads(json.dumps(original))),
                         compare_discovery_checklists(original))

    def test_api_invalid_reports_are_fixed_redacted_errors(self):
        from stackwrench.compose_env import compare_discovery_checklists, trace
        compose = self.put('compose.yaml', 'services: {}\n')
        child = discover_template(compose)
        manifest = self.put('scenarios.json', '{"first": {}, "second": {}}')
        original = discover_scenarios(compose, manifest)
        invalid = [None, [], {}, child, trace(compose), discovery_checklist(original),
                   compare_discovery_checklists(original)]
        for update in [{'scenarios': {}}, {'scenarios': {'only': child}}, {'schema_version': 2},
                       {'values_shown': True}, {'evaluation': 'evaluated'}, {'complete': False},
                       {'scenarios': {'bad-private': child, 'second': child}},
                       {'scenarios': {f'n{i}': child for i in range(17)}}]:
            bad = copy.deepcopy(original); bad.update(update); invalid.append(bad)
        for update in [{'references': [{'variable': 'PRIVATE', 'operator': 'private-operator'}]},
                       {'complete': 'private-bool'}, {'service_env_files': [None]}, {'scope': None}]:
            bad = copy.deepcopy(original); bad['scenarios']['second'].update(update); invalid.append(bad)
        for bad in invalid:
            with self.subTest(report=bad), self.assertRaises(TraceError) as caught:
                compare_discovery_checklists(bad)
            self.assertEqual(str(caught.exception), ERROR)
        polluted = copy.deepcopy(original); polluted['value'] = 'private-value'
        self.assertEqual(compare_discovery_checklists(polluted), compare_discovery_checklists(original))

    def test_invalid_cli_modes_fail_before_reads(self):
        from stackwrench.compose_env.cli import main
        for flags in [[], ['--scenario-file', 'private-manifest'], ['--discover-template'],
                      ['--discover-template', '--scenario-file', 'private-manifest', '--checklist-json'],
                      ['--discover-template', '--scenario-file', 'private-manifest', '--show-values'],
                      ['--discover-template', '--scenario-file', 'private-manifest', '--shell-env', 'private-shell'],
                      ['--discover-template', '--scenario-file', 'private-manifest', '--env-file', 'private-input']]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('stackwrench.compose_env.trace._read_text', side_effect=AssertionError('read')), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    main(['--compose', 'private-compose', '--compare-checklists-json', *flags])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertNotIn('private-', stderr.getvalue())

    def test_runtime_errors_gate_projection_and_exports_are_atomic(self):
        from stackwrench.compose_env.cli import main
        compose, manifest, *_ = self.fixture()
        output = self.root / 'blocked.template'
        common = ['--compose', compose, '--scenario-file', manifest, '--discover-template',
                  '--compare-checklists-json', '--service', 'z.api', '--env-template', output]
        for flags in [[], ['--scenario', 'z_base'], ['--scenario', 'z_base', '--scenario', 'a_later', '--require-complete']]:
            proc = self.run_cli(*common, *flags)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, '')
            self.assertIn('error', json.loads(proc.stderr))
            self.assertNotIn('private-', proc.stderr)
            self.assertFalse(output.exists())
        self.put('missing.env', 'OUT=${BAD/private-operand}\n')
        proc = self.run_cli(*common)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, '')
        self.assertNotIn('private-', proc.stderr)
        self.assertFalse(output.exists())
        selected = self.flags(compose, manifest)
        existing = self.put('existing.template', 'KEEP=private-value\n')
        link = self.root / 'link.template'; link.symlink_to(existing)
        for target in [existing, link]:
            proc = self.run_cli(*selected, '--compare-checklists-json', '--env-template', target)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, '')
            self.assertIn('error', json.loads(proc.stderr))
        self.assertEqual(existing.read_text(), 'KEEP=private-value\n')
        self.assertTrue(link.is_symlink())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('stackwrench.compose_env.cli.compare_discovery_checklists', side_effect=TraceError(ERROR)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([*map(str, selected), '--compare-checklists-json', '--env-template', str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('error', json.loads(stderr.getvalue()))
        self.assertFalse(output.exists())
