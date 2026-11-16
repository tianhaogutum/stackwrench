"""Inline batches share bounded query semantics without reading stdin/query files."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from . import test_cli_path_json as transport
from . import test_cli_paths_stdin as batch
from stackwrench.compose_merge.core import Project, parse

MAX_PATH_BYTES = 8 * 1024 * 1024


class JsonPathsArgvCliTests(unittest.TestCase):
    setUp = transport.JsonPathCliTests.setUp
    fixture = batch.JsonPathsStdinCliTests.fixture
    query = batch.JsonPathsStdinCliTests.query

    test_nul_present_removed_readded_exact_ordered_history = batch.JsonPathsStdinCliTests.test_nul_present_removed_readded_exact_ordered_history
    test_invalid_arrays_encoding_and_any_pointer_fail_atomically = batch.JsonPathsStdinCliTests.test_invalid_arrays_encoding_and_any_pointer_fail_atomically
    test_input_gates_with_batch_transport = batch.JsonPathsStdinCliTests.test_input_gates_with_batch_transport

    def call(self, files, data, error=None):
        return transport.JsonPathCliTests.call(self, files, '--paths-json',
                                               os.fsdecode(data), error=error)

    def test_decode_once_tokens_and_one_merge_without_query_io(self):
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
             patch.object(cli.json, 'loads', wraps=json.loads) as decode, \
             patch('pathlib.Path.open', autospec=True, side_effect=Path.open) as opened, \
             patch.object(cli.os, 'open', side_effect=AssertionError('query file read')), \
             patch.object(cli.sys, 'stdin') as stdin, \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
            text = json.dumps(paths, ensure_ascii=False)
            self.assertEqual(cli.main(['explain', '-f', str(file), '--paths-json', text]), 0)
            decode.assert_called_once_with(text)
            self.assertEqual(json.loads(stdout.getvalue()), expected)
            merge.assert_called_once_with([str(file)])
            self.assertEqual(opened.call_args_list, [unittest.mock.call(Path(file), 'rb')])
            stdin.buffer.read.assert_not_called()

    def test_representable_large_keys_preserve_exact_removal_and_readd(self):
        self.assertEqual(sys.get_int_max_str_digits(), 4300)
        # Beyond Python's integer conversion limit, within this OS's argv limit.
        keys = ['9' * 10000, '0' * 10000]
        mapping = '\n'.join('  ? "' + key + '"\n  : old' for key in keys)
        texts = ['x-data:\n' + mapping + '\n', 'x-data: !override [new]\n',
                 'x-data: !override\n' + mapping.replace(': old', ': restored') + '\n']
        files = [self.fixture(str(i) + '.yaml', text) for i, text in enumerate(texts)]
        project, prior = Project(), None
        paths = ['/x-data/' + key for key in keys]
        for i, (text, file) in enumerate(zip(texts, files)):
            project.add(parse(text, str(file), i))
            infos = self.query(files[:i + 1], paths)['explanations']
            self.assertEqual(infos, [project.explain(p) for p in paths])
            for j, info in enumerate(infos):
                source = dict(file=str(files[0]), line=3 + j * 2, column=5, input=0)
                if i == 0:
                    self.assertEqual((info['value'], info['winningSources']), ('old', [source]))
                elif i == 1:
                    self.assertEqual((info['present'], info['value'], info['winningSources']), (False, None, []))
                    self.assertEqual((info['events'][-1]['previous'], info['events'][-1]['previousSources']),
                                     ('old', [source]))
                else:
                    self.assertEqual(info['events'][:2], prior[j]['events'])
                    self.assertEqual([e['action'] for e in info['events']], ['add', 'override', 'override'])
                    self.assertEqual((info['value'], info['winningSources']),
                        ('restored', [dict(file=str(files[2]), line=3 + j * 2, column=5, input=2)]))
            prior = infos
        self.assertEqual(sys.get_int_max_str_digits(), 4300)

    def test_query_count_and_utf8_byte_cap_before_decode(self):
        base = self.fixture('base.yaml', 'x-data: {雪: live}\n')
        self.assertEqual(len(self.query([base], [''] * 64)['explanations']), 64)
        from stackwrench.compose_merge import cli
        # Deterministic in-process checks: OS argv limits preclude 8 MiB launches.
        for paths in [[''], ['/x-data/雪']]:
            text = json.dumps(paths, ensure_ascii=False)
            text += ' ' * (MAX_PATH_BYTES - len(text.encode('utf-8')))
            self.assertEqual(len(text.encode('utf-8')), MAX_PATH_BYTES)
            with patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
                self.assertEqual(cli.main(['explain', '-f', str(base), '--paths-json', text]), 0)
                expected = Project().add(parse(base.read_text(), str(base)))
                self.assertEqual(json.loads(stdout.getvalue()),
                                 {'explanations': [expected.explain(p) for p in paths]})
            for invalid, message in [(text + ' ', '8388608'),
                                     ('["雪"]' + ' ' * (MAX_PATH_BYTES - 5), '8388608'),
                                     ('["\ud800"]', 'UTF-8')]:
                with patch.object(cli.json, 'loads', side_effect=AssertionError('decode before budget check')), \
                     patch.object(cli, 'merge_files', side_effect=AssertionError('merge before budget check')), \
                     patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout, \
                     patch.object(cli.sys, 'stderr', new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(['explain', '-f', str(base), '--paths-json', invalid])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertIn(message, stderr.getvalue())
                    self.assertNotIn('Traceback', stderr.getvalue())

    def test_conflicts_help_open_pipe_and_all_singular_array_rejections(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        single = self.fixture('single.json', '"/x-data"\n')
        plural_file = self.fixture('plural.json', '["/x-data", ""]\n')
        prefix = [*transport.CLI, 'explain', '-f', str(base)]
        plural = ['--paths-json', '["/x-data", ""]']
        cases = [(prefix + plural, 0), (prefix + plural + ['--help'], 0),
                 (prefix + ['--paths-json'], 2), (prefix + plural + ['extra'], 2),
                 (prefix + plural + ['--unknown'], 2),
                 ([*transport.CLI, 'explain', *plural], 2)]
        for args in [['--path', '/x-data'], ['--path-json', '"/x-data"'],
                     ['--path-json-file', str(single)], ['--paths-json-file', str(plural_file)],
                     ['--path-json-stdin'], ['--paths-json-stdin']]:
            cases.extend([(prefix + args + plural, 2), (prefix + plural + args, 2)])
        for argv, code in cases:
            with self.subTest(argv=argv):
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as process:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        self.fail('inline transport read stdin during argument validation')
                    stdout, stderr = process.communicate()
                batch.JsonPathsStdinCliTests.record(self, argv, None,
                    subprocess.CompletedProcess(argv, process.returncode, stdout, stderr))
                self.assertEqual(process.returncode, code, stderr.decode())
                self.assertNotIn(b'Traceback', stderr)
                self.assertEqual(stdout if code == 2 else stderr, b'')
        for args in [['--path-json', '["/x-data", ""]'], ['--path-json-file', str(plural_file)]]:
            transport.JsonPathCliTests.call(self, [base], *args, error='JSON string')
        result = subprocess.run(prefix + ['--path-json-stdin'], input=b'["/x-data", ""]',
                                capture_output=True, timeout=5)
        batch.JsonPathsStdinCliTests.record(self, result.args, b'["/x-data", ""]', result)
        self.assertEqual((result.returncode, result.stdout), (2, b''))
        self.assertIn(b'JSON string', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
