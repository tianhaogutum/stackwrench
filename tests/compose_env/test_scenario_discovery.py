"""Named discovery is syntax-only, independent, redacted and atomic."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from .test_trace import ROOT, Workspace


class ScenarioDiscovery(Workspace):
    def fixture(self):
        compose = self.compose(
            "      - '${KEY:?private-message}=private-literal'\n"
            "      - '${WHOLE}'\n      - PASS\n"
            "      - 'VALUE=${TOKEN:?private-message}'\n",
            fields="    env_file: '${TOKEN:-unopened.env}'\n")
        self.put('unopened.env', 'MALFORMED private-content must stay unread\n')
        self.put('inputs/explicit.env', 'COPY=${UPSTREAM:?private-message}\nTOKEN=unopened.env\n')
        manifest = self.put('inputs/scenarios.json', json.dumps({
            'baseline': {}, 'supplied': {'env_files': ['explicit.env']}}))
        return compose, manifest

    def discover(self, compose, manifest):
        from stackwrench.compose_env import discover_scenarios
        return discover_scenarios(compose, manifest)

    def test_selected_supplied_cli_report_and_checklist(self):
        compose, manifest = self.fixture()
        definitions = json.loads(manifest.read_text())
        definitions['broken'] = {'env_files': ['missing.env']}
        manifest.write_text(json.dumps(definitions))
        output = self.root / 'selected.template'
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest,
                            '--discover-template', '--scenario', 'supplied',
                            '--env-template', output, '--json')
        # Prior implementation has no explicit selection CLI/API.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(list(report['scenarios']), ['supplied'])
        self.assertEqual(report['report_type'], 'scenario-template-discovery')
        self.assertFalse(report['complete'])
        child = report['scenarios']['supplied']
        self.assertFalse(child['complete'])
        self.assertEqual({r['variable'] for r in child['references']},
                         {'KEY', 'WHOLE', 'PASS', 'TOKEN', 'UPSTREAM'})
        self.assertEqual(len(child['unresolved_environment']), 2)
        self.assertEqual([r['status'] for r in child['service_env_files']], ['unresolved-dynamic'])
        from stackwrench.compose_env import discover_scenarios, discover_template, env_template
        self.assertEqual(report, discover_scenarios(compose, manifest, scenario_names=['supplied']))
        self.assertEqual(child, discover_template(compose, [manifest.parent / 'explicit.env']))
        template = output.read_text()
        self.assertEqual(template, env_template(report))
        self.assertEqual(template.count('# Scenario '), 1)
        self.assertIn('# Scenario supplied; complete: false', template)
        self.assertIn('# UPSTREAM=', template)
        self.assertTrue(all(not line or line.startswith('#') for line in template.splitlines()))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        text = self.run_cli('--compose', compose, '--scenario-file', manifest,
                            '--discover-template', '--scenario', 'supplied')
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertTrue(text.stdout.startswith('Scenario supplied\n'))
        self.assertEqual(text.stdout.count('Discovery incomplete'), 1)
        for forbidden in ['private-', 'unopened.env', 'COPY=', 'VALUE=', 'winner', 'comparisons',
                          'baseline_status', '_changed', 'input_status', 'result_status', '"value":',
                          'branch', 'Scenario baseline', 'Scenario broken']:
            self.assertNotIn(forbidden, proc.stdout + template + text.stdout)

    def test_cli_named_discovery_contract_and_separate_export(self):
        compose, manifest = self.fixture()
        output = self.root / 'checklist.env'
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest,
                            '--discover-template', '--env-template', output, '--json')
        # Fails on the prior CLI's intentional discovery/scenario conflict.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(set(report), {'schema_version', 'report_type', 'evaluation',
                         'values_shown', 'host_environment', 'implicit_dotenv', 'complete', 'scenarios'})
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['report_type'], 'scenario-template-discovery')
        self.assertEqual(report['evaluation'], 'syntax-only')
        self.assertFalse(report['values_shown'])
        self.assertEqual(report['host_environment'], 'not-read')
        self.assertEqual(report['implicit_dotenv'], 'not-read')
        self.assertFalse(report['complete'])
        self.assertEqual(list(report['scenarios']), ['baseline', 'supplied'])
        from stackwrench.compose_env import discover_template, env_template
        self.assertEqual(report, self.discover(compose, manifest))
        for name, files in [('baseline', []), ('supplied', [manifest.parent / 'explicit.env'])]:
            child = report['scenarios'][name]
            self.assertEqual(child, discover_template(compose, files))
            self.assertFalse(child['complete'])
            self.assertEqual(set(r['variable'] for r in child['references']),
                             {'KEY', 'WHOLE', 'PASS', 'TOKEN'} | ({'UPSTREAM'} if files else set()))
            self.assertEqual([r['field_path'] for r in child['unresolved_environment']],
                             [['services', 'demo', 'environment', i] for i in [0, 1]])
            self.assertTrue(all(set(r) == {'service', 'field_path', 'location', 'status'}
                                and r['status'] == 'unresolved-dynamic'
                                for r in child['unresolved_environment']))
            self.assertEqual(child['service_env_files'], [{
                'service': 'demo', 'location': f'{compose}:4:15',
                'field_path': ['services', 'demo', 'env_file'], 'required': True,
                'format': 'compose', 'status': 'unresolved-dynamic'}])
        template = output.read_text()
        self.assertEqual(template, env_template(report))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(not line or line.startswith('#') for line in template.splitlines()))
        sections = template.split('# Scenario ')[1:]
        self.assertEqual(len(sections), 2)
        for name, section, extra in [('baseline', sections[0], []), ('supplied', sections[1], ['UPSTREAM'])]:
            self.assertTrue(section.startswith(f'{name}; complete: false\n'))
            self.assertEqual([line.split(';')[0].removeprefix('# variable: ')
                              for line in section.splitlines() if line.startswith('# variable: ')],
                             sorted(['KEY', 'WHOLE', 'PASS', 'TOKEN', *extra]))
            self.assertEqual(section.count('# Unresolved dynamic environment declaration;'), 2)
            self.assertEqual(section.count('# Unresolved dynamic env_file;'), 1)
        text = self.run_cli('--compose', compose, '--scenario-file', manifest, '--discover-template')
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertLess(text.stdout.index('Scenario baseline'), text.stdout.index('Scenario supplied'))
        self.assertEqual(text.stdout.count('Discovery incomplete'), 2)
        for forbidden in ['private-', 'unopened.env', 'COPY=', 'VALUE=', 'winner', 'comparisons',
                          'baseline_status', '_changed', 'input_status', 'result_status', '"value":', 'branch']:
            self.assertNotIn(forbidden, proc.stdout + template + text.stdout)

    def test_selection_reads_only_selected_inputs_in_manifest_order(self):
        compose, manifest = self.fixture()
        self.put('inputs/next.env', 'SECOND=$ZED\n')
        self.put('inputs/malformed.env', 'MALFORMED private-unselected\n')
        definitions = json.loads(manifest.read_text())
        definitions['supplied']['env_files'].append('next.env')
        definitions['broken'] = {'env_files': ['missing.env', 'malformed.env']}
        manifest.write_text(json.dumps(definitions))
        self.put('.env', 'MALFORMED private-implicit\n')
        self.put('inputs/.env', 'MALFORMED private-implicit\n')
        from stackwrench.compose_env import discover_scenarios
        from stackwrench.compose_env.trace import _read_text
        reads, stats = [], []
        allowed = [manifest, compose, manifest.parent / 'explicit.env', manifest.parent / 'next.env', compose]
        original_stat = Path.stat
        def read(path, label):
            reads.append(path)
            self.assertIn(path, allowed)
            return _read_text(path, label)
        def stat(path, *args, **kwargs):
            stats.append(path)
            self.assertIn(path, allowed)
            return original_stat(path, *args, **kwargs)
        names = ['supplied', 'baseline']
        with patch.dict(os.environ, {'UPSTREAM': 'private-host', 'TOKEN': 'unopened.env'}):
            before = dict(os.environ)
            with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                    patch.object(Path, 'stat', autospec=True, side_effect=stat):
                report = discover_scenarios(compose, manifest, scenario_names=names)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(names, ['supplied', 'baseline'])
        self.assertEqual(reads, allowed)
        self.assertEqual(stats, allowed)
        self.assertEqual(list(report['scenarios']), ['baseline', 'supplied'])
        self.assertEqual([r['variable'] for r in report['scenarios']['supplied']['references']][:2],
                         ['UPSTREAM', 'ZED'])
        self.assertNotIn('UPSTREAM', {r['variable'] for r in report['scenarios']['baseline']['references']})
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest, '--discover-template',
                            '--scenario', 'supplied', '--scenario', 'baseline', '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), report)

    def test_selection_preflight_rejects_invalid_names_and_api_shapes(self):
        from stackwrench.compose_env import TraceError, discover_scenarios
        from stackwrench.compose_env.trace import _read_text
        compose, manifest = self.fixture()
        selections = [[], '', 'supplied', ('supplied',), {}, [None], [1], [True], [[]], [{}],
                      [''], ['private-unknown'], ['UNKNOWN'], ['supplied', 'supplied'],
                      ['supplied', 'UNKNOWN'], ['supplied', ''], ['bad\nprivate-name']]
        for names in selections:
            with self.subTest(names=names):
                reads = []
                def read(path, label):
                    reads.append(path)
                    return _read_text(path, label)
                with patch('stackwrench.compose_env.trace._read_text', side_effect=read):
                    with self.assertRaises(TraceError) as caught:
                        discover_scenarios(compose, manifest, scenario_names=names)
                self.assertEqual(reads, [manifest])
                self.assertNotIn('private-', str(caught.exception))
                self.assertNotIn('UNKNOWN', str(caught.exception))
        output = self.root / 'invalid.template'
        for flags in [('--scenario', ''), ('--scenario', 'UNKNOWN'),
                      ('--scenario', 'supplied', '--scenario', 'supplied'),
                      ('--scenario', 'supplied', '--scenario', 'private-unknown'),
                      ('--scenario', 'baseline,supplied')]:
            self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                  '--discover-template', '--env-template', output, *flags,
                                  forbidden=('UNKNOWN', 'private-unknown', 'baseline,supplied'))
            self.assertFalse(output.exists())

    def test_unselected_manifest_definitions_and_shell_snapshots_still_fail(self):
        from stackwrench.compose_env import TraceError, discover_scenarios
        from stackwrench.compose_env.trace import _read_text
        compose, manifest = self.fixture()
        invalid = [
            '{"supplied":{},"other":{},"other":{}}', '{"supplied":{}}',
            '{"supplied":{},"bad-name":{}}', '{"supplied":{},"other":null}',
            '{"supplied":{},"other":{"extra":"private-value"}}',
            '{"supplied":{},"other":{"env_files":"private-value"}}',
            '{"supplied":{},"other":{"env_files":[""]}}',
            '{"supplied":{},"other":{"env_files":["\\u0000private-path"]}}',
            '{"supplied":{},"other":{"shell_env":null}}',
            '{"supplied":{},"other":{"shell_env":"missing-private.json"}}',
            '{"supplied":{},"other":{"shell_env":"empty.json"}}',
        ]
        self.put('inputs/empty.json', '{}')
        output = self.root / 'invalid.template'
        for content in invalid:
            with self.subTest(content=content):
                manifest.write_text(content)
                reads = []
                def read(path, label):
                    reads.append(path)
                    return _read_text(path, label)
                with patch('stackwrench.compose_env.trace._read_text', side_effect=read):
                    with self.assertRaises(TraceError) as caught:
                        discover_scenarios(compose, manifest, scenario_names=['supplied'])
                self.assertEqual(reads, [manifest])
                self.assertNotIn('private-', str(caught.exception))
                self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                      '--discover-template', '--scenario', 'supplied',
                                      '--env-template', output, forbidden=('private-',))
                self.assertFalse(output.exists())

    def test_selection_completeness_and_selected_failures_are_atomic(self):
        from stackwrench.compose_env import TraceError, discover_scenarios
        manifest = self.put('scenarios.json', '{"broken":{"env_files":["bad.env"]},"complete":{},"also":{}}')
        # All selected children complete, though an unselected input is missing.
        compose = self.compose("      VALUE: '${TOKEN:?private-message}'\n")
        report = discover_scenarios(compose, manifest, scenario_names=['complete'])
        self.assertTrue(report['complete'])
        self.assertTrue(report['scenarios']['complete']['complete'])
        self.assertEqual(list(report['scenarios']), ['complete'])
        # A later selected failure must discard the earlier successful child.
        manifest.write_text('{"complete":{},"broken":{"env_files":["bad.env"]},"also":{}}')
        output = self.root / 'invalid.template'
        for content in [None, 'MALFORMED private-data\n', 'COPY=${BAD:=private-message}\n']:
            with self.subTest(content=content):
                if content is not None:
                    self.put('bad.env', content)
                with self.assertRaisesRegex(TraceError, 'scenario broken:'):
                    discover_scenarios(compose, manifest, scenario_names=['broken', 'complete'])
                self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                      '--discover-template', '--scenario', 'broken', '--scenario', 'complete',
                                      '--env-template', output, forbidden=('private-',))
                self.assertFalse(output.exists())
        existing = self.put('existing.template', 'KEEP=private-preserve\n')
        link = self.root / 'link.template'
        link.symlink_to(existing)
        for target in [existing, link]:
            self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                  '--discover-template', '--scenario', 'complete', '--env-template', target)
        self.assertEqual(existing.read_text(), 'KEEP=private-preserve\n')
        self.assertTrue(link.is_symlink())

    def test_selection_cli_requires_named_discovery(self):
        compose, manifest = self.fixture()
        output = self.root / 'invalid.template'
        cases = [(), ('--discover-template',), ('--scenario-file', manifest),
                 ('--scenario-file', manifest, '--discover-template', '--show-values'),
                 ('--scenario-file', manifest, '--discover-template', '--shell-env', 'private-path'),
                 ('--scenario-file', manifest, '--discover-template', '--env-file', 'private-path')]
        for flags in cases:
            proc = self.run_cli('--compose', compose, '--scenario', 'private-name',
                                '--env-template', output, *flags)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, '')
            self.assertNotIn('private-', proc.stderr)
            self.assertFalse(output.exists())

    def test_only_explicit_inputs_and_never_dynamic_targets_or_host(self):
        compose, manifest = self.fixture()
        self.put('.env', 'MALFORMED private-implicit\n')
        self.put('inputs/.env', 'MALFORMED private-implicit\n')
        from stackwrench.compose_env.trace import _read_text
        reads = []
        def read(path, label):
            reads.append(path)
            self.assertNotEqual(path, self.root / 'unopened.env')
            return _read_text(path, label)
        with patch.dict(os.environ, {'TOKEN': 'unopened.env', 'UPSTREAM': 'private-host'}):
            before = dict(os.environ)
            with patch('stackwrench.compose_env.trace._read_text', side_effect=read):
                report = self.discover(compose, manifest)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(reads, [manifest, compose, manifest.parent / 'explicit.env', compose])
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest, '--discover-template', '--json',
                            env={**os.environ, 'TOKEN': 'unopened.env', 'UPSTREAM': 'private-host'})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), report)

    def test_manifest_and_shell_restrictions_validate_before_input_reads(self):
        from stackwrench.compose_env import TraceError
        compose, manifest = self.fixture()
        invalid = [
            '{"one":{},"one":{}}', '{"one":{},"two":{"env_files":[],"env_files":[]}}',
            '{"one":{}}', '{"bad-name":{},"two":{}}',
            '{"one":{},"two":{"extra":"private-value"}}',
            '{"one":{},"two":{"env_files":"private-value"}}',
            '{"one":{},"two":{"env_files":[""]}}',
            '{"one":{},"two":{"env_files":["\\u0000private-path"]}}',
            '{"one":{},"two":{"shell_env":"missing-private.json"}}',
            '{"one":{},"two":{"shell_env":null}}',
            'private malformed JSON', json.dumps({f's{i}': {} for i in range(17)}),
        ]
        from stackwrench.compose_env.trace import _read_text
        for content in invalid:
            with self.subTest(content=content):
                manifest.write_text(content)
                reads = []
                def read(path, label):
                    reads.append(path)
                    return _read_text(path, label)
                with patch('stackwrench.compose_env.trace._read_text', side_effect=read):
                    with self.assertRaises(TraceError) as caught:
                        self.discover(compose, manifest)
                self.assertEqual(reads, [manifest])
                self.assertNotIn('private-', str(caught.exception))
                output = self.root / 'invalid.template'
                self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                      '--discover-template', '--env-template', output, forbidden=('private-',))
                self.assertFalse(output.exists())

    def test_later_input_failures_are_atomic_and_redacted(self):
        from stackwrench.compose_env import TraceError
        compose = self.compose("      VALUE: '${TOKEN:?private-message}'\n")
        manifest = self.put('inputs/scenarios.json', '{"first":{},"later":{"env_files":["explicit.env"]}}')
        output = self.root / 'invalid.template'
        cases = [None, 'MALFORMED private-data\n', 'COPY=${BAD:=private-message}\n',
                 'COPY=${BAD:-${NESTED}}\n', 'COPY=${BAD:?private-message}\nNEXT=${INVALID:=private-message}\n']
        for content in cases:
            with self.subTest(content=content):
                if content is not None:
                    self.put('inputs/explicit.env', content)
                with self.assertRaises(TraceError) as caught:
                    self.discover(compose, manifest)
                self.assertIn('scenario later:', str(caught.exception))
                self.assertNotIn('private-', str(caught.exception))
                self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                      '--discover-template', '--env-template', output, forbidden=('private-',))
                self.assertFalse(output.exists())
        # Literal service inputs are strict, in contrast to the unopened dynamic fixture.
        manifest.write_text('{"first":{},"later":{}}')
        compose = self.compose(fields='    env_file: literal.env\n')
        for content in [None, 'MALFORMED private-data\n']:
            if content is not None:
                self.put('literal.env', content)
            self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                  '--discover-template', '--env-template', output, forbidden=('private-',))
            self.assertFalse(output.exists())

    def test_cli_conflicts_and_exclusive_export(self):
        compose, manifest = self.fixture()
        output = self.root / 'invalid.template'
        for flags in [('--show-values',), ('--shell-env', 'private-path.json'),
                      ('--env-file', 'private-path.env')]:
            proc = self.run_cli('--compose', compose, '--scenario-file', manifest,
                                '--discover-template', '--env-template', output, *flags)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, '')
            self.assertNotIn('private-path', proc.stderr)
            self.assertFalse(output.exists())
        original = self.put('existing.env', 'KEEP=private-preserve\n')
        link = self.root / 'link.env'
        link.symlink_to(original)
        dangling = self.root / 'dangling.env'
        dangling.symlink_to(self.root / 'absent.env')
        for target in [original, link, dangling, self.root, self.root / 'missing' / 'private-path.env']:
            self.assert_cli_error('--compose', compose, '--scenario-file', manifest,
                                  '--discover-template', '--env-template', target, forbidden=('private-',))
        self.assertEqual(original.read_text(), 'KEEP=private-preserve\n')
        self.assertTrue(link.is_symlink())
        self.assertTrue(dangling.is_symlink())
        self.assertFalse((self.root / 'absent.env').exists())
        # Ordinary comparisons continue to evaluate required inputs, and cannot export.
        self.assert_cli_error('--compose', compose, '--scenario-file', manifest, forbidden=('private-',))
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest, '--env-template', output)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, '')
        self.assertFalse(output.exists())

    def test_complete_literal_optional_raw_scopes_and_ordering(self):
        compose = self.compose("      PASS:\n      VALUE: '${TOKEN:?private-message}'\n",
            fields='    env_file: [{path: absent.env, required: false}, {path: raw.env, format: raw}]\n')
        self.put('raw.env', 'RAW=${EXCLUDED:?private-message}\n')
        self.put('inputs/one.env', 'COPY=$ZED\nLITERAL=\'$EXCLUDED\'\n')
        self.put('inputs/two.env', 'COPY=${ALPHA:?private-message}\nCOPY=$ZED\n')
        manifest = self.put('inputs/scenarios.json', json.dumps({
            'z_last': {'env_files': ['one.env', 'two.env']}, 'a_first': {}}))
        report = self.discover(compose, manifest)
        self.assertTrue(report['complete'])
        self.assertEqual(list(report['scenarios']), ['z_last', 'a_first'])
        self.assertEqual([r['variable'] for r in report['scenarios']['z_last']['references']],
                         ['ZED', 'ALPHA', 'ZED', 'TOKEN', 'PASS'])
        self.assertEqual([r['variable'] for r in report['scenarios']['a_first']['references']], ['TOKEN', 'PASS'])
        for child in report['scenarios'].values():
            self.assertTrue(child['complete'])
            self.assertEqual(child['unresolved_environment'], [])
            self.assertEqual([r['status'] for r in child['service_env_files']], ['skipped-optional-missing', 'scanned'])
        from stackwrench.compose_env import env_template
        template = env_template(report)
        self.assertIn('# Scenario z_last; complete: true', template)
        self.assertNotIn('EXCLUDED', template)
        # Untrusted metadata is still confined to comments inside each section.
        odd = self.put('compose\nINJECTED=bad.json', json.dumps({'services': {'demo': {
            'odd\nFIELD=bad': '${REF:?private-message}'}}}))
        template = env_template(self.discover(odd, manifest))
        self.assertIn(r'\nINJECTED=bad.json', template)
        self.assertTrue(all(not line or line.startswith('#') for line in template.splitlines()))
        self.assertNotIn('private-', template)

    def test_checked_in_example(self):
        compose = ROOT / 'examples/compose_env/scenario-discovery/compose.yaml'
        manifest = compose.with_name('scenarios.json')
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest, '--discover-template', '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), self.discover(compose, manifest))
        self.assertFalse(json.loads(proc.stdout)['complete'])
