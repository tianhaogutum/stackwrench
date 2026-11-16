"""Exact suffix metadata listing with complete validation/counts before row limits."""

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


class SnapshotExtensionTests(unittest.TestCase):
    run_cli = inspection_tests.SnapshotInspectionTests.run_cli

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / 'context'
        self.context.mkdir()
        sizes = {'Dockerfile.bin': 13, 'root.bin': 3, 'zero.bin': 0, 'tree/nested/data.bin': 5,
                 'tree/excluded/drop.bin': 41, 'tree/excluded/keep.bin': 2,
                 'tree2/outside.bin': 7, 'tree/cache.bin/other.py': 11,
                 'upper.BIN': 17, 'archive.tar.gz': 19, '.hidden': 23, 'name.': 29,
                 'plain': 31, '.hidden.bin': 37, '全\n\x1b[31m\t.bin': 1,
                 'literal.*': 43, 'windows.\\suffix': 47}
        for name, size in sizes.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * size)
        (self.context / 'tree/empty.bin').mkdir()
        (self.context / 'link.bin').symlink_to('/never-open-this-target')
        (self.context / 'tree/alias.bin').symlink_to('nested', target_is_directory=True)
        self.rules = self.context / 'rules.bin'
        self.rules.write_text('tree/excluded\n!tree/excluded/keep.bin\nDockerfile.bin\nlink.bin\n')
        self.snapshot = self.work / 'saved.json'
        self.saved = from_report(analyze(self.context, dockerfile='Dockerfile.bin',
                                        ignore=self.rules, include_link_targets=False))
        write_snapshot(self.snapshot, self.saved)
        shutil.rmtree(self.context)

    def test_extension_regular_rows_every_role_and_decision_before_limit(self):
        expected_names = ['.hidden.bin', 'Dockerfile.bin', 'root.bin', 'rules.bin',
                          'tree/excluded/drop.bin', 'tree/excluded/keep.bin',
                          'tree/nested/data.bin', 'tree2/outside.bin', 'zero.bin', '全\n\x1b[31m\t.bin']
        by_path = {r['path']: r for r in self.saved['entries']}
        rows = [by_path[name] for name in expected_names]
        plain = self.run_cli()
        for limit in (None, 0, 1, 2, 9, 10, 11):
            with self.subTest(limit=limit):
                extra = [] if limit is None else ['--limit', str(limit)]
                result = self.run_cli('--extension', '.bin', *extra)
                selected = result['snapshot_inspection'].pop('selected_extension')
                returned = rows[:100 if limit is None else limit]
                self.assertEqual(selected, dict(extension='.bin', matching_entry_count=10,
                    included_payload_bytes=55, included_payload_files=7, entries=returned,
                    returned_entry_count=len(returned), truncated=len(returned) < 10))
                self.assertEqual(result, plain)
        self.assertFalse(by_path['Dockerfile.bin']['included_by_rules'])
        self.assertEqual(by_path['Dockerfile.bin']['role'], 'dockerfile')
        self.assertEqual(by_path['rules.bin']['role'], 'ignore')
        self.assertFalse(by_path['tree/excluded/drop.bin']['included_by_rules'])
        self.assertEqual(by_path['tree/excluded/drop.bin']['last_rule']['matched_path'], 'tree/excluded')
        self.assertTrue(by_path['tree/excluded/keep.bin']['last_rule']['negated'])

    def test_case_final_suffix_none_and_literal_metacharacters(self):
        for suffix, names, size in (('.BIN', ['upper.BIN'], 17), ('.gz', ['archive.tar.gz'], 19),
                                   ('<none>', ['.hidden', 'name.', 'plain'], 83),
                                   ('.*', ['literal.*'], 43), ('.\\suffix', ['windows.\\suffix'], 47),
                                   ('.missing', [], 0), ('.Bin', [], 0)):
            with self.subTest(suffix=suffix):
                result = self.run_cli('--extension', suffix)['snapshot_inspection']['selected_extension']
                self.assertEqual([r['path'] for r in result['entries']], names)
                self.assertEqual((result['matching_entry_count'], result['included_payload_bytes'],
                                  result['included_payload_files'], result['truncated']),
                                 (len(names), size, len(names), False))
        data = copy.deepcopy(self.saved)
        data['entries'] = []
        self.snapshot.write_text(json.dumps(data))
        self.assertEqual(self.run_cli('--extension', '.bin', '--limit', '0')['snapshot_inspection']['selected_extension'],
                         dict(extension='.bin', matching_entry_count=0, included_payload_bytes=0,
                              included_payload_files=0, entries=[], returned_entry_count=0, truncated=False))

    def test_default_100_all_counts_and_control_only_excluded_only_groups(self):
        data = copy.deepcopy(self.saved)
        prototype = next(r for r in data['entries'] if r['path'] == 'zero.bin')
        data['entries'] += [{**prototype, 'path': f'item{number:03}.bin', 'logical_bytes': 1}
                            for number in range(105)]
        data['entries'].sort(key=lambda r: r['path'])
        self.snapshot.write_text(json.dumps(data))
        selected = self.run_cli('--extension', '.bin')['snapshot_inspection']['selected_extension']
        self.assertEqual((selected['returned_entry_count'], selected['matching_entry_count'],
                          selected['included_payload_bytes'], selected['included_payload_files'], selected['truncated']),
                         (100, 115, 160, 112, True))
        expected = [r for r in data['entries'] if r['kind'] == 'file' and r['path'].endswith('.bin')]
        self.assertEqual(selected['entries'], expected[:100])
        for name, role in (('Dockerfile.bin', 'dockerfile'), ('rules.bin', 'ignore'),
                           ('tree/excluded/drop.bin', 'payload')):
            # Retain parents and the chosen file; metadata control identity stays unchanged.
            one = {**self.saved, 'entries': [r for r in self.saved['entries']
                    if r['kind'] == 'directory' or r['path'] == name]}
            self.snapshot.write_text(json.dumps(one))
            result = self.run_cli('--extension', '.bin')['snapshot_inspection']['selected_extension']
            self.assertEqual((result['matching_entry_count'], result['included_payload_bytes'],
                              result['included_payload_files']), (1, 0, 0))
            self.assertEqual(result['entries'][0]['role'], role)

    def test_safe_text_complete_metadata_provenance_and_json_ascii(self):
        data = copy.deepcopy(self.saved)
        for row in data['entries']:
            if row['last_rule']:
                row['last_rule']['raw'] += '\n\x1b[31m'
                row['last_rule']['pattern'] += '\t\x1b[0m'
        self.snapshot.write_text(json.dumps(data))
        text, error = self.run_cli('--extension', '.bin', json_output=False)
        self.assertEqual(error, '')
        self.assertEqual(text, self.run_cli('--extension', '.bin', json_output=False)[0])
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\t', text)
        self.assertIn("Selected recorded extension: '.bin'", text)
        self.assertIn('Extension entries: 10 returned / 10 matching; truncated: False', text)
        self.assertIn('Extension payload: 55 bytes, 7 files', text)
        rows = self.run_cli('--extension', '.bin')['snapshot_inspection']['selected_extension']['entries']
        for row in rows:
            for key, value in row.items():
                if key != 'last_rule':
                    self.assertIn(f'  {key}: {value!r}', text)
            if row['last_rule']:
                for key, value in row['last_rule'].items():
                    self.assertIn(f'    {key}: {value!r}', text)
            else:
                self.assertIn('last_rule: null (default include)', text)
        text, _ = self.run_cli('--extension', '.bin', '--limit', '0', json_output=False)
        self.assertIn('Extension entries: 0 returned / 10 matching; truncated: True', text)
        self.assertIn('Extension payload: 55 bytes, 7 files', text)
        self.assertNotIn('  kind:', text)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['inspect-snapshot', str(self.snapshot), '--extension', '.bin', '--json']), 0)
        output.getvalue().encode('ascii')

    def test_malformed_repeated_conflicting_selection_and_unsupported_limits(self):
        for suffix in ('', 'bin', '.', '..', '.tar.gz', '.bin.', './bin', '/.bin', '<NONE>', '*', '.b\x00in'):
            with self.subTest(suffix=suffix):
                self.assertEqual(set(self.run_cli('--extension', suffix, expected=2)),
                                 {'schema_version', 'status', 'error'})
        for extra in (('--extension', '.bin', '--extension', '.bin'),
                      ('--extension', '.bin', '--extension', '.BIN'),
                      ('--extension', '.bin', '--path', 'root.bin'),
                      ('--path', 'root.bin', '--extension', '.bin'),
                      ('--extension', '.bin', '--subtree', 'tree'),
                      ('--subtree', 'tree', '--extension', '.bin'),
                      ('--extension', '.bin', '--limit', '0', '--limit', '1'),
                      ('--extension', '.bin', '--json'), ('--limit', '0'),
                      ('--path', 'root.bin', '--limit', '0')):
            self.assertEqual(set(self.run_cli(*extra, expected=2)), {'schema_version', 'status', 'error'})
        text, error = self.run_cli('--extension', 'invalid\n\x1b[31m', expected=2, json_output=False)
        self.assertEqual(text, '')
        self.assertNotIn('\x1b', error)
        for kwargs in ({'extension': '.bin', 'path': 'root.bin'}, {'extension': '.bin', 'subtree': 'tree'},
                       {'extension': '.bin', 'limit': -1}, {'extension': '.bin', 'limit': True},
                       {'extension': '.tar.gz'}, {'limit': 0}):
            with self.assertRaises(InputError):
                inspect_snapshot(self.saved, **kwargs)

    def test_complete_strict_loader_even_limit_zero_absent_suffix(self):
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
            for suffix in ('.bin', '.missing', '.tar.gz'):
                result = self.run_cli('--extension', suffix, '--limit', '0', snapshot=bad, expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
                self.assertIn('invalid snapshot', result['error'])
        for contents in (b'{', b'{"format":1,"format":1}', b'\xff'):
            bad.write_bytes(contents)
            self.assertEqual(self.run_cli('--extension', '.bin', '--limit', '0', snapshot=bad, expected=2)['status'], 'error')
        with bad.open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        self.assertIn('exceeds', self.run_cli('--extension', '.bin', '--limit', '0', snapshot=bad, expected=2)['error'])
        bad.unlink()
        bad.symlink_to(self.snapshot)
        self.assertEqual(self.run_cli('--extension', '.bin', snapshot=bad, expected=2)['status'], 'error')

    def test_parser_errors_without_partial_stdout(self):
        for extra in (['--extension'], ['--extension', '.bin', '--limit'],
                      *(['--extension', '.bin', '--limit', value] for value in ('-1', '1.5', 'x', '', '9' * 5000)),
                      ['--extension', '.bin', '--max-bytes', '0'], ['--extension', '.bin', '--top', '0']):
            with self.subTest(extra=extra), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(['inspect-snapshot', str(self.snapshot), *extra, '--json'])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')
                self.assertIn('error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
