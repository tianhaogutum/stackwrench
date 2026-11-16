"""Explicit CLI discovery preserves the Python first-event path contract."""
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import call, patch

from . import test_cli_path_json as transport
from . import test_cli_paths_stdin as batch
from stackwrench.compose_merge import MergeError, Project, merge_files


class HistoryPathsCliTests(unittest.TestCase):
    setUp = transport.JsonPathCliTests.setUp
    fixture = batch.JsonPathsStdinCliTests.fixture

    def discover(self, files, *args, error=None):
        return transport.JsonPathCliTests.call(self, files, *args,
                                               command='history-paths', error=error)

    def test_installed_nul_example_exact_array(self):
        files = ['examples/compose_merge/nul-key.yaml', 'examples/compose_merge/type-change-next.yaml']
        before = [Path(f).read_bytes() for f in files]
        expected = ['/x-data/\0', '/x-data', '', '/x-data/0']
        self.assertEqual(self.discover(files), expected)
        self.assertEqual(merge_files(files).history_paths(), expected)
        self.assertEqual([Path(f).read_bytes() for f in files], before)

    def test_exact_tokens_long_numeric_removal_and_readd(self):
        keys = ['a/b~c', '~1', '\0', r'\u0000', '雪😀', '', '0', '00', '١',
                '9' * 4301, '0' * 4301]
        # Explicit keys avoid YAML's 1024-character implicit-key restriction.
        mapping = '\n'.join('  ? ' + json.dumps(k, ensure_ascii=False) + '\n  : old' for k in keys)
        base = self.fixture('tokens.yaml', 'x-data:\n' + mapping)
        removed = self.fixture('removed.yaml', 'x-data: !override [new]')
        restored = self.fixture('restored.yaml', 'x-data: !override\n' +
                                mapping.replace(': old', ': restored'))
        expected = ['/x-data/' + k.replace('~', '~0').replace('/', '~1') for k in keys]
        expected += ['/x-data', '']
        for files in [[base], [base, removed], [base, removed, restored]]:
            with self.subTest(files=files):
                self.assertEqual(self.discover(files), expected)
                project = merge_files(files)
                self.assertEqual(project.history_paths(), expected)
                self.assertEqual([project.explain(p)['path'] for p in expected], expected)
        self.assertFalse(merge_files([base, removed]).explain('/x-data/\0')['present'])
        self.assertEqual(merge_files([base, removed, restored]).explain('/x-data/\0')['value'],
                         'restored')

    def test_empty_root_nested_paths_reset_and_first_event_order(self):
        empty = self.fixture('empty.yaml', '{}\n')
        self.assertEqual(self.discover([empty]), [''])
        base = self.fixture('base.yaml', 'x-data: {z: old, a: old}')
        reset = self.fixture('reset.yaml', 'x-data: !override {gone: !reset null}')
        readd = self.fixture('readd.yaml', 'x-data: {a: restored, later: new}')
        expected = ['/x-data/z', '/x-data/a', '/x-data', '', '/x-data/gone', '/x-data/later']
        files = [base, base, reset, readd]
        self.assertEqual(self.discover(files), expected)
        project = merge_files(files)
        self.assertEqual(project.history_paths(), expected)
        self.assertFalse(project.explain('/x-data/gone')['present'])
        self.assertFalse(project.explain('/x-data/gone')['events'][0]['previousPresent'])
        self.assertFalse(project.explain('/x-data/z')['present'])
        self.assertEqual(project.explain('/x-data/a')['value'], 'restored')
        nested = self.fixture('nested.yaml', 'x-data: {"a/b~c": {"": {"雪😀\\0": [old, null]}}}')
        paths = ['/x-data/a~1b~0c//雪😀\0', '/x-data/a~1b~0c//雪😀\0/0',
                 '/x-data/a~1b~0c//雪😀\0/1', '/x-data/a~1b~0c/', '/x-data/a~1b~0c',
                 '/x-data', '']
        self.assertEqual(self.discover([nested]), paths)
        self.assertEqual(merge_files([nested]).history_paths(), paths)

    def test_more_than_64_paths_unsorted_and_explain_chunks_still_bounded(self):
        keys = ['key' + str(i) for i in range(129, -1, -1)]
        file = self.fixture('many.yaml', 'x-data: ' + json.dumps(dict.fromkeys(keys, 'value')))
        expected = ['/x-data/' + k for k in keys] + ['/x-data', '']
        paths = self.discover([file])
        self.assertEqual(paths, expected)
        project = merge_files([file])
        self.assertEqual(paths, project.history_paths())
        self.assertEqual([len(paths[i:i + 64]) for i in range(0, len(paths), 64)], [64, 64, 4])
        for start in range(0, len(paths), 64):
            chunk = paths[start:start + 64]
            self.assertEqual(project.explain_many(chunk),
                             {'explanations': [project.explain(p) for p in chunk]})
        for invalid in [[], paths[:65]]:
            with self.assertRaises(MergeError):
                project.explain_many(invalid)

    def test_merge_once_only_explicit_files_without_pointer_io_or_queries(self):
        from stackwrench.compose_merge import cli
        files = [self.fixture('one.yaml', 'x-data: {z: old}'),
                 self.fixture('two.yaml', 'x-data: {a: new}')]
        expected = merge_files(files).history_paths()
        argv = ['history-paths', '--file', str(files[0]), '-f', str(files[1])]
        with patch.object(cli, 'merge_files', wraps=cli.merge_files) as merge, \
             patch('pathlib.Path.open', autospec=True, side_effect=Path.open) as opened, \
             patch.object(cli.os, 'open', side_effect=AssertionError('pointer file read')), \
             patch.object(cli.json, 'loads', side_effect=AssertionError('JSON input decoding')), \
             patch.object(Project, 'explain', side_effect=AssertionError('per-path lookup')), \
             patch.object(Project, 'explain_many', side_effect=AssertionError('batch lookup')), \
             patch.object(Project, 'render', side_effect=AssertionError('render lookup')), \
             patch.object(cli.sys, 'stdin') as stdin, \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(argv), 0)
            output = stdout.getvalue()
            merge.assert_called_once_with([str(f) for f in files])
            self.assertEqual(opened.call_args_list, [call(f, 'rb') for f in files])
            self.assertEqual(stdin.mock_calls, [])
        self.assertEqual(json.loads(output), expected)

    def test_arguments_and_open_stdin_pipe_never_read(self):
        file = self.fixture('base.yaml', 'x-data: live')
        prefix = [*transport.CLI, 'history-paths', '-f', str(file)]
        cases = [(prefix, 0), (prefix + ['--help'], 0),
                 ([*transport.CLI, 'history-paths'], 2),
                 ([*transport.CLI, 'history-paths', '-f'], 2),
                 (prefix + ['extra'], 2), (prefix + ['--unknown'], 2)]
        for args in [['--path', '/x-data'], ['--path-json', '"/x-data"'],
                     ['--paths-json', '["/x-data"]'], ['--path-json-file', 'missing.json'],
                     ['--paths-json-file', 'missing.json'], ['--path-json-stdin'],
                     ['--paths-json-stdin']]:
            cases.append((prefix + args, 2))
        for argv, code in cases:
            with self.subTest(argv=argv):
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as process:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        self.fail('history-paths read stdin')
                    stdout, stderr = process.communicate()
                batch.JsonPathsStdinCliTests.record(self, argv, None,
                    subprocess.CompletedProcess(argv, process.returncode, stdout, stderr))
                self.assertEqual(process.returncode, code, stderr.decode())
                self.assertNotIn(b'Traceback', stderr)
                self.assertEqual(stdout if code == 2 else stderr, b'')
                if argv == prefix:
                    self.assertEqual(json.loads(stdout), ['/x-data', ''])

    def test_explicit_literal_dash_file_without_inferred_compose(self):
        from stackwrench.compose_merge import cli
        import os
        self.fixture('-', 'x-data: literal-dash')
        self.fixture('compose.yaml', 'unsupported: ignored')
        with patch.object(cli.sys, 'stdin') as stdin, \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
            previous = Path.cwd()
            try:
                os.chdir(self.directory)
                self.assertEqual(cli.main(['history-paths', '-f', '-']), 0)
            finally:
                os.chdir(previous)
            self.assertEqual(json.loads(stdout.getvalue()), ['/x-data', ''])
            self.assertEqual(stdin.mock_calls, [])

    def test_input_failures_are_atomic_and_keep_existing_gates(self):
        base = self.fixture('base.yaml', 'x-data: {"\\0": old}')
        for i, (text, error) in enumerate([
            ('x-data: [new]', 'type change'), ('x-data: !unknown value', 'tag'),
            ('x-data: [!reset null]', 'mapping values'),
            ('x-data: "${IMAGE}"', 'interpolation'),
            ('x-data: {a: one, a: two}', 'duplicate'),
            ('x-data: &a value', 'anchors'), ('unsupported: value', 'unsupported'),
            ('', 'empty YAML'), ('x-data: [', 'YAML'),
            ('x-data: ' + '[' * 70 + '0' + ']' * 70, 'depth'),
            ('x-data: "' + 'a' * (1024 * 1024) + '"', 'exceeds')]):
            with self.subTest(error=error):
                self.discover([base, self.fixture(str(i) + '.yaml', text)], error=error)
        for file in [self.directory / 'missing.yaml', self.directory]:
            self.discover([base, file], error='cannot read')
        bad = self.directory / 'invalid-utf8.yaml'
        bad.write_bytes(b'x-data: "\xff"')
        self.discover([base, bad], error='UTF-8')
        self.assertEqual(bad.read_bytes(), b'x-data: "\xff"')
        self.discover([base] * 33, error='1..32 files')
        resource = self.fixture('resource.yaml', 'services: {web: {ports: [{target: 80, name: old}]}}')
        loss = self.fixture('loss.yaml', 'services: {web: {ports: [80]}}')
        self.discover([resource, loss], error='omits')

    def test_budget_failures_and_argument_errors_precede_output(self):
        from stackwrench.compose_merge import cli, core

        def rejects(files, message):
            with patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout, \
                 patch.object(cli.sys, 'stderr', new_callable=io.StringIO) as stderr, \
                 patch.object(Project, 'history_paths', side_effect=AssertionError('partial discovery')):
                self.assertEqual(cli.main(['history-paths', *[a for f in files for a in ('-f', str(f))]]), 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn(message, stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())

        base = self.fixture('base.yaml', 'x-data: {a: old, b: old}')
        with patch.object(core, 'MAX_NODES', 3):
            rejects([base], 'depth/node limit')
        small_files = [self.fixture('small' + str(i), f'x-{i}: ' + 'z' * 38) for i in range(5)]
        with patch.object(core, 'MAX_BYTES', 50):
            rejects(small_files, 'combined input exceeds 4 MiB')
        # Synthetic parsed nodes isolate the unchanged combined-node gate.
        node = core.parse('x-data: [0]', 'synthetic.yaml')
        node.value['x-data'].value *= 34000
        with patch.object(core, 'parse', return_value=node):
            rejects([base] * 3, 'combined input exceeds 100000 nodes')
        record = Project.record

        def reach_event_limit(project, *args, **kwargs):
            if len(project.events) == 4:
                project.events.extend([None] * (100000 - len(project.events)))
            return record(project, *args, **kwargs)

        later = self.fixture('later.yaml', 'x-data: {a: changed}')
        with patch.object(Project, 'record', reach_event_limit):
            rejects([base, later], 'trace event limit (100000)')
        with patch.object(cli, 'merge_files', side_effect=AssertionError('merge before validation')), \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout, \
             patch.object(cli.sys, 'stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                cli.main(['history-paths', '-f', 'missing.yaml', '--paths-json-stdin'])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
