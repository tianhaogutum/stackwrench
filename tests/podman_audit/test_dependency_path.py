"""Directed static path contracts; declarations do not predict execution."""
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


def run_cli(path, source, target, *options):
    return subprocess.run([*CLI, str(path), '--dependency-path', source, target, *options],
                          capture_output=True, text=True, timeout=15)


def query(model, source, target):
    from stackwrench.podman_audit.impact import dependency_path_query
    return dependency_path_query(model, source, target)


class DependencyPathTests(unittest.TestCase):
    def test_directed_chain_cli_regression(self):
        for suffix in ('yaml', 'json'):
            with self.subTest(suffix=suffix):
                path = ROOT / ('examples/podman_audit/impact-chain.' + suffix)
                result = run_cli(path, 'web', 'db', '--json')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['mode'], 'dependency-path')
                self.assertEqual(report['assessment'], 'declared-dependency-path')
                self.assertTrue(report['reachable'])
                self.assertEqual(report['distance'], 2)
                self.assertEqual(report['witnessPath'], ['web', 'api', 'db'])
                self.assertEqual([(e['service'], e['target']) for e in report['witnessEdges']],
                                 [('web', 'api'), ('api', 'db')])
                self.assertEqual((report['sourceService'], report['targetService']), ('web', 'db'))
                self.assertEqual(result.stderr, '')
                reverse = run_cli(path, 'db', 'web', '--json')
                self.assertEqual(reverse.returncode, 0, reverse.stdout + reverse.stderr)
                unreachable = json.loads(reverse.stdout)
                self.assertFalse(unreachable['reachable'])
                self.assertIsNone(unreachable['distance'])
                self.assertEqual(unreachable['witnessPath'], [])
                self.assertEqual(unreachable['witnessEdges'], [])

    def test_original_coordinates_and_edge_defaults(self):
        for suffix in ('yaml', 'json'):
            model = read_document(ROOT / ('examples/podman_audit/impact-chain.' + suffix))
            before = copy.deepcopy(model)
            report = query(model, 'web', 'db')
            short, long = report['witnessEdges']
            points = [(5, 7), (6, 9), (7, 9), (8, 9)] if suffix == 'yaml' else [(6, 9), (7, 11), (8, 11), (9, 11)]
            base = '$.services.api.depends_on.db'
            self.assertEqual(long, dict(service='api', target='db', form='long',
                condition='service_healthy', conditionDeclared=True, required=False, requiredDeclared=True,
                restart=True, path=base, fieldLocations=[dict(path=base+end, declaration='present', line=line, column=col)
                    for end, (line,col) in zip(('', '.condition', '.required', '.restart'), points)]))
            self.assertEqual(short, dict(service='web', target='api', form='short',
                condition='service_started', conditionDeclared=False, required=True, requiredDeclared=False,
                restart=None, path='$.services.web.depends_on[0]', fieldLocations=[dict(
                    path='$.services.web.depends_on[0]', declaration='present',
                    line=10 if suffix == 'yaml' else 15, column=18 if suffix == 'yaml' else 9)]))
            for key, name, line, col in [('sourceServiceLocation','web',9 if suffix=='yaml' else 13,3 if suffix=='yaml' else 5),
                                         ('targetServiceLocation','db',2 if suffix=='yaml' else 3,3 if suffix=='yaml' else 5)]:
                self.assertEqual(report[key], dict(path='$.services.'+name, declaration='present', line=line, column=col))
            self.assertEqual(report['sourceCycle'], dict(participates=False, selfCycle=False, services=[]))
            self.assertEqual(report['targetCycle'], report['sourceCycle'])
            self.assertEqual(model, before)
            self.assertEqual(query(model, 'web', 'db'), report)

    def test_diamond_lexical_ties_shortcuts_disconnected_and_order(self):
        model = {'services': {'root': {'depends_on':['z', 'A']}, 'z': {'depends_on':['a']},
            'A': {'depends_on':['Z']}, 'a': {'depends_on':['db.root']},
            'Z': {'depends_on':['db.root']}, 'db.root': {}, 'isolated': {}}}
        report = query(model, 'root', 'db.root')
        self.assertEqual(report['witnessPath'], ['root','A','Z','db.root'])
        self.assertEqual(report['distance'], 3)
        self.assertTrue(all(p['declaration']=='unavailable' and p['line'] is None and p['column'] is None
                            for e in report['witnessEdges'] for p in e['fieldLocations']))
        model['services'] = dict(reversed(list(model['services'].items())))
        for svc in model['services'].values():
            if 'depends_on' in svc: svc['depends_on'].reverse()
        again = query(model, 'root', 'db.root')
        self.assertEqual({k:v for k,v in report.items() if k!='witnessEdges'},
                         {k:v for k,v in again.items() if k!='witnessEdges'})
        self.assertEqual(again['witnessEdges'][0]['path'], '$.services.root.depends_on[0]')
        for source,target in [('root','isolated'),('isolated','root'),('db.root','root')]:
            no = query(model, source, target)
            self.assertFalse(no['reachable'])
            self.assertIsNone(no['distance'])
            self.assertEqual(no['witnessPath'], [])
            self.assertEqual(no['witnessEdges'], [])
        model['services']['root']['depends_on'].append('db.root')
        self.assertEqual(query(model, 'root', 'db.root')['witnessPath'], ['root','db.root'])

    def test_optional_conditions_endpoint_cycles_same_service_and_absent_targets(self):
        model = {'services': {'a.b': {'depends_on': {'c-d': {'condition':'service_completed_successfully', 'required':False},
                                                   'a.b': {'condition':'service_started', 'required':True}}},
            'c-d': {'depends_on': {'a.b': {'condition':'service_healthy', 'restart':False}}},
            'incoming': {'depends_on':['c-d']}, 'u': {'depends_on':['v']},
            'v': {'depends_on':['u']}, 'alone': {}, 'dangling': {'depends_on':['absent']}}}
        loaded = load_document(json.dumps(model), format='json')
        report = query(loaded, 'a.b', 'c-d')
        self.assertEqual(report['witnessPath'], ['a.b','c-d'])
        self.assertEqual(report['sourceCycle'], dict(participates=True,selfCycle=True,services=['a.b','c-d']))
        self.assertEqual(report['targetCycle'], dict(participates=True,selfCycle=False,services=['a.b','c-d']))
        edge = report['witnessEdges'][0]
        self.assertEqual(edge['condition'], 'service_completed_successfully')
        self.assertFalse(edge['required'])
        self.assertTrue(edge['requiredDeclared'])
        self.assertEqual(edge['path'], '$.services["a.b"].depends_on["c-d"]')
        self.assertEqual([p['path'] for p in edge['fieldLocations']],
            [edge['path'],edge['path']+'.condition',edge['path']+'.required'])
        reverse = query(loaded, 'c-d', 'a.b')['witnessEdges'][0]
        self.assertEqual(reverse['condition'], 'service_healthy')
        self.assertTrue(reverse['required'])
        self.assertFalse(reverse['requiredDeclared'])
        self.assertIs(reverse['restart'], False)
        incoming = query(loaded, 'incoming', 'a.b')
        self.assertFalse(incoming['sourceCycle']['participates'])
        self.assertEqual(incoming['targetCycle'], report['sourceCycle'])
        disjoint = query(loaded, 'a.b', 'u')
        self.assertFalse(disjoint['reachable'])
        self.assertEqual(disjoint['targetCycle']['services'], ['u','v'])
        for name in model['services']:
            same = query(loaded, name, name)
            self.assertTrue(same['reachable'])
            self.assertEqual(same['distance'], 0)
            self.assertEqual(same['witnessPath'], [name])
            self.assertEqual(same['witnessEdges'], [])
            self.assertEqual(same['sourceCycle'], same['targetCycle'])
        self.assertFalse(query(loaded, 'dangling', 'alone')['reachable'])
        for source,target in [('dangling','absent'),('absent','alone')]:
            with self.assertRaises(InputError) as ctx: query(loaded, source, target)
            self.assertEqual(ctx.exception.detail['code'], 'unknown-service')

    def test_maximum_chain_and_cycle_terminates(self):
        model = {'services': {f's{i:03}': {'depends_on':[f's{i+1:03}']} for i in range(499)}}
        model['services']['s499'] = {}
        path = query(model, 's000', 's499')
        self.assertEqual(path['distance'], 499)
        self.assertEqual(path['witnessPath'], [f's{i:03}' for i in range(500)])
        self.assertFalse(query(model, 's499', 's000')['reachable'])
        model['services']['s499']['depends_on'] = ['s000']
        cycle = query(model, 's499', 's498')
        self.assertEqual(cycle['distance'], 499)
        self.assertEqual(cycle['sourceCycle']['services'], path['witnessPath'])
        self.assertEqual(cycle['sourceCycle'], cycle['targetCycle'])

    def test_conflicts_unknown_endpoints_arity_and_malformed_inputs(self):
        conflicts = [['--impact-service','db'],['--provider','podman-compose'],['--provider-version','1.6.0'],
            ['--compare-profiles','/must-not-read/left','/must-not-read/right'],['--explain'],
            *[['--fail-on', x] for x in ('error','warning','never')],['--fail-on=never']]
        for flags in conflicts:
            result = run_cli('/must-not-read/compose', 'web', 'db', '--json', *flags)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'invalid-arguments')
        path = ROOT/'examples/podman_audit/impact-chain.yaml'
        for source,target in [('', 'db'),('web',''),('WEB','db'),('web','DB'),('web',' db'),('missing','missing')]:
            result = run_cli(path, source, target, '--json')
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report['error']['code'], 'unknown-service')
            self.assertEqual(report['assessment'], 'not-evaluated')
            self.assertNotIn('reachable', report)
        human = run_cli(path, 'missing', 'db')
        self.assertEqual(human.returncode, 2)
        self.assertEqual(human.stdout, '')
        self.assertIn('unknown-service', human.stderr)
        for selectors in ([],['web'],['web','db','extra']):
            result = subprocess.run([*CLI,str(path),'--json','--dependency-path',*selectors],
                                    capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'invalid-arguments')
        missing = run_cli('/no-such-file/compose.yaml','web','db','--json')
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn('reachable', json.loads(missing.stdout))
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp)/'bad.yaml'
            for text in ('services: {a: {}, b: {depends_on: {x: {condition: bogus}}}}',
                         'services: {a: {}, b: {depends_on: {x: {condition: service_started, required: 0}}}}',
                         'services: {a: {}, b: {healthcheck: {disable: wrong}}}',
                         'services: {a: {}, b: {profiles: [dev]}}','services: {a: {}, a: {}}',
                         'services: {a: {}}\n---\nservices: {a: {}}',
                         'services: {a: {image: "${UNRESOLVED}"}}','services: [a]'):
                bad.write_text(text)
                result = run_cli(bad, 'a', 'a', '--json')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['assessment'], 'not-evaluated')
                self.assertNotIn('reachable', report)
            bad = Path(temp)/'bad.json'
            bad.write_text('{"services":{"a":{},"b":{"depends_on":{"a":{"condition":"bogus"}}}}}')
            self.assertEqual(run_cli(bad, 'a', 'a', '--json').returncode, 2)
        with self.assertRaises(InputError) as ctx:
            query({'services':{'a':{},'b':{'healthcheck':{'disable':'wrong'}}}}, 'missing', 'missing')
        self.assertEqual(ctx.exception.detail['code'], 'invalid-input')

    def test_human_json_equivalence_no_execution_no_catalog_and_source_preservation(self):
        from stackwrench.podman_audit.cli import main
        with tempfile.TemporaryDirectory() as temp:
            sentinel = Path(temp)/'MUST_NOT_EXIST'
            model = {'services': {'db': {'healthcheck': {'test':['CMD-SHELL',f'touch {sentinel}'],'disable':True}},
                'web-ui': {'command': f'touch {sentinel}', 'depends_on': {'db': {'condition':'service_healthy','required':False}}}}}
            path = Path(temp)/'compose.json'
            path.write_text(json.dumps(model))
            before = path.read_bytes()
            for source,target in [('web-ui','db'),('db','web-ui'),('web-ui','web-ui')]:
                reports = []
                with patch('stackwrench.podman_audit.audit._catalog_bytes',side_effect=AssertionError('catalog used')), \
                     patch('stackwrench.podman_audit.cli.audit',side_effect=AssertionError('audit used')), \
                     patch('stackwrench.podman_audit.cli.compare_profiles',side_effect=AssertionError('compare used')), \
                     patch('subprocess.run',side_effect=AssertionError('subprocess used')):
                    for flags in (['--json'],[]):
                        stdout,stderr = io.StringIO(),io.StringIO()
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            self.assertEqual(main([str(path),'--dependency-path',source,target,*flags]),0)
                        self.assertEqual(stderr.getvalue(),'')
                        self.assertNotIn(str(sentinel),stdout.getvalue())
                        reports.append(stdout.getvalue())
                report = json.loads(reports[0])
                for label,key in [('Source service','sourceService'),('Target service','targetService'),
                    ('Source declaration','sourceServiceLocation'),('Target declaration','targetServiceLocation'),
                    ('Source cycle','sourceCycle'),('Target cycle','targetCycle'),('Reachable','reachable'),
                    ('Distance','distance'),('Witness path','witnessPath'),('Witness edges','witnessEdges')]:
                    line = next(line for line in reports[1].splitlines() if line.startswith(label+': '))
                    self.assertEqual(json.loads(line.removeprefix(label+': ')), report[key])
                for key in ('provider','providerVersion','findings','summary','catalogVersion','compatibility'):
                    self.assertNotIn(key, report)
                self.assertIn('not predicted startup order', reports[1])
                self.assertEqual(path.read_bytes(),before)
                self.assertFalse(sentinel.exists())


if __name__ == '__main__':
    unittest.main()
