"""Opt-in binary stdin transport, exercised through real CLI pipes."""
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
from stackwrench.compose_merge.core import Project, parse

MAX_PATH_BYTES = 8 * 1024 * 1024


class JsonPathStdinCliTests(unittest.TestCase):
    setUp = transport.JsonPathCliTests.setUp
    fixture = transport.JsonPathCliTests.fixture

    def record(self, argv, data, result):
        if log := os.environ.get('CMT_CLI_LOG'):
            record = dict(command=argv, exitCode=result.returncode,
                          stdout=result.stdout.decode('utf-8'), stderr=result.stderr.decode('utf-8'))
            if data is not None:
                inputs = Path(log).parent / 'stdin-inputs'
                inputs.mkdir(exist_ok=True)
                digest = hashlib.sha256(data).hexdigest()
                blob = inputs / (digest + '.bin')
                blob.write_bytes(data)
                record.update(stdinFile=str(blob), stdinBytes=len(data), stdinSha256=digest)
            else:
                record['stdin'] = 'pipe left open without data until process exited'
            with open(log, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(record) + '\n')

    def call(self, files, data, *args, error=None):
        argv = [*transport.CLI, 'explain', *[a for f in files for a in ('-f', str(f))],
                '--path-json-stdin', *args]
        result = subprocess.run(argv, input=data, capture_output=True, timeout=15)
        self.record(argv, data, result)
        stderr = result.stderr.decode('utf-8')
        self.assertNotIn('Traceback', stderr)
        self.assertEqual(result.returncode, 2 if error else 0, stderr)
        if error:
            self.assertEqual(result.stdout, b'')
            self.assertIn(error, stderr)
            return
        self.assertEqual(stderr, '')
        return json.loads(result.stdout)

    def query(self, files, path):
        return self.call(files, (json.dumps(path, ensure_ascii=False) + '\n').encode('utf-8'))

    def test_nul_present_removed_readded_exact_history(self):
        texts = ['x-data: {"\\0": old}\n', 'x-data: !override []\n',
                 'x-data: !override {"\\0": !override restored}\n']
        files = [self.fixture(str(i) + '.yaml', text) for i, text in enumerate(texts)]
        nodes = [parse(text, str(file), i) for i, (text, file) in enumerate(zip(texts, files))]
        original = copy.deepcopy(nodes)
        path = '/x-data/\0'
        infos, project = [], Project()
        for i, node in enumerate(nodes):
            project.add(node)
            info = self.query(files[:i + 1], path)
            self.assertEqual(info, project.explain(path))
            infos.append(info)
        source = dict(file=str(files[0]), line=1, column=16, input=0)
        self.assertEqual((infos[0]['present'], infos[0]['value'], infos[0]['winningSources']),
                         (True, 'old', [source]))
        self.assertEqual(len(infos[0]['events']), 1)
        self.assertEqual((infos[0]['events'][0]['action'], infos[0]['events'][0]['previousPresent'],
                          infos[0]['events'][0]['previous'], infos[0]['events'][0]['previousSources']),
                         ('add', False, None, []))
        removed = infos[1]['events'][-1]
        self.assertEqual((infos[1]['present'], infos[1]['value'], infos[1]['winningSources']),
                         (False, None, []))
        self.assertEqual((removed['previousPresent'], removed['previous'], removed['previousSources']),
                         (True, 'old', [source]))
        self.assertEqual(removed['source'], dict(file=str(files[1]), line=1, column=9, input=1))
        self.assertEqual([e['action'] for e in infos[2]['events']], ['add', 'override', 'override'])
        self.assertEqual(infos[2]['events'][:2], infos[1]['events'])
        self.assertEqual((infos[2]['events'][-1]['previousPresent'],
                          infos[2]['events'][-1]['previous'], infos[2]['events'][-1]['previousSources']),
                         (False, None, []))
        self.assertEqual((infos[2]['present'], infos[2]['value'], infos[2]['winningSources']),
                         (True, 'restored', [dict(file=str(files[2]), line=1, column=26, input=2)]))
        self.assertEqual(nodes, original)

    def test_nested_escaped_unicode_empty_and_root_decode_once(self):
        values = {'\0': 'nul', r'\u0000': 'literal', '"quoted"': 'quotes', '~1': 'tilde-one',
                  'a/b~c': {'': {'雪😀\0': ['old', None]}}, '': 'empty'}
        base = self.fixture('tokens.yaml', 'x-data: ' + json.dumps(values, ensure_ascii=False) + '\n')
        project = Project().add(parse(base.read_text(), str(base)))
        for path, value in [('/x-data/\0', 'nul'), (r'/x-data/\u0000', 'literal'),
                            ('/x-data/"quoted"', 'quotes'), ('/x-data/~01', 'tilde-one'),
                            ('/x-data/a~1b~0c//雪😀\0', ['old', None]), ('/x-data/', 'empty'),
                            ('', project.render())]:
            with self.subTest(path=path):
                info = self.query([base], path)
                self.assertEqual(info, project.explain(path))
                self.assertEqual(info['value'], value)
        self.query([base], '/x-data/a~1b~0c//雪😀\0/1')
        self.call([base], json.dumps(json.dumps('/x-data/\0')).encode(), error='RFC 6901')
        later = self.fixture('next.yaml', 'x-data: !override [new]\n')
        info = self.query([base, later], '/x-data/a~1b~0c//雪😀\0')
        self.assertFalse(info['present'])
        self.assertEqual(info['events'][-1]['previous'], ['old', None])
        self.assertEqual(info['events'][-1]['previousSources'],
                         project.explain('/x-data/a~1b~0c//雪😀\0')['winningSources'])
        self.assertEqual(self.query([base, later], '/x-data/0')['value'], 'new')

    def test_long_numeric_tokens_present_removed_readded(self):
        self.assertEqual(sys.get_int_max_str_digits(), 4300)
        for digit in ['9', '0']:
            key = digit * 140000
            path = '/x-data/' + key
            texts = ['x-data:\n  ? "' + key + '"\n  : old\n', 'x-data: !override [new]\n',
                     'x-data: !override\n  ? "' + key + '"\n  : restored\n']
            files = [self.fixture(digit + '-' + str(i) + '.yaml', text) for i, text in enumerate(texts)]
            nodes = [parse(text, str(file), i) for i, (text, file) in enumerate(zip(texts, files))]
            original = copy.deepcopy(nodes)
            infos, project = [], Project()
            for i, node in enumerate(nodes):
                project.add(node)
                info = self.query(files[:i + 1], path)
                self.assertEqual(info, project.explain(path))
                infos.append(info)
            source = dict(file=str(files[0]), line=3, column=5, input=0)
            self.assertEqual((infos[0]['present'], infos[0]['value'], infos[0]['winningSources']),
                             (True, 'old', [source]))
            removed = infos[1]['events'][-1]
            self.assertEqual((infos[1]['present'], infos[1]['value'], infos[1]['winningSources']),
                             (False, None, []))
            self.assertEqual((removed['previousPresent'], removed['previous'], removed['previousSources']),
                             (True, 'old', [source]))
            self.assertEqual(removed['source'], dict(file=str(files[1]), line=1, column=9, input=1))
            self.assertEqual(infos[2]['events'][:2], infos[1]['events'])
            self.assertEqual([e['action'] for e in infos[2]['events']], ['add', 'override', 'override'])
            self.assertEqual((infos[2]['events'][-1]['previousPresent'],
                              infos[2]['events'][-1]['previousSources']), (False, []))
            self.assertEqual((infos[2]['present'], infos[2]['value'], infos[2]['winningSources']),
                             (True, 'restored', [dict(file=str(files[2]), line=3, column=5, input=2)]))
            self.query(files[:2], '/x-data/0')
            self.call(files[:2], json.dumps('/x-data/' + '8' * 140000).encode(), error='path has no history')
            self.assertEqual(nodes, original)
        self.assertEqual(sys.get_int_max_str_digits(), 4300)

    def test_invalid_encoding_json_and_pointers_fail_closed(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        for data in [b'', b'"unterminated', b'"\\x00"', b'"/x-data" trailing',
                     b'"/x-data" ""', b'null', b'false', b'123', b'NaN', b'[]', b'{}',
                     b'9' * 4301, b'[' * 2000 + b'0' + b']' * 2000, b'\xef\xbb\xbf"/x-data"']:
            with self.subTest(data=data[:40]):
                self.call([base], data, error='JSON string')
        for data in [b'"/x-data/\xff"', b'"/x-data/\xed\xa0\x80"', b'"/x-data/\xf0\x9f']:
            self.call([base], data, error='UTF-8')
        for path, error in [('x-data', 'RFC 6901'), ('/x-data/~2', 'RFC 6901'),
                            ('/x-data/\0', 'path has no history'), ('/x-data/\ud800', 'path has no history')]:
            self.call([base], json.dumps(path).encode(), error=error)

    def test_byte_cap_and_read_failures(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        data = b'""' + b' ' * (MAX_PATH_BYTES - 2)
        self.assertEqual(self.call([base], data)['value'], {'x-data': 'live'})
        self.call([base], data + b' ', error='8388608')
        from stackwrench.compose_merge.cli import main
        # Guard the actual read size, and error conversion through the CLI handler.
        with patch('stackwrench.compose_merge.cli.sys.stdin') as stdin:
            stdin.buffer.read.return_value = b'"/x-data"'
            with patch('stackwrench.compose_merge.cli.sys.stdout', new_callable=io.StringIO):
                self.assertEqual(main(['explain', '-f', str(base), '--path-json-stdin']), 0)
            stdin.buffer.read.assert_called_once_with(MAX_PATH_BYTES + 1)
            stdin.buffer.close.assert_not_called()
        for exc in [OSError('denied'), ValueError('closed stream')]:
            with patch('stackwrench.compose_merge.cli.sys.stdin') as stdin, \
                 patch('stackwrench.compose_merge.cli.sys.stdout', new_callable=io.StringIO) as stdout, \
                 patch('stackwrench.compose_merge.cli.sys.stderr', new_callable=io.StringIO) as stderr:
                stdin.buffer.read.side_effect = exc
                with self.assertRaises(SystemExit) as raised:
                    main(['explain', '-f', str(base), '--path-json-stdin'])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('cannot read path JSON stdin', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())

    def test_opt_in_conflicts_and_help_do_not_read_open_pipe(self):
        base = self.fixture('base.yaml', 'x-data: live\n')
        query = self.fixture('pointer.json', '"/x-data"\n')
        prefix = [*transport.CLI, 'explain', '-f', str(base)]
        cases = [(prefix + ['--help'], 0),
                 ([*transport.CLI, '--version'], 0),
                 ([*transport.CLI, 'render', '-f', str(base)], 0),
                 ([*transport.CLI, 'compare', '--left', str(base), '--right', str(base)], 0),
                 (prefix, 2), (prefix + ['--path-json-stdin', '--help'], 0),
                 (prefix + ['--path-json-stdin', 'extra'], 2)]
        for args in [['--path', '/x-data'], ['--path-json', '"/x-data"'],
                     ['--path-json-file', str(query)]]:
            cases.extend([(prefix + args, 0), (prefix + args + ['--path-json-stdin'], 2),
                          (prefix + ['--path-json-stdin'] + args, 2)])
        for argv, code in cases:
            with self.subTest(argv=argv):
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as process:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        self.fail('CLI waited for stdin without a valid explicit request')
                    stdout, stderr = process.communicate()
                result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                self.record(argv, None, result)
                self.assertEqual(result.returncode, code, stderr.decode())
                self.assertNotIn(b'Traceback', stderr)
                if code == 2:
                    self.assertEqual(stdout, b'')
                else:
                    self.assertEqual(stderr, b'')
        # An explicit regular-file transport still rejects pipe-backed /dev/stdin.
        if Path('/dev/stdin').exists():
            argv = prefix + ['--path-json-file', '/dev/stdin']
            result = subprocess.run(argv, input=b'"/x-data"', capture_output=True, timeout=5)
            self.record(argv, b'"/x-data"', result)
            self.assertEqual((result.returncode, result.stdout), (2, b''))
            self.assertIn(b'regular file', result.stderr)

    def test_input_gates_with_stdin_transport(self):
        base = self.fixture('base.yaml', 'x-data: {"\\0": old}\n')
        data = b'"/x-data/\\u0000"'
        for i, (text, error) in enumerate([
            ('x-data: [new]', 'type change'), ('x-data: !unknown value', 'tag'),
            ('x-data: [!reset null]', 'mapping values'), ('x-data: "${IMAGE}"', 'interpolation'),
            ('x-data: {a: one, a: two}', 'duplicate'), ('x-data: "' + 'a' * (1024 * 1024) + '"', 'exceeds')]):
            later = self.fixture(str(i) + '.yaml', text)
            self.call([base, later], data, error=error)
        resource = self.fixture('resource.yaml', 'services: {web: {ports: [{target: 80, name: old}]}}')
        loss = self.fixture('loss.yaml', 'services: {web: {ports: [80]}}')
        self.call([resource, loss], b'"/services/web/ports/0"', error='omits')


if __name__ == '__main__':
    unittest.main()
