"""Complete saved subtree selection before independently limiting displayed rows."""

import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from . import test_snapshot_inspection as inspection_tests
from stackwrench.context_budget.cli import main
from stackwrench.context_budget.core import analyze
from stackwrench.context_budget.snapshot import MAX_SNAPSHOT_BYTES, from_report, write_snapshot


class SnapshotSubtreeTests(unittest.TestCase):
    run_cli = inspection_tests.SnapshotInspectionTests.run_cli

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / 'context'
        self.context.mkdir()
        files = {'tree/Dockerfile': b'FROM scratch\n', 'tree/a': b'123',
                 'tree/nested/zero': b'', 'tree/nested/data': b'12345',
                 'tree/excluded/drop': b'x' * 19, 'tree/excluded/keep': b'12',
                 'tree/excluded/目录\n\x1b[31m\t': b'1', 'tree2/outside': b'x' * 101,
                 'treeish': b'x' * 103, 'outside': b'1234567', '全\n\x1b[31m/zero': b''}
        for name, data in files.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.context / 'tree/empty').mkdir()
        (self.context / 'tree/link').symlink_to('/never-open-this-target')
        (self.context / 'tree/alias').symlink_to('nested', target_is_directory=True)
        self.rules = self.context / 'tree/.ignore'
        self.rules.write_text('tree/excluded\n!tree/excluded/keep\n!tree/excluded/目录*\ntree/link\ntree/Dockerfile\n')
        self.snapshot = self.work / 'saved.json'
        self.saved = from_report(analyze(self.context, dockerfile='tree/Dockerfile',
                                        ignore=self.rules, include_link_targets=False))
        write_snapshot(self.snapshot, self.saved)
        shutil.rmtree(self.context)

    def test_subtree_complete_totals_and_rows_before_limit(self):
        plain = self.run_cli()
        rows = [r for r in self.saved['entries'] if r['path'] == 'tree' or r['path'].startswith('tree/')]
        for limit in (None, 0, 1, 2, len(rows) - 1, len(rows), len(rows) + 1):
            with self.subTest(limit=limit):
                args = [] if limit is None else ['--limit', str(limit)]
                result = self.run_cli('--subtree', 'tree', *args)
                selected = result['snapshot_inspection'].pop('selected_subtree')
                included = rows[:100 if limit is None else limit]
                self.assertEqual(selected, {'path': 'tree', 'matching_entry_count': len(rows),
                    'included_payload_bytes': 11, 'included_payload_files': 5,
                    'entries': included, 'returned_entry_count': len(included),
                    'truncated': len(included) < len(rows)})
                self.assertEqual(result, plain)
        by_path = {r['path']: r for r in rows}
        self.assertEqual(by_path['tree/.ignore']['role'], 'ignore')
        self.assertEqual(by_path['tree/Dockerfile']['role'], 'dockerfile')
        self.assertFalse(by_path['tree/Dockerfile']['included_by_rules'])
        self.assertEqual(by_path['tree/alias']['kind'], 'symlink')
        self.assertFalse(by_path['tree/link']['included_by_rules'])
        self.assertFalse(by_path['tree/excluded']['included_by_rules'])
        self.assertTrue(by_path['tree/excluded/keep']['last_rule']['negated'])
        self.assertEqual(by_path['tree/excluded/drop']['last_rule']['matched_path'], 'tree/excluded')

    def test_nested_empty_excluded_zero_byte_and_literal_directories(self):
        for selector, count, size, files in (('tree/nested', 3, 5, 2), ('tree/empty', 1, 0, 0),
                                          ('tree/excluded', 4, 3, 2), ('全\n\x1b[31m', 2, 0, 1)):
            with self.subTest(selector=selector):
                selected = self.run_cli('--subtree', selector)['snapshot_inspection']['selected_subtree']
                self.assertEqual((selected['matching_entry_count'], selected['included_payload_bytes'],
                                  selected['included_payload_files']), (count, size, files))
                self.assertFalse(selected['truncated'])
                self.assertEqual(selected['entries'][0]['path'], selector)
        # Globs are never evaluated, but literal metacharacters remain valid recorded names.
        data = copy.deepcopy(self.saved)
        prototype = next(r for r in data['entries'] if r['path'] == 'tree/empty')
        for name in ('*', 'back\\slash', '-dash'):
            data['entries'].append({**prototype, 'path': name})
        data['entries'].sort(key=lambda r: r['path'])
        self.snapshot.write_text(json.dumps(data))
        for name in ('*', 'back\\slash', '-dash'):
            self.assertEqual(self.run_cli('--subtree=' + name)['snapshot_inspection']['selected_subtree']['entries'],
                             [{**prototype, 'path': name}])

    def test_default_limit_100_with_complete_counts(self):
        data = copy.deepcopy(self.saved)
        prototype = next(r for r in data['entries'] if r['path'] == 'tree/nested/zero')
        for number in range(105):
            data['entries'].append({**prototype, 'path': f'tree/item{number:03}', 'logical_bytes': 1})
        data['entries'].sort(key=lambda r: r['path'])
        self.snapshot.write_text(json.dumps(data))
        result = self.run_cli('--subtree', 'tree')['snapshot_inspection']['selected_subtree']
        rows = [r for r in data['entries'] if r['path'] == 'tree' or r['path'].startswith('tree/')]
        self.assertEqual(result['entries'], rows[:100])
        self.assertEqual(result['returned_entry_count'], 100)
        self.assertEqual(result['matching_entry_count'], len(rows))
        self.assertTrue(result['truncated'])
        self.assertEqual((result['included_payload_bytes'], result['included_payload_files']), (116, 110))

    def test_text_complete_provenance_and_control_safe_determinism(self):
        data = copy.deepcopy(self.saved)
        for row in data['entries']:
            if row['last_rule']:
                row['last_rule']['raw'] += '\n\x1b[31m'
                row['last_rule']['pattern'] += '\t\x1b[0m'
        self.snapshot.write_text(json.dumps(data))
        self.snapshot = self.snapshot.rename(self.work / 'saved\n\x1b[31m.json')
        for selector in ('tree', 'tree/empty', '全\n\x1b[31m'):
            text, error = self.run_cli('--subtree', selector, json_output=False)
            self.assertEqual(error, '')
            self.assertEqual(text, self.run_cli('--subtree', selector, json_output=False)[0])
            self.assertNotIn('\x1b', text)
            self.assertNotIn('\t', text)
            selected = self.run_cli('--subtree', selector)['snapshot_inspection']['selected_subtree']
            for row in selected['entries']:
                for k, v in row.items():
                    if k != 'last_rule':
                        self.assertIn(f'  {k}: {v!r}', text)
                if row['last_rule']:
                    for k, v in row['last_rule'].items():
                        self.assertIn(f'    {k}: {v!r}', text)
                else:
                    self.assertIn('last_rule: null (default include)', text)
        text, _ = self.run_cli('--subtree', 'tree', '--limit', '0', json_output=False)
        self.assertIn('Subtree entries: 0 returned / 14 matching; truncated: True', text)
        self.assertIn('Subtree payload: 11 bytes, 5 files', text)
        self.assertNotIn('  kind:', text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['inspect-snapshot', str(self.snapshot), '--subtree', 'tree', '--json']), 0)
        output.getvalue().encode('ascii')

    def test_invalid_unknown_nondirectory_repeated_and_conflicting_selection(self):
        for selector in ('outside', 'tree/.ignore', 'tree/Dockerfile', 'tree/link', 'tree/alias',
                         'missing', 'Tree', 'tree/nest', '*', '', '.', '..', '/', '/tree', './tree',
                         'tree/', 'tree//nested', 'tree/../tree', 'tree\x00', 'tree/alias/child'):
            with self.subTest(selector=selector):
                result = self.run_cli('--subtree', selector, expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
        for extra in (('--subtree', 'tree', '--subtree', 'tree'),
                      ('--subtree', 'tree', '--subtree', 'tree2'),
                      ('--subtree', 'tree', '--limit', '1', '--limit', '1'),
                      ('--subtree', 'tree', '--path', 'tree'),
                      ('--path', 'tree', '--subtree', 'tree'), ('--limit', '0'),
                      ('--path', 'tree', '--limit', '0'), ('--subtree', 'tree', '--json')):
            self.assertEqual(set(self.run_cli(*extra, expected=2)), {'schema_version', 'status', 'error'})
        text, error = self.run_cli('--subtree', 'missing\n\x1b[31m', expected=2, json_output=False)
        self.assertEqual(text, '')
        self.assertNotIn('\x1b', error)

    def test_complete_strict_loader_before_selection_even_at_limit_zero(self):
        bad = self.work / 'bad.json'
        late = copy.deepcopy(self.saved)
        late['entries'][-1]['logical_bytes'] = -1
        provenance = copy.deepcopy(self.saved)
        provenance['entries'][-1]['last_rule'] = {}
        orphan = copy.deepcopy(self.saved)
        orphan['entries'] = [r for r in orphan['entries'] if r['path'] != 'tree/nested']
        for data in (late, provenance, orphan, {**self.saved, 'schema_version': 2},
                     {**self.saved, 'entries': self.saved['entries'] + [self.saved['entries'][-1]]}):
            bad.write_text(json.dumps(data))
            for selector in ('tree', 'unknown', './tree'):
                error = self.run_cli('--subtree', selector, '--limit', '0', snapshot=bad, expected=2)
                self.assertEqual(set(error), {'schema_version', 'status', 'error'})
                self.assertIn('invalid snapshot', error['error'])
        for contents in (b'{', b'{"format":1,"format":1}', b'\xff'):
            bad.write_bytes(contents)
            self.assertEqual(self.run_cli('--subtree', 'tree', '--limit', '0', snapshot=bad, expected=2)['status'], 'error')
        with bad.open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        self.assertIn('exceeds', self.run_cli('--subtree', 'tree', '--limit', '0', snapshot=bad, expected=2)['error'])
        bad.unlink()
        bad.symlink_to(self.snapshot)
        self.assertEqual(self.run_cli('--subtree', 'tree', '--limit', '0', snapshot=bad, expected=2)['status'], 'error')

    def test_parser_errors_do_not_emit_partial_json(self):
        for extra in (['--subtree'], ['--subtree', 'tree', '--limit'],
                      *(['--subtree', 'tree', '--limit', value] for value in ('-1', '1.5', 'x', '')),
                      ['--subtree', 'tree', '--max-bytes', '0'], ['--subtree', 'tree', '--top', '0']):
            with self.subTest(extra=extra), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(['inspect-snapshot', str(self.snapshot), *extra, '--json'])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')
                self.assertIn('error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
