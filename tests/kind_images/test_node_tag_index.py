"""Node-first navigation references authoritative evidence without copying states."""

import json
from pathlib import Path
import tempfile
import unittest

from stackwrench.kind_images.cli import render_text
from stackwrench.kind_images.planner import build_report

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'docker.io/library/demo:v1'


def resolve(report, pointer):
    value = report
    for part in pointer.split('/')[1:]:
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class NodeTagEvidenceIndexTests(unittest.TestCase):
    def report(self, fixture='participation', intent=True):
        pair = ROOT / 'examples/kind_images/diff' / fixture
        return build_report([ROOT / 'examples/kind_images/evidence/workloads.yaml'] if intent else None,
                            pair / 'after.json', pair / 'before.json')

    def assert_index(self, report):
        diff = report['inventoryDiff']
        expected = {}
        # Independently walk the existing partitions, rather than classify evidence again.
        for i, tag in enumerate(diff['tagRecordSetParticipation']):
            for field, values in tag.items():
                if field == 'image':
                    continue
                groups = values.items() if isinstance(values, dict) else [('', values)]
                for side, names in groups:
                    bucket = field + ('.' + side if side else '')
                    path = field + ('/' + side if side else '')
                    for j, name in enumerate(names):
                        key = name, tag['image']
                        self.assertNotIn(key, expected)
                        expected[key] = bucket, f'/inventoryDiff/tagRecordSetParticipation/{i}/{path}/{j}'
        declared = sorted({n['name'] for side in ('before', 'after') for n in diff[side]['nodes']})
        observed = [t['image'] for t in diff['tagRecordSetParticipation']]
        rows = diff['nodeTagEvidenceIndex']
        self.assertEqual([r['node'] for r in rows], declared)
        seen = []
        for row in rows:
            self.assertEqual(set(row), {'node', 'tags'})
            self.assertEqual([t['image'] for t in row['tags']], observed)
            for tag in row['tags']:
                self.assertEqual(set(tag), {'image', 'participationBucket', 'participationRef', 'evidenceRefs'})
                key = row['node'], tag['image']
                seen.append(key)
                bucket, pointer = expected[key]
                self.assertEqual((tag['participationBucket'], tag['participationRef']), (bucket, pointer))
                self.assertEqual(resolve(report, pointer), row['node'])
                refs = tag['evidenceRefs']
                self.assertEqual(refs, sorted(set(refs)))
                self.assertEqual(len(refs), 1)
                evidence = resolve(report, refs[0])
                if bucket in ('completePairNodes', 'incompletePairNodes'):
                    self.assertTrue(refs[0].startswith('/inventoryDiff/imageChanges/'))
                    self.assertEqual(evidence['image'], tag['image'])
                    parent = resolve(report, refs[0].split('/tags/')[0])
                    self.assertEqual(parent['node'], row['node'])
                    complete = all(evidence['recordSetComparison'][s]['status'] == 'complete'
                                   for s in ('before', 'after'))
                    self.assertEqual(complete, bucket == 'completePairNodes')
                else:
                    self.assertTrue(refs[0].startswith('/inventoryDiff/tagAvailabilityEvidence/'))
                    self.assertEqual(evidence['node'], row['node'])
                    parent = resolve(report, refs[0].split('/nodes/')[0])
                    self.assertEqual(parent['image'], tag['image'])
        self.assertEqual(sorted(seen), sorted(expected))
        return {r['node']: {t['image']: t for t in r['tags']} for r in rows}

    def test_all_declared_nodes_and_observed_tags_reference_existing_rows_once(self):
        report = self.report()
        diff = report['inventoryDiff']
        # The old report has all required evidence, scattered across tag-first views.
        self.assertEqual(len(diff['tagRecordSetParticipation']), 2)
        self.assertIn('partial-after', diff['tagRecordSetParticipation'][0]['incompletePairNodes'])
        index = self.assert_index(report)
        self.assertEqual(index['a'][IMAGE], {
            'image': IMAGE, 'participationBucket': 'completePairNodes',
            'participationRef': '/inventoryDiff/tagRecordSetParticipation/0/completePairNodes/0',
            'evidenceRefs': ['/inventoryDiff/imageChanges/0/tags/0']})
        self.assertEqual(index['unknown-absent-before'][IMAGE]['participationBucket'], 'missingCaptureNodes.beforeOnly')
        self.assertEqual(index['added-missing'][IMAGE]['participationBucket'], 'nodesAdded')
        self.assertEqual(index['removed-missing'][IMAGE]['participationBucket'], 'nodesRemoved')
        self.assertEqual(index['tag-added'][IMAGE]['participationBucket'], 'capturedTagAbsenceNodes.beforeOnly')
        for name in ['excluded', 'entering', 'leaving']:
            self.assertEqual(index[name][IMAGE]['participationBucket'], 'completePairNodes')
        self.assertEqual(diff, self.report(intent=False)['inventoryDiff'])
        # A second existing fixture includes repeated state pairs and duplicate aliases.
        self.assert_index(self.report('availability-cohorts'))

    def test_inventory_only_text_lists_pointers_without_intent_or_new_states(self):
        report = self.report(intent=False)
        self.assert_index(report)
        for key in ('assessments', 'imageOverview', 'loadPlan', 'loadPlanOmissions'):
            self.assertEqual(report[key], [])
        self.assertFalse(report['summary']['localUsePossibleEverywhere'])
        text = render_text(report)
        for row in report['inventoryDiff']['nodeTagEvidenceIndex']:
            self.assertIn(f"  Observed tag evidence index node={json.dumps(row['node'])} (all declared nodes, including schedulable=false):", text)
            for tag in row['tags']:
                self.assertIn(f"    {tag['image']}: participationBucket={tag['participationBucket']} "
                              f"participationRef={json.dumps(tag['participationRef'])} "
                              f"evidenceRefs={json.dumps(tag['evidenceRefs'])}", text)
        self.assertIn('Index pointers reference existing report rows only; no new availability, tag gains/losses, comparisons or workload intent inferred.', text)

    def test_canonical_universe_empty_tags_and_single_inventory_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / 'images.json'
            before, after, manifest = [root / n for n in ('before.json', 'after.json', 'pod.json')]
            manifest.write_text(json.dumps({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'unseen'},
                                           'spec': {'containers': [{'name': 'c', 'image': 'requested-only:v1'}]}}))
            data = {'schemaVersion': 1, 'cluster': 'test', 'nodes': {
                'z': {'schedulable': False, 'snapshot': str(capture)},
                'a': {'schedulable': True, 'snapshot': str(capture)}}}
            for path in (before, after):
                path.write_text(json.dumps(data))

            def report():
                return build_report([manifest], after, before)

            tags = ['demo:v1', 'docker.io/library/demo:v1', 'demo:V1', 'registry.test/demo:v1',
                    'docker.io:5000/library/demo:v1']
            capture.write_text(json.dumps({'images': [{'id': 'id-only:v1', 'repoTags': tags,
                                                       'repoDigests': ['unseen@sha256:' + 'a'*64]}]}))
            index = self.assert_index(report())
            self.assertEqual(list(index['a']), sorted([IMAGE, 'docker.io/library/demo:V1',
                                                      'registry.test/demo:v1', 'docker.io:5000/library/demo:v1']))
            self.assertTrue(all(t['participationBucket'] == 'incompletePairNodes' for t in index['z'].values()))
            # Neither digests/IDs nor manifest requests can supply observed tags.
            capture.write_text(json.dumps({'images': [{'id': 'id-only:v1', 'repoDigests': ['unseen@sha256:'+'a'*64]}]}))
            empty = [{'node': 'a', 'tags': []}, {'node': 'z', 'tags': []}]
            self.assertEqual(report()['inventoryDiff']['nodeTagEvidenceIndex'], empty)
            for path in (before, after):
                for node in data['nodes'].values():
                    node.pop('snapshot', None)
                path.write_text(json.dumps(data))
            result = report()
            self.assert_index(result)
            self.assertEqual(result['inventoryDiff']['nodeTagEvidenceIndex'], empty)
            self.assertIn('    (no observed tags)', render_text(result))
            self.assertNotIn('inventoryDiff', build_report([manifest], after))


if __name__ == '__main__':
    unittest.main()
