"""Saved comparison contract: CURRENT minus BASELINE, with no live inputs."""

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


class SavedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        for name, data in {"keep": b"1234", "gone": b"123", "ignored": b"1234567",
                           "blocked": b"12345", ".dockerignore": b"ignored\nblocked\n"}.items():
            (self.context / name).write_bytes(data)
        self.baseline = self.work / "before.json"
        self.current = self.work / "after.json"
        self.before = from_report(analyze(self.context, include_link_targets=False))
        write_snapshot(self.baseline, self.before)
        (self.context / "keep").write_bytes(b"123456789")
        (self.context / "gone").unlink()
        (self.context / "empty").touch()
        (self.context / ".dockerignore").write_text("# shifted\n# include ignored\nblocked\n")
        self.after = from_report(analyze(self.context, include_link_targets=False))
        write_snapshot(self.current, self.after)
        shutil.rmtree(self.context)

    def run_comparison(self, baseline=None, current=None, *, expected=0, json_output=True, max_bytes=None):
        args = ["compare-snapshots", str(baseline or self.baseline), str(current or self.current)]
        if max_bytes is not None:
            args.extend(["--max-bytes", str(max_bytes)])
        if json_output:
            args.append("--json")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), expected)
        return json.loads(output.getvalue()) if json_output else output.getvalue()

    def test_current_saved_budget_equality_and_exceedance_without_live_inputs(self):
        originals = [path.read_bytes() for path in (self.baseline, self.current)]
        unbudgeted = self.run_comparison()
        real_open = os.open

        def saved_open(path, *args, **kwargs):
            self.assertNotIn(Path(path).name, {"context", "Dockerfile", ".dockerignore",
                                              "keep", "gone", "empty", "ignored", "blocked"})
            return real_open(path, *args, **kwargs)

        with patch("stackwrench.context_budget.cli.analyze", side_effect=AssertionError("no live analysis")), patch(
            "os.listdir", side_effect=AssertionError("no context inventory")
        ), patch("os.readlink", side_effect=AssertionError("no symlink text")), patch(
            "stackwrench.context_budget.core.read_rules_file", side_effect=AssertionError("no live rules")
        ), patch("os.open", side_effect=saved_open):
            for threshold, code in ((16, 0), (15, 1), (0, 1), (10**30, 0)):
                with self.subTest(threshold=threshold):
                    report = self.run_comparison(max_bytes=threshold, expected=code)
                    self.assertEqual(report, {
                        **unbudgeted, "status": "ok" if code == 0 else "budget_exceeded",
                        "current_saved_budget": {"max_bytes": threshold, "included_payload_bytes": 16,
                                                 "passed": code == 0}})
        self.assertEqual([path.read_bytes() for path in (self.baseline, self.current)], originals)

    def test_budget_uses_current_total_even_when_baseline_is_larger_or_delta_is_negative(self):
        larger = copy.deepcopy(self.before)
        next(row for row in larger["entries"] if row["path"] == "keep")["logical_bytes"] = 40
        self.baseline.write_text(json.dumps(larger))
        originals = [path.read_bytes() for path in (self.baseline, self.current)]
        unbudgeted = self.run_comparison()
        self.assertEqual(unbudgeted["snapshot_comparison"]["baseline"]["included_payload_bytes"], 43)
        self.assertEqual(unbudgeted["snapshot_comparison"]["included_payload_bytes_delta"], -27)
        for threshold, code in ((16, 0), (15, 1)):
            report = self.run_comparison(max_bytes=threshold, expected=code)
            self.assertEqual(report["snapshot_comparison"], unbudgeted["snapshot_comparison"])
            self.assertEqual(report["current_saved_budget"], {
                "max_bytes": threshold, "included_payload_bytes": 16, "passed": code == 0})
            text = self.run_comparison(max_bytes=threshold, expected=code, json_output=False)
            self.assertIn(f"Current saved budget: 16 / {threshold} bytes; " +
                          ("PASS" if code == 0 else "EXCEEDED"), text)
        self.assertEqual([path.read_bytes() for path in (self.baseline, self.current)], originals)

    def test_empty_current_payload_passes_zero_budget(self):
        empty = {**self.after, "entries": []}
        self.current.write_text(json.dumps(empty))
        report = self.run_comparison(max_bytes=0)
        self.assertEqual(report["current_saved_budget"], {
            "max_bytes": 0, "included_payload_bytes": 0, "passed": True})
        self.assertEqual(report["status"], "ok")

    def test_malformed_negative_and_missing_threshold_are_parser_errors_without_output(self):
        originals = [path.read_bytes() for path in (self.baseline, self.current)]
        for extra in (("--max-bytes",), *(("--max-bytes", value) for value in
                       ("-1", "1.5", "16KB", "no", "", "9" * 5000))):
            with self.subTest(extra=extra), contextlib.redirect_stdout(io.StringIO()) as output, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(["compare-snapshots", str(self.baseline), str(self.current), "--json", *extra])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertIn("error:", error.getvalue())
        self.assertEqual([path.read_bytes() for path in (self.baseline, self.current)], originals)

    def test_saved_comparison_never_scans_and_retains_logical_and_rule_deltas(self):
        originals = [path.read_bytes() for path in (self.baseline, self.current)]
        with patch("stackwrench.context_budget.cli.analyze", side_effect=AssertionError("no live analysis")), patch(
            "os.listdir", side_effect=AssertionError("no context inventory")
        ), patch("os.readlink", side_effect=AssertionError("no symlink text")):
            report = self.run_comparison()
        self.assertEqual(set(report), {"schema_version", "status", "model", "snapshot_comparison"})
        self.assertEqual((report["schema_version"], report["status"], report["model"]), (1, "ok", MODEL))
        result = report["snapshot_comparison"]
        self.assertEqual(result["baseline_snapshot"], str(self.baseline))
        self.assertEqual(result["current_snapshot"], str(self.current))
        self.assertEqual(result["baseline"]["included_payload_bytes"], 7)
        self.assertEqual(result["current"]["included_payload_bytes"], 16)
        self.assertEqual(result["included_payload_bytes_delta"], 9)
        self.assertEqual(result["included_payload_files_delta"], 1)
        changes = {row["path"]: row for row in result["changes"]}
        self.assertEqual(list(changes), [".dockerignore", "blocked", "empty", "gone", "ignored", "keep"])
        self.assertEqual(changes["blocked"]["reasons"], ["rule_changed"])
        self.assertEqual(changes["blocked"]["before"]["last_rule"]["line"], 2)
        self.assertEqual(changes["blocked"]["after"]["last_rule"]["line"], 3)
        self.assertEqual(changes["blocked"]["included_payload_bytes_delta"], 0)
        self.assertEqual(changes["empty"]["included_payload_files_delta"], 1)
        self.assertEqual(changes["gone"]["reasons"], ["removed"])
        self.assertIsNone(changes["gone"]["after"])
        self.assertEqual(changes["ignored"]["before"]["last_rule"]["source"], str(self.context / ".dockerignore"))
        self.assertIsNone(changes["ignored"]["after"]["last_rule"])
        self.assertEqual(sum(row["included_payload_bytes_delta"] for row in changes.values()), 9)
        self.assertEqual([path.read_bytes() for path in (self.baseline, self.current)], originals)

    def test_identical_reverse_and_text_output(self):
        same = self.run_comparison(current=self.baseline)["snapshot_comparison"]
        self.assertEqual(same["changes"], [])
        self.assertEqual(same["included_payload_bytes_delta"], 0)
        self.assertEqual(same["included_payload_files_delta"], 0)
        reverse = self.run_comparison(self.current, self.baseline)["snapshot_comparison"]
        self.assertEqual(reverse["included_payload_bytes_delta"], -9)
        self.assertEqual(reverse["included_payload_files_delta"], -1)
        text = self.run_comparison(json_output=False)
        self.assertIn("Current minus baseline snapshot: +9 logical payload bytes, +1 files", text)
        self.assertIn(".dockerignore:2", text)
        self.assertIn(".dockerignore:3", text)
        self.assertIn("default include", text)
        self.assertIn(str(self.baseline), text)
        self.assertIn(str(self.current), text)
        self.assertIn("No inventory differences.", self.run_comparison(current=self.baseline, json_output=False))

    def test_both_inputs_are_strictly_validated_without_partial_results(self):
        bad = self.work / "bad.json"
        invalid = [None, {}, {**self.after, "schema_version": 2},
                   {**self.after, "entries": self.after["entries"] * 2}]
        malformed = copy.deepcopy(self.after)
        malformed["entries"][-1]["logical_bytes"] = True
        invalid.append(malformed)
        for value in invalid:
            bad.write_text(json.dumps(value))
            for before, after in ((bad, self.current), (self.baseline, bad)):
                with self.subTest(value=value, before=before):
                    for limit in (None, 0, 16):
                        result = self.run_comparison(before, after, expected=2, max_bytes=limit)
                        self.assertEqual(set(result), {"schema_version", "status", "error"})
                        self.assertEqual(result["status"], "error")
        for text in ('{"format":1,"format":1}', '{', '\ufeff{}'):
            bad.write_text(text)
            result = self.run_comparison(current=bad, expected=2, max_bytes=16)
            self.assertEqual(set(result), {"schema_version", "status", "error"})

    def test_incompatible_recorded_context_or_dockerfile_is_an_error(self):
        for field, value in (("context", str(self.work / "other")), ("dockerfile", "Otherfile")):
            bad = copy.deepcopy(self.after)
            bad[field] = value
            if field == "context":
                bad["rules_source"] = value + "/.dockerignore"
                for row in bad["entries"]:
                    if row["last_rule"] is not None:
                        row["last_rule"]["source"] = bad["rules_source"]
            self.current.write_text(json.dumps(bad))
            with self.subTest(field=field):
                for limit in (None, 0, 16):
                    result = self.run_comparison(expected=2, max_bytes=limit)
                    self.assertIn(field + " differs between inventories", result["error"])
                    self.assertEqual(set(result), {"schema_version", "status", "error"})

    def test_missing_unreadable_special_oversize_and_symlink_inputs_fail_closed(self):
        bad = self.work / "bad.json"

        def check():
            for before, after in ((bad, self.current), (self.baseline, bad)):
                for limit in (None, 16):
                    result = self.run_comparison(before, after, expected=2, max_bytes=limit)
                    self.assertEqual(set(result), {"schema_version", "status", "error"})
                    self.assertEqual(result["status"], "error")

        check()  # Missing input must not be created.
        self.assertFalse(bad.exists())
        bad.mkdir()
        check()
        bad.rmdir()
        os.mkfifo(bad)
        check()
        bad.unlink()
        bad.write_bytes(b"\xff")
        check()
        with bad.open("wb") as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        check()
        bad.write_bytes(self.baseline.read_bytes())
        bad.chmod(0)
        try:
            check()
        finally:
            bad.chmod(0o600)
        bad.unlink()
        bad.symlink_to(self.baseline)
        check()
        alias = self.work / "alias"
        alias.symlink_to(self.work, target_is_directory=True)
        result = self.run_comparison(current=alias / self.current.name, expected=2)
        self.assertEqual(result["status"], "error")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["compare-snapshots", str(bad), str(self.current)]), 2)
        self.assertIn("stackwrench context-budget:", error.getvalue())

    def test_scan_flags_and_wrong_argument_counts_are_parser_errors(self):
        for extra in (("--ignore", "rules"),
                      ("--dockerfile", "Dockerfile"), ("--largest", "1"),
                      ("--candidate-ignore", "rules"),
                      ("--save-snapshot", "new.json"), ("--baseline-snapshot", "old.json"), ("extra",)):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(["compare-snapshots", str(self.baseline), str(self.current), *extra, "--json"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unrecognized arguments", error.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["compare-snapshots", str(self.baseline)])
            self.assertEqual(raised.exception.code, 2)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main(["compare-snapshots", "--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("without accessing the recorded context", " ".join(output.getvalue().split()))
            self.assertIn("--max-bytes NONNEGATIVE_INTEGER", output.getvalue())

    def test_text_escapes_saved_paths_recorded_paths_and_provenance(self):
        for attribute in ("baseline", "current"):
            path = getattr(self, attribute)
            renamed = path.with_name(attribute + "\n\x1b[31m.json")
            data = json.loads(path.read_text())
            data["context"] += "\n\x1b[31m"
            data["rules_source"] = data["context"] + "/.dockerignore"
            for row in data["entries"]:
                if row["last_rule"] is not None:
                    row["last_rule"]["source"] = data["rules_source"]
                if row["path"] == "keep":
                    row["path"] += "\n\x1b[31m"
            renamed.write_text(json.dumps(data))
            setattr(self, attribute, renamed)
        text = self.run_comparison(json_output=False)
        self.assertNotIn("\x1b", text)
        self.assertIn("current\\n\\x1b[31m.json", text)
        self.assertIn("context\\n\\x1b[31m", text)
        self.assertIn("keep\\n\\x1b[31m", text)


if __name__ == "__main__":
    unittest.main()
