"""Explicit regular-file batches share stdin query semantics, never stdin reads."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from . import test_cli_path_json as transport
from . import test_cli_paths_stdin as batch
from stackwrench.compose_merge.core import Project, parse

MAX_PATH_BYTES = 8 * 1024 * 1024


class JsonPathsFileCliTests(unittest.TestCase):
    setUp = transport.JsonPathCliTests.setUp
    fixture = batch.JsonPathsStdinCliTests.fixture
    query = batch.JsonPathsStdinCliTests.query

    # Apply the same history, shape, pointer and fail-closed gates to file bytes.
    test_nul_present_removed_readded_exact_ordered_history = batch.JsonPathsStdinCliTests.test_nul_present_removed_readded_exact_ordered_history
    test_large_numeric_keys_keep_exact_removal_and_readd_history = batch.JsonPathsStdinCliTests.test_large_numeric_keys_keep_exact_removal_and_readd_history
    test_invalid_arrays_encoding_and_any_pointer_fail_atomically = batch.JsonPathsStdinCliTests.test_invalid_arrays_encoding_and_any_pointer_fail_atomically
    test_input_gates_with_batch_transport = batch.JsonPathsStdinCliTests.test_input_gates_with_batch_transport

    def query_file(self, data):
        digest = hashlib.sha256(data).hexdigest()
        file = self.directory / (digest + '.json')
        file.write_bytes(data)
        self.addCleanup(lambda: self.assertEqual(file.read_bytes(), data))
        if log := os.environ.get('CMT_CLI_LOG'):
            blobs = Path(log).parent / 'query-inputs'
            blobs.mkdir(exist_ok=True)
            blob = blobs / (digest + '.json')
            blob.write_bytes(data)
            with open(log, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(fixture=str(file), contentFile=str(blob), sha256=digest)) + '\n')
        return file

    def call_args(self, files, *args, error=None):
        return transport.JsonPathCliTests.call(self, files, *args, error=error)

    def call(self, files, data, error=None):
        return self.call_args(files, '--paths-json-file', str(self.query_file(data)), error=error)

    def test_decode_once_order_and_one_merge_without_stdin(self):
        values = {'\0': 'nul', r'\u0000': 'literal', '"quoted"': 'quotes', '~1': 'tilde-one',
                  'a/b~c': {'': {'雪😀\0': ['old', None]}}, '': 'empty'}
        file = self.fixture('tokens.yaml', 'x-data: ' + json.dumps(values, ensure_ascii=False) + '\n')
        paths = ['/x-data/\0', r'/x-data/\u0000', '/x-data/"quoted"', '/x-data/~01',
                 '/x-data/a~1b~0c//雪😀\0/1', '/x-data/', '', '/x-data/\0']
        expected = {'explanations': [Project().add(parse(file.read_text(), str(file))).explain(p)
                                     for p in paths]}
        self.assertEqual(self.query([file], paths), expected)
        from stackwrench.compose_merge import cli
        query = self.query_file(json.dumps(paths).encode())
        with patch.object(cli, 'merge_files', wraps=cli.merge_files) as merge, \
             patch('pathlib.Path.open', autospec=True, side_effect=Path.open) as opened, \
             patch.object(cli.sys, 'stdin') as stdin, \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(['explain', '-f', str(file), '--paths-json-file', str(query)]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), expected)
            merge.assert_called_once_with([str(file)])
            self.assertEqual(opened.call_args_list, [unittest.mock.call(Path(file), 'rb')])
            stdin.buffer.read.assert_not_called()

    def test_byte_query_caps_and_bounded_read_errors(self):
        base = self.fixture('base.yaml', 'x-data: {雪: live}\n')
        self.assertEqual(len(self.query([base], [''] * 64)['explanations']), 64)
        data = json.dumps(['/x-data/雪'], ensure_ascii=False).encode()
        data += b' ' * (MAX_PATH_BYTES - len(data))
        self.assertEqual(self.call([base], data)['explanations'][0]['value'], 'live')
        self.call([base], data + b' ', error='8388608')
        from stackwrench.compose_merge import cli
        query = self.query_file(b'[""]')
        # Actual cap+1 request; deterministic permission and read errors.
        with patch.object(cli.os, 'fdopen') as opened:
            opened.return_value.__enter__.return_value.read.return_value = b'[""]'
            self.assertEqual(cli.json_paths_file(str(query)), [''])
            opened.return_value.__enter__.return_value.read.assert_called_once_with(MAX_PATH_BYTES + 1)
        for target, exc in [('os.open', PermissionError('denied')),
                            ('os.fstat', OSError('cannot stat')),
                            ('os.fdopen', OSError('cannot read'))]:
            with patch('stackwrench.compose_merge.cli.' + target, side_effect=exc), \
                 patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout, \
                 patch.object(cli.sys, 'stderr', new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    cli.main(['explain', '-f', str(base), '--paths-json-file', str(query)])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('cannot read', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())

    def test_regular_files_symlinks_literal_dash_and_nonregular_errors(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        query = self.query_file(b'["/x-data", ""]')
        self.call_args([base], '--paths-json-file', str(self.directory / 'missing'), error='cannot read')
        self.call_args([base], '--paths-json-file', str(self.directory), error='regular file')
        link = self.directory / 'linked.json'
        link.symlink_to(query)
        self.assertEqual(self.call_args([base], '--paths-json-file', str(link)), self.query([base], ['/x-data', '']))
        dangling = self.directory / 'dangling.json'
        dangling.symlink_to(self.directory / 'missing')
        self.call_args([base], '--paths-json-file', str(dangling), error='cannot read')
        for name in ['fifo', 'linked-fifo']:
            file = self.directory / name
            if name == 'fifo':
                os.mkfifo(file)
            else:
                file.symlink_to(self.directory / 'fifo')
            self.call_args([base], '--paths-json-file', str(file), error='regular file')
        self.call_args([base], '--paths-json-file', '/dev/null', error='regular file')
        # '-' names an actual file, even while stdin remains open without data.
        dash = self.fixture('-', '["/x-data", ""]\n')
        argv = [*transport.CLI, 'explain', '-f', str(base), '--paths-json-file', '-']
        with subprocess.Popen(argv, cwd=self.directory, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                self.fail('file transport read stdin')
            stdout, stderr = process.communicate()
        transport_log = batch.JsonPathsStdinCliTests.record
        transport_log(self, argv, None, subprocess.CompletedProcess(argv, process.returncode, stdout, stderr))
        self.assertEqual((process.returncode, stderr), (0, b''))
        self.assertEqual(json.loads(stdout), self.query([base], ['/x-data', '']))
        self.assertEqual(dash.read_text(), '["/x-data", ""]\n')
        if Path('/dev/stdin').exists():
            argv[-1] = '/dev/stdin'
            result = subprocess.run(argv, input=b'[""]', capture_output=True, timeout=5)
            transport_log(self, argv, b'[""]', result)
            self.assertEqual((result.returncode, result.stdout), (2, b''))
            self.assertIn(b'regular file', result.stderr)

    def test_conflicts_help_and_singular_file_contract(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        query = self.query_file(b'["/x-data", ""]')
        single = self.fixture('single.json', '"/x-data"\n')
        self.call_args([base], '--path-json-file', str(query), error='JSON string')
        self.call_args([base], '--paths-json-file', str(single), error='JSON array of 1..64 strings')
        prefix = [*transport.CLI, 'explain', '-f', str(base)]
        plural = ['--paths-json-file', str(query)]
        cases = [(prefix + plural + ['--help'], 0), (prefix + ['--paths-json-file'], 2),
                 (prefix + plural + ['extra'], 2), (prefix + plural + ['--unknown'], 2)]
        for args in [['--path', '/x-data'], ['--path-json', '"/x-data"'],
                     ['--path-json-file', str(single)], ['--path-json-stdin'], ['--paths-json-stdin']]:
            cases.extend([(prefix + args + plural, 2), (prefix + plural + args, 2)])
        for argv, code in cases:
            with self.subTest(argv=argv):
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as process:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        self.fail('CLI read stdin during file argument validation')
                    stdout, stderr = process.communicate()
                batch.JsonPathsStdinCliTests.record(self, argv, None,
                    subprocess.CompletedProcess(argv, process.returncode, stdout, stderr))
                self.assertEqual(process.returncode, code, stderr.decode())
                self.assertNotIn(b'Traceback', stderr)
                self.assertEqual(stdout if code == 2 else stderr, b'')


if __name__ == '__main__':
    unittest.main()
