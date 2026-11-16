import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.compose_merge.core import MergeError, Project, merge_files, parse

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / 'tests/compose_merge/fixtures.json').read_text())
CLI = [sys.executable, '-m', 'stackwrench', 'compose-merge']


def replay(case):
    p = Project()
    for i, text in enumerate(case['files']):
        p.add(parse(text, f'{case["name"]}-{i}.yaml', i))
    return p


class GoldenTests(unittest.TestCase):
    def test_all_golden_trees_and_history(self):
        for case in CASES:
            with self.subTest(case=case['name']):
                p = replay(case)
                self.assertEqual(p.render(), case['expected'])
                info = p.explain(case['path'])
                self.assertEqual(info['events'][-1]['action'], case['action'])
                self.assertEqual(info['events'][-1]['source']['input'], case['winner'])
                self.assertGreater(info['events'][-1]['source']['line'], 0)
                if case['action'] == 'reset':
                    self.assertFalse(info['present'])
                    self.assertTrue(info['events'][-1]['previousPresent'])
                else:
                    self.assertTrue(info['present'])
                    self.assertIn(case['winner'], [s['input'] for s in info['winningSources']])
                if case['action'] == 'replace':
                    self.assertTrue(info['events'][-1]['previousPresent'])
                    self.assertEqual(info['events'][-1]['previousSources'][0]['input'], 0)

    def test_exact_command_locations_and_previous_value(self):
        p = replay(next(c for c in CASES if c['name'] == 'command'))
        e = p.explain('/services/web/command')['events'][-1]
        self.assertEqual(e['source'], {'file':'command-1.yaml','line':4,'column':14,'input':1})
        self.assertEqual(e['previousSources'][0]['line'], 4)
        self.assertEqual(e['previous'], ['echo', 'old'])
        self.assertEqual(e['value'], ['echo', 'new'])

    def test_every_output_pointer_has_history(self):
        def walk(value, path=''):
            yield path
            entries = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else []
            for k, v in entries:
                yield from walk(v, path + '/' + str(k).replace('~','~0').replace('/','~1'))
        for case in CASES:
            p = replay(case)
            for path in walk(p.render()):
                with self.subTest(case=case['name'], path=path):
                    self.assertTrue(p.explain(path)['present'])

    def test_three_files_and_removed_descendants(self):
        p = Project().add(parse('x-data: {a: [one, two], b: old}', 'a', 0))
        p.add(parse('x-data: !override {a: [new]}', 'b', 1))
        p.add(parse('x-data: {a: [last]}', 'c', 2))
        self.assertEqual(p.render(), {'x-data':{'a':['new','last']}})
        self.assertEqual([e['action'] for e in p.explain('/x-data/a')['events']], ['add','override','append'])
        self.assertFalse(p.explain('/x-data/b')['present'])
        e = p.explain('/x-data/a/1')['events']
        self.assertEqual([v['present'] for v in e], [True, False, True])

    def test_nested_tags_first_file_and_override(self):
        p = Project().add(parse('x-data: {a: !reset null, b: !override [one]}','a'))
        self.assertEqual(p.render(), {'x-data':{'b':['one']}})
        p.add(parse('x-data: !override {c: !reset null, d: new}', 'b', 1))
        self.assertEqual(p.render(), {'x-data':{'d':'new'}})

    def test_read_results_do_not_mutate_project(self):
        p = replay(CASES[0])
        plain = p.render()
        plain.clear()
        info = p.explain('/services/web/labels/change')
        info['events'].clear()
        self.assertEqual(p.render(), CASES[0]['expected'])
        self.assertEqual(len(p.explain('/services/web/labels/change')['events']), 2)

    def test_source_node_not_modified_by_merge(self):
        a = parse('services: {web: {ports: ["8080:80"]}}','a')
        b = parse('services: {web: {ports: ["8443:443"]}}','b',1)
        before = copy.deepcopy((a,b))
        Project().add(a).add(b)
        self.assertEqual((a,b), before)


