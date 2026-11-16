"""Offline two-inventory integration contract, including installed CLI output."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.kind_images.inputs import InputError
from stackwrench.kind_images.planner import build_report

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "kind-images"]
A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
IMAGE = "docker.io/library/demo:v1"
OLD = "docker.io/library/old:v1"


class InventoryDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.before = self.root / "before/nodes.json"
        self.after = self.root / "after/nodes.json"
        for path in (self.before, self.after):
            path.parent.mkdir()
        self.manifest = self.root / "pod.json"
        self.write(self.manifest, {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "demo"},
            "spec": {"containers": [{"name": "demo", "image": "demo:v1", "imagePullPolicy": "Never"},
                                     {"name": "old", "image": "old:v1"}]}})
        self.before_data = {"schemaVersion": 1, "cluster": "demo", "nodes": {
            "node-a": {"schedulable": True, "snapshot": "a.json"},
            "node-old": {"schedulable": True, "snapshot": "old.json"},
            "node-excluded": {"schedulable": False}}}
        self.after_data = {"schemaVersion": 1, "cluster": "demo", "nodes": {
            "node-a": {"schedulable": True, "snapshot": "a.json"},
            "node-new": {"schedulable": True, "snapshot": "new.json"},
            "node-unknown": {"schedulable": True},
            "node-excluded": {"schedulable": False}}}
        self.write(self.before, self.before_data)
        self.write(self.after, self.after_data)
        self.write(self.before.parent / "a.json", {"images": [self.image("demo:v1", A), self.image("old:v1")]})
        self.write(self.before.parent / "old.json", {"images": [self.image("demo:v1", A)]})
        self.write(self.after.parent / "a.json", {"images": [self.image("docker.io/library/demo:v1", B)]})
        self.write(self.after.parent / "new.json", {"images": []})

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def image(self, tag, digest=None):
        return {"id": "opaque-id", "repoTags": [tag],
                "repoDigests": ["demo@" + digest] if digest else []}

    def cli(self, *extra, inventory_only=False):
        intent = [] if inventory_only else ["--manifests", str(self.manifest)]
        return subprocess.run([*CLI, *intent, "--inventory", str(self.after),
                               "--baseline-inventory", str(self.before), *extra],
                              cwd=self.root, capture_output=True, text=True, timeout=10,
                              env={**os.environ, "PATH": "", "KUBECONFIG": str(self.root / "absent")})

    def report(self):
        result = self.cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_inventory_only_comparison_preserves_diff_without_inventing_workload_intent(self):
        result = self.cli("--json", inventory_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schemaVersion"], 2)
        self.assertEqual(report["mode"], "inventory-only")
        # These requested images are already observed, so both modes have the same diff.
        with_intent = self.report()
        self.assertEqual(report["inventoryDiff"], with_intent["inventoryDiff"])
        for key in ("nodes", "unassessedNodes", "excludedNodes", "assumptions", "cluster"):
            self.assertEqual(report[key], with_intent[key])
        for key in ("assessments", "imageOverview", "unassessed", "loadPlan", "loadPlanOmissions"):
            self.assertEqual(report[key], [])
        self.assertEqual(report["summary"], {"containers": 0, "nodeChecks": 0, "verdicts": {},
            "unassessed": 0, "localUsePossibleEverywhere": False})
        self.assertEqual([p["missingNodes"] for p in with_intent["loadPlan"]],
                         [["node-new"], ["node-a", "node-new"]])
        self.assertNotIn("mode", with_intent)
        text = self.cli(inventory_only=True)
        self.assertEqual(text.returncode, 0, text.stderr)
        for expected in ("Inventory-only", "no workload readiness assessed", "Inventory comparison",
                         "digest-evidence-changed", "capturedAt=MISSING", "source=MISSING", "node-unknown"):
            self.assertIn(expected, text.stdout)
        self.assertNotIn('["kind", "load"', text.stdout)

    def test_inventory_only_coverage_is_observed_references_not_implicit_requirements(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["spec"]["containers"].extend([
            {"name": "unseen", "image": "unseen:v1"},
            {"name": "pinned", "image": "unseen@" + A}])
        self.write(self.manifest, manifest)
        with_intent = self.report()
        result = self.cli("--json", inventory_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        images = [r["image"] for r in report["inventoryDiff"]["coverage"]]
        self.assertEqual(images, sorted([IMAGE, OLD, "docker.io/library/demo@" + A,
                                         "docker.io/library/demo@" + B]))
        self.assertIn("docker.io/library/unseen:v1", [p["image"] for p in with_intent["loadPlan"]])
        self.assertEqual(with_intent["loadPlanOmissions"][0]["image"], "docker.io/library/unseen@" + A)
        self.assertEqual(report["loadPlan"], [])
        self.assertEqual(report["loadPlanOmissions"], [])

    def test_inventory_only_check_cannot_certify_readiness_even_with_equal_complete_captures(self):
        for flags in (("--json", "--check"), ("--check",)):
            result = self.cli(*flags, inventory_only=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue(result.stdout)
        data = {"schemaVersion": 1, "cluster": "demo", "nodes": {
            "node-a": {"schedulable": True, "snapshot": "a.json"}}}
        for inventory in (self.before, self.after):
            self.write(inventory, data)
            self.write(inventory.parent / "a.json", {"images": []})
        result = self.cli("--json", "--check", inventory_only=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["inventoryDiff"]["coverage"], [])
        self.assertEqual(report["unassessedNodes"], [])
        self.assertFalse(report["summary"]["localUsePossibleEverywhere"])
        # Removing a capture remains unknown, never evidence of deleted images.
        data["nodes"]["node-a"].pop("snapshot")
        self.write(self.after, data)
        report = json.loads(self.cli("--json", inventory_only=True).stdout)
        self.assertEqual(report["inventoryDiff"]["imageChanges"], [])
        self.assertEqual(report["inventoryDiff"]["unassessedComparisons"],
                         [{"node": "node-a", "missingSnapshots": ["after"]}])
        self.assertEqual(report["unassessedNodes"][0]["node"], "node-a")

    def test_inventory_only_retains_atomic_validation_on_both_sides_and_excluded_nodes(self):
        for inventory, original in ((self.before, self.before_data), (self.after, self.after_data)):
            variants = []
            for change, reason in (({"typo": 1}, "unknown fields"),
                                   ({"schemaVersion": 2}, "schemaVersion"),
                                   ({"cluster": "other"}, "same cluster"),
                                   ({"nodes": {}}, "schedulable")):
                variants.append(({**original, **change}, reason))
            for field, value, reason in (("capturedAt", "2026-02-30T00:00:00Z", "capturedAt"),
                    ("source", None, "source"), ("snapshot", None, "snapshot"),
                    ("snapshot", "absent.json", "absent.json"), ("schedulable", "yes", "schedulable")):
                data = json.loads(json.dumps(original))
                data["nodes"]["node-excluded"][field] = value
                variants.append((data, reason))
            for data, reason in variants:
                with self.subTest(inventory=inventory, reason=reason):
                    self.write(inventory, data)
                    result = self.cli("--json", inventory_only=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(reason, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
            self.write(inventory, original)
            capture = inventory.parent / "a.json"
            original_capture = capture.read_bytes()
            for malformed in ({"images": None}, {"images": [{"id": "opaque", "repoTags": ["demo"]}]}):
                self.write(capture, malformed)
                result = self.cli("--json", inventory_only=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
            capture.write_bytes(original_capture)

    def test_inventory_only_requires_two_inventories_and_explicit_manifests_remain_validated(self):
        for args, reason in ((["--inventory", str(self.after)], "--baseline-inventory"),
                (["--baseline-inventory", str(self.before)], "--inventory"),
                (["--inventory", str(self.after), "--baseline-inventory", str(self.before),
                  "--manifests"], "--manifests")):
            result = subprocess.run([*CLI, *args, "--json"], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn(reason, result.stderr)
        # An explicitly supplied empty file must not activate inventory-only mode.
        self.manifest.write_text("# no objects\n")
        result = self.cli("--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("no Kubernetes objects", result.stderr)

    def test_inventory_only_api_does_not_read_manifests_and_is_offline_deterministic_read_only(self):
        paths = list(self.root.rglob("*.json"))
        before = {p: p.read_bytes() for p in paths}
        result = self.cli("--json", inventory_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(result.stdout, self.cli("--json", inventory_only=True).stdout)
        with patch("stackwrench.kind_images.planner.read_manifests", side_effect=AssertionError("no intent")), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.Popen", side_effect=AssertionError("process forbidden")), \
                patch("os.system", side_effect=AssertionError("shell forbidden")):
            for absent in (None, []):
                self.assertEqual(build_report(absent, self.after, self.before), report)
                with self.assertRaisesRegex(InputError, "baseline"):
                    build_report(absent, self.after)
        self.assertEqual(before, {p: p.read_bytes() for p in paths})

    def test_two_inventory_diff_tracks_content_node_changes_and_load_coverage(self):
        report = self.report()
        diff = report["inventoryDiff"]
        self.assertEqual(diff["schemaVersion"], 1)
        self.assertEqual(diff["nodesAdded"], ["node-new", "node-unknown"])
        self.assertEqual(diff["nodesRemoved"], ["node-old"])
        self.assertEqual(diff["schedulabilityChanges"], [])
        changes = diff["imageChanges"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["node"], "node-a")
        self.assertEqual(changes[0]["removedReferences"], ["docker.io/library/demo@" + A, OLD])
        self.assertEqual(changes[0]["addedReferences"], ["docker.io/library/demo@" + B])
        tag = changes[0]["tags"][0]
        self.assertEqual({k: v for k, v in tag.items() if k not in ("recordSetComparison", "recordEvidence")},
            {"image": IMAGE, "beforeRepoDigests": ["docker.io/library/demo@" + A],
            "afterRepoDigests": ["docker.io/library/demo@" + B], "comparison": "digest-evidence-changed"})
        self.assertEqual(tag["recordSetComparison"]["comparison"], "different-digest-evidence")
        coverage = {entry["image"]: entry for entry in diff["coverage"]}
        self.assertEqual(coverage[IMAGE]["before"], {"presentNodes": ["node-a", "node-old"],
            "missingNodes": [], "unassessedNodes": []})
        self.assertEqual(coverage[IMAGE]["after"], {"presentNodes": ["node-a"],
            "missingNodes": ["node-new"], "unassessedNodes": ["node-unknown"]})
        self.assertEqual(coverage[OLD]["after"]["missingNodes"], ["node-a", "node-new"])
        for plan in report["loadPlan"]:
            self.assertEqual(plan["missingNodes"], coverage[plan["image"]]["after"]["missingNodes"])
        self.assertEqual(self.cli("--json", "--check").returncode, 1)
        text = self.cli()
        self.assertEqual(text.returncode, 0, text.stderr)
        for expected in ("Inventory comparison", "node-old", "node-new", "digest-evidence-changed", OLD,
                         "capturedAt=MISSING", "source=MISSING", "does not guarantee freshness"):
            self.assertIn(expected, text.stdout)

    def test_metadata_is_optional_explicit_and_not_a_freshness_claim(self):
        entry = self.before_data["nodes"]["node-a"]
        entry.update(capturedAt="2099-01-01T00:00:00+01:00", source="synthetic capture A")
        self.write(self.before, self.before_data)
        diff = self.report()["inventoryDiff"]
        evidence = diff["before"]["nodes"][0]
        self.assertEqual(evidence["capturedAt"], entry["capturedAt"])
        self.assertEqual(evidence["source"], entry["source"])
        self.assertEqual(evidence["missingEvidence"], [])
        evidence = diff["after"]["nodes"][0]
        self.assertIsNone(evidence["capturedAt"])
        self.assertIsNone(evidence["source"])
        self.assertEqual(evidence["missingEvidence"], ["capturedAt", "source"])
        unknown = next(n for n in diff["after"]["nodes"] if n["name"] == "node-unknown")
        self.assertEqual(unknown["missingEvidence"], ["snapshot", "capturedAt", "source"])
        self.assertIn("does not guarantee freshness", diff["evidenceNote"])

    def test_missing_digest_is_unassessed_and_ids_other_repositories_do_not_prove_equivalence(self):
        image = self.image("demo:v1")
        image["id"] = B
        image["repoDigests"] = ["other@" + B]
        self.write(self.after.parent / "a.json", {"images": [image]})
        tag = self.report()["inventoryDiff"]["imageChanges"][0]["tags"][0]
        self.assertEqual(tag["comparison"], "unassessed")
        self.assertEqual(tag["afterRepoDigests"], [])
        # A digest in a separate CRI record must not be associated with the tag.
        self.write(self.after.parent / "a.json", {"images": [self.image("demo:v1"),
            {"id": "second", "repoDigests": ["demo@" + A]}]})
        tag = self.report()["inventoryDiff"]["imageChanges"][0]["tags"][0]
        self.assertEqual(tag["comparison"], "unassessed")
        self.assertEqual(tag["afterRepoDigests"], [])

    def test_alias_order_and_metadata_only_changes_do_not_report_content_drift(self):
        self.write(self.after.parent / "a.json", {"images": [self.image("old:v1"),
            self.image("index.docker.io/library/demo:v1", A)]})
        changes = self.report()["inventoryDiff"]["imageChanges"][0]
        self.assertEqual(changes["addedReferences"], [])
        self.assertEqual(changes["removedReferences"], [])
        tags = {t["image"]: t for t in changes["tags"]}
        self.assertEqual(tags[IMAGE]["comparison"], "same-digest-evidence")
        self.assertEqual(tags[OLD]["comparison"], "unassessed")

    def test_capture_gaps_and_schedulability_changes_do_not_invent_image_disappearance(self):
        self.after_data["nodes"]["node-a"].pop("snapshot")
        self.after_data["nodes"]["node-excluded"] = {"schedulable": True, "snapshot": "new.json"}
        self.write(self.after, self.after_data)
        diff = self.report()["inventoryDiff"]
        self.assertEqual(diff["imageChanges"], [])
        self.assertEqual(diff["unassessedComparisons"], [{"node": "node-a", "missingSnapshots": ["after"]},
            {"node": "node-excluded", "missingSnapshots": ["before"]}])
        self.assertEqual(diff["schedulabilityChanges"], [{"node": "node-excluded", "before": False, "after": True}])
        row = next(r for r in diff["coverage"] if r["image"] == IMAGE)
        self.assertEqual(row["after"]["unassessedNodes"], ["node-a", "node-unknown"])
        self.assertEqual(row["after"]["missingNodes"], ["node-excluded", "node-new"])

    def test_invalid_baseline_or_evidence_fails_atomically(self):
        variants = [({**self.before_data, "cluster": "other"}, "same cluster"),
                    ({**self.before_data, "typo": 1}, "unknown fields")]
        for field, value in [("capturedAt", "today"), ("capturedAt", "2026-02-30T12:00:00Z"),
                             ("capturedAt", "2026-11-11T12:00:00"), ("capturedAt", None),
                             ("source", ""), ("source", 123), ("source", "bad\nsource")]:
            data = json.loads(json.dumps(self.before_data))
            data["nodes"]["node-a"][field] = value
            variants.append((data, field))
        for data, reason in variants:
            with self.subTest(data=data):
                self.write(self.before, data)
                result = self.cli("--json")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn(reason, result.stderr)
        self.write(self.before, self.before_data)
        self.write(self.before.parent / "a.json", {"images": None})
        result = self.cli("--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_diff_is_deterministic_offline_and_preserves_both_inputs(self):
        paths = list(self.root.rglob("*.json"))
        before = {p: p.read_bytes() for p in paths}
        report = self.report()
        self.assertEqual(self.cli("--json").stdout, self.cli("--json").stdout)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.Popen", side_effect=AssertionError("process forbidden")), \
                patch("os.system", side_effect=AssertionError("shell forbidden")):
            self.assertEqual(build_report([self.manifest], self.after, self.before), report)
        self.assertEqual(before, {p: p.read_bytes() for p in paths})

    def test_digest_coverage_matches_omissions_without_reloading_a_present_changed_tag(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["spec"]["containers"].append({"name": "pinned", "image": "demo@" + A})
        self.write(self.manifest, manifest)
        report = self.report()
        coverage = {r["image"]: r for r in report["inventoryDiff"]["coverage"]}
        omission = report["loadPlanOmissions"][0]
        self.assertEqual(omission["image"], "docker.io/library/demo@" + A)
        self.assertEqual(omission["missingNodes"], ["node-a", "node-new"])
        self.assertEqual(coverage[omission["image"]]["after"]["missingNodes"], omission["missingNodes"])
        tag_plan = next(p for p in report["loadPlan"] if p["image"] == IMAGE)
        self.assertEqual(tag_plan["missingNodes"], ["node-new"])

    def test_each_tag_record_needs_digest_evidence_and_repeated_aliases_are_deduplicated(self):
        complete = self.image("demo:v1", A)
        complete["repoTags"].append("docker.io/library/demo:v1")
        complete["repoDigests"].append("docker.io/library/demo@" + A)
        self.write(self.after.parent / "a.json", {"images": [complete, self.image("demo:v1")]})
        tag = self.report()["inventoryDiff"]["imageChanges"][0]["tags"][0]
        self.assertEqual(tag["afterRepoDigests"], ["docker.io/library/demo@" + A])
        self.assertEqual(tag["comparison"], "unassessed")

    def test_diff_is_informational_check_uses_current_inventory_and_policy_only(self):
        # Current content differs but its tag is present on every schedulable node.
        self.after_data["nodes"] = {"node-a": self.after_data["nodes"]["node-a"]}
        self.write(self.after, self.after_data)
        manifest = json.loads(self.manifest.read_text())
        manifest["spec"]["containers"].pop()
        self.write(self.manifest, manifest)
        result = self.cli("--json", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["inventoryDiff"]["imageChanges"][0]["tags"][0]["comparison"],
                         "digest-evidence-changed")
        # Even a future timestamp cannot make a missing capture into readiness.
        self.after_data["nodes"]["node-a"].pop("snapshot")
        self.after_data["nodes"]["node-a"]["capturedAt"] = "2099-01-01T00:00:00Z"
        self.write(self.after, self.after_data)
        result = self.cli("--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("require a snapshot", result.stderr)

    def test_documented_example_works_from_another_directory(self):
        result = subprocess.run([*CLI, "--manifests", str(ROOT / "examples/kind_images/diff/workloads.yaml"),
            "--inventory", str(ROOT / "examples/kind_images/diff/after.json"), "--baseline-inventory",
            str(ROOT / "examples/kind_images/diff/before.json"), "--json", "--check"],
            cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["inventoryDiff"]["nodesRemoved"], ["node-old"])
        self.assertEqual(report["inventoryDiff"]["imageChanges"][0]["tags"][0]["comparison"],
                         "digest-evidence-changed")
        self.assertEqual([p["missingNodes"] for p in report["loadPlan"]], [["node-new"], ["node-a", "node-new"]])


if __name__ == "__main__":
    unittest.main()
