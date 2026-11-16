"""Explicit project trace exports the existing complete chronological events."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import call, patch

from . import test_cli_path_json as transport
from . import test_cli_paths_stdin as batch
from . import test_cli_history_paths as discovery
from stackwrench.compose_merge import Project, merge_files
from stackwrench.compose_merge.core import parse


def nul_contract():
    """Hand-written full-event contract, independent of the merge implementation."""
    def source(file, column, index):
        return dict(file=file, line=1, column=column, input=index)
    old = 'examples/compose_merge/nul-key.yaml'
    new = 'examples/compose_merge/type-change-next.yaml'
    leaf, mapping, root = [source(old, c, 0) for c in (16, 9, 1)]
    override, next_root = [source(new, c, 1) for c in (9, 1)]
    def event(order, path, action, src, prior_sources, was, prior, present, value, reason):
        return dict(order=order, path=path, action=action, source=src,
                    previousSources=prior_sources, previousPresent=was, previous=prior,
                    present=present, value=value, reason=reason)
    reason = '!override bypasses normal merge rules'
    return {'events': [
        event(1, '/x-data/\0', 'add', leaf, [], False, None, True, 'old', 'new declaration'),
        event(2, '/x-data', 'add', mapping, [], False, None, True, {'\0': 'old'}, 'new mapping'),
        event(3, '', 'add', root, [], False, None, True, {'x-data': {'\0': 'old'}}, 'new mapping'),
        event(4, '/x-data', 'override', override, [mapping, leaf], True, {'\0': 'old'}, True, ['new'], reason),
        event(5, '/x-data/\0', 'override', override, [leaf], True, 'old', False, None, reason),
        event(6, '/x-data/0', 'override', override, [], False, None, True, 'new', reason),
        event(7, '', 'merge', next_root, [root, mapping, leaf], True,
              {'x-data': {'\0': 'old'}}, True, {'x-data': ['new']}, 'mapping keys merge recursively'),
    ]}


class TraceCliTests(unittest.TestCase):
    setUp = transport.JsonPathCliTests.setUp
    fixture = batch.JsonPathsStdinCliTests.fixture

    def trace(self, files, *args, error=None):
        return transport.JsonPathCliTests.call(self, files, *args, command='trace', error=error)

    def test_installed_full_nul_events_exact_contract(self):
        files = ['examples/compose_merge/nul-key.yaml', 'examples/compose_merge/type-change-next.yaml']
        before = [Path(f).read_bytes() for f in files]
        actual = self.trace(files)
        self.assertEqual(actual, nul_contract())
        self.assertEqual(actual, {'events': merge_files(files).events})
        self.assertEqual([Path(f).read_bytes() for f in files], before)

    def test_empty_root_mapping_exact_contract(self):
        file = self.fixture('empty.yaml', '{}\n')
        self.assertEqual(self.trace([file]), {'events': [dict(
            order=1, path='', action='add',
            source=dict(file=str(file), line=1, column=1, input=0), previousSources=[],
            previousPresent=False, previous=None, present=True, value={}, reason='new mapping')]})

    def test_retain_reset_removal_readd_cross_path_chronology_and_sources(self):
        texts = ['x-data: old\n', 'x-data: null\n', 'x-data: !reset null\n',
                 'x-data: restored\n', 'x-data: !override [new]\n']
        files = [self.fixture(str(i) + '.yaml', t) for i, t in enumerate(texts)]
        nodes = [parse(t, str(f), i) for i, (t, f) in enumerate(zip(texts, files))]
        original = copy.deepcopy(nodes)
        project = Project()
        for i, node in enumerate(nodes):
            project.add(node)
            events = self.trace(files[:i + 1])['events']
            self.assertEqual(events, project.events)
            self.assertEqual([e['order'] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual([e['path'] for e in events],
                         ['/x-data', '', '/x-data', '', '/x-data', '', '/x-data', '',
                          '/x-data', '/x-data/0', ''])
        path_events = [e for e in events if e['path'] == '/x-data']
        self.assertEqual([(e['action'], e['previousPresent'], e['previous'], e['present'], e['value'])
                          for e in path_events], [
            ('add', False, None, True, 'old'), ('retain', True, 'old', True, 'old'),
            ('reset', True, 'old', False, None), ('add', False, None, True, 'restored'),
            ('override', True, 'restored', True, ['new'])])
        src = lambda i: dict(file=str(files[i]), line=1, column=9, input=i)
        self.assertEqual([e['source'] for e in path_events], [src(i) for i in range(5)])
        self.assertEqual([e['previousSources'] for e in path_events],
                         [[], [src(0)], [src(0)], [], [src(3)]])
        self.assertEqual(project.explain('/x-data/0')['winningSources'],
                         [dict(file=str(files[4]), line=1, column=20, input=4)])
        self.assertEqual(nodes, original)

    def test_exact_tokens_long_numeric_nested_removal_readd_and_no_truncation(self):
        keys = ['a/b~c', '~1', '\0', r'\u0000', '雪😀', '', '0', '00', '١',
                '9' * 4301, '0' * 4301] + ['key' + str(i) for i in range(70, -1, -1)]
        mapping = '\n'.join('  ? ' + json.dumps(k, ensure_ascii=False) + '\n  : old' for k in keys)
        texts = ['x-data:\n' + mapping + '\n', 'x-data: !override [new]\n',
                 'x-data: !override\n' + mapping.replace(': old', ': restored') + '\n']
        files = [self.fixture(str(i) + '.yaml', t) for i, t in enumerate(texts)]
        nodes = [parse(t, str(f), i) for i, (t, f) in enumerate(zip(texts, files))]
        original = copy.deepcopy(nodes)
        project = Project()
        paths = ['/x-data/' + k.replace('~', '~0').replace('/', '~1') for k in keys]
        for i, node in enumerate(nodes):
            project.add(node)
            events = self.trace(files[:i + 1])['events']
            self.assertEqual(events, project.events)
            self.assertEqual([e['path'] for e in events[:len(keys)]], paths)
            self.assertGreater(len(events), 64)
            for j, path in enumerate(paths):
                history = [e for e in events if e['path'] == path]
                if i == 0:
                    self.assertEqual((history[-1]['present'], history[-1]['value']), (True, 'old'))
                elif i == 1 and keys[j] != '0':
                    event = history[-1]
                    self.assertEqual((event['previousPresent'], event['previous'], event['present'], event['value']),
                                     (True, 'old', False, None))
                    self.assertEqual(event['previousSources'], [dict(file=str(files[0]),
                        line=3 + j * 2, column=5, input=0)])
                elif i == 2:
                    self.assertEqual((history[-1]['present'], history[-1]['value']), (True, 'restored'))
                    self.assertEqual(project.explain(path)['winningSources'], [dict(
                        file=str(files[2]), line=3 + j * 2, column=5, input=2)])
        self.assertEqual(nodes, original)
        nested = self.fixture('nested.yaml', 'x-data: {"a/b~c": {"": {"雪😀\\0": [old, null]}}}')
        actual = self.trace([nested])['events']
        self.assertEqual(actual, merge_files([nested]).events)
        self.assertEqual([e['path'] for e in actual], ['/x-data/a~1b~0c//雪😀\0',
            '/x-data/a~1b~0c//雪😀\0/0', '/x-data/a~1b~0c//雪😀\0/1',
            '/x-data/a~1b~0c/', '/x-data/a~1b~0c', '/x-data', ''])

    def test_one_merge_explicit_files_no_query_decode_pointer_io_or_stdin(self):
        from stackwrench.compose_merge import cli
        files = [self.fixture('base.yaml', 'x-data: old'), self.fixture('next.yaml', 'x-other: new')]
        expected = {'events': merge_files(files).events}
        with patch.object(cli, 'merge_files', wraps=cli.merge_files) as merge, \
             patch('pathlib.Path.open', autospec=True, side_effect=Path.open) as opened, \
             patch.object(cli.os, 'open', side_effect=AssertionError('pointer file read')), \
             patch.object(cli.json, 'loads', side_effect=AssertionError('JSON input decode')), \
             patch.object(Project, 'explain', side_effect=AssertionError('per-path query')), \
             patch.object(Project, 'explain_many', side_effect=AssertionError('batch query')), \
             patch.object(Project, 'history_paths', side_effect=AssertionError('deduplication')), \
             patch.object(Project, 'render', side_effect=AssertionError('render query')), \
             patch.object(cli.sys, 'stdin') as stdin, \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(['trace', '--file', str(files[0]), '-f', str(files[1])]), 0)
            output = stdout.getvalue()
            merge.assert_called_once_with([str(f) for f in files])
            self.assertEqual(opened.call_args_list, [call(f, 'rb') for f in files])
            self.assertEqual(stdin.mock_calls, [])
        self.assertEqual(json.loads(output), expected)

    def test_arguments_help_and_open_stdin_pipe_never_read(self):
        file = self.fixture('base.yaml', 'x-data: live')
        prefix = [*transport.CLI, 'trace', '-f', str(file)]
        cases = [(prefix, 0), (prefix + ['--help'], 0),
                 ([*transport.CLI, 'trace'], 2), ([*transport.CLI, 'trace', '-f'], 2),
                 (prefix + ['extra'], 2), (prefix + ['--unknown'], 2)]
        for args in [['--path', '/x-data'], ['--path-json', '"/x-data"'],
                     ['--paths-json', '["/x-data"]'], ['--path-json-file', 'missing.json'],
                     ['--paths-json-file', 'missing.json'], ['--path-json-stdin'],
                     ['--paths-json-stdin'], ['--sort'], ['--filter', '/x-data']]:
            cases.append((prefix + args, 2))
        for argv, code in cases:
            with self.subTest(argv=argv):
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as process:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        self.fail('trace waited for stdin')
                    stdout, stderr = process.communicate()
                batch.JsonPathsStdinCliTests.record(self, argv, None,
                    subprocess.CompletedProcess(argv, process.returncode, stdout, stderr))
                self.assertEqual(process.returncode, code, stderr.decode())
                self.assertNotIn(b'Traceback', stderr)
                self.assertEqual(stdout if code == 2 else stderr, b'')
                if argv == prefix:
                    self.assertEqual(json.loads(stdout), {'events': merge_files([file]).events})

    def test_literal_dash_file_and_no_default_compose_inference(self):
        from stackwrench.compose_merge import cli
        self.fixture('-', 'x-data: literal-dash')
        self.fixture('compose.yaml', 'unsupported: ignored')
        previous = Path.cwd()
        try:
            os.chdir(self.directory)
            expected = {'events': merge_files(['-']).events}
            with patch.object(cli.sys, 'stdin') as stdin, \
                 patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
                self.assertEqual(cli.main(['trace', '-f', '-']), 0)
                self.assertEqual(json.loads(stdout.getvalue()), expected)
                self.assertEqual(stdin.mock_calls, [])
        finally:
            os.chdir(previous)

    # Same concrete invalid YAML/merge inputs must also fail before trace output.
    discover = trace
    test_input_failures_are_atomic_and_keep_existing_gates = (
        discovery.HistoryPathsCliTests.test_input_failures_are_atomic_and_keep_existing_gates)

    def test_budget_failures_and_argument_errors_before_output(self):
        from stackwrench.compose_merge import cli, core
        def rejects(files, message):
            with patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout, \
                 patch.object(cli.sys, 'stderr', new_callable=io.StringIO) as stderr:
                self.assertEqual(cli.main(['trace', *[a for f in files for a in ('-f', str(f))]]), 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn(message, stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())
        base = self.fixture('base.yaml', 'x-data: {a: old, b: old}')
        with patch.object(core, 'MAX_NODES', 3):
            rejects([base], 'depth/node limit')
        files = [self.fixture('small' + str(i), f'x-{i}: ' + 'z' * 38) for i in range(5)]
        with patch.object(core, 'MAX_BYTES', 50):
            rejects(files, 'combined input exceeds 4 MiB')
        node = core.parse('x-data: [0]', 'synthetic.yaml')
        node.value['x-data'].value *= 34000
        with patch.object(core, 'parse', return_value=node):
            rejects([base] * 3, 'combined input exceeds 100000 nodes')
        record = Project.record
        def reach_limit(project, *args, **kwargs):
            if len(project.events) == 4:
                project.events.extend([None] * (100000 - len(project.events)))
            return record(project, *args, **kwargs)
        later = self.fixture('later.yaml', 'x-data: {a: changed}')
        with patch.object(Project, 'record', reach_limit):
            rejects([base, later], 'trace event limit (100000)')
        with patch.object(cli, 'merge_files', side_effect=AssertionError('merge before argument validation')), \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout, \
             patch.object(cli.sys, 'stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                cli.main(['trace', '-f', 'missing.yaml', '--paths-json-stdin'])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
