"""Saved-only baseline/current group deltas; no live reads or artifact writes."""

import contextlib
import copy
import io
import itertools
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from stackwrench.context_budget.cli import main
from stackwrench.context_budget.core import analyze
from stackwrench.context_budget.snapshot import from_report, write_snapshot
from .contribution_changes_fixture import prepare, advance, expected, budget_flags
from . import test_saved_contributions as prior_tests


class SavedContributionChangeTests(unittest.TestCase):
    # Reuse the existing artifact-only open guard and forbidden-live-read assertions.
    run_cli = prior_tests.SavedContributionTests.run_cli

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context, self.rules = prepare(self.work)
        self.baseline, self.current = self.work / 'before.json', self.work / 'after.json'
        self.before = self.capture()
        write_snapshot(self.baseline, self.before)
        advance(self.context, self.rules)
        self.after = self.capture()
        write_snapshot(self.current, self.after)
        shutil.rmtree(self.context)

    def capture(self):
        return from_report(analyze(self.context, dockerfile='a/control.ctl', ignore=self.rules,
                                   include_link_targets=False))

    def test_union_deltas_growth_shrinkage_and_count_only_without_live_inputs(self):
        plain = self.run_cli()
        result = self.run_cli('--top-changes', 99)
        self.assertEqual(result, {**plain, 'saved_contribution_changes': expected(99)})
        comparison = plain['snapshot_comparison']
        self.assertEqual(comparison['baseline']['included_payload_bytes'], 39)
        self.assertEqual(comparison['current']['included_payload_bytes'], 44)
        self.assertEqual(comparison['included_payload_files_delta'], 1)
        # Provenance-only prov/.same, unchanged ab, empty groups, controls, excluded
        # rows and symlinks never appear; root-only archive affects .gz but no root dir.
        self.assertTrue(any('rule_changed' in r['reasons'] for r in comparison['changes']))

    def test_independent_limits_zero_self_reverse_and_empty_inventories(self):
        for n in (0, 1, 2, 6, 7, 8, 99):
            self.assertEqual(self.run_cli('--top-changes', n)['saved_contribution_changes'], expected(n))
        self.assertEqual(self.run_cli('--top-changes', 99, baseline=self.current)['saved_contribution_changes'],
                         {'directories': [], 'extensions': []})
        reverse = self.run_cli('--top-changes', 99, baseline=self.current, current=self.baseline)
        expected_reverse = copy.deepcopy(expected(99))
        for rows in expected_reverse.values():
            for row in rows:
                for unit in ('bytes', 'files'):
                    b, c = 'baseline_included_payload_' + unit, 'current_included_payload_' + unit
                    row[b], row[c] = row[c], row[b]
                    row['included_payload_' + unit + '_delta'] *= -1
        self.assertEqual(reverse['saved_contribution_changes'], expected_reverse)
        self.current.write_text(json.dumps({**self.after, 'entries': []}))
        all_removed = self.run_cli('--top-changes', 99)['saved_contribution_changes']
        self.assertTrue(all(r['current_included_payload_files'] == 0 for rows in all_removed.values() for r in rows))
        self.assertNotIn('empty-before', [r['path'] for r in all_removed['directories']])
        self.baseline.write_text(json.dumps({**self.before, 'entries': []}))
        self.assertEqual(self.run_cli('--top-changes', 99)['saved_contribution_changes'],
                         {'directories': [], 'extensions': []})
        self.current.write_text(json.dumps(self.after))
        all_added = self.run_cli('--top-changes', 99)['saved_contribution_changes']
        self.assertTrue(all(r['baseline_included_payload_files'] == 0 for rows in all_added.values() for r in rows))

    def test_current_top_complete_budgets_status_exits_unchanged(self):
        for states in itertools.product((True, False), repeat=4):
            code = 0 if all(states) else 1
            for top in (None, 0, 1):
                args = [*budget_flags(states), *(['--top', top] if top is not None else [])]
                plain = self.run_cli(*args, expected=code)
                for n in (0, 1):
                    result = self.run_cli(*args, '--top-changes', n, expected=code)
                    self.assertEqual(result.pop('saved_contribution_changes'), expected(n))
                    self.assertEqual(result, plain)
                    self.assertEqual([len(result[k]) for k in ('current_saved_directory_budgets',
                        'current_saved_extension_budgets', 'current_saved_directory_extension_budgets')], [2, 2, 2])

    def test_invalid_negative_missing_overlong_n_no_stdout(self):
        originals = [p.read_bytes() for p in (self.baseline, self.current)]
        for extra in (('--top-changes',), *(('--top-changes', v) for v in ('-1', '1.5', 'no', '1KB', '', '9'*5000))):
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(['compare-snapshots', str(self.baseline), str(self.current), '--json', *extra])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')
                self.assertIn('error:', error.getvalue())
        self.assertEqual([p.read_bytes() for p in (self.baseline, self.current)], originals)

    def test_both_artifacts_validate_before_any_partial_output_even_n_zero(self):
        bad = self.work / 'bad.json'
        orphan = copy.deepcopy(self.after)
        orphan['entries'] = [r for r in orphan['entries'] if r['path'] != 'a']
        for data in ({}, {**self.after, 'schema_version': 2}, orphan,
                     {**self.after, 'entries': self.after['entries'] * 2},
                     {**self.after, 'context': str(self.work / 'other')},
                     {**self.after, 'dockerfile': 'Otherfile', 'entries': []}):
            bad.write_text(json.dumps(data))
            self.check_bad(bad)
        for content in ('{', '{"format":1,"format":1}', '\ufeff{}'):
            bad.write_text(content)
            self.check_bad(bad)
        bad.unlink()
        self.check_bad(bad)
        bad.symlink_to(self.current)
        self.check_bad(bad)

    def check_bad(self, bad):
        for before, after in ((bad, self.current), (self.baseline, bad)):
            for n in (0, 2):
                result = self.run_cli('--top-changes', n, '--top', 1, *budget_flags((True,)*4),
                                      baseline=before, current=after, expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
                self.assertEqual(result['status'], 'error')
            text, error = self.run_cli('--top-changes', 1, baseline=before, current=after,
                                       expected=2, json_output=False)
            self.assertEqual(text, '')
            self.assertIn('stackwrench context-budget:', error)

    def test_text_signs_labels_escaping_and_omitted_flag(self):
        plain, _ = self.run_cli(json_output=False)
        text, error = self.run_cli('--top-changes', 99, json_output=False)
        self.assertEqual(error, '')
        self.assertTrue(text.startswith(plain))
        self.assertIn("  directories: 'gone': baseline 8 bytes / 1 files; current 0 bytes / 0 files; delta -8 bytes / -1 files", text)
        self.assertIn("  extensions: '.bin': baseline 9 bytes / 3 files; current 21 bytes / 3 files; delta +12 bytes / +0 files", text)
        zero, _ = self.run_cli('--top-changes', 0, json_output=False)
        self.assertIn('Saved contribution changes: CURRENT minus BASELINE;', zero)
        self.assertNotIn('  directories:', zero)
        self.assertNotIn('  extensions:', zero)
        self.assertNotIn('saved_contribution_changes', self.run_cli())
        data = copy.deepcopy(self.after)
        directory, suffix = '目录=a\n\x1b[31m', '.x\n\x1b[31m'
        data['entries'].extend([{'path': p, 'kind': k, 'logical_bytes': n, 'role': 'payload',
             'included_by_rules': True, 'last_rule': None}
             for p, k, n in ((directory, 'directory', 0), (directory + '/a' + suffix, 'file', 100))])
        data['entries'].sort(key=lambda r: r['path'])
        self.current.write_text(json.dumps(data))
        text, _ = self.run_cli('--top-changes', 1, json_output=False)
        self.assertNotIn('\x1b', text)
        self.assertIn(repr(directory) + ': baseline 0 bytes / 0 files; current 100 bytes / 1 files;', text)
        self.assertIn(repr(suffix) + ': baseline 0 bytes / 0 files; current 100 bytes / 1 files;', text)


if __name__ == '__main__':
    unittest.main()
