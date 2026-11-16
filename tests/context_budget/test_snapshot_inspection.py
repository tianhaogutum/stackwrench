"""One saved inventory, with no live context or partial selection results."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.context_budget.cli import main
from stackwrench.context_budget.core import analyze
from stackwrench.context_budget.snapshot import MAX_SNAPSHOT_BYTES, MODEL, from_report, write_snapshot


class SnapshotInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / 'context'
        self.context.mkdir()
        (self.context / 'Dockerfile').write_text('FROM scratch\n')
        (self.context / 'keep').write_bytes(b'1234')
        for name, size in {'zero': 0, 'dir/child': 3, 'blocked/keep': 5,
                           'blocked/drop': 23, 'excluded': 17, '目录\n\x1b[31m\t': 2}.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * size)
        (self.context / 'empty').mkdir()
        (self.context / 'link').symlink_to('keep')
        (self.context / 'link-out').symlink_to('/never-open-this-target')
        self.rules = self.context / '.dockerignore'
        self.rules.write_text('excluded\nblocked\n!blocked/keep\nlink-out\nDockerfile\n')
        self.snapshot = self.work / 'saved.json'
        self.saved = from_report(analyze(self.context, include_link_targets=False))
        write_snapshot(self.snapshot, self.saved)
        shutil.rmtree(self.context)

    def run_cli(self, *extra, expected=0, json_output=True, snapshot=None):
        selected = snapshot or self.snapshot
        original = self.snapshot.read_bytes()
        real_open = os.open
        real_io_open = io.open
        parent_stat = selected.parent.stat()

        def artifact_open(path, flags, *args, **kwargs):
            self.assertFalse(Path(path) == self.context or Path(path).is_relative_to(self.context))
            if not flags & os.O_DIRECTORY:
                self.assertEqual(str(path), selected.name)
                parent = os.fstat(kwargs['dir_fd'])
                self.assertEqual((parent.st_dev, parent.st_ino), (parent_stat.st_dev, parent_stat.st_ino))
            return real_open(path, flags, *args, **kwargs)

        def fd_only_open(file, *args, **kwargs):
            self.assertIsInstance(file, int)  # os.fdopen wraps the already-guarded artifact fd.
            return real_io_open(file, *args, **kwargs)

        output, error = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for target in ('stackwrench.context_budget.cli.analyze', 'stackwrench.context_budget.core.inventory',
                           'stackwrench.context_budget.core.read_rules_file', 'stackwrench.context_budget.dimensions.suggestions',
                           'os.listdir', 'os.scandir', 'os.readlink', 'builtins.open'):
                stack.enter_context(patch(target, side_effect=AssertionError('forbidden live read: ' + target)))
            stack.enter_context(patch('os.open', side_effect=artifact_open))
            stack.enter_context(patch('io.open', side_effect=fd_only_open))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(error))
            self.assertEqual(main(['inspect-snapshot', str(selected), *extra,
                                   *(['--json'] if json_output else [])]), expected)
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertFalse(self.context.exists())
        if json_output:
            self.assertEqual(error.getvalue(), '')
            return json.loads(output.getvalue())
        return output.getvalue(), error.getvalue()

    def test_saved_summary_after_source_removal(self):
        self.assertEqual(self.run_cli(), {
            'schema_version': 1, 'status': 'ok', 'model': MODEL,
            'snapshot_inspection': {
                'snapshot': str(self.snapshot), 'format': self.saved['format'], 'schema_version': 1,
                'entry_count': 14, 'summary': {
                    'context': str(self.context), 'dockerfile': 'Dockerfile',
                    'ignore_selection': 'root', 'rules_source': str(self.rules),
                    'included_payload_bytes': 14, 'included_payload_files': 5}}})

    def test_exact_selection_all_rows_complete_metadata_and_unchanged_counts(self):
        plain = self.run_cli()
        for row in self.saved['entries']:
            with self.subTest(path=row['path']):
                result = self.run_cli('--path', row['path'])
                self.assertEqual(result['snapshot_inspection'].pop('selected_entry'), row)
                self.assertEqual(result, plain)
        rows = {r['path']: r for r in self.saved['entries']}
        self.assertEqual(rows['blocked']['kind'], 'directory')
        self.assertFalse(rows['blocked/drop']['included_by_rules'])
        self.assertEqual(rows['blocked/drop']['last_rule']['matched_path'], 'blocked')
        self.assertTrue(rows['blocked/keep']['last_rule']['negated'])
        self.assertEqual(rows['Dockerfile']['role'], 'dockerfile')
        self.assertFalse(rows['Dockerfile']['included_by_rules'])
        self.assertEqual(rows['.dockerignore']['role'], 'ignore')
        self.assertEqual(rows['link']['kind'], 'symlink')
        self.assertFalse(rows['link-out']['included_by_rules'])

    def test_text_and_json_determinism_complete_provenance_control_safe(self):
        # Unauthenticated but valid saved strings are displayed, never interpreted.
        data = copy.deepcopy(self.saved)
        data['context'] += '\n\x1b[31m'
        data['rules_source'] = data['context'] + '/.dockerignore'
        for row in data['entries']:
            if row['last_rule']:
                row['last_rule']['source'] = data['rules_source']
                row['last_rule']['raw'] += '\n\x1b[31m'
                row['last_rule']['pattern'] += '\t\x1b[0m'
        self.snapshot.write_text(json.dumps(data))
        self.snapshot = self.snapshot.rename(self.work / 'saved\n\x1b[31m.json')
        for selector in (None, '目录\n\x1b[31m\t', 'blocked/drop', 'blocked/keep'):
            args = [] if selector is None else ['--path', selector]
            text, error = self.run_cli(*args, json_output=False)
            self.assertEqual(error, '')
            self.assertEqual(text, self.run_cli(*args, json_output=False)[0])
            self.assertNotIn('\x1b', text)
            self.assertNotIn('\t', text)
            self.assertIn('Saved payload: 14 bytes, 5 files', text)
            self.assertIn('Saved metadata only; no live context scan, content or freshness verification.', text)
            if selector:
                row = next(r for r in data['entries'] if r['path'] == selector)
                for k, v in row.items():
                    if k != 'last_rule':
                        self.assertIn(f'  {k}: {v!r}', text)
                if row['last_rule']:
                    for k, v in row['last_rule'].items():
                        self.assertIn(f'    {k}: {v!r}', text)
                else:
                    self.assertIn('last_rule: null (default include)', text)
            self.assertEqual(self.run_cli(*args), self.run_cli(*args))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['inspect-snapshot', str(self.snapshot), '--path', '目录\n\x1b[31m\t', '--json']), 0)
        output.getvalue().encode('ascii')
        self.assertNotIn('\x1b', output.getvalue())

    def test_empty_inventory_and_zero_byte_payload(self):
        for entries, files in (([], 0), ([r for r in self.saved['entries'] if r['path'] == 'zero'], 1),
                               ([r for r in self.saved['entries'] if r['role'] != 'payload'], 0)):
            self.snapshot.write_text(json.dumps({**self.saved, 'entries': entries}))
            result = self.run_cli()['snapshot_inspection']
            self.assertEqual(result['entry_count'], len(entries))
            self.assertEqual(result['summary']['included_payload_bytes'], 0)
            self.assertEqual(result['summary']['included_payload_files'], files)

    def test_unknown_malformed_and_duplicate_selection_errors_only(self):
        for selector in ('missing', 'Keep', 'dir/chi', '*', '', '.', '..', '/keep', 'dir/',
                         './keep', 'dir//child', 'dir/../keep', 'keep\x00', 'link/child', 'dir\\child'):
            with self.subTest(selector=selector):
                result = self.run_cli('--path', selector, expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
                self.assertEqual(result['status'], 'error')
        for extra in (('--path', 'keep', '--path', 'keep'), ('--path', 'keep', '--path', 'zero')):
            self.assertIn('only once', self.run_cli(*extra, expected=2)['error'])
        text, error = self.run_cli('--path', 'missing\n\x1b[31m', expected=2, json_output=False)
        self.assertEqual(text, '')
        self.assertNotIn('\x1b', error)
        self.assertIn('no recorded entry', error)

    def test_complete_validation_precedes_known_or_unknown_selection(self):
        bad = self.work / 'bad.json'
        late = copy.deepcopy(self.saved)
        late['entries'][-1]['logical_bytes'] = -1
        duplicate = copy.deepcopy(self.saved)
        duplicate['entries'].append(duplicate['entries'][-1])
        bad_provenance = copy.deepcopy(self.saved)
        bad_provenance['entries'][-1]['last_rule'] = {}
        orphan = copy.deepcopy(self.saved)
        orphan['entries'] = [r for r in orphan['entries'] if r['path'] != 'dir']
        cases = [late, duplicate, bad_provenance, orphan, {},
                 {**self.saved, 'model': 'transfer bytes'}, {**self.saved, 'schema_version': 2},
                 {**self.saved, 'included_payload_bytes': 14}]
        for data in cases:
            bad.write_text(json.dumps(data))
            for args in ([], ['--path', 'keep'], ['--path', 'unknown']):
                result = self.run_cli(*args, snapshot=bad, expected=2)
                self.assertEqual(set(result), {'schema_version', 'status', 'error'})
                self.assertIn('invalid snapshot', result['error'])
        for contents in ('{', '{"format":1,"format":1}', '\ufeff{}', '\udcff'):
            bad.write_bytes(contents.encode('utf-8', errors='surrogatepass'))
            self.assertEqual(self.run_cli('--path', 'keep', snapshot=bad, expected=2)['status'], 'error')

    def test_strict_snapshot_file_boundaries(self):
        bad = self.work / 'bad.json'
        with bad.open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        self.assertIn('exceeds', self.run_cli(snapshot=bad, expected=2)['error'])
        bad.unlink()
        bad.symlink_to(self.snapshot)
        self.assertEqual(self.run_cli(snapshot=bad, expected=2)['status'], 'error')
        bad.unlink()
        os.mkfifo(bad)
        self.assertIn('regular', self.run_cli(snapshot=bad, expected=2)['error'])
        bad.unlink()
        self.assertEqual(self.run_cli(snapshot=bad, expected=2)['status'], 'error')
        bad.write_bytes(self.snapshot.read_bytes())
        bad.chmod(0)
        self.addCleanup(bad.chmod, 0o600)
        self.assertRegex(self.run_cli(snapshot=bad, expected=2)['error'], 'unreadable|Permission denied')
        alias = self.work / 'alias'
        alias.symlink_to(self.work, target_is_directory=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['inspect-snapshot', str(alias / self.snapshot.name), '--json']), 2)
        self.assertEqual(set(json.loads(output.getvalue())), {'schema_version', 'status', 'error'})

    def test_parser_missing_operands_and_unrelated_flags_no_stdout(self):
        for args in ([], [str(self.snapshot), '--path'], [str(self.snapshot), 'extra'],
                     *([str(self.snapshot), flag, '1'] for flag in ('--max-bytes', '--top', '--top-changes',
                         '--largest', '--ignore', '--dockerfile', '--baseline-snapshot', '--save-snapshot'))):
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(['inspect-snapshot', *args, '--json'])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), '')
                self.assertIn('error:', error.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main(['inspect-snapshot', '--help'])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn('--path EXACT_RELATIVE_PATH', output.getvalue())


if __name__ == '__main__':
    unittest.main()
