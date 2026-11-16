"""Exact DOT bytes, full-source refusal and inert, handled delivery."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from stackwrench.just_bridge import cli

CLI = [sys.executable, '-m', 'stackwrench', 'just']
FIXTURES = Path(__file__).parent / 'fixtures'


class GraphTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jtb-graph-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / 'justfile'
        self.raw = (FIXTURES / 'graph.justfile').read_bytes()
        self.source.write_bytes(self.raw)

    def run_cli(self, *args, **kwargs):
        return subprocess.run([*CLI, *map(str, args)], cwd=self.directory,
                              stderr=subprocess.PIPE, timeout=5, **kwargs)

    def graph(self, *args):
        return self.run_cli('graph', self.source, *args, stdout=subprocess.PIPE)

    def assert_inputs_only(self):
        self.assertEqual(set(self.directory.iterdir()), {self.source})
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_full_graph_golden_and_stable_bytes(self):
        expected = (FIXTURES / 'graph.dot').read_bytes()
        for _ in range(2):
            result = self.graph()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
            self.assertEqual(result.stderr, b'')
        self.assert_inputs_only()

    def test_canonical_bytes_ignore_stdout_encoding(self):
        for encoding in ['ascii', 'utf-16', 'utf-8-sig']:
            with self.subTest(encoding=encoding):
                result = self.run_cli('graph', self.source, stdout=subprocess.PIPE,
                                      env=dict(os.environ, PYTHONIOENCODING=encoding))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, (FIXTURES / 'graph.dot').read_bytes())

    def test_multiple_roots_share_conversion_closure_and_deduplicate(self):
        args = ['--recipe', 'test', '--recipe', 'build', '--recipe', 'test']
        result = self.graph(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, (FIXTURES / 'graph-selected.dot').read_bytes())
        converted = self.run_cli('convert', self.source, *args, stdout=subprocess.PIPE)
        self.assertEqual(converted.returncode, 0, converted.stderr)
        self.assertEqual(list(json.loads(converted.stdout)['tasks']), ['build', 'test', 'common', 'shared'])
        # Root request ordering cannot change the graph's source ordering.
        self.assertEqual(result.stdout, self.graph('--recipe', 'build', '--recipe', 'test').stdout)
        self.assert_inputs_only()

    def test_leaves_keywords_and_exact_allowed_names(self):
        names = ['default', 'graph', 'node', 'edge', 'digraph', 'strict', 'subgraph', 'Graph', 'a', 'Z9_-']
        self.raw = ('\n'.join(name + ':' for name in names) + '\n').encode()
        self.source.write_bytes(self.raw)
        expected = ('digraph recipes {\n' + ''.join(f'  "{name}";\n' for name in names) + '}\n').encode()
        result = self.graph()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        for name in names:
            with self.subTest(name=name):
                selected = self.graph('--recipe', name)
                self.assertEqual(selected.returncode, 0, selected.stderr)
                self.assertEqual(selected.stdout, f'digraph recipes {{\n  "{name}";\n}}\n'.encode())
        self.assert_inputs_only()

    def test_unknown_empty_and_inexact_selections_emit_nothing(self):
        for name in ['', 'missing', 'Build', ' build', 'build ', 'build test']:
            with self.subTest(name=name):
                result = self.graph('--recipe', 'build', '--recipe', name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b'')
                self.assertIn(b'[recipe-selection]', result.stderr)
        self.assert_inputs_only()

    def test_omitted_invalid_source_is_always_refused_before_selection(self):
        cases = [
            (b'good:\nother: missing\n', b'missing-dependency'),
            (b'good:\nleft: right\nright: left\n', b'dependency-cycle'),
            (b'good:\nother: other\n', b'dependency-cycle'),
            (b"good:\nimport 'not-read.just'\n", b'unsupported-syntax'),
            (b'good:\nother:\n  echo {{missing}}\n', b'unsupported-interpolation'),
            (b'good:\nother:\n  @echo quiet\n', b'command-prefix'),
            (b'good:\ndefault:\n', b'default-conflict'),
            (b'good:\ngood:\n', b'duplicate-recipe'),
            (b'good:\n_private:\n', b'private-recipe'),
            (b'good:\n' + b''.join(f'r{i}:\n'.encode() for i in range(512)), b'recipe-limit'),
            (b'good:\nx := "' + b'x' * 8192 + b'"\nother:\n  echo ' + b'{{x}}' * 129 + b'\n',
             b'unsupported-interpolation'),
            (b'', b'no-recipes'),
            (b'# comments only\n', b'no-recipes'),
        ]
        for raw, code in cases:
            self.raw = raw
            self.source.write_bytes(raw)
            for roots in [[], ['--recipe', 'good'], ['--recipe', 'missing']]:
                with self.subTest(code=code, roots=roots):
                    result = self.graph(*roots)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, b'')
                    self.assertIn(b'[' + code + b']', result.stderr)
                    self.assertNotIn(b'[recipe-selection]', result.stderr)
            self.assert_inputs_only()

    def test_source_name_escapes_remain_unsupported(self):
        for name in ['a"b', 'a\\b', 'a:b', 'café', '9start', 'a.b']:
            with self.subTest(name=name):
                self.raw = ('good:\n' + name + ':\n').encode()
                self.source.write_bytes(self.raw)
                result = self.graph('--recipe', 'good')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b'')
        self.assert_inputs_only()

    def test_source_byte_budget_original_bytes_and_input_failures(self):
        self.raw = '# café🚀\r\nbuild: leaf\r\nleaf:\r\n'.encode()
        self.source.write_bytes(self.raw)
        result = self.graph('--recipe', 'build', '--max-source-bytes', '000'+str(len(self.raw)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'digraph recipes {\n  "build";\n  "leaf";\n  "leaf" -> "build";\n}\n')
        overflow = self.graph('--recipe', 'missing', '--max-source-bytes', str(len(self.raw)-1))
        self.assertEqual(overflow.returncode, 2)
        self.assertEqual(overflow.stdout, b'')
        self.assertIn(b'[input-limit]', overflow.stderr)
        self.assertNotIn(b'[recipe-selection]', overflow.stderr)
        self.source.write_bytes(b'\xff')
        invalid = self.graph('--recipe', 'missing')
        self.assertEqual(invalid.returncode, 3)
        self.assertEqual(invalid.stdout, b'')
        self.assertIn(b'[input-error]', invalid.stderr)
        self.source.unlink()
        for path in [self.source, self.directory]:
            result = self.run_cli('graph', path, '--recipe', 'missing', stdout=subprocess.PIPE)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, b'')
            self.assertIn(b'[input-error]', result.stderr)

    @unittest.skipUnless(os.name == 'posix', 'POSIX FIFO/symlink')
    def test_symlink_and_fifo_descriptor_contract(self):
        link = self.directory / 'link'
        link.symlink_to(self.source)
        result = self.run_cli('graph', link, stdout=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, (FIXTURES / 'graph.dot').read_bytes())
        link.unlink()
        self.source.unlink()
        os.mkfifo(self.source)
        result = self.graph()
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'[input-error]', result.stderr)

    def test_invalid_budget_and_graph_options_fail_before_load(self):
        for value in ['0', '-1', '+1', ' 1', '1.0', '1e3', '١', '1048577']:
            with self.subTest(value=value), mock.patch.object(cli, '_load') as load:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main(['graph', str(self.source), '--max-source-bytes', value])
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()
        for flag in ['--json', '--output', '--report']:
            result = self.graph(flag)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b'')
        self.assert_inputs_only()

    def test_graph_does_not_render_taskfile_report_or_execute(self):
        from stackwrench.just_bridge import core, render_dot, analyze
        with mock.patch.object(core, 'render_taskfile', side_effect=AssertionError('Taskfile rendering')), \
                mock.patch.object(core.Analysis, 'report', side_effect=AssertionError('analysis report')), \
                mock.patch('subprocess.run', side_effect=AssertionError('external execution')):
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(['graph', str(self.source)])
            self.assertEqual(code, 0)
            self.assertEqual(capture.getvalue().encode(), (FIXTURES / 'graph.dot').read_bytes())
            with self.assertRaisesRegex(ValueError, 'unsupported input'):
                render_dot(analyze('bad: missing\n'))
        self.assert_inputs_only()

    def test_injected_delivery_failures_and_rejected_graph_never_touches_stdout(self):
        for operation in ['reconfigure', 'write', 'flush']:
            for error in [OSError('sink'), BrokenPipeError(), ValueError('closed'),
                          UnicodeError('encoding'), RuntimeError('sink')]:
                with self.subTest(operation=operation, error=type(error)):
                    sink = mock.Mock(spec=['reconfigure', 'write', 'flush'])
                    getattr(sink, operation).side_effect = error
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(stderr):
                        code = cli.main(['graph', str(self.source)])
                        self.assertIsNone(sys.stdout)
                    self.assertEqual(code, 3)
                    self.assertIn('<stdout>: error [output-error]:', stderr.getvalue())
                    getattr(sink, operation).assert_called_once()
        for raw, expected in [(b'bad: missing\n', 2), (b'\xff', 3)]:
            self.source.write_bytes(raw)
            sink = mock.Mock(spec=['write', 'flush'])
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(['graph', str(self.source)])
            self.assertEqual(code, expected)
            sink.write.assert_not_called()
            sink.flush.assert_not_called()

    @unittest.skipUnless(os.name == 'posix' and Path('/dev/full').exists(), 'POSIX full device')
    def test_real_full_device_and_closed_pipe_have_handled_exit(self):
        for large in [False, True]:
            if large:
                self.raw = b''.join(f'very_long_recipe_name_{i}:\n'.encode() for i in range(512))
                self.source.write_bytes(self.raw)
            with self.subTest(large=large), open('/dev/full', 'wb', buffering=0) as sink:
                result = self.run_cli('graph', self.source, stdout=sink)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(b'<stdout>: error [output-error]:', result.stderr)
                self.assertNotIn(b'Exception ignored', result.stderr)
                self.assertNotIn(b'Traceback', result.stderr)
        reader, writer = os.pipe()
        os.close(reader)
        try:
            result = self.run_cli('graph', self.source, stdout=writer)
        finally:
            os.close(writer)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b'[output-error]', result.stderr)
        self.assertNotIn(b'Exception ignored', result.stderr)
        self.assert_inputs_only()
