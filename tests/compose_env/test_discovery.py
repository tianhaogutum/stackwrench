"""Syntax-only discovery: required inputs never evaluate, dynamic paths never open."""

import json
import os
from unittest.mock import patch

from .test_trace import ROOT, Workspace


class Discovery(Workspace):
    def test_missing_required_inputs_discover_without_evaluation(self):
        compose = self.compose(
            "      TOKEN_VALUE: '${TOKEN:?private-message}'\n"
            "      EMPTY_VALUE: '${EMPTY?private-message}'\n"
            "      MODE_VALUE: '${MODE:-private-default}'\n"
            "      ALT_VALUE: '${ALT+private-word}|${ALT:+private-word}'\n"
            "      DIRECT: '$TOKEN'\n      PASS:\n      LITERAL: '$$IGNORED'\n")
        output = self.root / 'template.env'
        proc = self.run_cli('--compose', compose, '--discover-template',
                            '--env-template', output, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['report_type'], 'env-template-discovery')
        self.assertEqual(report['evaluation'], 'syntax-only')
        self.assertTrue(report['complete'])
        self.assertFalse(report['values_shown'])
        self.assertNotIn('interpolation', report)
        self.assertNotIn('services', report)  # No purported winners/states.
        from stackwrench.compose_env import discover_template, env_template
        self.assertEqual(report, discover_template(compose))
        template = output.read_text()
        self.assertEqual(template, env_template(report))
        self.assertEqual([line for line in template.splitlines() if line.startswith('# variable:')], [
            '# variable: ALT; required: no', '# variable: EMPTY; required: set (empty allowed)',
            '# variable: MODE; required: no', '# variable: PASS; required: no',
            '# variable: TOKEN; required: nonempty'])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(not line or line.startswith('#') for line in template.splitlines()))
        for forbidden in ['private-', 'IGNORED', 'TOKEN_VALUE=', 'input_status', 'result_status']:
            self.assertNotIn(forbidden, template + proc.stdout)
        # The normal trace still requires inputs and cannot export a partial file.
        output.unlink()
        self.assert_cli_error('--compose', compose, '--env-template', output,
                              forbidden=('private-message',))
        self.assertFalse(output.exists())

    def test_all_contexts_dynamic_paths_not_guessed_even_with_inputs(self):
        compose = self.compose("      PASS:\n      VALUE: '${TOKEN:?private-message}'\n",
            fields="    env_file: ['literal$$file.env', '${FILE:-guessed.env}', '${REQUIRED:?private-path}']\n")
        inputs = self.put('inputs.env', 'FILE=guessed.env\nUNUSED=private-value\nCOPY=${INPUT:?private-message}\n')
        self.put('literal$file.env', "LOCAL=${SERVICE:?private-message}\nLITERAL='$EXCLUDED'\n")
        guessed = self.put('guessed.env', 'INVALID and must never be read\n')
        from stackwrench.compose_env import discover_template, env_template
        from stackwrench.compose_env.trace import _read_text
        reads = []
        def checked_read(path, label):
            reads.append(path)
            self.assertNotEqual(path, guessed)
            return _read_text(path, label)
        with patch('stackwrench.compose_env.trace._read_text', side_effect=checked_read):
            report = discover_template(compose, [inputs])
        self.assertEqual(set(reads), {compose, inputs, self.root / 'literal$file.env'})
        self.assertFalse(report['complete'])
        files = report['service_env_files']
        self.assertEqual([f['status'] for f in files], ['scanned', 'unresolved-dynamic', 'unresolved-dynamic'])
        self.assertEqual(files[1]['field_path'], ['services', 'demo', 'env_file', 1])
        self.assertEqual(files[1]['service'], 'demo')
        self.assertEqual(sorted({r['variable'] for r in report['references']}),
                         ['FILE', 'INPUT', 'PASS', 'REQUIRED', 'SERVICE', 'TOKEN'])
        self.assertEqual({r['phase'] for r in report['references']},
                         {'compose', 'interpolation-input', 'service-env-file'})
        template = env_template(report)
        self.assertIn('Unresolved dynamic env_file', template)
        for forbidden in ['guessed.env', 'private-', 'EXCLUDED', 'UNUSED', 'LITERAL=']:
            self.assertNotIn(forbidden, json.dumps(report) + template)
        proc = self.run_cli('--compose', compose, '--env-file', inputs, '--discover-template')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('unresolved-dynamic', proc.stdout)
        self.assertIn('Syntax-only', proc.stdout)
        # Missing dynamic input also skips; neither fallback nor empty substitution is used.
        self.assertFalse(discover_template(compose)['complete'])

    def test_restricted_syntax_and_static_failures_are_atomic(self):
        for expression in ['${V:-${NESTED}}', '${V:=private-secret}', '${V', '${V:-$OTHER}']:
            with self.subTest(expression=expression):
                compose = self.compose("      REQUIRED: '${MISSING:?private-message}'\n"
                                       f"      BAD: '{expression}'\n")
                output = self.root / 'template.env'
                self.assert_cli_error('--compose', compose, '--discover-template',
                                      '--env-template', output, forbidden=('private-',))
                self.assertFalse(output.exists())
        for contents in [None, 'BARE\n', 'VALUE=${V:-${BAD}}\n']:
            with self.subTest(contents=contents):
                static = self.root / 'static.env'
                static.unlink(missing_ok=True)
                if contents is not None:
                    self.put('static.env', contents)
                compose = self.compose(fields='    env_file: static.env\n')
                self.assert_cli_error('--compose', compose, '--discover-template')

    def test_dynamic_list_declarations_are_redacted_unresolved_scopes(self):
        from stackwrench.compose_env import discover_template, env_template
        compose = self.compose("      - '${KEY:?private-message}=private-literal'\n"
                               "      - '${WHOLE}'\n      - PASS\n"
                               "      - 'VALUE=${TOKEN:?private-message}'\n")
        self.put('.env', 'KEY=private-implicit\nWHOLE=private-implicit\n')
        with patch.dict(os.environ, {'KEY': 'private-host', 'WHOLE': 'private-host'}):
            report = discover_template(compose)
        self.assertFalse(report['complete'])
        self.assertEqual(report['unresolved_environment'], [
            {'service': 'demo', 'field_path': ['services', 'demo', 'environment', index],
             'location': f'{compose}:{line}:9', 'status': 'unresolved-dynamic'}
            for index, line in [(0, 5), (1, 6)]])
        self.assertEqual(sorted(r['variable'] for r in report['references']),
                         ['KEY', 'PASS', 'TOKEN', 'WHOLE'])
        self.assertEqual([r['variable'] for r in report['references']
                          if r['operator'] == 'inherited-key-only'], ['PASS'])
        self.assertNotIn('services', report)
        output = self.root / 'dynamic.template'
        proc = self.run_cli('--compose', compose, '--discover-template',
                            '--env-template', output, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), report)
        template = env_template(report)
        self.assertEqual(output.read_text(), template)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(template.count('# Unresolved dynamic environment declaration;'), 2)
        self.assertTrue(all(not line or line.startswith('#') for line in template.splitlines()))
        for name in ['KEY', 'WHOLE', 'PASS', 'TOKEN']:
            self.assertIn(f'# {name}=', template)
        text = self.run_cli('--compose', compose, '--discover-template')
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout.count('environment unresolved-dynamic'), 2)
        self.assertIn('Discovery incomplete', text.stdout)
        self.assertNotIn('env_file contents were not read', text.stdout)
        for forbidden in ['private-', 'VALUE=', 'input_status', 'result_status', 'branch']:
            self.assertNotIn(forbidden, proc.stdout + template + text.stdout)
        # Required evaluation still fails atomically in ordinary tracing.
        absent = self.root / 'absent.template'
        self.assert_cli_error('--compose', compose, '--env-template', absent,
                              forbidden=('private-',))
        self.assertFalse(absent.exists())
        # Exclusive creation remains enforced for unresolved reports, too.
        self.assert_cli_error('--compose', compose, '--discover-template', '--env-template', output)
        self.assertEqual(output.read_text(), template)
        link = self.root / 'linked.template'
        link.symlink_to(output)
        self.assert_cli_error('--compose', compose, '--discover-template', '--env-template', link)

    def test_list_inheritance_and_dynamic_assignment_names(self):
        from stackwrench.compose_env import discover_template, env_template
        compose = self.compose("      - PASS\n      - 'VALUE=${TOKEN?private-message}'\n")
        report = discover_template(compose)
        self.assertTrue(report['complete'])
        self.assertEqual(report['unresolved_environment'], [])
        template = env_template(report)
        self.assertIn('inherited-key-only', template)
        self.assertIn('# TOKEN=', template)

    def test_dynamic_list_token_boundaries_and_ordinary_trace(self):
        from stackwrench.compose_env import discover_template
        compose = self.compose(
            "      - '${KEY:?private=message}=private-literal'\n"
            "      - '${WHOLE:-private=operand}'\n"
            "      - '${FIRST:-private=operand}${SECOND}=private-literal'\n"
            "      - '$BARE=private-literal'\n"
            "      - 'VALUE=${TOKEN:?private=message}'\n"
            "      - 'ESCAPED=$${IGNORED:-private=operand}|$$ALSO'\n"
            "      - PASS\n")
        report = discover_template(compose)
        self.assertEqual([r['field_path'][-1] for r in report['unresolved_environment']], [0, 1, 2, 3])
        self.assertEqual(sorted({r['variable'] for r in report['references']}),
                         ['BARE', 'FIRST', 'KEY', 'PASS', 'SECOND', 'TOKEN', 'WHOLE'])
        self.assertNotIn('private-', json.dumps(report))
        # Neither explicit values nor fallbacks are evaluated by discovery.
        inputs = self.put('explicit.env', 'KEY=RENAMED\nWHOLE=INHERITED\nFIRST=PRE\n'
                          'SECOND=FIX\nBARE=BARE_OUT\nTOKEN=synthetic-token\n'
                          'INHERITED=synthetic-inherited\nPASS=synthetic-pass\n')
        self.assertEqual(discover_template(compose, [inputs]), report)
        traced = self.result(compose, inputs)
        self.assertNotIn('unresolved_environment', traced)
        self.assertEqual(self.values(traced), {
            'RENAMED': 'private-literal', 'INHERITED': 'synthetic-inherited',
            'PREFIX': 'private-literal', 'BARE_OUT': 'private-literal',
            'VALUE': 'synthetic-token', 'ESCAPED': '${IGNORED:-private=operand}|$ALSO',
            'PASS': 'synthetic-pass'})
        # Whole declarations can also resolve to assignments; discovery cannot guess.
        inputs.write_text(inputs.read_text().replace('WHOLE=INHERITED', 'WHOLE=GENERATED=synthetic-whole'))
        self.assertEqual(self.values(self.result(compose, inputs))['GENERATED'], 'synthetic-whole')
        self.assertEqual(discover_template(compose, [inputs]), report)

    def test_dynamic_list_invalid_syntax_still_fails_atomically(self):
        from stackwrench.compose_env import discover_template, TraceError
        for declaration in [
            '${KEY:-${NESTED}}=private-value', '${KEY:=private-message}=private-value',
            '${KEY:-$OTHER}=private-value', '${KEY=private-message}=private-value',
            '${KEY:?private-message', '${KEY}=VALUE=${BAD:=private-message}',
            '${KEY}=VALUE=${BAD:-${NESTED}}',
            '$${KEY}=private-value', '$$KEY', 'BAD-NAME=private-value', '=private-value',
        ]:
            with self.subTest(declaration=declaration):
                compose = self.compose(f"      - '{declaration}'\n")
                with self.assertRaises(TraceError) as caught:
                    discover_template(compose)
                self.assertNotIn('private-', str(caught.exception))
                output = self.root / 'invalid.template'
                self.assert_cli_error('--compose', compose, '--discover-template',
                                      '--env-template', output, forbidden=('private-',))
                self.assertFalse(output.exists())

    def test_dynamic_list_and_optional_raw_file_scope_completeness(self):
        from stackwrench.compose_env import discover_template
        self.put('raw.env', 'RAW=${NOT_A_REFERENCE:?private-message}\n')
        fields = ('    env_file: [{path: absent.env, required: false}, '
                  '{path: raw.env, format: raw}]\n')
        compose = self.compose("      - '${KEY:?private-message}=private-value'\n", fields=fields)
        report = discover_template(compose)
        self.assertFalse(report['complete'])
        self.assertEqual([r['status'] for r in report['service_env_files']],
                         ['skipped-optional-missing', 'scanned'])
        self.assertEqual([r['variable'] for r in report['references']], ['KEY'])
        self.assertEqual(len(report['unresolved_environment']), 1)
        # Once the environment name is static, optional missing files remain covered.
        compose = self.compose("      - 'VALUE=${TOKEN:?private-message}'\n", fields=fields)
        report = discover_template(compose)
        self.assertTrue(report['complete'])
        self.assertEqual(report['unresolved_environment'], [])
        self.assertEqual([r['variable'] for r in report['references']], ['TOKEN'])

    def test_no_host_or_implicit_inputs_and_comment_injection(self):
        from stackwrench.compose_env import discover_template, env_template
        compose = self.put('compose\nINJECTED=bad.yaml', json.dumps({
            'services': {'demo': {'odd\nFIELD=bad': '${REF:?private-message}',
                                  'env_file': '${FILE:-private-path}',
                                  'environment': ['${NAME:?private-message}=private-value']}}}))
        self.put('.env', 'REF=private-implicit\n')
        with patch.dict(os.environ, {'REF': 'private-host'}):
            before = dict(os.environ)
            report = discover_template(compose)
            self.assertEqual(dict(os.environ), before)
        template = env_template(report)
        self.assertTrue(all(not line or line.startswith('#') for line in template.splitlines()))
        self.assertIn(r'\nINJECTED=bad.yaml', template)
        self.assertEqual(len(report['unresolved_environment']), 1)
        self.assertIn('Unresolved dynamic environment declaration', template)
        self.assertNotIn('private-', template + json.dumps(report))

    def test_cli_conflicts_exclusive_output_and_checked_in_example(self):
        compose = ROOT / 'examples/compose_env/discovery/compose.yaml'
        snapshot = self.put('shell.json', '{}')
        output = self.root / 'template.env'
        for extra in [('--shell-env', snapshot), ('--show-values',)]:
            proc = self.run_cli('--compose', compose, '--discover-template', '--env-template', output, *extra)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, '')
            self.assertFalse(output.exists())
        original = self.put('existing.env', 'KEEP=private-preserve\n')
        link = self.root / 'link.env'
        link.symlink_to(original)
        for target in [original, link, self.root]:
            self.assert_cli_error('--compose', compose, '--discover-template', '--env-template', target)
        self.assertEqual(original.read_text(), 'KEEP=private-preserve\n')
        proc = self.run_cli('--compose', compose, '--discover-template', '--env-template', output, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)['complete'])
        self.assertEqual([line for line in output.read_text().splitlines() if line.startswith('# variable:')], [
            '# variable: EMPTY; required: set (empty allowed)', '# variable: FILE; required: no',
            '# variable: LOCAL; required: nonempty', '# variable: PASS; required: no',
            '# variable: TOKEN; required: nonempty'])
