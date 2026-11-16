"""Stored inclusion decisions across all kinds/roles, never live rule evaluation."""

import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import patch

from . import test_snapshot_inspection as inspection_tests
from stackwrench.context_budget.cli import main
from stackwrench.context_budget.ignore import InputError
from stackwrench.context_budget.snapshot import MAX_SNAPSHOT_BYTES, inspect_snapshot


class SnapshotDecisionTests(unittest.TestCase):
    setUp = inspection_tests.SnapshotInspectionTests.setUp
    run_cli = inspection_tests.SnapshotInspectionTests.run_cli

    def test_decision_complete_rows_roles_provenance_counts_and_limits(self):
        plain = self.run_cli()
        expected_names = {
            'included': ['.dockerignore', 'blocked/keep', 'dir', 'dir/child', 'empty',
                         'keep', 'link', 'zero', '目录\n\x1b[31m\t'],
            'excluded': ['Dockerfile', 'blocked', 'blocked/drop', 'excluded', 'link-out'],
        }
        by_path = {row['path']: row for row in self.saved['entries']}
        for decision, names in expected_names.items():
            rows = [by_path[name] for name in names]
            size, files = (14, 5) if decision == 'included' else (0, 0)
            for limit in (None, 0, 1, len(rows) - 1, len(rows), len(rows) + 1):
                with self.subTest(decision=decision, limit=limit):
                    result = self.run_cli('--decision', decision,
                        *([] if limit is None else ['--limit', str(limit)]))
                    selected = result['snapshot_inspection'].pop('selected_decision')
                    returned = rows[:100 if limit is None else limit]
                    self.assertEqual(selected, dict(decision=decision, matching_entry_count=len(rows),
                        included_payload_bytes=size, included_payload_files=files, entries=returned,
                        returned_entry_count=len(returned), truncated=len(returned) < len(rows)))
                    self.assertEqual(result, plain)
        self.assertIsNone(by_path['keep']['last_rule'])
        self.assertEqual(by_path['blocked/drop']['last_rule']['matched_path'], 'blocked')
        self.assertTrue(by_path['blocked/keep']['last_rule']['negated'])
        self.assertEqual(by_path['excluded']['logical_bytes'], 17)
        self.assertEqual(by_path['blocked/drop']['logical_bytes'], 23)
        self.assertEqual(by_path['Dockerfile']['role'], 'dockerfile')
        self.assertEqual(by_path['.dockerignore']['role'], 'ignore')

    def test_default100_both_decisions_account_past_limit(self):
        data = copy.deepcopy(self.saved)
        by_path = {row['path']: row for row in data['entries']}
        for name, prototype in (('a-include', by_path['zero']), ('b-exclude', by_path['excluded'])):
            data['entries'] += [{**prototype, 'path': f'{name}{n:03}', 'logical_bytes': 2,
                                'last_rule': None if name == 'a-include' else
                                    {**prototype['last_rule'], 'matched_path': f'{name}{n:03}'}}
                               for n in range(105)]
        data['entries'].sort(key=lambda r: r['path'])
        self.snapshot.write_text(json.dumps(data))
        for decision, count, size, files in (('included', 114, 224, 110), ('excluded', 110, 0, 0)):
            selected = self.run_cli('--decision', decision)['snapshot_inspection']['selected_decision']
            rows = [r for r in data['entries'] if r['included_by_rules'] == (decision == 'included')]
            self.assertEqual(selected, dict(decision=decision, matching_entry_count=count,
                included_payload_bytes=size, included_payload_files=files, entries=rows[:100],
                returned_entry_count=100, truncated=True))
            # zero-byte and reincluded files occur after the returned rows.
            if decision == 'included':
                self.assertNotIn('zero', [r['path'] for r in selected['entries']])

    def test_empty_groups_and_empty_inventory(self):
        variants = [[], [r for r in self.saved['entries'] if r['path'] == 'zero'],
                    [r for r in self.saved['entries'] if r['path'] == 'excluded'],
                    [r for r in self.saved['entries'] if r['role'] != 'payload']]
        for entries in variants:
            self.snapshot.write_text(json.dumps({**self.saved, 'entries': entries}))
            for decision in ('included', 'excluded'):
                for limit in (None, 0):
                    selected = self.run_cli('--decision', decision,
                        *([] if limit is None else ['--limit', str(limit)]))['snapshot_inspection']['selected_decision']
                    rows = [r for r in entries if r['included_by_rules'] == (decision == 'included')]
                    returned = rows if limit is None else []
                    self.assertEqual(selected, dict(decision=decision, matching_entry_count=len(rows),
                        included_payload_bytes=0, included_payload_files=int(any(r['path'] == 'zero' for r in rows)),
                        entries=returned, returned_entry_count=len(returned), truncated=len(returned) < len(rows)))

    def test_safe_text_full_provenance_and_stored_decision_only(self):
        data = copy.deepcopy(self.saved)
        for row in data['entries']:
            if row['last_rule']:
                # Valid metadata, intentionally not executable ignore syntax.
                row['last_rule']['raw'] = '[malformed\n\x1b[31m'
                row['last_rule']['pattern'] = '[malformed\t\x1b[0m'
        self.snapshot.write_text(json.dumps(data))
        with contextlib.ExitStack() as stack:
            for target in ('stackwrench.context_budget.ignore.parse_rules', 'stackwrench.context_budget.ignore.compile_pattern',
                           'stackwrench.context_budget.ignore.decide', 'stackwrench.context_budget.core.decide',
                           'stackwrench.context_budget.snapshot.write_snapshot', 'hashlib.sha256'):
                stack.enter_context(patch(target, side_effect=AssertionError('forbidden: ' + target)))
            for decision, count, size, files in (('included', 9, 14, 5), ('excluded', 5, 0, 0)):
                text, error = self.run_cli('--decision', decision, json_output=False)
                self.assertEqual(error, '')
                self.assertNotIn('\x1b', text)
                self.assertNotIn('\t', text)
                self.assertIn(f"Selected recorded decision: {decision!r}", text)
                self.assertIn(f'Decision entries: {count} returned / {count} matching; truncated: False', text)
                self.assertIn(f'Decision payload: {size} bytes, {files} files', text)
                self.assertIn('Saved payload: 14 bytes, 5 files', text)
                for row in data['entries']:
                    if row['included_by_rules'] != (decision == 'included'):
                        continue
                    for key, value in row.items():
                        if key != 'last_rule':
                            self.assertIn(f'  {key}: {value!r}', text)
                    if row['last_rule']:
                        for key, value in row['last_rule'].items():
                            self.assertIn(f'    {key}: {value!r}', text)
                    else:
                        self.assertIn('last_rule: null (default include)', text)
                self.assertEqual(self.run_cli('--decision', decision, json_output=False)[0], text)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(['inspect-snapshot', str(self.snapshot), '--decision', decision, '--json']), 0)
                output.getvalue().encode('ascii')
                self.assertNotIn('\x1b', output.getvalue())
        text, _ = self.run_cli('--decision', 'included', '--limit', '0', json_output=False)
        self.assertNotIn('  kind:', text)

    def test_conflicts_repeats_and_library_contract(self):
        for extra in (['--decision', 'included'], ['--decision', 'excluded'], ['--path', 'keep'],
                      ['--subtree', 'dir'], ['--extension', '<none>'],
                      ['--subtree-extension', 'dir', '<none>'], ['--limit', '0', '--limit', '1'], ['--json']):
            for before in (True, False):
                args = ['--decision', 'included']
                result = self.run_cli(*(extra + args if before else args + extra), expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
        for value in ('', 'include', 'exclude', 'Included', 'EXCLUDED', ' included', 'included ', True, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(InputError):
                inspect_snapshot(self.saved, decision=value)
        for kwargs in ({'path': 'keep'}, {'subtree': 'dir'}, {'extension': '<none>'},
                       {'subtree_extension': ('dir', '<none>')}, {'limit': -1}, {'limit': True}, {'limit': '0'}):
            with self.assertRaises(InputError):
                inspect_snapshot(self.saved, decision='included', **kwargs)
        text, error = self.run_cli('--decision', 'excluded', '--decision', 'included', expected=2, json_output=False)
        self.assertEqual(text, '')
        self.assertIn('only once', error)

    def test_strict_complete_validation_even_outside_decision_at_limit0(self):
        bad = self.work / 'bad.json'
        late = copy.deepcopy(self.saved)
        late['entries'][-1]['logical_bytes'] = -1  # included late row, outside excluded selection.
        provenance = copy.deepcopy(self.saved)
        provenance['entries'][-1]['last_rule'] = {}
        orphan = copy.deepcopy(self.saved)
        orphan['entries'] = [r for r in orphan['entries'] if r['path'] != 'dir']
        for data in (late, provenance, orphan, {**self.saved, 'schema_version': 2},
                     {**self.saved, 'entries': self.saved['entries'] + [self.saved['entries'][-1]]}):
            bad.write_text(json.dumps(data))
            for decision in ('included', 'excluded'):
                result = self.run_cli('--decision', decision, '--limit', '0', snapshot=bad, expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
                self.assertIn('invalid snapshot', result['error'])
            with self.assertRaisesRegex(InputError, 'invalid snapshot'):
                inspect_snapshot(data, decision='excluded', limit=0)
        for contents in (b'{', b'{"format":1,"format":1}', b'\xff'):
            bad.write_bytes(contents)
            self.assertEqual(self.run_cli('--decision', 'excluded', '--limit', '0', snapshot=bad, expected=2)['status'], 'error')
        with bad.open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        self.assertIn('exceeds', self.run_cli('--decision', 'excluded', '--limit', '0', snapshot=bad, expected=2)['error'])
        bad.unlink()
        bad.symlink_to(self.snapshot)
        self.assertEqual(self.run_cli('--decision', 'included', '--limit', '0', snapshot=bad, expected=2)['status'], 'error')
        bad.unlink()
        os.mkfifo(bad)
        self.assertIn('regular', self.run_cli('--decision', 'included', '--limit', '0', snapshot=bad, expected=2)['error'])

    def test_parser_enum_missing_operand_malformed_limit_and_unrelated_flags(self):
        cases = [['--decision'], ['--decision', '--json'],
                 *(['--decision', v] for v in ('', 'include', 'exclude', 'Included', 'EXCLUDED', ' included', 'included ')),
                 ['--decision', 'included', '--limit'],
                 *(['--decision', 'included', '--limit', v] for v in ('-1', 'x', '1.5', '', '9' * 5000)),
                 ['--decision', 'included', '--max-bytes', '0'], ['--decision', 'included', '--top', '0']]
        for extra in cases:
            with self.subTest(extra=extra), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(['inspect-snapshot', str(self.snapshot), *extra, '--json'])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')
                self.assertIn('error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