class RejectionTests(unittest.TestCase):
    def test_unsupported_and_invalid_yaml(self):
        bad = [
            ('', 'empty'), ('[]','dict'), ('a: 1\na: 2','duplicate'),
            ('services: {web: {image: first, image: second}}','duplicate'),
            ('services: [bad]','dict'), ('services: {web: {include: other}}','unsupported'),
            ('include: other.yaml','unsupported'), ('services: {web: {extends: base}}','unsupported'),
            ('services: {web: {env_file: secret.env}}','unsupported'),
            ('services: {web: {build: .}}','unsupported'),
            ('services: {web: {image: "${IMAGE}"}}','interpolation'),
            ('services: {web: {command: "echo $$USER"}}','interpolation'),
            ('x-data: {"$KEY": value}','interpolation'),
            ('x-data: &x [one]','anchors'), ('x-data: *other','alias'),
            ('x-data: {<<: {a: b}}','mapping keys'),
            ('%YAML 1.1\n---\nx-data: ok','directives'),
            ('x-data: !unknown value','tag'), ('x-data: !!binary SGVsbG8=','scalar tag'),
            ('x-data: !!python/object/apply:os.system [echo]','tag'),
            ('x-data: !!int 1:20','sexagesimal'), ('x-data: !!bool yes','true or false'),
            ('x-data: \"\\uD800\"','Unicode scalar'), ('x-data: {\"\\uD800\": value}','Unicode mapping'),
            ('x-data: .nan','non-finite'), ('x-data: .inf','non-finite'),
            ('x-data: [!reset null]','mapping values'),
            ('x-data: [{a: !reset null}]','mapping values'),
            ('!override {services: {}}','mapping values'),
            ('services: {web: {dns: !reset [1.1.1.1]}}','payload'),
            ('services: {web: {image: !reset []}}','payload'),
            ('services: {web: {ports: !override nope}}','list'),
            ('services: {web: {environment: {A: null}}}','explicit strings'),
            ('services: {web: {environment: [A=one]}}','dict'),
            ('services: {web: {labels: {key: true}}}','explicit strings'),
            ('services: {web: {ports: ["8000-8005:80"]}}','single numeric'),
            ('services: {web: {ports: ["[::1]:8080:80"]}}','IPv6'),
            ('services: {web: {ports: ["localhost:8080:80"]}}','literal IP'),
            ('services: {web: {ports: ["8080:80/quic"]}}','protocol'),
            ('services: {web: {ports: [0]}}','outside'),
            ('services: {web: {ports: [true]}}','dict'),
            ('services: {web: {ports: [{published: 80}]}}','requires target'),
            ('services: {web: {ports: [{target: 80, mystery: true}]}}','unsupported'),
            ('services: {web: {ports: [80, "80"]}}','duplicate'),
            ('services: {web: {volumes: ["~/data:/data"]}}','home-expanding'),
            ("services: {web: {volumes: ['C:\\data:/data']}}",'Windows'),
            ('services: {web: {volumes: ["foo:/data:ro,z"]}}','ro/rw'),
            ('services: {web: {volumes: ["foo:data"]}}','absolute POSIX'),
            ('services: {web: {volumes: ["foo:/a/../data"]}}','absolute POSIX'),
            ('services: {web: {volumes: [{type: tmpfs, target: /tmp}]}}','only volume'),
            ('services: {web: {volumes: [{type: bind, target: /tmp}]}}','requires source'),
            ('services: {web: {secrets: [{target: token}]}}','requires source'),
            ('services: {web: {configs: [{source: a, mode: 1024}]}}','mode'),
            ('services: {web: {healthcheck: {test: [echo, bad]}}}','must start'),
            ('services: {web: {healthcheck: {retries: -1}}}','nonnegative'),
            ('services: {web: {tty: yes}}','bool'),
            ('services: {web: {ports: ["8080:80"]}}\n---\nservices: {}','single document'),
            ('services: {web: {image: [broken}}','invalid YAML'),
            ('x-data: {1: value}','mapping keys'),
        ]
        for text, message in bad:
            with self.subTest(text=text):
                with self.assertRaisesRegex(MergeError, message):
                    parse(text, 'bad.yaml')

    def test_unsupported_merges(self):
        for a,b,message in [
            ('x-data: [a]', 'x-data: {a: b}', 'type change'),
            ('services: {web: {dns: [a]}}','services: {web: {dns: [a]}}','identical'),
            ('services: {web: {ports: [{target: 80, name: old}]}}','services: {web: {ports: [80]}}','omits'),
            ('services: {web: {volumes: ["cache:/data"]}}','services: {web: {volumes: ["./dir:/data"]}}','mount type'),
        ]:
            with self.subTest(a=a,b=b):
                with self.assertRaisesRegex(MergeError, message):
                    Project().add(parse(a,'a')).add(parse(b,'b',1))

    def test_pointer_failure(self):
        p = replay(CASES[0])
        for pointer in ['services/web','/missing','/bad~2escape','/services/web/labels/change/0']:
            with self.subTest(pointer=pointer), self.assertRaises(MergeError):
                p.explain(pointer)

    def test_limits(self):
        with self.assertRaisesRegex(MergeError,'exceeds'):
            parse('x-data: '+ 'a' * (1024*1024), 'huge')
        with self.assertRaisesRegex(MergeError,'depth'):
            parse('x-data: ' + '['*70+'0'+']'*70, 'deep')
        with self.assertRaisesRegex(MergeError,'1..32'):
            merge_files([])


