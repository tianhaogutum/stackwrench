"""Temporal tag record groupings are distinct from union and runtime identity."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.kind_images.planner import build_report

A = 'sha256:' + 'a' * 64
B = 'sha256:' + 'b' * 64
C = 'sha256:' + 'c' * 64
IMAGE = 'docker.io/library/demo:v1'
DA, DB = ['docker.io/library/demo@' + digest for digest in (A, B)]
CLI = [sys.executable, '-m', 'stackwrench', 'kind-images']


def record(*digests, tags=None, identifier='opaque'):
    return {'id': identifier, 'repoTags': ['demo:v1'] if tags is None else tags,
            'repoDigests': ['demo@' + d for d in digests]}


def side(sets, missing=0):
    return {'status': 'unassessed' if missing else 'complete',
            'repoDigestSets': sets, 'missingRecordCount': missing}


class TemporalRecordSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.before = self.root / 'before.json'
        self.after = self.root / 'after.json'
        for name in ('before', 'after'):
            self.write(self.root / (name + '.json'), {'schemaVersion': 1, 'cluster': 'demo', 'nodes': {
                'node-a': {'schedulable': True, 'snapshot': name + '-images.json'}}})
        self.manifest = self.root / 'pod.json'
        self.write(self.manifest, {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'demo'},
            'spec': {'containers': [{'name': 'app', 'image': 'demo:v1', 'imagePullPolicy': 'Never'}]}})
        self.snapshots([record(A), record(B)], [record(A, B)])

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def snapshots(self, before, after):
        for name, records in (('before', before), ('after', after)):
            self.write(self.root / (name + '-images.json'), {'images': records})

    def report(self, intent=True):
        return build_report([self.manifest] if intent else None, self.after, self.before)

    def tag(self, report=None):
        report = self.report() if report is None else report
        return next(t for t in report['inventoryDiff']['imageChanges'][0]['tags'] if t['image'] == IMAGE)

    def test_equal_unions_expose_changed_complete_record_sets(self):
        report = self.report()
        tag = self.tag(report)
        # Establish the old blind spot before requiring the additive signal.
        self.assertEqual(tag['comparison'], 'same-digest-evidence')
        self.assertEqual(tag['beforeRepoDigests'], [DA, DB])
        self.assertEqual(tag['afterRepoDigests'], [DA, DB])
        evidence = tag['recordSetComparison']
        self.assertEqual(evidence['comparison'], 'different-digest-evidence')
        self.assertEqual(evidence['before'], side([[DA], [DB]]))
        self.assertEqual(evidence['after'], side([[DA, DB]]))
        self.assertIn('record', evidence['reason'])
        self.assertTrue(report['summary']['localUsePossibleEverywhere'])
        self.assertEqual(report['loadPlan'], [])
        # Adding the baseline does not affect any current-inventory assessment.
        without_baseline = build_report([self.manifest], self.after)
        self.assertEqual({k: v for k, v in report.items() if k != 'inventoryDiff'}, without_baseline)

    def test_order_alias_duplicates_ids_and_unrelated_evidence_do_not_change_groupings(self):
        alias = record(A, tags=['demo:v1', 'index.docker.io/library/demo:v1', 'demo:v1'], identifier=B)
        alias['repoDigests'] += ['docker.io/library/demo@' + A, 'other@' + C]
        unrelated = [record(C, tags=[]), record(C, tags=['demo:V1']),
                     record(C, tags=['other:v1']), record(C, tags=['registry.test/demo:v1']),
                     record(C, tags=['docker.io:5000/library/demo:v1'])]
        for before, after, expected in (
                ([record(B), alias], [*unrelated, record(A), record(B), alias], [[DA], [DB]]),
                ([record(B, A)], [record(A, B), record(B, A)], [[DA, DB]])):
            with self.subTest(expected=expected):
                self.snapshots(before, after)
                tag = self.tag()
                self.assertEqual(tag['comparison'], 'same-digest-evidence')
                evidence = tag['recordSetComparison']
                self.assertEqual(evidence['comparison'], 'same-digest-evidence')
                self.assertEqual(evidence['before'], side(expected))
                self.assertEqual(evidence['after'], side(expected))

    def test_any_incomplete_record_on_either_side_leaves_temporal_comparison_unassessed(self):
        empty = record(identifier=A)
        empty['repoDigests'] = ['other@' + A]
        for before, after, expected_before, expected_after in (
                ([record(A), record(B)], [record(A, B), empty], side([[DA], [DB]]), side([[DA, DB]], 1)),
                ([record(A), empty], [record(A)], side([[DA]], 1), side([[DA]])),
                ([record(A), record(B), empty], [record(A, B)], side([[DA], [DB]], 1), side([[DA, DB]])),
                ([empty], [empty, record(B, tags=[])], side([], 1), side([], 1))):
            with self.subTest(before=expected_before, after=expected_after):
                self.snapshots(before, after)
                tag = self.tag()
                self.assertEqual(tag['comparison'], 'unassessed')
                evidence = tag['recordSetComparison']
                self.assertEqual(evidence['comparison'], 'unassessed')
                self.assertEqual(evidence['before'], expected_before)
                self.assertEqual(evidence['after'], expected_after)
                self.assertIn('lack', evidence['reason'])
                self.assertNotIn('knownDifference', evidence)

    def test_absent_tags_and_captures_remain_explicit_without_empty_set_equality(self):
        self.snapshots([record(A)], [record(A, tags=['demo:V1'])])
        report = self.report()
        change = report['inventoryDiff']['imageChanges'][0]
        self.assertEqual(change['tags'], [])
        self.assertIn(IMAGE, change['removedReferences'])
        self.write(self.after, {'schemaVersion': 1, 'cluster': 'demo', 'nodes': {
            'node-a': {'schedulable': True}}})
        diff = self.report()['inventoryDiff']
        self.assertEqual(diff['imageChanges'], [])
        self.assertEqual(diff['unassessedComparisons'], [{'node': 'node-a', 'missingSnapshots': ['after']}])

    def test_cli_modes_text_check_and_offline_read_only_evidence(self):
        paths = list(self.root.glob('*.json'))
        contents = {p: p.read_bytes() for p in paths}
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            report = self.report()
            self.assertEqual(report, self.report())
            inventory_only = self.report(intent=False)
        self.assertEqual(inventory_only['inventoryDiff'], report['inventoryDiff'])
        for key in ('assessments', 'imageOverview', 'loadPlan', 'loadPlanOmissions'):
            self.assertEqual(inventory_only[key], [])
        self.assertFalse(inventory_only['summary']['localUsePossibleEverywhere'])
        for intent, expected in ((True, 0), (False, 1)):
            for output in ([], ['--json']):
                args = [*CLI, '--inventory', str(self.after), '--baseline-inventory', str(self.before), '--check', *output]
                if intent:
                    args += ['--manifests', str(self.manifest)]
                result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=10,
                                        env={**os.environ, 'PATH': '', 'KUBECONFIG': str(self.root / 'absent')})
                self.assertEqual(result.returncode, expected, result.stderr)
                if output:
                    self.assertEqual(json.loads(result.stdout), report if intent else inventory_only)
                else:
                    for text in ('Temporal tag record sets: different-digest-evidence',
                                 'before: complete', 'after: complete', 'missingRecordCount=0',
                                 json.dumps([[DA], [DB]]), json.dumps([[DA, DB]]), 'not a runtime/content guarantee'):
                        self.assertIn(text, result.stdout)
        self.assertEqual(contents, {p: p.read_bytes() for p in paths})


if __name__ == '__main__':
    unittest.main()
