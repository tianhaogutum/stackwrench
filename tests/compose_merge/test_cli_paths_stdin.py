"""Bounded opt-in batches keep individual explanations and fail atomically."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from . import test_cli_path_json as transport
from . import test_cli_path_stdin as singular
from stackwrench.compose_merge.core import Project, parse

MAX_PATH_BYTES = 8 * 1024 * 1024


class JsonPathsStdinCliTests(unittest.TestCase):
    setUp = transport.JsonPathCliTests.setUp

    def fixture(self, name, text):
        file = transport.JsonPathCliTests.fixture(self, name, text)
        if log := os.environ.get('CMT_CLI_LOG'):
            blobs = Path(log).parent / 'fixture-inputs'
            blobs.mkdir(exist_ok=True)
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            blob = blobs / (digest + '.yaml')
            blob.write_bytes(file.read_bytes())
            with open(log, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(fixture=str(file), contentFile=str(blob), sha256=digest)) + '\n')
        return file

    record = singular.JsonPathStdinCliTests.record

    def call(self, files, data, error=None):
        argv = [*transport.CLI, 'explain', *[a for f in files for a in ('-f', str(f))],
                '--paths-json-stdin']
        result = subprocess.run(argv, input=data, capture_output=True, timeout=15)
        self.record(argv, data, result)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertEqual(result.returncode, 2 if error else 0, result.stderr.decode())
        if error:
            self.assertEqual(result.stdout, b'')
            self.assertIn(error, result.stderr.decode())
            return
        self.assertEqual(result.stderr, b'')
        return json.loads(result.stdout)

    def query(self, files, paths):
        return self.call(files, (json.dumps(paths, ensure_ascii=False) + '\n').encode('utf-8'))

    def test_nul_present_removed_readded_exact_ordered_history(self):
        texts = ['x-data: {"\\0": old}\n', 'x-data: !override []\n',
                 'x-data: !override {"\\0": !override restored}\n']
        files = [self.fixture(str(i) + '.yaml', text) for i, text in enumerate(texts)]
        nodes = [parse(text, str(file), i) for i, (text, file) in enumerate(zip(texts, files))]
        original = copy.deepcopy(nodes)
        path, project, infos = '/x-data/\0', Project(), []
        for i, node in enumerate(nodes):
            project.add(node)
            paths = [path, '', path, '/x-data']
            batch = self.query(files[:i + 1], paths)
            self.assertEqual(batch, {'explanations': [project.explain(p) for p in paths]})
            infos.append(batch['explanations'][0])
        source = dict(file=str(files[0]), line=1, column=16, input=0)
        self.assertEqual((infos[0]['present'], infos[0]['value'], infos[0]['winningSources']),
                         (True, 'old', [source]))
        self.assertEqual([e['action'] for e in infos[0]['events']], ['add'])
        self.assertEqual((infos[1]['present'], infos[1]['value'], infos[1]['winningSources']),
                         (False, None, []))
        removed = infos[1]['events'][-1]
        self.assertEqual((removed['previousPresent'], removed['previous'], removed['previousSources']),
                         (True, 'old', [source]))
        self.assertEqual(removed['source'], dict(file=str(files[1]), line=1, column=9, input=1))
        self.assertEqual(infos[2]['events'][:2], infos[1]['events'])
        self.assertEqual([e['action'] for e in infos[2]['events']], ['add', 'override', 'override'])
        self.assertEqual((infos[2]['events'][-1]['previousPresent'],
                          infos[2]['events'][-1]['previousSources']), (False, []))
        self.assertEqual((infos[2]['value'], infos[2]['winningSources']),
                         ('restored', [dict(file=str(files[2]), line=1, column=26, input=2)]))
        self.assertEqual(nodes, original)

    def test_decode_once_tokens_root_duplicates_and_single_merge(self):
        values = {'\0': 'nul', r'\u0000': 'literal', '"quoted"': 'quotes', '~1': 'tilde-one',
                  'a/b~c': {'': {'雪😀\0': ['old', None]}}, '': 'empty'}
        file = self.fixture('tokens.yaml', 'x-data: ' + json.dumps(values, ensure_ascii=False) + '\n')
        paths = ['/x-data/\0', r'/x-data/\u0000', '/x-data/"quoted"', '/x-data/~01',
                 '/x-data/a~1b~0c//雪😀\0/1', '/x-data/', '', '/x-data/\0']
        project = Project().add(parse(file.read_text(), str(file)))
        expected = {'explanations': [project.explain(p) for p in paths]}
        self.assertEqual(self.query([file], paths), expected)
        from stackwrench.compose_merge import cli
        with patch.object(cli, 'merge_files', wraps=cli.merge_files) as merge, \
             patch('pathlib.Path.open', autospec=True, side_effect=Path.open) as opened, \
             patch.object(cli.sys, 'stdin') as stdin, \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
            stdin.buffer.read.return_value = json.dumps(paths).encode()
            self.assertEqual(cli.main(['explain', '-f', str(file), '--paths-json-stdin']), 0)
            self.assertEqual(json.loads(stdout.getvalue()), expected)
            merge.assert_called_once_with([str(file)])
            self.assertEqual(opened.call_args_list, [unittest.mock.call(Path(file), 'rb')])
            stdin.buffer.read.assert_called_once_with(MAX_PATH_BYTES + 1)
            stdin.buffer.close.assert_not_called()

    def test_large_numeric_keys_keep_exact_removal_and_readd_history(self):
        self.assertEqual(sys.get_int_max_str_digits(), 4300)
        keys = ['9' * 140000, '0' * 140000]
        mapping = '\n'.join('  ? "' + key + '"\n  : old' for key in keys)
        texts = ['x-data:\n' + mapping + '\n', 'x-data: !override [new]\n',
                 'x-data: !override\n' + mapping.replace(': old', ': restored') + '\n']
        files = [self.fixture(str(i) + '.yaml', text) for i, text in enumerate(texts)]
        project, prior = Project(), None
        for i, (text, file) in enumerate(zip(texts, files)):
            project.add(parse(text, str(file), i))
            paths = ['/x-data/' + key for key in keys]
            result = self.query(files[:i + 1], paths)['explanations']
            self.assertEqual(result, [project.explain(p) for p in paths])
            for j, info in enumerate(result):
                old_source = dict(file=str(files[0]), line=3 + j * 2, column=5, input=0)
                if i == 0:
                    self.assertEqual((info['value'], info['winningSources']), ('old', [old_source]))
                elif i == 1:
                    self.assertFalse(info['present'])
                    self.assertEqual((info['events'][-1]['previous'], info['events'][-1]['previousSources']),
                                     ('old', [old_source]))
                else:
                    self.assertEqual(info['events'][:2], prior[j]['events'])
                    self.assertEqual([e['action'] for e in info['events']], ['add', 'override', 'override'])
                    self.assertEqual((info['value'], info['winningSources']),
                                     ('restored', [dict(file=str(files[2]), line=3 + j * 2, column=5, input=2)]))
            prior = result
        self.assertEqual(sys.get_int_max_str_digits(), 4300)

    def test_invalid_arrays_encoding_and_any_pointer_fail_atomically(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        for data in [b'', b'["/x-data"', b'["\\x00"]', b'[""] trailing', b'[""] [""]',
                     b'"/x-data"', b'null', b'false', b'123', b'NaN', b'{}',
                     b'9' * 4301, b'[' * 2000 + b'0' + b']' * 2000, b'\xef\xbb\xbf[""]',
                     b'[]', json.dumps([''] * 65).encode()]:
            with self.subTest(data=data[:40]):
                self.call([base], data, error='JSON array of 1..64 strings')
        for member in [None, False, 1, 1.5, float('nan'), [], {}, ['']]:
            self.call([base], json.dumps(['/x-data', member]).encode(), error='JSON array of 1..64 strings')
        for data in [b'["/x-data/\xff"]', b'["/x-data/\xed\xa0\x80"]', b'["/x-data/\xf0\x9f']:
            self.call([base], data, error='UTF-8')
        for path, error in [('x-data', 'RFC 6901'), ('/x-data/~2', 'RFC 6901'),
                            ('/missing', 'path has no history'), ('/x-data/\0', 'path has no history'),
                            ('/x-data/\ud800', 'path has no history'),
                            (json.dumps('/x-data'), 'RFC 6901')]:
            for paths in [[path, ''], ['', path], ['/x-data', path, '']]:
                self.call([base], json.dumps(paths).encode(), error=error)

    def test_query_count_byte_cap_and_read_failures(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        self.assertEqual(len(self.query([base], [''] * 64)['explanations']), 64)
        data = b'[""]' + b' ' * (MAX_PATH_BYTES - 4)
        self.assertEqual(self.call([base], data)['explanations'][0]['value'], {'x-data': 'live'})
        self.call([base], data + b' ', error='8388608')
        # Budget is bytes, including multibyte Unicode and trailing whitespace.
        data = json.dumps(['/x-data/雪'], ensure_ascii=False).encode()
        unicode_base = self.fixture('unicode.yaml', 'x-data: {雪: live}\n')
        data += b' ' * (MAX_PATH_BYTES - len(data))
        self.assertEqual(self.call([unicode_base], data)['explanations'][0]['value'], 'live')
        self.call([unicode_base], data + b' ', error='8388608')
        from stackwrench.compose_merge.cli import main
        for exc in [OSError('denied'), ValueError('closed stream')]:
            with patch('stackwrench.compose_merge.cli.sys.stdin') as stdin, \
                 patch('stackwrench.compose_merge.cli.sys.stdout', new_callable=io.StringIO) as stdout, \
                 patch('stackwrench.compose_merge.cli.sys.stderr', new_callable=io.StringIO) as stderr:
                stdin.buffer.read.side_effect = exc
                with self.assertRaises(SystemExit) as raised:
                    main(['explain', '-f', str(base), '--paths-json-stdin'])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('cannot read path JSON stdin', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())

    def test_help_and_argument_errors_never_read_open_pipe(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        query = self.fixture('pointer.json', '"/x-data"\n')
        prefix = [*transport.CLI, 'explain', '-f', str(base)]
        plural = ['--paths-json-stdin']
        cases = [(prefix + plural + ['--help'], 0), (prefix + plural + ['extra'], 2),
                 (prefix + plural + ['--unknown'], 2),
                 ([*transport.CLI, 'explain', *plural], 2)]
        for args in [['--path', '/x-data'], ['--path-json', '"/x-data"'],
                     ['--path-json-file', str(query)], ['--path-json-stdin']]:
            cases.extend([(prefix + args + plural, 2), (prefix + plural + args, 2)])
        for argv, code in cases:
            with self.subTest(argv=argv):
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as process:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        self.fail('CLI waited for stdin before valid arguments')
                    stdout, stderr = process.communicate()
                result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                self.record(argv, None, result)
                self.assertEqual(result.returncode, code, stderr.decode())
                self.assertNotIn(b'Traceback', stderr)
                if code == 2:
                    self.assertEqual(stdout, b'')
                else:
                    self.assertEqual(stderr, b'')

    def test_input_gates_with_batch_transport(self):
        base = self.fixture('base.yaml', 'x-data: {"\\0": old}\n')
        data = b'["/x-data/\\u0000", ""]'
        for i, (text, error) in enumerate([
            ('x-data: [new]', 'type change'), ('x-data: !unknown value', 'tag'),
            ('x-data: [!reset null]', 'mapping values'), ('x-data: "${IMAGE}"', 'interpolation'),
            ('x-data: {a: one, a: two}', 'duplicate'), ('x-data: "' + 'a' * (1024 * 1024) + '"', 'exceeds')]):
            later = self.fixture(str(i) + '.yaml', text)
            self.call([base, later], data, error=error)
        self.call([base] * 33, data, error='1..32 files')
        missing = self.directory / 'missing.yaml'
        self.call([missing], data, error='cannot read')
        resource = self.fixture('resource.yaml', 'services: {web: {ports: [{target: 80, name: old}]}}')
        loss = self.fixture('loss.yaml', 'services: {web: {ports: [80]}}')
        self.call([resource, loss], b'["/services/web/ports/0", ""]', error='omits')


if __name__ == '__main__':
    unittest.main()
