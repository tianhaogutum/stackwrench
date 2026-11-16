"""Exact suffix budgets use only validated CURRENT saved payload rows."""

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

FLAG = "--max-extension-bytes"
PAIR = "--max-dir-extension-bytes"


class SavedExtensionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / "context"
        self.context.mkdir()
        for name, size in {"build/a.bin": 2000, "build/empty.bin": 0,
                           "outside/a.bin": 17, "archive.tar.gz": 11, "c.BIN": 13,
                           ".hidden": 2, "plain": 3, "end.": 5, "cache.bin/a.py": 31,
                           "build/control.bin": 23, "build/excluded.bin": 29,
                           "ignored/zero.bin": 0}.items():
            path = self.context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
        self.rules = self.context / "build/rules.bin"
        self.rules.write_text("build/excluded.bin\nignored\n!ignored/zero.bin\n")
        (self.context / "link.bin").symlink_to("missing-target")
        self.baseline, self.current = self.work / "before.json", self.work / "after.json"
        self.before = self.capture()
        write_snapshot(self.baseline, self.before)
        (self.context / "build/a.bin").write_bytes(b"x" * 1000)
        self.after = self.capture()
        write_snapshot(self.current, self.after)
        shutil.rmtree(self.context)

    def capture(self):
        return from_report(analyze(self.context, dockerfile="build/control.bin",
                                   ignore=self.rules, include_link_targets=False))

    def run_cli(self, *extra, expected=0, json_output=True, baseline=None, current=None):
        before, after = baseline or self.baseline, current or self.current
        args = ["compare-snapshots", str(before), str(after), *map(str, extra)]
        if json_output:
            args.append("--json")
        originals = [path.read_bytes() for path in (self.baseline, self.current)]
        real_open = os.open

        def saved_open(path, *args, **kwargs):
            self.assertNotIn(Path(path).name, {"context", "rules.bin", "control.bin"})
            return real_open(path, *args, **kwargs)

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

    def test_current_suffix_equality_and_exceedance_without_live_inputs(self):
        plain = self.run_cli()
        self.assertEqual(set(plain), {"schema_version", "status", "model", "snapshot_comparison"})
        comparison = plain["snapshot_comparison"]
        self.assertEqual(comparison["baseline"]["included_payload_bytes"], 2082)
        self.assertEqual(comparison["current"]["included_payload_bytes"], 1082)
        self.assertEqual(comparison["included_payload_bytes_delta"], -1000)
        for limit, code in ((1017, 0), (1016, 1)):
            with self.subTest(limit=limit):
                result = self.run_cli(FLAG, f".bin={limit}", expected=code)
                self.assertEqual(result, {**plain, "status": "ok" if code == 0 else "budget_exceeded",
                    "current_saved_extension_budgets": [{
                        "extension": ".bin", "included_payload_bytes": 1017,
                        "included_payload_files": 4, "max_bytes": limit, "passed": code == 0}],
                    "current_saved_budgets_passed": code == 0})

    def test_sorted_exact_suffixes_zero_counts_and_payload_partition(self):
        expected = [(".BIN", 13, 1), (".absent", 0, 0), (".bin", 1017, 4),
                    (".gz", 11, 1), (".py", 31, 1), ("<none>", 10, 3)]
        args = []
        for suffix, size, _ in reversed(expected):
            args.extend((FLAG, f"{suffix}={size}"))
        result = self.run_cli(*args)
        rows = result["current_saved_extension_budgets"]
        self.assertEqual(rows, [
            {"extension": suffix, "included_payload_bytes": size, "included_payload_files": count,
             "max_bytes": size, "passed": True} for suffix, size, count in expected])
        self.assertEqual(sum(row["included_payload_bytes"] for row in rows), 1082)
        self.assertEqual(sum(row["included_payload_files"] for row in rows), 10)
        self.assertTrue(result["current_saved_budgets_passed"])

    def test_total_extensions_and_pairs_must_all_pass(self):
        for total_ok, ext_ok, pair_ok in itertools.product((True, False), repeat=3):
            total, ext, pair = 1082 - (not total_ok), 1017 - (not ext_ok), 1000 - (not pair_ok)
            passed = total_ok and ext_ok and pair_ok
            with self.subTest(total=total, extension=ext, pair=pair):
                result = self.run_cli("--max-bytes", total, FLAG, f".bin={ext}",
                                      FLAG, ".gz=11", PAIR, "build", f".bin={pair}",
                                      expected=0 if passed else 1)
                self.assertEqual(result["current_saved_budgets_passed"], passed)
                self.assertEqual(result["current_saved_budget"], {
                    "max_bytes": total, "included_payload_bytes": 1082, "passed": total_ok})
                self.assertEqual(result["current_saved_extension_budgets"][0]["passed"], ext_ok)
                self.assertTrue(result["current_saved_extension_budgets"][1]["passed"])
                self.assertEqual(result["current_saved_directory_extension_budgets"], [{
                    "path": "build", "extension": ".bin", "included_payload_bytes": 1000,
                    "included_payload_files": 2, "max_bytes": pair, "passed": pair_ok}])

    def test_zero_byte_only_suffix_and_empty_current_pass_zero(self):
        data = copy.deepcopy(self.after)
        data["entries"] = [row for row in data["entries"] if row["path"] in
                           {"build", "build/empty.bin", "build/control.bin", "build/rules.bin"}]
        self.current.write_text(json.dumps(data))
        result = self.run_cli(FLAG, ".bin=0", "--max-bytes", 0)
        self.assertEqual(result["current_saved_extension_budgets"][0], {
            "extension": ".bin", "included_payload_bytes": 0, "included_payload_files": 1,
            "max_bytes": 0, "passed": True})
        self.current.write_text(json.dumps({**self.after, "entries": []}))
        result = self.run_cli(FLAG, ".bin=0", FLAG, "<none>=0", "--max-bytes", 0)
        self.assertTrue(result["current_saved_budgets_passed"])
        self.assertTrue(all(row["included_payload_files"] == 0 for row in result["current_saved_extension_budgets"]))

    def test_malformed_negative_duplicate_and_missing_limits_fail_closed(self):
        invalid = ("bin=1", ".tar.gz=1", ".=1", ".bin/a=0", ".bin=-1", ".bin=+1", ".bin=١",
                   ".bin=1.5", ".bin=1KB", ".bin=", ".bin", "=1", "", ".b\x00=1", ".bin=" + "9" * 5000)
        for value in invalid:
            with self.subTest(value=value[:50]):
                result = self.run_cli(FLAG, value, "--max-bytes", 1082, expected=2)
                self.assertEqual(set(result), {"schema_version", "status", "error"})
                self.assertEqual(result["status"], "error")
        for value in (".bin=1017", ".bin=1"):
            result = self.run_cli(FLAG, ".bin=1017", FLAG, value, expected=2)
            self.assertIn("duplicate", result["error"])
        text, error = self.run_cli(FLAG, ".tar.gz=0", expected=2, json_output=False)
        self.assertEqual(text, "")
        self.assertIn("final suffix", error)
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit) as raised:
                main(["compare-snapshots", str(self.baseline), str(self.current), "--json", FLAG])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("expected one argument", error.getvalue())

    def test_invalid_or_incompatible_either_artifact_emits_no_partial_results(self):
        bad = self.work / "bad.json"
        malformed = copy.deepcopy(self.after)
        malformed["entries"][0]["logical_bytes"] = True
        invalid = [{}, malformed, {**self.after, "schema_version": 2},
                   {**self.after, "entries": self.after["entries"] * 2},
                   {**self.after, "entries": [row for row in self.after["entries"] if row["path"] != "build"]},
                   {**self.after, "context": str(self.work / "other")},
                   {**self.after, "dockerfile": "Otherfile", "entries": []}]
        for data in invalid:
            bad.write_text(json.dumps(data))
            for before, after in ((bad, self.current), (self.baseline, bad)):
                result = self.run_cli(FLAG, ".bin=1017", "--max-bytes", 1082,
                                      PAIR, "build", ".bin=1000", baseline=before, current=after, expected=2)
                self.assertEqual(set(result), {"schema_version", "status", "error"})
                self.assertEqual(result["status"], "error")
        for content in ('{"format":1,"format":1}', '{', '\ufeff{}'):
            bad.write_text(content)
            self.assertEqual(set(self.run_cli(FLAG, ".bin=0", current=bad, expected=2)),
                             {"schema_version", "status", "error"})
        bad.unlink()
        for before, after in ((bad, self.current), (self.baseline, bad)):
            self.assertEqual(self.run_cli(FLAG, ".bin=0", baseline=before, current=after,
                                          expected=2)["status"], "error")

    def test_text_escaping_equals_unicode_and_total_only_schema(self):
        suffix = ".后=\n\x1b[31m"
        data = copy.deepcopy(self.after)
        data["entries"].append({"path": "a" + suffix, "kind": "file", "logical_bytes": 8,
                                "role": "payload", "included_by_rules": True, "last_rule": None})
        data["entries"].sort(key=lambda row: row["path"])
        self.current.write_text(json.dumps(data))
        text, error = self.run_cli(FLAG, suffix + "=7", expected=1, json_output=False)
        self.assertEqual(error, "")
        self.assertNotIn("\x1b", text)
        self.assertIn("Current saved extension budget '.后=\\n\\x1b[31m': 8 / 7 bytes; 1 files; EXCEEDED", text)
        self.assertIn("Current saved budgets: EXCEEDED", text)
        text, _ = self.run_cli(FLAG, suffix + "=8", json_output=False)
        self.assertIn("Current saved budgets: PASS", text)
        plain = self.run_cli()
        total_only = self.run_cli("--max-bytes", 1090)
        self.assertEqual(total_only, {**plain, "current_saved_budget": {
            "max_bytes": 1090, "included_payload_bytes": 1090, "passed": True}})


if __name__ == "__main__":
    unittest.main()
