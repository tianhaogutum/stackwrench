"""Snapshot boundaries and metadata-only behavior on local synthetic trees."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.context_budget.cli import main
from stackwrench.context_budget.core import analyze
from stackwrench.context_budget.ignore import InputError
from stackwrench.context_budget.snapshot import (
    MAX_SNAPSHOT_BYTES, compare_snapshots, from_report, read_snapshot, write_snapshot,
)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        (self.context / "a").write_bytes(b"1234")
        self.path = self.work / "saved.json"

    def capture(self):
        return from_report(analyze(self.context, include_link_targets=False))

    def test_snapshot_modes_never_read_payload_or_symlink_text(self):
        (self.context / "link").symlink_to("outside-secret")
        (self.context / ".dockerignore").write_text("# empty rules\n")
        original_fdopen = os.fdopen
        original_readlink = os.readlink
        reads = []

        def checked_fdopen(fd, mode, *args, **kwargs):
            if "r" in mode:
                opened = original_readlink(f"/proc/self/fd/{fd}")
                self.assertIn(opened, (str(self.context / ".dockerignore"), str(self.path)))
                reads.append(opened)
            return original_fdopen(fd, mode, *args, **kwargs)

        with patch("os.fdopen", side_effect=checked_fdopen), patch(
            "os.readlink", side_effect=AssertionError("symlink text must not be read")
        ):
            for flag in ("--save-snapshot", "--baseline-snapshot"):
                with self.subTest(flag=flag), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([str(self.context), flag, str(self.path), "--json"]), 0)
                    report = json.loads(output.getvalue())
                    self.assertNotIn("link_target", report["baseline"]["entries"][-1])
        self.assertEqual(reads.count(str(self.context / ".dockerignore")), 2)
        self.assertEqual(reads.count(str(self.path)), 1)
        self.assertNotIn("outside-secret", self.path.read_text())

    def test_same_size_content_and_link_target_changes_are_out_of_model(self):
        (self.context / "link").symlink_to("first")
        baseline = self.capture()
        (self.context / "a").write_bytes(b"abcd")
        (self.context / "link").unlink()
        (self.context / "link").symlink_to("a-much-longer-second-target")
        self.assertEqual(compare_snapshots(baseline, self.capture())["changes"], [])

    def test_snapshot_text_escapes_context_and_rule_source_paths(self):
        self.context = self.context.rename(self.work / "line\n\x1b[31m")
        (self.context / ".dockerignore").write_text("a\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(self.context), "--save-snapshot", str(self.path)]), 0)
        (self.context / ".dockerignore").write_text("# include a\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([str(self.context), "--baseline-snapshot", str(self.path)]), 0)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("line\\n\\x1b[31m", output.getvalue())

    def test_zero_byte_addition_and_file_replaced_by_directory(self):
        baseline = self.capture()
        (self.context / "empty").touch()
        (self.context / "a").unlink()
        (self.context / "a").mkdir()
        (self.context / "a/child").write_bytes(b"123")
        result = compare_snapshots(baseline, self.capture())
        self.assertEqual(result["included_payload_bytes_delta"], -1)
        self.assertEqual(result["included_payload_files_delta"], 1)
        changes = {row["path"]: row for row in result["changes"]}
        self.assertEqual(changes["a"]["reasons"], ["kind_changed", "size_changed"])
        self.assertEqual(changes["empty"]["included_payload_bytes_delta"], 0)
        self.assertEqual(changes["empty"]["included_payload_files_delta"], 1)
        self.assertEqual(sum(row["included_payload_bytes_delta"] for row in result["changes"]), -1)

    def test_control_partition_is_reevaluated_and_explained(self):
        (self.context / ".dockerignore").write_text("a\n")
        (self.context / "Dockerfile.dockerignore").write_text("# specific\n")
        baseline = self.capture()
        (self.context / "Dockerfile.dockerignore").unlink()
        result = compare_snapshots(baseline, self.capture())
        self.assertEqual(result["baseline"]["ignore_selection"], "dockerfile-specific")
        self.assertEqual(result["current"]["ignore_selection"], "root")
        changes = {row["path"]: row for row in result["changes"]}
        self.assertIn("role_changed", changes[".dockerignore"]["reasons"])
        self.assertEqual(changes["a"]["after"]["last_rule"]["source"], str(self.context / ".dockerignore"))
        self.assertEqual(changes["a"]["included_payload_bytes_delta"], -4)

    def test_malformed_snapshots_are_errors_without_partial_pass(self):
        good = self.capture()
        bad_cases = [None, [], {}, {**good, "schema_version": True},
                     {**good, "schema_version": 2}, {**good, "model": "network bytes"},
                     {**good, "entries": {}}, {**good, "entries": [None]},
                     {**good, "entries": good["entries"] * 2},
                     {**good, "rules_source": "relative"}]
        for field, value in (("path", "../escape"), ("path", "/absolute"),
                             ("path", "missing/child"), ("logical_bytes", -1),
                             ("logical_bytes", True), ("logical_bytes", 1.5),
                             ("kind", "socket"), ("kind", "symlink"),
                             ("included_by_rules", False), ("included_by_rules", 1),
                             ("role", "ignore"), ("last_rule", {})):
            bad = copy.deepcopy(good)
            bad["entries"][0][field] = value
            bad_cases.append(bad)
        for snapshot in bad_cases:
            with self.subTest(snapshot=snapshot):
                self.path.write_text(json.dumps(snapshot))
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([str(self.context), "--baseline-snapshot", str(self.path),
                                           "--max-bytes", "1000", "--json"]), 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "error")
                self.assertNotIn("baseline", result)
        for text in ('{"schema_version":1,"schema_version":1}', '{', '\ufeff{}'):
            self.path.write_text(text)
            with self.assertRaises(InputError):
                read_snapshot(self.path)

    def test_rule_provenance_validation(self):
        (self.context / ".dockerignore").write_text("a\n")
        good = self.capture()
        index = next(i for i, row in enumerate(good["entries"]) if row["path"] == "a")
        for field, value in (("source", "other"), ("line", 0), ("line", True),
                             ("raw", None), ("pattern", []), ("negated", True),
                             ("matched_path", "unrelated")):
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(good)
                bad["entries"][index]["last_rule"][field] = value
                self.path.write_text(json.dumps(bad))
                with self.assertRaises(InputError):
                    read_snapshot(self.path)

    def test_snapshot_size_type_and_symlink_boundaries(self):
        with self.path.open("wb") as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        with self.assertRaisesRegex(InputError, "exceeds"):
            read_snapshot(self.path)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaisesRegex(InputError, "regular"):
            read_snapshot(self.path)
        self.path.unlink()
        target = self.work / "target"
        target.write_text("do not replace")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            read_snapshot(self.path)
        with self.assertRaises(FileExistsError):
            write_snapshot(self.path, self.capture())
        self.assertEqual(target.read_text(), "do not replace")
        self.assertEqual(list(self.work.glob(".docker-context-budget-*")), [])
        self.path.unlink()
        alias = self.work / "alias"
        alias.symlink_to(self.context, target_is_directory=True)
        with self.assertRaises(OSError):
            write_snapshot(alias / "new.json", self.capture())
        self.assertFalse((self.context / "new.json").exists())

    def test_snapshot_context_and_dockerfile_must_match(self):
        baseline = self.capture()
        for key, value in (("context", str(self.work / "different")), ("dockerfile", "other")):
            with self.subTest(key=key):
                bad = {**baseline, key: value}
                with self.assertRaisesRegex(InputError, "differs"):
                    compare_snapshots(bad, baseline)


if __name__ == "__main__":
    unittest.main()
