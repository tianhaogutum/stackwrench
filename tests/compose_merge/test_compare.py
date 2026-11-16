"""Explicit order comparison: exact values, causal declarations and fail-closed gates."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.compose_merge import MergeError
from stackwrench.compose_merge.core import Project, parse

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, '-m', 'stackwrench', 'compose-merge']


def compare(left, right):
    from stackwrench.compose_merge import compare_files
    return compare_files(left, right)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def file(self, name, text):
        file = self.directory / (name + '.yaml')
        file.write_text(text)
        return str(file)

    def call(self, left, right):
        args = [*CLI, 'compare']
        args += [arg for file in left for arg in ('--left', file)]
        args += [arg for file in right for arg in ('--right', file)]
        return subprocess.run(args, capture_output=True, text=True, timeout=20)

    def test_overwrite_reports_only_changed_field_and_relevant_order(self):
        a = self.file('one', 'services:\n  web:\n    image: one\n')
        b = self.file('two', 'services:\n  web:\n    image: two\n')
        unrelated = self.file('labels', 'services: {web: {labels: {stable: value}}}')
        left, right = [a, unrelated, b], [b, a, unrelated]
        report = compare(left, right)
        self.assertFalse(report['equivalent'])
        self.assertEqual(report['leftFiles'], left)
        self.assertEqual(report['rightFiles'], right)
        self.assertEqual(len(report['differences']), 1)
        change = report['differences'][0]
        self.assertEqual(change['path'], '/services/web/image')
        for side, value, winner, index in [('left', 'two', b, 2), ('right', 'one', a, 1)]:
            self.assertEqual(change[side]['value'], value)
            self.assertTrue(change[side]['present'])
            self.assertEqual(change[side]['winningSources'], [dict(file=winner, line=3, column=12, input=index)])
        self.assertEqual(change['decisiveOrder'], {
            'left': [dict(file=a, input=0), dict(file=b, input=2)],
            'right': [dict(file=b, input=0), dict(file=a, input=1)],
        })
        self.assertEqual([v['action'] for v in change['left']['operations']], ['add', 'replace'])
        self.assertEqual(change['left']['operations'][-1]['source']['line'], 3)

    def test_unrelated_reordering_and_same_value_writers_have_no_noise(self):
        a = self.file('one', 'services: {web: {image: same}}')
        b = self.file('two', 'services: {web: {image: same}}')
        c = self.file('labels', 'services: {web: {labels: {stable: value}}}')
        for left, right in [([a, c], [c, a]), ([a, b, c], [c, b, a]), ([a], [a])]:
            with self.subTest(left=left):
                report = compare(left, right)
                self.assertTrue(report['equivalent'])
                self.assertEqual(report['differences'], [])

    def test_generic_arrays_are_fields(self):
        for field, first, second, expected in [
            ('command', '[echo, one]', '[echo, two]', ['echo', 'two']),
            ('dns', '[1.1.1.1]', '[8.8.8.8]', ['1.1.1.1', '8.8.8.8']),
        ]:
            with self.subTest(field=field):
                a = self.file('one', f'services: {{web: {{{field}: {first}}}}}')
                b = self.file('two', f'services: {{web: {{{field}: {second}}}}}')
                report = compare([a, b], [b, a])
                self.assertEqual([d['path'] for d in report['differences']], ['/services/web/' + field])
                if expected is not None:
                    self.assertEqual(report['differences'][0]['left']['value'], expected)
                self.assertEqual(len(report['differences'][0]['decisiveOrder']['left']), 2)

    def test_reset_absence_null_types_and_escaped_pointers(self):
        for first, second, path, left_present, left_value, right_value in [
            ('x-data: {"a/b~": live}', 'x-data: {"a/b~": !reset null}', '/x-data/a~1b~0', False, None, 'live'),
            ('x-data: {value: live}', 'x-data: !override {gone: !reset null}', '/x-data/value', False, None, 'live'),
            ('services: {web: {command: [echo]}}', 'services: {web: {command: null}}', '/services/web/command', True, None, ['echo']),
            ('x-value: !override true', 'x-value: !override 1', '/x-value', True, 1, True),
            ('x-value: !override 1', 'x-value: !override 1.0', '/x-value', True, 1.0, 1),
            ('x-value: null', 'x-value: !reset null', '/x-value', False, None, None),
            ('x-value: !override {}', 'x-value: !override []', '/x-value', True, [], {}),
        ]:
            with self.subTest(first=first, second=second):
                a, b = self.file('one', first), self.file('two', second)
                change, = compare([a, b], [b, a])['differences']
                self.assertEqual(change['path'], path)
                self.assertEqual(change['left']['present'], left_present)
                self.assertEqual(change['left']['value'], left_value)
                self.assertEqual(change['right']['value'], right_value)
                self.assertEqual([v['file'] for v in change['decisiveOrder']['left']], [a, b])

    def test_mapping_addition_is_one_subtree_without_parent_noise(self):
        a = self.file('one', 'x-data: {nested: {a: live, b: []}, stable: same}')
        b = self.file('two', 'x-data: {nested: !reset null}')
        change, = compare([a, b], [b, a])['differences']
        self.assertEqual(change['path'], '/x-data/nested')
        self.assertFalse(change['left']['present'])
        self.assertEqual(change['right']['value'], {'a': 'live', 'b': []})

    def test_cli_matches_library_is_stable_and_does_not_modify_inputs(self):
        a = self.file('one', 'services: {web: {image: one}}')
        b = self.file('two', 'services: {web: {image: two}}')
        before = [hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in [a, b]]
        outputs = [self.call([a, b], [b, a]) for _ in range(2)]
        for result in outputs:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), compare([a, b], [b, a]))
        self.assertEqual(outputs[0].stdout, outputs[1].stdout)
        self.assertEqual(before, [hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in [a, b]])
        self.assertEqual(json.loads(self.call([a, b], [a, b]).stdout)['differences'], [])

    def test_each_file_is_read_once_for_both_orders(self):
        a = self.file('one', 'x-a: one')
        b = self.file('two', 'x-b: two')
        original = Path.open
        reads = []
        def counted(file, *args, **kwargs):
            reads.append(str(file))
            return original(file, *args, **kwargs)
        with patch.object(Path, 'open', counted):
            self.assertTrue(compare([a, b], [b, a])['equivalent'])
        self.assertEqual(reads, [a, b])

    def test_either_unevaluated_order_fails_without_partial_report(self):
        # The first order is evaluable; the reversed order omits an old port option.
        a = self.file('one', 'services: {web: {ports: [80]}}')
        b = self.file('two', 'services: {web: {ports: [{target: 80, name: named}]}}')
        for left, right in [([a, b], [b, a]), ([b, a], [a, b])]:
            with self.subTest(left=left):
                with self.assertRaisesRegex(MergeError, 'omits'):
                    compare(left, right)
                result = self.call(left, right)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('omits', result.stderr)

    def test_invalid_files_orders_and_per_side_limits_fail_closed(self):
        a = self.file('one', 'x-data: live')
        for text in ['services: {web: {build: .}}', 'x-data: "${HOST}"',
                     'x-data: !unknown null', 'x-data: [', 'x-data: ' + '[' * 70 + '0' + ']' * 70,
                     'x-data: ' + 'z' * (1024 * 1024)]:
            b = self.file('bad', text)
            with self.subTest(text=text[:50]):
                with self.assertRaises(MergeError):
                    compare([a, b], [b, a])
                result = self.call([a, b], [b, a])
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)
        for left, right in [([], []), ([a], []), ([a], [a, a]), ([a, a], [a, a]),
                            ([a] * 33, [a] * 33), ([a], [str(self.directory / 'other')])]:
            with self.subTest(left=left, right=right):
                with self.assertRaises(MergeError):
                    compare(left, right)
                result = self.call(left, right)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
        Path(a).write_bytes(b'\xff')
        self.assertEqual(self.call([a], [a]).stdout, '')
        self.assertEqual(self.call([a], [a]).returncode, 2)

    def test_combined_node_and_event_limits_apply_to_both_evaluations(self):
        # Small deterministic budgets exercise the real gates without huge traces.
        a = self.file('one', 'x-a: [one, two]')
        b = self.file('two', 'x-b: [three, four]')
        from stackwrench.compose_merge import core
        with patch.object(core, 'MAX_BYTES', 10):
            with self.assertRaisesRegex(MergeError, 'bytes'):
                compare([a], [a])
        small_files = [self.file(str(i), f'x-{i}: ' + 'z' * 38) for i in range(5)]
        with patch.object(core, 'MAX_BYTES', 50):
            with self.assertRaisesRegex(MergeError, 'combined input exceeds 4 MiB'):
                compare(small_files, small_files[::-1])
        with patch.object(core, 'MAX_NODES', 3):
            with self.assertRaisesRegex(MergeError, 'depth/node limit'):
                compare([a], [a])
        def many_nodes(text, filename, index=0):
            return parse('x-data: [' + ','.join('0' for _ in range(34000)) + ']', filename, index)
        c = self.file('three', 'x-c: []')
        with patch.object(core, 'parse', side_effect=many_nodes):
            with self.assertRaisesRegex(MergeError, 'combined input exceeds 100000 nodes'):
                compare([a, b, c], [c, b, a])
        # Existing trace limit is enforced by Project.record in each order.
        record = Project.record
        def bounded_record(project, *args, **kwargs):
            if len(project.events) >= 8:
                raise MergeError('trace event limit (synthetic 8) exceeded')
            return record(project, *args, **kwargs)
        a = self.file('one', 'x-data: !override {}')
        b = self.file('two', 'x-data: {nested: {one: a, two: b}}')
        # a,b needs 7 events; b,a needs 10. Only the right evaluation fails.
        with patch.object(Project, 'record', bounded_record):
            with self.assertRaisesRegex(MergeError, 'right order unevaluated: trace event limit'):
                compare([a, b], [b, a])

    def test_maximum_file_count_is_accepted(self):
        files = [self.file(str(i), f'x-{i}: value') for i in range(32)]
        self.assertTrue(compare(files, files[::-1])['equivalent'])

    def test_documented_example_matches_complete_report(self):
        left = ['examples/compose_merge/order-one.yaml', 'examples/compose_merge/order-labels.yaml', 'examples/compose_merge/order-two.yaml']
        right = ['examples/compose_merge/order-two.yaml', 'examples/compose_merge/order-one.yaml', 'examples/compose_merge/order-labels.yaml']
        result = self.call(left, right)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = json.loads((ROOT / 'examples/compose_merge/order-comparison.json').read_text())
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual([d['path'] for d in expected['differences']], ['/services/web/image'])


class OverrideResetTests(unittest.TestCase):
    def test_reset_inside_override_has_own_source_even_without_old_path(self):
        for base in [None, 'x-data: {gone: old, keep: before}']:
            with self.subTest(base=base):
                project = Project()
                if base:
                    project.add(parse(base, 'base.yaml'))
                node = parse('x-data: !override\n  gone: !reset null\n  nested:\n    removed: !reset {}\n  keep: live\n', 'override.yaml', 1)
                project.add(node)
                self.assertEqual(project.render(), {'x-data': {'nested': {}, 'keep': 'live'}})
                for path, line, column in [('/x-data/gone', 2, 9), ('/x-data/nested/removed', 4, 14)]:
                    info = project.explain(path)
                    self.assertFalse(info['present'])
                    self.assertEqual(info['winningSources'], [])
                    event = info['events'][-1]
                    self.assertEqual(event['action'], 'reset')
                    self.assertEqual(event['source'], dict(file='override.yaml', line=line, column=column, input=1))
                    self.assertEqual(event['previousPresent'], bool(base) and path.endswith('/gone'))
                self.assertEqual(project.explain('/x-data')['events'][-1]['action'], 'override')
                self.assertEqual(node.value['x-data'].value['gone'].tag, '!reset')
                project.add(parse('x-data: {gone: again}', 'last.yaml', 2))
                self.assertEqual([e['action'] for e in project.explain('/x-data/gone')['events']][-2:], ['reset', 'add'])

    def test_installed_cli_explains_never_present_reset_inside_override(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'override.yaml'
            file.write_text('x-data: !override\n  gone: !reset null\n  keep: live\n')
            result = subprocess.run([*CLI, 'explain', '-f', str(file), '--path', '/x-data/gone'],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            info = json.loads(result.stdout)
            self.assertFalse(info['present'])
            event, = info['events']
            self.assertEqual(event['action'], 'reset')
            self.assertEqual(event['source'], dict(file=str(file), line=2, column=9, input=0))
            self.assertFalse(event['previousPresent'])
