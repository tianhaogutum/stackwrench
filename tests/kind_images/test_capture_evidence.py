"""Current capture metadata is visible evidence, never a readiness/freshness gate."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.kind_images.planner import build_report


CLI = [sys.executable, "-m", "stackwrench", "kind-images"]
STAMP = "2099-01-02T03:04:05.123456+02:30"
SOURCE = 'caller label: "MISSING" https://invalid.example/never-fetch'


class CaptureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inventory = self.root / "nodes.json"
        self.manifest = self.root / "pod.json"
        self.write(self.manifest, {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "demo"},
            "spec": {"containers": [{"name": "app", "image": "demo:v1", "imagePullPolicy": "Never"}]}})
        self.write(self.root / "images.json", {"images": [{"id": "opaque", "repoTags": ["demo:v1"],
            "repoDigests": ["demo@sha256:" + "a" * 64]}]})
        self.write(self.root / "empty.json", {"images": []})
        # Deliberately unsorted input; evidence includes excluded nodes.
        self.data = {"schemaVersion": 1, "cluster": "demo", "nodes": {
            "node-unknown": {"schedulable": True},
            "node-excluded": {"schedulable": False, "snapshot": "empty.json", "source": "MISSING"},
            "node-b": {"schedulable": True, "snapshot": "empty.json"},
            "node-a": {"schedulable": True, "snapshot": "images.json", "capturedAt": STAMP, "source": SOURCE}}}
        self.write(self.inventory, self.data)

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def report(self, baseline=None, inventory_only=False):
        return build_report(None if inventory_only else [self.manifest], self.inventory, baseline)

    def cli(self, *flags, inventory_only=False):
        manifests = [] if inventory_only else ["--manifests", str(self.manifest)]
        return subprocess.run([*CLI, *manifests,
            "--inventory", str(self.inventory), *flags], cwd=self.root, capture_output=True,
            text=True, timeout=10, env={**os.environ, "PATH": "", "KUBECONFIG": str(self.root / "absent")})

    def test_comparison_text_distinguishes_supplied_missing_label_from_gaps(self):
        baseline = self.root / "baseline.json"
        self.write(baseline, self.data)
        for inventory_only in (False, True):
            with self.subTest(inventory_only=inventory_only):
                result = self.cli("--baseline-inventory", str(baseline), "--check",
                                  inventory_only=inventory_only)
                self.assertEqual(result.returncode, 1, result.stderr)
                comparison = result.stdout.split("Inventory comparison (explicit baseline -> current):", 1)[1]
                for side in ("before", "after"):
                    section = comparison.split(f"  {side}: ", 1)[1].split("\n  after: ", 1)[0].split("\n  Nodes added:", 1)[0]
                    self.assertIn('node-excluded schedulable=false snapshot="empty.json" '
                                  'capturedAt=MISSING source="MISSING" missingEvidence=["capturedAt"]', section)
                    self.assertIn('node-b schedulable=true snapshot="empty.json" capturedAt=MISSING '
                                  'source=MISSING missingEvidence=["capturedAt", "source"]', section)
                    self.assertIn('node-unknown schedulable=true snapshot=MISSING capturedAt=MISSING '
                                  'source=MISSING missingEvidence=["snapshot", "capturedAt", "source"]', section)

    def test_comparison_text_quotes_evidence_paths_and_labels(self):
        self.inventory = self.root / 'current "quoted".json'
        baseline = self.root / 'baseline "quoted".json'
        snapshot = 'capture "quoted".json'
        (self.root / "images.json").rename(self.root / snapshot)
        source = 'operator "A" path C:\\capture → source=MISSING'
        self.data["nodes"]["node-a"].update(snapshot=snapshot, source=source)
        self.write(self.inventory, self.data)
        before = copy.deepcopy(self.data)
        before["nodes"]["node-a"]["capturedAt"] = "2099-12-31T23:00:00Z"
        self.write(baseline, before)
        for inventory_only in (False, True):
            with self.subTest(inventory_only=inventory_only):
                result = self.cli("--baseline-inventory", str(baseline), inventory_only=inventory_only)
                self.assertEqual(result.returncode, 0, result.stderr)
                comparison = result.stdout.split("Inventory comparison (explicit baseline -> current):", 1)[1]
                for side, path, stamp in (("before", baseline, "2099-12-31T23:00:00Z"),
                                          ("after", self.inventory, STAMP)):
                    section = comparison.split(f"  {side}: ", 1)[1].split("\n  after: ", 1)[0].split("\n  Nodes added:", 1)[0]
                    self.assertEqual(json.loads(section.splitlines()[0]), str(path))
                    for phrase in (f"snapshot={json.dumps(snapshot)}", f"capturedAt={json.dumps(stamp)}",
                                   f"source={json.dumps(source)} missingEvidence=[]"):
                        self.assertIn(phrase, section)

    def test_current_capture_evidence_is_visible_without_baseline(self):
        report = self.report()
        self.assertNotIn("inventoryDiff", report)
        evidence = report["currentInventoryEvidence"]
        self.assertEqual(evidence["inventory"], str(self.inventory))
        self.assertEqual(evidence["nodes"], [
            {"name": "node-a", "schedulable": True, "snapshot": "images.json", "capturedAt": STAMP,
             "source": SOURCE, "missingEvidence": []},
            {"name": "node-b", "schedulable": True, "snapshot": "empty.json", "capturedAt": None,
             "source": None, "missingEvidence": ["capturedAt", "source"]},
            {"name": "node-excluded", "schedulable": False, "snapshot": "empty.json", "capturedAt": None,
             "source": "MISSING", "missingEvidence": ["capturedAt"]},
            {"name": "node-unknown", "schedulable": True, "snapshot": None, "capturedAt": None,
             "source": None, "missingEvidence": ["snapshot", "capturedAt", "source"]}])
        self.assertIn("caller-supplied evidence only", evidence["evidenceNote"])
        self.assertIn("does not guarantee freshness", evidence["evidenceNote"])
        self.assertEqual([n["name"] for n in report["nodes"]], ["node-a", "node-b", "node-unknown"])
        self.assertEqual([n["availability"] for n in report["imageOverview"][0]["nodes"]],
                         ["present", "missing", "unassessed"])
        self.assertEqual(report["loadPlan"][0]["missingNodes"], ["node-b"])
        result = self.cli("--json", "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), report)
        text = self.cli("--check")
        self.assertEqual(text.returncode, 1, text.stderr)
        for phrase in ("Current inventory evidence (caller-supplied):", evidence["evidenceNote"],
                       f"capturedAt={json.dumps(STAMP)}", f"source={json.dumps(SOURCE)}",
                       'source="MISSING"', 'missingEvidence=["capturedAt", "source"]',
                       'snapshot=MISSING capturedAt=MISSING source=MISSING',
                       'node-excluded schedulable=false'):
            self.assertIn(phrase, text.stdout)

    def test_current_evidence_matches_diff_after_in_both_modes(self):
        baseline = self.root / "baseline.json"
        data = copy.deepcopy(self.data)
        data["nodes"]["node-a"]["capturedAt"] = "2099-12-31T23:00:00Z"
        data["nodes"]["node-a"]["source"] = "baseline evidence must stay separate"
        self.write(baseline, data)
        for inventory_only in (False, True):
            report = self.report(baseline, inventory_only)
            evidence = report["currentInventoryEvidence"]
            self.assertEqual({k: evidence[k] for k in ("inventory", "nodes")}, report["inventoryDiff"]["after"])
            self.assertEqual(evidence["evidenceNote"], report["inventoryDiff"]["evidenceNote"])
            self.assertEqual(evidence["nodes"][0]["source"], SOURCE)
            self.assertNotEqual(evidence["nodes"][0]["source"], report["inventoryDiff"]["before"]["nodes"][0]["source"])
            if inventory_only:
                for key in ("imageOverview", "assessments", "loadPlan", "loadPlanOmissions"):
                    self.assertEqual(report[key], [])
                self.assertFalse(report["summary"]["localUsePossibleEverywhere"])

    def test_metadata_gaps_and_future_timestamps_do_not_change_existing_assessments_or_check(self):
        self.data["nodes"] = {"node-a": self.data["nodes"]["node-a"]}
        self.write(self.inventory, self.data)
        original = self.report()
        self.assertEqual(self.cli("--json", "--check").returncode, 0)
        self.data["nodes"]["node-a"].pop("capturedAt")
        self.data["nodes"]["node-a"].pop("source")
        self.write(self.inventory, self.data)
        no_metadata = self.report()
        self.assertEqual(no_metadata["currentInventoryEvidence"]["nodes"][0]["missingEvidence"], ["capturedAt", "source"])
        original.pop("currentInventoryEvidence")
        no_metadata.pop("currentInventoryEvidence")
        self.assertEqual(no_metadata, original)
        self.assertEqual(self.cli("--json", "--check").returncode, 0)

    def test_invalid_current_metadata_and_captures_still_fail_atomically(self):
        for node, change in (("node-a", {"capturedAt": None}),
                             ("node-a", {"capturedAt": "2026-02-30T00:00:00Z"}),
                             ("node-a", {"source": " "}),
                             ("node-unknown", {"source": "label"}),
                             ("node-unknown", {"capturedAt": STAMP}),
                             ("node-excluded", {"snapshot": "absent.json"})):
            with self.subTest(node=node, change=change):
                data = copy.deepcopy(self.data)
                data["nodes"][node].update(change)
                self.write(self.inventory, data)
                for flags in (("--json",), ()):
                    result = self.cli(*flags)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)
        self.write(self.inventory, self.data)

    def test_current_evidence_is_offline_deterministic_and_read_only(self):
        before = {p: p.read_bytes() for p in self.root.glob("*.json")}
        expected = self.report()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.Popen", side_effect=AssertionError("process forbidden")), \
                patch("os.system", side_effect=AssertionError("shell forbidden")):
            self.assertEqual(self.report(), expected)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.glob("*.json")})
        first = self.cli("--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, self.cli("--json").stdout)


if __name__ == "__main__":
    unittest.main()
