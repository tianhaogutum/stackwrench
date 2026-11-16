"""Subtree replacement pairs children by exact RFC 6901 token, across types."""
import copy
import json
import sys
import unittest

from stackwrench.compose_merge.core import MergeError, Project, parse


def source(file, line, column, index):
    return dict(file=file, line=line, column=column, input=index)


class ReplacementHistoryTests(unittest.TestCase):
    def assert_transition(self, project, path, before, value, present=True,
                          action='override', location=None):
        info = project.explain(path)
        self.assertEqual(len(info['events']), len(before['events']) + 1)
        event = info['events'][-1]
        self.assertEqual(event['action'], action)
        self.assertEqual(event['previousPresent'], before['present'])
        self.assertEqual(event['previous'], before['value'])
        self.assertEqual(event['previousSources'], before['winningSources'])
        self.assertEqual((event['present'], event['value']), (present, value))
        self.assertEqual((info['present'], info['value']), (present, value))
        if location:
            self.assertEqual(event['source'], location)
        return info

    def test_same_pointer_both_container_directions(self):
        for base, later, expected, old_column, new_column in [
            ('x-data: {"0": old}', 'x-data: !override [new]', ['new'], 15, 20),
            ('x-data: [old]', 'x-data: !override {"0": new}', {'0': 'new'}, 10, 25),
        ]:
            with self.subTest(base=base):
                nodes = [parse(base, 'base.yaml'), parse(later, 'next.yaml', 1)]
                original = copy.deepcopy(nodes)
                project = Project().add(nodes[0])
                before = project.explain('/x-data/0')
                self.assertEqual(before['winningSources'], [source('base.yaml', 1, old_column, 0)])
                project.add(nodes[1])
                self.assertEqual(project.render(), {'x-data': expected})
                info = self.assert_transition(project, '/x-data/0', before, 'new',
                                              location=source('next.yaml', 1, 9, 1))
                self.assertEqual(info['winningSources'], [source('next.yaml', 1, new_column, 1)])
                self.assertEqual(nodes, original)

    def test_multiple_indexes_and_noncanonical_neighbors(self):
        mapping = '{"0": old0, "1": old1, "10": old10, "01": leading, "99": far}'
        sequence = '[' + ', '.join('new' + str(i) for i in range(11)) + ']'
        for reverse in [False, True]:
            with self.subTest(reverse=reverse):
                base, later = (sequence, mapping) if reverse else (mapping, sequence)
                project = Project().add(parse('x-data: ' + base, 'base.yaml'))
                before = {k: project.explain('/x-data/' + k) for k in ['0', '1', '10']}
                project.add(parse('x-data: !override ' + later, 'next.yaml', 1))
                for key in before:
                    self.assert_transition(project, '/x-data/' + key, before[key],
                                           ('old' if reverse else 'new') + key)
                for key in ['01', '99']:
                    info = project.explain('/x-data/' + key)
                    self.assertEqual(info['present'], reverse)
                    self.assertEqual(info['events'][-1]['previousPresent'], not reverse)
                with self.assertRaisesRegex(MergeError, 'path has no history'):
                    project.explain('/x-data/010')

    def test_nested_cross_types_and_removed_replaced_descendants(self):
        cases = [
            ('{"0": {"0": {gone: old, same: before}, "1": {leaf: drop}}}',
             '[[{same: after, added: live}]]',
             [[{'same': 'after', 'added': 'live'}]], '/x-data/0/1/leaf'),
            ('[[{gone: old, same: before}, {leaf: drop}]]',
             '{"0": {"0": {same: after, added: live}}}',
             {'0': {'0': {'same': 'after', 'added': 'live'}}}, '/x-data/0/1/leaf'),
        ]
        for base, later, expected, removed in cases:
            with self.subTest(base=base):
                nodes = [parse('x-data: ' + base, 'base.yaml'),
                         parse('x-data: !override ' + later, 'next.yaml', 1)]
                original = copy.deepcopy(nodes)
                project = Project().add(nodes[0])
                paths = ['/x-data/0', '/x-data/0/0', '/x-data/0/0/same',
                         '/x-data/0/0/gone', '/x-data/0/1', removed]
                before = {p: project.explain(p) for p in paths}
                project.add(nodes[1])
                self.assertEqual(project.render(), {'x-data': expected})
                for path, value, present in [
                    ('/x-data/0', expected[0] if isinstance(expected, list) else expected['0'], True),
                    ('/x-data/0/0', {'same': 'after', 'added': 'live'}, True),
                    ('/x-data/0/0/same', 'after', True),
                    ('/x-data/0/0/gone', None, False),
                    ('/x-data/0/1', None, False), (removed, None, False),
                ]:
                    self.assert_transition(project, path, before[path], value, present,
                                           location=source('next.yaml', 1, 9, 1))
                added = project.explain('/x-data/0/0/added')['events']
                self.assertEqual(len(added), 1)
                self.assertFalse(added[0]['previousPresent'])
                self.assertEqual(nodes, original)

    def test_numeric_keys_nested_tags_and_reset_keep_source_scope(self):
        nodes = [parse('x-data: [{gone: {leaf: old}, keep: before}, {leaf: drop}]', 'base.yaml'),
                 parse('x-data: !override\n  "0": !override\n    gone: !reset null\n'
                       '    keep: after\n  "1": !reset null\n', 'next.yaml', 1)]
        original = copy.deepcopy(nodes)
        project = Project().add(nodes[0])
        paths = ['/x-data', '/x-data/0', '/x-data/0/gone', '/x-data/0/gone/leaf',
                 '/x-data/0/keep', '/x-data/1', '/x-data/1/leaf']
        before = {p: project.explain(p) for p in paths}
        project.add(nodes[1])
        self.assertEqual(project.render(), {'x-data': {'0': {'keep': 'after'}}})
        for path, value, present, action, line, column in [
            ('/x-data', {'0': {'keep': 'after'}}, True, 'override', 1, 9),
            ('/x-data/0', {'keep': 'after'}, True, 'override', 2, 8),
            ('/x-data/0/keep', 'after', True, 'override', 2, 8),
            ('/x-data/0/gone', None, False, 'reset', 3, 11),
            ('/x-data/0/gone/leaf', None, False, 'reset', 3, 11),
            ('/x-data/1', None, False, 'reset', 5, 8),
            ('/x-data/1/leaf', None, False, 'reset', 5, 8),
        ]:
            self.assert_transition(project, path, before[path], value, present, action,
                                   source('next.yaml', line, column, 1))
        self.assertEqual(project.explain('/x-data/0/keep')['winningSources'],
                         [source('next.yaml', 4, 11, 1)])
        self.assertEqual(nodes, original)

    def test_noncanonical_keys_escaping_and_no_history(self):
        keys = ['00', '01', '-0', '+0', '1.0', '-']
        for reverse in [False, True]:
            with self.subTest(reverse=reverse):
                mapping = '{' + ', '.join('"' + k + '": old' for k in keys) + ', "0": before}'
                base, later = ('[before]', mapping) if reverse else (mapping, '[after]')
                project = Project().add(parse('x-data: {"a/b~c": {"": ' + base + '}}', 'base.yaml'))
                prefix = '/x-data/a~1b~0c/'
                before = project.explain(prefix + '/0')
                old_sources = {k: project.explain(prefix + '/' + k)['winningSources']
                               for k in keys} if not reverse else {}
                project.add(parse('x-data: !override {"a/b~c": {"": ' + later + '}}', 'next.yaml', 1))
                self.assert_transition(project, prefix + '/0', before, 'before' if reverse else 'after')
                for key in keys:
                    info = project.explain(prefix + '/' + key)
                    event = info['events'][-1]
                    self.assertEqual(info['present'], reverse)
                    self.assertEqual(len(info['events']), 1 if reverse else 2)
                    self.assertEqual(event['previousPresent'], not reverse)
                    self.assertEqual(event['previousSources'], old_sources.get(key, []))
                for path in [prefix + '/never', '/x-data/a/b~0c', prefix + '/2']:
                    with self.assertRaisesRegex(MergeError, 'path has no history'):
                        project.explain(path)
        project = Project().add(parse('x-data: [old]', 'base.yaml'))
        for key in keys:
            with self.assertRaisesRegex(MergeError, 'path has no history'):
                project.explain('/x-data/' + key)
        with self.assertRaisesRegex(MergeError, 'RFC 6901'):
            project.explain('/x-data/~2')

    def test_scalar_null_boundaries_and_real_removal_readd(self):
        for base, later, value in [
            ('{"0": {leaf: old}}', '[null]', None),
            ('[{leaf: old}]', '{"0": new}', 'new'),
            ('{"0": null}', '[{leaf: new}]', {'leaf': 'new'}),
            ('[old]', '{"0": {leaf: new}}', {'leaf': 'new'}),
        ]:
            with self.subTest(base=base):
                project = Project().add(parse('x-data: ' + base, 'base.yaml'))
                before = project.explain('/x-data/0')
                project.add(parse('x-data: !override ' + later, 'next.yaml', 1))
                self.assert_transition(project, '/x-data/0', before, value)
                snapshot = project.explain('/x-data/0')
                project.add(parse('x-data: !reset null', 'reset.yaml', 2))
                project.add(parse('x-data: [again]', 'last.yaml', 3))
                info = project.explain('/x-data/0')
                self.assertEqual([e['action'] for e in info['events']], ['add', 'override', 'reset', 'add'])
                self.assertEqual(info['events'][:2], snapshot['events'])
                self.assertFalse(info['events'][-1]['previousPresent'])

    def test_type_changes_still_require_override(self):
        for base, later in [('{"0": old}', '[new]'), ('[old]', '{"0": new}')]:
            project = Project().add(parse('x-data: ' + base, 'base.yaml'))
            with self.assertRaisesRegex(MergeError, 'type change'):
                project.add(parse('x-data: ' + later, 'next.yaml', 1))

    def test_removed_huge_numeric_key_over_list_retains_history(self):
        limit = sys.get_int_max_str_digits()
        key = '9' * 4301
        for sequence in ['[new]', '[]']:
            with self.subTest(sequence=sequence):
                nodes = [parse('x-data:\n  ? "' + key + '"\n  : old\n', 'base.yaml'),
                         parse('x-data: !override ' + sequence, 'next.yaml', 1)]
                original = copy.deepcopy(nodes)
                project = Project().add(nodes[0])
                path = '/x-data/' + key
                before = project.explain(path)
                self.assertEqual(before['winningSources'], [source('base.yaml', 3, 5, 0)])
                project.add(nodes[1])
                self.assertEqual(project.render(), {'x-data': ['new'] if sequence == '[new]' else []})
                info = self.assert_transition(project, path, before, None, False,
                                              location=source('next.yaml', 1, 9, 1))
                self.assertEqual(info['winningSources'], [])
                self.assertEqual([e['action'] for e in info['events']], ['add', 'override'])
                self.assertEqual(nodes, original)
        self.assertEqual(sys.get_int_max_str_digits(), limit)

    def test_long_keys_nested_escaped_removal_and_readd(self):
        for key in ['1' + '0' * 4300, '0' * 4301]:
            with self.subTest(key_prefix=key[:1]):
                quoted = json.dumps(key)
                nodes = [parse('x-data: {"a/b~c": {"": {\n  ? ' + quoted + '\n'
                               '  : {leaf: old}\n}}}', 'base.yaml'),
                         parse('x-data: !override {"a/b~c": {"": [new]}}', 'next.yaml', 1),
                         parse('x-data: !override\n  "a/b~c":\n    "":\n'
                               '      ? ' + quoted + '\n      : !override\n'
                               '        leaf: restored\n        gone: !reset null\n', 'last.yaml', 2)]
                original = copy.deepcopy(nodes)
                project = Project().add(nodes[0])
                path = '/x-data/a~1b~0c//' + key
                before = {p: project.explain(p) for p in [path, path + '/leaf']}
                project.add(nodes[1])
                for p in before:
                    self.assert_transition(project, p, before[p], None, False,
                                           location=source('next.yaml', 1, 9, 1))
                removed = project.explain(path + '/leaf')
                self.assertEqual(project.render(), {'x-data': {'a/b~c': {'': ['new']}}})
                project.add(nodes[2])
                info = project.explain(path + '/leaf')
                self.assertEqual(info['events'][:-1], removed['events'])
                self.assertEqual(info['value'], 'restored')
                self.assertFalse(info['events'][-1]['previousPresent'])
                self.assertEqual(info['events'][-1]['source'], source('last.yaml', 5, 9, 2))
                self.assertEqual(info['winningSources'], [source('last.yaml', 6, 15, 2)])
                reset_info = project.explain(path + '/gone')
                self.assertFalse(reset_info['present'])
                self.assertEqual(reset_info['events'][-1]['action'], 'reset')
                self.assertEqual(reset_info['events'][-1]['source'], source('last.yaml', 7, 15, 2))
                self.assertEqual(nodes, original)

    def test_list_index_boundaries_preserve_exact_tokens(self):
        keys = ['0', '9', '10', '11', '99', '00', '01', '-0', '+0', '١', '0' * 4301]
        mapping = ''.join('  ? ' + json.dumps(k) + '\n  : old\n' for k in keys)
        project = Project().add(parse('x-data:\n' + mapping, 'base.yaml'))
        before = {k: project.explain('/x-data/' + k) for k in keys}
        project.add(parse('x-data: !override [' + ', '.join('new' + str(i) for i in range(11)) + ']', 'next.yaml', 1))
        for key in keys:
            present = key in ['0', '9', '10']
            info = self.assert_transition(project, '/x-data/' + key, before[key],
                                          'new' + key if present else None, present)
            if not present:
                self.assertEqual(info['winningSources'], [])
        for key in ['never', '8' * 4301, '0' * 4302]:
            with self.assertRaisesRegex(MergeError, 'path has no history'):
                project.explain('/x-data/' + key)
        with self.assertRaisesRegex(MergeError, 'RFC 6901'):
            project.explain('/x-data/~2')
