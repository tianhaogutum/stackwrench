"""Installed CLI contract for pointers larger than the OS argv transport."""
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch

from . import test_cli_path_json as transport
from stackwrench.compose_merge.core import Project, parse

MAX_PATH_BYTES = 8 * 1024 * 1024


class JsonPathFileCliTests(unittest.TestCase):
    setUp = transport.JsonPathCliTests.setUp
    fixture = transport.JsonPathCliTests.fixture
    call = transport.JsonPathCliTests.call

    def query(self, path):
        name = 'pointer-' + str(len(list(self.directory.glob('pointer-*.json')))) + '.json'
        return self.fixture(name, json.dumps(path, ensure_ascii=False) + '\n')

    def test_large_keys_present_removed_readded_exact_history(self):
        self.assertEqual(sys.get_int_max_str_digits(), 4300)
        for digit in ['9', '0']:
            key = digit * 140000
            path = '/x-data/' + key
            texts = ['x-data:\n  ? "' + key + '"\n  : old\n',
                     'x-data: !override [new]\n',
                     'x-data: !override\n  ? "' + key + '"\n  : restored\n']
            files = [self.fixture(digit + '-' + str(i) + '.yaml', text)
                     for i, text in enumerate(texts)]
            nodes = [parse(text, str(file), i) for i, (text, file) in enumerate(zip(texts, files))]
            original = copy.deepcopy(nodes)
            query = self.query(path)
            project = Project()
            infos = []
            for i, node in enumerate(nodes):
                project.add(node)
                info = self.call(files[:i + 1], '--path-json-file', str(query))
                self.assertEqual(info, project.explain(path))
                infos.append(info)
            old_source = dict(file=str(files[0]), line=3, column=5, input=0)
            self.assertEqual((infos[0]['present'], infos[0]['value'], infos[0]['winningSources']),
                             (True, 'old', [old_source]))
            self.assertEqual([e['action'] for e in infos[2]['events']], ['add', 'override', 'override'])
            removed = infos[1]['events'][-1]
            self.assertEqual((infos[1]['present'], infos[1]['value'], infos[1]['winningSources']),
                             (False, None, []))
            self.assertEqual((removed['previousPresent'], removed['previous'], removed['previousSources']),
                             (True, 'old', [old_source]))
            self.assertEqual(removed['source'], dict(file=str(files[1]), line=1, column=9, input=1))
            self.assertEqual(infos[2]['events'][:2], infos[1]['events'])
            self.assertFalse(infos[2]['events'][-1]['previousPresent'])
            self.assertEqual(infos[2]['events'][-1]['previousSources'], [])
            self.assertEqual((infos[2]['present'], infos[2]['value'], infos[2]['winningSources']),
                             (True, 'restored', [dict(file=str(files[2]), line=3, column=5, input=2)]))
            self.assertEqual(nodes, original)
            self.call(files[:2], '--path-json-file', str(self.query('/x-data/' + '8' * 140000)),
                      error='path has no history')
        self.assertEqual(sys.get_int_max_str_digits(), 4300)

    def test_nested_tokens_nul_literal_root_and_exact_once(self):
        values = {'\0': 'nul', r'\u0000': 'literal', '~1': 'tilde-one',
                  'a/b~c': {'': {'雪😀\0': 'nested'}}, '': 'empty'}
        base = self.fixture('tokens.yaml', 'x-data: ' + json.dumps(values, ensure_ascii=False) + '\n')
        project = Project().add(parse(base.read_text(), str(base)))
        for path in ['/x-data/\0', r'/x-data/\u0000', '/x-data/~01',
                     '/x-data/a~1b~0c//雪😀\0', '/x-data/', '']:
            query = self.query(path)
            info = self.call([base], '--path-json-file', str(query))
            self.assertEqual(info, project.explain(path))
        query = self.query(json.dumps('/x-data/\0'))
        self.call([base], '--path-json-file', str(query), error='RFC 6901')
        next_file = self.fixture('next.yaml', 'x-data: !override [new]\n')
        query = self.query('/x-data/a~1b~0c//雪😀\0')
        info = self.call([base, next_file], '--path-json-file', str(query))
        self.assertFalse(info['present'])
        self.assertEqual(info['events'][-1]['previous'], 'nested')
        query = self.query('/x-data/0')
        self.assertEqual(self.call([base, next_file], '--path-json-file', str(query))['value'], 'new')

    def test_invalid_files_json_pointers_and_conflicts_fail_closed(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        for i, text in enumerate(['', '"unterminated', '"\\x00"', '"/x-data" trailing',
                                   'null', 'false', '123', 'NaN', '[]', '{}', '9' * 4301,
                                   '[' * 2000 + '0' + ']' * 2000]):
            file = self.fixture(str(i) + '.json', text)
            self.call([base], '--path-json-file', str(file), error='JSON string')
        for path, error in [('x-data', 'RFC 6901'), ('/x-data/~2', 'RFC 6901'),
                            ('/x-data/\0', 'path has no history')]:
            self.call([base], '--path-json-file', str(self.query(path)), error=error)
        self.call([base], '--path-json-file', str(self.directory / 'missing'), error='cannot read')
        self.call([base], '--path-json-file', str(self.directory), error='regular file')
        for i, data in enumerate([b'"/x-data/\xff"', b'\xef\xbb\xbf"/x-data"']):
            file = self.directory / ('bytes-' + str(i) + '.json')
            file.write_bytes(data)
            self.call([base], '--path-json-file', str(file), error='error:')
            self.assertEqual(file.read_bytes(), data)
        if hasattr(os, 'mkfifo'):
            fifo = self.directory / 'fifo'
            os.mkfifo(fifo)
            self.call([base], '--path-json-file', str(fifo), error='regular file')
        query = self.query('/x-data')
        for args in [['--path-json-file'],
                     ['--path', '/x-data', '--path-json-file', str(query)],
                     ['--path-json-file', str(query), '--path', '/x-data'],
                     ['--path-json', '"/x-data"', '--path-json-file', str(query)],
                     ['--path-json-file', str(query), '--path-json', '"/x-data"']]:
            self.call([base], *args, error='error:')

    def test_file_byte_cap_and_read_failure(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        text = '""' + ' ' * (MAX_PATH_BYTES - 2)
        query = self.fixture('limit.json', text)
        self.assertEqual(self.call([base], '--path-json-file', str(query))['value'], {'x-data': 'live'})
        over = self.fixture('oversize.json', text + ' ')
        self.call([base], '--path-json-file', str(over), error='8388608')
        # Deterministic permission/read failure even when tests run as root.
        from stackwrench.compose_merge.cli import json_path_file
        import argparse
        with patch('stackwrench.compose_merge.cli.os.open', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(argparse.ArgumentTypeError, 'cannot read'):
                json_path_file(str(query))

    def test_input_gates_with_file_transport(self):
        base = self.fixture('base.yaml', 'x-data: {"\\0": old}\n')
        query = self.query('/x-data/\0')
        for i, (text, error) in enumerate([
            ('x-data: [new]', 'type change'), ('x-data: !unknown value', 'tag'),
            ('x-data: [!reset null]', 'mapping values'), ('x-data: "${IMAGE}"', 'interpolation'),
            ('x-data: {a: one, a: two}', 'duplicate')]):
            later = self.fixture(str(i) + '.yaml', text)
            self.call([base, later], '--path-json-file', str(query), error=error)
        resource = self.fixture('resource.yaml', 'services: {web: {ports: [{target: 80, name: old}]}}')
        loss = self.fixture('loss.yaml', 'services: {web: {ports: [80]}}')
        self.call([resource, loss], '--path-json-file', str(query), error='omits')


if __name__ == '__main__':
    unittest.main()
