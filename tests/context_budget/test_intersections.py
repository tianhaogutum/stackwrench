"""Subtree/final-suffix intersection contracts and independently replayed advice."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from stackwrench.context_budget.core import analyze

from .support import CLI, ENV

FLAG = "--max-dir-extension-bytes"


class IntersectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        self.write("src/a.py", 20)
        self.write("build/b.bin", 1000)

    def write(self, name, size):
        path = self.context / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"a" * size)
        return path

    def cli(self, *args, expected=0):
        process = subprocess.run([*CLI, str(self.context), *map(str, args), "--json"],
                                 cwd=self.work, capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, expected, (process.stdout, process.stderr))
        return json.loads(process.stdout)

    def test_intersection_only_failure_and_equality(self):
        options = ("--max-bytes", 2000, "--max-dir-bytes", "build=1000",
                   "--max-extension-bytes", ".bin=1000")
        report = self.cli(*options, FLAG, "build", ".bin=999", expected=1)
        budget = report["baseline"]["budget"]
        self.assertTrue(budget["total_passed"])
        self.assertTrue(budget["directories"][0]["passed"])
        self.assertTrue(budget["extensions"][0]["passed"])
        self.assertFalse(budget["passed"])
        self.assertEqual(budget["directory_extensions"], [{
            "path": "build", "extension": ".bin", "included_payload_bytes": 1000,
            "included_payload_files": 1, "max_bytes": 999, "passed": False}])
        self.cli(*options, FLAG, "build", ".bin=1000")
        self.assertEqual(self.cli()["baseline"]["budget"]["directory_extensions"], [])

    def test_exact_subtree_suffix_logical_bytes_and_untruncated_limits(self):
        nested = self.write("build/nested/a.bin", 7)
        self.write("build/zero.bin", 0)
        self.write("build/.bin", 2)
        self.write("build/end.", 3)
        self.write("build/c.BIN", 5)
        self.write("build/archive.tar.bin", 4)
        self.write("build/cache.bin/child.txt", 13)
        self.write("other/out.bin", 29)
        self.write("building/wrong.bin", 31)
        self.write("build/excluded.bin", 41)
        self.write("build/control.bin", 12)
        (self.context / ".dockerignore").write_text("build/excluded.bin\n")
        (self.context / "build/link.bin").symlink_to("nonexistent-outside")
        os.link(nested, self.context / "build/alias.bin")
        with (self.context / "build/sparse.bin").open("wb") as stream:
            stream.truncate(4096)
        options = ["--dockerfile", "build/control.bin", "--top", 0]
        for directory, suffix, limit in (("build/nested", ".bin", 7), ("build", "<none>", 5),
                                         ("build", ".bin", 5114), ("build", ".absent", 0),
                                         ("build", ".BIN", 5)):
            options.extend((FLAG, directory, f"{suffix}={limit}"))
        side = self.cli(*options)["baseline"]
        self.assertEqual([(r["path"], r["extension"], r["included_payload_bytes"],
                           r["included_payload_files"]) for r in side["budget"]["directory_extensions"]],
                         [("build", ".BIN", 5, 1), ("build", ".absent", 0, 0),
                          ("build", ".bin", 5114, 6), ("build", "<none>", 5, 2),
                          ("build/nested", ".bin", 7, 1)])
        self.assertEqual(side["ignore_suggestions"], [])
        self.assertTrue(all(not rows for rows in side["contributions"].values()))

    def test_invalid_pairs_fail_without_partial_results(self):
        (self.context / "link").symlink_to("build", target_is_directory=True)
        invalid = [(directory, ".bin=0") for directory in (
            "", ".", "/build", "../build", "build/", "build//x", "build/./x", "missing",
            "build/b.bin", "link")]
        invalid += [("build", value) for value in (
            "bin=1", ".tar.gz=1", ".=1", ".bin/a=0", ".bin=-1", ".bin=+1",
            ".bin=1.5", ".bin=1KB", ".bin=", ".bin", ".bin=١", ".bin=" + "9" * 5000)]
        for directory, value in invalid:
            with self.subTest(directory=directory, value=value[:50]):
                report = self.cli(FLAG, directory, value, expected=2)
                self.assertEqual(set(report), {"schema_version", "status", "error"})
                self.assertEqual(report["status"], "error")
        self.cli(FLAG, "build", ".bin=1", FLAG, "build", ".bin=2", expected=2)
        process = subprocess.run([*CLI, str(self.context), FLAG, "build"],
                                 capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout, "")
        self.assertIn("expected 2 arguments", process.stderr)

    def test_library_pair_validation_and_cli_equivalence(self):
        report = analyze(self.context, max_dir_extension_bytes={("build", ".bin"): 1000})
        self.assertEqual(report, self.cli(FLAG, "build", ".bin=1000"))
        for limits in ([], {"build": 1}, {(1, ".bin"): 1}, {("build", 1): 1},
                       {("build", ".bin", "x"): 1}, {("build", ".bin"): True},
                       {("build", ".bin"): -1}, {("build", ".bin"): 1.5},
                       {("build\x00", ".bin"): 1}, {("build", ".tar.gz"): 1}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                analyze(self.context, max_dir_extension_bytes=limits)

    def test_candidate_and_snapshot_modes_keep_selected_side(self):
        candidate = self.work / "candidate.ignore"
        candidate.write_text("build\n")
        report = self.cli("--candidate-ignore", candidate, FLAG, "build", ".bin=0")
        self.assertFalse(report["baseline"]["budget"]["passed"])
        self.assertTrue(report["candidate"]["budget"]["passed"])
        self.assertEqual(report["candidate"]["ignore_suggestions"], [])
        saved = self.work / "baseline.json"
        self.cli("--save-snapshot", saved, FLAG, "build", ".bin=999", expected=1)
        self.write("build/b.bin", 1001)
        current = self.cli("--baseline-snapshot", saved, FLAG, "build", ".bin=1000", expected=1)
        self.assertEqual(current["snapshot_comparison"]["included_payload_bytes_delta"], 1)
        self.assertEqual(current["baseline"]["budget"]["directory_extensions"][0]["included_payload_bytes"], 1001)

    def test_scoped_advice_replays_all_impact_including_collateral(self):
        self.write("build/deep/c.bin", 7)
        self.write("build/cache.bin/input.py", 13)
        self.write("build/cache.bin/Dockerfile", 12)
        self.write("build/keep.txt", 11)
        self.write("elsewhere/out.bin", 29)
        (self.context / "build/cache.bin/link").symlink_to("outside")
        rules = self.context / ".dockerignore"
        rules.write_text("build\n!build/b.bin\n!build/deep/c.bin\n!build/cache.bin/**\n!build/keep.txt\n")
        original = {p.relative_to(self.context).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.context.rglob("*") if p.is_file() and not p.is_symlink()}
        options = ("--dockerfile", "build/cache.bin/Dockerfile", FLAG, "build", ".bin=1006", "--top", 30)
        report = self.cli(*options, expected=1)
        side = report["baseline"]
        advice = side["ignore_suggestions"]
        scoped = next(r for r in advice if r["append_pattern"] == "build/**/*.bin")
        self.assertEqual(scoped["scope"], {"kind": "directory_extension_pattern", "selector": "build", "extension": ".bin"})
        self.assertEqual(scoped["logical_payload_bytes_delta"], -1020)
        self.assertEqual(scoped["included_payload_files_delta"], -3)
        impacts = {row["path"]: row for row in scoped["changed_entries"]}
        self.assertEqual(impacts["build/cache.bin/input.py"]["logical_payload_bytes_delta"], -13)
        self.assertEqual(impacts["build/cache.bin/Dockerfile"]["role"], "dockerfile")
        self.assertEqual(impacts["build/cache.bin/Dockerfile"]["logical_payload_bytes_delta"], 0)
        self.assertIn("build/cache.bin/link", impacts)
        self.assertNotIn("elsewhere/out.bin", impacts)
        self.assertNotIn("build/keep.txt", impacts)
        candidate = self.work / "appended.ignore"
        for suggestion in advice:
            candidate.write_text(rules.read_text() + "\n" + suggestion["append_pattern"] + "\n")
            replay = self.cli(*options, "--candidate-ignore", candidate,
                              expected=0 if suggestion["budget_after"]["passed"] else 1)
            self.assertEqual(replay["candidate"]["budget"], suggestion["budget_after"])
            self.assertEqual(replay["comparison"]["included_payload_bytes_delta"], suggestion["logical_payload_bytes_delta"])
            self.assertEqual(replay["comparison"]["included_payload_files_delta"], suggestion["included_payload_files_delta"])
            self.assertEqual([r["path"] for r in replay["comparison"]["changes"]],
                             [r["path"] for r in suggestion["changed_entries"]])
        self.assertEqual(original, {p.relative_to(self.context).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in self.context.rglob("*") if p.is_file() and not p.is_symlink()})

    def test_no_extension_and_below_top_cutoff_still_have_relevant_advice(self):
        self.write("tiny/plain", 4)
        self.write("tiny/.hidden", 2)
        self.write("tiny/keep.bin", 3)
        report = self.cli(FLAG, "tiny", "<none>=5", "--top", 1, expected=1)
        suggestion, = report["baseline"]["ignore_suggestions"]
        self.assertEqual(suggestion["append_pattern"], "tiny/plain")
        self.assertEqual(suggestion["logical_payload_bytes_delta"], -4)
        self.assertTrue(suggestion["budget_after"]["passed"])
        candidate = self.work / "candidate.ignore"
        candidate.write_text(suggestion["append_pattern"] + "\n")
        self.assertEqual(self.cli(FLAG, "tiny", "<none>=5", "--candidate-ignore", candidate)["candidate"]["budget"],
                         suggestion["budget_after"])

    def test_scoped_literals_and_text_escape_untrusted_selectors(self):
        directory = "目录=a!*[x]"
        self.write(directory + "/a.b?", 8)
        report = self.cli(FLAG, directory, ".b?=7", "--top", 30, expected=1)
        scoped = next(r for r in report["baseline"]["ignore_suggestions"]
                      if r["scope"]["kind"] == "directory_extension_pattern")
        candidate = self.work / "candidate.ignore"
        candidate.write_text(scoped["append_pattern"] + "\n")
        self.assertEqual(self.cli(FLAG, directory, ".b?=7", "--candidate-ignore", candidate)["candidate"]["budget"],
                         scoped["budget_after"])
        unsafe = "line\n\x1b[31m"
        self.write(unsafe + "/a.bin", 1)
        process = subprocess.run([*CLI, str(self.context), FLAG, unsafe, ".bin=0"],
                                 capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 1)
        self.assertIn("Budget directory_extensions", process.stdout)
        self.assertNotIn("\x1b", process.stdout)
        self.assertIn("extension '.bin'", process.stdout)


if __name__ == "__main__":
    unittest.main()
