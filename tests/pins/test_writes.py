from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.pins.core import inspect
from stackwrench.pins.fs import InputError, Root


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stackwrench-pins-writes-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / "mise.toml").write_text('[tools]\nnode="22.14.0"\n')
        (self.path / "Dockerfile").write_text("FROM node:20.11.0-alpine\n")
        (self.path / ".nvmrc").write_text("20.11.0\n")
        self.root = Root(str(self.path))
        self.addCleanup(self.root.close)
        self.report = inspect(self.root, "mise.toml")
        self.before = self.contents()

    def contents(self):
        return {p.name: p.read_bytes() for p in self.path.iterdir() if p.is_file()}

    def test_changed_source_is_detected_before_any_write(self):
        (self.path / "mise.toml").write_text('[tools]\nnode="24.1.0"\n')
        changed = self.contents()
        with self.assertRaisesRegex(InputError, "changed since inspection"):
            self.root.apply(self.report.snapshots, self.report.updates)
        self.assertEqual(self.contents(), changed)

    def test_changed_target_is_detected_before_any_write(self):
        (self.path / ".nvmrc").write_text("user edit\n")
        changed = self.contents()
        with self.assertRaisesRegex(InputError, "changed since inspection"):
            self.root.apply(self.report.snapshots, self.report.updates)
        self.assertEqual(self.contents(), changed)

    def test_replaced_with_same_bytes_still_detected(self):
        path = self.path / ".nvmrc"
        data = path.read_bytes()
        path.rename(self.path / "old")
        path.write_bytes(data)
        with self.assertRaisesRegex(InputError, "changed since inspection"):
            self.root.apply(self.report.snapshots, self.report.updates)
        self.assertEqual((self.path / "Dockerfile").read_bytes(), self.before["Dockerfile"])

    def test_symlink_swap_after_inspection_is_refused(self):
        path = self.path / ".nvmrc"
        path.rename(self.path / "original")
        path.symlink_to(self.path / "original")
        with self.assertRaises(InputError):
            self.root.apply(self.report.snapshots, self.report.updates)
        self.assertEqual((self.path / "original").read_bytes(), self.before[".nvmrc"])
        self.assertEqual((self.path / "Dockerfile").read_bytes(), self.before["Dockerfile"])

    def test_second_replace_failure_rolls_back_first_edit(self):
        actual_replace = os.replace
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic second replacement failure")
            return actual_replace(*args, **kwargs)

        with patch("stackwrench.pins.fs.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(InputError, "apply aborted"):
                self.root.apply(self.report.snapshots, self.report.updates)
        self.assertEqual(calls, 3)
        self.assertEqual(self.contents(), self.before)

    def test_staging_failure_leaves_all_inputs_unchanged(self):
        actual_stage = Root._stage
        calls = 0

        def fail_third(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("synthetic staging failure")
            return actual_stage(*args, **kwargs)

        with patch.object(Root, "_stage", side_effect=fail_third):
            with self.assertRaisesRegex(InputError, "staging failure"):
                self.root.apply(self.report.snapshots, self.report.updates)
        self.assertEqual(self.contents(), self.before)

    def test_rollback_failure_is_reported_and_backup_retained(self):
        actual_replace = os.replace
        calls = 0

        def fail_after_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise OSError("synthetic persistent write failure")
            return actual_replace(*args, **kwargs)

        with patch("stackwrench.pins.fs.os.replace", side_effect=fail_after_first):
            with self.assertRaisesRegex(InputError, "ROLLBACK FAILED.*original bytes saved"):
                self.root.apply(self.report.snapshots, self.report.updates)
        backups = list(self.path.glob(".stackwrench-pins-*.tmp"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.before["Dockerfile"])
        self.assertEqual((self.path / ".nvmrc").read_bytes(), self.before[".nvmrc"])


if __name__ == "__main__":
    unittest.main()
