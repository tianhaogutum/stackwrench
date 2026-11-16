"""Bounded service env_file optional/raw declarations, without host inputs."""

import importlib
import json
from pathlib import Path
from unittest.mock import patch

from stackwrench.compose_env import TraceError, compare_scenarios, discover_template, env_template, trace
from .test_trace import ROOT, Workspace


class EnvFileOptions(Workspace):
    def fixture(self):
        self.put('base.env', 'BASE=base\nOVERRIDE=base\n')
        self.put('raw.env', 'TOKEN=${NOT_FROM_SHELL}\nQUOTED="literal $HOME"\nOVERRIDE=raw\n')
        return self.compose("      OVERRIDE: ''\n", fields=(
            '    env_file:\n      - base.env\n      - path: optional.env\n'
            '        required: false\n      - path: raw.env\n        format: raw\n'))

    def test_optional_raw_and_environment_override_contract(self):
        compose = self.fixture()
        shell = self.put('shell.json', '{"NOT_FROM_SHELL":"private-shell", "HOME":"private-home"}')
        with patch.dict('os.environ', {'NOT_FROM_SHELL': 'private-host', 'HOME': 'private-host'}):
            report = trace(compose, shell_env=shell, show_values=True)
        self.assertEqual(self.values(report), {'BASE': 'base', 'TOKEN': '${NOT_FROM_SHELL}',
                                              'QUOTED': '"literal $HOME"', 'OVERRIDE': ''})
        variables = report['services']['demo']['variables']
        for name in ('TOKEN', 'QUOTED'):
            self.assertEqual(variables[name]['winner']['syntax'], 'raw-literal')
            self.assertEqual(variables[name]['winner']['interpolation_uses'], [])
        self.assertEqual(report['interpolation']['uses'], [])
        self.assertEqual(variables['OVERRIDE']['winner']['source'], 'environment')
        self.assertEqual([e['syntax'] for e in variables['OVERRIDE']['shadowed']], ['assignment', 'raw-literal'])
        files = report['service_env_files']
        self.assertEqual([f['status'] for f in files], ['scanned', 'skipped-optional-missing', 'scanned'])
        self.assertEqual([f['format'] for f in files], ['compose', 'compose', 'raw'])
        self.assertEqual([f['required'] for f in files], [True, False, True])
        self.assertEqual(files[1]['reason'], 'missing')
        self.assertEqual(files[1]['field_path'], ['services', 'demo', 'env_file', 1, 'path'])
        self.assertTrue(files[1]['location'].endswith(':6:15'))
        hidden = trace(compose, shell_env=shell)
        for forbidden in ('private-shell', 'private-home', 'NOT_FROM_SHELL}', 'literal $HOME'):
            self.assertNotIn(forbidden, json.dumps(hidden))
        self.assertNotIn('# NOT_FROM_SHELL=', env_template(hidden))
        proc = self.run_cli('--compose', compose, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), trace(compose))
        proc = self.run_cli('--compose', compose)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('skipped-optional-missing', proc.stdout)
        self.assertIn('raw-literal', proc.stdout)
        self.assertNotIn('literal $HOME', proc.stdout)

    def test_raw_remainder_literal_and_normal_parser_unchanged(self):
        self.put('normal.env', 'LOCAL=normal\nCOPY=${LOCAL}\nQUOTED="$EXTERNAL"\n')
        raw = ('SPACES=  keep spaces  \r\nHASH=value # literal\r\n'
               'QUOTES=\'unclosed\r\nESCAPE="\\q\\n"\r\n'
               'EXPRESSION=${TOKEN:?private-message}\r\nMALFORMED=${BROKEN\r\n'
               'EQUAL=a=b\r\nEMPTY=\r\n # ignored\r\n\r\n')
        self.put('raw.env', raw)
        compose = self.compose('      RESULT: ok\n', fields=(
            '    env_file: [{path: normal.env, required: true}, {path: raw.env, format: raw}]\n'))
        inputs = self.put('inputs.env', 'EXTERNAL=explicit\n')
        report = self.result(compose, inputs)
        values = self.values(report)
        self.assertEqual({n: values[n] for n in ('COPY', 'QUOTED')}, {'COPY': 'normal', 'QUOTED': 'explicit'})
        for name, expected in {'SPACES': '  keep spaces  ', 'HASH': 'value # literal',
                               'QUOTES': "'unclosed", 'ESCAPE': '"\\q\\n"',
                               'EXPRESSION': '${TOKEN:?private-message}', 'MALFORMED': '${BROKEN',
                               'EQUAL': 'a=b', 'EMPTY': ''}.items():
            self.assertEqual(values[name], expected)
        discovery = discover_template(compose)
        self.assertTrue(discovery['complete'])
        self.assertEqual({u['variable'] for u in discovery['references']}, {'LOCAL', 'EXTERNAL'})
        self.assertNotIn('private-message', env_template(discovery))

    def test_strict_options_and_required_missing_fail_atomically(self):
        options = ['{path: missing.env}', '{path: missing.env, required: true}',
                   '{path: missing.env, required: "false"}', '{path: missing.env, required: 0}',
                   '{path: missing.env, required: null}', '{path: missing.env, required: yes}', '{path: missing.env, required: []}',
                   '{path: missing.env, format: private-format, required: false}',
                   '{path: missing.env, format: compose}', '{path: missing.env, format: 1}',
                   '{path: missing.env, format: null}', '{path: missing.env, format: []}',
                   '{path: missing.env, private-option: true}', '{required: false}',
                   '{path: 1}', '{path: null}', '{path: []}', '{path: missing.env, path: other.env}',
                   '{path: ""}', '{path: "https://private-url"}', '{path: "\\0"}']
        for option in options:
            compose = self.compose(fields=f'    env_file: [{option}]\n')
            with self.subTest(option=option):
                for discover in (False, True):
                    with self.assertRaises(TraceError):
                        (discover_template if discover else trace)(compose)
                    target = self.root / 'must-not-exist.template'
                    self.assert_cli_error('--compose', compose, '--env-template', target,
                                          *(['--discover-template'] if discover else []),
                                          forbidden=('private-format', 'private-option', 'private-url'))
                    self.assertFalse(target.exists())
        compose = self.compose(fields='    env_file: {path: missing.env, required: false}\n')
        self.assert_cli_error('--compose', compose)

    def test_optional_does_not_hide_invalid_existing_inputs(self):
        from stackwrench.compose_env.trace import MAX_BYTES
        bad = self.root / 'bad.env'
        cases = [b'\xff', b'X=' + b'a' * MAX_BYTES, b'key-only\n', b'export X=value\n', b'X: value\n']
        for data in cases:
            bad.write_bytes(data)
            for fmt in ('', ', format: raw'):
                compose = self.compose(fields=f'    env_file: [{{path: bad.env, required: false{fmt}}}]\n')
                with self.subTest(data_size=len(data), fmt=fmt):
                    self.assert_cli_error('--compose', compose)
        bad.unlink()
        bad.mkdir()
        self.assert_cli_error('--compose', compose)
        bad.rmdir()
        bad.symlink_to(self.root / 'absent.env')
        # A dangling symlink has no target input, so it follows missing-file semantics.
        self.assertEqual(trace(compose)['service_env_files'][0]['status'], 'skipped-optional-missing')

    def test_optional_inaccessible_file_is_an_error(self):
        bad = self.put('bad.env', 'TOKEN=private-value\n')
        compose = self.compose(fields='    env_file: [{path: bad.env, required: false, format: raw}]\n')
        real_open = Path.open
        def checked_open(path, *args, **kwargs):
            if path == bad:
                raise PermissionError('private-os-error')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', checked_open):
            for analyze in (trace, discover_template):
                with self.assertRaises(TraceError) as caught:
                    analyze(compose)
                self.assertNotIn('private-', str(caught.exception))

    def test_raw_values_feed_normal_files_without_recursive_expansion(self):
        self.put('raw.env', 'TOKEN=${NOT_FROM_SHELL}\nREMOVED=raw\n')
        self.put('normal.env', 'FOLLOW=${TOKEN}\n')
        compose = self.compose('      REMOVED:\n', fields=(
            '    env_file: [{path: raw.env, format: raw}, normal.env]\n'))
        report = self.result(compose)
        self.assertEqual(self.values(report), {'TOKEN': '${NOT_FROM_SHELL}',
                                              'FOLLOW': '${NOT_FROM_SHELL}', 'REMOVED': None})
        uses = report['interpolation']['uses']
        self.assertEqual([use['variable'] for use in uses], ['TOKEN'])
        self.assertEqual(uses[0]['input_source']['syntax'], 'raw-literal')
        self.assertEqual(uses[0]['input_source']['interpolation_uses'], [])
        self.assertEqual(report['services']['demo']['variables']['REMOVED']['winner']['source'], 'environment')
        self.assertNotIn('# NOT_FROM_SHELL=', env_template(report))

    def test_dynamic_mapping_paths_preserve_redaction_discovery_and_selectors(self):
        self.put('selected.env', 'PLAIN=literal $NOT_INPUT\n')
        self.put('other.env', 'PLAIN=literal $NOT_INPUT\n')
        compose = self.compose("      OVERRIDE: ''\n", fields=(
            '    env_file: [{path: "${FILE:-selected.env}", required: false, format: raw}]\n'))
        supplied = self.put('inputs.env', 'FILE=selected.env\n')
        with patch('stackwrench.compose_env.trace._read_text', wraps=importlib.import_module('stackwrench.compose_env.trace')._read_text) as reader:
            discovery = discover_template(compose, [supplied])
        self.assertEqual([call.args[0] for call in reader.call_args_list], [supplied, compose])
        self.assertFalse(discovery['complete'])
        self.assertEqual(discovery['service_env_files'][0]['status'], 'unresolved-dynamic')
        self.assertEqual({u['variable'] for u in discovery['references']}, {'FILE'})
        manifest = self.put('scenarios.json', json.dumps({'base': {}, 'explicit': {'env_files': ['inputs.env']},
                                                        'other': {'env_files': ['other-input.env']},
                                                        'missing': {'env_files': ['missing-input.env']}}))
        self.put('other-input.env', 'FILE=other.env\n')
        self.put('missing-input.env', 'FILE=private-missing.env\n')
        report = compare_scenarios(compose, manifest)
        change = report['comparisons']['explicit']['services']['demo']['variables']['PLAIN']
        self.assertEqual(change, {'baseline_status': 'set', 'scenario_status': 'set',
                                  'source_changed': False, 'resolution_changed': False,
                                  'value_changed': False, 'path_selection_changed': True,
                                  'dependency_path_selection_changed': False})
        self.assertTrue(report['comparisons']['other']['services']['demo']['variables']['PLAIN']['source_changed'])
        self.assertEqual(report['scenarios']['missing']['service_env_files'][0]['status'], 'skipped-optional-missing')
        self.assertNotIn('private-missing.env', json.dumps(report))
        # Ordinary evaluation still fails a required path expression before optional-file skipping.
        compose = self.compose(fields='    env_file: [{path: "${FILE:?private-message}", required: false}]\n')
        self.assert_cli_error('--compose', compose, forbidden=('private-message',))
        self.assertFalse(discover_template(compose)['complete'])

    def test_raw_mapping_dependency_selector_is_distinct(self):
        self.put('selected.env', 'PLAIN=${LITERAL_RAW}\n')
        self.put('stable.env', 'FOLLOW=${PLAIN}\n')
        compose = self.compose("      OVERRIDE: ''\n", fields=(
            '    env_file: [{path: "${FILE:-selected.env}", required: false, format: raw}, stable.env]\n'))
        self.put('explicit.json', '{"FILE":"selected.env"}')
        manifest = self.put('scenarios.json', json.dumps({
            'base': {}, 'explicit': {'shell_env': 'explicit.json'}, 'identical': {}}))
        report = compare_scenarios(compose, manifest)
        for scenario, comparison in report['comparisons'].items():
            for variable, change in comparison['services']['demo']['variables'].items():
                self.assertEqual(change, {
                    'baseline_status': 'empty' if variable == 'OVERRIDE' else 'set',
                    'scenario_status': 'empty' if variable == 'OVERRIDE' else 'set',
                    'source_changed': False, 'resolution_changed': False, 'value_changed': False,
                    'path_selection_changed': scenario == 'explicit' and variable == 'PLAIN',
                    'dependency_path_selection_changed': scenario == 'explicit' and variable == 'FOLLOW',
                })
        self.assertNotIn('LITERAL_RAW', json.dumps(report))

    def test_checked_in_example(self):
        compose = ROOT / 'examples/compose_env/envfile-options/compose.yaml'
        report = trace(compose, show_values=True)
        self.assertEqual(self.values(report, 'app'), {'BASE': 'base', 'TOKEN': '${NOT_FROM_SHELL}',
                                                    'QUOTED': '"literal $HOME"', 'OVERRIDE': ''})
        discovery = discover_template(compose)
        self.assertTrue(discovery['complete'])
        self.assertEqual(discovery['references'], [])
