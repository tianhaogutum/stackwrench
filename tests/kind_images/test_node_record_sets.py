"""Current cross-node record groupings must not be reduced to digest unions."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.kind_images.planner import build_report

A, B = ['sha256:' + c * 64 for c in 'ab']
DA, DB = ['docker.io/library/demo@' + d for d in (A, B)]
IMAGE = 'docker.io/library/demo:v1'
CLI = [sys.executable, '-m', 'stackwrench', 'kind-images']


def record(*digests, tags=None):
    return {'id': A, 'repoTags': ['demo:v1'] if tags is None else tags,
            'repoDigests': ['demo@' + d for d in digests]}


class NodeRecordSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inventory = self.root / 'nodes.json'
        self.manifest = self.root / 'pod.json'
        self.nodes = {}
        self.write(self.manifest, {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'demo'},
            'spec': {'containers': [{'name': 'app', 'image': 'demo:v1', 'imagePullPolicy': 'Never'}]}})
        self.node('node-a', [record(A), record(B)])
        self.node('node-b', [record(A, B)])

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def node(self, name, records=None, schedulable=True):
        entry = {'schedulable': schedulable}
        if records is not None:
            entry['snapshot'] = name + '-images.json'
            self.write(self.root / entry['snapshot'], {'images': records})
        self.nodes[name] = entry
        self.write(self.inventory, {'schemaVersion': 1, 'cluster': 'demo', 'nodes': self.nodes})

    def report(self, baseline=None):
        return build_report([self.manifest], self.inventory, baseline)

    def evidence(self):
        return self.report()['imageOverview'][0]['tagDigestEvidence']

    def test_equal_unions_expose_different_complete_node_record_groupings(self):
        report = self.report()
        tag = report['imageOverview'][0]['tagDigestEvidence']
        # Reproduce the old blind spot before requiring the new comparison.
        self.assertEqual(tag['comparison'], 'same-digest-evidence')
        self.assertFalse(tag['knownDifference'])
        self.assertEqual([n['repoDigests'] for n in tag['nodes']], [[DA, DB], [DA, DB]])
        evidence = tag['recordSetComparison']
        self.assertEqual(evidence['comparison'], 'different-digest-evidence')
        self.assertEqual([n['repoDigestSets'] for n in evidence['nodes']], [[[DA], [DB]], [[DA, DB]]])
        for old, new in zip(tag['nodes'], evidence['nodes']):
            self.assertEqual(new, {'node': old['node'], 'status': 'complete', 'reason': old['reason'],
                                  'repoDigestSets': [[DA], [DB]] if old['node'] == 'node-a' else [[DA, DB]],
                                  'missingRecordCount': 0})
        self.assertEqual([n['recordComparison']['comparison'] for n in tag['nodes']],
                         ['different-digest-evidence', 'unassessed'])
        self.assertTrue(report['summary']['localUsePossibleEverywhere'])
        self.assertEqual(report['loadPlan'], [])

    def test_order_aliases_duplicate_records_ids_and_unrelated_evidence_do_not_change_groupings(self):
        alias = record(A, A, tags=['demo:v1', IMAGE, 'index.docker.io/demo:v1'])
        alias['repoDigests'] += ['index.docker.io/library/demo@' + A, 'other@' + B,
                                'docker.io:443/library/demo@' + B]
        unrelated = [record(B, tags=[]), record(B, tags=['demo:V1', 'other:v1']),
                     record(B, tags=['registry.test/demo:v1', 'docker.io:5000/library/demo:v1'])]
        for left, right, expected in (([record(B), alias], [*unrelated, alias, record(B), record(A)], [[DA], [DB]]),
                                      ([record(B, A)], [record(A, B), record(B, A)], [[DA, DB]])):
            with self.subTest(expected=expected):
                self.node('node-a', left)
                self.node('node-b', right)
                self.node('node-excluded', [record()], schedulable=False)
                tag = self.evidence()
                self.assertEqual(tag['comparison'], 'same-digest-evidence')
                evidence = tag['recordSetComparison']
                self.assertEqual(evidence['comparison'], 'same-digest-evidence')
                self.assertEqual([n['node'] for n in evidence['nodes']], ['node-a', 'node-b'])
                self.assertEqual([n['repoDigestSets'] for n in evidence['nodes']], [expected, expected])
                self.assertEqual(evidence['cohorts'], [{'repoDigestSets': expected, 'nodes': ['node-a', 'node-b']}])
                self.assertEqual(evidence['unassessedNodes'], [])

    def test_partial_groupings_cannot_establish_equality_or_difference(self):
        empty = record()
        empty['repoDigests'] = ['other@' + A]  # ID and other repository cannot fill the gap.
        for records, expected, missing, within_known in (([record(A), record(B), empty], [[DA], [DB]], 1, True),
                                                        ([record(A, B), empty], [[DA, DB]], 1, False),
                                                        ([empty, empty], [], 2, False)):
            with self.subTest(records=records):
                self.node('node-a', records)
                tag = self.evidence()
                evidence = tag['recordSetComparison']
                self.assertEqual(evidence['comparison'], 'unassessed')
                self.assertNotIn('knownDifference', evidence)
                row = evidence['nodes'][0]
                self.assertEqual(row['status'], 'unassessed')
                self.assertEqual(row['repoDigestSets'], expected)
                self.assertEqual(row['missingRecordCount'], missing)
                self.assertIn('lacks same-repository', row['reason'])
                self.assertEqual(tag['nodes'][0]['recordComparison']['knownDifference'], within_known)
        # Even an established complete-node grouping difference does not override a gap.
        self.node('node-a', [record(A), record(B)])
        self.node('node-c', [record(A, B), empty])
        self.assertEqual(self.evidence()['recordSetComparison']['comparison'], 'unassessed')

    def test_absent_tag_omitted_capture_and_one_node_remain_explicit(self):
        self.node('node-missing', [record(A, tags=['other:v1']), record(B, tags=[])])
        self.node('node-unknown')
        evidence = self.evidence()['recordSetComparison']
        self.assertEqual(evidence['comparison'], 'unassessed')
        for row, phrase in zip(evidence['nodes'][2:], ('Exact tag is absent', 'No snapshot supplied')):
            self.assertEqual(row['status'], 'unassessed')
            self.assertEqual(row['repoDigestSets'], [])
            self.assertEqual(row['missingRecordCount'], 0)
            self.assertIn(phrase, row['reason'])
        self.nodes = {}
        self.node('only', [record(A), record(B)])
        evidence = self.evidence()['recordSetComparison']
        self.assertEqual(evidence['comparison'], 'unassessed')
        self.assertIn('At least two', evidence['reason'])
        self.node('only')
        self.assertEqual(self.evidence()['recordSetComparison']['comparison'], 'unassessed')

    def test_cohorts_group_repeated_complete_nodes_and_keep_gaps_separate(self):
        alias = record(A, A, tags=[IMAGE, 'demo:v1', 'index.docker.io/demo:v1'])
        alias['id'] = B
        alias['repoDigests'] += ['index.docker.io/library/demo@' + A, 'other@' + B]
        self.node('node-d', [record(B, A), record(A, B)])
        self.node('node-c', [record(B), alias, record(A)])
        # Visible sets match both complete cohorts, but neither partial node may join.
        self.node('node-e', [record(A), record(B), record(), record()])
        self.node('node-f', [record(A, B), record()])
        self.node('node-missing', [record(A, tags=['demo:V1']), record(B, tags=[])])
        self.node('node-unknown')
        self.node('node-excluded', [record(A), record(B)], schedulable=False)
        tag = self.evidence()
        grouping = tag['recordSetComparison']
        self.assertEqual(grouping['comparison'], 'unassessed')
        self.assertFalse(tag['knownDifference'])
        self.assertEqual(grouping['cohorts'], [
            {'repoDigestSets': [[DA], [DB]], 'nodes': ['node-a', 'node-c']},
            {'repoDigestSets': [[DA, DB]], 'nodes': ['node-b', 'node-d']}])
        self.assertEqual(grouping['unassessedNodes'], ['node-e', 'node-f', 'node-missing', 'node-unknown'])
        rows = {n['node']: n for n in grouping['nodes']}
        self.assertEqual([rows[n]['missingRecordCount'] for n in grouping['unassessedNodes']], [2, 1, 0, 0])
        self.assertEqual(rows['node-e']['repoDigestSets'], [[DA], [DB]])
        self.assertEqual(rows['node-f']['repoDigestSets'], [[DA, DB]])
        self.assertIn('Exact tag is absent', rows['node-missing']['reason'])
        self.assertIn('No snapshot supplied', rows['node-unknown']['reason'])
        self.assertNotIn('knownDifference', grouping)
        self.assertTrue(tag['nodes'][0]['recordComparison']['knownDifference'])
        # Inventory declaration order cannot affect cohort order, membership or gap references.
        self.nodes = dict(reversed(list(self.nodes.items())))
        self.node('node-unknown')
        self.assertEqual(self.evidence(), tag)

    def test_cohorts_allow_singletons_but_never_empty_or_partial_groupings(self):
        for first, second, expected in (
                ([record(A)], [record(A), record()], [{'repoDigestSets': [[DA]], 'nodes': ['node-a']}]),
                ([record(A), record()], [record(B), record()], []),
                ([], None, [])):
            with self.subTest(first=first, second=second):
                self.node('node-a', first)
                self.node('node-b', second)
                grouping = self.evidence()['recordSetComparison']
                self.assertEqual(grouping['cohorts'], expected)
                self.assertEqual(grouping['unassessedNodes'], ['node-b'] if expected else ['node-a', 'node-b'])
                self.assertEqual(grouping['comparison'], 'unassessed')
        self.nodes = {}
        self.node('only', [record(A), record(B)])
        grouping = self.evidence()['recordSetComparison']
        self.assertEqual(grouping['cohorts'], [{'repoDigestSets': [[DA], [DB]], 'nodes': ['only']}])
        self.assertEqual(grouping['unassessedNodes'], [])
        self.assertEqual(grouping['comparison'], 'unassessed')
        self.assertIn('At least two', grouping['reason'])

    def test_baseline_is_independent_and_inventory_only_infers_no_groups(self):
        baseline = self.root / 'before.json'
        self.write(self.root / 'old.json', {'images': [record(B)]})
        self.write(baseline, {'schemaVersion': 1, 'cluster': 'demo', 'nodes': {
            n: {'schedulable': True, 'snapshot': 'old.json'} for n in self.nodes}})
        report = self.report(baseline)
        self.assertEqual({k: v for k, v in report.items() if k != 'inventoryDiff'}, self.report())
        inventory_only = build_report(None, self.inventory, baseline)
        self.assertEqual(inventory_only['imageOverview'], [])
        self.assertEqual(inventory_only['loadPlan'], [])
        self.assertFalse(inventory_only['summary']['localUsePossibleEverywhere'])

    def test_offline_deterministic_read_only_json_and_text_with_check(self):
        paths = list(self.root.glob('*.json'))
        original = {p: p.read_bytes() for p in paths}
        expected = self.report()
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            self.assertEqual(self.report(), expected)
        self.assertEqual(original, {p: p.read_bytes() for p in paths})
        args = [*CLI, '--manifests', str(self.manifest), '--inventory', str(self.inventory), '--check']
        def cli(*flags):
            return subprocess.run([*args, *flags], capture_output=True, text=True, cwd=self.root,
                                  timeout=10, env={**os.environ, 'PATH': '', 'KUBECONFIG': '/absent'})
        result = cli('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, cli('--json').stdout)
        self.assertEqual(json.loads(result.stdout), expected)
        text = cli()
        self.assertEqual(text.returncode, 0, text.stderr)
        for phrase in ('Cross-node tag record sets: different-digest-evidence', 'repoDigestSets=',
                       'missingRecordCount=0', 'Complete tag record-set cohort: nodes=',
                       'Unassessed tag record-set nodes: []', 'Within-node tag records: different-digest-evidence',
                       'no runtime content-equivalence or freshness guarantee'):
            self.assertIn(phrase, text.stdout)


if __name__ == '__main__':
    unittest.main()
