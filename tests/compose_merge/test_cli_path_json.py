"""JSON is an opt-in transport for pointers that cannot fit in OS argv."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.compose_merge.core import Project, parse

CLI = [sys.executable, '-m', 'stackwrench', 'compose-merge']


class JsonPathCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def fixture(self, name, text):
        path = self.directory / name
        path.write_text(text, encoding='utf-8')
        self.addCleanup(lambda: self.assertEqual(path.read_text(encoding='utf-8'), text))
        return path

    def call(self, files, *args, error=None, command='explain'):
        argv = [*CLI, command, *[a for f in files for a in ('-f', str(f))], *args]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        if log := os.environ.get('CMT_CLI_LOG'):
            with open(log, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(command=argv, exitCode=result.returncode,
                                             stdout=result.stdout, stderr=result.stderr)) + '\n')
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(result.returncode, 2 if error else 0, result.stderr)
        if error:
            self.assertEqual(result.stdout, '')
            self.assertIn(error, result.stderr)
            return
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def test_nul_present_removed_readded_exact_history(self):
        texts = ['x-data: {"\\0": old}\n', 'x-data: !override []\n',
                 'x-data: !override {"\\0": !override restored}\n']
        files = [self.fixture(name, text) for name, text in
                 zip(['base.yaml', 'next.yaml', 'last.yaml'], texts)]
        nodes = [parse(text, str(file), i) for i, (text, file) in enumerate(zip(texts, files))]
        originals = copy.deepcopy(nodes)
        path = '/x-data/\0'
        project = Project()
        infos = []
        for i, node in enumerate(nodes):
            project.add(node)
            info = self.call(files[:i + 1], '--path-json', json.dumps(path))
            self.assertEqual(info, project.explain(path))
            self.assertEqual(self.call(files[:i + 1], command='render'), project.render())
            infos.append(info)
        original_source = dict(file=str(files[0]), line=1, column=16, input=0)
        self.assertEqual(infos[0]['winningSources'], [original_source])
        self.assertEqual(infos[0]['value'], 'old')
        self.assertEqual((infos[1]['present'], infos[1]['value'], infos[1]['winningSources']),
                         (False, None, []))
        removed = infos[1]['events'][-1]
        self.assertTrue(removed['previousPresent'])
        self.assertEqual(removed['previous'], 'old')
        self.assertEqual(removed['previousSources'], [original_source])
        self.assertEqual(removed['source'], dict(file=str(files[1]), line=1, column=9, input=1))
        self.assertEqual([e['action'] for e in infos[2]['events']], ['add', 'override', 'override'])
        self.assertEqual(infos[2]['events'][:2], infos[1]['events'])
        self.assertFalse(infos[2]['events'][-1]['previousPresent'])
        self.assertEqual(infos[2]['value'], 'restored')
        self.assertEqual(infos[2]['events'][-1]['source'],
                         dict(file=str(files[2]), line=1, column=26, input=2))
        self.assertEqual(infos[2]['winningSources'],
                         [dict(file=str(files[2]), line=1, column=26, input=2)])
        self.assertEqual(nodes, originals)

    def test_exact_once_literal_paths_escaping_unicode_and_root(self):
        values = {'\0': 'nul', r'\u0000': 'literal', '"quoted"': 'quotes',
                  '~1': 'tilde-one', 'a/b~c': {'': {'雪😀\0': 'nested'}}, '': 'empty'}
        file = self.fixture('keys.yaml', 'x-data: ' + json.dumps(values, ensure_ascii=False) + '\n')
        project = Project().add(parse(file.read_text(), str(file)))
        for path, value in [('/x-data/\0', 'nul'), (r'/x-data/\u0000', 'literal'),
                            ('/x-data/"quoted"', 'quotes'), ('/x-data/~01', 'tilde-one'),
                            ('/x-data/a~1b~0c//雪😀\0', 'nested'), ('/x-data/', 'empty'),
                            ('', project.render())]:
            with self.subTest(path=path):
                info = self.call([file], '--path-json', json.dumps(path))
                self.assertEqual(info, project.explain(path))
                self.assertEqual(info['value'], value)
                if '\0' not in path:
                    self.assertEqual(self.call([file], '--path', path), info)
        self.call([file], '--path', '"/x-data/\\u0000"', error='RFC 6901')

    def test_long_numeric_tokens_and_list_indexes(self):
        self.assertEqual(sys.get_int_max_str_digits(), 4300)
        for key in ['9' * 4301, '0' * 4301]:
            with self.subTest(prefix=key[0]):
                base = self.fixture('base-' + key[0] + '.yaml',
                                    'x-data:\n  ? "' + key + '"\n  : {leaf: old}\n')
                later = self.fixture('next-' + key[0] + '.yaml', 'x-data: !override [new]\n')
                path = '/x-data/' + key + '/leaf'
                info = self.call([base, later], '--path-json', json.dumps(path))
                self.assertEqual((info['present'], info['value'], info['winningSources']),
                                 (False, None, []))
                self.assertEqual(info['events'][-1]['previous'], 'old')
                self.assertEqual(self.call([base, later], '--path', path), info)
                self.call([base, later], '--path-json', json.dumps('/x-data/' + '8' * 4301),
                          error='path has no history')
                self.assertEqual(self.call([base, later], '--path-json', '"/x-data/0"')['value'], 'new')
                self.call([base, later], '--path-json', '"/x-data/00"', error='path has no history')
        self.assertEqual(sys.get_int_max_str_digits(), 4300)

    def test_invalid_json_pointer_and_arguments_fail_closed(self):
        file = self.fixture('base.yaml', 'x-data: live\n')
        for value in ['', '"unterminated', '"\\x00"', '"/x-data" trailing',
                      'null', 'false', '123', 'NaN', '[]', '{}', '9' * 4301,
                      '[' * 2000 + '0' + ']' * 2000]:
            with self.subTest(value=value[:40]):
                self.call([file], '--path-json', value, error='JSON string')
        for path, error in [('x-data', 'RFC 6901'), ('/x-data/~2', 'RFC 6901'),
                            ('/x-data/\0', 'path has no history'),
                            ('/x-data/\ud800', 'path has no history')]:
            with self.subTest(path=path):
                self.call([file], '--path-json', json.dumps(path), error=error)
        for args in [[], ['--path-json'], ['--path', '/x-data', '--path-json', '"/x-data"'],
                     ['--path-json', '"/x-data"', '--path', '/x-data']]:
            with self.subTest(args=args):
                self.call([file], *args, error='error:')

    def test_input_failures_still_emit_no_partial_result(self):
        base = self.fixture('base.yaml', 'x-data: {"\\0": old}\n')
        for i, (text, error) in enumerate([
            ('x-data: [new]', 'type change'), ('x-data: !unknown value', 'tag'),
            ('x-data: [!reset null]', 'mapping values'),
            ('x-data: "${IMAGE}"', 'interpolation'),
            ('x-data: {a: one, a: two}', 'duplicate'),
        ]):
            later = self.fixture(str(i) + '.yaml', text)
            self.call([base, later], '--path-json', '"/x-data/\\u0000"', error=error)
        resource = self.fixture('resource.yaml', 'services: {web: {ports: [{target: 80, name: old}]}}')
        loss = self.fixture('loss.yaml', 'services: {web: {ports: [80]}}')
        self.call([resource, loss], '--path-json', '"/services/web/ports/0"', error='omits')


if __name__ == '__main__':
    unittest.main()
