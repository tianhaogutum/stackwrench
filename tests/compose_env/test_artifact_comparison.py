"""Offline saved-discovery pairs: strict ingestion and independent syntax coverage."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from stackwrench.compose_env import TraceError, compare_discovery_checklists, discover_template, discovery_checklist
from .test_trace import Workspace

ERROR = 'artifact comparison requires two valid v1 single syntax-only discovery JSON files'


class ArtifactComparisons(Workspace):
    def fixture(self):
        before = self.put('old.yaml', 'services: {old: {environment: ["A=$REMOVE", "B=${TOKEN?private-message}", "PASS"]}}\n')
        inputs = self.put('old.env', 'OUT=$SHARED $SHARED\n')
        a = discover_template(before, [inputs], service_names=['old'])
        after = self.put('new.yaml', 'x-meta: "$TOP"\nservices: {new: {env_file: ["${FILE:-private-target.env}"], environment: ["A=${ADD:?private-message}", "B=${TOKEN:?private-message}", "PASS", "${KEY}=private-value"]}}\n')
        b = discover_template(after)
        # All original sources disappear; even the dynamic target has never existed.
        for path in [before, after, inputs]:
            path.unlink()
        return a, b, self.save('baseline.json', a), self.save('candidate.json', b)

    def save(self, name, report):
        return self.put(name, json.dumps(report))

    def expected(self, a, b):
        common = {key: a[key] for key in ['schema_version', 'evaluation', 'values_shown',
                                         'host_environment', 'implicit_dotenv']}
        return compare_discovery_checklists({**common, 'report_type': 'scenario-template-discovery',
                   'complete': a['complete'] and b['complete'], 'scenarios': {'baseline': a, 'candidate': b}})

    def test_cli_saved_pair_after_original_inputs_removed(self):
        a, b, baseline, candidate = self.fixture()
        proc = self.run_cli('--compare-discovery-artifacts', baseline, candidate)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result, self.expected(a, b))
        self.assertEqual(result['baseline'], 'baseline')
        self.assertEqual(list(result['scenarios']), ['baseline', 'candidate'])
        self.assertEqual(list(result['comparisons']), ['candidate'])
        self.assertTrue(result['scenarios']['baseline']['complete'])
        self.assertFalse(result['scenarios']['candidate']['complete'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['scenarios']['baseline']['scope']['services'], ['old'])
        self.assertEqual(result['scenarios']['candidate']['scope']['service_selection'], 'all')
        rows = {r['variable']: r for r in result['comparisons']['candidate']['variables']}
        self.assertEqual(rows['ADD']['presence_change'], 'added')
        self.assertEqual(rows['REMOVE']['presence_change'], 'removed')
        self.assertTrue(rows['TOKEN']['operators_changed'])
        self.assertTrue(rows['TOKEN']['requirement_changed'])
        self.assertTrue(rows['TOKEN']['source_changed'])
        for forbidden in ['private-', '"value":', '"operand":', '"message":', str(baseline), str(candidate)]:
            self.assertNotIn(forbidden, proc.stdout)
        explicit = self.run_cli('--compare-discovery-artifacts', baseline, candidate, '--json', env={'TOKEN': 'private-host'})
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(explicit.stdout, proc.stdout)

    def test_api_reads_only_explicit_artifacts_and_projects_allowlist(self):
        from stackwrench.compose_env import compare_discovery_artifacts
        a, b, baseline, candidate = self.fixture()
        clean = self.expected(a, b)
        for report in [a, b]:
            report.update(value='private-top', operand='private-top', message='private-top', extra={'secret': '$BAD'})
            for use in report['references']:
                use.update(value='private-value', operand='private-operand', message='private-message')
        self.save(baseline.name, a); self.save(candidate.name, b)
        originals = copy.deepcopy((a, b))
        actual_open, actual_stat = Path.open, Path.stat
        reads, stats = [], []
        def opened(path, *args, **kwargs):
            self.assertIn(path, [baseline, candidate]); reads.append(path)
            return actual_open(path, *args, **kwargs)
        def statted(path, *args, **kwargs):
            self.assertIn(path, [baseline, candidate]); stats.append(path)
            return actual_stat(path, *args, **kwargs)
        with patch.object(Path, 'open', autospec=True, side_effect=opened), \
                patch.object(Path, 'stat', autospec=True, side_effect=statted), \
                patch('builtins.open', side_effect=AssertionError('unexpected read')), \
                patch.object(type(os.environ), '__getitem__', side_effect=AssertionError('host lookup')):
            result = compare_discovery_artifacts(str(baseline), candidate)
        self.assertEqual(reads, [baseline, candidate])
        self.assertEqual(stats, [baseline, candidate])
        self.assertEqual(result, clean)
        self.assertEqual((a, b), originals)
        result['scenarios']['baseline']['scope']['services'].append('mutation')
        self.assertEqual(compare_discovery_artifacts(baseline, candidate), clean)
        same = compare_discovery_artifacts(baseline, baseline)
        for row in same['comparisons']['candidate']['variables']:
            self.assertEqual(row['presence_change'], 'unchanged')
            self.assertFalse(any(row[k] for k in ['operators_changed', 'requirement_changed', 'source_changed']))
        self.assertEqual(json.loads(self.run_cli('--compare-discovery-artifacts', baseline, candidate).stdout), clean)

    def test_strict_json_rejects_duplicates_even_in_unknown_keys_and_nonfinite(self):
        from stackwrench.compose_env import compare_discovery_artifacts
        a, _, baseline, candidate = self.fixture()
        text = json.dumps(a)
        invalid = ['', '{"private-secret":', '[]', text + ' trailing', '\ufeff' + text,
                   text[:-1] + ', "extra": {"private-key":1,"private-key":2}}',
                   text[:-1] + ', "extra": {"x":1,"\\u0078":2}}',
                   text.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')]
        invalid += [text[:-1] + ', "extra": ' + number + '}' for number in ['NaN', 'Infinity', '-Infinity', '1e9999']]
        for bad in invalid:
            for target in [baseline, candidate]:
                self.save(baseline.name, a); self.save(candidate.name, a)
                target.write_text(bad)
                with self.subTest(bad=bad[:90], target=target.name), self.assertRaises(TraceError) as caught:
                    compare_discovery_artifacts(baseline, candidate)
                self.assertEqual(str(caught.exception), ERROR)
                proc = self.run_cli('--compare-discovery-artifacts', baseline, candidate)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertEqual(json.loads(proc.stderr), {'error': {'code': 'invalid-or-unsupported-input', 'message': ERROR}})

    def test_resource_boundaries_utf8_and_regular_files(self):
        from stackwrench.compose_env import compare_discovery_artifacts
        a, _, baseline, candidate = self.fixture()
        text = json.dumps(a)
        # Exact byte limit and container depth64 are accepted, counting ignored extras.
        candidate.write_text(text + ' ' * (2 * 1024 * 1024 - len(text)))
        self.assertEqual(compare_discovery_artifacts(baseline, candidate), self.expected(a, a))
        with candidate.open('ab') as stream:
            stream.write(b' ')
        self.assert_artifact_error(baseline, candidate)
        for depth in [63, 64, 2000]:
            candidate.write_text(text[:-1] + ',"extra":' + '[' * depth + '0' + ']' * depth + '}')
            if depth == 63:
                self.assertEqual(compare_discovery_artifacts(baseline, candidate), self.expected(a, a))
            else:
                self.assert_artifact_error(baseline, candidate)
        # Brackets and escaped quote/backslash inside a JSON string are not depth.
        a['extra'] = '[[{{' * 100 + '\\"' * 100
        self.save(candidate.name, a)
        self.assertEqual(compare_discovery_artifacts(baseline, candidate), self.expected(a, a))
        candidate.write_bytes(b'\xffprivate-secret')
        self.assert_artifact_error(baseline, candidate)
        for path in [self.root, self.root / 'private-missing.json']:
            self.assert_artifact_error(baseline, path)
        with patch.object(Path, 'open', side_effect=PermissionError('private-access')):
            self.assert_artifact_error(baseline, candidate)
        for path in [None, [], {}, 1, 'private\0path']:
            self.assert_artifact_error(path, candidate)

    def assert_artifact_error(self, baseline, candidate):
        from stackwrench.compose_env import compare_discovery_artifacts
        with self.assertRaises(TraceError) as caught:
            compare_discovery_artifacts(baseline, candidate)
        self.assertEqual(str(caught.exception), ERROR)

    def test_wrong_modes_malformed_shapes_and_coverage_are_rejected(self):
        a, b, baseline, candidate = self.fixture()
        invalid = [None, [], {}, discovery_checklist(a), self.expected(a, b),
                   {'report_type': 'scenario-template-discovery', 'scenarios': {'baseline': a, 'candidate': b}}]
        for updates in [{'schema_version': True}, {'schema_version': 2}, {'evaluation': 'private-evaluated'},
                        {'values_shown': True}, {'host_environment': 'private-host'}, {'implicit_dotenv': 'read'},
                        {'complete': False}, {'references': [None]}, {'scope': None},
                        {'scope': {'services': ['old', 'old'], 'shared': []}},
                        {'references': [{'variable': 'private-bad'}]}, {'service_env_files': [{}]},
                        {'unresolved_environment': [{}]}]:
            bad = copy.deepcopy(a); bad.update(updates); invalid.append(bad)
        for bad in invalid:
            self.save(candidate.name, bad)
            self.assert_artifact_error(baseline, candidate)
        # Source labels are literal metadata, preserved with JSON escaping, never dereferenced.
        a['references'][0]['location'] = '/missing/source\n# INJECT="metadata"'
        a['references'][0]['field_path'].append('x\n"literal')
        self.save(baseline.name, a); self.save(candidate.name, b)
        from stackwrench.compose_env import compare_discovery_artifacts
        self.assertEqual(compare_discovery_artifacts(baseline, candidate), self.expected(a, b))

    def test_cli_conflicts_and_missing_live_compose_fail_before_reads_or_exports(self):
        from stackwrench.compose_env.cli import main
        for extra in [['--compose', 'private-compose'], ['--env-file', ''], ['--shell-env', 'private-shell'],
                      ['--scenario-file', 'private-manifest'], ['--scenario', 'private-name'],
                      ['--service', 'private-service'], ['--env-template', str(self.root / 'blocked')],
                      ['--discover-template'], ['--require-complete'], ['--checklist-json'],
                      ['--compare-checklists-json'], ['--show-values'],
                      ['--compare-discovery-artifacts', 'third', 'fourth']]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(Path, 'open', side_effect=AssertionError('read')), \
                    patch.object(Path, 'stat', side_effect=AssertionError('stat')), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    main(['--compare-discovery-artifacts', 'private-before', 'private-after', *extra])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertNotIn('private-', stderr.getvalue())
            self.assertFalse((self.root / 'blocked').exists())
        for args in [[], ['--json'], ['--compare-discovery-artifacts', 'one']]:
            proc = self.run_cli(*args)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, '')
