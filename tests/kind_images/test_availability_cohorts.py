"""State-pair cohorts reference existing nonparticipant rows, never tag transitions."""

import json
from pathlib import Path
import tempfile
import unittest

from stackwrench.kind_images.cli import render_text
from stackwrench.kind_images.planner import build_report

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/kind_images/diff/availability-cohorts'
IMAGE = 'docker.io/library/demo:v1'
SOLO = 'docker.io/library/solo:v1'


class TagAvailabilityCohortTests(unittest.TestCase):
    def report(self, intent=True, before=None, after=None):
        return build_report([ROOT / 'examples/kind_images/evidence/workloads.yaml'] if intent else None,
                            after or FIXTURE / 'after.json', before or FIXTURE / 'before.json')

    def test_repeated_state_pairs_reference_exactly_the_existing_nonparticipants(self):
        report = self.report()
        diff = report['inventoryDiff']
        tags = diff['tagAvailabilityEvidence']
        self.assertEqual([tag['image'] for tag in tags], [IMAGE, SOLO])
        expected = {
            ('captured-absent', 'captured-absent'): ['absent-both'],
            ('captured-absent', 'captured-present'): ['tag-added'],
            ('captured-absent', 'missing-capture'): ['absent-missing-a', 'absent-missing-b'],
            ('captured-present', 'captured-absent'): ['tag-removed'],
            ('captured-present', 'missing-capture'): ['present-missing-a', 'present-missing-b'],
            ('captured-present', 'node-not-declared'): ['removed-present-a', 'removed-present-b'],
            ('missing-capture', 'captured-present'): ['missing-present-a', 'missing-present-b'],
            ('missing-capture', 'missing-capture'): ['unknown-a', 'unknown-b'],
            ('missing-capture', 'node-not-declared'): ['removed-missing-a', 'removed-missing-b'],
            ('node-not-declared', 'captured-absent'): ['added-absent-a', 'added-absent-b'],
            ('node-not-declared', 'captured-present'): ['added-present-a', 'added-present-b'],
            ('node-not-declared', 'missing-capture'): ['added-missing-a', 'added-missing-b']}
        # Prior implementation has repeated side-state rows, but no concise cohort view.
        demo = tags[0]
        self.assertEqual([n['node'] for n in demo['nodes'] if n['before']['state'] == 'captured-present'
                          and n['after']['state'] == 'missing-capture'], expected['captured-present', 'missing-capture'])
        self.assertEqual(demo['cohorts'], [dict(beforeState=b, afterState=a, nodes=nodes)
                                         for (b, a), nodes in sorted(expected.items())])
        for tag, participation in zip(tags, diff['tagRecordSetParticipation']):
            rows = {row['node']: row for row in tag['nodes']}
            keys = [(c['beforeState'], c['afterState']) for c in tag['cohorts']]
            self.assertEqual(keys, sorted(set(keys)))
            members = []
            for cohort in tag['cohorts']:
                self.assertEqual(set(cohort), {'beforeState', 'afterState', 'nodes'})
                self.assertEqual(cohort['nodes'], sorted(set(cohort['nodes'])))
                self.assertTrue(cohort['nodes'])
                for name in cohort['nodes']:
                    self.assertEqual([rows[name][s]['state'] for s in ('before', 'after')],
                                     [cohort['beforeState'], cohort['afterState']])
                members += cohort['nodes']
            self.assertEqual(sorted(members), sorted(rows))  # exactly once, not just set equality
            self.assertTrue(set(members).isdisjoint(participation['completePairNodes'] + participation['incompletePairNodes']))
        # Aliases and duplicated capture records change matches, not the state cohort.
        rows = {n['node']: n for n in demo['nodes']}
        self.assertNotEqual(rows['present-missing-a']['before']['matches'], rows['present-missing-b']['before']['matches'])
        self.assertIn('absent-missing-b', report['excludedNodes'])
        self.assertIn('present-missing-b', [r['node'] for r in diff['schedulabilityChanges']])

    def test_inventory_only_and_text_keep_unknown_lifecycle_pairs_literal(self):
        report = self.report(False)
        self.assertEqual(report['inventoryDiff'], self.report()['inventoryDiff'])
        for key in ('assessments', 'imageOverview', 'loadPlan', 'loadPlanOmissions'):
            self.assertEqual(report[key], [])
        self.assertFalse(report['summary']['localUsePossibleEverywhere'])
        text = render_text(report)
        for tag in report['inventoryDiff']['tagAvailabilityEvidence']:
            for cohort in tag['cohorts']:
                self.assertIn(f"    Observed tag availability state cohort: beforeState={cohort['beforeState']} "
                              f"afterState={cohort['afterState']} nodes={json.dumps(cohort['nodes'])}", text)
        self.assertIn('State cohorts reference the per-node side evidence only; unknown/lifecycle pairs do not imply tag gains or losses, content transitions or workload intent.', text)

    def test_empty_nonparticipants_and_no_observed_tags_never_create_cohorts(self):
        for filename in ('before.json', 'incomplete.json'):
            path = ROOT / 'examples/kind_images/diff/record-sets' / filename
            with self.subTest(filename=filename):
                tags = self.report(before=path, after=path)['inventoryDiff']['tagAvailabilityEvidence']
                self.assertEqual(tags, [{'image': IMAGE, 'nodes': [], 'cohorts': []}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / 'images.json'
            capture.write_text(json.dumps({'images': [{'id': 'demo:v1', 'repoDigests': ['demo@sha256:'+'a'*64]}]}))
            before, after = root / 'before.json', root / 'after.json'
            for path in (before, after):
                path.write_text(json.dumps({'schemaVersion': 1, 'cluster': 'evidence-demo', 'nodes': {
                    'a': {'schedulable': True, 'snapshot': str(capture)}}}))
            report = self.report(before=before, after=after)
            self.assertEqual(report['inventoryDiff']['tagAvailabilityEvidence'], [])
            self.assertIn(IMAGE, [r['image'] for r in report['inventoryDiff']['coverage']])  # requested only
            after.write_text(json.dumps({'schemaVersion': 1, 'cluster': 'evidence-demo', 'nodes': {
                'a': {'schedulable': True}}}))
            before.write_text(after.read_text())
            self.assertEqual(self.report(before=before, after=after)['inventoryDiff']['tagAvailabilityEvidence'], [])


if __name__ == '__main__':
    unittest.main()
