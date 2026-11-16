"""Public Python batches retain singular histories and reject failures atomically."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.compose_merge import MergeError, Project, merge_files
from stackwrench.compose_merge.core import parse


class ExplainManyTests(unittest.TestCase):
    def test_public_installed_example(self):
        project = merge_files(['examples/compose_merge/nul-key.yaml'])
        paths = ['/x-data/\0', '']
        result = project.explain_many(paths)
        self.assertEqual(result, {'explanations': [project.explain(p) for p in paths]})
        info = result['explanations'][0]
        source = dict(file='examples/compose_merge/nul-key.yaml', line=1, column=16, input=0)
        self.assertEqual((info['path'], info['present'], info['value'], info['winningSources']),
                         (paths[0], True, 'old', [source]))
        self.assertEqual(info['events'], [dict(action='add', order=1, path=paths[0],
            present=True, previous=None, previousPresent=False, previousSources=[],
            reason='new declaration', source=source, value='old')])
        self.assertEqual(result['explanations'][1]['value'], {'x-data': {'\0': 'old'}})

    def test_exact_tokens_order_duplicates_and_detached_results_without_io(self):
        values = {'\0': 'nul', r'\u0000': 'literal', '"quoted"': 'quotes', '~1': 'tilde-one',
                  'a/b~c': {'': {'雪😀\0': ['old', None]}}, '': 'empty'}
        project = Project().add(parse('x-data: ' + json.dumps(values, ensure_ascii=False), 'tokens.yaml'))
        paths = ['/x-data/\0', r'/x-data/\u0000', '/x-data/"quoted"', '/x-data/~01',
                 '/x-data/a~1b~0c//雪😀\0/1', '/x-data/', '', '/x-data/\0', '']
        original_paths, original_project = paths[:], copy.deepcopy(project.__dict__)
        expected = {'explanations': [project.explain(p) for p in paths]}
        with patch('builtins.open', side_effect=AssertionError('unexpected I/O')), \
             patch('pathlib.Path.open', side_effect=AssertionError('unexpected I/O')), \
             patch('json.loads', side_effect=AssertionError('unexpected JSON decoding')):
            result = project.explain_many(paths)
        self.assertEqual(result, expected)
        self.assertEqual([i['value'] for i in result['explanations'][:6]],
                         ['nul', 'literal', 'quotes', 'tilde-one', None, 'empty'])
        result['explanations'][0]['events'][0]['source']['line'] = 999
        result['explanations'][6]['value']['x-data']['\0'] = 'changed'
        result['explanations'][6]['events'][0]['value']['x-data']['\0'] = 'changed'
        result['explanations'][6]['winningSources'][0]['line'] = 999
        self.assertEqual(result['explanations'][7], expected['explanations'][7])
        self.assertEqual(result['explanations'][8], expected['explanations'][8])
        self.assertEqual(project.__dict__, original_project)
        self.assertEqual(paths, original_paths)
        self.assertEqual(project.explain_many(paths), expected)

    def test_present_removed_readded_exact_provenance_and_node_immutability(self):
        texts = ['x-data: {"\\0": old}\n', 'x-data: !override []\n',
                 'x-data: !override {"\\0": !override restored}\n']
        nodes = [parse(text, str(i) + '.yaml', i) for i, text in enumerate(texts)]
        originals = copy.deepcopy(nodes)
        path, project, infos = '/x-data/\0', Project(), []
        for node in nodes:
            project.add(node)
            paths = [path, '', path, '/x-data']
            before = copy.deepcopy(project.__dict__)
            result = project.explain_many(paths)
            self.assertEqual(result, {'explanations': [project.explain(p) for p in paths]})
            self.assertEqual(project.__dict__, before)
            infos.append(result['explanations'][0])
        source = dict(file='0.yaml', line=1, column=16, input=0)
        self.assertEqual((infos[0]['value'], infos[0]['winningSources']), ('old', [source]))
        self.assertEqual((infos[1]['present'], infos[1]['value'], infos[1]['winningSources']), (False, None, []))
        removed = infos[1]['events'][-1]
        self.assertEqual((removed['previousPresent'], removed['previous'], removed['previousSources']),
                         (True, 'old', [source]))
        self.assertEqual(removed['source'], dict(file='1.yaml', line=1, column=9, input=1))
        self.assertEqual(infos[2]['events'][:2], infos[1]['events'])
        self.assertEqual([e['action'] for e in infos[2]['events']], ['add', 'override', 'override'])
        self.assertEqual((infos[2]['events'][-1]['previousPresent'], infos[2]['events'][-1]['previousSources']),
                         (False, []))
        self.assertEqual((infos[2]['value'], infos[2]['winningSources']),
                         ('restored', [dict(file='2.yaml', line=1, column=26, input=2)]))
        self.assertEqual(nodes, originals)

    def test_large_numeric_tokens_and_canonical_indexes_across_replacement(self):
        self.assertEqual(sys.get_int_max_str_digits(), 4300)
        keys = ['9' * 140000, '0' * 140000, '0', '00', '1']
        mapping = '\n'.join('  ? "' + key + '"\n  : old' for key in keys)
        texts = ['x-data:\n' + mapping, 'x-data: !override [new, null]',
                 'x-data: !override\n' + mapping.replace(': old', ': restored')]
        project, prior = Project(), None
        paths = ['/x-data/' + key for key in keys]
        for i, text in enumerate(texts):
            project.add(parse(text, str(i) + '.yaml', i))
            result = project.explain_many(paths)['explanations']
            self.assertEqual(result, [project.explain(p) for p in paths])
            for j, info in enumerate(result):
                if i == 1:
                    present = keys[j] in ['0', '1']
                    value = 'new' if keys[j] == '0' else None
                    self.assertEqual((info['present'], info['value']), (present, value))
                    self.assertEqual(info['events'][-1]['previousSources'],
                                     [dict(file='0.yaml', line=3 + j * 2, column=5, input=0)])
                if i == 2:
                    self.assertEqual(info['events'][:2], prior[j]['events'])
                    self.assertEqual((info['value'], info['winningSources']),
                        ('restored', [dict(file='2.yaml', line=3 + j * 2, column=5, input=2)]))
            prior = result
        self.assertEqual(sys.get_int_max_str_digits(), 4300)

    def test_concrete_list_shape_and_64_limit_before_lookups(self):
        project = merge_files(['examples/compose_merge/nul-key.yaml'])
        paths = [''] * 64
        self.assertEqual(project.explain_many(paths), {'explanations': [project.explain('')] * 64})
        self.assertEqual(paths, [''] * 64)
        class Unconsumable:
            def __iter__(self):
                raise AssertionError('arbitrary iterable consumed')
        iterator = iter([''])
        invalid = [[], [''] * 65, '', '/x-data', '[""]', {}, {'paths': ['']}, None,
                   False, 1, 1.5, b'', ('',), {''}, iterator, Unconsumable()]
        invalid += [['', v] for v in [None, False, 1, 1.5, [], {}, [''], b'']]
        before = copy.deepcopy(project.__dict__)
        for paths in invalid:
            with self.subTest(type=type(paths).__name__), \
                 patch.object(project, 'explain', side_effect=AssertionError('lookup before shape validation')):
                with self.assertRaises(MergeError):
                    project.explain_many(paths)
        self.assertEqual(next(iterator), '')
        self.assertEqual(project.__dict__, before)

    def test_bad_or_unseen_pointer_fails_atomically_without_mutation(self):
        project = merge_files(['examples/compose_merge/nul-key.yaml'])
        before = copy.deepcopy(project.__dict__)
        expected = project.explain('')
        for bad in ['x-data', '"/x-data"', '/x-data/~', '/x-data/~2', '/never-seen',
                    '/x-data/\ud800', '/x-data/00']:
            for paths in [[bad, ''], ['', bad], ['/x-data', bad, '']]:
                original = paths[:]
                with self.subTest(paths=paths), self.assertRaises(MergeError):
                    project.explain_many(paths)
                self.assertEqual(paths, original)
                self.assertEqual(project.__dict__, before)
        self.assertEqual(project.explain_many(['']), {'explanations': [expected]})
        with self.assertRaises(MergeError):
            Project().explain_many([''])

    def test_cli_batch_transports_match_public_api_and_share_its_failure(self):
        from stackwrench.compose_merge import cli
        paths = ['/x-data/\0', '', '/x-data/\0']
        project = merge_files(['examples/compose_merge/nul-key.yaml'])
        expected = project.explain_many(paths)
        with tempfile.TemporaryDirectory() as temp:
            query = Path(temp) / 'paths.json'
            query.write_text(json.dumps(paths))
            for args in [['--paths-json', json.dumps(paths)], ['--paths-json-file', str(query)],
                         ['--paths-json-stdin']]:
                with self.subTest(args=args), \
                     patch.object(cli, 'merge_files', return_value=project) as merged, \
                     patch.object(project, 'explain_many', wraps=project.explain_many) as batch, \
                     patch.object(cli.sys, 'stdin') as stdin, \
                     patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
                    stdin.buffer.read.return_value = json.dumps(paths).encode()
                    self.assertEqual(cli.main(['explain', '-f', 'examples/compose_merge/nul-key.yaml', *args]), 0)
                    self.assertEqual(json.loads(stdout.getvalue()), expected)
                    merged.assert_called_once_with(['examples/compose_merge/nul-key.yaml'])
                    batch.assert_called_once_with(paths)
                with patch.object(cli, 'merge_files', return_value=project), \
                     patch.object(project, 'explain_many', side_effect=MergeError('batch failed')), \
                     patch.object(cli.sys, 'stdin') as stdin, \
                     patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout, \
                     patch.object(cli.sys, 'stderr', new_callable=io.StringIO) as stderr:
                    stdin.buffer.read.return_value = json.dumps(paths).encode()
                    self.assertEqual(cli.main(['explain', '-f', 'examples/compose_merge/nul-key.yaml', *args]), 2)
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertIn('batch failed', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
