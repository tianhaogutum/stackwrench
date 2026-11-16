"""Opt-in discovery coverage gate, without resolving or opening dynamic scopes."""

import json
from unittest.mock import patch

from .test_trace import Workspace


class Completeness(Workspace):
    def test_complete_cli_gate_preserves_report_and_private_export(self):
        compose = self.compose("      RESULT: '${TOKEN:?private-message}'\n")
        default = self.run_cli('--compose', compose, '--discover-template', '--json')
        self.assertEqual(default.returncode, 0, default.stderr)
        output = self.root / 'complete.template'
        proc = self.run_cli('--compose', compose, '--discover-template', '--require-complete',
                            '--env-template', output, '--json')
        # Prior CLI rejects the missing opt-in gate as an unknown argument.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), json.loads(default.stdout))
        self.assertTrue(json.loads(proc.stdout)['complete'])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertIn('# TOKEN=', output.read_text())
        self.assertNotIn('private-', output.read_text() + proc.stdout)
        self.assertTrue(all(not line or line.startswith('#') for line in output.read_text().splitlines()))

    def incomplete_fixture(self, *, environment=True, file=True):
        env = ("      - '${KEY:?private-message}=private-value'\n      - '${WHOLE}'\n"
               if environment else "      - 'VALUE=${TOKEN:?private-message}'\n")
        compose = self.compose(env, fields=("    env_file: '${FILE:-unopened.env}'\n" if file else ''))
        target = self.put('unopened.env', 'MALFORMED private-dynamic-data\n')
        supplied = self.put('inputs/explicit.env',
                            'FILE=unopened.env\nKEY=private-name\nWHOLE=private-name=private-value\n')
        return compose, supplied, target

    def test_single_gate_rejects_each_unresolved_scope_without_dynamic_io(self):
        from stackwrench.compose_env import TraceError, discover_template, env_template
        from stackwrench.compose_env.trace import _read_text
        from pathlib import Path
        original_stat = Path.stat
        for environment, file in [(True, False), (False, True), (True, True)]:
            for supplied_input in [False, True]:
                with self.subTest(environment=environment, file=file, supplied=supplied_input):
                    compose, supplied, _ = self.incomplete_fixture(environment=environment, file=file)
                    files = [supplied] if supplied_input else []
                    expected_reads = [*files, compose]
                    reads, stats = [], []
                    def read(path, label):
                        reads.append(path)
                        self.assertIn(path, expected_reads)
                        return _read_text(path, label)
                    def stat(path, *args, **kwargs):
                        stats.append(path)
                        self.assertIn(path, expected_reads)
                        return original_stat(path, *args, **kwargs)
                    default = discover_template(compose, files)
                    self.assertFalse(default['complete'])
                    self.assertEqual(default, discover_template(compose, files, require_complete=False))
                    self.assertEqual(bool(default['unresolved_environment']), environment)
                    self.assertEqual(bool(default['service_env_files']), file)
                    with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                            patch.object(Path, 'stat', autospec=True, side_effect=stat):
                        with self.assertRaises(TraceError) as caught:
                            discover_template(compose, files, require_complete=True)
                    self.assertEqual(str(caught.exception),
                        'syntax-only discovery incomplete: dynamic file contents or '
                        'environment declaration names remain unresolved')
                    self.assertEqual(reads, expected_reads)
                    self.assertEqual(stats, expected_reads)
                    output = self.root / 'blocked.template'
                    flags = ['--env-file', supplied] if supplied_input else []
                    for format_flags in [[], ['--json']]:
                        proc = self.run_cli('--compose', compose, '--discover-template', '--require-complete',
                                            '--env-template', output, *flags, *format_flags)
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        self.assertEqual(proc.stdout, '')
                        self.assertIn(str(caught.exception), proc.stderr)
                        self.assertNotIn('private-', proc.stderr)
                        self.assertNotIn('unopened.env', proc.stderr)
                        self.assertFalse(output.exists())
                        if format_flags:
                            self.assertEqual(json.loads(proc.stderr)['error']['code'], 'invalid-or-unsupported-input')
                    # The opt-in gate does not remove default incomplete export support.
                    output = self.root / 'default.template'
                    proc = self.run_cli('--compose', compose, '--discover-template', '--env-template', output,
                                        '--json', *flags)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(json.loads(proc.stdout), default)
                    self.assertEqual(output.read_text(), env_template(default))
                    output.unlink()

    def test_named_gate_reads_all_selected_in_order_then_rejects_atomically(self):
        from stackwrench.compose_env import TraceError, discover_scenarios
        from stackwrench.compose_env.trace import _read_text
        from pathlib import Path
        compose, supplied, _ = self.incomplete_fixture()
        second = self.put('inputs/second.env', 'COPY=${UPSTREAM:?private-message}\n')
        manifest = self.put('inputs/scenarios.json', json.dumps({
            'first': {}, 'supplied': {'env_files': [supplied.name, second.name]},
            'broken': {'env_files': ['missing.env']}}))
        expected = [manifest, compose, supplied, second, compose]
        reads, stats = [], []
        original_stat = Path.stat
        def read(path, label):
            reads.append(path)
            self.assertIn(path, expected)
            return _read_text(path, label)
        def stat(path, *args, **kwargs):
            stats.append(path)
            self.assertIn(path, expected)
            return original_stat(path, *args, **kwargs)
        names = ['supplied', 'first']
        with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                patch.object(Path, 'stat', autospec=True, side_effect=stat):
            with self.assertRaisesRegex(TraceError, 'syntax-only discovery incomplete'):
                discover_scenarios(compose, manifest, scenario_names=names, require_complete=True)
        self.assertEqual(reads, expected)
        self.assertEqual(stats, expected)
        self.assertEqual(names, ['supplied', 'first'])
        default = discover_scenarios(compose, manifest, scenario_names=names)
        self.assertEqual(list(default['scenarios']), ['first', 'supplied'])
        self.assertFalse(default['complete'])
        self.assertEqual(default, discover_scenarios(compose, manifest, scenario_names=names, require_complete=False))
        output = self.root / 'blocked.template'
        self.assert_cli_error('--compose', compose, '--scenario-file', manifest, '--discover-template',
                              '--scenario', 'supplied', '--scenario', 'first', '--require-complete',
                              '--env-template', output, forbidden=('private-', 'unopened.env', 'missing.env'))
        self.assertFalse(output.exists())
        # No explicit selection means all children: gate does not hide later missing-input errors.
        message = self.assert_cli_error('--compose', compose, '--scenario-file', manifest, '--discover-template',
                                        '--require-complete', '--env-template', output, forbidden=('private-',))
        self.assertIn('scenario broken:', message)
        self.assertNotIn('discovery incomplete', message)
        self.assertFalse(output.exists())

    def test_complete_selected_literal_optional_raw_scopes_succeed(self):
        from stackwrench.compose_env import discover_scenarios, discover_template, env_template
        from stackwrench.compose_env.trace import _read_text
        from pathlib import Path
        compose = self.compose("      PASS:\n      VALUE: '${TOKEN:?private-message}'\n",
            fields='    env_file: [literal.env, {path: absent.env, required: false}, {path: raw.env, format: raw}]\n')
        literal = self.put('literal.env', 'COPY=${LOCAL:?private-message}\n')
        raw = self.put('raw.env', 'RAW=${NOT_PARSED:=private-data}\n')
        supplied = self.put('inputs/explicit.env', 'COPY=${UPSTREAM:?private-message}\n')
        manifest = self.put('inputs/scenarios.json', json.dumps({
            'broken': {'env_files': ['missing.env']}, 'z_last': {'env_files': [supplied.name]}, 'a_first': {}}))
        expected = [manifest, supplied, compose, literal, self.root / 'absent.env', raw,
                    compose, literal, self.root / 'absent.env', raw]
        reads, stats = [], []
        original_stat = Path.stat
        def read(path, label):
            reads.append(path)
            self.assertIn(path, expected)
            return _read_text(path, label)
        def stat(path, *args, **kwargs):
            stats.append(path)
            self.assertIn(path, expected)
            return original_stat(path, *args, **kwargs)
        names = ['a_first', 'z_last']
        with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                patch.object(Path, 'stat', autospec=True, side_effect=stat):
            report = discover_scenarios(compose, manifest, scenario_names=names, require_complete=True)
        self.assertEqual(reads, expected)
        self.assertEqual(stats, expected)
        self.assertTrue(report['complete'])
        self.assertEqual(list(report['scenarios']), ['z_last', 'a_first'])
        self.assertEqual(report, discover_scenarios(compose, manifest, scenario_names=names))
        self.assertTrue(discover_template(compose, require_complete=True)['complete'])
        for child in report['scenarios'].values():
            self.assertEqual([r['status'] for r in child['service_env_files']],
                             ['scanned', 'skipped-optional-missing', 'scanned'])
            self.assertNotIn('NOT_PARSED', {r['variable'] for r in child['references']})
        for format_flags in [[], ['--json']]:
            output = self.root / 'complete-selected.template'
            proc = self.run_cli('--compose', compose, '--scenario-file', manifest, '--discover-template',
                                '--scenario', 'a_first', '--scenario', 'z_last', '--require-complete',
                                '--env-template', output, *format_flags)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(output.read_text(), env_template(report))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            if format_flags:
                self.assertEqual(json.loads(proc.stdout), report)
            else:
                self.assertLess(proc.stdout.index('Scenario z_last'), proc.stdout.index('Scenario a_first'))
            self.assertNotIn('private-', output.read_text() + proc.stdout)
            output.unlink()
        # With all children selected and complete, omission also succeeds.
        manifest.write_text('{"one":{},"two":{}}')
        self.assertTrue(discover_scenarios(compose, manifest, require_complete=True)['complete'])

    def test_strict_preflight_and_selected_input_errors_precede_gate(self):
        from stackwrench.compose_env import TraceError, discover_scenarios, discover_template
        from stackwrench.compose_env.trace import _read_text
        compose, supplied, _ = self.incomplete_fixture()
        manifest = self.put('inputs/scenarios.json', '{"first":{},"other":{}}')
        for content, names in [
            ('{"first":{},"other":{"shell_env":"private-shell.json"}}', ['first']),
            ('{"first":{},"other":{"extra":"private-data"}}', ['first']),
            ('{"first":{},"other":{}}', ['private-unknown']),
            ('{"first":{},"other":{}}', ['first', 'first']),
        ]:
            with self.subTest(content=content, names=names):
                manifest.write_text(content)
                reads = []
                def read(path, label):
                    reads.append(path)
                    return _read_text(path, label)
                with patch('stackwrench.compose_env.trace._read_text', side_effect=read):
                    with self.assertRaises(TraceError) as caught:
                        discover_scenarios(compose, manifest, scenario_names=names, require_complete=True)
                self.assertEqual(reads, [manifest])
                self.assertNotIn('private-', str(caught.exception))
                self.assertNotIn('discovery incomplete', str(caught.exception))
        manifest.write_text('{"first":{},"later":{"env_files":["bad.env"]}}')
        for content in [None, 'MALFORMED private-data\n', 'COPY=${BAD:=private-data}\n']:
            if content is not None:
                self.put('inputs/bad.env', content)
            with self.assertRaises(TraceError) as caught:
                discover_scenarios(compose, manifest, require_complete=True)
            self.assertIn('scenario later:', str(caught.exception))
            self.assertNotIn('discovery incomplete', str(caught.exception))
            self.assertNotIn('private-', str(caught.exception))
        # A skipped dynamic file never prevents strict reading of a later literal file.
        compose = self.compose("      - '${KEY}'\n",
            fields="    env_file: ['${FILE:-unopened.env}', literal.env]\n")
        for content in [None, 'MALFORMED private-data\n']:
            if content is not None:
                self.put('literal.env', content)
            with self.assertRaises(TraceError) as caught:
                discover_template(compose, require_complete=True)
            self.assertNotIn('discovery incomplete', str(caught.exception))
            self.assertNotIn('private-', str(caught.exception))

    def test_option_validation_and_ordinary_modes_reject_gate_before_reads(self):
        from stackwrench.compose_env import TraceError, discover_scenarios, discover_template, trace, compare_scenarios
        compose = self.root / 'private-missing-compose.yaml'
        manifest = self.root / 'private-missing-manifest.json'
        for value in [None, '', 'private-value', 0, 1, [], {}, object()]:
            for function, args in [(discover_template, (compose,)), (discover_scenarios, (compose, manifest))]:
                with self.subTest(function=function.__name__, value=value):
                    with patch('stackwrench.compose_env.trace._read_text') as read:
                        with self.assertRaisesRegex(TraceError, 'require_complete must be a boolean'):
                            function(*args, require_complete=value)
                        read.assert_not_called()
        for function, args in [(trace, (compose,)), (compare_scenarios, (compose, manifest))]:
            with self.assertRaises(TypeError):
                function(*args, require_complete=True)
        from stackwrench.compose_env.cli import main
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        output = self.root / 'blocked.template'
        for flags in [[], ['--scenario-file', str(manifest)], ['--show-values']]:
            stdout, stderr = StringIO(), StringIO()
            with patch('stackwrench.compose_env.cli.trace') as trace_call, \
                    patch('stackwrench.compose_env.cli.compare_scenarios') as compare_call, \
                    patch('stackwrench.compose_env.trace._read_text') as read, \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    main(['--compose', str(compose), '--require-complete', '--json',
                          '--env-template', str(output), *flags])
                self.assertEqual(caught.exception.code, 2)
                trace_call.assert_not_called()
                compare_call.assert_not_called()
                read.assert_not_called()
            self.assertEqual(stdout.getvalue(), '')
            self.assertNotIn('private-', stderr.getvalue())
            self.assertFalse(output.exists())
        compose = self.compose()
        for flags in [['--show-values'], ['--shell-env', 'private-path.json']]:
            proc = self.run_cli('--compose', compose, '--discover-template', '--require-complete', *flags)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, '')
            self.assertNotIn('private-', proc.stderr)

    def test_gate_and_complete_exports_preserve_existing_paths(self):
        complete = self.compose("      RESULT: '${TOKEN:?private-message}'\n")
        incomplete = self.put('dynamic.yaml', "services:\n  demo:\n    environment: ['${KEY}']\n")
        manifest = self.put('scenarios.json', '{"one":{},"two":{}}')
        existing = self.put('existing.template', 'KEEP=private-preserve\n')
        link = self.root / 'link.template'
        link.symlink_to(existing)
        dangling = self.root / 'dangling.template'
        dangling.symlink_to(self.root / 'absent.template')
        for compose in [complete, incomplete]:
            for scenario_flags in [[], ['--scenario-file', manifest, '--scenario', 'two']]:
                for target in [existing, link, dangling, self.root, self.root / 'missing' / 'new.template']:
                    self.assert_cli_error('--compose', compose, '--discover-template', '--require-complete',
                                          '--env-template', target, *scenario_flags, forbidden=('private-',))
        self.assertEqual(existing.read_text(), 'KEEP=private-preserve\n')
        self.assertTrue(link.is_symlink())
        self.assertTrue(dangling.is_symlink())
        self.assertFalse((self.root / 'absent.template').exists())
        self.assertFalse((self.root / 'missing').exists())
