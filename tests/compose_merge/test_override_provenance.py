"""Explicit nested tags keep their own sources during ancestor replacement."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.compose_merge import compare_files
from stackwrench.compose_merge.core import MergeError, Project, parse

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, '-m', 'stackwrench', 'compose-merge']
EXAMPLE = ROOT / 'examples/compose_merge/override-provenance.yaml'
TEXT = EXAMPLE.read_text()
EXPECTED = {'x-data': {'nested': {'keep': 'live'}}}


def source(line, column, file='override.yaml', index=1):
    return dict(file=file, line=line, column=column, input=index)


class NestedOverrideProvenanceTests(unittest.TestCase):
    def test_nested_override_reset_previous_sources_and_readd(self):
        for base in [None, 'x-data: null',
                     'x-data:\n  dropped: outer\n  nested:\n    gone: {leaf: old}\n'
                     '    keep: before\n    omitted: inner\n']:
            with self.subTest(base=base):
                project = Project()
                if base:
                    project.add(parse(base, 'base.yaml'))
                old_nested = project.explain('/x-data/nested') if base and 'gone' in base else None
                node = parse(TEXT, 'override.yaml', 1)
                original = copy.deepcopy(node)
                project.add(node)
                self.assertEqual(project.render(), EXPECTED)
                for path, location in [('/x-data', source(1, 9)),
                                       ('/x-data/nested', source(2, 11)),
                                       ('/x-data/nested/keep', source(2, 11))]:
                    event = project.explain(path)['events'][-1]
                    self.assertEqual(event['action'], 'override')
                    self.assertEqual(event['source'], location)
                info = project.explain('/x-data/nested/gone')
                self.assertFalse(info['present'])
                self.assertEqual(info['winningSources'], [])
                event = info['events'][-1]
                self.assertEqual(event['action'], 'reset')
                self.assertEqual(event['source'], source(3, 11))
                self.assertEqual(event['previousPresent'], old_nested is not None)
                if old_nested:
                    inner_event = project.explain('/x-data/nested')['events'][-1]
                    self.assertEqual(inner_event['previous'], old_nested['value'])
                    self.assertEqual(inner_event['previousSources'], old_nested['winningSources'])
                    self.assertEqual(event['previous'], {'leaf': 'old'})
                    self.assertEqual(event['previousSources'], [source(4, 11, 'base.yaml', 0),
                                                               source(4, 18, 'base.yaml', 0)])
                    for path, action, location in [
                        ('/x-data/dropped', 'override', source(1, 9)),
                        ('/x-data/nested/omitted', 'override', source(2, 11)),
                        ('/x-data/nested/gone/leaf', 'reset', source(3, 11)),
                    ]:
                        info = project.explain(path)
                        self.assertFalse(info['present'])
                        self.assertEqual(info['events'][-1]['action'], action)
                        self.assertEqual(info['events'][-1]['source'], location)
                self.assertEqual(project.explain('/x-data/nested/keep')['winningSources'],
                                 [source(4, 11)])
                with self.assertRaisesRegex(MergeError, 'path has no history'):
                    project.explain('/x-data/nested/never')
                project.add(parse('x-data: {nested: {gone: again}}', 'last.yaml', 2))
                self.assertEqual([e['action'] for e in project.explain('/x-data/nested/gone')['events']][-2:],
                                 ['reset', 'add'])
                self.assertEqual(node, original)

    def test_nested_override_shapes_escaped_paths_and_nearest_tag(self):
        for payload, expected in [('null', None), ('[]', []), ('{}', {}),
                                  ('[one, two]', ['one', 'two']), ('live', 'live'),
                                  ('{keep: live, gone: !reset null}', {'keep': 'live'})]:
            with self.subTest(payload=payload):
                text = ('x-data: !override\n  wrapper:\n    a/b~c: !override ' + payload +
                        '\n  plain: {keep: live}\n')
                node = parse(text, 'override.yaml', 1)
                original = copy.deepcopy(node)
                project = Project().add(parse('x-data: {wrapper: {a/b~c: {old: stale}}}', 'base.yaml'))
                project.add(node)
                self.assertEqual(project.render(), {'x-data': {
                    'wrapper': {'a/b~c': expected}, 'plain': {'keep': 'live'},
                }})
                for path, location in [('/x-data/wrapper', source(1, 9)),
                                       ('/x-data/wrapper/a~1b~0c', source(3, 12)),
                                       ('/x-data/wrapper/a~1b~0c/old', source(3, 12)),
                                       ('/x-data/plain/keep', source(1, 9))]:
                    event = project.explain(path)['events'][-1]
                    self.assertEqual(event['action'], 'override')
                    self.assertEqual(event['source'], location)
                if isinstance(expected, list) and expected:
                    self.assertEqual(project.explain('/x-data/wrapper/a~1b~0c/0')['events'][-1]['source'],
                                     source(3, 12))
                self.assertEqual(node, original)
        node = parse('x-data: !override\n  nested: !override\n    deeper: !override live\n', 'override.yaml', 1)
        project = Project().add(node)
        self.assertEqual(project.explain('/x-data/nested/deeper')['events'][-1]['source'], source(3, 13))

    def test_compare_operations_retain_inner_override_source(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = [Path(directory) / name for name in ('override.yaml', 'later.yaml')]
            a.write_text(TEXT)
            b.write_text('x-data: {nested: !override {keep: later}}')
            result = compare_files([a, b], [b, a])
            change, = result['differences']
            self.assertEqual(change['path'], '/x-data/nested/keep')
            self.assertEqual(change['left']['value'], 'later')
            self.assertEqual(change['right']['value'], 'live')
            operation = change['right']['operations'][-1]
            self.assertEqual(operation, {'action': 'override', 'source': source(2, 11, str(a), 1)})

    def test_installed_cli_example_and_negative_gates(self):
        def call(command, file=EXAMPLE, path=None):
            args = [*CLI, command, '-f', str(file)]
            if path is not None:
                args += ['--path', path]
            return subprocess.run(args, capture_output=True, text=True, timeout=15)

        result = call('render')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), EXPECTED)
        for path, action, line, column, present in [
            ('/x-data', 'override', 1, 9, True),
            ('/x-data/nested', 'override', 2, 11, True),
            ('/x-data/nested/keep', 'override', 2, 11, True),
            ('/x-data/nested/gone', 'reset', 3, 11, False),
        ]:
            result = call('explain', path=path)
            self.assertEqual(result.returncode, 0, result.stderr)
            info = json.loads(result.stdout)
            self.assertEqual(info['present'], present)
            event, = info['events']
            self.assertEqual(event['action'], action)
            self.assertEqual(event['source'], source(line, column, str(EXAMPLE), 0))
        result = call('explain', path='/x-data/nested/never')
        self.assertEqual((result.returncode, result.stdout), (2, ''))
        self.assertIn('path has no history', result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / 'invalid.yaml'
            for value, message in [('!unknown null', 'unsupported YAML tag'),
                                   ('!reset [nonempty]', 'payload'),
                                   ('[!override live]', 'mapping values'),
                                   ('"${HOST}"', 'interpolation')]:
                invalid.write_text('x-data: !override\n  nested: !override\n    gone: ' + value + '\n')
                for command in ['render', 'explain']:
                    with self.subTest(value=value, command=command):
                        result = call(command, invalid, '/x-data/nested/gone' if command == 'explain' else None)
                        self.assertEqual((result.returncode, result.stdout), (2, ''))
                        self.assertIn(message, result.stderr)
