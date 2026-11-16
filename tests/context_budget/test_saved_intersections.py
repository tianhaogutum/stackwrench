"""CURRENT saved intersection limits never depend on baseline sizes or live inputs."""

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
from stackwrench.context_budget.snapshot import from_report, write_snapshot

FLAG = "--max-dir-extension-bytes"


class SavedIntersectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        self.rules = self.work / "external.rules"
        self.rules.write_text("build/excluded.bin\nignored\n!ignored/zero.bin\n")
        for name, size in {"build/a.bin": 2000, "build/keep.txt": 13,
                           "outside/a.bin": 17, "building/a.bin": 19,
                           "build/control.bin": 23, "build/excluded.bin": 29,
                           "ignored/zero.bin": 0}.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
        (self.context / "build/link.bin").symlink_to("missing-target")
        (self.context / "linkdir").symlink_to("build", target_is_directory=True)
        (self.context / "old").mkdir()
        self.baseline, self.current = self.work / "before.json", self.work / "after.json"
        self.before = self.capture()
        write_snapshot(self.baseline, self.before)
        (self.context / "build/a.bin").write_bytes(b"x" * 1000)
        (self.context / "old").rmdir()
        (self.context / "new").mkdir()
        self.after = self.capture()
        write_snapshot(self.current, self.after)
        shutil.rmtree(self.context)
        self.rules.unlink()

    def capture(self):
        return from_report(analyze(self.context, dockerfile="build/control.bin",
                                   ignore=self.rules, include_link_targets=False))

    def run_cli(self, *extra, expected=0, json_output=True, baseline=None, current=None):
        args = ["compare-snapshots", str(baseline or self.baseline), str(current or self.current),
                *map(str, extra), *(["--json"] if json_output else [])]
        # No recorded live path may be opened, even if a future implementation
        # catches the missing-file error and falls back to saved data.
        real_open = os.open

        def saved_open(path, *args, **kwargs):
            self.assertNotIn(Path(path).name, {"context", "external.rules", "control.bin"})
            return real_open(path, *args, **kwargs)

        originals = [path.read_bytes() for path in (self.baseline, self.current)]
        with contextlib.ExitStack() as stack:
            for target in ("stackwrench.context_budget.cli.analyze", "stackwrench.context_budget.core.inventory",
                           "stackwrench.context_budget.core.read_rules_file", "os.listdir", "os.scandir",
                           "os.readlink"):
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

    def test_saved_intersection_equality_and_exceedance_no_live_inputs(self):
        plain = self.run_cli()
        self.assertEqual(set(plain), {"schema_version", "status", "model", "snapshot_comparison"})
        self.assertEqual(plain["snapshot_comparison"]["included_payload_bytes_delta"], -1000)
        for limit, code in ((1000, 0), (999, 1)):
            with self.subTest(limit=limit):
                result = self.run_cli(FLAG, "build", f".bin={limit}", expected=code)
                self.assertEqual(result, {**plain, "status": "ok" if code == 0 else "budget_exceeded",
                    "current_saved_directory_extension_budgets": [{
                        "path": "build", "extension": ".bin", "included_payload_bytes": 1000,
                        "included_payload_files": 1, "max_bytes": limit, "passed": code == 0}],
                    "current_saved_budgets_passed": code == 0})

    def test_total_and_every_pair_must_pass_independently(self):
        total = self.run_cli()["snapshot_comparison"]["current"]["included_payload_bytes"]
        for total_limit, pair_limit, code in ((total, 1000, 0), (total-1, 1000, 1),
                                              (total, 999, 1), (total-1, 999, 1)):
            with self.subTest(total=total_limit, pair=pair_limit):
                result = self.run_cli("--max-bytes", total_limit, FLAG, "build", f".bin={pair_limit}",
                                      FLAG, "outside", ".bin=17", expected=code)
                self.assertEqual(result["current_saved_budget"], {
                    "max_bytes": total_limit, "included_payload_bytes": total, "passed": total <= total_limit})
                self.assertEqual(result["current_saved_budgets_passed"], code == 0)
                self.assertTrue(result["current_saved_directory_extension_budgets"][1]["passed"])

    def test_nested_suffix_boundaries_zero_counts_controls_and_excluded_directories(self):
        data = copy.deepcopy(self.after)
        def add(path, size=0, kind="file"):
            data["entries"].append({"path": path, "logical_bytes": size, "kind": kind,
                                    "role": "payload", "included_by_rules": True, "last_rule": None})
        add("build/nested", kind="directory")
        add("build/nested/empty.bin")
        add("build/nested/archive.tar.bin", 7)
        add("build/nested/archive.bin.gz", 11)
        add("build/cache.bin", kind="directory")
        add("build/cache.bin/a.py", 31)
        add("build/.bin", 2)
        add("build/no_suffix", 3)
        add("build/end.", 5)
        add("build/c.BIN", 13)
        add("build/zero.bin")
        # Active ignore controls are excluded from payload even when their
        # saved inclusion decision and final suffix would otherwise match.
        data["rules_source"] = str(self.context / "build/rules.bin")
        add("build/rules.bin", 37)
        for row in data["entries"]:
            if row["path"] == "build/rules.bin":
                row["role"] = "ignore"
            if row["last_rule"] is not None:
                row["last_rule"]["source"] = data["rules_source"]
        data["entries"].sort(key=lambda row: row["path"])
        self.current.write_text(json.dumps(data))
        expected = [("build", ".BIN", 13, 1), ("build", ".absent", 0, 0),
                    ("build", ".bin", 1007, 4), ("build", ".gz", 11, 1),
                    ("build", "<none>", 10, 3), ("build/nested", ".bin", 7, 2),
                    ("ignored", ".bin", 0, 1), ("new", ".bin", 0, 0)]
        args = []
        for directory, suffix, size, _ in reversed(expected):
            args.extend((FLAG, directory, f"{suffix}={size}"))
        result = self.run_cli(*args)
        self.assertEqual(result["current_saved_directory_extension_budgets"], [
            {"path": directory, "extension": suffix, "included_payload_bytes": size,
             "included_payload_files": count, "max_bytes": size, "passed": True}
            for directory, suffix, size, count in expected])
        self.assertTrue(result["current_saved_budgets_passed"])

    def test_invalid_duplicate_missing_and_non_directory_current_pairs_fail_closed(self):
        invalid = [(directory, ".bin=0") for directory in
                   ("", ".", "/build", "../build", "build/", "build//nested", "build/./nested",
                    "build/../outside", "build\x00", "missing", "old", "build/a.bin", "linkdir")]
        invalid += [("build", value) for value in
                    ("bin=1", ".tar.gz=1", ".=1", ".bin/a=0", ".bin=-1", ".bin=+1", ".bin=١",
                     ".bin=1.5", ".bin=1KB", ".bin=", ".bin", ".bin=" + "9" * 5000)]
        for directory, value in invalid:
            with self.subTest(directory=directory, value=value[:50]):
                result = self.run_cli(FLAG, directory, value, "--max-bytes", 100000, expected=2)
                self.assertEqual(set(result), {"schema_version", "status", "error"})
                self.assertEqual(result["status"], "error")
        result = self.run_cli(FLAG, "build", ".bin=1000", FLAG, "build", ".bin=999", expected=2)
        self.assertIn("duplicate intersection", result["error"])
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as raised:
                main(["compare-snapshots", str(self.baseline), str(self.current), "--json", FLAG, "build"])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("expected 2 arguments", error.getvalue())
        text, error = self.run_cli(FLAG, "old", ".bin=0", expected=2, json_output=False)
        self.assertEqual(text, "")
        self.assertIn("CURRENT saved directory", error)

    def test_invalid_or_incompatible_either_artifact_has_no_partial_results(self):
        bad = self.work / "bad.json"
        malformed = copy.deepcopy(self.after)
        malformed["entries"][0]["logical_bytes"] = True
        bad_parent = copy.deepcopy(self.after)
        bad_parent["entries"] = [r for r in bad_parent["entries"] if r["path"] != "build"]
        incompatible = {**self.after, "context": str(self.work / "other-context")}
        for value in ({}, malformed, bad_parent, {**self.after, "schema_version": 2},
                      {**self.after, "entries": self.after["entries"] * 2}, incompatible,
                      {**self.after, "dockerfile": "Otherfile", "entries": []}):
            bad.write_text(json.dumps(value))
            for before, after in ((bad, self.current), (self.baseline, bad)):
                result = self.run_cli(FLAG, "build", ".bin=1000", "--max-bytes", 100000,
                                      baseline=before, current=after, expected=2)
                self.assertEqual(set(result), {"schema_version", "status", "error"})
                self.assertEqual(result["status"], "error")
        for content in ('{"format":1,"format":1}', '{', '\ufeff{}'):
            bad.write_text(content)
            result = self.run_cli(FLAG, "build", ".bin=1000", current=bad, expected=2)
            self.assertEqual(set(result), {"schema_version", "status", "error"})
        bad.unlink()
        for before, after in ((bad, self.current), (self.baseline, bad)):
            self.assertEqual(self.run_cli(FLAG, "build", ".bin=0", baseline=before, current=after,
                                          expected=2)["status"], "error")

    def test_text_selector_escaping_and_equals_unicode_pair_names(self):
        directory = "目录=a\n\x1b[31m"
        data = copy.deepcopy(self.after)
        data["entries"].extend([
            {"path": path, "kind": kind, "logical_bytes": size, "role": "payload",
             "included_by_rules": True, "last_rule": None}
            for path, kind, size in ((directory, "directory", 0), (directory + "/a.b=", "file", 8))])
        data["entries"].sort(key=lambda r: r["path"])
        self.current.write_text(json.dumps(data))
        text, error = self.run_cli(FLAG, directory, ".b==7", expected=1, json_output=False)
        self.assertEqual(error, "")
        self.assertNotIn("\x1b", text)
        self.assertIn("目录=a\\n\\x1b[31m", text)
        self.assertIn("Current saved intersection budget", text)
        self.assertIn("extension '.b=': 8 / 7 bytes; 1 files; EXCEEDED", text)
        self.assertIn("Current saved budgets: EXCEEDED", text)
        result = self.run_cli(FLAG, directory, ".b==8")
        self.assertEqual(result["current_saved_directory_extension_budgets"][0]["included_payload_files"], 1)
        self.assertTrue(result["current_saved_budgets_passed"])


if __name__ == "__main__":
    unittest.main()