class CliTests(unittest.TestCase):
    def call(self, *args, **kwargs):
        return subprocess.run([*CLI, *args], cwd=ROOT, text=True, capture_output=True, timeout=15, **kwargs)

    def test_acceptance_commands_and_input_immutability(self):
        paths = [ROOT/'examples/compose_merge/base.yaml',ROOT/'examples/compose_merge/override.yaml']
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        args = ['-f','examples/compose_merge/base.yaml','-f','examples/compose_merge/override.yaml']
        for _ in range(2):
            r = self.call('render', *args)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads(r.stdout),json.loads((ROOT/'examples/compose_merge/merged.json').read_text()))
        r = self.call('explain', *args, '--path','/services/web/command')
        self.assertEqual(r.returncode,0,r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data['value'], ['echo','new'])
        self.assertEqual(data['events'][-1]['source']['line'],3)
        self.assertEqual(data['events'][-1]['previousSources'][0]['line'],5)
        self.assertEqual(before,[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_all_fixture_cli_render_and_explain(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            for case in CASES:
                files=[]
                for i,text in enumerate(case['files']):
                    p=Path(d)/f'{i}.yaml'
                    p.write_text(text)
                    files += ['-f',str(p)]
                r=self.call('render',*files)
                self.assertEqual(r.returncode,0,(case['name'],r.stderr))
                self.assertEqual(json.loads(r.stdout),case['expected'],case['name'])
                r=self.call('explain',*files,'--path',case['path'])
                self.assertEqual(r.returncode,0,(case['name'],r.stderr))
                self.assertEqual(json.loads(r.stdout)['events'][-1]['action'],case['action'])

    def test_tags_example_cli(self):
        r=self.call('render','-f','examples/compose_merge/base.yaml','-f','examples/compose_merge/override.yaml','-f','examples/compose_merge/tags.yaml')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(json.loads(r.stdout),json.loads((ROOT/'examples/compose_merge/tagged.json').read_text()))

    def test_failure_stdout_empty(self):
        for args in [
            ['render','-f','examples/compose_merge/unsupported.yaml'],
            ['render','-f','missing-file.yaml'],
            ['render'], ['explain','-f','examples/compose_merge/base.yaml','--path','/missing'],
            ['explain','-f','examples/compose_merge/base.yaml','--path','bad'],
        ]:
            with self.subTest(args=args):
                r=self.call(*args)
                self.assertEqual(r.returncode,2)
                self.assertEqual(r.stdout,'')
                self.assertIn('error',r.stderr)

    def test_second_input_error_never_emits_partial_result(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            bad=Path(d)/'bad.yaml'
            for data in [b'services: [', b'\xff', b'x-data: \x00', b'x-data: '+b'a'*(1024*1024)]:
                bad.write_bytes(data)
                r=self.call('render','-f','examples/compose_merge/base.yaml','-f',str(bad))
                self.assertEqual(r.returncode,2)
                self.assertEqual(r.stdout,'')
                self.assertNotIn('Traceback',r.stderr)

    def test_no_environment_interpolation(self):
        r=self.call('render','-f','examples/compose_merge/unsupported.yaml',env={**os.environ,'IMAGE':'should-not-matter'})
        self.assertEqual(r.returncode,2)
        self.assertEqual(r.stdout,'')

    def test_data_is_not_executed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            marker=Path(d)/'MUST_NOT_EXIST'
            f=Path(d)/'input.yaml'
            f.write_text('services:\n  web:\n    command: '+json.dumps(f'touch {marker}')+'\n')
            r=self.call('render','-f',str(f))
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
