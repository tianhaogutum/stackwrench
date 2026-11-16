"""Subtree limits use CURRENT saved directory rows, never live directory checks."""

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

FLAG = "--max-dir-bytes"
EXT = "--max-extension-bytes"
PAIR = "--max-dir-extension-bytes"


class SavedDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        for name, size in {"build/a.bin": 2000, "build/empty.bin": 0, "build/keep.txt": 13,
                           "build/nested/part.bin": 7, "build/nested/zero": 0,
                           "outside/a.bin": 17, "building/a.bin": 19, "root": 5,
                           "cache=a/file": 8, "build/control.bin": 23,
                           "build/excluded.bin": 29, "ignored/zero.bin": 0}.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
        self.rules = self.context / "build/rules.bin"
        self.rules.write_text("build/excluded.bin\nignored\n!ignored/zero.bin\n")
        (self.context / "build/link.bin").symlink_to("missing-target")
        (self.context / "linkdir").symlink_to("build", target_is_directory=True)
        for name in ("old", "empty", "build/emptydir"):
            (self.context / name).mkdir()
        self.baseline, self.current = self.work / "before.json", self.work / "after.json"
        self.before = self.capture()
        write_snapshot(self.baseline, self.before)
        (self.context / "build/a.bin").write_bytes(b"x" * 1000)
        (self.context / "old").rmdir()
        (self.context / "new").mkdir()
        self.after = self.capture()
        write_snapshot(self.current, self.after)
        shutil.rmtree(self.context)

    def capture(self):
        return from_report(analyze(self.context, dockerfile="build/control.bin",
                                   ignore=self.rules, include_link_targets=False))

    def run_cli(self, *extra, expected=0, json_output=True, baseline=None, current=None):
        args = ["compare-snapshots", str(baseline or self.baseline), str(current or self.current),
                *map(str, extra), *(["--json"] if json_output else [])]
        originals = [path.read_bytes() for path in (self.baseline, self.current)]
        real_open = os.open

        def saved_open(path, *args, **kwargs):
            self.assertNotIn(Path(path).name, {"context", "build", "rules.bin", "control.bin"})
            return real_open(path, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            for target in ("stackwrench.context_budget.cli.analyze", "stackwrench.context_budget.core.inventory",
                           "stackwrench.context_budget.core.read_rules_file", "os.listdir", "os.scandir", "os.readlink"):
                stack.enter_context(patch(target, side_effect=AssertionError("forbidden live read: " + target)))
            stack.enter_context(patch("os.open", side_effect=saved_open))
            output, error = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(error))
            self.assertEqual(main(args), expected)
        self.assertEqual([path.read_bytes() for path in (self.baseline, self.current)], originals)
        self.assertFalse(self.context.exists())
        self.assertFalse(self.rules.exists())
        if json_output:
            self.assertEqual(error.getvalue(), "")
            return json.loads(output.getvalue())
        return output.getvalue(), error.getvalue()

    def test_current_subtree_equality_and_exceedance_without_live_inputs(self):
        plain = self.run_cli()
        self.assertEqual(set(plain), {"schema_version", "status", "model", "snapshot_comparison"})
        comparison = plain["snapshot_comparison"]
        self.assertEqual(comparison["baseline"]["included_payload_bytes"], 2069)
        self.assertEqual(comparison["current"]["included_payload_bytes"], 1069)
        self.assertEqual(comparison["included_payload_bytes_delta"], -1000)
        for limit, code in ((1020, 0), (1019, 1)):
            with self.subTest(limit=limit):
                result = self.run_cli(FLAG, f"build={limit}", expected=code)
                self.assertEqual(result, {**plain, "status": "ok" if code == 0 else "budget_exceeded",
                    "current_saved_directory_budgets": [{"path": "build", "included_payload_bytes": 1020,
                        "included_payload_files": 5, "max_bytes": limit, "passed": code == 0}],
                    "current_saved_budgets_passed": code == 0})

    def test_sorted_nested_overlaps_siblings_reinclusion_zero_and_empty(self):
        expected = [("build", 1020, 5), ("build/emptydir", 0, 0), ("build/nested", 7, 2),
                    ("building", 19, 1), ("cache=a", 8, 1), ("empty", 0, 0),
                    ("ignored", 0, 1), ("new", 0, 0), ("outside", 17, 1)]
        args = []
        for directory, size, _ in reversed(expected):
            args.extend((FLAG, f"{directory}={size}"))
        result = self.run_cli(*args)
        self.assertEqual(result["current_saved_directory_budgets"], [
            {"path": directory, "included_payload_bytes": size, "included_payload_files": count,
             "max_bytes": size, "passed": True} for directory, size, count in expected])
        self.assertTrue(result["current_saved_budgets_passed"])
        # A nonzero reincluded child counts even though its parent remains excluded.
        data = copy.deepcopy(self.after)
        next(r for r in data["entries"] if r["path"] == "ignored/zero.bin")["logical_bytes"] = 3
        self.current.write_text(json.dumps(data))
        result = self.run_cli(FLAG, "ignored=2", expected=1)
        self.assertEqual(result["current_saved_directory_budgets"][0], {
            "path": "ignored", "included_payload_bytes": 3, "included_payload_files": 1,
            "max_bytes": 2, "passed": False})

    def test_all_four_dimensions_must_pass_independently(self):
        for total_ok, dir_ok, ext_ok, pair_ok in itertools.product((True, False), repeat=4):
            passed = total_ok and dir_ok and ext_ok and pair_ok
            with self.subTest(total=total_ok, directory=dir_ok, extension=ext_ok, pair=pair_ok):
                result = self.run_cli("--max-bytes", 1069 - (not total_ok),
                    FLAG, f"build={1020 - (not dir_ok)}", FLAG, "build/nested=7",
                    EXT, f".bin={1043 - (not ext_ok)}", PAIR, "build", f".bin={1007 - (not pair_ok)}",
                    expected=0 if passed else 1)
                self.assertEqual(result["current_saved_budgets_passed"], passed)
                self.assertEqual(result["current_saved_budget"]["passed"], total_ok)
                self.assertEqual(result["current_saved_directory_budgets"][0]["passed"], dir_ok)
                self.assertTrue(result["current_saved_directory_budgets"][1]["passed"])
                self.assertEqual(result["current_saved_extension_budgets"][0]["passed"], ext_ok)
                self.assertEqual(result["current_saved_directory_extension_budgets"][0]["passed"], pair_ok)

    def test_outputs_without_directory_flags_remain_exact(self):
        plain = self.run_cli()
        for total, ext, pair in itertools.product((True, False), repeat=3):
            args, fields = [], {}
            if total:
                args += ["--max-bytes", 1069]
                fields["current_saved_budget"] = {"max_bytes": 1069, "included_payload_bytes": 1069, "passed": True}
            if ext:
                args += [EXT, ".bin=1043"]
                fields["current_saved_extension_budgets"] = [{"extension": ".bin", "included_payload_bytes": 1043,
                    "included_payload_files": 6, "max_bytes": 1043, "passed": True}]
            if pair:
                args += [PAIR, "build", ".bin=1007"]
                fields["current_saved_directory_extension_budgets"] = [{"path": "build", "extension": ".bin",
                    "included_payload_bytes": 1007, "included_payload_files": 3, "max_bytes": 1007, "passed": True}]
            if ext or pair:
                fields["current_saved_budgets_passed"] = True
            self.assertEqual(self.run_cli(*args), {**plain, **fields})
            text, error = self.run_cli(*args, json_output=False)
            self.assertEqual(error, "")
            self.assertNotIn("Current saved directory budget", text)
            self.assertEqual("Current saved budgets:" in text, ext or pair)

    def test_invalid_selectors_amounts_duplicates_and_missing_operand_fail_closed(self):
        invalid = ("old=0", "missing=0", "build/a.bin=0", "linkdir=0", "build/link.bin=0",
                   "=0", ".=0", "/=0", "/build=0", "build/=0", "build//nested=0", "build/../outside=0",
                   "./build=0", "build/./nested=0", "build\x00=0", "build=-1", "build=+1", "build=١",
                   "build=1.5", "build=1KB", "build=", "build", "", "build=" + "9" * 5000)
        for value in invalid:
            with self.subTest(value=value[:50]):
                result = self.run_cli(FLAG, value, "--max-bytes", 1069, EXT, ".bin=1043",
                                      PAIR, "build", ".bin=1007", expected=2)
                self.assertEqual(set(result), {"schema_version", "status", "error"})
                self.assertEqual(result["status"], "error")
        for value in ("build=1020", "build=0"):
            result = self.run_cli(FLAG, "build=1020", FLAG, value, expected=2)
            self.assertIn("duplicate", result["error"])
        text, error = self.run_cli(FLAG, "old=0", json_output=False, expected=2)
        self.assertEqual(text, "")
        self.assertIn("CURRENT saved directory", error)
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as raised:
                main(["compare-snapshots", str(self.baseline), str(self.current), "--json", FLAG])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("expected one argument", error.getvalue())

    def test_invalid_or_incompatible_either_artifact_has_no_partial_results(self):
        bad = self.work / "bad.json"
        malformed = copy.deepcopy(self.after)
        malformed["entries"][0]["logical_bytes"] = True
        for data in ({}, malformed, {**self.after, "schema_version": 2},
                     {**self.after, "entries": self.after["entries"] * 2},
                     {**self.after, "entries": [r for r in self.after["entries"] if r["path"] != "build"]},
                     {**self.after, "context": str(self.work / "other")},
                     {**self.after, "dockerfile": "Otherfile", "entries": []}):
            bad.write_text(json.dumps(data))
            for before, after in ((bad, self.current), (self.baseline, bad)):
                result = self.run_cli(FLAG, "build=1020", "--max-bytes", 1069, EXT, ".bin=1043",
                                      PAIR, "build", ".bin=1007", baseline=before, current=after, expected=2)
                self.assertEqual(set(result), {"schema_version", "status", "error"})
                self.assertEqual(result["status"], "error")
        for content in ('{"format":1,"format":1}', '{', '\ufeff{}'):
            bad.write_text(content)
            for before, after in ((bad, self.current), (self.baseline, bad)):
                self.assertEqual(set(self.run_cli(FLAG, "build=1020", baseline=before, current=after, expected=2)),
                                 {"schema_version", "status", "error"})
        bad.unlink()
        for before, after in ((bad, self.current), (self.baseline, bad)):
            self.assertEqual(self.run_cli(FLAG, "build=1020", baseline=before, current=after, expected=2)["status"], "error")

    def test_text_escaping_unicode_equals_and_current_directory_membership(self):
        directory = "目录=a\n\x1b[31m"
        data = copy.deepcopy(self.after)
        data["entries"].extend([
            {"path": path, "kind": kind, "logical_bytes": size, "role": "payload",
             "included_by_rules": True, "last_rule": None}
            for path, kind, size in ((directory, "directory", 0), (directory + "/file", "file", 8))])
        data["entries"].sort(key=lambda r: r["path"])
        self.current.write_text(json.dumps(data))
        text, error = self.run_cli(FLAG, directory + "=7", expected=1, json_output=False)
        self.assertEqual(error, "")
        self.assertNotIn("\x1b", text)
        self.assertIn("Current saved directory budget '目录=a\\n\\x1b[31m': 8 / 7 bytes; 1 files; EXCEEDED", text)
        self.assertIn("Current saved budgets: EXCEEDED", text)
        result = self.run_cli(FLAG, directory + "=8")
        self.assertTrue(result["current_saved_budgets_passed"])
        # A directory present only in CURRENT is valid; an empty inventory has none.
        self.assertTrue(self.run_cli(FLAG, "new=0")["current_saved_budgets_passed"])
        self.current.write_text(json.dumps({**self.after, "entries": []}))
        self.assertEqual(self.run_cli(FLAG, "build=0", expected=2)["status"], "error")


if __name__ == "__main__":
    unittest.main()
