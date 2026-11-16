"""History discovery preserves exact event paths and first-recorded order."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.compose_merge import MergeError, Project, merge_files
from stackwrench.compose_merge.core import parse


class HistoryPathsTests(unittest.TestCase):
    def assert_explainable(self, project, paths):
        infos = [project.explain(path) for path in paths]
        self.assertEqual([info['path'] for info in infos], paths)
        for start in range(0, len(paths), 64):
            self.assertEqual(project.explain_many(paths[start:start + 64]),
                             {'explanations': infos[start:start + 64]})
        return infos

    def test_public_merge_files_removed_nul_example(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / name for name in ['base.yaml', 'next.yaml']]
            texts = ['x-data: {"\\0": old}\n', 'x-data: !override []\n']
            for file, text in zip(files, texts):
                file.write_text(text, encoding='utf-8')
            project = merge_files(files)
            paths = project.history_paths()
            self.assertEqual(paths, ['/x-data/\0', '/x-data', ''])
            infos = self.assert_explainable(project, paths)
            self.assertFalse(infos[0]['present'])
            self.assertEqual(infos[0]['winningSources'], [])
            self.assertEqual([e['action'] for e in infos[0]['events']], ['add', 'override'])
            self.assertEqual(infos[0]['events'][-1]['previous'], 'old')
            self.assertEqual(infos[0]['events'][-1]['previousSources'],
                             [dict(file=str(files[0]), line=1, column=16, input=0)])
            self.assertEqual([file.read_text(encoding='utf-8') for file in files], texts)

    def test_untouched_project_and_explicit_empty_mapping(self):
        project = Project()
        self.assertEqual(project.history_paths(), [])
        project.history_paths().append('/invented')
        self.assertEqual(project.history_paths(), [])
        with self.assertRaises(MergeError):
            project.explain('')
        project.add(parse('{}', 'empty.yaml'))
        self.assertEqual(project.history_paths(), [''])
        self.assertEqual(self.assert_explainable(project, [''])[0]['value'], {})
        with self.assertRaises(TypeError):
            project.history_paths('/selector')

    def test_first_event_order_repeated_history_removal_readd_and_later_paths(self):
        texts = ['x-data: {z: old, a: old}', 'x-data: {z: changed, a: old}',
                 'x-data: !override {}', 'x-data: {a: restored, later: new}']
        nodes = [parse(text, str(i) + '.yaml', i) for i, text in enumerate(texts)]
        originals = copy.deepcopy(nodes)
        project = Project()
        expected = ['/x-data/z', '/x-data/a', '/x-data', '']
        for i, node in enumerate(nodes):
            project.add(node)
            paths = project.history_paths()
            self.assertEqual(paths, expected + (['/x-data/later'] if i == 3 else []))
            self.assert_explainable(project, paths)
        self.assertFalse(project.explain('/x-data/z')['present'])
        self.assertEqual(project.explain('/x-data/a')['value'], 'restored')
        self.assertEqual(nodes, originals)
        self.assertNotIn('/never-seen', project.history_paths())
        with self.assertRaises(MergeError):
            project.explain('/never-seen')

    def test_exact_escaped_nul_unicode_empty_and_numeric_tokens(self):
        values = {'a/b~c': {'': {'雪😀\0': ['old', None]}}, '~1': 'tilde-one',
                  '\0': 'nul', r'\u0000': 'literal', '': 'empty',
                  '0': 'zero', '00': 'double-zero', '١': 'unicode-digit'}
        node = parse('x-data: ' + json.dumps(values, ensure_ascii=False), 'tokens.yaml')
        original = copy.deepcopy(node)
        project = Project().add(node)
        expected = ['/x-data/a~1b~0c//雪😀\0', '/x-data/a~1b~0c//雪😀\0/0',
                    '/x-data/a~1b~0c//雪😀\0/1', '/x-data/a~1b~0c/', '/x-data/a~1b~0c',
                    '/x-data/~01', '/x-data/\0', r'/x-data/\u0000', '/x-data/',
                    '/x-data/0', '/x-data/00', '/x-data/١', '/x-data', '']
        self.assertEqual(project.history_paths(), expected)
        self.assert_explainable(project, expected)
        self.assertEqual(node, original)

    def test_long_numeric_tokens_survive_mapping_list_replacement(self):
        limit = sys.get_int_max_str_digits()
        keys = ['9' * 4301, '0' * 4301, '0', '00', '1']
        mapping = '\n'.join('  ? "' + key + '"\n  : old' for key in keys)
        project = Project().add(parse('x-data:\n' + mapping, 'base.yaml'))
        paths = ['/x-data/' + key for key in keys] + ['/x-data', '']
        self.assertEqual(project.history_paths(), paths)
        self.assert_explainable(project, paths)
        project.add(parse('x-data: !override [new, null]', 'next.yaml', 1))
        self.assertEqual(project.history_paths(), paths)
        infos = self.assert_explainable(project, paths)
        self.assertEqual([i['present'] for i in infos[:5]], [False, False, True, False, True])
        project.add(parse('x-data: !override\n' + mapping, 'readd.yaml', 2))
        self.assertEqual(project.history_paths(), paths)
        self.assert_explainable(project, paths)
        self.assertEqual(sys.get_int_max_str_digits(), limit)

    def test_never_present_reset_is_discovered_and_batches_stay_bounded(self):
        project = Project().add(parse('x-data: !override {gone: !reset null}', 'reset.yaml'))
        self.assertEqual(project.history_paths(), ['/x-data', '/x-data/gone', ''])
        info = self.assert_explainable(project, project.history_paths())[1]
        self.assertFalse(info['present'])
        self.assertEqual(info['events'][0]['action'], 'reset')
        self.assertFalse(info['events'][0]['previousPresent'])
        project = Project().add(parse('x-data: ' + json.dumps({str(i): i for i in range(130)}),
                                      'many.yaml'))
        expected = ['/x-data/' + str(i) for i in range(130)] + ['/x-data', '']
        self.assertEqual(project.history_paths(), expected)
        self.assertEqual([len(expected[i:i + 64]) for i in range(0, len(expected), 64)], [64, 64, 4])
        self.assert_explainable(project, expected)
        for invalid in [[], expected[:65]]:
            with self.assertRaises(MergeError):
                project.explain_many(invalid)

    def test_detached_snapshots_no_io_decoding_lookup_or_project_mutation(self):
        project = Project().add(parse('x-data: {z: old}', 'base.yaml'))
        before = copy.deepcopy(project.__dict__)
        expected = ['/x-data/z', '/x-data', '']
        with patch('builtins.open', side_effect=AssertionError('unexpected I/O')), \
             patch('pathlib.Path.open', side_effect=AssertionError('unexpected I/O')), \
             patch('json.loads', side_effect=AssertionError('unexpected decoding')), \
             patch.object(Project, 'explain', side_effect=AssertionError('unexpected lookup')), \
             patch.object(Project, 'render', side_effect=AssertionError('unexpected render')), \
             patch.object(Project, 'merge', side_effect=AssertionError('unexpected merge')):
            first, second = project.history_paths(), project.history_paths()
        self.assertEqual(first, expected)
        self.assertIsNot(first, second)
        first.reverse()
        first.append('/invented')
        first[0] = '/changed'
        self.assertEqual(second, expected)
        self.assertEqual(project.history_paths(), expected)
        self.assertEqual(project.__dict__, before)
        project.add(parse('x-data: {later: new}', 'next.yaml', 1))
        self.assertEqual(second, expected)
        self.assertEqual(project.history_paths(), expected + ['/x-data/later'])


if __name__ == '__main__':
    unittest.main()
