import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from stackwrench.context_budget.core import analyze

from .support import CLI, ENV, ROOT

EXAMPLES = ROOT / "examples" / "context_budget"


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()

    def write(self, path, data):
        target = self.context / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
        return target

    def run_cli(self, *args, expected=0, json_output=True):
        argv = [*CLI, str(self.context), *map(str, args)]
        if json_output:
            argv.append("--json")
        process = subprocess.run(argv, cwd=self.work, text=True, capture_output=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, expected, (argv, process.stdout, process.stderr))
        if json_output:
            return json.loads(process.stdout)
        return process

    def test_exact_budget_pass_and_fail(self):
        self.write("a", b"a" * 20)
        self.write("out/b", b"b" * 1000)
        report = self.run_cli("--max-bytes", 1020)
        self.assertEqual(report["baseline"]["summary"]["included_payload_bytes"], 1020)
        self.assertTrue(report["baseline"]["budget"]["passed"])
        report = self.run_cli("--max-bytes", 1019, expected=1)
        self.assertEqual(report["status"], "budget_exceeded")
        self.assertFalse(report["baseline"]["budget"]["passed"])

    def test_candidate_compare_maps_bytes_and_provenance_without_mutation(self):
        self.write("src/a.py", b"a" * 20)
        self.write("build/b.bin", b"b" * 1000)
        baseline = self.write(".dockerignore", "# baseline\n")
        self.write("Dockerfile", "FROM scratch\n")
        candidate = self.work / "candidate.ignore"
        candidate.write_text("# synthetic only\nbuild/**\n")
        original = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.work.rglob("*") if path.is_file()}
        report = self.run_cli("--ignore", baseline, "--candidate-ignore", candidate, "--max-bytes", 20)
        self.assertEqual(report["baseline"]["summary"]["included_payload_bytes"], 1020)
        self.assertEqual(report["candidate"]["summary"]["included_payload_bytes"], 20)
        self.assertFalse(report["baseline"]["budget"]["passed"])
        self.assertTrue(report["candidate"]["budget"]["passed"])
        self.assertEqual(report["comparison"]["included_payload_bytes_delta"], -1000)
        self.assertEqual(report["comparison"]["included_payload_files_delta"], -1)
        change, = report["comparison"]["changes"]
        self.assertEqual(change["path"], "build/b.bin")
        self.assertEqual(change["candidate_last_rule"]["line"], 2)
        self.assertEqual(change["candidate_last_rule"]["source"], str(candidate))
        self.assertEqual(report["budget_applies_to"], "candidate")
        self.assertEqual(original, {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in self.work.rglob("*") if path.is_file()})
        self.run_cli("--candidate-ignore", candidate, "--max-bytes", 19, expected=1)

    def test_compare_alias(self):
        candidate = self.work / "candidate.ignore"
        candidate.write_text("**\n")
        self.write("a", "a")
        process = subprocess.run([*CLI, "compare", str(self.context), "--candidate-ignore", str(candidate), "--json"],
                                 capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["candidate"]["summary"]["included_payload_bytes"], 0)

    def test_snapshot_inventory_changes_and_logical_bytes(self):
        self.write("keep", b"1234")
        self.write("gone", b"123")
        self.write("ignored", b"1234567")
        self.write(".dockerignore", "ignored\n")
        snapshot = self.work / "baseline.json"
        original = {path.relative_to(self.context).as_posix(): path.read_bytes()
                    for path in self.context.rglob("*") if path.is_file()}
        created = self.run_cli("--save-snapshot", snapshot)
        self.assertEqual(created["snapshot_written"], str(snapshot))
        self.assertEqual(original, {path.relative_to(self.context).as_posix(): path.read_bytes()
                                    for path in self.context.rglob("*") if path.is_file()})
        saved = snapshot.read_bytes()
        same = self.run_cli("--baseline-snapshot", snapshot)["snapshot_comparison"]
        self.assertEqual(same["changes"], [])
        self.assertEqual(same["included_payload_bytes_delta"], 0)
        self.assertEqual(same["included_payload_files_delta"], 0)
        self.write("keep", b"123456789")
        (self.context / "gone").unlink()
        self.write(".dockerignore", "# now include ignored\n")
        with (self.context / "large").open("wb") as stream:
            stream.truncate(1048576)
        report = self.run_cli("--baseline-snapshot", snapshot, "--max-bytes", 1048592)
        comparison = report["snapshot_comparison"]
        self.assertEqual(comparison["included_payload_bytes_delta"], 1048585)
        self.assertEqual(comparison["included_payload_files_delta"], 1)
        self.assertEqual(comparison["baseline"]["included_payload_bytes"], 7)
        self.assertEqual(comparison["current"]["included_payload_bytes"], 1048592)
        changes = {row["path"]: row for row in comparison["changes"]}
        self.assertEqual(list(changes), [".dockerignore", "gone", "ignored", "keep", "large"])
        self.assertEqual(changes["gone"]["reasons"], ["removed"])
        self.assertIsNone(changes["gone"]["after"])
        self.assertEqual(changes["large"]["reasons"], ["added"])
        self.assertEqual(changes["large"]["included_payload_bytes_delta"], 1048576)
        self.assertEqual(changes["keep"]["reasons"], ["size_changed"])
        self.assertEqual(changes["keep"]["included_payload_bytes_delta"], 5)
        self.assertEqual(changes["ignored"]["before"]["last_rule"]["source"], str(self.context / ".dockerignore"))
        self.assertEqual(changes["ignored"]["before"]["last_rule"]["line"], 1)
        self.assertIsNone(changes["ignored"]["after"]["last_rule"])
        self.assertEqual(snapshot.read_bytes(), saved)
        self.run_cli("--baseline-snapshot", snapshot, "--max-bytes", 1048591, expected=1)
        text = self.run_cli("--baseline-snapshot", snapshot, json_output=False).stdout
        self.assertIn("Current minus saved snapshot: +1048585 logical payload bytes", text)
        self.assertIn(".dockerignore:1", text)
        self.assertIn("default include", text)

    def test_snapshot_creation_and_invalid_baselines_fail_closed(self):
        self.write("a", "1234")
        snapshot = self.work / "baseline.json"
        self.run_cli("--save-snapshot", snapshot)
        original = snapshot.read_bytes()
        self.assertEqual(self.run_cli("--save-snapshot", snapshot, expected=2)["status"], "error")
        self.assertEqual(snapshot.read_bytes(), original)
        inside = self.context / "baseline.json"
        self.assertIn("outside", self.run_cli("--save-snapshot", inside, expected=2)["error"])
        self.assertFalse(inside.exists())
        missing = self.work / "missing.json"
        report = self.run_cli("--baseline-snapshot", missing, "--max-bytes", 100000, expected=2)
        self.assertEqual(report["status"], "error")
        self.assertNotIn("baseline", report)
        self.assertFalse(missing.exists())
        snapshot.write_text('{"schema_version":999}')
        self.assertEqual(self.run_cli("--baseline-snapshot", snapshot, expected=2)["status"], "error")

    def test_snapshot_retains_changed_rule_provenance_without_byte_change(self):
        self.write("out/a", "12")
        self.write(".dockerignore", "out\n!out/a\n")
        snapshot = self.work / "baseline.json"
        self.run_cli("--save-snapshot", snapshot)
        self.write(".dockerignore", "out\n# shifted\n!out/a\n")
        comparison = self.run_cli("--baseline-snapshot", snapshot)["snapshot_comparison"]
        self.assertEqual(comparison["included_payload_bytes_delta"], 0)
        change = next(row for row in comparison["changes"] if row["path"] == "out/a")
        self.assertEqual(change["reasons"], ["rule_changed"])
        self.assertEqual(change["before"]["last_rule"]["line"], 2)
        self.assertEqual(change["after"]["last_rule"]["line"], 3)

    def test_snapshot_incomplete_scan_never_saves_but_exceeded_budget_does(self):
        self.write("a", "1234")
        snapshot = self.work / "baseline.json"
        self.run_cli("--save-snapshot", snapshot, "--max-bytes", 3, expected=1)
        self.assertEqual(self.run_cli("--baseline-snapshot", snapshot)["snapshot_comparison"]["changes"], [])
        self.write(".dockerignore", "**\n")
        os.mkfifo(self.context / "pipe")
        incomplete = self.work / "incomplete.json"
        result = self.run_cli("--save-snapshot", incomplete, expected=2)
        self.assertNotIn("baseline", result)
        self.assertFalse(incomplete.exists())

    def test_specific_ignore_precedes_root_not_merged(self):
        self.write("Dockerfile", "FROM scratch\n")
        self.write(".dockerignore", "a\n")
        self.write("Dockerfile.dockerignore", "b\n")
        self.write("a", b"aaa")
        self.write("b", b"bbbbb")
        report = self.run_cli()
        self.assertEqual(report["ignore_selection"], "dockerfile-specific")
        entries = {row["path"]: row for row in report["baseline"]["entries"]}
        self.assertTrue(entries["a"]["included_by_rules"])
        self.assertFalse(entries["b"]["included_by_rules"])
        self.assertEqual(entries[".dockerignore"]["role"], "payload")
        self.assertEqual(entries["Dockerfile.dockerignore"]["role"], "ignore")
        self.assertEqual(report["baseline"]["summary"]["included_payload_bytes"], 5)

    def test_empty_specific_ignore_still_takes_precedence(self):
        self.write(".dockerignore", "**\n")
        self.write("Dockerfile.dockerignore", "")
        self.write("a", "payload")
        report = self.run_cli()
        self.assertEqual(report["ignore_selection"], "dockerfile-specific")
        self.assertEqual(report["baseline"]["summary"]["included_payload_bytes"], 10)

    def test_nested_dockerfile_and_control_visibility(self):
        self.write("docker/dev.Dockerfile", "FROM scratch\n")
        self.write("docker/dev.Dockerfile.dockerignore", "docker\n!docker/source\n")
        self.write("docker/source", b"1234")
        report = self.run_cli("--dockerfile", "docker/dev.Dockerfile", "--max-bytes", 4)
        summary = report["baseline"]["summary"]
        self.assertEqual(summary["included_payload_bytes"], 4)
        self.assertEqual(summary["control_files"], 2)
        self.assertGreater(summary["control_bytes_on_disk"], 0)
        self.assertTrue(all(not row["included_by_rules"] for row in report["baseline"]["control_files"]))

    def test_explicit_ignore_overrides_automatic_selection(self):
        self.write("Dockerfile.dockerignore", "[bad\n")
        override = self.work / "override"
        override.write_text("**\n")
        report = self.run_cli("--ignore", override)
        self.assertEqual(report["ignore_selection"], "explicit")
        self.assertEqual(report["baseline"]["summary"]["included_payload_bytes"], 0)

    def test_ignored_parent_reinclusion_e2e(self):
        self.write(".dockerignore", "out\n!out/sub/keep\n")
        self.write("out/sub/keep", b"123")
        self.write("out/drop", b"12345")
        report = self.run_cli("--max-bytes", 3)
        self.assertEqual(report["baseline"]["summary"]["included_payload_files"], 1)
        self.assertEqual(report["baseline"]["largest_included_files"][0]["path"], "out/sub/keep")

    def test_symlink_escape_dangling_and_directory_no_traversal(self):
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"secret" * 100)
        (self.context / "escape").symlink_to(outside, target_is_directory=True)
        (self.context / "dangling").symlink_to("/no-such-target-dcb")
        (self.context / "file-link").symlink_to(outside / "secret")
        report = self.run_cli("--max-bytes", 0)
        self.assertEqual(report["baseline"]["summary"]["included_symlinks"], 3)
        self.assertEqual(report["baseline"]["summary"]["included_payload_bytes"], 0)
        self.assertEqual([row["path"] for row in report["baseline"]["entries"]], ["dangling", "escape", "file-link"])
        self.assertEqual(report["baseline"]["entries"][1]["link_target"], str(outside))

    def test_excluded_symlink_is_separate(self):
        self.write(".dockerignore", "link\n")
        (self.context / "link").symlink_to("nonexistent")
        summary = self.run_cli()["baseline"]["summary"]
        self.assertEqual(summary["excluded_symlinks"], 1)
        self.assertEqual(summary["excluded_payload_files"], 0)

    def test_symlink_root_is_rejected(self):
        alias = self.work / "alias"
        alias.symlink_to(self.context, target_is_directory=True)
        self.context = alias
        self.assertEqual(self.run_cli(expected=2)["status"], "error")

    def test_symlink_ignore_is_rejected(self):
        other = self.work / "other"
        other.write_text("**\n")
        (self.context / ".dockerignore").symlink_to(other)
        self.assertEqual(self.run_cli(expected=2)["status"], "error")

    def test_symlink_dockerfile_is_rejected(self):
        self.write("original", "FROM scratch\n")
        (self.context / "Dockerfile").symlink_to("original")
        self.assertIn("regular file", self.run_cli(expected=2)["error"])

    def test_symlink_candidate_parent_is_rejected(self):
        original = self.work / "rule-directory"
        original.mkdir()
        (original / "rules").write_text("**\n")
        alias = self.work / "rule-alias"
        alias.symlink_to(original, target_is_directory=True)
        self.assertEqual(self.run_cli("--candidate-ignore", alias / "rules", expected=2)["status"], "error")

    def test_fifo_explicit_error_even_if_ignored(self):
        self.write(".dockerignore", "**\n")
        os.mkfifo(self.context / "pipe")
        report = self.run_cli("--max-bytes", 0, expected=2)
        self.assertIn("unsupported filesystem entry", report["error"])
        self.assertNotIn("baseline", report)

    def test_socket_explicit_error(self):
        with socket.socket(socket.AF_UNIX) as server_socket:
            server_socket.bind(str(self.context / "socket"))
            self.assertIn("unsupported filesystem entry", self.run_cli(expected=2)["error"])

    def test_unreadable_file_prevents_budget_pass(self):
        path = self.write("private", "data")
        path.chmod(0)
        try:
            report = self.run_cli("--max-bytes", 100000, expected=2)
            self.assertIn("unreadable", report["error"])
            self.assertNotIn("baseline", report)
        finally:
            path.chmod(0o600)

    def test_unreadable_ignored_directory_prevents_pass(self):
        self.write(".dockerignore", "hidden\n")
        hidden = self.context / "hidden"
        hidden.mkdir()
        hidden.chmod(0)
        try:
            self.assertEqual(self.run_cli("--max-bytes", 0, expected=2)["status"], "error")
        finally:
            hidden.chmod(0o700)

    def test_unreadable_candidate(self):
        candidate = self.work / "candidate"
        candidate.write_text("**\n")
        candidate.chmod(0)
        try:
            self.assertEqual(self.run_cli("--candidate-ignore", candidate, expected=2)["status"], "error")
        finally:
            candidate.chmod(0o600)

    def test_invalid_pattern_in_candidate_is_diagnostic(self):
        candidate = self.work / "candidate"
        candidate.write_text("# line 1\n[bad\n")
        report = self.run_cli("--candidate-ignore", candidate, expected=2)
        self.assertIn(str(candidate) + ":2:", report["error"])

    def test_invalid_utf8_ignore(self):
        self.write(".dockerignore", b"\xff\n")
        self.assertEqual(self.run_cli(expected=2)["status"], "error")

    def test_missing_candidate_and_context(self):
        self.run_cli("--candidate-ignore", self.work / "missing", expected=2)
        self.context = self.work / "missing-context"
        self.run_cli(expected=2)

    def test_dockerfile_outside_and_missing_explicit_rejected(self):
        for value in ("../outside", "/absolute", "missing/file", "."):
            with self.subTest(value=value):
                self.run_cli("--dockerfile", value, expected=2)

    def test_deterministic_listing_and_largest_tie_break(self):
        self.write("z", b"zz")
        self.write("a/x", b"11")
        self.write("b", b"bbbb")
        first = self.run_cli("--largest", 2)
        second = self.run_cli("--largest", 2)
        self.assertEqual(first, second)
        self.assertEqual([row["path"] for row in first["baseline"]["entries"]], ["a", "a/x", "b", "z"])
        self.assertEqual([row["path"] for row in first["baseline"]["largest_included_files"]], ["b", "a/x"])

    def test_sparse_and_hardlinked_files_count_logical_bytes_per_path(self):
        sparse = self.context / "sparse"
        with sparse.open("wb") as stream:
            stream.truncate(1000000)
        os.link(sparse, self.context / "hardlink")
        report = self.run_cli("--max-bytes", 2000000)
        self.assertEqual(report["baseline"]["summary"]["included_payload_bytes"], 2000000)

    def test_text_output_escapes_unusual_filenames(self):
        self.write("line\n\x1b[31m", b"abc")
        process = self.run_cli(json_output=False)
        self.assertIn("'line\\n\\x1b[31m'", process.stdout)
        self.assertNotIn("\x1b", process.stdout)

    def test_cli_argument_errors(self):
        for args in (("--max-bytes", "-1"), ("--max-bytes", "1KB"), ("--largest", "-2")):
            with self.subTest(args=args):
                process = self.run_cli(*args, expected=2, json_output=False)
                self.assertIn("nonnegative integer", process.stderr)
        for args in (("--save-snapshot", "a", "--baseline-snapshot", "b"),
                     ("--save-snapshot", "a", "--candidate-ignore", "b"),
                     ("--baseline-snapshot", "a", "--candidate-ignore", "b")):
            with self.subTest(args=args):
                self.run_cli(*args, expected=2, json_output=False)

    def test_library_report_matches_cli(self):
        self.write("a", "1234")
        self.assertEqual(analyze(self.context, max_bytes=4), self.run_cli("--max-bytes", 4))

    def test_examples_acceptance_and_expected_failure(self):
        process = subprocess.run([*CLI, str(EXAMPLES / "context"), "--json"],
                                 capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["baseline"]["summary"]["included_payload_bytes"], 1020)
        process = subprocess.run([*CLI, str(EXAMPLES / "context"), "--candidate-ignore", str(EXAMPLES / "lean.dockerignore"), "--max-bytes", "100000"],
                                 capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("candidate: 20 bytes, 1 files", process.stdout)
        process = subprocess.run([*CLI, str(EXAMPLES / "context"), "--candidate-ignore", str(EXAMPLES / "invalid.dockerignore"), "--json"],
                                 capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["status"], "error")

    def test_replayable_experiment(self):
        # Inline replay of the committed synthetic comparison (formerly a standalone script).
        process = subprocess.run([
            *CLI, str(EXAMPLES / "context"),
            "--candidate-ignore", str(EXAMPLES / "lean.dockerignore"),
            "--max-bytes", "100000", "--json",
        ], capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        change, = report["comparison"]["changes"]
        result = {
            "model": "static logical regular-file payload; not transfer, compression, image size or timing",
            "baseline": {"bytes": report["baseline"]["summary"]["included_payload_bytes"],
                         "files": report["baseline"]["summary"]["included_payload_files"]},
            "candidate": {"bytes": report["candidate"]["summary"]["included_payload_bytes"],
                          "files": report["candidate"]["summary"]["included_payload_files"]},
            "delta": {"bytes": report["comparison"]["included_payload_bytes_delta"],
                      "files": report["comparison"]["included_payload_files_delta"]},
            "excluded": {"path": change["path"], "bytes": change["logical_bytes"],
                         "candidate_rule_line": change["candidate_last_rule"]["line"],
                         "candidate_rule": change["candidate_last_rule"]["raw"]},
        }
        self.assertEqual(result, json.loads((EXAMPLES / "experiment.expected.json").read_text()))
        self.assertEqual(result["delta"], {"bytes": -1000, "files": -1})

    def test_module_entry_point_e2e(self):
        self.write("a", "12")
        process = subprocess.run([*CLI, str(self.context), "--json", "--max-bytes", "2"],
                                 cwd=self.work, capture_output=True, text=True, timeout=10, env=ENV)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["baseline"]["summary"]["included_payload_bytes"], 2)


if __name__ == "__main__":
    unittest.main()
