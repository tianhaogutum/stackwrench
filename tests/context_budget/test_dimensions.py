"""Dimension budgets and independently replayable, read-only suggestions."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from stackwrench.context_budget.ignore import parse_rules
from stackwrench.context_budget.core import analyze

from .support import CLI, ENV


class DimensionCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        self.write("src/a.py", 20)
        self.write("build/b.bin", 1000)

    def write(self, path, size):
        file = self.context / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"a" * size)

    def cli(self, *args, expected=0):
        process = subprocess.run([*CLI, str(self.context), *map(str, args), "--json"],
                                 cwd=self.work, capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, expected, (process.stdout, process.stderr))
        return json.loads(process.stdout)

    def test_dimension_directory_only_failure_and_equal_boundary(self):
        report = self.cli("--max-bytes", 2000, "--max-dir-bytes", "build=999", expected=1)
        budget = report["baseline"]["budget"]
        self.assertTrue(budget["total_passed"])
        self.assertFalse(budget["passed"])
        self.assertEqual(budget["directories"], [
            {"path": "build", "included_payload_bytes": 1000, "max_bytes": 999, "passed": False}])
        self.cli("--max-dir-bytes", "build=1000", "--max-extension-bytes", ".bin=1000")
        self.cli("--max-bytes", 1019, expected=1)
        self.cli("--max-bytes", 1020)
        self.cli("--max-extension-bytes", ".bin=999", expected=1)

    def test_dimension_top_n_stable_and_does_not_hide_budgets(self):
        self.write("aaa/c.dat", 1000)
        report = self.cli("--top", 1, "--largest", 0,
                          "--max-dir-bytes", "src=19", expected=1)
        side = report["baseline"]
        self.assertEqual(side["contributions"]["directories"], [
            {"path": "aaa", "included_payload_bytes": 1000, "included_payload_files": 1}])
        self.assertEqual(side["contributions"]["extensions"], [
            {"extension": ".bin", "included_payload_bytes": 1000, "included_payload_files": 1}])
        self.assertEqual(side["budget"]["directories"][0]["path"], "src")
        self.assertLessEqual(len(side["ignore_suggestions"]), 1)
        self.assertTrue(side["ignore_suggestions"])
        self.assertEqual(report, self.cli("--top", 1, "--largest", 0,
                                          "--max-dir-bytes", "src=19", expected=1))
        zero = self.cli("--top", 0, "--max-dir-bytes", "src=19", expected=1)
        self.assertEqual(zero["baseline"]["contributions"]["directories"], [])
        self.assertEqual(zero["baseline"]["contributions"]["extensions"], [])
        self.assertEqual(zero["baseline"]["ignore_suggestions"], [])

    def test_dimension_suggestions_replay_and_never_mutate(self):
        rules = self.context / ".dockerignore"
        rules.write_text("build\n!build/b.bin\n")
        self.write("Dockerfile", 12)
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.context.rglob("*") if p.is_file()}
        report = self.cli("--max-dir-bytes", "build=999", expected=1)
        suggestions = report["baseline"]["ignore_suggestions"]
        self.assertEqual(suggestions, sorted(suggestions, key=lambda row: (
            row["logical_payload_bytes_delta"], row["append_pattern"])))
        chosen = next(row for row in suggestions if row["append_pattern"] == "build")
        self.assertEqual(chosen["logical_payload_bytes_delta"], -1000)
        # build itself was already excluded; only its reincluded file changes.
        self.assertEqual([row["path"] for row in chosen["changed_entries"]], ["build/b.bin"])
        self.assertTrue(chosen["budget_after"]["passed"])
        for row in suggestions:
            self.assertEqual(len(parse_rules(row["append_pattern"], "suggestion")), 1)
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in self.context.rglob("*") if p.is_file()})
        candidate = self.work / "candidate.ignore"
        for row in suggestions:
            candidate.write_text(rules.read_text() + "\n" + row["append_pattern"] + "\n")
            replay = self.cli("--candidate-ignore", candidate, "--max-dir-bytes", "build=999",
                              expected=0 if row["budget_after"]["passed"] else 1)
            self.assertEqual(replay["comparison"]["included_payload_bytes_delta"],
                             row["logical_payload_bytes_delta"])
            self.assertEqual(replay["candidate"]["budget"], row["budget_after"])

    def test_dimension_nested_subtrees_extensions_and_accounting(self):
        self.write("build/deep/c.bin", 20)
        self.write("build/empty", 0)
        self.write("build/.hidden", 2)
        self.write("build/end.", 3)
        self.write("build/archive.tar.gz", 4)
        self.write("build/c.BIN", 5)
        self.write("Dockerfile", 200)
        (self.context / ".dockerignore").write_text("build/b.bin\n")
        (self.context / "build/link").symlink_to("outside-never-read")
        report = self.cli("--max-dir-bytes", "build=34", "--max-dir-bytes", "build/deep=20",
                          "--max-extension-bytes", "<none>=5", "--max-extension-bytes", ".absent=0")
        side = report["baseline"]
        dirs = {row["path"]: row for row in side["contributions"]["directories"]}
        self.assertEqual(dirs["build"]["included_payload_bytes"], 34)
        self.assertEqual(dirs["build/deep"]["included_payload_bytes"], 20)
        exts = {row["extension"]: row for row in side["contributions"]["extensions"]}
        self.assertEqual(exts["<none>"]["included_payload_bytes"], 5)
        self.assertEqual(exts["<none>"]["included_payload_files"], 3)
        self.assertEqual(exts[".bin"]["included_payload_bytes"], 20)
        self.assertEqual(exts[".BIN"]["included_payload_bytes"], 5)
        self.assertEqual(exts[".gz"]["included_payload_bytes"], 4)
        self.assertEqual(side["ignore_suggestions"], [])

    def test_dimension_extension_suggestion_reports_collateral_descendants_and_controls(self):
        self.write("cache.bin/source.py", 7)
        self.write("cache.bin/Dockerfile", 13)
        report = self.cli("--dockerfile", "cache.bin/Dockerfile",
                          "--max-extension-bytes", ".bin=999", expected=1)
        row = next(row for row in report["baseline"]["ignore_suggestions"]
                   if row["append_pattern"] == "**/*.bin")
        self.assertEqual(row["logical_payload_bytes_delta"], -1007)
        changes = {change["path"]: change for change in row["changed_entries"]}
        self.assertEqual(changes["cache.bin/source.py"]["logical_payload_bytes_delta"], -7)
        self.assertEqual(changes["cache.bin/Dockerfile"]["logical_payload_bytes_delta"], 0)
        self.assertEqual(changes["cache.bin/Dockerfile"]["role"], "dockerfile")
        candidate = self.work / "candidate.ignore"
        candidate.write_text("**/*.bin\n")
        replay = self.cli("--dockerfile", "cache.bin/Dockerfile", "--candidate-ignore", candidate)
        self.assertEqual(replay["comparison"]["included_payload_bytes_delta"], -1007)

    def test_dimension_advice_escapes_literals_and_omits_unrepresentable_names(self):
        # Remove standard inputs so every proposed pattern has to handle these names.
        (self.context / "build/b.bin").unlink()
        (self.context / "src/a.py").unlink()
        paths = ["!cache**[x]/a?b", "line\n\x1b[31m", "ending ", "x\x1cy", "\ufeffbom", "\u00a0leading"]
        for path in paths:
            self.write(path, 10)
        report = self.cli("--max-bytes", 0, expected=1)
        suggestions = report["baseline"]["ignore_suggestions"]
        self.assertTrue(suggestions)
        for row in suggestions:
            self.assertNotIn("\n", row["append_pattern"])
            self.assertNotIn("\x1b", row["append_pattern"])
            self.assertEqual(row["logical_payload_bytes_delta"], -10)
            self.assertEqual([p["path"] for p in row["changed_entries"] if p["kind"] == "file"],
                             [paths[0]])
            candidate = self.work / "candidate.ignore"
            candidate.write_text(row["append_pattern"] + "\n")
            replay = self.cli("--candidate-ignore", candidate, "--max-bytes", 50)
            self.assertEqual(replay["comparison"]["included_payload_bytes_delta"], -10)
        process = subprocess.run([*CLI, str(self.context), "--max-bytes", "0"],
                                 capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("\x1b", process.stdout)

    def test_dimension_invalid_inputs_fail_without_partial_report(self):
        for flag, value in (("--max-dir-bytes", "../build=0"), ("--max-dir-bytes", "/build=0"),
                            ("--max-dir-bytes", "build/=0"), ("--max-dir-bytes", "build//x=0"),
                            ("--max-dir-bytes", "missing=0"), ("--max-dir-bytes", "build/b.bin=0"),
                            ("--max-dir-bytes", "build=-1"), ("--max-dir-bytes", "build=1KB"),
                            ("--max-dir-bytes", "build"), ("--max-dir-bytes", "build=1.5"),
                            ("--max-extension-bytes", "bin=1"), ("--max-extension-bytes", ".tar.gz=1"),
                            ("--max-extension-bytes", ".=1"), ("--max-extension-bytes", ".bin/a=0")):
            with self.subTest(flag=flag, value=value):
                report = self.cli(flag, value, expected=2)
                self.assertEqual(report["status"], "error")
                self.assertNotIn("baseline", report)
        self.cli("--max-dir-bytes", "build=1", "--max-dir-bytes", "build=2", expected=2)
        for kwargs in ({"max_dir_bytes": {"build": True}}, {"max_extension_bytes": {".bin": -1}},
                       {"max_extension_bytes": {".bin": 1.5}}, {"top": True}, {"top": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                analyze(self.context, **kwargs)

    def test_dimension_candidate_and_snapshot_modes_use_current_selected_side(self):
        candidate = self.work / "candidate.ignore"
        candidate.write_text("build\n")
        report = self.cli("--candidate-ignore", candidate, "--max-dir-bytes", "build=0",
                          "--max-extension-bytes", ".bin=0")
        self.assertFalse(report["baseline"]["budget"]["passed"])
        self.assertTrue(report["candidate"]["budget"]["passed"])
        self.assertEqual(report["candidate"]["ignore_suggestions"], [])
        snapshot = self.work / "baseline.json"
        self.cli("--save-snapshot", snapshot, "--max-dir-bytes", "build=999", expected=1)
        report = self.cli("--baseline-snapshot", snapshot, "--max-dir-bytes", "build=1000")
        self.assertEqual(report["snapshot_comparison"]["changes"], [])

    def test_dimension_root_only_no_extension_suggestion_and_empty_directory(self):
        (self.context / "build/b.bin").unlink()
        (self.context / "src/a.py").unlink()
        self.write("plain", 4)
        self.write("empty", 0)
        report = self.cli("--max-extension-bytes", "<none>=3", "--max-dir-bytes", "build=0", expected=1)
        suggestion, = report["baseline"]["ignore_suggestions"]
        self.assertEqual(suggestion["append_pattern"], "plain")
        self.assertEqual(suggestion["logical_payload_bytes_delta"], -4)
        self.assertTrue(suggestion["budget_after"]["passed"])
        self.cli("--max-extension-bytes", "<none>=4")

    def test_dimension_failing_candidate_advice_uses_candidate_rules(self):
        baseline = self.context / ".dockerignore"
        baseline.write_text("build\n")
        candidate = self.work / "candidate.ignore"
        candidate.write_text("**\n!build/b.bin\n")
        original = candidate.read_text()
        report = self.cli("--candidate-ignore", candidate, "--max-dir-bytes", "build=999", expected=1)
        self.assertTrue(report["baseline"]["budget"]["passed"])
        self.assertEqual(report["baseline"]["ignore_suggestions"], [])
        suggestion = report["candidate"]["ignore_suggestions"][0]
        self.assertEqual(candidate.read_text(), original)
        appended = self.work / "appended.ignore"
        appended.write_text(original + "\n" + suggestion["append_pattern"] + "\n")
        replay = self.cli("--candidate-ignore", appended, "--max-dir-bytes", "build=999")
        self.assertEqual(replay["candidate"]["summary"]["included_payload_bytes"] -
                         report["candidate"]["summary"]["included_payload_bytes"],
                         suggestion["logical_payload_bytes_delta"])
        self.assertEqual(replay["candidate"]["budget"], suggestion["budget_after"])

    def test_dimension_directory_selector_containing_equals_and_unicode(self):
        self.write("目录=a/keep", 4)
        self.cli("--max-dir-bytes", "目录=a=4")
        report = self.cli("--max-dir-bytes", "目录=a=3", expected=1)
        self.assertEqual(report["baseline"]["budget"]["directories"][0]["path"], "目录=a")


if __name__ == "__main__":
    unittest.main()
