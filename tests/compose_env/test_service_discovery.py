"""Explicit syntax discovery scopes: selection, whole-document preflight and I/O."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from stackwrench.compose_env import TraceError, discover_scenarios, discover_template, env_template
from stackwrench.compose_env.trace import _read_text
from .test_trace import Workspace


class ServiceDiscovery(Workspace):
    def fixture(self):
        compose = self.put('compose.yaml', """name: '${PROJECT:?private-message}'
services:
  z.api:
    env_file: local.env
    environment:
      VALUE: '${TOKEN:?private-message}'
  a-worker:
    environment: [PASS]
  missing:
    env_file: absent.env
  dynamic:
    env_file: '${FILE:-unopened.env}'
    environment: ['${KEY:?private-message}=private-value']
""")
        self.put('local.env', 'COPY=${LOCAL:?private-message}\n')
        self.put('unopened.env', 'MALFORMED private-unopened\n')
        inputs = self.put('explicit.env', 'COPY=${SHARED:?private-message}\nFILE=unopened.env\n')
        return compose, inputs

    def test_selected_services_cover_shared_inputs_without_unrelated_files(self):
        compose, inputs = self.fixture()
        selected = ['a-worker', 'z.api']
        allowed = [compose, inputs, self.root / 'local.env']
        reads, stats = [], []
        original_stat = Path.stat
        def read(path, label):
            reads.append(path)
            self.assertIn(path, allowed)
            return _read_text(path, label)
        def stat(path, *args, **kwargs):
            stats.append(path)
            self.assertIn(path, allowed)
            return original_stat(path, *args, **kwargs)
        with patch.dict(os.environ, {'TOKEN': 'private-host', 'FILE': 'unopened.env'}):
            before = dict(os.environ)
            with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                    patch.object(Path, 'stat', autospec=True, side_effect=stat):
                report = discover_template(compose, [inputs], service_names=selected, require_complete=True)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(reads, allowed)
        self.assertEqual(stats, allowed)
        self.assertEqual(selected, ['a-worker', 'z.api'])
        self.assertEqual(report['scope'], {'services': ['z.api', 'a-worker'],
                         'shared': ['explicit-dotenv-inputs', 'top-level-compose-values']})
        self.assertEqual([r['variable'] for r in report['references']],
                         ['SHARED', 'PROJECT', 'TOKEN', 'LOCAL', 'PASS'])
        self.assertEqual([r['service'] for r in report['service_env_files']], ['z.api'])
        self.assertEqual(report['unresolved_environment'], [])
        output = self.root / 'selected.template'
        proc = self.run_cli('--compose', compose, '--env-file', inputs, '--discover-template',
                            '--service', 'a-worker', '--service', 'z.api', '--require-complete',
                            '--env-template', output, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), report)
        template = output.read_text()
        self.assertEqual(template, env_template(report))
        self.assertTrue(all(not line or line.startswith('#') for line in template.splitlines()))
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        for forbidden in ['private-', 'unopened.env', 'absent.env', 'FILE', 'KEY', 'winner', '"value":']:
            self.assertNotIn(forbidden, proc.stdout + template)
        self.assertIn('Selected services:', template)
        # Omission retains the all-service strict literal failure.
        with self.assertRaisesRegex(TraceError, 'input file missing'):
            discover_template(compose, [inputs])

    def test_selection_validation_precedes_dotenv_reads_and_redacts_names(self):
        compose, inputs = self.fixture()
        invalid = [[], '', 'z.api', ('z.api',), {}, [None], [True], [1], [[]], [{}],
                   [''], ['\0private-name'], ['private-unknown'], ['z.api', 'z.api'],
                   ['z.api', 'private-unknown'], ['z.api,a-worker']]
        for names in invalid:
            with self.subTest(names=names):
                reads = []
                def read(path, label):
                    reads.append(path)
                    return _read_text(path, label)
                with patch('stackwrench.compose_env.trace._read_text', side_effect=read):
                    with self.assertRaisesRegex(TraceError, 'service selection') as caught:
                        discover_template(compose, [inputs], service_names=names)
                self.assertTrue(all(p == compose for p in reads))
                self.assertNotIn('private-', str(caught.exception))
        output = self.root / 'invalid.template'
        for flags in [('--service', ''), ('--service', 'private-unknown'),
                      ('--service', 'z.api', '--service', 'z.api')]:
            self.assert_cli_error('--compose', compose, '--env-file', inputs, '--discover-template',
                                  '--env-template', output, *flags, forbidden=('private-',))
            self.assertFalse(output.exists())

    def test_whole_document_preflight_precedes_selected_inputs(self):
        # A missing selected file precedes invalid unselected structure in document
        # order, so the expected diagnostic proves preflight ran before any reads.
        bad_services = [
            ('    extends: private-base\n', 'extends/multi-file'),
            ('    env_file: [{path: x, extra: private-value}]\n', 'only supports'),
            ('    env_file: [{path: x, required: "false"}]\n', 'true/false'),
            ('    env_file: [{path: x, format: compose}]\n', 'format must'),
            ('    env_file: []\n    environment: {VALUE: true}\n', 'quote booleans'),
            ('    environment: {VALUE: 0x10}\n', 'canonical'),
            ('    environment: {VALUE: 1e3}\n', 'ambiguous'),
            ('    environment: {BAD-NAME: literal}\n', 'ASCII identifier'),
            ('    environment: ["BAD-NAME=value"]\n', 'ASCII identifier'),
            ('    environment: ["${KEY}=x", "${BAD:=private-message}"]\n', 'unsupported'),
            ('    environment: {VALUE: "${BAD:-${NESTED}}"}\n', 'unsupported'),
            ('    environment: {VALUE: "${BAD"}\n', 'unterminated'),
            ('    environment: scalar\n', 'mapping or list'),
            ('    env_file: ""\n', 'nonempty local'),
            ('    env_file: "https://private-file"\n', 'nonempty local'),
            ('    env_file: {path: private-file}\n', 'path string'),
            ('    environment: {}\n    environment: {}\n', 'duplicate mapping'),
        ]
        for bad, expected in bad_services:
            with self.subTest(bad=bad):
                compose = self.put('compose.yaml', 'services:\n  chosen:\n    env_file: absent.env\n  other:\n' + bad)
                reads, stats = [], []
                original_stat = Path.stat
                def read(path, label):
                    reads.append(path)
                    self.assertEqual(path, compose)
                    return _read_text(path, label)
                def stat(path, *args, **kwargs):
                    stats.append(path)
                    self.assertEqual(path, compose)
                    return original_stat(path, *args, **kwargs)
                with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                        patch.object(Path, 'stat', autospec=True, side_effect=stat):
                    with self.assertRaisesRegex(TraceError, expected) as caught:
                        discover_template(compose, [self.root / 'absent-explicit.env'], service_names=['chosen'])
                self.assertEqual(reads, [compose])
                self.assertEqual(stats, [compose])
                self.assertNotIn('private-', str(caught.exception))
                output = self.root / 'invalid.template'
                self.assert_cli_error('--compose', compose, '--discover-template', '--service', 'chosen',
                                      '--env-template', output, forbidden=('private-',))
                self.assertFalse(output.exists())
        for content in ['include: private-file\nservices: {chosen: {}}\n',
                        'services: {chosen: {}, other: []}\n',
                        'name: "${BAD:=private-value}"\nservices: {chosen: {}}\n']:
            compose.write_text(content)
            with patch('stackwrench.compose_env.trace.dotenv', side_effect=AssertionError('dotenv opened')):
                with self.assertRaises(TraceError):
                    discover_template(compose, [self.root / 'absent.env'], service_names=['chosen'])

    def test_selected_dynamic_scopes_gate_without_resolving_and_default_is_all(self):
        compose, inputs = self.fixture()
        self.put('absent.env', 'COPY=$MISSING_SERVICE\n')
        all_report = discover_template(compose, [inputs])
        self.assertNotIn('scope', all_report)
        self.assertFalse(all_report['complete'])
        self.assertEqual([f['service'] for f in all_report['service_env_files']], ['z.api', 'missing', 'dynamic'])
        self.assertIn('KEY', [r['variable'] for r in all_report['references']])
        report = discover_template(compose, [inputs], service_names=['dynamic'])
        self.assertFalse(report['complete'])
        self.assertEqual([r['variable'] for r in report['references']], ['SHARED', 'PROJECT', 'FILE', 'KEY'])
        self.assertEqual(report['service_env_files'][0]['status'], 'unresolved-dynamic')
        self.assertEqual(report['unresolved_environment'][0]['service'], 'dynamic')
        with self.assertRaisesRegex(TraceError, 'discovery incomplete'):
            discover_template(compose, [inputs], service_names=['dynamic'], require_complete=True)
        output = self.root / 'blocked.template'
        self.assert_cli_error('--compose', compose, '--discover-template', '--service', 'dynamic',
                              '--require-complete', '--env-template', output, forbidden=('private-', 'unopened.env'))
        self.assertFalse(output.exists())
        text = self.run_cli('--compose', compose, '--discover-template', '--service', 'a-worker')
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn('Selected services: ["a-worker"]', text.stdout)
        self.assertIn('Shared scope:', text.stdout)
        self.assertNotIn('TOKEN', text.stdout)

    def test_named_selection_is_independent_and_reads_only_selected_scopes(self):
        compose, inputs = self.fixture()
        second = self.put('second.env', 'COPY=$SECOND\n')
        manifest = self.put('scenarios.json', json.dumps({
            'z_first': {'env_files': [inputs.name]}, 'a_second': {'env_files': [second.name]},
            'broken': {'env_files': ['absent-explicit.env']}}))
        allowed = [manifest, compose, inputs, self.root / 'local.env', compose, second, self.root / 'local.env']
        reads, stats = [], []
        original_stat = Path.stat
        def read(path, label):
            reads.append(path)
            self.assertIn(path, allowed)
            return _read_text(path, label)
        def stat(path, *args, **kwargs):
            stats.append(path)
            self.assertIn(path, allowed)
            return original_stat(path, *args, **kwargs)
        with patch('stackwrench.compose_env.trace._read_text', side_effect=read), \
                patch.object(Path, 'stat', autospec=True, side_effect=stat):
            report = discover_scenarios(compose, manifest, scenario_names=['a_second', 'z_first'],
                                        service_names=['a-worker', 'z.api'], require_complete=True)
        self.assertEqual(reads, allowed)
        self.assertEqual(stats, allowed)
        self.assertEqual(list(report['scenarios']), ['z_first', 'a_second'])
        self.assertTrue(report['complete'])
        self.assertEqual([r['variable'] for r in report['scenarios']['a_second']['references']],
                         ['SECOND', 'PROJECT', 'TOKEN', 'LOCAL', 'PASS'])
        output = self.root / 'named.template'
        proc = self.run_cli('--compose', compose, '--scenario-file', manifest, '--discover-template',
                            '--service', 'a-worker', '--service', 'z.api', '--scenario', 'a_second',
                            '--scenario', 'z_first', '--require-complete', '--env-template', output, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), report)
        template = output.read_text()
        self.assertEqual(template.count('Selected services:'), 2)
        first, later = template.split('# Scenario a_second;')
        self.assertIn('# SHARED=', first)
        self.assertNotIn('# SECOND=', first)
        self.assertIn('# SECOND=', later)
        self.assertNotIn('# SHARED=', later)
        for selections in [['z_first', 'broken'], None]:
            with self.assertRaisesRegex(TraceError, 'scenario broken:'):
                discover_scenarios(compose, manifest, scenario_names=selections, service_names=['z.api'])
        # Later child failure discards the earlier successful report/export.
        blocked = self.root / 'blocked.template'
        self.assert_cli_error('--compose', compose, '--scenario-file', manifest, '--discover-template',
                              '--service', 'z.api', '--env-template', blocked, forbidden=('private-',))
        self.assertFalse(blocked.exists())

    def test_selected_strict_inputs_optional_raw_and_exclusive_export(self):
        compose, inputs = self.fixture()
        output = self.root / 'blocked.template'
        # Shared explicit inputs are strict even when unrelated to selected service.
        for content in [None, 'MALFORMED private-data\n', 'COPY=${BAD:=private-message}\n']:
            if content is not None:
                self.put('bad.env', content)
            self.assert_cli_error('--compose', compose, '--env-file', self.root / 'bad.env',
                                  '--discover-template', '--service', 'a-worker', '--env-template', output,
                                  forbidden=('private-',))
            self.assertFalse(output.exists())
        local = self.root / 'local.env'
        local.unlink()
        for content in [None, 'MALFORMED private-data\n', 'COPY=${BAD:-${NESTED}}\n']:
            if content is not None:
                local.write_text(content)
            self.assert_cli_error('--compose', compose, '--discover-template', '--service', 'z.api',
                                  '--env-template', output, forbidden=('private-',))
            self.assertFalse(output.exists())
        compose.write_text('services:\n  chosen:\n    env_file: [{path: absent.env, required: false}, {path: raw.env, format: raw}]\n'
                           '    environment: {VALUE: "${TOKEN:?private-message}"}\n  other:\n    env_file: missing.env\n')
        self.put('raw.env', 'COPY=${EXCLUDED:?private-message}\n')
        report = discover_template(compose, service_names=['chosen'], require_complete=True)
        self.assertEqual([f['status'] for f in report['service_env_files']], ['skipped-optional-missing', 'scanned'])
        self.assertEqual([r['variable'] for r in report['references']], ['TOKEN'])
        original = self.put('existing.template', 'KEEP=private-preserve\n')
        link = self.root / 'link.template'
        link.symlink_to(original)
        for target in [original, link]:
            self.assert_cli_error('--compose', compose, '--discover-template', '--service', 'chosen',
                                  '--require-complete', '--env-template', target, forbidden=('private-',))
        self.assertEqual(original.read_text(), 'KEEP=private-preserve\n')
        self.assertTrue(link.is_symlink())

    def test_service_flag_rejected_outside_discovery_before_any_reads(self):
        from stackwrench.compose_env.cli import main
        import contextlib
        import io
        for flags in [[], ['--scenario-file', 'private-manifest']]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('stackwrench.compose_env.trace._read_text', side_effect=AssertionError('input opened')), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    main(['--compose', 'private-compose', '--service', 'private-name', *flags])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertNotIn('private-', stderr.getvalue())
