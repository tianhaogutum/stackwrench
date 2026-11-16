"""Resource comparison contracts: normalized identity, subfields, order and gates."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stackwrench.compose_merge import MergeError, compare_files

CLI = [sys.executable, '-m', 'stackwrench', 'compose-merge']


class ResourceCompareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def file(self, name, text):
        path = self.directory / (name + '.yaml')
        path.write_text(text)
        return str(path)

    def test_matches_resources_across_positions_and_separates_order(self):
        a = self.file('one', 'services:\n  web:\n    volumes:\n      - old:/cache\n      - stable:/stable\n')
        b = self.file('two', 'services:\n  web:\n    volumes:\n      - stable:/stable\n      - new:/cache\n')
        noise = self.file('noise', 'services: {web: {labels: {unrelated: value}}}')
        report = compare_files([a, noise, b], [b, a, noise])
        change, = report['differences']
        self.assertEqual(change['path'], '/services/web/volumes')
        self.assertEqual(change['resourceKey'], {'target': '/cache'})
        self.assertEqual(change['fieldPath'], '/source')
        for side, index, value, winner, input_index, winner_line, lines in [
            ('left', 0, 'new', b, 2, 5, [4, 5]), ('right', 1, 'old', a, 1, 4, [4, 4])
        ]:
            self.assertEqual(change[side]['path'], f'/services/web/volumes/{index}/source')
            self.assertEqual(change[side]['value'], value)
            self.assertEqual(change[side]['winningSources'], [dict(file=winner, input=input_index, line=winner_line, column=9)])
            self.assertEqual([op['source']['line'] for op in change[side]['operations']], lines)
            self.assertEqual([op['action'] for op in change[side]['operations']], ['add', 'replace'])
        self.assertEqual(change['decisiveOrder'], {
            'left': [dict(file=a, input=0), dict(file=b, input=2)],
            'right': [dict(file=b, input=0), dict(file=a, input=1)]})
        order, = report['resourceOrderChanges']
        self.assertEqual(order['path'], '/services/web/volumes')
        self.assertEqual(order['left']['identities'], [{'target': '/cache'}, {'target': '/stable'}])
        self.assertEqual(order['right']['identities'], [{'target': '/stable'}, {'target': '/cache'}])
        self.assertFalse(report['equivalent'])

    def test_nested_options_and_all_four_resource_kinds(self):
        cases = [
            ('volumes', '{type: volume, source: cache, target: /cache, volume: {nocopy: false, subpath: one}}',
             '{type: volume, source: cache, target: /cache, volume: {nocopy: true, subpath: two}}',
             {'target': '/cache'}, ['/volume/nocopy', '/volume/subpath']),
            ('ports', '{target: 80, published: "8080", name: one, app_protocol: http}',
             '{target: 80, published: 8080, name: two, app_protocol: https}',
             {'host_ip': '0.0.0.0', 'target': 80, 'published': '8080', 'protocol': 'tcp'}, ['/app_protocol', '/name']),
            ('secrets', '{source: one, target: /a~b, uid: "1", mode: 0444}',
             '{source: two, target: /a~b, uid: "2", mode: 0400}',
             {'target': '/a~b'}, ['/mode', '/source', '/uid']),
            ('configs', '{source: one, target: relative, gid: "1"}',
             '{source: two, target: relative, gid: "2"}',
             {'target': 'relative'}, ['/gid', '/source']),
        ]
        for kind, one, two, key, fields in cases:
            with self.subTest(kind=kind):
                a = self.file('one', f'services: {{web: {{{kind}: [{one}]}}}}')
                b = self.file('two', f'services: {{web: {{{kind}: [{two}]}}}}')
                report = compare_files([a, b], [b, a])
                self.assertEqual([d['fieldPath'] for d in report['differences']], fields)
                self.assertTrue(all(d['resourceKey'] == key for d in report['differences']))
                self.assertEqual(report['resourceOrderChanges'], [])

    def test_normalized_identity_and_literal_defaults(self):
        for kind, short, long in [
            ('volumes', 'cache:/cache:ro', '{source: cache, target: /cache, type: volume, read_only: true}'),
            ('ports', '8080:80', '{target: 80, published: "8080", protocol: tcp, mode: ingress}'),
            ('secrets', 'token', '{source: token, target: /run/secrets/token}'),
            ('configs', 'app', '{source: app, target: /app}'),
        ]:
            with self.subTest(kind=kind):
                a = self.file('one', f'services: {{web: {{{kind}: [{short}]}}}}')
                b = self.file('two', f'services: {{web: {{{kind}: [{long}]}}}}')
                report = compare_files([a, b], [b, a])
                self.assertTrue(report['equivalent'])
                self.assertEqual(report['differences'], [])
                self.assertEqual(report['resourceOrderChanges'], [])
        a = self.file('one', 'services: {web: {ports: !override [{target: 80}]}}')
        b = self.file('two', 'services: {web: {ports: !override [{target: 80, host_ip: 0.0.0.0}]}}')
        report = compare_files([a, b], [b, a])
        change, = report['differences']
        self.assertEqual(change['fieldPath'], '/host_ip')
        self.assertTrue(change['left']['present'])
        self.assertFalse(change['right']['present'])
        self.assertIsNone(change['right']['path'])
        self.assertEqual(report['resourceOrderChanges'], [])

    def test_ancestor_override_and_reset_keep_identity_scoped_history(self):
        for tag in ['!override {}', '!reset null']:
            with self.subTest(tag=tag):
                a = self.file('one', 'services: {web: {configs: [{source: old, target: /same}]}}')
                b = self.file('clear', f'services: {tag}')
                c = self.file('three', 'services: {web: {configs: [{source: new, target: /same}]}}')
                change, = compare_files([a, b, c], [c, b, a])['differences']
                self.assertEqual(change['resourceKey'], {'target': '/same'})
                self.assertEqual(change['fieldPath'], '/source')
                for side in ['left', 'right']:
                    self.assertEqual([op['action'] for op in change[side]['operations']],
                                     ['add', tag.split()[0][1:], 'add'])
                    self.assertEqual(change[side]['operations'][1]['source'],
                                     dict(file=b, input=1, line=1, column=11))

    def test_subtree_options_and_reordering_with_membership_changes(self):
        a = self.file('one', 'services: {web: {volumes: !override [{type: volume, source: cache, target: /cache}, a:/a, b:/b]}}')
        b = self.file('two', 'services: {web: {volumes: !override [b:/b, {type: volume, source: cache, target: /cache, volume: {nocopy: true}}, c:/c, a:/a]}}')
        report = compare_files([a, b], [b, a])
        self.assertEqual([(d['resourceKey'], d['fieldPath']) for d in report['differences']],
                         [({'target': '/c'}, ''), ({'target': '/cache'}, '/volume')])
        change = report['differences'][1]
        self.assertEqual(change['left']['value'], {'nocopy': True})
        self.assertFalse(change['right']['present'])
        self.assertEqual(len(change['left']['operations']), 1)
        order, = report['resourceOrderChanges']
        self.assertEqual(len(order['left']['identities']), 4)
        self.assertEqual(len(order['right']['identities']), 3)

    def test_override_reordering_records_each_resource_operation_once(self):
        a = self.file('one', 'services: {web: {volumes: !override [old:/cache, other:/other]}}')
        b = self.file('two', 'services: {web: {volumes: !override [other:/other, new:/cache]}}')
        report = compare_files([a, b], [b, a])
        change, = report['differences']
        self.assertEqual(change['fieldPath'], '/source')
        self.assertEqual(change['resourceKey'], {'target': '/cache'})
        for side, order in [('left', [a, b]), ('right', [b, a])]:
            self.assertEqual([op['source']['file'] for op in change[side]['operations']], order)
            self.assertEqual([op['action'] for op in change[side]['operations']], ['override', 'override'])

    def test_pure_order_and_port_identity_dimensions(self):
        # Every uniqueness dimension distinguishes a resource; compare no array positions.
        entries = ['{target: 80}', '{target: 81}', '{target: 80, published: 8080}',
                   '{target: 80, protocol: udp}', '{target: 80, host_ip: 127.0.0.1}']
        a = self.file('one', 'services: {web: {ports: [' + ','.join(entries) + ']}}')
        b = self.file('two', 'services: {web: {ports: [' + ','.join(reversed(entries)) + ']}}')
        report = compare_files([a, b], [b, a])
        self.assertEqual(report['differences'], [])
        order, = report['resourceOrderChanges']
        self.assertEqual(len(order['left']['identities']), 5)
        self.assertEqual(order['left']['identities'], list(reversed(order['right']['identities'])))
        self.assertFalse(report['equivalent'])

    def test_membership_and_empty_or_missing_list_boundaries(self):
        a = self.file('one', 'services: {web: {configs: !override [a, common]}}')
        b = self.file('two', 'services: {web: {configs: !override [common, b]}}')
        report = compare_files([a, b], [b, a])
        self.assertEqual([d['resourceKey'] for d in report['differences']], [{'target': '/a'}, {'target': '/b'}])
        for change in report['differences']:
            self.assertEqual(change['fieldPath'], '')
            missing = 'left' if change['resourceKey']['target'] == '/a' else 'right'
            self.assertFalse(change[missing]['present'])
            self.assertIsNone(change[missing]['path'])
            self.assertEqual(change[missing]['winningSources'], [])
            self.assertEqual(change[missing]['operations'][-1]['action'], 'override')
        self.assertEqual(report['resourceOrderChanges'], [])
        a = self.file('one', 'services: {web: {configs: !override []}}')
        report = compare_files([a, b], [b, a])
        self.assertEqual(len(report['differences']), 2)
        self.assertEqual(report['resourceOrderChanges'], [])
        b = self.file('two', 'services: {web: {configs: !reset null}}')
        change, = compare_files([a, b], [b, a])['differences']
        self.assertEqual(change['path'], '/services/web/configs')
        self.assertNotIn('resourceKey', change)
        self.assertFalse(change['left']['present'])
        self.assertEqual(change['right']['value'], [])
        self.assertTrue(compare_files([a], [a])['equivalent'])

    def test_history_does_not_follow_reused_array_index(self):
        # A temporarily absent resource is re-added at a different numeric position.
        a = self.file('one', 'services: {web: {volumes: [old:/cache]}}')
        b = self.file('two', 'services: {web: {volumes: !override [other:/other]}}')
        c = self.file('three', 'services: {web: {volumes: [new:/cache]}}')
        report = compare_files([a, b, c], [c, b, a])
        change, = report['differences']
        self.assertEqual(change['resourceKey'], {'target': '/cache'})
        self.assertEqual(change['fieldPath'], '/source')
        for side, value in [('left', 'new'), ('right', 'old')]:
            self.assertEqual(change[side]['value'], value)
            self.assertEqual(change[side]['path'], '/services/web/volumes/1/source')
            self.assertEqual([op['action'] for op in change[side]['operations']], ['add', 'override', 'add'])
        self.assertEqual(report['resourceOrderChanges'], [])
        # An unrelated former occupant must not appear in the current resource evidence.
        noise = self.file('noise', 'services: {web: {volumes: [noise:/noise]}}')
        clear = self.file('clear', 'services: {web: {volumes: !override []}}')
        report = compare_files([noise, clear, a, c], [noise, clear, c, a])
        change, = report['differences']
        for side in ['left', 'right']:
            self.assertEqual(len(change[side]['operations']), 2)
            self.assertNotIn(noise, [op['source']['file'] for op in change[side]['operations']])
            self.assertEqual(len(change['decisiveOrder'][side]), 2)

    def test_generic_arrays_and_extension_shapes_are_not_resources(self):
        a = self.file('one', 'x-data: {services: {web: {volumes: [one]}}}\nservices: {web: {command: [one], dns: [1.1.1.1]}}')
        b = self.file('two', 'x-data: {services: {web: {volumes: [two]}}}\nservices: {web: {command: [two], dns: [8.8.8.8]}}')
        report = compare_files([a, b], [b, a])
        self.assertEqual([d['path'] for d in report['differences']], [
            '/services/web/command', '/services/web/dns', '/x-data/services/web/volumes'])
        self.assertTrue(all('resourceKey' not in d for d in report['differences']))
        self.assertEqual(report['resourceOrderChanges'], [])

    def test_gates_on_either_side_and_cli_empty_stdout(self):
        cases = [
            ('ports: [80]', 'ports: [{target: 80, name: named}]', 'omits'),
            ('volumes: [cache:/cache]', 'volumes: [{type: bind, source: ./data, target: /cache}]', 'mount type'),
            ('ports: [80]', 'ports: [80, 80]', 'duplicate'),
            ('configs: [app]', 'configs: ["${HOST}"]', 'interpolation'),
            ('configs: [app]', 'configs: [!override app]', 'tags'),
        ]
        for first, second, message in cases:
            a = self.file('one', f'services: {{web: {{{first}}}}}')
            b = self.file('two', f'services: {{web: {{{second}}}}}')
            for left, right in [([a, b], [b, a]), ([b, a], [a, b])]:
                with self.subTest(first=first, left=left):
                    with self.assertRaisesRegex(MergeError, message):
                        compare_files(left, right)
                    args = [*CLI, 'compare']
                    args += [v for file in left for v in ('--left', file)]
                    args += [v for file in right for v in ('--right', file)]
                    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn(message, result.stderr)

    def test_documented_resource_example_matches_complete_report(self):
        left = ['examples/compose_merge/resources-one.yaml', 'examples/compose_merge/resources-two.yaml']
        right = list(reversed(left))
        expected = json.loads(Path('examples/compose_merge/resource-comparison.json').read_text())
        self.assertEqual(compare_files(left, right), expected)
