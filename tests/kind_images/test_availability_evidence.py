"""Nonparticipating temporal nodes retain explicit per-side name evidence."""

import json
from pathlib import Path
import tempfile
import unittest

from stackwrench.kind_images.cli import render_text
from stackwrench.kind_images.planner import build_report
from stackwrench.kind_images.references import parse_reference

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/kind_images/diff/participation'
IMAGE = 'docker.io/library/demo:v1'
SOLO = 'docker.io/library/solo:v1'


def resolve(report, pointer):
    value = report
    for part in pointer.lstrip('/').split('/'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class TagAvailabilityEvidenceTests(unittest.TestCase):
    def report(self, intent=True, before=None, after=None):
        return build_report([ROOT / 'examples/kind_images/evidence/workloads.yaml'] if intent else None,
                            after or FIXTURE / 'after.json', before or FIXTURE / 'before.json')

    def rows(self, report):
        return {tag['image']: {node['node']: node for node in tag['nodes']}
                for tag in report['inventoryDiff']['tagAvailabilityEvidence']}

    def test_nonparticipating_states_and_references_use_only_same_side_facts(self):
        report = self.report()
        rows = self.rows(report)
        self.assertEqual(list(rows), [IMAGE, SOLO])
        expected = {
            'absent-both': ('captured-absent', 'captured-absent'),
            'added': ('node-not-declared', 'captured-present'),
            'added-missing': ('node-not-declared', 'missing-capture'),
            'added-observed': ('node-not-declared', 'captured-absent'),
            'missing-after': ('captured-present', 'missing-capture'),
            'missing-before': ('missing-capture', 'captured-present'),
            'missing-both': ('missing-capture', 'missing-capture'),
            'removed': ('captured-present', 'node-not-declared'),
            'removed-missing': ('missing-capture', 'node-not-declared'),
            'tag-added': ('captured-absent', 'captured-present'),
            'tag-removed': ('captured-present', 'captured-absent'),
            'unknown-absent-after': ('captured-absent', 'missing-capture'),
            'unknown-absent-before': ('missing-capture', 'captured-absent')}
        self.assertEqual({name: tuple(row[s]['state'] for s in ('before', 'after'))
                          for name, row in rows[IMAGE].items()}, expected)
        self.assertEqual(rows[SOLO]['added-observed']['after']['state'], 'captured-present')
        # An excluded changed-schedulability node still has two explicit captured-absent sides for solo.
        self.assertEqual(tuple(rows[SOLO]['leaving'][s]['state'] for s in ('before', 'after')),
                         ('captured-absent', 'captured-absent'))
        diff = report['inventoryDiff']
        for tag in diff['tagRecordSetParticipation']:
            nonparticipants = rows[tag['image']]
            declared = {n['name'] for s in ('before', 'after') for n in diff[s]['nodes']}
            self.assertEqual(set(nonparticipants), declared - set(tag['completePairNodes'] + tag['incompletePairNodes']))
            self.assertEqual(list(nonparticipants), sorted(nonparticipants))
            for name, row in nonparticipants.items():
                self.assertEqual(row['evidenceRefs'], sorted(set(row['evidenceRefs'])))
                expected_refs = []
                for side in ('before', 'after'):
                    evidence = row[side]
                    state = evidence['state']
                    if state == 'node-not-declared':
                        self.assertIsNone(evidence['captureRef'])
                    else:
                        expected_refs.append(evidence['captureRef'])
                        capture = resolve(report, evidence['captureRef'])
                        self.assertEqual(capture['name'], name)
                        self.assertEqual(evidence['captureRef'].split('/')[2], side)
                        self.assertEqual(capture['snapshot'] is None, state == 'missing-capture')
                        if capture['snapshot'] is not None:
                            snapshot = Path(diff[side]['inventory']).parent / capture['snapshot']
                            records = json.loads(snapshot.read_text())['images']
                            matches = {(i, 'repoTags', raw) for i, record in enumerate(records)
                                       for raw in record.get('repoTags', [])
                                       if parse_reference(raw).canonical == tag['image']}
                            self.assertEqual(evidence['matches'], [dict(imageIndex=i, field=f, reference=r)
                                                                   for i, f, r in sorted(matches)])
                            self.assertEqual(bool(matches), state == 'captured-present')
                    if state != 'captured-present':
                        self.assertEqual(evidence['matches'], [])
                for key in ('nodesAdded', 'nodesRemoved', 'imageChanges', 'unassessedComparisons'):
                    for i, item in enumerate(diff[key]):
                        if (item if isinstance(item, str) else item['node']) == name:
                            expected_refs.append(f'/inventoryDiff/{key}/{i}')
                self.assertEqual(row['evidenceRefs'], sorted(expected_refs))
                for pointer in row['evidenceRefs']:
                    self.assertIsNotNone(resolve(report, pointer))

    def test_inventory_only_has_same_evidence_and_no_intent_text_is_explicit(self):
        report = self.report(False)
        self.assertEqual(report['inventoryDiff'], self.report()['inventoryDiff'])
        for key in ('assessments', 'imageOverview', 'loadPlan', 'loadPlanOmissions'):
            self.assertEqual(report[key], [])
        self.assertFalse(report['summary']['localUsePossibleEverywhere'])
        text = render_text(report)
        for expected in ('Observed tag side availability ' + IMAGE,
                         'node=missing-before before=missing-capture after=captured-present',
                         'node=unknown-absent-before before=missing-capture after=captured-absent',
                         'node=added-missing before=node-not-declared after=missing-capture',
                         'captureRef=', 'matches=', 'evidenceRefs=',
                         'Unknown sides do not assert availability; no tag transition or workload intent inferred.'):
            self.assertIn(expected, text)

    def test_exact_observed_tags_aliases_not_digest_id_request_or_other_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / 'images.json'
            capture.write_text(json.dumps({'images': [
                {'id': 'solo:v1', 'repoTags': ['demo:v1', 'docker.io/library/demo:v1', 'demo:v1'],
                 'repoDigests': ['solo@sha256:' + 'a' * 64]},
                {'id': 'demo:v1', 'repoTags': ['demo:V1', 'registry.test/demo:v1', 'docker.io:5000/library/demo:v1']}]}))
            variants = root / 'variants.json'
            variants.write_text(json.dumps({'images': [
                {'id': 'demo:v1', 'repoTags': ['demo:V1', 'registry.test/demo:v1', 'docker.io:5000/library/demo:v1'],
                 'repoDigests': ['demo@sha256:' + 'a' * 64]}]}))
            before, after = root / 'before.json', root / 'after.json'
            before.write_text(json.dumps({'schemaVersion': 1, 'cluster': 'test', 'nodes': {
                'a': {'schedulable': True}, 'variants': {'schedulable': False}, 'z': {'schedulable': False}}}))
            after.write_text(json.dumps({'schemaVersion': 1, 'cluster': 'test', 'nodes': {
                'a': {'schedulable': False}, 'variants': {'schedulable': False, 'snapshot': str(variants)},
                'z': {'schedulable': True, 'snapshot': str(capture)}}}))
            report = self.report(before=before, after=after)
            rows = self.rows(report)
            self.assertEqual(list(rows), sorted([IMAGE, 'docker.io/library/demo:V1',
                                                'registry.test/demo:v1', 'docker.io:5000/library/demo:v1']))
            self.assertEqual(rows[IMAGE]['z']['after']['matches'], [
                {'imageIndex': 0, 'field': 'repoTags', 'reference': 'demo:v1'},
                {'imageIndex': 0, 'field': 'repoTags', 'reference': IMAGE}])
            self.assertEqual(rows[IMAGE]['z']['before']['state'], 'missing-capture')
            self.assertEqual(rows[IMAGE]['a']['after']['state'], 'missing-capture')
            self.assertEqual(rows[IMAGE]['variants']['after']['state'], 'captured-absent')
            self.assertEqual(rows[IMAGE]['variants']['after']['matches'], [])
            self.assertEqual(report['inventoryDiff']['tagRecordSetTransitions'], [])
            self.assertEqual(report['inventoryDiff']['imageChanges'], [])
            # Digests / IDs cannot populate observed-tag rows, even with a requested demo tag.
            capture.write_text(json.dumps({'images': [{'id': 'demo:v1', 'repoDigests': ['demo@sha256:' + 'a' * 64]}]}))
            variants.write_text(capture.read_text())
            self.assertEqual(self.rows(self.report(before=before, after=after)), {})
            after.write_text(before.read_text())  # no captures at all, but still explicit intent
            self.assertEqual(self.rows(self.report(before=before, after=after)), {})

    def test_retained_complete_and_incomplete_tag_pairs_are_not_nonparticipants(self):
        for filename in ('before.json', 'incomplete.json'):
            with self.subTest(filename=filename):
                path = ROOT / 'examples/kind_images/diff/record-sets' / filename
                report = self.report(before=path, after=path)
                self.assertEqual(self.rows(report), {IMAGE: {}})


if __name__ == '__main__':
    unittest.main()
