"""Explicit top-N summaries of CURRENT saved rows, with no live input access."""

import contextlib
import copy
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.context_budget.cli import main
from stackwrench.context_budget.core import analyze
from stackwrench.context_budget.snapshot import from_report, write_snapshot


def rows(key, values):
    return [{key: name, "included_payload_bytes": size, "included_payload_files": count}
            for name, size, count in values]


DIRECTORIES = [("build", 15, 5), ("building", 7, 1), ("tie", 7, 1),
               ("build/nested", 5, 2), ("ignored", 3, 1),
               ("empty", 0, 0), ("zero", 0, 1)]
EXTENSIONS = [(".bin", 23, 6), ("<none>", 10, 4), (".BIN", 7, 1), (".A", 4, 1),
              (".gz", 4, 1), (".txt", 2, 1), (".zero", 0, 1)]


class SavedContributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        for name, size in {"build/a.bin": 108, "build/empty.bin": 0, "build/keep.txt": 2,
                           "build/nested/part.bin": 5, "build/nested/zero": 0,
                           "building/a.bin": 7, "tie/a.BIN": 7, "ignored/keep.bin": 3,
                           "archive.tar.gz": 4, "root.A": 4, ".hidden": 4, "root": 4, "name.": 2,
                           "zero/only.zero": 0, "empty.bin": 0,
                           "build/control.ctl": 40, "build/excluded.skip": 90}.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
        (self.context / "empty").mkdir()
        (self.context / "build/link.link").symlink_to("missing")
        (self.context / "alias").symlink_to("build", target_is_directory=True)
        self.rules = self.context / "rules.rules"
        self.rules.write_text("build/excluded.skip\nignored\n!ignored/keep.bin\n")
        self.baseline, self.current = self.work / "before.json", self.work / "after.json"
        write_snapshot(self.baseline, self.capture())
        (self.context / "build/a.bin").write_bytes(b"x" * 8)
        self.after = self.capture()
        write_snapshot(self.current, self.after)
        shutil.rmtree(self.context)

    def capture(self):
        return from_report(analyze(self.context, dockerfile="build/control.ctl",
                                   ignore=self.rules, include_link_targets=False))

    def run_cli(self, *extra, expected=0, json_output=True, baseline=None, current=None):
        args = ["compare-snapshots", str(baseline or self.baseline), str(current or self.current),
                *map(str, extra), *(["--json"] if json_output else [])]
        originals = [p.read_bytes() for p in (self.baseline, self.current)]
        real_open = os.open
        allowed = {Path(baseline or self.baseline).name, Path(current or self.current).name}
        parent_stat = self.work.stat()

        def saved_open(path, flags, *args, **kwargs):
            # The strict saved reader opens ancestor directory descriptors and the artifacts only.
            p = Path(path)
            self.assertFalse(p == self.context or p.is_relative_to(self.context))
            if not flags & os.O_DIRECTORY:
                self.assertIn(str(p), allowed)
                parent = os.fstat(kwargs["dir_fd"])
                self.assertEqual((parent.st_dev, parent.st_ino), (parent_stat.st_dev, parent_stat.st_ino))
            return real_open(path, flags, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            for target in ("stackwrench.context_budget.cli.analyze", "stackwrench.context_budget.core.inventory",
                           "stackwrench.context_budget.core.read_rules_file", "stackwrench.context_budget.dimensions.suggestions",
                           "os.listdir", "os.scandir", "os.readlink"):
                stack.enter_context(patch(target, side_effect=AssertionError("forbidden live read/advice: " + target)))
            stack.enter_context(patch("os.open", side_effect=saved_open))
            output, error = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(error))
            self.assertEqual(main(args), expected)
        self.assertEqual([p.read_bytes() for p in (self.baseline, self.current)], originals)
        self.assertFalse(self.context.exists())
        self.assertFalse(self.rules.exists())
        if json_output:
            self.assertEqual(error.getvalue(), "")
            return json.loads(output.getvalue())
        return output.getvalue(), error.getvalue()

    def test_explicit_top_current_only_ordered_groups_without_live_inputs(self):
        plain = self.run_cli()
        result = self.run_cli("--top", 99)
        self.assertEqual(plain["snapshot_comparison"]["baseline"]["included_payload_bytes"], 150)
        self.assertEqual(plain["snapshot_comparison"]["current"]["included_payload_bytes"], 50)
        self.assertEqual(plain["snapshot_comparison"]["included_payload_bytes_delta"], -100)
        self.assertEqual(result, {**plain, "current_saved_contributions": {
            "directories": rows("path", DIRECTORIES), "extensions": rows("extension", EXTENSIONS)}})
        # The two extension ties sort by suffix; excluded parents retain reincluded children.
        self.assertEqual(sum(r[1] for r in EXTENSIONS), 50)
        self.assertEqual(sum(r[2] for r in EXTENSIONS), 15)

    def test_top_independently_limits_each_list_including_zero_and_absent_default(self):
        for n in (0, 1, 2, 6, 7, 99):
            with self.subTest(top=n):
                result = self.run_cli("--top", n)
                self.assertEqual(result["current_saved_contributions"], {
                    "directories": rows("path", DIRECTORIES[:n]),
                    "extensions": rows("extension", EXTENSIONS[:n])})
        self.assertEqual(set(self.run_cli()), {"schema_version", "status", "model", "snapshot_comparison"})
        text, _ = self.run_cli(json_output=False)
        self.assertNotIn("Current saved contributions", text)
        # A completely empty CURRENT is valid; no synthetic root or suffix rows.
        self.current.write_text(json.dumps({**self.after, "entries": []}))
        self.assertEqual(self.run_cli("--top", 99)["current_saved_contributions"],
                         {"directories": [], "extensions": []})

    def test_complete_budgets_and_exit_decisions_unchanged_below_top_cutoff(self):
        for total_ok, dir_ok, ext_ok, pair_ok in itertools.product((True, False), repeat=4):
            passed = total_ok and dir_ok and ext_ok and pair_ok
            args = ["--max-bytes", 50 - (not total_ok),
                    "--max-dir-bytes", f"ignored={3 - (not dir_ok)}", "--max-dir-bytes", "zero=0",
                    "--max-extension-bytes", f".txt={2 - (not ext_ok)}", "--max-extension-bytes", ".absent=0",
                    "--max-dir-extension-bytes", "build/nested", f".bin={5 - (not pair_ok)}",
                    "--max-dir-extension-bytes", "zero", ".zero=0"]
            code = 0 if passed else 1
            with self.subTest(states=(total_ok, dir_ok, ext_ok, pair_ok)):
                plain = self.run_cli(*args, expected=code)
                for top in (0, 1):
                    result = self.run_cli(*args, "--top", top, expected=code)
                    self.assertEqual(result.pop("current_saved_contributions"), {
                        "directories": rows("path", DIRECTORIES[:top]),
                        "extensions": rows("extension", EXTENSIONS[:top])})
                    self.assertEqual(result, plain)
                    self.assertEqual([len(result[k]) for k in ("current_saved_directory_budgets",
                        "current_saved_extension_budgets", "current_saved_directory_extension_budgets")], [2, 2, 2])
                    self.assertEqual(result["current_saved_budgets_passed"], passed)

    def test_invalid_negative_missing_and_overlong_top_have_no_stdout(self):
        originals = [p.read_bytes() for p in (self.baseline, self.current)]
        for extra in (("--top",), *(("--top", v) for v in ("-1", "1.5", "no", "1KB", "", "9" * 5000))):
            with self.subTest(extra=extra[:1]), contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(["compare-snapshots", str(self.baseline), str(self.current), "--json", *extra])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("error:", error.getvalue())
        self.assertEqual([p.read_bytes() for p in (self.baseline, self.current)], originals)

    def test_invalid_and_incompatible_both_sides_produce_error_only(self):
        bad = self.work / "bad.json"
        orphan = copy.deepcopy(self.after)
        orphan["entries"] = [r for r in orphan["entries"] if r["path"] != "build"]
        for data in ({}, {**self.after, "schema_version": 2}, orphan,
                     {**self.after, "entries": self.after["entries"] * 2},
                     {**self.after, "context": str(self.work / "other")},
                     {**self.after, "dockerfile": "Otherfile", "entries": []}):
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
            for top in (0, 2):
                result = self.run_cli("--top", top, "--max-bytes", 50, "--max-dir-bytes", "build=15",
                    "--max-extension-bytes", ".bin=23", "--max-dir-extension-bytes", "build", ".bin=13",
                    baseline=before, current=after, expected=2)
                self.assertEqual(set(result), {"schema_version", "status", "error"})
                self.assertEqual(result["status"], "error")
            text, error = self.run_cli("--top", 1, baseline=before, current=after, expected=2, json_output=False)
            self.assertEqual(text, "")
            self.assertIn("stackwrench context-budget:", error)

    def test_text_labels_empty_lists_and_escaped_selectors(self):
        text, error = self.run_cli("--top", 1, json_output=False)
        self.assertEqual(error, "")
        self.assertIn("Current saved contributions: directory subtrees overlap; final extensions are case-sensitive.", text)
        self.assertIn("  directories: 'build': 15 bytes / 5 files", text)
        self.assertIn("  extensions: '.bin': 23 bytes / 6 files", text)
        zero, _ = self.run_cli("--top", 0, json_output=False)
        self.assertIn("Current saved contributions:", zero)
        self.assertNotIn("  directories:", zero)
        self.assertNotIn("  extensions:", zero)
        data = copy.deepcopy(self.after)
        directory, suffix = "目录=a\n\x1b[31m", ".x\n\x1b[31m"
        data["entries"].extend([
            {"path": p, "kind": k, "logical_bytes": n, "role": "payload",
             "included_by_rules": True, "last_rule": None}
            for p, k, n in ((directory, "directory", 0), (directory + "/a" + suffix, "file", 100))])
        data["entries"].sort(key=lambda r: r["path"])
        self.current.write_text(json.dumps(data))
        text, _ = self.run_cli("--top", 1, json_output=False)
        self.assertNotIn("\x1b", text)
        self.assertIn(repr(directory) + ": 100 bytes / 1 files", text)
        self.assertIn(repr(suffix) + ": 100 bytes / 1 files", text)


if __name__ == "__main__":
    unittest.main()
