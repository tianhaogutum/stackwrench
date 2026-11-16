from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.cache_replay.model import InputError, compare, scan_context


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "cache_replay"
CLI = [sys.executable, "-m", "stackwrench", "cache-replay"]


def cli_env(base=None):
    """Return an environment in which the source checkout is importable by subprocesses."""
    env = dict(os.environ if base is None else base)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if part)
    return env


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = self.root / "context"
        shutil.copytree(EXAMPLES / "context", self.context)
        self.before, self.after = self.root / "before", self.root / "after"
        shutil.copyfile(EXAMPLES / "Dockerfile.before", self.before)
        shutil.copyfile(EXAMPLES / "Dockerfile.after", self.after)
        self.changes = self.root / "changes.json"
        self.weights = self.root / "weights.json"
        self.events([{"path": "src/app.js", "kind": "content"}])

    def events(self, changes, sid="test"):
        self.changes.write_text(json.dumps({"schemaVersion": 1, "scenarios": [{"id": sid, "changes": changes}]}))

    def run_model(self, weighted=False, **options):
        return compare(self.before, self.after, self.changes, self.context, self.weights if weighted else None, **options)

    def cli(self, *extra, changes=None, environment=None):
        return subprocess.run([*CLI, "--before", str(self.before), "--after", str(self.after),
                               "--context", str(self.context), "--changes", str(changes or self.changes),
                               *extra], cwd=self.root, env=cli_env(environment), text=True, capture_output=True,
                              timeout=10)

    def assert_cli_error(self, *extra, phrase=None):
        result = self.cli(*extra)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("input not evaluated", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if phrase:
            self.assertIn(phrase, result.stderr)

    def assert_history_origin_reuses(self, report, producer_ids):
        from stackwrench.cache_replay.cli import render_text
        summaries = report["totals"]["syntheticHistoryReusesByOrigin"]
        self.assertEqual(set(summaries), {"before", "after"})
        expected_lines = []
        for side in ("before", "after"):
            weights = {i["id"]: i["weightModelUnits"] for i in report["dockerfiles"][side]["instructions"]}
            contributions = [step for item in report["scenarios"] for step in item[side]["steps"]
                             if step["decision"] == "synthetic-hit" and step["syntheticStateReuse"] == "retained-history"]
            expected = []
            for origin in [{"kind": "initial-warm"}] + [{"kind": "scenario", "scenarioId": sid} for sid in producer_ids]:
                hits = [s for s in contributions if s["syntheticStateOrigin"] == origin]
                if hits:
                    expected.append({"origin": origin, "reuseCount": len(hits),
                                     "reusedModelUnits": sum(weights[s["instructionId"]] for s in hits)})
            self.assertEqual(summaries[side], expected)
            self.assertEqual(sum(r["reuseCount"] for r in expected),
                             report["totals"]["syntheticReuseCounts"][side]["retainedHistory"])
            self.assertTrue(math.isclose(sum(r["reusedModelUnits"] for r in expected),
                report["totals"]["syntheticReuseModelUnits"][side]["retainedHistory"], rel_tol=1e-12, abs_tol=1e-9))
            for row in summaries[side]:
                self.assertEqual(set(row), {"origin", "reuseCount", "reusedModelUnits"})
                self.assertIs(type(row["reuseCount"]), int)
                self.assertGreater(row["reuseCount"], 0)
                origin = row["origin"]
                label = "initial-warm" if origin["kind"] == "initial-warm" else f"scenario {origin['scenarioId']}"
                expected_lines.append(f"  {side} synthetic retained-history reuses from {label}: "
                    f"count={row['reuseCount']}, reused={row['reusedModelUnits']:g} model units")
        lines = render_text(report).splitlines()
        self.assertEqual([line for line in lines if " synthetic retained-history reuses from " in line], expected_lines)
        if expected_lines:
            start = lines.index(expected_lines[0])
            self.assertEqual(lines[start:start + len(expected_lines) + 2], expected_lines + ["", "Assumptions:"])
            self.assertTrue(lines[start - 1].startswith("  after synthetic retained-history reuses I"))

    def test_history_origin_reuses_initial_restores_and_selection(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        ids = [s["id"] for s in json.loads(fixture.read_text())["scenarios"]]
        for mode in ("cumulative-history", "cumulative-cold"):
            full = compare(self.before, self.after, fixture, self.context,
                           EXAMPLES / "weights.json", mode=mode)
            cold = mode == "cumulative-cold"
            origin = {"kind": "scenario", "scenarioId": "revert-initial"} if cold else {"kind": "initial-warm"}
            self.assertEqual(full["totals"]["syntheticHistoryReusesByOrigin"], {
                "before": [{"origin": origin, "reuseCount": 3 if cold else 6, "reusedModelUnits": 110 if cold else 220}],
                "after": [{"origin": origin, "reuseCount": 2 if cold else 4, "reusedModelUnits": 10 if cold else 20}]})
            self.assert_history_origin_reuses(full, ids)
            self.assertEqual(full["schemaVersion"], 1)
            for options, indexes in (({"scenario": "revert-initial"}, [1]),
                    ({"select_scenarios": ["readd-initial", "edit-v2"]}, [0, 3])):
                selected = compare(self.before, self.after, fixture, self.context,
                                   EXAMPLES / "weights.json", mode=mode, **options)
                self.assertEqual(selected["scenarios"], [full["scenarios"][i] for i in indexes])
                self.assert_history_origin_reuses(selected, ids)
                if cold and indexes == [1]:
                    self.assertEqual(selected["totals"]["syntheticHistoryReusesByOrigin"], {"before": [], "after": []})

    def test_history_origin_reuses_first_producer_order_and_repeated_hits(self):
        self.before.write_text("FROM scratch\nCOPY src /app/\nENV MODE=demo\nRUN true\n")
        self.after.write_text("FROM scratch\nRUN candidate\n")
        revisions = ("v1", "v2", "v3", "v2", "v1", None, "v3", "v1")
        self.history(*([{"path": "src/app.js", "kind": "content", "revisionId": v}] if v else [] for v in revisions))
        # This scenario name must not collide with the distinct warm-origin kind.
        data = json.loads(self.changes.read_text())
        data["scenarios"][0]["id"] = "initial-warm"
        self.changes.write_text(json.dumps(data))
        ids = [s["id"] for s in data["scenarios"]]
        for mode in ("cumulative-history", "cumulative-cold"):
            full = self.run_model(mode=mode)
            rows = full["totals"]["syntheticHistoryReusesByOrigin"]
            self.assertEqual(rows["before"], [
                {"origin": {"kind": "scenario", "scenarioId": "initial-warm"}, "reuseCount": 6, "reusedModelUnits": 2},
                {"origin": {"kind": "scenario", "scenarioId": "step-2"}, "reuseCount": 3, "reusedModelUnits": 1},
                {"origin": {"kind": "scenario", "scenarioId": "step-3"}, "reuseCount": 3, "reusedModelUnits": 1}])
            self.assertEqual(rows["after"], [])
            self.assert_history_origin_reuses(full, ids)
            self.assertEqual(full["scenarios"][5]["before"]["syntheticReuseCounts"]["retainedHistory"], 0)
            for options in ({"scenario": "step-8"}, {"select_scenarios": ["step-8", "step-7", "step-4"]}):
                selected = self.run_model(mode=mode, **options)
                self.assert_history_origin_reuses(selected, ids)
                self.assertEqual(selected["totals"]["syntheticHistoryReusesByOrigin"]["before"][0]["origin"],
                                 {"kind": "scenario", "scenarioId": "initial-warm"})
                self.assertEqual(selected["scenarios"][-1]["before"]["steps"][-1]["syntheticStateOrigin"],
                                 {"kind": "scenario", "scenarioId": "initial-warm"})

    def test_history_origin_reuses_numeric_separate_caches_and_empty(self):
        self.instruction_miss_history()
        ids = [s["id"] for s in json.loads(self.changes.read_text())["scenarios"]]
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)
        for values in (None, (0,) * 7, (0.1, 0, 0.3, 0.2, 10**12, 0.7, 0.00001)):
            if values is not None:
                self.weights.write_text(json.dumps({"schemaVersion": 1,
                    "before": dict(zip((f"I{i:03d}" for i in range(1, 6)), values)),
                    "after": dict(zip((f"I{i:03d}" for i in range(1, 8)), reversed(values)))}))
            for mode in ("cumulative-history", "cumulative-cold"):
                with self.subTest(values=values, mode=mode), patch.object(Path, "open", guarded_open), \
                        patch("subprocess.Popen", side_effect=AssertionError("no execution")):
                    full = self.run_model(weighted=values is not None, mode=mode)
                    self.assert_history_origin_reuses(full, ids)
                    for item in full["scenarios"]:
                        selected = self.run_model(weighted=values is not None, mode=mode, scenario=item["id"])
                        self.assert_history_origin_reuses(selected, ids)
                        if item["id"] in ("step-1", "step-2", "step-8"):
                            self.assertEqual(selected["totals"]["syntheticHistoryReusesByOrigin"], {"before": [], "after": []})
                    selected = self.run_model(weighted=values is not None, mode=mode,
                                              select_scenarios=["step-7", "step-4", "step-3"])
                    self.assert_history_origin_reuses(selected, ids)
                    if values == (0,) * 7:
                        self.assertTrue(all(r["reusedModelUnits"] == 0 for rows in full["totals"]["syntheticHistoryReusesByOrigin"].values() for r in rows))
                    self.assertNotEqual(full["totals"]["syntheticHistoryReusesByOrigin"]["before"],
                                        full["totals"]["syntheticHistoryReusesByOrigin"]["after"])
        from stackwrench.cache_replay.cli import render_text
        self.before.write_text("FROM scratch\nARG MODE=demo\nENV MODE=demo\n")
        self.after.write_text("FROM scratch\nRUN true\n")
        self.history([], [{"path": "src/app.js", "kind": "mtime"}], [])
        for mode in ("cumulative-history", "cumulative-cold"):
            report = self.run_model(mode=mode)
            self.assert_history_origin_reuses(report, ["step-1", "step-2", "step-3"])
            self.assertEqual(report["totals"]["syntheticHistoryReusesByOrigin"], {"before": [], "after": []})
        for mode in ("independent-warm", "independent-cold"):
            report = self.run_model(mode=mode)
            self.assertNotIn("syntheticHistoryReusesByOrigin", json.dumps(report))
            self.assertNotIn("synthetic retained-history reuses from ", render_text(report))

    def test_history_origin_reuses_output_only_hidden_inputs(self):
        for mode in ("cumulative-history", "cumulative-cold"):
            for field in ("syntheticHistoryReusesByOrigin", "origin", "reuseCount", "reusedModelUnits"):
                for location in ("top", "scenario", "event", "weights", "weight-side"):
                    self.history([], [{"path": "src/app.js", "kind": "mtime"}], [])
                    data = json.loads(self.changes.read_text())
                    weights = {"schemaVersion": 1, "before": {}, "after": {}}
                    target = {"top": data, "scenario": data["scenarios"][1], "event": data["scenarios"][1]["changes"][0],
                              "weights": weights, "weight-side": weights["after"]}[location]
                    target[field] = []
                    self.changes.write_text(json.dumps(data))
                    self.weights.write_text(json.dumps(weights))
                    for options, args in (({"scenario": "step-1"}, ("--scenario", "step-1")),
                            ({"select_scenarios": ["step-3", "step-1"]}, ("--select-scenarios", "step-3", "step-1"))):
                        with self.subTest(mode=mode, field=field, location=location, options=options):
                            with self.assertRaises(InputError):
                                self.run_model(weighted=True, mode=mode, **options)
                            for output in ((), ("--json",)):
                                self.assert_cli_error("--mode", mode, *args, "--weights", str(self.weights),
                                                      "--fail-on-regression", *output)

    def test_history_origin_reuses_hidden_revision_and_copy_atomic(self):
        for mode in ("cumulative-history", "cumulative-cold"):
            for defect in ("revision", "unchanged", "copy"):
                self.history([], [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}], [])
                data = json.loads(self.changes.read_text())
                data["scenarios"][-1]["changes"] = ([{"path": "src/app.js", "kind": "content",
                    "revisionId": False if defect == "revision" else "v1"}] if defect != "copy" else
                    [{"path": "package-lock.json", "kind": "delete"}])
                self.changes.write_text(json.dumps(data))
                for args in (("--scenario", "step-1"), ("--select-scenarios", "step-2", "step-1")):
                    for output in ((), ("--json",)):
                        self.assert_cli_error("--mode", mode, *args, "--fail-on-regression", *output)

    def regression_fixture(self, after_weight=0.25, before_weight=10):
        self.before.write_text("FROM scratch\nCOPY src /app/\nRUN baseline\n")
        self.after.write_text("FROM scratch\nCOPY docs /docs/\nRUN candidate\n")
        self.weights.write_text(json.dumps({"schemaVersion": 1,
            "before": {"I003": before_weight}, "after": {"I003": after_weight}}))
        self.changes.write_text(json.dumps({"schemaVersion": 1, "scenarios": [
            {"id": "prime", "changes": []},
            {"id": "regress", "changes": [{"path": "docs/note.txt", "kind": "content"}]},
            {"id": "save", "changes": [{"path": "src/app.js", "kind": "content"}]},
            {"id": "tie", "changes": [{"path": "src/app.js", "kind": "mtime"}]}]}))

    def assert_regression_gate(self, mode, *selector, expected_exit):
        options = ("--weights", str(self.weights), "--mode", mode, *selector)
        for fmt in ((), ("--json",)):
            plain = self.cli(*options, *fmt)
            gated = self.cli(*options, *fmt, "--fail-on-regression")
            self.assertEqual(plain.returncode, 0, plain.stderr)
            self.assertEqual(gated.returncode, expected_exit, gated.stderr)
            self.assertEqual(gated.stdout, plain.stdout)
            self.assertEqual(gated.stderr, plain.stderr)
            self.assertEqual(gated.stderr, "")
            if fmt:
                report = json.loads(gated.stdout)
                self.assertEqual(expected_exit, int(any(s["savedModelUnits"] < 0 for s in report["scenarios"])))
        return report

    def test_fail_on_regression_mixed_scenarios_and_displayed_only(self):
        self.regression_fixture()
        for mode in ("independent-warm", "cumulative-history", "cumulative-cold"):
            with self.subTest(mode=mode):
                full = self.assert_regression_gate(mode, expected_exit=1)
                self.assertGreater(full["totals"]["savedModelUnits"], 0)
                self.assertEqual([s["savedModelUnits"] for s in full["scenarios"]],
                    [9.75 if mode == "cumulative-cold" else 0, -0.25, 10, 0])
                self.assert_regression_gate(mode, "--scenario", "regress", expected_exit=1)
                self.assert_regression_gate(mode, "--scenario", "save", expected_exit=0)
                self.assert_regression_gate(mode, "--scenario", "tie", expected_exit=0)
                selected = self.assert_regression_gate(mode, "--select-scenarios", "save", "regress", expected_exit=1)
                self.assertEqual([s["id"] for s in selected["scenarios"]], ["regress", "save"])
                self.assertEqual(selected["totals"]["savedModelUnits"], 9.75)
                selected = self.assert_regression_gate(mode, "--select-scenarios", "tie", "save", expected_exit=0)
                self.assertEqual([s["id"] for s in selected["scenarios"]], ["save", "tie"])
                self.assertEqual(selected["scenarios"], full["scenarios"][2:])
                self.assert_regression_gate(mode, "--select-scenarios", "regress",
                                            "--select-scenarios", "save", expected_exit=1)

    def test_fail_on_regression_all_modes_zero_tie_and_tiny_negative(self):
        modes = ("independent-warm", "independent-cold", "cumulative-history", "cumulative-cold")
        for mode in modes:
            for before, after, exit_code in ((0, 0, 0), (1, 1, 0), (1, 1 + 1e-12, 1)):
                with self.subTest(mode=mode, before=before, after=after):
                    self.regression_fixture(after_weight=after, before_weight=before)
                    # Both COPYs miss on the same content event, even in warm modes.
                    document = json.loads(self.changes.read_text())
                    for scenario in document["scenarios"]:
                        scenario["changes"] = [{"path": path, "kind": "content"}
                                               for path in ("src/app.js", "docs/note.txt")]
                    self.changes.write_text(json.dumps(document))
                    full = self.assert_regression_gate(mode, expected_exit=exit_code)
                    if exit_code:
                        for scenario in full["scenarios"]:
                            self.assertLess(scenario["savedModelUnits"], 0)
                            self.assertGreater(scenario["savedModelUnits"], -1e-9)
                    self.assert_regression_gate(mode, "--scenario", "save", expected_exit=exit_code)
                    self.assert_regression_gate(mode, "--select-scenarios", "tie", "regress", expected_exit=exit_code)
            # A positive-cost tie must also pass; independent-cold ignores changes.
            self.regression_fixture(after_weight=1, before_weight=1)
            self.assert_regression_gate(mode, "--scenario", "prime", expected_exit=0)
            self.regression_fixture(after_weight=10.25)
            if mode == "independent-cold":
                full = self.assert_regression_gate(mode, expected_exit=1)
                self.assertEqual([s["savedModelUnits"] for s in full["scenarios"]], [-0.25] * 4)

    def test_fail_on_regression_hidden_invalid_input_precedes_gate(self):
        for mode in ("independent-warm", "independent-cold", "cumulative-history", "cumulative-cold"):
            for defect in ("schema", "revision", "copy", "flag-field"):
                self.regression_fixture(after_weight=10.25 if mode == "independent-cold" else 0.25)
                document = json.loads(self.changes.read_text())
                later = document["scenarios"][-1]
                if defect == "schema":
                    later["unknown"] = True
                elif defect == "revision":
                    later["changes"] = [{"path": "docs/note.txt", "kind": "content", "revisionId": False}]
                elif defect == "copy":
                    later["changes"] = [{"path": "docs/note.txt", "kind": "delete"}]
                else:
                    later["failOnRegression"] = True
                self.changes.write_text(json.dumps(document))
                for fmt in ((), ("--json",)):
                    for selector in (("--scenario", "regress"), ("--select-scenarios", "save", "regress")):
                        with self.subTest(mode=mode, defect=defect, fmt=fmt, selector=selector):
                            options = ("--mode", mode, "--weights", str(self.weights), *selector, *fmt)
                            plain = self.cli(*options)
                            gated = self.cli(*options, "--fail-on-regression")
                            self.assertEqual(gated.returncode, 2, gated.stderr)
                            self.assertEqual(gated.stdout, "")
                            self.assertEqual((gated.stdout, gated.stderr), (plain.stdout, plain.stderr))
                            self.assertIn("input not evaluated", gated.stderr)
        for mode in ("cumulative-history", "cumulative-cold"):
            self.regression_fixture()
            document = json.loads(self.changes.read_text())
            document["scenarios"][1]["changes"][0]["revisionId"] = "v1"
            document["scenarios"][-1]["changes"] = [{"path": "docs/note.txt", "kind": "content", "revisionId": "v1"}]
            self.changes.write_text(json.dumps(document))
            for fmt in ((), ("--json",)):
                self.assert_cli_error("--mode", mode, "--scenario", "regress", *fmt,
                                      "--fail-on-regression", phrase="unchanged revisionId")

    def test_fail_on_regression_invalid_selectors_and_module_entry(self):
        self.regression_fixture()
        for selector in (("--scenario", "missing"), ("--select-scenarios", "save", "save"),
                         ("--scenario", "save", "--select-scenarios", "regress")):
            for fmt in ((), ("--json",)):
                result = self.cli(*selector, *fmt, "--fail-on-regression")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
        result = subprocess.run([*CLI,
            "--before", str(self.before), "--after", str(self.after), "--context", str(self.context),
            "--changes", str(self.changes), "--weights", str(self.weights), "--fail-on-regression", "--json"],
            cwd=self.root, env=cli_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["totals"]["savedModelUnits"], 9.75)
        self.assertEqual(result.stderr, "")

    def assert_history_reuses(self, report):
        from stackwrench.cache_replay.cli import render_text
        summaries = report["totals"]["syntheticHistoryReusesByInstruction"]
        self.assertEqual(set(summaries), {"before", "after"})
        expected_lines = []
        for side in ("before", "after"):
            rows = summaries[side]
            instructions = report["dockerfiles"][side]["instructions"]
            self.assertEqual([r["instructionId"] for r in rows], [i["id"] for i in instructions])
            for row, instruction in zip(rows, instructions):
                self.assertEqual(set(row), {"instructionId", "reuseCount", "reusedModelUnits", "reusedScenarioIds"})
                ids = [s["id"] for s in report["scenarios"] if any(
                    step["instructionId"] == instruction["id"] and step["decision"] == "synthetic-hit"
                    and step["syntheticStateReuse"] == "retained-history" for step in s[side]["steps"])]
                self.assertIs(type(row["reuseCount"]), int)
                self.assertEqual(row["reuseCount"], len(ids))
                self.assertEqual(row["reusedScenarioIds"], ids)
                self.assertEqual(len(set(ids)), len(ids))
                self.assertEqual(row["reusedModelUnits"], sum(instruction["weightModelUnits"] for _ in ids))
                self.assertTrue(math.isfinite(row["reusedModelUnits"]))
                self.assertGreaterEqual(row["reusedModelUnits"], 0)
                expected_lines.append(f"  {side} synthetic retained-history reuses {row['instructionId']}: "
                    f"count={len(ids)}, reused={row['reusedModelUnits']:g} model units; scenarios=[{', '.join(ids)}]")
            self.assertEqual(sum(r["reuseCount"] for r in rows),
                             report["totals"]["syntheticReuseCounts"][side]["retainedHistory"])
            self.assertTrue(math.isclose(sum(r["reusedModelUnits"] for r in rows),
                report["totals"]["syntheticReuseModelUnits"][side]["retainedHistory"], rel_tol=1e-12, abs_tol=1e-9))
        lines = render_text(report).splitlines()
        # Per-origin lines form a separate additive block after this one.
        actual = [line for line in lines if " synthetic retained-history reuses " in line and " reuses from " not in line]
        self.assertEqual(actual, expected_lines)
        start = lines.index(expected_lines[0])
        self.assertTrue(lines[start - 1].startswith("  after synthetic misses "))
        self.assertEqual(lines[start:start + len(expected_lines)], expected_lines)
        origin_lines = [line for line in lines if " synthetic retained-history reuses from " in line]
        end = start + len(expected_lines)
        self.assertEqual(lines[end:end + len(origin_lines)], origin_lines)
        self.assertEqual(lines[end + len(origin_lines):end + len(origin_lines) + 2], ["", "Assumptions:"])

    def test_history_reuses_initial_restores_and_selection(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        for mode in ("cumulative-history", "cumulative-cold"):
            full = compare(self.before, self.after, fixture, self.context,
                           EXAMPLES / "weights.json", mode=mode)
            rows = full["totals"]["syntheticHistoryReusesByInstruction"]
            ids = ["revert-initial", "readd-initial"] if mode == "cumulative-history" else ["readd-initial"]
            self.assertEqual([r["reusedScenarioIds"] for r in rows["before"]], [[], []] + [ids] * 3)
            self.assertEqual([r["reusedScenarioIds"] for r in rows["after"]], [[], [], [], []] + [ids] * 2)
            self.assertEqual(rows["before"][3], {"instructionId": "I004", "reuseCount": len(ids),
                "reusedModelUnits": 100 * len(ids), "reusedScenarioIds": ids})
            self.assert_history_reuses(full)
            for options, indexes in (({"scenario": "revert-initial"}, [1]),
                    ({"select_scenarios": ["readd-initial", "edit-v2"]}, [0, 3])):
                selected = compare(self.before, self.after, fixture, self.context,
                                   EXAMPLES / "weights.json", mode=mode, **options)
                self.assertEqual(selected["scenarios"], [full["scenarios"][i] for i in indexes])
                self.assert_history_reuses(selected)
                for side in ("before", "after"):
                    for row in selected["totals"]["syntheticHistoryReusesByInstruction"][side]:
                        self.assertTrue(set(row["reusedScenarioIds"]) <= {full["scenarios"][i]["id"] for i in indexes})

    def test_history_reuses_numeric_noop_mtime_and_hidden_producers(self):
        self.instruction_miss_history()
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)

        for values in (None, (0,) * 7, (0.1, 0, 0.3, 0.2, 10**12, 0.7, 0.00001)):
            if values is not None:
                self.weights.write_text(json.dumps({"schemaVersion": 1,
                    "before": dict(zip((f"I{i:03d}" for i in range(1, 6)), values)),
                    "after": dict(zip((f"I{i:03d}" for i in range(1, 8)), values))}))
            for mode in ("cumulative-history", "cumulative-cold"):
                with self.subTest(values=values, mode=mode), patch.object(Path, "open", guarded_open), \
                        patch("subprocess.Popen", side_effect=AssertionError("no execution")):
                    full = self.run_model(weighted=values is not None, mode=mode)
                    self.assert_history_reuses(full)
                    self.assertEqual(full["schemaVersion"], 1)
                    for item in full["scenarios"]:
                        selected = self.run_model(weighted=values is not None, mode=mode, scenario=item["id"])
                        self.assertEqual(selected["scenarios"], [item])
                        self.assert_history_reuses(selected)
                    for index in (0, 1, 7):  # no-op / mtime / no-op are never older-state restores
                        for side in ("before", "after"):
                            self.assertEqual(full["scenarios"][index][side]["syntheticReuseCounts"]["retainedHistory"], 0)
                    selected = self.run_model(weighted=values is not None, mode=mode,
                                              select_scenarios=["step-7", "step-4", "step-3"])
                    self.assert_history_reuses(selected)
                    self.assertEqual(selected["scenarios"][-1]["previousBuild"],
                                     {"kind": "scenario", "scenarioId": "step-6"})
                    self.assertEqual(selected["scenarios"][-1]["after"]["steps"][-1]["syntheticStateOrigin"],
                                     {"kind": "scenario", "scenarioId": "step-5"})
        shutil.copyfile(EXAMPLES / "Dockerfile.before", self.before)
        shutil.copyfile(EXAMPLES / "Dockerfile.after", self.after)
        for mode in ("cumulative-history", "cumulative-cold"):
            selected = compare(self.before, self.after, EXAMPLES / "changes.revisions.json", self.context,
                               mode=mode, select_scenarios=["readd-v1", "declare-v2"])
            self.assert_history_reuses(selected)
            for side in ("before", "after"):
                row = selected["totals"]["syntheticHistoryReusesByInstruction"][side][-1]
                self.assertEqual(row["reusedScenarioIds"], ["readd-v1"])
                self.assertEqual(selected["scenarios"][-1][side]["steps"][-1]["syntheticStateOrigin"],
                                 {"kind": "scenario", "scenarioId": "declare-v1"})

    def test_history_reuses_no_copy_and_independent_omission(self):
        from stackwrench.cache_replay.cli import render_text
        self.before.write_text("FROM scratch\nARG MODE=demo\nENV APP=demo\n")
        self.after.write_text("FROM scratch\nRUN true\n")
        self.history([], [{"path": "src/app.js", "kind": "mtime"}], [])
        for mode in ("cumulative-history", "cumulative-cold"):
            report = self.run_model(mode=mode)
            self.assert_history_reuses(report)
            for rows in report["totals"]["syntheticHistoryReusesByInstruction"].values():
                self.assertTrue(all(r["reuseCount"] == r["reusedModelUnits"] == 0 and r["reusedScenarioIds"] == [] for r in rows))
        for mode in ("independent-warm", "independent-cold"):
            report = self.run_model(mode=mode)
            self.assertNotIn("syntheticHistoryReusesByInstruction", json.dumps(report))
            self.assertNotIn("reusedScenarioIds", json.dumps(report))
            self.assertNotIn("synthetic retained-history reuses", render_text(report))

    def test_history_reuses_output_only_hidden_inputs(self):
        for mode in ("cumulative-history", "cumulative-cold"):
            for field in ("syntheticHistoryReusesByInstruction", "instructionId", "reuseCount", "reusedModelUnits", "reusedScenarioIds"):
                for location in ("top", "scenario", "event", "weights", "weight-side"):
                    self.history([], [{"path": "src/app.js", "kind": "mtime"}], [])
                    data = json.loads(self.changes.read_text())
                    weights = {"schemaVersion": 1, "before": {}, "after": {}}
                    target = {"top": data, "scenario": data["scenarios"][1],
                              "event": data["scenarios"][1]["changes"][0],
                              "weights": weights, "weight-side": weights["after"]}[location]
                    target[field] = []
                    self.changes.write_text(json.dumps(data))
                    self.weights.write_text(json.dumps(weights))
                    for options, args in (({"scenario": "step-1"}, ("--scenario", "step-1")),
                            ({"select_scenarios": ["step-3", "step-1"]}, ("--select-scenarios", "step-3", "step-1"))):
                        with self.subTest(mode=mode, field=field, location=location, options=options):
                            with self.assertRaises(InputError):
                                self.run_model(weighted=True, mode=mode, **options)
                            for output in ((), ("--json",)):
                                self.assert_cli_error("--mode", mode, *args, "--weights", str(self.weights), *output)

    def test_multi_selection_ordered_restore_and_displayed_totals(self):
        from stackwrench.cache_replay.cli import render_text
        fixture = EXAMPLES / "changes.initial-revisions.json"
        for mode, costs, counts, reuse in (
            ("cumulative-history", (110, 10, 100),
             ([0, 0, 1, 1, 1], [0, 0, 0, 0, 1, 1]),
             ((4, 3, 3), (8, 2, 2))),
            ("cumulative-cold", (220, 120, 100),
             ([1, 1, 2, 2, 2], [1, 1, 1, 1, 2, 2]),
             ((2, 0, 8), (4, 0, 8)))):
            full = compare(self.before, self.after, fixture, self.context,
                           EXAMPLES / "weights.json", mode=mode)
            for selectors in (["edit-v2", "revert-initial"], ("revert-initial", "edit-v2")):
                with self.subTest(mode=mode, selectors=selectors):
                    report = compare(self.before, self.after, fixture, self.context,
                                     EXAMPLES / "weights.json", mode=mode, select_scenarios=selectors)
                    self.assertEqual(report["scenarios"], full["scenarios"][:2])
                    self.assertEqual(report["selectedScenarios"], {
                        "scenarioIds": ["edit-v2", "revert-initial"],
                        "evaluatedScenarioCount": 4, "displayedScenarioCount": 2})
                    self.assertNotIn("selection", report)
                    self.assertEqual(report["schemaVersion"], 1)
                    self.assertEqual(tuple(report["totals"][k] for k in
                        ("beforeModelUnits", "afterModelUnits", "savedModelUnits")), costs)
                    for side, expected_counts, expected_reuse in zip(("before", "after"), counts, reuse):
                        self.assertEqual([r["missCount"] for r in report["totals"]["syntheticMissesByInstruction"][side]],
                                         expected_counts)
                        self.assertEqual(report["totals"]["syntheticReuseCounts"][side],
                                         dict(zip(("previousBuild", "retainedHistory", "misses"), expected_reuse)))
                    self.assertEqual(report["totals"]["syntheticReuseModelUnits"],
                        {"before": {"previousBuild": 0, "retainedHistory": 110, "misses": 110},
                         "after": {"previousBuild": 200, "retainedHistory": 10, "misses": 10}}
                        if mode == "cumulative-history" else
                        {"before": {"previousBuild": 0, "retainedHistory": 0, "misses": 220},
                         "after": {"previousBuild": 100, "retainedHistory": 0, "misses": 120}})
                    self.assert_instruction_misses(report)
                    lines = render_text(report).splitlines()
                    self.assertIn("Report filter: scenarios edit-v2, revert-initial (2 displayed / 4 evaluated)", lines)
                    self.assertEqual([s for s in lines if s.startswith("Scenario ")],
                                     ["Scenario edit-v2", "Scenario revert-initial"])
                    self.assertIn(f"Total (displayed scenarios): before={costs[0]}, after={costs[1]}, saved=100 model units", lines)

    def test_multi_selection_hidden_producers_inventory_and_fractional_misses(self):
        fixture = EXAMPLES / "changes.revisions.json"
        for mode in ("cumulative-history", "cumulative-cold"):
            full = compare(self.before, self.after, fixture, self.context, mode=mode)
            selected = compare(self.before, self.after, fixture, self.context, mode=mode,
                               select_scenarios=["readd-v1", "declare-v2"])
            self.assertEqual(selected["scenarios"], [full["scenarios"][1], full["scenarios"][4]])
            self.assertEqual(selected["scenarios"][1]["previousBuild"],
                             {"kind": "scenario", "scenarioId": "delete-source"})
            for side in ("before", "after"):
                step = selected["scenarios"][1][side]["steps"][-1]
                self.assertEqual(step["syntheticStateOrigin"], {"kind": "scenario", "scenarioId": "declare-v1"})
                self.assertEqual(step["syntheticStateReuse"], "retained-history")
            history = EXAMPLES / "changes.history.json"
            full = compare(self.before, self.after, history, self.context, mode=mode)
            selected = compare(self.before, self.after, history, self.context, mode=mode,
                               select_scenarios=["remove-added-source", "manifest-edit"])
            self.assertEqual(selected["scenarios"], [full["scenarios"][1], full["scenarios"][3]])
            self.assertEqual(selected["scenarios"][1]["inventoryBeforeFileCount"], 5)
        self.instruction_miss_history()
        self.weights.write_text(json.dumps({"schemaVersion": 1,
            "before": {"I001": 0.125, "I003": 0.25, "I005": 0},
            "after": {"I002": 0.5, "I005": 0.1}}))
        for mode in ("cumulative-history", "cumulative-cold"):
            report = self.run_model(weighted=True, mode=mode, select_scenarios=["step-7", "step-4", "step-3"])
            self.assert_instruction_misses(report)
            self.assertEqual(report["totals"]["beforeModelUnits"], 0.25)
            self.assertTrue(math.isclose(report["totals"]["afterModelUnits"], 2.1))
            self.assertEqual([r["missCount"] for r in report["totals"]["syntheticMissesByInstruction"]["before"]],
                             [0, 0, 1, 1, 1])

    def test_multi_selection_independent_modes_cli_order_singleton_and_all(self):
        fixture = EXAMPLES / "changes.json"
        for mode, costs in (("independent-warm", (110, 10, 100)), ("independent-cold", (220, 220, 0))):
            full = compare(self.before, self.after, fixture, self.context, EXAMPLES / "weights.json", mode=mode)
            selectors = ["mtime-only", "source-edit"]
            selected = compare(self.before, self.after, fixture, self.context, EXAMPLES / "weights.json",
                               mode=mode, select_scenarios=selectors)
            self.assertEqual(selected["scenarios"], [full["scenarios"][0], full["scenarios"][-1]])
            self.assertEqual(selected["totals"], dict(zip(
                ("beforeModelUnits", "afterModelUnits", "savedModelUnits"), costs)))
            self.assertEqual(selected["selectedScenarios"], {"scenarioIds": ["source-edit", "mtime-only"],
                "evaluatedScenarioCount": 5, "displayedScenarioCount": 2})
            for args in (("--select-scenarios", *selectors),
                         ("--select-scenarios", "source-edit", "--select-scenarios", "mtime-only")):
                result = self.cli("--mode", mode, "--weights", str(EXAMPLES / "weights.json"),
                                  *args, "--json", changes=fixture)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout), selected)
            all_ids = [s["id"] for s in full["scenarios"]]
            all_selected = compare(self.before, self.after, fixture, self.context, EXAMPLES / "weights.json",
                                   mode=mode, select_scenarios=list(reversed(all_ids)))
            self.assertEqual(all_selected, {**full, "selectedScenarios": {"scenarioIds": all_ids,
                "evaluatedScenarioCount": 5, "displayedScenarioCount": 5}})
        for sid in ("A", "A_0.-" + "z" * 75):
            self.events([], sid=sid)
            one = self.run_model(select_scenarios=[sid])
            self.assertEqual(one["scenarios"], self.run_model(scenario=sid)["scenarios"])
            self.assertEqual(one["selectedScenarios"], {"scenarioIds": [sid],
                "evaluatedScenarioCount": 1, "displayedScenarioCount": 1})
        self.assertEqual(self.run_model(select_scenarios=None), self.run_model())

    def test_multi_selection_invalid_selectors_and_mutual_exclusion_are_atomic(self):
        for value in ([], (), "test", {"test"}, {"test": 1}, False, 1, [False], [1], [[]], [None],
                      [""], ["two words"], ["é"], ["test\n"], ["x/y"], ["x" * 81], ["test", "test"]):
            with self.subTest(value=value), self.assertRaisesRegex(InputError, "select_scenarios"):
                self.run_model(select_scenarios=value)
        with self.assertRaisesRegex(InputError, "mutually exclusive"):
            self.run_model(scenario="test", select_scenarios=["test"])
        for ids in (["test", "missing"], ["Test"], ["te"]):
            with self.subTest(ids=ids), self.assertRaisesRegex(InputError, "unknown scenario ID"):
                self.run_model(select_scenarios=ids)
        for args in (("--select-scenarios",), ("--select-scenarios", "test", "test"),
                     ("--select-scenarios", "test", "--select-scenarios", "test"),
                     ("--select-scenarios", "test", "missing"), ("--select-scenarios", "Test"),
                     ("--select-scenarios", "test*"),
                     ("--scenario", "test", "--select-scenarios", "test"),
                     ("--select-scenarios", "test", "--scenario", "test")):
            for output in ((), ("--json",)):
                with self.subTest(args=args, output=output):
                    result = self.cli(*args, *output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertTrue(result.stderr)

    def test_multi_selection_hidden_invalid_inputs_still_reject_everything(self):
        for mode in ("independent-warm", "independent-cold", "cumulative-history", "cumulative-cold"):
            for event, phrase in (({"path": "missing", "kind": "content"}, "unknown change path"),
                                  ({"path": "package-lock.json", "kind": "delete"}, "step-3 / after"),
                                  ({"path": "src/app.js", "kind": "mtime", "extra": 1}, "expected exactly fields")):
                self.history([], [], [event])
                for output in ((), ("--json",)):
                    with self.subTest(mode=mode, event=event, output=output):
                        self.assert_cli_error("--mode", mode, "--select-scenarios", "step-2", "step-1",
                                              *output, phrase=phrase)
                with self.assertRaisesRegex(InputError, phrase):
                    self.run_model(mode=mode, select_scenarios=["step-2", "step-1"])
            self.history([], [], [])
            data = json.loads(self.changes.read_text())
            data["selectedScenarios"] = {"scenarioIds": ["step-1", "step-2"]}
            self.changes.write_text(json.dumps(data))
            self.assert_cli_error("--mode", mode, "--select-scenarios", "step-1", "step-2", "--json",
                                  phrase="expected exactly fields")
        for mode in ("cumulative-history", "cumulative-cold"):
            self.history([], [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                         [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}])
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", mode, "--select-scenarios", "step-1", "step-2", *output,
                                      phrase="step-3: content event has unchanged revisionId")
            self.history([{"path": "package-lock.json", "kind": "delete"}], [], [])
            self.assert_cli_error("--mode", mode, "--select-scenarios", "step-2", "step-3", "--json",
                                  phrase="step-1 / after")

    def cold_pair(self):
        self.before.write_text("FROM scratch\nCOPY . /app/\nRUN echo inert\n")
        self.after.write_text("FROM node:22\nCOPY src /app/\nRUN echo candidate\n")

    def test_miss_scenario_attribution_selected_history(self):
        from stackwrench.cache_replay.cli import render_text
        fixture = EXAMPLES / "changes.initial-revisions.json"
        for mode in ("cumulative-history", "cumulative-cold"):
            cold = mode == "cumulative-cold"
            full = compare(self.before, self.after, fixture, self.context,
                           EXAMPLES / "weights.json", mode=mode)
            selected = compare(self.before, self.after, fixture, self.context,
                               EXAMPLES / "weights.json", mode=mode,
                               select_scenarios=["revert-initial", "edit-v2"])
            expected = {
                "before": [["edit-v2"] if cold else []] * 2 +
                          [["edit-v2", "revert-initial"] if cold else ["edit-v2"]] * 3,
                "after": [["edit-v2"] if cold else []] * 4 +
                         [["edit-v2", "revert-initial"] if cold else ["edit-v2"]] * 2}
            for side in ("before", "after"):
                rows = selected["totals"]["syntheticMissesByInstruction"][side]
                self.assertEqual([r["missedScenarioIds"] for r in rows], expected[side])
                for row, instruction in zip(rows, selected["dockerfiles"][side]["instructions"]):
                    self.assertEqual(row["missCount"], len(row["missedScenarioIds"]))
                    self.assertTrue(math.isclose(row["missedModelUnits"],
                        sum(instruction["weightModelUnits"] for _ in row["missedScenarioIds"]),
                        rel_tol=1e-12, abs_tol=1e-9))
                    self.assertIn(f"  {side} synthetic misses {row['instructionId']}: "
                        f"count={row['missCount']}, missed={row['missedModelUnits']:g} model units; "
                        f"scenarios=[{', '.join(row['missedScenarioIds'])}]", render_text(selected).splitlines())
            self.assertEqual(selected["scenarios"], full["scenarios"][:2])
            # A hidden producer and predecessor remain evidence, never contributions.
            restored = compare(self.before, self.after, EXAMPLES / "changes.revisions.json",
                               self.context, mode=mode, select_scenarios=["readd-v1", "declare-v2"])
            self.assertEqual(restored["scenarios"][1]["previousBuild"],
                             {"kind": "scenario", "scenarioId": "delete-source"})
            for side in ("before", "after"):
                self.assertEqual(restored["scenarios"][1][side]["steps"][-1]["syntheticStateOrigin"],
                                 {"kind": "scenario", "scenarioId": "declare-v1"})
                self.assertTrue(all(set(r["missedScenarioIds"]) <= {"declare-v2", "readd-v1"}
                    for r in restored["totals"]["syntheticMissesByInstruction"][side]))
            self.assert_instruction_misses(restored)

    def test_miss_scenario_attribution_output_only_hidden_inputs(self):
        for mode in ("cumulative-history", "cumulative-cold"):
            for location in ("top", "scenario", "event", "weights", "weight-side"):
                self.history([], [{"path": "src/app.js", "kind": "mtime"}], [])
                data = json.loads(self.changes.read_text())
                weights = {"schemaVersion": 1, "before": {}, "after": {}}
                target = {"top": data, "scenario": data["scenarios"][1],
                          "event": data["scenarios"][1]["changes"][0],
                          "weights": weights, "weight-side": weights["before"]}[location]
                target["missedScenarioIds"] = []
                self.changes.write_text(json.dumps(data))
                self.weights.write_text(json.dumps(weights))
                for options, args in (({"scenario": "step-1"}, ("--scenario", "step-1")),
                        ({"select_scenarios": ["step-3", "step-1"]},
                         ("--select-scenarios", "step-3", "step-1"))):
                    with self.subTest(mode=mode, location=location, options=options):
                        with self.assertRaises(InputError):
                            self.run_model(weighted=True, mode=mode, **options)
                        for output in ((), ("--json",)):
                            self.assert_cli_error("--mode", mode, *args, "--weights", str(self.weights), *output)

    def instruction_miss_history(self):
        self.before.write_text("FROM scratch\nWORKDIR /app\nCOPY . /app/\nENV MODE=demo\nRUN true\n")
        self.after.write_text("FROM scratch\nARG MODE=demo\nCOPY src /app/\nRUN true\nCOPY docs /docs/\nRUN true\nWORKDIR /done\n")
        self.history([], [{"path": "src/app.js", "kind": "mtime"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}],
                     [{"path": "docs/note.txt", "kind": "content"}],
                     [{"path": "src/new.js", "kind": "add"}],
                     [{"path": "src/new.js", "kind": "delete"}], [],
                     [{"path": "src/app.js", "kind": "content"}])
        self.set_initial_revisions({"src/app.js": "v0"})

    def assert_instruction_misses(self, report):
        summaries = report["totals"]["syntheticMissesByInstruction"]
        self.assertEqual(set(summaries), {"before", "after"})
        for side in ("before", "after"):
            records = summaries[side]
            instructions = report["dockerfiles"][side]["instructions"]
            self.assertEqual([r["instructionId"] for r in records], [i["id"] for i in instructions])
            for record, instruction in zip(records, instructions):
                self.assertEqual(set(record), {"instructionId", "missCount", "missedModelUnits", "missedScenarioIds"})
                misses = [item for item in report["scenarios"] if any(
                    s["instructionId"] == instruction["id"] and s["decision"] == "synthetic-miss"
                    for s in item[side]["steps"])]
                self.assertIs(type(record["missCount"]), int)
                self.assertEqual(record["missCount"], len(misses))
                self.assertEqual(record["missedScenarioIds"], [item["id"] for item in misses])
                self.assertEqual(len(record["missedScenarioIds"]), record["missCount"])
                self.assertEqual(record["missedModelUnits"], sum(instruction["weightModelUnits"] for _ in misses))
                self.assertIn(type(record["missedModelUnits"]), (int, float))
                self.assertTrue(math.isfinite(record["missedModelUnits"]))
                self.assertGreaterEqual(record["missedModelUnits"], 0)
            self.assertEqual(sum(r["missCount"] for r in records),
                             report["totals"]["syntheticReuseCounts"][side]["misses"])
            self.assertTrue(math.isclose(sum(r["missedModelUnits"] for r in records),
                report["totals"]["syntheticReuseModelUnits"][side]["misses"], rel_tol=1e-12, abs_tol=1e-9))

    def test_instruction_misses_ordered_numeric_contract(self):
        self.instruction_miss_history()
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)

        for values in (None, (0,) * 7, (0.1, 0, 0.3, 0.2, 10**12, 0.7, 0.00001)):
            if values is not None:
                self.weights.write_text(json.dumps({"schemaVersion": 1,
                    "before": dict(zip((f"I{i:03d}" for i in range(1, 6)), values)),
                    "after": dict(zip((f"I{i:03d}" for i in range(1, 8)), values))}))
            for mode in ("cumulative-history", "cumulative-cold"):
                with self.subTest(values=values, mode=mode), patch.object(Path, "open", guarded_open), \
                        patch("subprocess.Popen", side_effect=AssertionError("no execution")):
                    report = self.run_model(weighted=values is not None, mode=mode)
                    self.assert_instruction_misses(report)
                    cold = int(mode == "cumulative-cold")
                    self.assertEqual(report["schemaVersion"], 1)
                    for side, counts in (("before", [0, 0, 4, 4, 4]), ("after", [0, 0, 3, 3, 4, 4, 4])):
                        self.assertEqual([r["missCount"] for r in report["totals"]["syntheticMissesByInstruction"][side]],
                                         [c + cold for c in counts])
                    # Restores and deleting the added file use retained states, not new misses.
                    for index in (3, 6):
                        self.assertEqual(report["scenarios"][index]["after"]["invalidatedInstructionIds"], [])
                    if values is None:
                        self.assertEqual(report["totals"]["syntheticMissesByInstruction"]["before"][-1],
                            {"instructionId": "I005", "missCount": 4 + cold, "missedModelUnits": 4 + cold,
                             "missedScenarioIds": (["step-1"] if cold else []) +
                                                  ["step-3", "step-5", "step-6", "step-9"]})

    def test_instruction_misses_selection_and_exact_text(self):
        from stackwrench.cache_replay.cli import render_text
        self.instruction_miss_history()
        self.weights.write_text(json.dumps({"schemaVersion": 1,
            "before": {"I001": 0.125, "I003": 0.25}, "after": {"I002": 0.5, "I005": 0.1}}))
        for mode in ("cumulative-history", "cumulative-cold"):
            full = self.run_model(weighted=True, mode=mode)
            for item in full["scenarios"]:
                selected = self.run_model(weighted=True, mode=mode, scenario=item["id"])
                self.assertEqual(selected["scenarios"], [item])
                self.assert_instruction_misses(selected)
                lines = render_text(selected).splitlines()
                start = lines.index(next(line for line in lines if line.startswith("  before synthetic misses")))
                expected = []
                for side in ("before", "after"):
                    for instruction in selected["dockerfiles"][side]["instructions"]:
                        count = int(instruction["id"] in item[side]["invalidatedInstructionIds"])
                        units = instruction["weightModelUnits"] if count else 0
                        ids = item["id"] if count else ""
                        expected.append(f"  {side} synthetic misses {instruction['id']}: count={count}, "
                                        f"missed={units:g} model units; scenarios=[{ids}]")
                self.assertEqual(lines[start:start + len(expected)], expected)
                self.assertTrue(lines[start - 1].startswith("  after synthetic reuse model units:"))
                self.assertTrue(lines[start + len(expected)].startswith("  before synthetic retained-history reuses "))
            selected = self.run_model(weighted=True, mode=mode, scenario="step-7")
            self.assertEqual(selected["scenarios"][0]["previousBuild"], {"kind": "scenario", "scenarioId": "step-6"})
            self.assertTrue(all(r["missCount"] == 0 for rows in selected["totals"]["syntheticMissesByInstruction"].values() for r in rows))
            origin = selected["scenarios"][0]["after"]["steps"][-1]["syntheticStateOrigin"]
            self.assertEqual(origin, {"kind": "scenario", "scenarioId": "step-5"})
            result = self.cli("--mode", mode, "--scenario", "step-7", "--weights", str(self.weights), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), selected)

    def test_instruction_misses_no_copy_and_independent_omission(self):
        from stackwrench.cache_replay.cli import render_text
        self.before.write_text("FROM scratch\nARG MODE=demo\nENV APP=demo\n")
        self.after.write_text("FROM scratch\nRUN true\n")
        self.history([], [{"path": "src/app.js", "kind": "mtime"}], [])
        for mode in ("cumulative-history", "cumulative-cold"):
            report = self.run_model(mode=mode)
            self.assert_instruction_misses(report)
            for side in ("before", "after"):
                for record in report["totals"]["syntheticMissesByInstruction"][side]:
                    self.assertEqual(record["missCount"], int(mode == "cumulative-cold"))
        for mode in ("independent-warm", "independent-cold"):
            report = self.run_model(mode=mode)
            self.assertNotIn("syntheticMissesByInstruction", json.dumps(report))
            self.assertNotIn("missedScenarioIds", json.dumps(report))
            self.assertNotIn("synthetic misses", render_text(report))

    def test_instruction_misses_output_only_and_atomic_later_errors(self):
        for mode in ("cumulative-history", "cumulative-cold"):
            for field in ("syntheticMissesByInstruction", "instructionId", "missCount", "missedModelUnits"):
                for location in ("top", "scenario", "event", "weights", "weight-side"):
                    self.history([], [{"path": "src/app.js", "kind": "mtime"}])
                    data = json.loads(self.changes.read_text())
                    weights = {"schemaVersion": 1, "before": {}, "after": {}}
                    target = {"top": data, "scenario": data["scenarios"][1],
                              "event": data["scenarios"][1]["changes"][0],
                              "weights": weights, "weight-side": weights["before"]}[location]
                    target[field] = 0
                    self.changes.write_text(json.dumps(data))
                    self.weights.write_text(json.dumps(weights))
                    with self.subTest(mode=mode, field=field, location=location):
                        with self.assertRaises(InputError):
                            self.run_model(weighted=True, mode=mode, scenario="step-1")
                        for output in ((), ("--json",)):
                            self.assert_cli_error("--mode", mode, "--scenario", "step-1",
                                                  "--weights", str(self.weights), *output)
            for invalid in ("schema", "revision", "source"):
                self.instruction_miss_history()
                data = json.loads(self.changes.read_text())
                if invalid == "schema":
                    data["scenarios"][-1]["unknown"] = 1
                elif invalid == "revision":
                    data["scenarios"][-1]["changes"] = [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}]
                else:
                    self.after.write_text("FROM scratch\nCOPY src/app.js /app/\nRUN true\n")
                    data["scenarios"][-1]["changes"] = [{"path": "src/app.js", "kind": "delete"}]
                self.changes.write_text(json.dumps(data))
                with self.assertRaises(InputError):
                    self.run_model(mode=mode, scenario="step-1")
                for output in ((), ("--json",)):
                    self.assert_cli_error("--mode", mode, "--scenario", "step-1", *output)

    def assert_cold_result(self, result, ids, units):
        self.assertEqual(result["invalidatedInstructionIds"], ids)
        self.assertEqual(result["invalidatedModelUnits"], units)
        self.assertEqual(result["cache"], {"hitInstructionIds": [], "missInstructionIds": ids,
                                          "entryCountBefore": 0, "entryCountAfter": len(ids)})
        self.assertEqual(result["reasonChain"], [
            {"instructionId": i, "because": "empty synthetic cache (independent-cold)", "causedBy": []}
            for i in ids])
        self.assertNotIn("syntheticReuseCounts", result)
        self.assertNotIn("syntheticReuseModelUnits", result)
        for step in result["steps"]:
            self.assertEqual(step["decision"], "synthetic-miss")
            self.assertEqual(step["why"], "Empty synthetic cache for this independent scenario; "
                             "every instruction, including FROM, is assumed to miss.")
            self.assertIsNone(step["causedByInstructionId"])
            self.assertNotIn("syntheticStateOrigin", step)
            self.assertNotIn("syntheticStateReuse", step)

    def test_cumulative_cold_noop_content_noop_contract(self):
        self.cold_pair()
        self.history([], [{"path": "src/app.js", "kind": "content"}], [])
        self.weights.write_text(json.dumps({"schemaVersion": 1,
            "before": {"I001": 0.25, "I002": 0, "I003": 1.5},
            "after": {"I001": 2, "I002": 0.5, "I003": 0}}))
        report = self.run_model(weighted=True, mode="cumulative-cold")
        self.assertEqual((report["schemaVersion"], report["mode"], report["model"]),
                         (1, "cumulative-cold", "linear-copy-history-v1"))
        self.assertEqual([s["previousBuild"] for s in report["scenarios"]],
                         [None, {"kind": "scenario", "scenarioId": "step-1"},
                          {"kind": "scenario", "scenarioId": "step-2"}])
        for side, costs in (("before", [1.75, 1.5, 0]), ("after", [2.5, 0.5, 0])):
            for index, (misses, hits, entries) in enumerate((
                (["I001", "I002", "I003"], [], (0, 3)),
                (["I002", "I003"], ["I001"], (3, 5)),
                ([], ["I001", "I002", "I003"], (5, 5)))):
                result = report["scenarios"][index][side]
                self.assertEqual(result["cache"], {"hitInstructionIds": hits, "missInstructionIds": misses,
                    "entryCountBefore": entries[0], "entryCountAfter": entries[1]})
                self.assertEqual(result["invalidatedInstructionIds"], misses)
                self.assertEqual(result["invalidatedRunIds"], ["I003"] if misses else [])
                self.assertEqual(result["invalidatedModelUnits"], costs[index])
                self.assertEqual(result["syntheticReuseCounts"],
                                 {"previousBuild": len(hits), "retainedHistory": 0, "misses": len(misses)})
                self.assertEqual(result["firstAffectedCopy"], "I002" if index == 1 else None)
                self.assertEqual(result["firstInvalidatedCopy"], "I002" if misses else None)
                for step in result["steps"]:
                    self.assertEqual(step["decision"], "synthetic-miss" if step["instructionId"] in misses else "synthetic-hit")
                    self.assertEqual(step["syntheticStateReuse"], None if step["instructionId"] in misses else "previous-build")
            first = report["scenarios"][0][side]
            self.assertEqual(first["reasonChain"], [
                {"instructionId": i, "because": "empty synthetic cache (cumulative-cold)", "causedBy": []}
                for i in ("I001", "I002", "I003")])
            for step in first["steps"]:
                self.assertIsNone(step["syntheticStateOrigin"])
                self.assertIsNone(step["causedByInstructionId"])
                self.assertEqual(step["why"], "Empty synthetic cache for the first ordered scenario; "
                                 "every instruction, including FROM, is assumed to miss.")
            self.assertEqual([s["syntheticStateOrigin"] for s in report["scenarios"][-1][side]["steps"]],
                             [{"kind": "scenario", "scenarioId": "step-1"},
                              {"kind": "scenario", "scenarioId": "step-2"},
                              {"kind": "scenario", "scenarioId": "step-2"}])
        self.assert_history_reuse_totals(report)
        self.assertEqual([report["totals"][k] for k in ("beforeModelUnits", "afterModelUnits", "savedModelUnits")],
                         [3.25, 3.0, 0.25])
        self.assertNotIn('"initial-warm"', json.dumps(report))
        text = self.cli("--mode", "cumulative-cold", "--weights", str(self.weights))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout.count("  previous modeled build: none (cold start)\n"), 1)
        self.assertEqual(text.stdout.count("synthetic cache: 0 hits, 3 misses; retained entries 0 -> 3"), 2)
        self.assertIn("previous modeled build: scenario step-2", text.stdout)
        self.assertIn("not Docker pulls, network work, real BuildKit cache keys or timing", text.stdout)
        self.assertNotIn("initial-warm", text.stdout)
        default = self.run_model(mode="cumulative-cold")
        self.assertEqual(default["totals"]["beforeModelUnits"], 2)
        self.assertEqual(default["totals"]["afterModelUnits"], 2)

    def test_cumulative_cold_retains_only_scenario_states_and_revisions(self):
        self.cold_pair()
        # Initial declarations do not prewarm states, even on a later revert.
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "mtime"}], [],
                     [{"path": "src/new.js", "kind": "add", "revisionId": "new"}],
                     [{"path": "src/new.js", "kind": "content"}],
                     [{"path": "src/new.js", "kind": "delete"}],
                     [{"path": "src/new.js", "kind": "add", "revisionId": "new"}])
        self.set_initial_revisions({"src/app.js": "v0"})
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            full = self.run_model(mode="cumulative-cold")
            selected = self.run_model(mode="cumulative-cold", scenario="step-8")
        self.assertEqual(selected["scenarios"], [full["scenarios"][7]])
        self.assertEqual(selected["selection"], {"scenarioId": "step-8", "evaluatedScenarioCount": 9,
                                                "displayedScenarioCount": 1})
        self.assert_history_reuse_totals(full)
        self.assert_history_reuse_totals(selected)
        for side in ("before", "after"):
            results = [s[side] for s in full["scenarios"]]
            self.assertEqual([len(s["cache"]["missInstructionIds"]) for s in results], [3, 2, 0, 0, 0, 2, 2, 0, 0])
            self.assertEqual([s["cache"]["entryCountAfter"] for s in results], [3, 5, 5, 5, 5, 7, 9, 9, 9])
            self.assertEqual(results[1]["steps"][-1]["syntheticStateOrigin"], None)
            self.assertEqual(results[2]["steps"][-1]["syntheticStateOrigin"], {"kind": "scenario", "scenarioId": "step-1"})
            self.assertEqual(results[2]["steps"][-1]["syntheticStateReuse"], "retained-history")
            self.assertEqual(results[3]["steps"][-1]["syntheticStateReuse"], "previous-build")
            self.assertEqual(results[7]["steps"][-1]["syntheticStateOrigin"], {"kind": "scenario", "scenarioId": "step-1"})
            self.assertEqual(results[8]["steps"][-1]["syntheticStateOrigin"], {"kind": "scenario", "scenarioId": "step-6"})
        self.assertEqual([(s["inventoryBeforeFileCount"], s["inventoryAfterFileCount"]) for s in full["scenarios"]],
                         [(4, 4)] * 5 + [(4, 5), (5, 5), (5, 4), (4, 5)])
        self.assertNotIn('"initial-warm"', json.dumps(full))
        # Equal instruction keys still live in two separate empty caches.
        self.after.write_text(self.before.read_text())
        self.history([], [])
        report = self.run_model(mode="cumulative-cold")
        self.assertEqual(report["scenarios"][0]["before"], report["scenarios"][0]["after"])
        self.assertEqual(report["scenarios"][0]["after"]["cache"]["entryCountBefore"], 0)

    def test_cumulative_cold_separate_sides_metadata_weights_and_selection(self):
        self.before.write_text("FROM scratch\nENV A=b\nCOPY . /app/\nRUN true\nWORKDIR /app\n")
        self.after.write_text("FROM scratch\nCOPY src /app/\nRUN true\n")
        self.history([], [{"path": "docs/note.txt", "kind": "content"}],
                     [{"path": "src/app.js", "kind": "mtime"}], [])
        for value in (0, 0.125):
            self.weights.write_text(json.dumps({"schemaVersion": 1,
                "before": {f"I{i:03d}": value for i in range(1, 6)},
                "after": {f"I{i:03d}": value for i in range(1, 4)}}))
            full = self.run_model(weighted=True, mode="cumulative-cold")
            self.assert_history_reuse_totals(full)
            self.assertEqual(full["scenarios"][1]["before"]["cache"]["missInstructionIds"], ["I003", "I004", "I005"])
            self.assertEqual(full["scenarios"][1]["after"]["cache"]["missInstructionIds"], [])
            self.assertEqual(full["scenarios"][1]["after"]["uncopiedChangePaths"], ["docs/note.txt"])
            for item in full["scenarios"]:
                selected = self.run_model(weighted=True, mode="cumulative-cold", scenario=item["id"])
                self.assertEqual(selected["scenarios"], [item])
                self.assert_history_reuse_totals(selected)
                self.assertEqual(selected["totals"]["syntheticReuseCounts"],
                                 {side: item[side]["syntheticReuseCounts"] for side in ("before", "after")})
        self.after.write_text("FROM scratch\n")
        report = self.run_model(mode="cumulative-cold")
        self.assertEqual(report["scenarios"][0]["after"]["cache"]["missInstructionIds"], ["I001"])
        self.assertIsNone(report["scenarios"][0]["after"]["firstInvalidatedCopy"])
        self.assertEqual(report["scenarios"][1]["after"]["cache"]["hitInstructionIds"], ["I001"])

    def test_cumulative_cold_atomic_errors_and_original_copy_validation(self):
        self.cold_pair()
        for event, phrase in (
            ({"path": "missing", "kind": "content"}, "unknown change path"),
            ({"path": "src/app.js", "kind": "mtime", "revisionId": "v1"}, "content/add"),
            ({"path": "src/app.js", "kind": "content", "revisionId": "v0"}, "unchanged revisionId"),
            ({"path": "src/app.js", "kind": "content", "revisionId": "bad id"}, "revisionId"),
            ({"path": "src/app.js", "kind": "mtime", "extra": True}, "fields"),
            ({"path": "src/app.js", "kind": "add"}, "already exists")):
            self.history([], [event])
            self.set_initial_revisions({"src/app.js": "v0"})
            with self.subTest(event=event):
                for output in ((), ("--json",)):
                    self.assert_cli_error("--mode", "cumulative-cold", "--scenario", "step-1", *output, phrase=phrase)
                with self.assertRaises(InputError):
                    self.run_model(mode="cumulative-cold", scenario="step-1")
        self.history([], [{"path": "src/app.js", "kind": "delete"}],
                     [{"path": "src/app.js", "kind": "add"}])
        self.after.write_text("FROM scratch\nCOPY src/app.js /app/\nRUN true\n")
        for output in ((), ("--json",)):
            self.assert_cli_error("--mode", "cumulative-cold", "--scenario", "step-1", *output, phrase="step-2 / after")
        self.after.write_text("FROM scratch\nCOPY future /app/\nRUN true\n")
        self.history([{"path": "future", "kind": "add"}])
        self.assert_cli_error("--mode", "cumulative-cold", "--json", phrase="matches no regular files")
        self.cold_pair()
        for steps in (([{"path": "new.js", "kind": "add"}], [{"path": "new.js", "kind": "add"}]),
                      ([{"path": "src/app.js", "kind": "delete"}], [{"path": "src/app.js", "kind": "mtime"}])):
            self.history(*steps)
            self.assert_cli_error("--mode", "cumulative-cold", "--scenario", "step-1", "--json")
        self.history([])
        for declarations in ([], {"missing": "v1"}, {"src/app.js": "bad id"}):
            self.set_initial_revisions(declarations)
            self.assert_cli_error("--mode", "cumulative-cold", "--json", phrase="initialRevisionIds")
        self.history([])
        self.set_initial_revisions({})
        self.assertEqual(self.run_model(mode="cumulative-cold")["initialRevisionIds"], {})
        for selector in ("unknown", "bad id"):
            self.assert_cli_error("--mode", "cumulative-cold", "--scenario", selector, "--json")
        for field in ("previousBuild", "syntheticStateOrigin", "syntheticStateReuse", "cache",
                      "syntheticReuseCounts", "syntheticReuseModelUnits"):
            data = json.loads(self.changes.read_text())
            data["scenarios"].append({"id": "bad", "changes": [], field: None})
            self.changes.write_text(json.dumps(data))
            self.assert_cli_error("--mode", "cumulative-cold", "--scenario", "step-1", "--json", phrase="fields")
            self.history([])
        for value in (-1, True, "1", 1e13):
            self.weights.write_text(json.dumps({"schemaVersion": 1, "before": {"I001": value}, "after": {}}))
            self.assert_cli_error("--mode", "cumulative-cold", "--weights", str(self.weights), "--json", phrase="finite number")

    def test_independent_cold_noop_content_and_report_contract(self):
        self.cold_pair()
        self.history([], [{"path": "src/app.js", "kind": "content"}], [],
                     [{"path": "src/app.js", "kind": "content"}])
        report = self.run_model(mode="independent-cold")
        self.assertEqual((report["schemaVersion"], report["mode"], report["model"]),
                         (1, "independent-cold", "linear-copy-v1"))
        self.assertFalse(report["actualCacheHitProven"])
        self.assertFalse(report["semanticEquivalenceProven"])
        self.assertIsNone(report["measuredBuildSeconds"])
        self.assertEqual(report["totals"], {"beforeModelUnits": 4, "afterModelUnits": 4, "savedModelUnits": 0})
        for index, scenario in enumerate(report["scenarios"]):
            self.assertNotIn("previousBuild", scenario)
            for side in ("before", "after"):
                result = scenario[side]
                self.assert_cold_result(result, ["I001", "I002", "I003"], 1)
                self.assertEqual(result["invalidatedRunIds"], ["I003"])
                self.assertEqual(result["firstAffectedCopy"], "I002" if index % 2 else None)
                self.assertEqual(result["firstInvalidatedCopy"], "I002")
                self.assertEqual(result["steps"][1]["matchedChanges"], result["triggers"])
        self.assertEqual(report["scenarios"][0]["before"], report["scenarios"][2]["before"])
        self.assertEqual(report["scenarios"][1]["after"], report["scenarios"][3]["after"])
        result = self.cli("--mode", "independent-cold", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), report)
        text = self.cli("--mode", "independent-cold")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Mode: independent-cold", text.stdout)
        self.assertEqual(text.stdout.count("synthetic cold cache: 0 hits, 3 misses; "
                                          "entries 0 -> 3 (discarded after scenario)"), 8)
        self.assertIn("I001 FROM scratch: synthetic-miss", text.stdout)
        self.assertNotIn("retained entries", text.stdout)
        self.assertNotIn("synthetic reuse", text.stdout)
        self.assertNotIn("synthetic state origin", text.stdout)
        self.assertIn("not Docker pulls, network work, real BuildKit cache keys or timing", text.stdout)

    def test_independent_cold_original_inventory_mtime_uncopied_add_delete(self):
        self.cold_pair()
        added = [{"path": "src/new.js", "kind": "add"}]
        deleted = [{"path": "src/app.js", "kind": "delete"}]
        # Keep src's required selection valid after deleting its other file.
        (self.context / "src/keep.js").write_text("inventory only")
        self.history([{"path": "src/app.js", "kind": "mtime"}],
                     [{"path": "docs/note.txt", "kind": "content"}], added, added, deleted, deleted, [])
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be read or written")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open), patch("subprocess.Popen", side_effect=AssertionError("no execution")):
            report = self.run_model(mode="independent-cold")
        self.assertEqual([(s["inventoryBeforeFileCount"], s["inventoryAfterFileCount"])
                          for s in report["scenarios"]], [(5, 5), (5, 5), (5, 6), (5, 6), (5, 4), (5, 4), (5, 5)])
        for scenario in report["scenarios"]:
            for side in ("before", "after"):
                self.assert_cold_result(scenario[side], ["I001", "I002", "I003"], 1)
        mtime, uncopied, add, _, delete = report["scenarios"][:5]
        self.assertEqual(mtime["after"]["ignoredMtimePaths"], ["src/app.js"])
        self.assertEqual(mtime["after"]["triggers"], [])
        self.assertEqual(uncopied["after"]["uncopiedChangePaths"], ["docs/note.txt"])
        self.assertIsNone(uncopied["after"]["firstAffectedCopy"])
        self.assertEqual(uncopied["before"]["firstAffectedCopy"], "I002")
        self.assertEqual(add["after"]["triggers"][0]["path"], "src/new.js")
        self.assertEqual(delete["after"]["triggers"][0]["path"], "src/app.js")
        self.assertEqual(add["after"], report["scenarios"][3]["after"])
        self.assertEqual(delete["before"], report["scenarios"][5]["before"])

    def test_independent_cold_weights_metadata_and_no_copy(self):
        self.cold_pair()
        self.history([], [{"path": "src/app.js", "kind": "content"}])
        for before, after in (({"I001": 0.25, "I002": 0, "I003": 1.5},
                               {"I001": 2, "I002": 0.5, "I003": 0}),
                              ({"I001": 0, "I002": 0, "I003": 0}, {"I001": 1e12})):
            with self.subTest(before=before, after=after):
                self.weights.write_text(json.dumps({"schemaVersion": 1, "before": before, "after": after}))
                report = self.run_model(weighted=True, mode="independent-cold")
                for side in ("before", "after"):
                    expected = sum(i["weightModelUnits"] for i in report["dockerfiles"][side]["instructions"])
                    for scenario in report["scenarios"]:
                        self.assert_cold_result(scenario[side], ["I001", "I002", "I003"], expected)
                    self.assertEqual(report["totals"][side + "ModelUnits"], expected * 2)
        self.before.write_text("FROM scratch\nENV X=fixed\nARG Y=fixed\nWORKDIR /app\nRUN echo inert\n")
        self.after.write_text("FROM scratch\n")
        self.weights.write_text(json.dumps({"schemaVersion": 1, "before":
                                           {"I001": 0.1, "I002": 0.2, "I003": 0.3, "I004": 0.4, "I005": 0},
                                           "after": {"I001": 2.5}}))
        scenario = self.run_model(weighted=True, mode="independent-cold")["scenarios"][0]
        self.assert_cold_result(scenario["before"], [f"I{i:03d}" for i in range(1, 6)], 1.0)
        self.assert_cold_result(scenario["after"], ["I001"], 2.5)
        for side in ("before", "after"):
            self.assertIsNone(scenario[side]["firstAffectedCopy"])
            self.assertIsNone(scenario[side]["firstInvalidatedCopy"])

    def test_independent_cold_selection_is_presentation_only(self):
        self.cold_pair()
        self.history([], [{"path": "src/app.js", "kind": "content"}], [])
        full = self.run_model(mode="independent-cold")
        selected = self.run_model(mode="independent-cold", scenario="step-2")
        self.assertEqual(selected["scenarios"], [full["scenarios"][1]])
        self.assertEqual(selected["selection"], {"scenarioId": "step-2", "evaluatedScenarioCount": 3,
                                                "displayedScenarioCount": 1})
        self.assertEqual(selected["totals"], {"beforeModelUnits": 1, "afterModelUnits": 1, "savedModelUnits": 0})
        for output in ((), ("--json",)):
            result = self.cli("--mode", "independent-cold", "--scenario", "step-2", *output)
            self.assertEqual(result.returncode, 0, result.stderr)
            if output:
                self.assertEqual(json.loads(result.stdout), selected)
            else:
                self.assertIn("Report filter: scenario step-2 (1 displayed / 3 evaluated)", result.stdout)
                self.assertIn("Total (displayed scenario): before=1, after=1, saved=0", result.stdout)
                self.assertNotIn("Scenario step-1", result.stdout)
        for selector in ("missing", "STEP-2", "bad/id"):
            with self.subTest(selector=selector):
                self.assert_cli_error("--mode", "independent-cold", "--scenario", selector, "--json")
                with self.assertRaises(InputError):
                    self.run_model(mode="independent-cold", scenario=selector)

    def test_independent_cold_invalid_later_inputs_remain_atomic(self):
        self.cold_pair()
        for events, phrase in (([{"path": "src/app.js", "kind": "delete"}], "matches no regular files"),
                               ([{"path": "missing", "kind": "content"}], "unknown change path"),
                               ([{"path": "src/app.js", "kind": "add"}], "already exists"),
                               ([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}], "requires cumulative-history"),
                               ([{"path": "src/app.js", "kind": "content", "extra": 1}], "expected exactly fields")):
            with self.subTest(events=events):
                self.history([], events)
                for output in ((), ("--json",)):
                    self.assert_cli_error("--mode", "independent-cold", "--scenario", "step-1", *output, phrase=phrase)
                with self.assertRaisesRegex(InputError, phrase):
                    self.run_model(mode="independent-cold", scenario="step-1")
        for kind in ("content", "delete", "mtime"):
            self.history([], [{"path": "new.js", "kind": "add"}], [{"path": "new.js", "kind": kind}])
            self.assert_cli_error("--mode", "independent-cold", "--scenario", "step-1", "--json", phrase="unknown change path")
        self.history([])
        self.set_initial_revisions({})
        self.assert_cli_error("--mode", "independent-cold", "--json", phrase="initialRevisionIds requires cumulative-history")
        self.history([])
        self.before.write_text("FROM scratch\nCOPY missing /app/\nRUN echo inert\n")
        self.assert_cli_error("--mode", "independent-cold", "--json", phrase="matches no regular files")
        self.before.write_text("FROM scratch\nCOPY --from=other src /app/\n")
        self.assert_cli_error("--mode", "independent-cold", "--json", phrase="COPY flags")
        self.cold_pair()
        for value in (-1, True, 1e13, "1"):
            self.weights.write_text(json.dumps({"schemaVersion": 1, "before": {"I001": value}, "after": {}}))
            self.assert_cli_error("--mode", "independent-cold", "--weights", str(self.weights), "--json", phrase="finite number")

    def test_installed_cli_acceptance_and_weighted_experiment(self):
        result = self.cli("--json", "--weights", str(EXAMPLES / "weights.json"),
                          changes=EXAMPLES / "changes.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        self.assertEqual(report["totals"], {"beforeModelUnits": 440, "afterModelUnits": 140, "savedModelUnits": 300})
        self.assertIsNone(report["measuredBuildSeconds"])
        self.assertFalse(report["semanticEquivalenceProven"])
        self.assertTrue(report["dockerfiles"]["before"]["mutableFrom"])
        expected = [(110, 10), (110, 110), (110, 10), (110, 10), (0, 0)]
        self.assertEqual([(s["before"]["invalidatedModelUnits"], s["after"]["invalidatedModelUnits"])
                          for s in report["scenarios"]], expected)
        source = report["scenarios"][0]
        self.assertEqual(source["before"]["invalidatedInstructionIds"], ["I003", "I004", "I005"])
        self.assertEqual(source["after"]["invalidatedRunIds"], ["I006"])
        self.assertEqual(source["before"]["reasonChain"][1]["causedBy"], ["I003"])
        self.assertEqual(source["before"]["reasonChain"][0]["causedBy"][0]["path"], "src/app.js")
        self.assertIn("secret values", " ".join(report["unmodeled"]))

    def test_cli_acceptance_default_unit_totals(self):
        result = self.cli("--json", changes=EXAMPLES / "changes.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["totals"], {"beforeModelUnits": 8, "afterModelUnits": 5, "savedModelUnits": 3})

    def test_cli_text_explanation_and_determinism(self):
        first, second = self.cli(), self.cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("src/app.js (content) -> I003 COPY .", first.stdout)
        self.assertIn("I003 -> I004 -> I005", first.stdout)
        self.assertIn("not measured", first.stdout)
        self.assertIn("Mutable FROM", first.stdout)

    def test_step_explanations_source_and_dependency_changes(self):
        report = compare(self.before, self.after, EXAMPLES / "changes.json", self.context)
        source, manifest = report["scenarios"][:2]
        self.assertFalse(report["actualCacheHitProven"])
        for scenario, root_copy, run_ids in ((source, "I005", ["I006"]),
                                             (manifest, "I003", ["I004", "I006"])):
            result = scenario["after"]
            steps = result["steps"]
            self.assertEqual([s["instructionId"] for s in steps], [f"I{i:03d}" for i in range(1, 7)])
            self.assertEqual(result["firstInvalidatedCopy"], root_copy)
            self.assertEqual([s["instructionId"] for s in steps if s["op"] == "RUN" and
                              s["decision"] == "invalidated"], run_ids)
            self.assertTrue(all(s["why"] for s in steps))
            self.assertEqual([s["instructionId"] for s in steps if s["decision"] == "invalidated"],
                             result["invalidatedInstructionIds"])
        self.assertEqual([s["decision"] for s in source["after"]["steps"]],
                         ["unaffected"] * 4 + ["invalidated"] * 2)
        self.assertEqual(source["after"]["steps"][2]["matchedChanges"], [])
        self.assertEqual(source["after"]["steps"][4]["matchedChanges"],
                         [{"path": "src/app.js", "kind": "content", "copyId": "I005", "source": "."}])
        self.assertEqual(source["after"]["steps"][5]["causedByInstructionId"], "I005")
        self.assertEqual(manifest["after"]["steps"][2]["matchedChanges"][0]["path"], "package.json")
        text = self.cli().stdout
        self.assertIn("I004 RUN npm ci: unaffected", text)
        self.assertIn("I006 RUN npm run build: invalidated", text)
        self.assertIn("why:", text)
        self.assertIn("Actual cache hits: unknown", text)

    def test_step_explanations_noop_mtime_and_uncopied_are_not_hit_proof(self):
        self.after.write_text("FROM node:22\nCOPY src /app/\nRUN echo inert\n")
        for events in ([], [{"path": "src/app.js", "kind": "mtime"}],
                       [{"path": "package.json", "kind": "content"}]):
            with self.subTest(events=events):
                self.events(events)
                report = self.run_model()
                result = report["scenarios"][0]["after"]
                self.assertIsNone(result["firstInvalidatedCopy"])
                self.assertEqual([s["decision"] for s in result["steps"]], ["unaffected"] * 3)
                self.assertTrue(all("external factors" in s["why"] for s in result["steps"]))
                self.assertFalse(report["actualCacheHitProven"])

    def test_step_explanations_history_distinguish_affected_copy_from_miss(self):
        dockerfile = "FROM scratch\nCOPY docs /docs/\nRUN echo docs\nCOPY src /src/\nRUN echo src\n"
        self.before.write_text(dockerfile)
        self.after.write_text(dockerfile)
        self.history([{"path": "docs/new.txt", "kind": "add"}],
                     [{"path": "docs/new.txt", "kind": "delete"}, {"path": "src/app.js", "kind": "content"}],
                     [])
        report = self.run_model(mode="cumulative-history")
        result = report["scenarios"][1]["after"]
        self.assertEqual(result["firstAffectedCopy"], "I002")
        self.assertEqual(result["firstInvalidatedCopy"], "I004")
        self.assertEqual([s["decision"] for s in result["steps"]],
                         ["synthetic-hit"] * 3 + ["synthetic-miss"] * 2)
        self.assertEqual(result["steps"][1]["matchedChanges"][0]["path"], "docs/new.txt")
        self.assertIn("retained synthetic", result["steps"][1]["why"])
        self.assertEqual(result["steps"][4]["causedByInstructionId"], "I004")
        self.assertIsNone(report["scenarios"][2]["after"]["firstInvalidatedCopy"])
        self.assertFalse(report["actualCacheHitProven"])

    def test_candidate_diff_installed_cli_confirmation_and_read_only(self):
        original = {p: p.read_bytes() for p in (self.before, self.after)}
        first, second = self.cli("--json"), self.cli("--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        candidate = json.loads(first.stdout)["candidateComparison"]
        self.assertEqual(candidate["source"], "caller-supplied")
        self.assertTrue(candidate["requiresSemanticEquivalenceConfirmation"])
        self.assertEqual(candidate["diff"],
                         '--- before/Dockerfile\n+++ after/Dockerfile\n@@ -1,5 +1,6 @@\n'
                         ' FROM node:22\n WORKDIR /app\n'
                         '+COPY ["package.json", "package-lock.json", "./"]\n+RUN npm ci\n'
                         ' COPY . .\n-RUN npm ci\n RUN npm run build\n')
        self.assertIn(candidate["diff"].rstrip(), self.cli().stdout)
        self.assertIn("User must confirm", self.cli().stdout)
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        # Reporting a diff must not let an invalid later scenario leak partial stdout.
        self.history([], [{"path": "package-lock.json", "kind": "delete"}])
        self.assert_cli_error("--json", phrase="matches no regular files")

    def test_candidate_diff_same_file_comments_continuations_and_missing_newline(self):
        self.before.write_text("FROM scratch\nCOPY src /src/\nRUN echo inert")
        self.after.write_bytes(self.before.read_bytes())
        self.assertEqual(self.run_model()["candidateComparison"]["diff"], "")
        self.after.write_text("# candidate comment\nFROM scratch\nCOPY src/ap\\\np.js /src/\nRUN echo inert\n")
        diff = self.run_model()["candidateComparison"]["diff"]
        self.assertIn("+# candidate comment\n", diff)
        self.assertIn("+COPY src/ap\\\n+p.js /src/\n", diff)
        self.assertIn("-RUN echo inert\n\\ No newline at end of file\n", diff)
        self.assertIn("+RUN echo inert\n", diff)

    def test_continuation_comments_json_copy_literal_env_arg(self):
        self.after.write_text('FROM node:22\nARG MODE=dev\nENV X="two words" Y=literal\nWORKDIR /app\n'
                              'COPY package.json \\\n# this comment is skipped\n package-lock.json ./\n'
                              'RUN ["echo", "do not run"]\nCOPY ["src", "/app/src/"]\nRUN echo $X\n')
        report = self.run_model()
        items = report["dockerfiles"]["after"]["instructions"]
        self.assertEqual(items[4]["sources"], ["package.json", "package-lock.json"])
        self.assertEqual(items[4]["line"], 5)
        self.assertEqual(items[5]["line"], 8)
        self.assertEqual(report["scenarios"][0]["after"]["firstAffectedCopy"], "I007")
        self.assertEqual(report["scenarios"][0]["after"]["invalidatedRunIds"], ["I008"])

    def test_component_glob_matches_manifest_and_new_file(self):
        self.after.write_text("FROM node:22\nCOPY package*.json /app/\nRUN npm ci\nCOPY src/ /app/src/\nRUN echo build\n")
        for changes, first in [([{"path": "package-lock.json", "kind": "content"}], "I002"),
                               ([{"path": "package-extra.json", "kind": "add"}], "I002"),
                               ([{"path": "src/new.js", "kind": "add"}], "I004")]:
            with self.subTest(changes=changes):
                self.events(changes)
                report = self.run_model()
                self.assertEqual(report["scenarios"][0]["after"]["firstAffectedCopy"], first)

    def test_wildcard_multiple_matches_require_destination_slash(self):
        self.after.write_text("FROM scratch\nCOPY package*.json /app\n")
        self.assert_cli_error("--json", phrase="multiple resources")
        # A previously singular match becoming multiple is invalid as well.
        self.after.write_text("FROM scratch\nCOPY src/*.js /app\n")
        self.events([{"path": "src/new.js", "kind": "add"}])
        self.assert_cli_error("--json", phrase="multiple resources")

    def test_glob_must_not_match_synthetic_inventory_root(self):
        self.after.write_text("FROM scratch\nCOPY ? /app/\n")
        self.assert_cli_error("--json", phrase="matches no regular files")

    def test_continuation_does_not_insert_source_characters(self):
        self.after.write_text("FROM scratch\nCOPY src/ap\\\np.js /app/\nRUN echo inert\n")
        self.assertEqual(self.run_model()["scenarios"][0]["after"]["firstAffectedCopy"], "I002")
        (self.context / "space  file.txt").write_text("synthetic")
        self.after.write_text('FROM scratch\nCOPY "space \\\n file.txt" /app/\nRUN echo inert\n')
        self.events([{"path": "space  file.txt", "kind": "content"}])
        self.assertEqual(self.run_model()["scenarios"][0]["after"]["triggers"][0]["source"], "space  file.txt")

    def test_glob_directory_descendants_but_not_across_separator(self):
        (self.context / "src/deep").mkdir()
        (self.context / "src/deep/file.js").write_text("synthetic")
        self.after.write_text("FROM scratch\nCOPY src/*.js /app/\nRUN echo inert\n")
        self.events([{"path": "src/deep/file.js", "kind": "content"}])
        report = self.run_model()
        self.assertEqual(report["scenarios"][0]["after"]["invalidatedInstructionIds"], [])
        self.assertEqual(report["scenarios"][0]["after"]["uncopiedChangePaths"], ["src/deep/file.js"])
        self.after.write_text("FROM scratch\nCOPY src/* /app/\nRUN echo inert\n")
        self.assertEqual(self.run_model()["scenarios"][0]["after"]["invalidatedRunIds"], ["I003"])

    def test_question_glob_case_sensitivity_and_hidden_files(self):
        (self.context / "src/a.js").write_text("synthetic")
        (self.context / "src/A.JS").write_text("synthetic")
        (self.context / ".hidden").write_text("synthetic")
        self.after.write_text("FROM scratch\nCOPY src/?.js /app/\nRUN echo inert\n")
        self.events([{"path": "src/A.JS", "kind": "content"}])
        self.assertEqual(self.run_model()["scenarios"][0]["after"]["invalidatedRunIds"], [])
        self.events([{"path": ".hidden", "kind": "content"}])
        self.assertEqual(self.run_model()["scenarios"][0]["before"]["invalidatedRunIds"], ["I004", "I005"])
        self.events([{"path": "src/a.js", "kind": "content"}])
        self.assertEqual(self.run_model()["scenarios"][0]["after"]["invalidatedRunIds"], ["I003"])

    def test_json_and_shell_paths_with_spaces(self):
        (self.context / "space file.txt").write_text("synthetic")
        for copy_line in ('COPY "space file.txt" /app/', 'COPY ["space file.txt", "/app/"]'):
            with self.subTest(copy_line=copy_line):
                self.after.write_text("FROM scratch\n" + copy_line + "\nRUN echo inert\n")
                self.events([{"path": "space file.txt", "kind": "content"}])
                self.assertEqual(self.run_model()["scenarios"][0]["after"]["firstAffectedCopy"], "I002")

    def test_delete_glob_and_rename(self):
        self.after.write_text("FROM node:22\nCOPY package*.json /app/\nRUN npm ci\n")
        self.events([{"path": "package-lock.json", "kind": "delete"}])
        after = self.run_model()["scenarios"][0]["after"]
        self.assertEqual(after["triggers"][0]["kind"], "delete")
        self.assertEqual(after["invalidatedRunIds"], ["I003"])
        self.events([{"path": "src/app.js", "kind": "delete"}, {"path": "src/renamed.js", "kind": "add"}])
        self.assertEqual(self.run_model()["scenarios"][0]["before"]["firstAffectedCopy"], "I003")

    def test_mtime_and_content_mix_and_noop(self):
        self.events([{"path": "package.json", "kind": "mtime"}, {"path": "src/app.js", "kind": "content"}])
        after = self.run_model()["scenarios"][0]["after"]
        self.assertEqual(after["firstAffectedCopy"], "I005")
        self.assertEqual(after["ignoredMtimePaths"], ["package.json"])
        self.events([])
        self.assertEqual(self.run_model()["totals"]["savedModelUnits"], 0)

    def test_independent_scenarios_not_cumulative(self):
        self.changes.write_text(json.dumps({"schemaVersion": 1, "scenarios": [
            {"id": "one", "changes": [{"path": "src/new.js", "kind": "add"}]},
            {"id": "two", "changes": [{"path": "src/new.js", "kind": "add"}]}]}))
        report = self.run_model()
        self.assertEqual(report["scenarios"][0]["after"], report["scenarios"][1]["after"])

    def history(self, *steps):
        self.changes.write_text(json.dumps({"schemaVersion": 1, "scenarios": [
            {"id": f"step-{index}", "changes": changes} for index, changes in enumerate(steps, 1)]}))

    def test_history_acceptance_installed_cli_and_cache_restore(self):
        result = self.cli("--mode", "cumulative-history", "--json", "--weights", str(EXAMPLES / "weights.json"),
                          changes=EXAMPLES / "changes.history.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, compare(self.before, self.after, EXAMPLES / "changes.history.json", self.context,
                                         EXAMPLES / "weights.json", mode="cumulative-history"))
        self.assertEqual(report["mode"], "cumulative-history")
        self.assertEqual([(s["before"]["invalidatedModelUnits"], s["after"]["invalidatedModelUnits"])
                          for s in report["scenarios"]], [(110, 10), (110, 110), (110, 10), (0, 0), (0, 0)])
        self.assertEqual({k: report["totals"][k] for k in ("beforeModelUnits", "afterModelUnits", "savedModelUnits")},
                         {"beforeModelUnits": 330, "afterModelUnits": 130, "savedModelUnits": 200})
        self.assert_history_reuse_totals(report)
        self.assertEqual([(s["inventoryBeforeFileCount"], s["inventoryAfterFileCount"])
                          for s in report["scenarios"]], [(4, 4), (4, 4), (4, 5), (5, 4), (4, 4)])
        for side, instruction_count in (("before", 5), ("after", 6)):
            restored, noop = report["scenarios"][3][side], report["scenarios"][4][side]
            self.assertIsNotNone(restored["firstAffectedCopy"])
            self.assertEqual(restored["invalidatedInstructionIds"], [])
            self.assertEqual(restored["reasonChain"], [])
            self.assertEqual(restored["triggers"][0]["kind"], "delete")
            self.assertEqual(len(restored["cache"]["hitInstructionIds"]), instruction_count)
            self.assertEqual(restored["cache"]["entryCountBefore"], restored["cache"]["entryCountAfter"])
            self.assertEqual(noop["cache"], restored["cache"])
        for side, counts in (("before", [(5, 8), (8, 11), (11, 14), (14, 14), (14, 14)]),
                             ("after", [(6, 8), (8, 12), (12, 14), (14, 14), (14, 14)])):
            self.assertEqual([(s[side]["cache"]["entryCountBefore"], s[side]["cache"]["entryCountAfter"])
                              for s in report["scenarios"]], counts)
        self.assertIsNone(report["measuredBuildSeconds"])
        self.assertFalse(report["semanticEquivalenceProven"])

    def test_mode_labels_default_and_explicit_independent(self):
        default = self.run_model()
        self.assertEqual(default["mode"], "independent-warm")
        self.assertEqual(default, self.run_model(mode="independent-warm"))
        for mode in ("independent-warm", "cumulative-history"):
            result = self.cli("--mode", mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Mode: {mode}", result.stdout)
        with self.assertRaisesRegex(InputError, "mode"):
            self.run_model(mode="unknown")
        self.assertEqual(self.cli("--mode", "unknown").returncode, 2)

    def test_scenario_filter_history_preserves_preceding_inventory_and_cache(self):
        fixture = EXAMPLES / "changes.history.json"
        full = compare(self.before, self.after, fixture, self.context,
                       EXAMPLES / "weights.json", mode="cumulative-history")
        selected = compare(self.before, self.after, fixture, self.context,
                           EXAMPLES / "weights.json", mode="cumulative-history",
                           scenario="remove-added-source")
        self.assertNotIn("selection", full)
        self.assertEqual(selected, {**full, "scenarios": [full["scenarios"][3]],
                                    "selection": {"scenarioId": "remove-added-source",
                                                  "evaluatedScenarioCount": 5, "displayedScenarioCount": 1},
                                    "totals": {"beforeModelUnits": 0, "afterModelUnits": 0, "savedModelUnits": 0,
                                               **{field: {side: full["scenarios"][3][side][field]
                                                          for side in ("before", "after")}
                                                  for field in ("syntheticReuseCounts", "syntheticReuseModelUnits")},
                                               "syntheticMissesByInstruction": {
                                                   side: [{"instructionId": i["id"], "missCount": 0, "missedModelUnits": 0,
                                                           "missedScenarioIds": []}
                                                          for i in full["dockerfiles"][side]["instructions"]]
                                                   for side in ("before", "after")},
                                               "syntheticHistoryReusesByInstruction": {
                                                   side: [{"instructionId": i["id"], "reuseCount": int(n >= prefix),
                                                           "reusedModelUnits": i["weightModelUnits"] if n >= prefix else 0,
                                                           "reusedScenarioIds": ["remove-added-source"] if n >= prefix else []}
                                                          for n, i in enumerate(full["dockerfiles"][side]["instructions"])]
                                                   for side, prefix in (("before", 2), ("after", 4))},
                                               "syntheticHistoryReusesByOrigin": {
                                                   "before": [{"origin": {"kind": "scenario", "scenarioId": "manifest-edit"}, "reuseCount": 3, "reusedModelUnits": 110}],
                                                   "after": [{"origin": {"kind": "scenario", "scenarioId": "manifest-edit"}, "reuseCount": 2, "reusedModelUnits": 10}]}}})
        item = selected["scenarios"][0]
        self.assertEqual((item["inventoryBeforeFileCount"], item["inventoryAfterFileCount"]), (5, 4))
        for side, count in (("before", 5), ("after", 6)):
            self.assertEqual(len(item[side]["steps"]), count)
            self.assertTrue(all(s["decision"] == "synthetic-hit" for s in item[side]["steps"]))
            self.assertIsNone(item[side]["firstInvalidatedCopy"])
            self.assertIsNotNone(item[side]["firstAffectedCopy"])
        result = self.cli("--mode", "cumulative-history", "--scenario", "remove-added-source", "--json",
                          "--weights", str(EXAMPLES / "weights.json"), changes=fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), selected)

    def test_scenario_filter_revision_restore_and_text_explanations(self):
        fixture = EXAMPLES / "changes.revisions.json"
        report = compare(self.before, self.after, fixture, self.context,
                         mode="cumulative-history", scenario="revert-v1")
        self.assertEqual([s["id"] for s in report["scenarios"]], ["revert-v1"])
        for side in ("before", "after"):
            restored = report["scenarios"][0][side]
            self.assertTrue(all(s["decision"] == "synthetic-hit" for s in restored["steps"]))
            self.assertTrue(all(t["revisionId"] == "source-v1" for t in restored["triggers"]))
        text = self.cli("--mode", "cumulative-history", "--scenario", "revert-v1", changes=fixture)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Report filter: scenario revert-v1 (1 displayed / 5 evaluated)", text.stdout)
        self.assertEqual([line for line in text.stdout.splitlines() if line.startswith("Scenario ")],
                         ["Scenario revert-v1"])
        self.assertIn(report["candidateComparison"]["diff"].rstrip("\n"), text.stdout)
        self.assertIn(report["candidateComparison"]["notice"], text.stdout)
        self.assertIn("revisionId=source-v1", text.stdout)
        for side in ("before", "after"):
            for step in report["scenarios"][0][side]["steps"]:
                self.assertIn(f"{step['instructionId']} {step['op']} {step['argument']}: {step['decision']}", text.stdout)
                self.assertIn("why: " + step["why"], text.stdout)
        self.assertIn("Total (displayed scenario): before=0, after=0, saved=0 model units", text.stdout)

    def test_scenario_filter_independent_totals_exact_ids_and_default_unchanged(self):
        fixture = EXAMPLES / "changes.json"
        full = compare(self.before, self.after, fixture, self.context, EXAMPLES / "weights.json")
        self.assertEqual(full, compare(self.before, self.after, fixture, self.context,
                                      EXAMPLES / "weights.json", scenario=None))
        selected = compare(self.before, self.after, fixture, self.context,
                           EXAMPLES / "weights.json", scenario="source-edit")
        self.assertEqual(selected, {**full, "scenarios": [full["scenarios"][0]],
                                    "selection": {"scenarioId": "source-edit", "evaluatedScenarioCount": 5,
                                                  "displayedScenarioCount": 1},
                                    "totals": {"beforeModelUnits": 110, "afterModelUnits": 10, "savedModelUnits": 100}})
        # Exact matching also accepts IDs at both grammar boundaries.
        for sid in ("A", "A_0.-" + "z" * 75):
            self.events([], sid=sid)
            self.assertEqual(self.run_model(scenario=sid)["scenarios"][0]["id"], sid)
            with self.assertRaisesRegex(InputError, "unknown scenario ID"):
                self.run_model(scenario=sid.lower())

    def test_scenario_filter_rejects_invalid_unknown_and_missing_selectors(self):
        for value in (False, 1, [], {}, "", "x" * 81, "two words", "é", "test\n", "x/y"):
            with self.subTest(value=value), self.assertRaisesRegex(InputError, "scenario filter"):
                self.run_model(scenario=value)
        for value in ("missing", "Test", "te", "test*", "", "x" * 81):
            with self.subTest(value=value):
                for output in ((), ("--json",)):
                    self.assert_cli_error("--scenario", value, *output, phrase="scenario")
        missing = self.cli("--scenario")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")
        self.assertIn("expected one argument", missing.stderr)

    def test_scenario_filter_invalid_unselected_input_is_atomic(self):
        for mode in ("independent-warm", "cumulative-history"):
            for event, phrase in (({"path": "missing", "kind": "content"}, "step-2: unknown change path"),
                                  ({"path": "package-lock.json", "kind": "delete"}, "step-2 / after")):
                self.history([], [event])
                for output in ((), ("--json",)):
                    with self.subTest(mode=mode, event=event, output=output):
                        self.assert_cli_error("--mode", mode, "--scenario", "step-1", *output, phrase=phrase)
                with self.assertRaisesRegex(InputError, phrase):
                    self.run_model(mode=mode, scenario="step-1")
        # Contradictions are discovered during replay, beyond schema validation.
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}])
        for output in ((), ("--json",)):
            self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output,
                                  phrase="step-2: content event has unchanged revisionId")
        # Invalid preceding history cannot be hidden by selecting a later scenario.
        self.history([{"path": "package-lock.json", "kind": "delete"}], [])
        self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-2", "--json",
                              phrase="step-1 / after")

    def test_history_add_edit_mtime_delete_and_noop(self):
        self.history([{"path": "new/child.js", "kind": "add"}],
                     [{"path": "new/child.js", "kind": "content"}],
                     [{"path": "new/child.js", "kind": "mtime"}],
                     [{"path": "new/child.js", "kind": "delete"}], [])
        report = self.run_model(mode="cumulative-history")
        self.assertEqual([s["before"]["invalidatedModelUnits"] for s in report["scenarios"]], [2, 2, 0, 0, 0])
        self.assertEqual(report["scenarios"][2]["before"]["ignoredMtimePaths"], ["new/child.js"])
        self.assertEqual(report["scenarios"][-1]["inventoryAfterFileCount"], 4)
        self.assertFalse((self.context / "new").exists())
        with self.assertRaisesRegex(InputError, "unknown change path"):
            self.run_model()  # The added file is absent from the independent initial inventory.

    def test_history_content_and_readded_files_get_new_revisions(self):
        for steps in (("content", "content"), ("delete", "add")):
            with self.subTest(steps=steps):
                self.history(*[[{"path": "src/app.js", "kind": kind}] for kind in steps])
                report = self.run_model(mode="cumulative-history")
                self.assertEqual([s["before"]["invalidatedModelUnits"] for s in report["scenarios"]], [2, 2])
                for side in ("before", "after"):
                    self.assertGreater(report["scenarios"][1][side]["cache"]["entryCountAfter"],
                                       report["scenarios"][0][side]["cache"]["entryCountAfter"])

    def set_initial_revisions(self, declarations):
        data = json.loads(self.changes.read_text())
        data["initialRevisionIds"] = declarations
        self.changes.write_text(json.dumps(data))

    def test_previous_build_full_order_selection_and_no_content_reads(self):
        self.history([], [{"path": "src/app.js", "kind": "mtime"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}], [],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            report = self.run_model(mode="cumulative-history")
            for index, item in enumerate(report["scenarios"]):
                with self.subTest(scenario=item["id"]):
                    expected = ({"kind": "initial-warm"} if index == 0 else
                                {"kind": "scenario", "scenarioId": f"step-{index}"})
                    self.assertEqual(item["previousBuild"], expected)
                    selected = self.run_model(mode="cumulative-history", scenario=item["id"])
                    self.assertEqual(selected["scenarios"], [item])
        # Predecessor follows build order even when reuse/origin refer further back.
        for index, reuse, origin in ((3, "retained-history", {"kind": "initial-warm"}),
                                    (4, "previous-build", {"kind": "initial-warm"}),
                                    (5, "retained-history", {"kind": "scenario", "scenarioId": "step-3"}),
                                    (6, "previous-build", {"kind": "scenario", "scenarioId": "step-3"})):
            for side in ("before", "after"):
                step = report["scenarios"][index][side]["steps"][-1]
                self.assertEqual(step["syntheticStateReuse"], reuse)
                self.assertEqual(step["syntheticStateOrigin"], origin)

    def test_previous_build_anonymous_misses_and_literal_scenario_ids(self):
        self.before.write_text("FROM scratch\nCOPY . /app/\nRUN true\n")
        self.after.write_text("FROM scratch\nCOPY src /app/\nRUN true\n")
        self.changes.write_text(json.dumps({"schemaVersion": 1, "scenarios": [
            {"id": "initial-warm", "changes": [{"path": "src/app.js", "kind": "content"}]},
            {"id": "Build.2", "changes": [{"path": "src/app.js", "kind": "content"}]},
            {"id": "LAST", "changes": []}]}))
        report = self.run_model(mode="cumulative-history")
        self.assertEqual([item["previousBuild"] for item in report["scenarios"]],
                         [{"kind": "initial-warm"},
                          {"kind": "scenario", "scenarioId": "initial-warm"},
                          {"kind": "scenario", "scenarioId": "Build.2"}])
        for side in ("before", "after"):
            for item in report["scenarios"][:2]:
                self.assertEqual(item[side]["cache"]["missInstructionIds"], ["I002", "I003"])
                self.assertEqual([step["syntheticStateReuse"] for step in item[side]["steps"]],
                                 ["previous-build", None, None])
            self.assertEqual(report["scenarios"][2][side]["steps"][-1]["syntheticStateOrigin"],
                             {"kind": "scenario", "scenarioId": "Build.2"})

    def test_previous_build_text_selected_and_independent_omission(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        for sid, label in (("edit-v2", "initial-warm"), ("revert-initial", "scenario edit-v2"),
                           ("readd-initial", "scenario delete-source")):
            result = self.cli("--mode", "cumulative-history", "--scenario", sid, changes=fixture)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count("  previous modeled build: "), 1)
            self.assertIn(f"  previous modeled build: {label}\n", result.stdout)
        self.assertTrue(all("previousBuild" not in item for item in self.run_model()["scenarios"]))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("previous modeled build:", result.stdout)

    def test_previous_build_later_errors_and_output_only_field(self):
        for event, phrase in (({"path": "src/app.js", "kind": "content", "revisionId": "v0"}, "unchanged revisionId"),
                              ({"path": "package-lock.json", "kind": "delete"}, "COPY source")):
            self.history([], [event])
            self.set_initial_revisions({"src/app.js": "v0"})
            with self.assertRaisesRegex(InputError, phrase):
                self.run_model(mode="cumulative-history", scenario="step-1")
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output, phrase=phrase)
        # The report field never becomes accepted input at either level.
        for level in ("document", "scenario"):
            self.history([])
            data = json.loads(self.changes.read_text())
            target = data if level == "document" else data["scenarios"][0]
            target["previousBuild"] = {"kind": "initial-warm"}
            self.changes.write_text(json.dumps(data))
            self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="fields")

    def assert_history_reuse_totals(self, report):
        categories = ("previousBuild", "retainedHistory", "misses")
        for field in ("syntheticReuseCounts", "syntheticReuseModelUnits"):
            aggregate = report["totals"][field]
            self.assertEqual(set(aggregate), {"before", "after"})
            for side in ("before", "after"):
                self.assertEqual(set(aggregate[side]), set(categories))
                for category in categories:
                    self.assertEqual(aggregate[side][category],
                                     sum(item[side][field][category] for item in report["scenarios"]))
        for side in ("before", "after"):
            counts = report["totals"]["syntheticReuseCounts"][side]
            units = report["totals"]["syntheticReuseModelUnits"][side]
            self.assertTrue(all(type(n) is int and n >= 0 for n in counts.values()))
            self.assertTrue(all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in units.values()))
            self.assertEqual(sum(counts.values()), sum(len(item[side]["steps"]) for item in report["scenarios"]))
            self.assertEqual(counts["previousBuild"] + counts["retainedHistory"],
                             sum(len(item[side]["cache"]["hitInstructionIds"]) for item in report["scenarios"]))
            self.assertEqual(counts["misses"], sum(len(item[side]["cache"]["missInstructionIds"])
                                                   for item in report["scenarios"]))
            self.assertEqual(units["misses"], report["totals"][side + "ModelUnits"])
            whole = len(report["scenarios"]) * sum(i["weightModelUnits"]
                                                      for i in report["dockerfiles"][side]["instructions"])
            self.assertTrue(math.isclose(sum(units.values()), whole, rel_tol=1e-12, abs_tol=1e-9))

    def test_history_reuse_totals_mixed_sides_and_selected_only(self):
        self.before.write_text("FROM scratch\nWORKDIR /app\nCOPY . /app/\nENV MODE=demo\nRUN true\n")
        self.after.write_text("FROM scratch\nENV MODE=demo\nCOPY src /app/\nRUN true\nCOPY docs /docs/\nRUN true\nWORKDIR /done\n")
        self.weights.write_text(json.dumps({"schemaVersion": 1,
            "before": dict(zip((f"I{i:03d}" for i in range(1, 6)), (0.125, 0, 2.25, 0.5, 4.125))),
            "after": dict(zip((f"I{i:03d}" for i in range(1, 8)), (0.125, 0.25, 2.5, 0, 0.75, 1.5, 0.125)))}))
        self.history([], [{"path": "src/app.js", "kind": "mtime"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"},
                      {"path": "docs/note.txt", "kind": "content"}],
                     [{"path": "docs/note.txt", "kind": "content"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open), patch("subprocess.Popen", side_effect=AssertionError("no execution")):
            full = self.run_model(weighted=True, mode="cumulative-history")
            self.assertEqual(full["schemaVersion"], 1)
            self.assert_history_reuse_totals(full)
            self.assertEqual(full["totals"]["syntheticReuseCounts"], {
                "before": {"previousBuild": 21, "retainedHistory": 0, "misses": 9},
                "after": {"previousBuild": 29, "retainedHistory": 2, "misses": 11}})
            self.assertEqual(full["totals"]["syntheticReuseModelUnits"], {
                "before": {"previousBuild": 21.375, "retainedHistory": 0, "misses": 20.625},
                "after": {"previousBuild": 19.375, "retainedHistory": 2.5, "misses": 9.625}})
            for item in full["scenarios"]:
                selected = self.run_model(weighted=True, mode="cumulative-history", scenario=item["id"])
                self.assertEqual(selected["scenarios"], [item])
                self.assert_history_reuse_totals(selected)
                for field in ("syntheticReuseCounts", "syntheticReuseModelUnits"):
                    self.assertEqual(selected["totals"][field], {side: item[side][field] for side in ("before", "after")})
            mixed = full["scenarios"][3]
            self.assertEqual(mixed["previousBuild"], {"kind": "scenario", "scenarioId": "step-3"})
            self.assertEqual(mixed["after"]["steps"][2]["syntheticStateOrigin"], {"kind": "initial-warm"})
            self.assertEqual([s["syntheticStateReuse"] for s in mixed["after"]["steps"]],
                             ["previous-build"] * 2 + ["retained-history"] * 2 + [None] * 3)

    def test_history_reuse_totals_defaults_zero_fractional_and_restore(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        for values in (None, (0, 0, 0, 0, 0, 0), (0.1, 0.2, 0.3, 0.00001, 10**12, 0.7)):
            if values is not None:
                self.weights.write_text(json.dumps({"schemaVersion": 1,
                    "before": dict(zip((f"I{i:03d}" for i in range(1, 6)), values)),
                    "after": dict(zip((f"I{i:03d}" for i in range(1, 7)), values))}))
            report = compare(self.before, self.after, fixture, self.context,
                             self.weights if values is not None else None, mode="cumulative-history")
            self.assert_history_reuse_totals(report)
            self.assertEqual(report["totals"]["syntheticReuseCounts"], {
                "before": {"previousBuild": 8, "retainedHistory": 6, "misses": 6},
                "after": {"previousBuild": 16, "retainedHistory": 4, "misses": 4}})
            if values is None:
                self.assertEqual(report["totals"]["syntheticReuseModelUnits"], {
                    "before": {"previousBuild": 0, "retainedHistory": 4, "misses": 4},
                    "after": {"previousBuild": 4, "retainedHistory": 2, "misses": 2}})
            elif not any(values):
                self.assertTrue(all(n == 0 for side in report["totals"]["syntheticReuseModelUnits"].values()
                                    for n in side.values()))
        # No COPY: all instructions in each displayed build reuse the preceding build.
        self.after.write_text("FROM scratch\nENV MODE=demo\nRUN true\n")
        self.history([], [{"path": "src/app.js", "kind": "mtime"}])
        report = self.run_model(mode="cumulative-history")
        self.assert_history_reuse_totals(report)
        self.assertEqual(report["totals"]["syntheticReuseCounts"]["after"],
                         {"previousBuild": 6, "retainedHistory": 0, "misses": 0})
        self.assertEqual(report["totals"]["syntheticReuseModelUnits"]["after"],
                         {"previousBuild": 2, "retainedHistory": 0, "misses": 0})

    def test_history_reuse_totals_exact_text_and_independent_omission(self):
        from stackwrench.cache_replay.cli import render_text
        self.history([], [{"path": "src/app.js", "kind": "mtime"}])
        self.weights.write_text(json.dumps({"schemaVersion": 1, "before": {"I001": 0.125}, "after": {}}))
        for scenario, before_count, after_count, before_units, after_units, label in (
            (None, 10, 12, "4.25", "4", "Total"),
            ("step-2", 5, 6, "2.125", "2", "Total (displayed scenario)")):
            report = self.run_model(weighted=True, mode="cumulative-history", scenario=scenario)
            lines = render_text(report).splitlines()
            index = lines.index(f"{label}: before=0, after=0, saved=0 model units")
            self.assertEqual(lines[index + 1:index + 5], [
                f"  before synthetic reuse: previous-build={before_count}, retained-history=0, misses=0",
                f"  before synthetic reuse model units: previous-build={before_units}, retained-history=0, misses=0",
                f"  after synthetic reuse: previous-build={after_count}, retained-history=0, misses=0",
                f"  after synthetic reuse model units: previous-build={after_units}, retained-history=0, misses=0"])
        independent = self.run_model(weighted=True)
        self.assertEqual(independent["totals"], {"beforeModelUnits": 0, "afterModelUnits": 0, "savedModelUnits": 0})
        for field in ("syntheticReuseCounts", "syntheticReuseModelUnits"):
            self.assertNotIn(field, json.dumps(independent))
        self.assertNotIn("synthetic reuse", render_text(independent))

    def test_history_reuse_totals_output_only_and_atomic_later_errors(self):
        summary = {"before": {"previousBuild": 1, "retainedHistory": 0, "misses": 0},
                   "after": {"previousBuild": 1, "retainedHistory": 0, "misses": 0}}
        for field in ("syntheticReuseCounts", "syntheticReuseModelUnits", "totals"):
            for location in ("top", "scenario", "event", "weights", "weight-side"):
                self.history([], [{"path": "src/app.js", "kind": "mtime"}])
                data = json.loads(self.changes.read_text())
                weights = {"schemaVersion": 1, "before": {}, "after": {}}
                target = {"top": data, "scenario": data["scenarios"][1],
                          "event": data["scenarios"][1]["changes"][0],
                          "weights": weights, "weight-side": weights["before"]}[location]
                target[field] = summary
                self.changes.write_text(json.dumps(data))
                self.weights.write_text(json.dumps(weights))
                with self.subTest(field=field, location=location):
                    with self.assertRaises(InputError):
                        self.run_model(weighted=True, mode="cumulative-history", scenario="step-1")
                    for output in ((), ("--json",)):
                        self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1",
                                              "--weights", str(self.weights), *output)
        for event, phrase in (({"path": "src/app.js", "kind": "content", "revisionId": "v0"}, "unchanged revisionId"),
                              ({"path": "package-lock.json", "kind": "delete"}, "COPY source")):
            self.history([], [event])
            self.set_initial_revisions({"src/app.js": "v0"})
            with self.assertRaises(InputError):
                self.run_model(mode="cumulative-history", scenario="step-1")
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output, phrase=phrase)

    def assert_reuse_counts(self, result, expected):
        counts = result["syntheticReuseCounts"]
        self.assertEqual(counts, dict(zip(("previousBuild", "retainedHistory", "misses"), expected)))
        self.assertTrue(all(type(n) is int and n >= 0 for n in counts.values()))
        self.assertEqual(sum(counts.values()), len(result["steps"]))
        self.assertEqual(counts["previousBuild"] + counts["retainedHistory"],
                         len(result["cache"]["hitInstructionIds"]))
        self.assertEqual(counts["misses"], len(result["cache"]["missInstructionIds"]))

    def test_synthetic_reuse_counts_initial_event_restores_and_selection(self):
        self.history([], [{"path": "src/app.js", "kind": "mtime"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}], [],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "delete"}],
                     [{"path": "src/app.js", "kind": "add", "revisionId": "v1"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            full = self.run_model(mode="cumulative-history")
            for index, item in enumerate(full["scenarios"]):
                selected = self.run_model(mode="cumulative-history", scenario=item["id"])
                self.assertEqual(selected["scenarios"], [item])
                self.assertEqual(item["previousBuild"], {"kind": "initial-warm"} if index == 0 else
                                 {"kind": "scenario", "scenarioId": f"step-{index}"})
                self.assertEqual(selected["totals"]["syntheticReuseCounts"],
                                 {side: item[side]["syntheticReuseCounts"] for side in ("before", "after")})
                for side, warm, total in (("before", 2, 5), ("after", 4, 6)):
                    expected = ((warm, 0, total - warm) if index in (2, 6) else
                                (warm, total - warm, 0) if index in (3, 5, 7) else (total, 0, 0))
                    with self.subTest(scenario=item["id"], side=side):
                        self.assert_reuse_counts(item[side], expected)
            # First producer survives later event restore and repeated reuse.
            for side in ("before", "after"):
                self.assertEqual(full["scenarios"][-1][side]["steps"][-1]["syntheticStateOrigin"],
                                 {"kind": "scenario", "scenarioId": "step-3"})

    def test_synthetic_reuse_counts_mixed_separate_sides_and_anonymous_misses(self):
        self.before.write_text("FROM scratch\nCOPY . /app/\nRUN true\n")
        self.after.write_text("FROM scratch\nCOPY src /app/\nRUN true\nCOPY docs /docs/\nRUN true\n")
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"},
                      {"path": "docs/note.txt", "kind": "content"}],
                     [{"path": "docs/note.txt", "kind": "content"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        report = self.run_model(mode="cumulative-history")
        for item, before, after in zip(report["scenarios"],
                                      ((1, 0, 2), (1, 0, 2), (1, 0, 2), (3, 0, 0)),
                                      ((1, 0, 4), (1, 2, 2), (3, 0, 2), (5, 0, 0))):
            self.assert_reuse_counts(item["before"], before)
            self.assert_reuse_counts(item["after"], after)
        # A Dockerfile with no COPY still counts its warm instruction states.
        self.after.write_text("FROM scratch\nENV MODE=demo\nRUN true\n")
        for item in self.run_model(mode="cumulative-history")["scenarios"]:
            self.assert_reuse_counts(item["after"], (3, 0, 0))

    def test_synthetic_reuse_counts_exact_text_and_independent_omission(self):
        from stackwrench.cache_replay.cli import render_text
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}])
        self.set_initial_revisions({"src/app.js": "v0"})
        selected = self.run_model(mode="cumulative-history", scenario="step-2")
        lines = render_text(selected).splitlines()
        summaries = [i for i, line in enumerate(lines) if line.startswith("    synthetic cache: ")]
        self.assertEqual(len(summaries), 2)
        self.assertEqual([lines[i + 1] for i in summaries],
                         ["    synthetic reuse: previous-build=2, retained-history=3, misses=0",
                          "    synthetic reuse: previous-build=4, retained-history=2, misses=0"])
        self.events([{"path": "src/app.js", "kind": "content"}])
        independent = self.run_model()
        self.assertNotIn("syntheticReuseCounts", json.dumps(independent))
        self.assertNotIn("synthetic reuse:", render_text(independent))

    def test_synthetic_reuse_counts_output_only_and_atomic_later_errors(self):
        for location in ("top", "scenario", "event"):
            self.history([], [{"path": "src/app.js", "kind": "mtime"}])
            data = json.loads(self.changes.read_text())
            target = (data if location == "top" else data["scenarios"][1] if location == "scenario"
                      else data["scenarios"][1]["changes"][0])
            target["syntheticReuseCounts"] = {"previousBuild": 1, "retainedHistory": 0, "misses": 0}
            self.changes.write_text(json.dumps(data))
            with self.assertRaises(InputError):
                self.run_model(mode="cumulative-history", scenario="step-1")
            self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", "--json", phrase="fields")
        for event, phrase in (({"path": "src/app.js", "kind": "content", "revisionId": "v0"}, "unchanged revisionId"),
                              ({"path": "package-lock.json", "kind": "delete"}, "COPY source"),
                              ({"path": "missing", "kind": "content"}, "unknown change path")):
            self.history([], [event])
            self.set_initial_revisions({"src/app.js": "v0"})
            with self.assertRaises(InputError):
                self.run_model(mode="cumulative-history", scenario="step-1")
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output, phrase=phrase)

    def test_synthetic_reuse_model_units_weighted_mixed_history_and_selection(self):
        self.before.write_text("FROM scratch\nWORKDIR /app\nCOPY . /app/\nENV MODE=demo\nRUN true\n")
        self.after.write_text("FROM scratch\nENV MODE=demo\nCOPY src /app/\nRUN true\nCOPY docs /docs/\nRUN true\nWORKDIR /done\n")
        self.weights.write_text(json.dumps({"schemaVersion": 1,
            "before": dict(zip((f"I{i:03d}" for i in range(1, 6)), (0.125, 0, 2.25, 0.5, 4.125))),
            "after": dict(zip((f"I{i:03d}" for i in range(1, 8)), (10**12, 0.25, 2.5, 0, 0.75, 1.5, 0.125)))}))
        self.history([], [{"path": "src/app.js", "kind": "mtime"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"},
                      {"path": "docs/note.txt", "kind": "content"}],
                     [{"path": "docs/note.txt", "kind": "content"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)

        expected_before = ((7, 0, 0), (7, 0, 0), (0.125, 0, 6.875),
                           (0.125, 0, 6.875), (0.125, 0, 6.875), (7, 0, 0))
        expected_after = ((10**12 + 5.125, 0, 0), (10**12 + 5.125, 0, 0),
                          (10**12 + 0.25, 0, 4.875), (10**12 + 0.25, 2.5, 2.375),
                          (10**12 + 2.75, 0, 2.375), (10**12 + 5.125, 0, 0))
        with patch.object(Path, "open", guarded_open), patch("subprocess.Popen", side_effect=AssertionError("no execution")):
            full = self.run_model(weighted=True, mode="cumulative-history")
            self.assertEqual(full["schemaVersion"], 1)
            for index, item in enumerate(full["scenarios"]):
                selected = self.run_model(weighted=True, mode="cumulative-history", scenario=item["id"])
                self.assertEqual(selected["scenarios"], [item])
                self.assertEqual(selected["totals"]["syntheticReuseModelUnits"],
                                 {side: item[side]["syntheticReuseModelUnits"] for side in ("before", "after")})
                for side, expected, total in (("before", expected_before[index], 7),
                                              ("after", expected_after[index], 10**12 + 5.125)):
                    with self.subTest(scenario=item["id"], side=side):
                        units = item[side]["syntheticReuseModelUnits"]
                        self.assertEqual(units, dict(zip(("previousBuild", "retainedHistory", "misses"), expected)))
                        self.assertEqual(sum(units.values()), total)
                        self.assertEqual(units["misses"], item[side]["invalidatedModelUnits"])
                        self.assertTrue(all(type(n) in (int, float) and math.isfinite(n) and n >= 0
                                            for n in units.values()))
            mixed = full["scenarios"][3]
            self.assert_reuse_counts(mixed["after"], (2, 2, 3))
            self.assertEqual(mixed["previousBuild"], {"kind": "scenario", "scenarioId": "step-3"})
            self.assertEqual(mixed["after"]["steps"][2]["syntheticStateOrigin"], {"kind": "initial-warm"})

    def test_synthetic_reuse_model_units_defaults_zero_and_fractional_partition(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        defaults = compare(self.before, self.after, fixture, self.context, mode="cumulative-history")
        for index, item in enumerate(defaults["scenarios"]):
            for side in ("before", "after"):
                expected = {"previousBuild": 0 if side == "before" else 1,
                            "retainedHistory": 0 if index in (0, 2) else (2 if side == "before" else 1),
                            "misses": (2 if side == "before" else 1) if index in (0, 2) else 0}
                self.assertEqual(item[side]["syntheticReuseModelUnits"], expected)
        # Decimal grouping need not be bit-for-bit equal to the whole inventory sum.
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}])
        self.set_initial_revisions({"src/app.js": "v0"})
        for values in ((0, 0, 0, 0, 0, 0), (0.1, 0.2, 0.3, 0.00001, 10**12, 0.7)):
            self.weights.write_text(json.dumps({"schemaVersion": 1,
                "before": dict(zip((f"I{i:03d}" for i in range(1, 6)), values)),
                "after": dict(zip((f"I{i:03d}" for i in range(1, 7)), values))}))
            report = self.run_model(weighted=True, mode="cumulative-history")
            for item in report["scenarios"]:
                for side in ("before", "after"):
                    units = item[side]["syntheticReuseModelUnits"]
                    total = sum(i["weightModelUnits"] for i in report["dockerfiles"][side]["instructions"])
                    self.assertTrue(math.isclose(sum(units.values()), total, rel_tol=1e-12, abs_tol=1e-9))
                    self.assertEqual(units["misses"], item[side]["invalidatedModelUnits"])
        self.after.write_text("FROM scratch\nENV MODE=demo\nRUN true\n")
        for item in self.run_model(mode="cumulative-history")["scenarios"]:
            self.assertEqual(item["after"]["syntheticReuseModelUnits"],
                             {"previousBuild": 1, "retainedHistory": 0, "misses": 0})

    def test_synthetic_reuse_model_units_text_and_independent_omission(self):
        from stackwrench.cache_replay.cli import render_text
        fixture = EXAMPLES / "changes.initial-revisions.json"
        selected = compare(self.before, self.after, fixture, self.context, EXAMPLES / "weights.json",
                           mode="cumulative-history", scenario="revert-initial")
        lines = render_text(selected).splitlines()
        summaries = [i for i, line in enumerate(lines) if line.startswith("    synthetic reuse: ")]
        self.assertEqual(len(summaries), 2)
        self.assertEqual([lines[i + 1] for i in summaries],
                         ["    synthetic reuse model units: previous-build=0, retained-history=110, misses=0",
                          "    synthetic reuse model units: previous-build=100, retained-history=10, misses=0"])
        # Fractional text uses the existing :g model-cost format.
        self.weights.write_text(json.dumps({"schemaVersion": 1, "before": {"I001": 0.125}, "after": {}}))
        fractional = self.run_model(weighted=True, mode="cumulative-history")
        self.assertIn("    synthetic reuse model units: previous-build=0.125, retained-history=0, misses=2\n",
                      render_text(fractional))
        independent = self.run_model(weighted=True)
        self.assertNotIn("syntheticReuseModelUnits", json.dumps(independent))
        self.assertNotIn("synthetic reuse model units:", render_text(independent))

    def test_synthetic_reuse_model_units_output_only_and_atomic_errors(self):
        for location in ("top", "scenario", "event"):
            self.history([], [{"path": "src/app.js", "kind": "mtime"}])
            data = json.loads(self.changes.read_text())
            target = (data if location == "top" else data["scenarios"][1] if location == "scenario"
                      else data["scenarios"][1]["changes"][0])
            target["syntheticReuseModelUnits"] = {"previousBuild": 1, "retainedHistory": 0, "misses": 0}
            self.changes.write_text(json.dumps(data))
            with self.assertRaises(InputError):
                self.run_model(mode="cumulative-history", scenario="step-1")
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output, phrase="fields")
        for event, phrase in (({"path": "src/app.js", "kind": "content", "revisionId": "v0"}, "unchanged revisionId"),
                              ({"path": "package-lock.json", "kind": "delete"}, "COPY source"),
                              ({"path": "missing", "kind": "content"}, "unknown change path")):
            self.history([], [event])
            self.set_initial_revisions({"src/app.js": "v0"})
            with self.assertRaises(InputError):
                self.run_model(mode="cumulative-history", scenario="step-1")
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output, phrase=phrase)
        self.history([])
        for value in (-1, True, float("nan"), float("inf"), 10**12 + 1):
            self.weights.write_text(json.dumps({"schemaVersion": 1, "before": {"I001": value}, "after": {}}))
            with self.assertRaises(InputError):
                self.run_model(weighted=True, mode="cumulative-history")
            self.assert_cli_error("--mode", "cumulative-history", "--weights", str(self.weights), "--json")
        for location in ("top", "before"):
            weights = {"schemaVersion": 1, "before": {}, "after": {}}
            target = weights if location == "top" else weights["before"]
            target["syntheticReuseModelUnits"] = {}
            self.weights.write_text(json.dumps(weights))
            with self.assertRaises(InputError):
                self.run_model(weighted=True, mode="cumulative-history")
            self.assert_cli_error("--mode", "cumulative-history", "--weights", str(self.weights), "--json")

    def test_synthetic_state_reuse_initial_and_event_restores_then_previous_build(self):
        self.history([], [{"path": "src/app.js", "kind": "mtime"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}], [],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "mtime"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            report = self.run_model(mode="cumulative-history")
            selected = self.run_model(mode="cumulative-history", scenario="step-6")
        self.assertEqual(selected["scenarios"], [report["scenarios"][5]])
        for side, warm in (("before", 2), ("after", 4)):
            for index, reuse in enumerate(("previous-build", "previous-build", None,
                                           "retained-history", "previous-build", "retained-history",
                                           "previous-build", "previous-build")):
                steps = report["scenarios"][index][side]["steps"]
                self.assertEqual([s["syntheticStateReuse"] for s in steps],
                                 ["previous-build"] * warm + [reuse] * (len(steps) - warm))
                for step in steps[warm:]:
                    self.assertEqual(step["syntheticStateOrigin"],
                                     None if index == 2 else {"kind": "scenario", "scenarioId": "step-3"}
                                     if index >= 5 else {"kind": "initial-warm"})
            self.assertEqual([s[side]["invalidatedModelUnits"] for s in report["scenarios"]],
                             [0, 0, 2, 0, 0, 0, 0, 0] if side == "before" else [0, 0, 1, 0, 0, 0, 0, 0])

    def test_synthetic_state_reuse_separate_sides_partial_hits_and_anonymous_events(self):
        # Anonymous edits miss even when the caller may have restored old bytes.
        # A src revert plus docs edit restores the early COPY but misses broadly.
        self.before.write_text("FROM scratch\nCOPY . /app/\nRUN true\n")
        self.after.write_text("FROM scratch\nCOPY src /app/\nRUN true\nCOPY docs /docs/\nRUN true\n")
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"},
                      {"path": "docs/note.txt", "kind": "content"}],
                     [{"path": "docs/note.txt", "kind": "content"}], [],
                     [{"path": "src/new.js", "kind": "add"}],
                     [{"path": "src/new.js", "kind": "delete"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        report = self.run_model(mode="cumulative-history")
        mixed = report["scenarios"][1]
        self.assertEqual([s["syntheticStateReuse"] for s in mixed["before"]["steps"]],
                         ["previous-build", None, None])
        self.assertEqual([s["syntheticStateReuse"] for s in mixed["after"]["steps"]],
                         ["previous-build", "retained-history", "retained-history", None, None])
        anonymous = report["scenarios"][2]
        self.assertEqual([s["syntheticStateReuse"] for s in anonymous["after"]["steps"]],
                         ["previous-build"] * 3 + [None, None])
        for index in (3, 6):
            self.assertTrue(all(s["syntheticStateReuse"] == "previous-build"
                                for side in ("before", "after") for s in report["scenarios"][index][side]["steps"]))
        for side in ("before", "after"):
            restored = report["scenarios"][5][side]["steps"]
            self.assertEqual([s["syntheticStateReuse"] for s in restored],
                             ["previous-build"] + ["retained-history"] * (len(restored) - 1))
            self.assertEqual(restored[-1]["syntheticStateOrigin"], {"kind": "scenario", "scenarioId": "step-3"})

    def test_synthetic_state_reuse_text_selection_and_independent_omission(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        full = compare(self.before, self.after, fixture, self.context, mode="cumulative-history")
        for index, sid in ((1, "revert-initial"), (3, "readd-initial")):
            selected = self.cli("--mode", "cumulative-history", "--scenario", sid, "--json", changes=fixture)
            self.assertEqual(selected.returncode, 0, selected.stderr)
            self.assertEqual(json.loads(selected.stdout)["scenarios"], [full["scenarios"][index]])
            for side, warm in (("before", 2), ("after", 4)):
                steps = full["scenarios"][index][side]["steps"]
                self.assertEqual([s["syntheticStateReuse"] for s in steps],
                                 ["previous-build"] * warm + ["retained-history"] * (len(steps) - warm))
                self.assertTrue(all(s["syntheticStateOrigin"] == {"kind": "initial-warm"} for s in steps))
            text = self.cli("--mode", "cumulative-history", "--scenario", sid, changes=fixture)
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertEqual(text.stdout.count("      synthetic state reuse: retained-history\n"), 5)
            self.assertEqual(text.stdout.count("      synthetic state reuse: previous-build\n"), 6)
        text = self.cli("--mode", "cumulative-history", "--scenario", "edit-v2", changes=fixture)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout.count("      synthetic state reuse: none (miss)\n"), 5)
        independent = self.run_model()
        self.assertTrue(all("syntheticStateReuse" not in s for side in ("before", "after")
                            for s in independent["scenarios"][0][side]["steps"]))
        text = self.cli()
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertNotIn("synthetic state reuse", text.stdout)

    def test_synthetic_state_reuse_later_invalid_input_rejects_selected_report(self):
        for event, phrase in (({"path": "src/app.js", "kind": "content", "revisionId": "v0"}, "unchanged revisionId"),
                              ({"path": "package-lock.json", "kind": "delete"}, "COPY source")):
            self.history([], [event])
            self.set_initial_revisions({"src/app.js": "v0"})
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output, phrase=phrase)
            with self.assertRaises(InputError):
                self.run_model(mode="cumulative-history", scenario="step-1")

    def test_synthetic_state_origin_initial_restore_and_selected_no_content_reads(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be read or written")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            report = compare(self.before, self.after, fixture, self.context,
                             mode="cumulative-history")
            selected = compare(self.before, self.after, fixture, self.context,
                               mode="cumulative-history", scenario="revert-initial")
        self.assertEqual(selected["scenarios"], [report["scenarios"][1]])
        for index in (1, 3):
            for side in ("before", "after"):
                result = report["scenarios"][index][side]
                self.assertEqual(result["invalidatedModelUnits"], 0)
                self.assertEqual(result["cache"]["missInstructionIds"], [])
                self.assertTrue(all(step["syntheticStateOrigin"] == {"kind": "initial-warm"}
                                    for step in result["steps"]))
        for side in ("before", "after"):
            for step in report["scenarios"][0][side]["steps"]:
                self.assertEqual(step["syntheticStateOrigin"],
                                 None if step["decision"] == "synthetic-miss" else {"kind": "initial-warm"})
        self.assertFalse(report["actualCacheHitProven"])

    def test_synthetic_state_origin_first_scenario_survives_repeated_hits(self):
        fixture = EXAMPLES / "changes.revisions.json"
        data = json.loads(fixture.read_text())
        data["scenarios"].extend([
            {"id": "mtime", "changes": [{"path": "src/app.js", "kind": "mtime"}]},
            {"id": "noop", "changes": []}])
        self.changes.write_text(json.dumps(data))
        report = self.run_model(mode="cumulative-history")
        for index in (2, 4, 5, 6):
            for side, warm_count in (("before", 2), ("after", 4)):
                steps = report["scenarios"][index][side]["steps"]
                self.assertEqual([step["syntheticStateOrigin"] for step in steps],
                                 [{"kind": "initial-warm"}] * warm_count +
                                 [{"kind": "scenario", "scenarioId": "declare-v1"}] * (len(steps) - warm_count))
        selected = self.run_model(mode="cumulative-history", scenario="noop")
        self.assertEqual(selected["scenarios"], [report["scenarios"][-1]])
        result = self.cli("--mode", "cumulative-history", "--scenario", "noop", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), selected)
        text = self.cli("--mode", "cumulative-history", "--scenario", "noop")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout.count("synthetic state origin: initial-warm"), 6)
        self.assertEqual(text.stdout.count("synthetic state origin: scenario declare-v1"), 5)
        self.assertNotIn("synthetic state origin: none (miss)", text.stdout)

    def test_synthetic_state_origin_separate_caches_partial_hits_and_anonymous_events(self):
        # The broad before COPY misses on docs; the staged after COPY of src hits.
        self.before.write_text("FROM scratch\nCOPY . /app/\nRUN true\n")
        self.after.write_text("FROM scratch\nCOPY src /app/\nRUN true\n")
        self.history([{"path": "docs/note.txt", "kind": "content"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"}],
                     [{"path": "docs/note.txt", "kind": "content"}], [])
        self.set_initial_revisions({"src/app.js": "v0"})
        report = self.run_model(mode="cumulative-history")
        restored = report["scenarios"][2]
        self.assertEqual([s["syntheticStateOrigin"] for s in restored["before"]["steps"]],
                         [{"kind": "initial-warm"}] + [{"kind": "scenario", "scenarioId": "step-1"}] * 2)
        self.assertTrue(all(s["syntheticStateOrigin"] == {"kind": "initial-warm"}
                            for s in restored["after"]["steps"]))
        anonymous = report["scenarios"][3]
        self.assertEqual([s["syntheticStateOrigin"] for s in anonymous["before"]["steps"]],
                         [{"kind": "initial-warm"}, None, None])
        self.assertEqual(anonymous["before"]["invalidatedModelUnits"], 1)
        self.assertEqual(anonymous["after"]["invalidatedModelUnits"], 0)
        noop = report["scenarios"][4]
        self.assertEqual([s["syntheticStateOrigin"] for s in noop["before"]["steps"]],
                         [{"kind": "initial-warm"}] + [{"kind": "scenario", "scenarioId": "step-4"}] * 2)
        # Reverting src while changing another selected file still misses broadly.
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v0"},
                      {"path": "docs/note.txt", "kind": "content"}])
        self.set_initial_revisions({"src/app.js": "v0"})
        mixed = self.run_model(mode="cumulative-history")["scenarios"][-1]
        self.assertEqual([s["syntheticStateOrigin"] for s in mixed["before"]["steps"]],
                         [{"kind": "initial-warm"}, None, None])
        self.assertTrue(all(s["syntheticStateOrigin"] == {"kind": "initial-warm"}
                            for s in mixed["after"]["steps"]))

    def test_synthetic_state_origin_text_misses_and_independent_omission(self):
        self.history([{"path": "src/app.js", "kind": "content"}], [])
        result = self.cli("--mode", "cumulative-history", "--scenario", "step-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("synthetic state origin: none (miss)"), 5)
        self.assertEqual(result.stdout.count("synthetic state origin: initial-warm"), 6)
        for mode in ({}, {"mode": "independent-warm"}):
            independent = self.run_model(**mode)
            self.assertTrue(all("syntheticStateOrigin" not in step for item in independent["scenarios"]
                                for side in ("before", "after") for step in item[side]["steps"]))
        text = self.cli()
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertNotIn("synthetic state origin", text.stdout)

    def test_initial_revision_ids_restore_warm_states_without_content_reads(self):
        fixture = EXAMPLES / "changes.initial-revisions.json"
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be read or written")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            report = compare(self.before, self.after, fixture, self.context,
                             EXAMPLES / "weights.json", mode="cumulative-history")
            selected = compare(self.before, self.after, fixture, self.context,
                               EXAMPLES / "weights.json", mode="cumulative-history", scenario="revert-initial")
        self.assertEqual(report["initialRevisionIds"], {"src/app.js": "source-v1"})
        self.assertEqual([(s["before"]["invalidatedModelUnits"], s["after"]["invalidatedModelUnits"])
                          for s in report["scenarios"]], [(110, 10), (0, 0), (110, 10), (0, 0)])
        self.assertEqual({k: report["totals"][k] for k in ("beforeModelUnits", "afterModelUnits", "savedModelUnits")},
                         {"beforeModelUnits": 220, "afterModelUnits": 20, "savedModelUnits": 200})
        self.assert_history_reuse_totals(report)
        self.assertEqual(selected["scenarios"], [report["scenarios"][1]])
        self.assertEqual(selected["initialRevisionIds"], report["initialRevisionIds"])
        for index in (1, 3):
            for side, count in (("before", 5), ("after", 6)):
                restored = report["scenarios"][index][side]
                self.assertIsNotNone(restored["firstAffectedCopy"])
                self.assertIsNone(restored["firstInvalidatedCopy"])
                self.assertEqual(restored["cache"]["missInstructionIds"], [])
                self.assertEqual(len(restored["cache"]["hitInstructionIds"]), count)
                self.assertEqual(restored["cache"]["entryCountBefore"], restored["cache"]["entryCountAfter"])
                self.assertEqual(restored["reasonChain"], [])
                self.assertTrue(all(s["decision"] == "synthetic-hit" for s in restored["steps"]))
        result = self.cli("--mode", "cumulative-history", "--json", "--weights", str(EXAMPLES / "weights.json"),
                          changes=fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), report)
        text = self.cli("--mode", "cumulative-history", "--scenario", "readd-initial", changes=fixture)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Initial revision declarations: 1\n  src/app.js revisionId=source-v1", text.stdout)
        self.assertIn("Total (displayed scenario): before=0, after=0, saved=0", text.stdout)
        self.assertTrue(any("initialRevisionIds" in a and "anonymous" in a for a in report["assumptions"]))
        self.assertFalse(report["actualCacheHitProven"])

    def test_initial_revision_ids_namespace_unlisted_paths_and_other_inputs(self):
        for revision in ("0", "1", "A_.-9", "x" * 128):
            with self.subTest(revision=revision):
                self.history([{"path": "src/app.js", "kind": "content"}],
                             [{"path": "src/app.js", "kind": "content", "revisionId": revision}])
                self.set_initial_revisions({"src/app.js": revision})
                report = self.run_model(mode="cumulative-history")
                self.assertEqual([s["before"]["invalidatedModelUnits"] for s in report["scenarios"]], [2, 0])
        # Declarations on a different (even uncopied) path do not label this file.
        for kinds in (("content", "content"), ("delete", "add")):
            self.history([{"path": "src/app.js", "kind": kinds[0]}],
                         [{"path": "src/app.js", "kind": kinds[1], "revisionId": "v1"}])
            self.set_initial_revisions({"docs/note.txt": "v1"})
            report = self.run_model(mode="cumulative-history")
            self.assertEqual([s["before"]["invalidatedModelUnits"] for s in report["scenarios"]], [2, 2])
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "V1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"},
                      {"path": "package.json", "kind": "content", "revisionId": "V1"}])
        self.set_initial_revisions({"src/app.js": "v1", "package.json": "v1"})
        result = self.run_model(mode="cumulative-history")["scenarios"][-1]
        self.assertEqual(result["before"]["invalidatedModelUnits"], 2)
        self.assertEqual(result["after"]["invalidatedModelUnits"], 2)
        self.assertEqual(result["after"]["firstInvalidatedCopy"], "I003")

    def test_initial_revision_ids_empty_and_omitted_output_contract(self):
        self.history([])
        original = self.run_model(mode="cumulative-history")
        self.assertNotIn("initialRevisionIds", original)
        self.set_initial_revisions({})
        declared = self.run_model(mode="cumulative-history")
        self.assertEqual(declared.pop("initialRevisionIds"), {})
        self.assertEqual(declared, original)
        text = self.cli("--mode", "cumulative-history")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Initial revision declarations: 0", text.stdout)
        self.assert_cli_error("--json", phrase="initialRevisionIds requires cumulative-history")

    def test_initial_revision_ids_reject_schema_paths_ids_and_independent_mode(self):
        for value in (None, [], "v1", True, 1):
            with self.subTest(value=value):
                self.history([])
                self.set_initial_revisions(value)
                self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="initialRevisionIds: expected")
        for path in ("missing", "future.js", "src", ".", "./src/app.js", "src/app.js/", "src//app.js",
                     "src/./app.js", "../src/app.js", "/src/app.js", "src/*.js", "src/?.js", "src/[a].js",
                     "src/{a}.js", "src/**.js", "src\\app.js", "x:y", "src/$app.js", "src/\napp.js"):
            with self.subTest(path=path):
                self.history([{"path": "future.js", "kind": "add"}])
                self.set_initial_revisions({path: "v1"})
                self.assert_cli_error("--mode", "cumulative-history", "--json")
                with self.assertRaises(InputError):
                    self.run_model(mode="cumulative-history")
        for value in (None, True, 1, 1.5, [], {}, "", "x" * 129, "two words", "é", "x\n", "x:y", "x/y"):
            with self.subTest(value=value):
                self.history([])
                self.set_initial_revisions({"src/app.js": value})
                self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="initialRevisionIds: revisionId")
        self.history([])
        self.set_initial_revisions({"src/app.js": "v1"})
        for output in ((), ("--json",)):
            self.assert_cli_error(*output, phrase="initialRevisionIds requires cumulative-history")
        data = json.loads(self.changes.read_text())
        data["unknown"] = 1
        self.changes.write_text(json.dumps(data))
        self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="expected exactly fields")

    def test_initial_revision_ids_duplicate_keys_and_unsupported_inventory(self):
        for declaration in ('"initialRevisionIds":{"src/app.js":"v1","src/app.js":"v2"}',
                            '"initialRevisionIds":{},"initialRevisionIds":{}'):
            self.changes.write_text('{"schemaVersion":1,' + declaration + ',"scenarios":[{"id":"test","changes":[]}]}')
            self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="duplicate JSON key")
        for name, special in (("link.js", "symlink"), ("pipe.js", "fifo")):
            with self.subTest(special=special):
                self.history([])
                self.set_initial_revisions({name: "v1"})
                path = self.context / name
                if special == "symlink":
                    path.symlink_to(self.context / "src/app.js")
                else:
                    os.mkfifo(path)
                try:
                    self.assert_cli_error("--mode", "cumulative-history", "--json")
                finally:
                    path.unlink()

    def test_initial_revision_ids_current_id_and_later_errors_remain_atomic(self):
        for intervening in ([], [{"path": "src/app.js", "kind": "mtime"}]):
            self.history(intervening, [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}])
            self.set_initial_revisions({"src/app.js": "v1"})
            for output in ((), ("--json",)):
                self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output,
                                      phrase="step-2: content event has unchanged revisionId")
            with self.assertRaisesRegex(InputError, "step-2: content event has unchanged revisionId"):
                self.run_model(mode="cumulative-history", scenario="step-1")
        self.history([], [{"path": "package-lock.json", "kind": "delete"}],
                     [{"path": "package-lock.json", "kind": "add", "revisionId": "v1"}])
        self.set_initial_revisions({"package-lock.json": "v1"})
        for output in ((), ("--json",)):
            self.assert_cli_error("--mode", "cumulative-history", "--scenario", "step-1", *output,
                                  phrase="step-2 / after: I003: COPY source")
        # Declaring an input Dockerfile never relaxes the existing edit restriction.
        self.before = self.context / "Dockerfile"
        self.before.write_text("FROM scratch\nCOPY . /app/\nRUN true\n")
        self.history([{"path": "Dockerfile", "kind": "content", "revisionId": "v2"}])
        self.set_initial_revisions({"Dockerfile": "v1"})
        self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="Dockerfile/ignore-file changes")

    def test_revision_ids_restore_content_and_readded_file_without_content_reads(self):
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be read or written")
            return original_open(path, *args, **kwargs)

        fixture = EXAMPLES / "changes.revisions.json"
        with patch.object(Path, "open", guarded_open):
            report = compare(self.before, self.after, fixture, self.context,
                             EXAMPLES / "weights.json", mode="cumulative-history")
        self.assertEqual([(s["before"]["invalidatedModelUnits"], s["after"]["invalidatedModelUnits"])
                          for s in report["scenarios"]], [(110, 10), (110, 10), (0, 0), (110, 10), (0, 0)])
        self.assertEqual({k: report["totals"][k] for k in ("beforeModelUnits", "afterModelUnits", "savedModelUnits")},
                         {"beforeModelUnits": 330, "afterModelUnits": 30, "savedModelUnits": 300})
        self.assert_history_reuse_totals(report)
        self.assertEqual([(s["inventoryBeforeFileCount"], s["inventoryAfterFileCount"])
                          for s in report["scenarios"]], [(4, 4), (4, 4), (4, 4), (4, 3), (3, 4)])
        self.assertEqual([s["changes"][0].get("revisionId") for s in report["scenarios"]],
                         ["source-v1", "source-v2", "source-v1", None, "source-v1"])
        for index in (2, 4):
            for side, count in (("before", 5), ("after", 6)):
                restored = report["scenarios"][index][side]
                self.assertIsNotNone(restored["firstAffectedCopy"])
                self.assertIsNone(restored["firstInvalidatedCopy"])
                self.assertEqual(restored["invalidatedInstructionIds"], [])
                self.assertEqual(restored["reasonChain"], [])
                self.assertEqual(len(restored["cache"]["hitInstructionIds"]), count)
                self.assertEqual(restored["cache"]["entryCountBefore"], restored["cache"]["entryCountAfter"])
                self.assertTrue(all(step["decision"] == "synthetic-hit" for step in restored["steps"]))
                self.assertTrue(all(t["revisionId"] == "source-v1" for t in restored["triggers"]))
        self.assertFalse(report["actualCacheHitProven"])
        result = self.cli("--mode", "cumulative-history", "--json", "--weights", str(EXAMPLES / "weights.json"),
                          changes=fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), report)
        text = self.cli("--mode", "cumulative-history", changes=fixture)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("revisionId=source-v1", text.stdout)

    def test_revision_ids_namespace_case_and_anonymous_events(self):
        # A caller ID cannot collide with initial revision 0 or anonymous step 1.
        for revision in ("0", "1", "A_.-9", "x" * 128):
            with self.subTest(revision=revision):
                self.history([{"path": "src/app.js", "kind": "content"}],
                             [{"path": "src/app.js", "kind": "content", "revisionId": revision}],
                             [{"path": "src/app.js", "kind": "content"}],
                             [{"path": "src/app.js", "kind": "content", "revisionId": revision}])
                self.assertEqual([s["before"]["invalidatedModelUnits"] for s in
                                  self.run_model(mode="cumulative-history")["scenarios"]], [2, 2, 2, 0])
        self.history(*[[{"path": "src/app.js", "kind": "content", "revisionId": rev}]
                       for rev in ("v1", "V1", "v1")])
        self.assertEqual([s["before"]["invalidatedModelUnits"] for s in
                          self.run_model(mode="cumulative-history")["scenarios"]], [2, 2, 0])

    def test_revision_ids_add_revert_mtime_and_path_scope(self):
        self.history([{"path": "new.js", "kind": "add", "revisionId": "v1"}],
                     [{"path": "new.js", "kind": "content", "revisionId": "v2"}],
                     [{"path": "new.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "new.js", "kind": "mtime"}],
                     [{"path": "new.js", "kind": "delete"}],
                     [{"path": "other.js", "kind": "add", "revisionId": "v1"}])
        report = self.run_model(mode="cumulative-history")
        self.assertEqual([s["before"]["invalidatedModelUnits"] for s in report["scenarios"]], [2, 2, 0, 0, 0, 2])
        # Same label on two paths is valid; restoring one path cannot mask another edit.
        self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v2"}],
                     [{"path": "src/app.js", "kind": "content", "revisionId": "v1"},
                      {"path": "package.json", "kind": "content", "revisionId": "v1"}])
        result = self.run_model(mode="cumulative-history")["scenarios"][-1]
        self.assertEqual(result["before"]["invalidatedModelUnits"], 2)
        self.assertEqual(result["after"]["invalidatedModelUnits"], 2)
        self.assertEqual(result["after"]["firstInvalidatedCopy"], "I003")

    def test_revision_ids_reject_invalid_schema_mode_and_unchanged_content(self):
        for value in (None, True, 1, 1.5, [], {}, "", "x" * 129, "two words", "é", "x\n", "x:y", "x/y"):
            with self.subTest(value=value):
                self.history([], [{"path": "src/app.js", "kind": "content", "revisionId": value}])
                self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="revisionId")
        for kind in ("mtime", "delete"):
            with self.subTest(kind=kind):
                self.events([{"path": "src/app.js", "kind": kind, "revisionId": "v1"}])
                self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="content/add")
        self.events([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}])
        self.assert_cli_error("--json", phrase="cumulative-history")
        self.events([{"path": "src/app.js", "kind": "content", "revisionId": "v1", "extra": 1}])
        self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="fields")
        for intervening in ([], [{"path": "src/app.js", "kind": "mtime"}]):
            self.history([{"path": "src/app.js", "kind": "content", "revisionId": "v1"}], intervening,
                         [{"path": "src/app.js", "kind": "content", "revisionId": "v1"}])
            self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="unchanged revisionId")
        invalid = self.cli("--mode", "cumulative-history", "--json",
                           changes=EXAMPLES / "changes.revisions.invalid.json")
        self.assertEqual(invalid.returncode, 2, invalid.stderr)
        self.assertEqual(invalid.stdout, "")
        self.assertIn("contradictory-edit: content event has unchanged revisionId", invalid.stderr)
        # Existing missing-source and inventory constraints still apply with IDs.
        self.history([{"path": "package-lock.json", "kind": "delete"}],
                     [{"path": "package-lock.json", "kind": "add", "revisionId": "v1"}])
        self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="step-1 / after")
        self.events([{"path": "src/app.js", "kind": "add", "revisionId": "v1"}])
        self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="already exists")

    def test_history_early_copy_cache_hit_with_later_copy_miss(self):
        dockerfile = "FROM scratch\nCOPY docs /docs/\nRUN echo docs\nCOPY src /src/\nRUN echo src\n"
        self.before.write_text(dockerfile)
        self.after.write_text(dockerfile)
        self.history([{"path": "docs/new.txt", "kind": "add"}],
                     [{"path": "docs/new.txt", "kind": "delete"}, {"path": "src/app.js", "kind": "content"}])
        result = self.run_model(mode="cumulative-history")["scenarios"][1]["after"]
        self.assertEqual(result["firstAffectedCopy"], "I002")
        self.assertEqual(result["invalidatedInstructionIds"], ["I004", "I005"])
        self.assertEqual(result["cache"]["hitInstructionIds"], ["I001", "I002", "I003"])
        self.assertEqual(result["reasonChain"][0]["causedBy"][0]["path"], "src/app.js")

    def test_history_invalid_later_events_have_no_partial_output(self):
        for steps, phrase in (
            (("add", "add"), "already exists"),
            (("delete", "delete"), "unknown change path"),
            (("delete", "content"), "unknown change path"),
        ):
            path = "new.js" if steps[0] == "add" else "src/app.js"
            with self.subTest(steps=steps):
                self.history(*[[{"path": path, "kind": kind}] for kind in steps])
                self.assert_cli_error("--mode", "cumulative-history", "--json", phrase=phrase)
        self.history([], [{"path": "package-lock.json", "kind": "delete"}])
        self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="step-2 / after")
        self.history([{"path": "new/child", "kind": "add"}], [{"path": "new", "kind": "add"}])
        self.assert_cli_error("--mode", "cumulative-history", "--json", phrase="already exists")

    def test_downstream_metadata_invalidation_and_weights(self):
        self.after.write_text("FROM scratch\nCOPY src /app/\nENV X=fixed\nARG Y=fixed\nWORKDIR /app\nRUN echo inert\n")
        self.weights.write_text(json.dumps({"schemaVersion": 1, "before": {}, "after": {"I003": 0.5, "I006": 2}}))
        report = self.run_model(weighted=True)
        self.assertEqual(report["scenarios"][0]["after"]["invalidatedInstructionIds"], ["I002", "I003", "I004", "I005", "I006"])
        self.assertEqual(report["scenarios"][0]["after"]["invalidatedModelUnits"], 2.5)
        self.assertEqual(report["scenarios"][0]["savedModelUnits"], -0.5)

    def test_pinned_and_scratch_image_flags(self):
        self.before.write_text("FROM example@sha256:" + "a" * 64 + "\nCOPY . /app/\n")
        self.after.write_text("FROM scratch\nCOPY . /app/\n")
        report = self.run_model()
        self.assertFalse(report["dockerfiles"]["before"]["mutableFrom"])
        self.assertFalse(report["dockerfiles"]["after"]["mutableFrom"])

    def test_literal_registry_port_tag_and_digest(self):
        for image in ("registry.example:5000/team/app:Tag_1", "example.com/team/app:tag@sha256:" + "a" * 64):
            with self.subTest(image=image):
                self.after.write_text(f"FROM {image}\nCOPY . /app/\n")
                self.assertEqual(self.run_model()["dockerfiles"]["after"]["image"], image)

    def test_invalid_image_references_are_not_evaluated(self):
        for image in ("https://example.com/app", "app@bad", "app@sha256:abcd", "app:",
                      "app::tag", "registry:99999/app", "registry:/app", "UPPER/repo", "a//b"):
            with self.subTest(image=image):
                self.after.write_text(f"FROM {image}\nCOPY . /app/\n")
                self.assert_cli_error("--json")

    def test_no_execution_no_network_no_context_content_reads_or_writes(self):
        self.after.write_text(f"FROM scratch\nCOPY . /app/\nRUN touch {self.root / 'marker'}; curl https://invalid.example\n")
        before_contents = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.context.rglob("*") if p.is_file()}
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.context):
                raise AssertionError("context contents must not be read or written")
            return original_open(path, *args, **kwargs)

        with ExitStack() as stack:
            for target in ("subprocess.run", "subprocess.Popen", "os.system", "os.popen", "os.execve", "socket.create_connection", "socket.socket"):
                stack.enter_context(patch(target, side_effect=AssertionError("no execution/network")))
            stack.enter_context(patch.object(Path, "open", guarded_open))
            report = self.run_model()
            history_report = self.run_model(mode="cumulative-history")
        self.assertEqual(report["scenarios"][0]["after"]["invalidatedRunIds"], ["I003"])
        self.assertEqual(history_report["scenarios"][0]["after"]["invalidatedRunIds"], ["I003"])
        self.assertFalse((self.root / "marker").exists())
        self.assertEqual(before_contents, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.context.rglob("*") if p.is_file()})
        # The real CLI subprocess also succeeds with an empty executable search path.
        result = self.cli("--json", environment={**os.environ, "PATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "marker").exists())

    def test_unsupported_dockerfile_constructs_cli(self):
        cases = [
            "ADD . /app/", "COPY --from=builder /x /x", "COPY --chown=1 src /app/",
            "FROM alpine", "RUN --mount=type=cache,target=/tmp echo no", "RUN <<EOF",
            "CMD echo no", "LABEL x=y", "COPY https://example.invalid/a /app/",
            "COPY ../secret /app/", "COPY /absolute /app/", "COPY $SRC /app/",
            "COPY src/** /app/", "COPY src/[ab].js /app/", "COPY src/{a,b} /app/",
            "COPY src//app.js /app/", "WORKDIR relative", "WORKDIR $HOME",
            'WORKDIR /a"p"p',
            "ENV X=$Y", "ENV X value", "ARG X", "ARG X=$Y", "COPY src package.json /app",
            'COPY ["src", 2]', 'COPY ["src", "/app/",]', 'RUN ["echo", 2]',
            'RUN [' + '9' * 10000 + ']', "RUN echo \x00invalid",
        ]
        for instruction in cases:
            with self.subTest(instruction=instruction):
                self.after.write_text("FROM scratch\n" + instruction + "\n")
                self.assert_cli_error("--json")

    def test_invalid_first_instruction_directive_continuation_and_empty_cli(self):
        for text in ["", "# only comment\n", "ARG X=y\nFROM scratch\n", "FROM alpine AS stage\n",
                     "FROM --platform=linux/amd64 alpine\n", "FROM ${IMAGE}\n", "FROM scratch\nCOPY . \\\n",
                     "# syntax=docker/dockerfile:1\nFROM scratch\n", "# escape=`\nFROM scratch\n",
                     "# check=skip=all\nFROM scratch\n", "FROM scratch\nRUN\n", 'FROM scratch\nCOPY "unterminated /app/\n',
                     "FROM scratch\n\\\n"]:
            with self.subTest(text=text):
                self.after.write_text(text)
                self.assert_cli_error("--json")

    def test_nonexistent_and_empty_copy_sources_cli(self):
        (self.context / "empty").mkdir()
        for source in ("missing", "empty", "no*.json"):
            with self.subTest(source=source):
                self.after.write_text(f"FROM scratch\nCOPY {source} /app/\n")
                self.assert_cli_error("--json", phrase="matches no regular files")

    def test_deleted_required_source_fails_entire_comparison(self):
        self.events([{"path": "package-lock.json", "kind": "delete"}])
        self.assert_cli_error("--json", phrase="after")

    def test_unknown_path_cli_and_bundled_failure(self):
        self.events([{"path": "missing.js", "kind": "content"}])
        self.assert_cli_error("--json", phrase="unknown change path")
        result = self.cli("--json", changes=EXAMPLES / "changes.invalid.json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_invalid_change_events(self):
        cases = [
            [{"path": "missing", "kind": "mtime"}], [{"path": "missing", "kind": "delete"}],
            [{"path": "src", "kind": "content"}], [{"path": "src/app.js", "kind": "add"}],
            [{"path": "src", "kind": "add"}], [{"path": "src/app.js/child", "kind": "add"}],
            [{"path": "./src/app.js", "kind": "content"}], [{"path": "src/app.js/", "kind": "content"}],
            [{"path": "../other", "kind": "add"}], [{"path": "/other", "kind": "add"}],
            [{"path": "new*.js", "kind": "add"}], [{"path": {}, "kind": "content"}],
            [{"path": "src/app.js", "kind": "mode"}], [{"path": "src/app.js", "kind": []}],
            [{"path": "src/app.js", "kind": "mtime", "extra": True}],
            [{"path": "src/app.js", "kind": "mtime"}] * 2,
            [{"path": "new", "kind": "add"}, {"path": "new/child", "kind": "add"}],
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.events(changes)
                with self.assertRaises(InputError):
                    self.run_model()

    def test_invalid_schema_and_unknown_fields(self):
        cases = [[], {}, {"schemaVersion": True, "scenarios": []}, {"schemaVersion": 2, "scenarios": []},
                 {"schemaVersion": 1, "scenarios": []}, {"schemaVersion": 1, "scenarios": {}},
                 {"schemaVersion": 1, "scenarios": [{"id": "ok", "changes": None}]},
                 {"schemaVersion": 1, "scenarios": [{"id": "spaces no", "changes": []}]},
                 {"schemaVersion": 1, "scenarios": [{"id": "same", "changes": []}] * 2},
                 {"schemaVersion": 1, "scenarios": [{"id": "ok", "changes": [], "silent": 1}]}]
        for data in cases:
            with self.subTest(data=data):
                self.changes.write_text(json.dumps(data))
                self.assert_cli_error("--json")

    def test_invalid_weights_cli(self):
        for value in (-1, True, "seconds", None, [], {}, 1e13, float("inf"), float("nan")):
            with self.subTest(value=value):
                self.weights.write_text(json.dumps({"schemaVersion": 1, "before": {"I004": value}, "after": {}}))
                self.assert_cli_error("--json", "--weights", str(self.weights))
        self.weights.write_text('{"schemaVersion":1,"before":{"I999":3},"after":{}}')
        self.assert_cli_error("--json", "--weights", str(self.weights), phrase="unknown instruction ID")
        for data in (None, [], {"schemaVersion": True, "before": {}, "after": {}},
                     {"schemaVersion": 1, "before": [], "after": {}},
                     {"schemaVersion": 1, "before": {}, "after": {}, "extra": 0}):
            with self.subTest(data=data):
                self.weights.write_text(json.dumps(data))
                self.assert_cli_error("--weights", str(self.weights))

    def test_duplicate_json_keys_and_malformed_inputs(self):
        for text in ('{"schemaVersion":1,"schemaVersion":1,"scenarios":[]}', '{', 'NaN',
                     '{"schemaVersion":1,"scenarios":[],"extra":Infinity}'):
            with self.subTest(text=text):
                self.changes.write_text(text)
                self.assert_cli_error("--json")
        self.changes.write_bytes(b"\xff\xfe")
        self.assert_cli_error("--json")
        self.changes.write_text('{"schemaVersion":' + '9' * 10000 + ',"scenarios":[]}')
        self.assert_cli_error("--json")

    def test_size_limit_missing_files_and_cli_usage(self):
        self.changes.write_text(" " * (5 * 1024 * 1024 + 1))
        self.assert_cli_error("--json", phrase="exceeds")
        self.changes.unlink()
        self.assert_cli_error("--json")
        result = subprocess.run(CLI, env=cli_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("required", result.stderr)

    def test_text_input_fifo_is_rejected_without_reading(self):
        self.changes.unlink()
        os.mkfifo(self.changes)
        self.assert_cli_error("--json", phrase="regular file")

    def test_inventory_scan_errors_are_not_silently_ignored(self):
        original_walk = Path.walk

        def failing_walk(path, *args, **kwargs):
            if path == self.context:
                kwargs["on_error"](PermissionError("synthetic directory enumeration failure"))
            return original_walk(path, *args, **kwargs)

        with patch.object(Path, "walk", failing_walk):
            with self.assertRaisesRegex(PermissionError, "enumeration failure"):
                self.run_model()

    def test_dockerignore_and_dockerfile_specific_ignore_rejected(self):
        for path in (self.context / ".dockerignore", self.after.with_name(self.after.name + ".dockerignore")):
            with self.subTest(path=path):
                path.write_text("")
                self.assert_cli_error("--json", phrase="ignore rules are unsupported")
                path.unlink()

    def test_adding_ignore_or_changing_an_input_dockerfile_is_unsupported(self):
        self.events([{"path": ".dockerignore", "kind": "add"}])
        self.assert_cli_error("--json", phrase="Dockerfile/ignore-file changes")
        self.after = self.context / "Dockerfile"
        self.after.write_text("FROM scratch\nCOPY . /app/\n")
        self.events([{"path": "Dockerfile", "kind": "content"}])
        self.assert_cli_error("--json", phrase="Dockerfile/ignore-file changes")
        self.events([{"path": "Dockerfile.dockerignore", "kind": "add"}])
        self.assert_cli_error("--json", phrase="Dockerfile/ignore-file changes")
        self.events([{"path": "Dockerfile", "kind": "mtime"}])
        self.assertEqual(self.run_model()["scenarios"][0]["after"]["invalidatedRunIds"], [])

    def test_symlinks_and_special_files_rejected(self):
        link = self.context / "link"
        for target in ("src", "src/app.js", "/nonexistent"):
            with self.subTest(target=target):
                link.symlink_to(target)
                self.assert_cli_error("--json", phrase="symlinks are unsupported")
                link.unlink()
        pipe = self.context / "pipe"
        os.mkfifo(pipe)
        self.assert_cli_error("--json", phrase="only regular")
        pipe.unlink()
        root_link = self.root / "context-link"
        root_link.symlink_to(self.context, target_is_directory=True)
        with self.assertRaises(InputError):
            scan_context(root_link, (self.before, self.after))

    def test_ambiguous_inventory_names_rejected(self):
        for name in ("a[b]", "file:colon", "a\\b", "a\nb"):
            with self.subTest(name=name):
                path = self.context / name
                path.write_text("synthetic")
                self.assert_cli_error("--json")
                path.unlink()

    def test_later_invalid_scenario_produces_no_partial_output(self):
        self.changes.write_text(json.dumps({"schemaVersion": 1, "scenarios": [
            {"id": "valid", "changes": [{"path": "src/app.js", "kind": "content"}]},
            {"id": "invalid", "changes": [{"path": "package-lock.json", "kind": "delete"}]}]}))
        self.assert_cli_error("--json", phrase="invalid / after")


if __name__ == "__main__":
    unittest.main()
