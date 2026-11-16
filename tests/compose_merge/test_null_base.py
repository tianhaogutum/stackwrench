"""Regression for audit 056: null-base mappings must evaluate nested tags."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.compose_merge.core import Project, merge_files, parse

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, '-m', 'stackwrench', 'compose-merge']
FILES = [ROOT / 'examples/compose_merge/null-base.yaml', ROOT / 'examples/compose_merge/null-next.yaml']
EXPECTED = json.loads((ROOT / 'examples/compose_merge/null-merged.json').read_text())


class NullBaseTests(unittest.TestCase):
    def test_audited_tree_and_exact_reset_sources(self):
        project = merge_files(FILES)
        self.assertEqual(project.render(), EXPECTED)
        for path, line in [('/volumes/cache/labels/DROP', 4), ('/x-data/gone', 7)]:
            with self.subTest(path=path):
                info = project.explain(path)
                self.assertFalse(info['present'])
                self.assertIsNone(info['value'])
                self.assertEqual(info['winningSources'], [])
                self.assertEqual(len(info['events']), 1)
                event = info['events'][0]
                self.assertEqual(event['action'], 'reset')
                self.assertFalse(event['previousPresent'])
                self.assertFalse(event['present'])
                self.assertEqual(event['source'], {
                    'file': str(FILES[1]), 'line': line,
                    'column': 13 if line == 4 else 9, 'input': 1,
                })
        for path, base_line, next_line in [('/volumes/cache', 2, 3), ('/x-data', 3, 7)]:
            info = project.explain(path)
            self.assertEqual([e['action'] for e in info['events']], ['add', 'replace'])
            event = info['events'][-1]
            self.assertTrue(event['previousPresent'])
            self.assertIsNone(event['previous'])
            self.assertEqual(event['previousSources'][0]['file'], str(FILES[0]))
            self.assertEqual(event['previousSources'][0]['line'], base_line)
            self.assertEqual(event['source']['line'], next_line)
            self.assertEqual(event['value'], info['value'])
            self.assertTrue(all(s['input'] == 1 for s in info['winningSources']))
        keep = project.explain('/volumes/cache/labels/KEEP')
        self.assertEqual(keep['value'], 'yes')
        self.assertEqual(keep['winningSources'], [{
            'file': str(FILES[1]), 'line': 5, 'column': 13, 'input': 1,
        }])

    def test_nested_tags_readd_and_input_immutability(self):
        nodes = [
            parse('x-data: null', 'base.yaml', 0),
            parse('x-data:\n  nested:\n    map: !reset {}\n    list: !reset []\n'
                  '    selected: !override [new]\n', 'next.yaml', 1),
            parse('x-data: {nested: {map: {added: live}, selected: [last]}}', 'last.yaml', 2),
        ]
        before = copy.deepcopy(nodes)
        project = Project().add(nodes[0]).add(nodes[1])
        self.assertEqual(project.render(), {'x-data': {'nested': {'selected': ['new']}}})
        for key, line in [('map', 3), ('list', 4)]:
            info = project.explain('/x-data/nested/' + key)
            self.assertFalse(info['present'])
            self.assertEqual(info['events'][0]['action'], 'reset')
            self.assertEqual(info['events'][0]['source']['line'], line)
        selected = project.explain('/x-data/nested/selected')
        self.assertEqual(selected['events'][0]['action'], 'override')
        self.assertEqual(selected['winningSources'][0]['line'], 5)
        project.add(nodes[2])
        self.assertEqual(project.render(), {'x-data': {'nested': {
            'map': {'added': 'live'}, 'selected': ['new', 'last'],
        }}})
        self.assertEqual([e['action'] for e in project.explain('/x-data/nested/map')['events']], ['reset', 'add'])
        self.assertEqual([e['action'] for e in project.explain('/x-data/nested/selected')['events']], ['override', 'append'])
        self.assertEqual(nodes, before)

    def test_null_retention_and_non_mapping_replacements(self):
        for text, expected, action in [
            ('x-data: null', None, 'retain'),
            ('x-data: {}', {}, 'replace'),
            ('x-data: [one]', ['one'], 'replace'),
            ('x-data: live', 'live', 'replace'),
        ]:
            with self.subTest(text=text):
                project = Project().add(parse('x-data: null', 'base.yaml'))
                project.add(parse(text, 'next.yaml', 1))
                self.assertEqual(project.render(), {'x-data': expected})
                self.assertEqual(project.explain('/x-data')['events'][-1]['action'], action)

    def test_installed_cli_render_and_removed_path(self):
        args = [arg for file in FILES for arg in ('-f', str(file))]
        result = subprocess.run([*CLI, 'render', *args], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), EXPECTED)
        project = merge_files(FILES)
        for path in ['/volumes/cache', '/volumes/cache/labels/DROP', '/volumes/cache/labels/KEEP', '/x-data/gone']:
            result = subprocess.run([*CLI, 'explain', *args, '--path', path], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), project.explain(path))

    def test_null_base_does_not_relax_input_or_path_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            later = Path(directory) / 'unsupported.yaml'
            for text, message in [
                ('x-data: {gone: !unknown null}', 'unsupported YAML tag'),
                ('x-data: {gone: "${HOST}"}', 'interpolation'),
                ('x-data: {gone: !reset [nonempty]}', 'payload'),
                ('x-data: [!reset null]', 'mapping values'),
            ]:
                later.write_text(text)
                for command in ['render', 'explain']:
                    with self.subTest(text=text, command=command):
                        args = [*CLI, command, '-f', str(FILES[0]), '-f', str(later)]
                        if command == 'explain':
                            args += ['--path', '/x-data/gone']
                        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
                        self.assertEqual(result.returncode, 2)
                        self.assertEqual(result.stdout, '')
                        self.assertIn(message, result.stderr)
            result = subprocess.run([
                *CLI, 'explain', '-f', str(FILES[0]), '-f', str(FILES[1]),
                '--path', '/x-data/never-defined',
            ], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('path has no history', result.stderr)
