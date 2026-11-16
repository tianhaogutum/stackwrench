"""Observed tags partition declared nodes without inventing missing evidence."""

import json
from pathlib import Path
import tempfile
import unittest

from stackwrench.kind_images.cli import render_text
from stackwrench.kind_images.planner import build_report

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/kind_images/diff/participation'
IMAGE = 'docker.io/library/demo:v1'
SOLO = 'docker.io/library/solo:v1'


class TagParticipationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {}
        for side in ('before', 'after'):
            data = json.loads((FIXTURE / (side + '.json')).read_text())
            for node in data['nodes'].values():
                if 'snapshot' in node:
                    node['snapshot'] = str((FIXTURE / node['snapshot']).resolve())
            self.paths[side] = self.root / (side + '.json')
            self.write(self.paths[side], data)

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding='utf-8')

    def report(self, intent=True):
        return build_report([ROOT / 'examples/kind_images/evidence/workloads.yaml'] if intent else None,
                            self.paths['after'], self.paths['before'])

    def rows(self, report):
        return {r['image']: r for r in report['inventoryDiff']['tagRecordSetParticipation']}

    def test_observed_tag_partition_references_existing_pairs_gaps_absence_and_lifecycle(self):
        report = self.report()
        diff = report['inventoryDiff']
        # Reproduce the boundary after proving existing cohorts cover only retained pairs.
        transition = diff['tagRecordSetTransitions'][0]
        self.assertEqual(transition['unassessedNodes'], ['partial-after', 'partial-before'])
        self.assertEqual(len(transition['cohorts']), 2)
        rows = self.rows(report)
        self.assertEqual(list(rows), [IMAGE, SOLO])
        self.assertEqual(rows[IMAGE], {
            'image': IMAGE,
            'completePairNodes': ['a', 'b', 'c', 'd', 'entering', 'excluded', 'leaving'],
            'incompletePairNodes': ['partial-after', 'partial-before'],
            'missingCaptureNodes': {'beforeOnly': ['missing-before', 'unknown-absent-before'],
                                    'afterOnly': ['missing-after', 'unknown-absent-after'],
                                    'both': ['missing-both']},
            'capturedTagAbsenceNodes': {'beforeOnly': ['tag-added'], 'afterOnly': ['tag-removed'],
                                       'both': ['absent-both']},
            'nodesAdded': ['added', 'added-missing', 'added-observed'],
            'nodesRemoved': ['removed', 'removed-missing']})
        pairs = {c['node']: {t['image']: t for t in c['tags']} for c in diff['imageChanges']}
        for row in rows.values():
            for category in ('completePairNodes', 'incompletePairNodes'):
                for name in row[category]:
                    pair = pairs[name][row['image']]['recordSetComparison']
                    self.assertEqual(all(pair[s]['status'] == 'complete' for s in ('before', 'after')),
                                     category == 'completePairNodes')
            self.assertEqual(row['nodesAdded'], diff['nodesAdded'])
            self.assertEqual(row['nodesRemoved'], diff['nodesRemoved'])
            gaps = {g['node']: g['missingSnapshots'] for g in diff['unassessedComparisons']}
            for key, sides in [('beforeOnly', ['before']), ('afterOnly', ['after']), ('both', ['before', 'after'])]:
                for name in row['missingCaptureNodes'][key]:
                    self.assertEqual(gaps[name], sides)
            for category, change in [('beforeOnly', 'addedReferences'), ('afterOnly', 'removedReferences')]:
                for name in row['capturedTagAbsenceNodes'][category]:
                    node = next(c for c in diff['imageChanges'] if c['node'] == name)
                    self.assertIn(row['image'], node[change])
            references = row['completePairNodes'] + row['incompletePairNodes'] + row['nodesAdded'] + row['nodesRemoved']
            for category in ('missingCaptureNodes', 'capturedTagAbsenceNodes'):
                references += sum(row[category].values(), [])
            universe = {n['name'] for side in ('before', 'after') for n in diff[side]['nodes']}
            self.assertEqual(sorted(references), sorted(universe))  # exactly once per node

    def test_excluded_observed_tag_without_a_retained_pair_has_no_intent_or_transition(self):
        report = self.report(intent=False)
        diff = report['inventoryDiff']
        solo = self.rows(report)[SOLO]
        self.assertEqual(solo['completePairNodes'], [])
        self.assertEqual(solo['incompletePairNodes'], [])
        self.assertEqual(solo['capturedTagAbsenceNodes']['beforeOnly'], [])
        self.assertEqual(solo['capturedTagAbsenceNodes']['afterOnly'], [])
        self.assertEqual(solo['capturedTagAbsenceNodes']['both'], [
            'a', 'absent-both', 'b', 'c', 'd', 'entering', 'excluded', 'leaving',
            'partial-after', 'partial-before', 'tag-added', 'tag-removed'])
        self.assertNotIn(SOLO, [r['image'] for r in diff['tagRecordSetTransitions']])
        self.assertNotIn(SOLO, [r['image'] for r in diff['coverage']])  # schedulable scope differs
        self.assertEqual(diff, self.report()['inventoryDiff'])
        for key in ('assessments', 'imageOverview', 'loadPlan', 'loadPlanOmissions'):
            self.assertEqual(report[key], [])
        self.assertFalse(report['summary']['localUsePossibleEverywhere'])

    def test_tag_universe_is_observed_exact_canonical_tags_not_digests_ids_or_requests(self):
        digest = 'sha256:' + 'a' * 64
        capture = self.root / 'images.json'
        self.write(capture, {'images': [
            {'id': 'id-only:v1', 'repoTags': ['demo:v1', 'docker.io/library/demo:v1', 'demo:V1',
                                            'registry.test/demo:v1', 'docker.io:5000/library/demo:v1'],
             'repoDigests': ['unseen@' + digest]}]})
        for path in self.paths.values():
            self.write(path, {'schemaVersion': 1, 'cluster': 'evidence-demo', 'nodes': {
                'z': {'schedulable': False, 'snapshot': str(capture)},
                'a': {'schedulable': True, 'snapshot': str(capture)}}})
        rows = self.rows(self.report())
        self.assertEqual(list(rows), sorted([IMAGE, 'docker.io/library/demo:V1',
                                          'registry.test/demo:v1', 'docker.io:5000/library/demo:v1']))
        for row in rows.values():
            self.assertEqual(row['completePairNodes'], [])
            self.assertEqual(row['incompletePairNodes'], ['a', 'z'])  # unrelated digests cannot fill gaps
        manifest = self.root / 'unobserved.json'
        self.write(manifest, {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'unobserved'},
                             'spec': {'containers': [{'name': 'unseen', 'image': 'unseen:v1'}]}})
        requested_only = build_report([manifest], self.paths['after'], self.paths['before'])
        self.assertEqual(self.rows(requested_only), rows)
        self.assertIn('docker.io/library/unseen:v1', [r['image'] for r in requested_only['inventoryDiff']['coverage']])
        # Digest-only captures and image IDs cannot create observed tags either.
        self.write(capture, {'images': [{'id': 'id-only:v1', 'repoDigests': ['unseen@' + digest]}]})
        self.assertEqual(self.rows(self.report()), {})
        # An entirely uncaptured inventory pair cannot create observed tags from intent.
        for path in self.paths.values():
            self.write(path, {'schemaVersion': 1, 'cluster': 'evidence-demo',
                              'nodes': {'a': {'schedulable': True}}})
        self.assertEqual(self.rows(self.report()), {})

    def test_missing_capture_precedes_captured_absence_and_text_explains_references(self):
        report = self.report()
        row = self.rows(report)[IMAGE]
        for name in ('unknown-absent-before', 'unknown-absent-after', 'missing-both'):
            self.assertNotIn(name, sum(row['capturedTagAbsenceNodes'].values(), []))
        text = render_text(report)
        for expected in ('Observed tag temporal participation ' + IMAGE,
                         'Complete retained pair nodes:', 'Incomplete retained pair nodes:',
                         'Missing capture nodes (tag availability unknown on missing sides):',
                         'Captured tag absence nodes (two captures required):',
                         'Lifecycle node references (no tag presence claim):',
                         'see imageChanges, unassessedComparisons, before/after capture evidence and nodesAdded/nodesRemoved'):
            self.assertIn(expected, text)
        self.assertNotIn('knownDifference', row)


if __name__ == '__main__':
    unittest.main()
