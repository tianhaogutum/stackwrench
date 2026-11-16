"""Temporal provenance preserves supplied record positions without comparing identity."""

import json
from pathlib import Path
import tempfile
import unittest

from stackwrench.kind_images.cli import render_text
from stackwrench.kind_images.evidence import tag_digest_evidence
from stackwrench.kind_images.inventory import read_inventory_details
from stackwrench.kind_images.planner import build_report
from stackwrench.kind_images.references import parse_reference

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/kind_images/diff/record-provenance'
IMAGE = 'docker.io/library/demo:v1'
DA, DB = ['docker.io/library/demo@sha256:' + c * 64 for c in 'ab']


def resolve(report, pointer):
    value = report
    for part in pointer.split('/')[1:]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class TemporalProvenanceTests(unittest.TestCase):
    def report(self, pair=FIXTURE, intent=True):
        return build_report([ROOT / 'examples/kind_images/evidence/workloads.yaml'] if intent else None,
                            pair / 'after.json', pair / 'before.json')

    def assert_provenance(self, report):
        diff = report['inventoryDiff']
        inventories = {s: read_inventory_details(Path(diff[s]['inventory'])) for s in ('before', 'after')}
        for change in diff['imageChanges']:
            for tag in change['tags']:
                self.assertEqual(set(tag['recordEvidence']), {'before', 'after'})
                for side in ('before', 'after'):
                    evidence = tag['recordEvidence'][side]
                    self.assertEqual(set(evidence), {'captureRef', 'records'})
                    capture = resolve(report, evidence['captureRef'])
                    self.assertTrue(evidence['captureRef'].startswith(f'/inventoryDiff/{side}/nodes/'))
                    self.assertEqual(capture['name'], change['node'])
                    node = next(n for n in inventories[side].nodes if n.name == change['node'])
                    self.assertEqual(evidence['records'], tag_digest_evidence(node)[tag['image']]['records'])
                    snapshot = Path(capture['snapshot'])
                    if not snapshot.is_absolute():
                        snapshot = inventories[side].path.parent / snapshot
                    images = json.loads(snapshot.read_text())['images']
                    indices = [r['imageIndex'] for r in evidence['records']]
                    self.assertEqual(indices, sorted(set(indices)))
                    # Independently resolve raw CRI records, including empty digest evidence.
                    for record in evidence['records']:
                        raw = images[record['imageIndex']]
                        self.assertIn(tag['image'], [parse_reference(t).canonical for t in raw.get('repoTags') or []])
                        repo = parse_reference(tag['image']).repository
                        expected = sorted({parse_reference(d).canonical for d in raw.get('repoDigests') or []
                                           if parse_reference(d).repository == repo})
                        self.assertEqual(record['repoDigests'], expected)

    def test_equal_partial_groupings_keep_duplicate_indices_and_empty_records(self):
        report = self.report()
        tag = report['inventoryDiff']['imageChanges'][0]['tags'][0]
        # Old summaries coincide despite different record positions and multiplicities.
        self.assertEqual(tag['beforeRepoDigests'], [DA, DB])
        self.assertEqual(tag['afterRepoDigests'], [DA, DB])
        self.assertEqual(tag['comparison'], 'unassessed')
        self.assertEqual(tag['recordSetComparison']['before'], tag['recordSetComparison']['after'])
        self.assertEqual(tag['recordSetComparison']['comparison'], 'unassessed')
        self.assertEqual(tag['recordEvidence'], {
            'before': {'captureRef': '/inventoryDiff/before/nodes/0', 'records': [
                {'imageIndex': 1, 'repoDigests': [DA, DB]},
                {'imageIndex': 2, 'repoDigests': [DA]},
                {'imageIndex': 3, 'repoDigests': [DA]},
                {'imageIndex': 4, 'repoDigests': []}]},
            'after': {'captureRef': '/inventoryDiff/after/nodes/0', 'records': [
                {'imageIndex': 0, 'repoDigests': [DA]},
                {'imageIndex': 2, 'repoDigests': [DA, DB]},
                {'imageIndex': 3, 'repoDigests': []}]}})
        self.assert_provenance(report)
        current = report['imageOverview'][0]['tagDigestEvidence']['nodes'][0]
        self.assertEqual(current['records'], tag['recordEvidence']['after']['records'])
        self.assertTrue(current['recordComparison']['knownDifference'])
        self.assertTrue(report['summary']['localUsePossibleEverywhere'])
        self.assertEqual(report['loadPlan'], [])
        self.assertEqual(report['inventoryDiff'], self.report(intent=False)['inventoryDiff'])

    def test_complete_equal_and_changed_groupings_keep_original_record_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair = Path(tmp)
            for name in ('before', 'after'):
                data = json.loads((FIXTURE / (name + '.json')).read_text())
                (pair / (name + '.json')).write_text(json.dumps(data))
            before = json.loads((FIXTURE / 'before-images.json').read_text())['images'][:-1]
            after = json.loads((FIXTURE / 'after-images.json').read_text())['images'][:-1]
            for records, comparison in ((after, 'same-digest-evidence'), ([after[2]], 'different-digest-evidence')):
                for name, images in (('before', before), ('after', records)):
                    (pair / (name + '-images.json')).write_text(json.dumps({'images': images}))
                report = self.report(pair)
                tag = report['inventoryDiff']['imageChanges'][0]['tags'][0]
                self.assertEqual(tag['comparison'], 'same-digest-evidence')
                self.assertEqual(tag['recordSetComparison']['comparison'], comparison)
                self.assertEqual([r['imageIndex'] for r in tag['recordEvidence']['before']['records']], [1, 2, 3])
                self.assert_provenance(report)

    def test_only_existing_pairs_including_excluded_and_changed_schedulability(self):
        pair = ROOT / 'examples/kind_images/diff/participation'
        report = self.report(pair)
        diff = report['inventoryDiff']
        expected = ['a', 'b', 'c', 'd', 'entering', 'excluded', 'leaving', 'partial-after', 'partial-before']
        actual = [c['node'] for c in diff['imageChanges'] for t in c['tags'] if t['image'] == IMAGE]
        self.assertEqual(actual, expected)
        self.assert_provenance(report)
        # Array offsets differ across the declared sides because lifecycle nodes sort first.
        tag = next(c for c in diff['imageChanges'] if c['node'] == 'b')['tags'][0]
        self.assertNotEqual(tag['recordEvidence']['before']['captureRef'].split('/')[-1],
                            tag['recordEvidence']['after']['captureRef'].split('/')[-1])
        for name in ['excluded', 'entering', 'leaving']:
            self.assertIn(name, actual)
        self.assertNotIn('recordEvidence', json.dumps(diff['tagAvailabilityEvidence']))
        self.assertEqual(diff, self.report(pair, intent=False)['inventoryDiff'])
        self.assertNotIn('inventoryDiff', build_report([ROOT / 'examples/kind_images/evidence/workloads.yaml'], pair / 'after.json'))

    def test_inventory_only_text_exposes_both_sides_without_workload_intent(self):
        report = self.report(intent=False)
        self.assert_provenance(report)
        for key in ('assessments', 'imageOverview', 'loadPlan', 'loadPlanOmissions'):
            self.assertEqual(report[key], [])
        self.assertFalse(report['summary']['localUsePossibleEverywhere'])
        text = render_text(report)
        tag = report['inventoryDiff']['imageChanges'][0]['tags'][0]
        for side, evidence in tag['recordEvidence'].items():
            self.assertIn(f"      Temporal tag record evidence {side}: captureRef={json.dumps(evidence['captureRef'])} "
                          f"records={json.dumps(evidence['records'])}", text)
        self.assertIn('Supplied record positions only; no cross-capture record identity, runtime content or freshness inferred.', text)


if __name__ == '__main__':
    unittest.main()
