"""Static reverse reachability contracts: shortest witnesses, cycles, no execution."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from stackwrench.podman_audit.input import InputError, load_document, read_document

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "podman-audit"]


def query(model, selected):
    from stackwrench.podman_audit.impact import impact_query
    return impact_query(model, selected)


def run_cli(path, selected, *options):
    return subprocess.run([*CLI, str(path), '--impact-service', selected, *options],
                          capture_output=True, text=True, timeout=15)


class ImpactTests(unittest.TestCase):
    def test_chain_cli_regression(self):
        for suffix in ('yaml', 'json'):
            with self.subTest(suffix=suffix):
                result = run_cli(ROOT / ('examples/podman_audit/impact-chain.' + suffix), 'db', '--json')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['directDependents'], ['api'])
                self.assertEqual(report['transitiveDependents'], ['web'])
                self.assertEqual([r['witnessPath'] for r in report['dependents']],
                                 [['api', 'db'], ['web', 'api', 'db']])
                self.assertEqual([r['distance'] for r in report['dependents']], [1, 2])
                self.assertEqual([r['direct'] for r in report['dependents']], [True, False])
                self.assertEqual(report['mode'], 'impact')
                self.assertEqual(report['assessment'], 'declared-dependency-impact')
                self.assertEqual(report['selectedCycle'], dict(participates=False, selfCycle=False, services=[]))
                for key in ('provider', 'providerVersion', 'findings', 'compatibility', 'catalogVersion', 'summary'):
                    self.assertNotIn(key, report)
                self.assertEqual(result.stderr, '')

    def test_original_yaml_json_coordinates_and_edge_metadata(self):
        for suffix in ('yaml', 'json'):
            model = read_document(ROOT / ('examples/podman_audit/impact-chain.' + suffix))
            original = copy.deepcopy(model)
            report = query(model, 'db')
            api, web = report['dependents']
            points = [(5, 7), (6, 9), (7, 9), (8, 9)] if suffix == 'yaml' else [(6, 9), (7, 11), (8, 11), (9, 11)]
            base = '$.services.api.depends_on.db'
            locations = [dict(path=base + end, declaration='present', line=line, column=col)
                         for end, (line, col) in zip(('', '.condition', '.required', '.restart'), points)]
            self.assertEqual(api['witnessEdges'], [dict(service='api', target='db', form='long',
                condition='service_healthy', conditionDeclared=True, required=False, requiredDeclared=True,
                restart=True, path=base, fieldLocations=locations)])
            edge = web['witnessEdges'][0]
            self.assertEqual({k: edge[k] for k in ('form', 'condition', 'conditionDeclared', 'required', 'requiredDeclared', 'restart')},
                dict(form='short', condition='service_started', conditionDeclared=False, required=True,
                     requiredDeclared=False, restart=None))
            self.assertEqual(edge['fieldLocations'], [dict(path='$.services.web.depends_on[0]', declaration='present',
                line=10 if suffix == 'yaml' else 15, column=18 if suffix == 'yaml' else 9)])
            self.assertEqual(web['witnessEdges'][1], api['witnessEdges'][0])
            self.assertEqual(report['selectedServiceLocation'], dict(path='$.services.db', declaration='present',
                line=2 if suffix == 'yaml' else 3, column=3 if suffix == 'yaml' else 5))
            self.assertEqual(model, original)

    def test_diamond_shortest_tie_breaking_exact_names_and_permutations(self):
        # Tie-breaking from dependent toward selector differs from reverse-BFS discovery order.
        model = {'services': {'db.root': {}, 'z': {'depends_on': ['db.root']},
            'a': {'depends_on': ['db.root']}, 'A': {'depends_on': ['z']},
            'B': {'depends_on': ['a']}, 'web-ui': {'depends_on': ['B', 'A']},
            'direct': {'depends_on': ['web-ui', 'db.root']}, 'isolated': {}}}
        report = query(model, 'db.root')
        rows = {r['service']: r for r in report['dependents']}
        self.assertEqual(rows['web-ui']['witnessPath'], ['web-ui', 'A', 'z', 'db.root'])
        self.assertEqual(rows['direct']['witnessPath'], ['direct', 'db.root'])
        self.assertEqual(report['directDependents'], ['a', 'direct', 'z'])
        self.assertEqual(report['transitiveDependents'], ['A', 'B', 'web-ui'])
        self.assertEqual([r['service'] for r in report['dependents']], sorted(rows))
        self.assertEqual(rows['a']['witnessEdges'][0]['path'], '$.services.a.depends_on[0]')
        for svc in model['services'].values():
            if 'depends_on' in svc:
                svc['depends_on'].reverse()
        model['services'] = dict(reversed(list(model['services'].items())))
        again = query(model, 'db.root')
        # Short-list indices really change when text order changes; graph choices do not.
        for r, s in zip(report['dependents'], again['dependents']):
            self.assertEqual({k:v for k,v in r.items() if k != 'witnessEdges'},
                             {k:v for k,v in s.items() if k != 'witnessEdges'})
        self.assertEqual(query(model, 'isolated')['dependents'], [])
        self.assertTrue(all(p['declaration'] == 'unavailable' and p['line'] is None and p['column'] is None
                            for r in again['dependents'] for e in r['witnessEdges'] for p in e['fieldLocations']))
        self.assertEqual(query(model, 'db.root'), again)

    def test_optional_all_conditions_cycles_selected_exclusion_and_missing_targets(self):
        model = {'services': {'root': {'depends_on': {'peer': {'condition':'service_completed_successfully', 'required':False},
                                                     'root': {'condition':'service_started', 'required':False}}},
            'peer': {'depends_on': {'root': {'condition':'service_healthy', 'restart':False}}},
            'incoming': {'depends_on':['peer']}, 'u': {'depends_on':['v', 'root']},
            'v': {'depends_on':['u']}, 'dangling': {'depends_on':['absent']},
            'separate': {'depends_on':['separate']}}}
        report = query(model, 'root')
        self.assertEqual([r['service'] for r in report['dependents']], ['incoming', 'peer', 'u', 'v'])
        self.assertEqual(report['selectedCycle'], dict(participates=True, selfCycle=True, services=['peer','root']))
        rows = {r['service']: r for r in report['dependents']}
        self.assertEqual(rows['peer']['cycle'], dict(participates=True, selfCycle=False, services=['peer','root']))
        self.assertEqual(rows['u']['cycle']['services'], ['u','v'])
        self.assertFalse(rows['incoming']['cycle']['participates'])
        self.assertEqual(rows['v']['witnessPath'], ['v','u','root'])
        peer = query(model, 'peer')
        edge = next(r for r in peer['dependents'] if r['service'] == 'root')['witnessEdges'][0]
        self.assertEqual(edge['condition'], 'service_completed_successfully')
        self.assertIs(edge['required'], False)
        self.assertEqual(query(model, 'dangling')['dependents'], [])
        self_only = query(model, 'separate')
        self.assertEqual(self_only['dependents'], [])
        self.assertEqual(self_only['selectedCycle'], dict(participates=True, selfCycle=True, services=['separate']))
        with self.assertRaises(InputError) as ctx:
            query(model, 'absent')
        self.assertEqual(ctx.exception.detail['code'], 'unknown-service')

    def test_cycle_safe_maximum_chain_and_graph_matches_audit_sccs(self):
        from stackwrench.podman_audit.audit import audit
        model = {'services': {f's{i:03}': {'depends_on':[f's{i-1:03}']} for i in range(1,500)}}
        model['services']['s000'] = {'depends_on':['s499']}
        report = query(model, 's000')
        self.assertEqual(len(report['dependents']), 499)
        self.assertEqual(report['dependents'][-1]['distance'], 499)
        self.assertEqual(len(report['selectedCycle']['services']), 500)
        audit_report = audit(model, provider='podman-compose', provider_version='1.6.0')
        cycles = [r['details']['cycleServices'] for r in audit_report['findings'] if r['ruleId'] == 'DEP003']
        self.assertEqual(len(cycles), 500)
        self.assertTrue(all(c == report['selectedCycle']['services'] for c in cycles))

    def test_human_json_no_execution_no_catalog_and_bytes_preserved(self):
        from stackwrench.podman_audit.cli import main
        with tempfile.TemporaryDirectory() as temp:
            sentinel = Path(temp) / 'MUST_NOT_EXIST'
            model = {'services': {'db': {'healthcheck': {'test':['CMD-SHELL', f'touch {sentinel}'], 'start_interval':'1s'}},
                'api': {'command':f'touch {sentinel}', 'depends_on': {'db': {'condition':'service_healthy'}}}}}
            path = Path(temp) / 'compose.json'
            path.write_text(json.dumps(model))
            before = path.read_bytes()
            with patch('stackwrench.podman_audit.audit._catalog_bytes', side_effect=AssertionError('catalog used')), \
                 patch('stackwrench.podman_audit.cli.audit', side_effect=AssertionError('audit used')), \
                 patch('stackwrench.podman_audit.cli.compare_profiles', side_effect=AssertionError('compare used')), \
                 patch('subprocess.run', side_effect=AssertionError('subprocess used')):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main([str(path),'--impact-service','db','--json']), 0)
                report = json.loads(stdout.getvalue())
                self.assertEqual(stderr.getvalue(), '')
            result = run_cli(path, 'db')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Declared dependency impact', result.stdout)
            detail = next(line for line in result.stdout.splitlines() if line.startswith('Dependent: '))
            self.assertEqual(json.loads(detail.removeprefix('Dependent: ')), report['dependents'][0])
            self.assertIn('not predicted startup order', result.stdout)
            self.assertNotIn(str(sentinel), stdout.getvalue() + result.stdout)
            self.assertFalse(sentinel.exists())
            self.assertEqual(path.read_bytes(), before)

    def test_conflicts_unknown_selectors_and_malformed_documents_before_result(self):
        path = ROOT / 'examples/podman_audit/impact-chain.yaml'
        conflicts = [['--provider','podman-compose'], ['--provider-version','1.6.0'], ['--explain'],
            ['--compare-profiles','/must-not-read/left','/must-not-read/right'],
            *[['--fail-on', x] for x in ('error','warning','never')], ['--fail-on=never']]
        for flags in conflicts:
            with self.subTest(flags=flags):
                result = run_cli('/must-not-read/compose', 'db', '--json', *flags)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)['error']['code'], 'invalid-arguments')
        for selected in ('', 'DB', 'absent', ' db'):
            result = run_cli(path, selected, '--json')
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stdout)
            self.assertEqual(error['error']['code'], 'unknown-service')
            self.assertNotIn('dependents', error)
        result = run_cli(path, 'missing')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('unknown-service', result.stderr)
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / 'bad.yaml'
            for text in ('services: {db: {depends_on: {x: {condition: bogus}}}}',
                         'services: {db: {depends_on: {x: {condition: service_started, required: 0}}}}',
                         'services: {db: {healthcheck: {disable: text}}}',
                         'services: {db: {profiles: [dev]}}', 'services: {db: {}, db: {}}',
                         'services: {db: {}}\n---\nservices: {db: {}}',
                         'services: {db: {image: "${UNRESOLVED}"}}', 'services: [db]'):
                bad.write_text(text)
                result = run_cli(bad, 'db', '--json')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                error = json.loads(result.stdout)
                self.assertEqual(error['assessment'], 'not-evaluated')
                self.assertNotIn('dependents', error)
                self.assertNotIn('selectedService', error)
        with self.assertRaises(InputError):
            query({'services': {'db': {'healthcheck': {'disable':'wrong'}}}}, 'absent')

    def test_escaped_paths_long_defaults_restart_false_and_mapping_order(self):
        model = {'services': {'db.root': {}, 'web-ui': {'depends_on': {'db.root':
            {'condition':'service_completed_successfully', 'restart':False}}}}}
        text = json.dumps(model, ensure_ascii=True)
        report = query(load_document(text, format='json'), 'db.root')
        edge = report['dependents'][0]['witnessEdges'][0]
        self.assertEqual(edge['path'], '$.services["web-ui"].depends_on["db.root"]')
        self.assertEqual([p['path'] for p in edge['fieldLocations']],
                         [edge['path'], edge['path']+'.condition', edge['path']+'.restart'])
        self.assertTrue(all(p['declaration'] == 'present' for p in edge['fieldLocations']))
        self.assertTrue(edge['required'])
        self.assertFalse(edge['requiredDeclared'])
        self.assertIs(edge['restart'], False)
        baseline = query(model, 'db.root')
        model['services'] = dict(reversed(list(model['services'].items())))
        self.assertEqual(query(model, 'db.root'), baseline)


if __name__ == '__main__':
    unittest.main()
