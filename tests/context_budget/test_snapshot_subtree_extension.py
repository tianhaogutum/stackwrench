"""Exact saved directory/suffix pairs, validated and counted before limiting."""

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
from stackwrench.context_budget.ignore import InputError
from stackwrench.context_budget.snapshot import MAX_SNAPSHOT_BYTES, from_report, inspect_snapshot, write_snapshot


class SnapshotSubtreeExtensionTests(unittest.TestCase):
    run_cli = inspection_tests.SnapshotInspectionTests.run_cli

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / 'context'
        self.context.mkdir()
        sizes = {'tree/Dockerfile.bin': 13, 'root.bin': 37, 'tree/a.bin': 3, 'tree/zero.bin': 0,
                 'tree/nested/data.bin': 5, 'tree/excluded/drop.bin': 41, 'tree/excluded/keep.bin': 2,
                 'tree2/outside.bin': 7, 'tree/cache.bin/other.py': 11, 'tree/upper.BIN': 17,
                 'tree/archive.tar.gz': 19, 'tree/.hidden': 23, 'tree/name.': 29, 'tree/plain': 31,
                 'tree/全\n\x1b[31m\t.bin': 1, 'tree/literal.*': 43, 'tree/windows.\\suffix': 47}
        for name, size in sizes.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * size)
        (self.context / 'tree/empty.bin').mkdir()
        (self.context / 'tree/link.bin').symlink_to('/never-read')
        (self.context / 'tree/alias.bin').symlink_to('nested', target_is_directory=True)
        self.rules = self.context / 'tree/rules.bin'
        self.rules.write_text('tree/excluded\n!tree/excluded/keep.bin\ntree/Dockerfile.bin\ntree/link.bin\n')
        self.snapshot = self.work / 'saved.json'
        self.saved = from_report(analyze(self.context, dockerfile='tree/Dockerfile.bin',
                                        ignore=self.rules, include_link_targets=False))
        write_snapshot(self.snapshot, self.saved)
        shutil.rmtree(self.context)

    def test_pair_all_roles_decisions_boundaries_and_limits(self):
        names = ['tree/Dockerfile.bin', 'tree/a.bin', 'tree/excluded/drop.bin',
                 'tree/excluded/keep.bin', 'tree/nested/data.bin', 'tree/rules.bin',
                 'tree/zero.bin', 'tree/全\n\x1b[31m\t.bin']
        rows = [r for r in self.saved['entries'] if r['path'] in names]
        plain = self.run_cli()
        for limit in (None, 0, 1, 7, 8, 9):
            with self.subTest(limit=limit):
                result = self.run_cli('--subtree-extension', 'tree', '.bin',
                                      *([] if limit is None else ['--limit', str(limit)]))
                selected = result['snapshot_inspection'].pop('selected_subtree_extension')
                returned = rows[:100 if limit is None else limit]
                self.assertEqual(selected, dict(path='tree', extension='.bin', matching_entry_count=8,
                    included_payload_bytes=11, included_payload_files=5, entries=returned,
                    returned_entry_count=len(returned), truncated=len(returned) < 8))
                self.assertEqual(result, plain)
        self.assertEqual([r['path'] for r in rows], names)
        self.assertEqual(rows[0]['role'], 'dockerfile')
        self.assertFalse(rows[0]['included_by_rules'])
        self.assertEqual(rows[2]['last_rule']['matched_path'], 'tree/excluded')
        self.assertTrue(rows[3]['last_rule']['negated'])
        self.assertEqual(rows[5]['role'], 'ignore')

    def test_nested_empty_excluded_case_final_suffix_none_and_literals(self):
        cases = [('tree/nested', '.bin', ['tree/nested/data.bin'], 5, 1),
                 ('tree/excluded', '.bin', ['tree/excluded/drop.bin', 'tree/excluded/keep.bin'], 2, 1),
                 ('tree/empty.bin', '.bin', [], 0, 0), ('tree', '.missing', [], 0, 0),
                 ('tree', '.Bin', [], 0, 0), ('tree', '.BIN', ['tree/upper.BIN'], 17, 1),
                 ('tree', '.gz', ['tree/archive.tar.gz'], 19, 1),
                 ('tree', '<none>', ['tree/.hidden', 'tree/name.', 'tree/plain'], 83, 3),
                 ('tree', '.*', ['tree/literal.*'], 43, 1),
                 ('tree', '.\\suffix', ['tree/windows.\\suffix'], 47, 1)]
        for directory, suffix, names, size, files in cases:
            for limit in (None, 0):
                with self.subTest(directory=directory, suffix=suffix, limit=limit):
                    selected = self.run_cli('--subtree-extension', directory, suffix,
                        *([] if limit is None else ['--limit', str(limit)]))['snapshot_inspection']['selected_subtree_extension']
                    self.assertEqual([r['path'] for r in selected['entries']], names if limit is None else [])
                    self.assertEqual((selected['matching_entry_count'], selected['included_payload_bytes'],
                        selected['included_payload_files'], selected['truncated']),
                        (len(names), size, files, bool(names) and limit == 0))

    def test_default100_and_zero_byte_count_after_truncation(self):
        data = copy.deepcopy(self.saved)
        prototype = next(r for r in data['entries'] if r['path'] == 'tree/zero.bin')
        data['entries'] += [{**prototype, 'path': f'tree/item{n:03}.bin', 'logical_bytes': 1}
                            for n in range(105)]
        data['entries'].sort(key=lambda r: r['path'])
        self.snapshot.write_text(json.dumps(data))
        selected = self.run_cli('--subtree-extension', 'tree', '.bin')['snapshot_inspection']['selected_subtree_extension']
        self.assertEqual((selected['returned_entry_count'], selected['matching_entry_count'],
            selected['included_payload_bytes'], selected['included_payload_files'], selected['truncated']),
            (100, 113, 116, 110, True))
        rows = [r for r in data['entries'] if r['kind'] == 'file' and r['path'].startswith('tree/') and r['path'].endswith('.bin')]
        self.assertEqual(selected['entries'], rows[:100])

    def test_safe_text_complete_provenance_and_json(self):
        data = copy.deepcopy(self.saved)
        for row in data['entries']:
            if row['last_rule']:
                row['last_rule']['raw'] += '\n\x1b[31m'
                row['last_rule']['pattern'] += '\t\x1b[0m'
        self.snapshot.write_text(json.dumps(data))
        text, error = self.run_cli('--subtree-extension', 'tree', '.bin', json_output=False)
        self.assertEqual(error, '')
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\t', text)
        self.assertIn("Selected recorded subtree extension: 'tree' extension '.bin'", text)
        self.assertIn('Subtree extension entries: 8 returned / 8 matching; truncated: False', text)
        self.assertIn('Subtree extension payload: 11 bytes, 5 files', text)
        rows = self.run_cli('--subtree-extension', 'tree', '.bin')['snapshot_inspection']['selected_subtree_extension']['entries']
        for row in rows:
            for key, value in row.items():
                if key != 'last_rule':
                    self.assertIn(f'  {key}: {value!r}', text)
            if row['last_rule']:
                for key, value in row['last_rule'].items():
                    self.assertIn(f'    {key}: {value!r}', text)
            else:
                self.assertIn('last_rule: null (default include)', text)
        text, _ = self.run_cli('--subtree-extension', 'tree', '.bin', '--limit', '0', json_output=False)
        self.assertIn('Subtree extension entries: 0 returned / 8 matching; truncated: True', text)
        self.assertNotIn('  kind:', text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['inspect-snapshot', str(self.snapshot), '--subtree-extension', 'tree', '.bin', '--json']), 0)
        output.getvalue().encode('ascii')

    def test_invalid_selectors_conflicts_repeats_and_api_shapes(self):
        for directory in ('', '.', '..', '/tree', './tree', 'tree/', 'tree//nested', 'tree/../tree',
                          'unknown', 'tree/a.bin', 'tree/Dockerfile.bin', 'tree/rules.bin', 'tree/link.bin', 'tree/alias.bin', 'tree\x00'):
            self.assertEqual(set(self.run_cli('--subtree-extension', directory, '.bin', expected=2)),
                             {'schema_version', 'status', 'error'})
        for suffix in ('', 'bin', '.', '..', '.tar.gz', '.bin.', './bin', '/.bin', '<NONE>', '*', '.b\x00in'):
            self.assertEqual(set(self.run_cli('--subtree-extension', 'tree', suffix, expected=2)),
                             {'schema_version', 'status', 'error'})
        for extra in (['--subtree-extension', 'tree', '.bin'], ['--path', 'tree/a.bin'],
                      ['--subtree', 'tree'], ['--extension', '.bin'], ['--limit', '0', '--limit', '1'], ['--json']):
            for before in (True, False):
                pair = ['--subtree-extension', 'tree', '.bin']
                self.assertEqual(set(self.run_cli(*(extra + pair if before else pair + extra), expected=2)),
                                 {'schema_version', 'status', 'error'})
        text, error = self.run_cli('--subtree-extension', 'bad\n\x1b[31m', '.bin', expected=2, json_output=False)
        self.assertEqual(text, '')
        self.assertNotIn('\x1b', error)
        for pair in ('tree', [], ['tree', '.bin'], (), ('tree',), ('tree', '.bin', 'extra'), (1, '.bin'), ('tree', 1)):
            with self.assertRaises(InputError):
                inspect_snapshot(self.saved, subtree_extension=pair)
        for kwargs in ({'path': 'tree/a.bin'}, {'subtree': 'tree'}, {'extension': '.bin'}, {'limit': -1}, {'limit': True}):
            with self.assertRaises(InputError):
                inspect_snapshot(self.saved, subtree_extension=('tree', '.bin'), **kwargs)

    def test_complete_strict_bounded_validation_before_pair_even_limit0(self):
        bad = self.work / 'bad.json'
        late = copy.deepcopy(self.saved)
        late['entries'][-1]['logical_bytes'] = -1  # tree2/outside.bin, outside selection.
        provenance = copy.deepcopy(self.saved)
        provenance['entries'][-1]['last_rule'] = {}
        orphan = copy.deepcopy(self.saved)
        orphan['entries'] = [r for r in orphan['entries'] if r['path'] != 'tree/nested']
        for data in (late, provenance, orphan, {**self.saved, 'schema_version': 2},
                     {**self.saved, 'entries': self.saved['entries'] + [self.saved['entries'][-1]]}):
            bad.write_text(json.dumps(data))
            for directory, suffix in (('tree', '.bin'), ('tree/empty.bin', '.missing'), ('unknown', '.tar.gz')):
                result = self.run_cli('--subtree-extension', directory, suffix, '--limit', '0', snapshot=bad, expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
                self.assertIn('invalid snapshot', result['error'])
        for contents in (b'{', b'{"format":1,"format":1}', b'\xff'):
            bad.write_bytes(contents)
            self.assertEqual(self.run_cli('--subtree-extension', 'tree', '.bin', '--limit', '0', snapshot=bad, expected=2)['status'], 'error')
        with bad.open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        self.assertIn('exceeds', self.run_cli('--subtree-extension', 'tree', '.bin', '--limit', '0', snapshot=bad, expected=2)['error'])
        bad.unlink()
        bad.symlink_to(self.snapshot)
        self.assertEqual(self.run_cli('--subtree-extension', 'tree', '.bin', snapshot=bad, expected=2)['status'], 'error')

    def test_missing_operands_bad_limit_and_unsupported_flags_no_stdout(self):
        for extra in (['--subtree-extension'], ['--subtree-extension', 'tree'],
                      ['--subtree-extension', 'tree', '--json'],
                      ['--subtree-extension', 'tree', '.bin', '--limit'],
                      *(['--subtree-extension', 'tree', '.bin', '--limit', value] for value in ('-1', 'x', '1.5', '', '9' * 5000)),
                      ['--subtree-extension', 'tree', '.bin', '--max-bytes', '0'],
                      ['--subtree-extension', 'tree', '.bin', '--top', '0']):
            with self.subTest(extra=extra), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(['inspect-snapshot', str(self.snapshot), *extra, '--json'])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')
                self.assertIn('error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
