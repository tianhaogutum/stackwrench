"""Aggregate retained complete temporal pairs without weakening explicit gaps."""

import json
from pathlib import Path
import tempfile
import unittest

from stackwrench.kind_images.planner import build_report

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/kind_images/diff/transitions'
IMAGE = 'docker.io/library/demo:v1'
A, B = ['docker.io/library/demo@sha256:' + c * 64 for c in 'ab']
SPLIT, COMBINED = [[A], [B]], [[A, B]]


class TemporalTransitionCohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {}
        for label in ('before', 'after'):
            data = json.loads((FIXTURE / (label + '.json')).read_text())
            for node in data['nodes'].values():
                if 'snapshot' in node:
                    node['snapshot'] = str((FIXTURE / node['snapshot']).resolve())
            self.paths[label] = self.root / (label + '.json')
            self.write(self.paths[label], data)

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding='utf-8')

    def report(self, intent=True):
        return build_report([ROOT / 'examples/kind_images/evidence/workloads.yaml'] if intent else None,
                            self.paths['after'], self.paths['before'])

    def transition(self, report):
        return next(row for row in report['inventoryDiff']['tagRecordSetTransitions'] if row['image'] == IMAGE)

    def test_repeated_complete_transitions_include_retained_excluded_and_schedulability_changes(self):
        report = self.report()
        diff = report['inventoryDiff']
        tags = {row['node']: next(t for t in row['tags'] if t['image'] == IMAGE)
                for row in diff['imageChanges'] if row['tags']}
        self.assertEqual(tags['a']['comparison'], 'same-digest-evidence')
        self.assertEqual(tags['a']['recordSetComparison']['comparison'], 'different-digest-evidence')
        self.assertEqual(tags['c']['recordSetComparison'], tags['a']['recordSetComparison'])
        self.assertEqual(tags['d']['recordSetComparison'], tags['b']['recordSetComparison'])
        self.assertEqual(self.transition(report), {'image': IMAGE, 'cohorts': [
            {'beforeRepoDigestSets': SPLIT, 'afterRepoDigestSets': COMBINED,
             'nodes': ['a', 'c', 'entering', 'excluded']},
            {'beforeRepoDigestSets': COMBINED, 'afterRepoDigestSets': COMBINED,
             'nodes': ['b', 'd', 'leaving']}], 'unassessedNodes': ['partial-after', 'partial-before']})
        self.assertEqual(diff, self.report(intent=False)['inventoryDiff'])
        self.assertEqual(diff['schedulabilityChanges'], [
            {'node': 'entering', 'before': False, 'after': True},
            {'node': 'leaving', 'before': True, 'after': False}])
        for label in ('before', 'after'):
            excluded = next(n for n in diff[label]['nodes'] if n['name'] == 'excluded')
            self.assertFalse(excluded['schedulable'])
            self.assertEqual(excluded['source'], 'synthetic excluded ' + label)
        # Current workload scope still excludes these nodes, independently of temporal scope.
        current = report['imageOverview'][0]['tagDigestEvidence']['recordSetComparison']
        self.assertNotIn('excluded', [n['node'] for n in current['nodes']])
        self.assertNotIn('leaving', [n['node'] for n in current['nodes']])

    def test_partial_pairs_capture_gaps_absent_tags_and_lifecycle_stay_explicit(self):
        diff = self.report()['inventoryDiff']
        self.assertEqual(diff['nodesAdded'], ['added'])
        self.assertEqual(diff['nodesRemoved'], ['removed'])
        self.assertEqual(diff['unassessedComparisons'], [
            {'node': 'missing-after', 'missingSnapshots': ['after']},
            {'node': 'missing-before', 'missingSnapshots': ['before']},
            {'node': 'missing-both', 'missingSnapshots': ['before', 'after']}])
        changes = {r['node']: r for r in diff['imageChanges']}
        for name, side in [('partial-before', 'before'), ('partial-after', 'after')]:
            pair = changes[name]['tags'][0]['recordSetComparison']
            self.assertEqual(pair['comparison'], 'unassessed')
            self.assertEqual(pair[side], {'status': 'unassessed', 'repoDigestSets': SPLIT, 'missingRecordCount': 1})
            self.assertNotIn('knownDifference', pair)
        for name, direction in [('tag-added', 'addedReferences'), ('tag-removed', 'removedReferences')]:
            self.assertEqual(changes[name]['tags'], [])
            self.assertIn(IMAGE, changes[name][direction])
        self.assertEqual(len(diff['tagRecordSetTransitions']), 1)
        only = self.report(intent=False)
        for key in ('assessments', 'imageOverview', 'loadPlan', 'loadPlanOmissions'):
            self.assertEqual(only[key], [])
        self.assertFalse(only['summary']['localUsePossibleEverywhere'])

    def test_both_sides_define_transition_key_and_cohort_order(self):
        for label, path in self.paths.items():
            data = json.loads(path.read_text())
            data['nodes'] = {n: data['nodes'][n] for n in ('d', 'c', 'b', 'a')}
            if label == 'after':
                for name in ('b', 'c'):
                    data['nodes'][name]['snapshot'] = str(ROOT / 'examples/kind_images/evidence/records-a.json')
            self.write(path, data)
        self.assertEqual(self.transition(self.report()), {'image': IMAGE, 'cohorts': [
            {'beforeRepoDigestSets': SPLIT, 'afterRepoDigestSets': SPLIT, 'nodes': ['c']},
            {'beforeRepoDigestSets': SPLIT, 'afterRepoDigestSets': COMBINED, 'nodes': ['a']},
            {'beforeRepoDigestSets': COMBINED, 'afterRepoDigestSets': SPLIT, 'nodes': ['b']},
            {'beforeRepoDigestSets': COMBINED, 'afterRepoDigestSets': COMBINED, 'nodes': ['d']}],
            'unassessedNodes': []})

    def test_singleton_all_gap_and_no_retained_tag_have_no_invented_comparison(self):
        originals = {label: json.loads(path.read_text()) for label, path in self.paths.items()}
        for names, expected in [(['a'], [{'image': IMAGE, 'cohorts': [
                {'beforeRepoDigestSets': SPLIT, 'afterRepoDigestSets': COMBINED, 'nodes': ['a']}], 'unassessedNodes': []}]),
                (['partial-before', 'partial-after'], [{'image': IMAGE, 'cohorts': [],
                  'unassessedNodes': ['partial-after', 'partial-before']}]),
                (['tag-added', 'tag-removed', 'missing-before', 'missing-after', 'missing-both', 'added', 'removed'], [])]:
            with self.subTest(names=names):
                for label, path in self.paths.items():
                    self.write(path, {**originals[label], 'nodes': {n: v for n, v in originals[label]['nodes'].items() if n in names}})
                self.assertEqual(self.report()['inventoryDiff']['tagRecordSetTransitions'], expected)

    def test_exact_tags_and_same_record_repository_boundaries_cannot_fill_partial_pairs(self):
        digest = 'sha256:' + 'a' * 64
        # A same-repository digest on an unrelated record cannot fill the demo:v1 gap.
        records = [{'id': 'opaque', 'repoTags': [tag], 'repoDigests': [repo + '@' + digest]}
                   for tag, repo in [('demo:v1', 'other'), ('demo:V1', 'demo'),
                                    ('registry.test/demo:v1', 'registry.test/demo'),
                                    ('docker.io:5000/library/demo:v1', 'docker.io:5000/library/demo')]]
        records += [{'id': digest, 'repoTags': [], 'repoDigests': ['demo@' + digest]}]
        for label, path in self.paths.items():
            capture = self.root / (label + '-images.json')
            self.write(capture, {'images': records if label == 'before' else list(reversed(records))})
            self.write(path, {'schemaVersion': 1, 'cluster': 'evidence-demo', 'nodes': {
                'a': {'schedulable': True, 'snapshot': str(capture)}}})
        rows = self.report()['inventoryDiff']['tagRecordSetTransitions']
        self.assertEqual([r['image'] for r in rows], sorted([
            IMAGE, 'docker.io/library/demo:V1', 'registry.test/demo:v1', 'docker.io:5000/library/demo:v1']))
        demo = next(r for r in rows if r['image'] == IMAGE)
        self.assertEqual(demo, {'image': IMAGE, 'cohorts': [], 'unassessedNodes': ['a']})
        for row in rows:
            if row is not demo:
                self.assertEqual(row['cohorts'][0]['nodes'], ['a'])
                self.assertEqual(row['unassessedNodes'], [])


if __name__ == '__main__':
    unittest.main()
