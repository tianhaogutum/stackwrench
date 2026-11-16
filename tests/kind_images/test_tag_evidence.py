"""Current-node tag evidence is informational and never establishes runtime identity."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from stackwrench.kind_images.planner import build_report


ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "kind-images"]
IMAGE = "docker.io/library/demo:v1"
A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
DA, DB = ("docker.io/library/demo@" + d for d in (A, B))


class TagEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "workloads.json"
        self.inventory = self.root / "nodes.json"
        self.nodes = {}
        self.workloads()

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def workloads(self, *containers):
        self.write(self.manifest, {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "review"},
            "spec": {"containers": list(containers) or [{"name": "app", "image": "demo:v1"}]}})

    def record(self, *digests, tags=None):
        return {"id": A, "repoTags": ["demo:v1"] if tags is None else tags,
                "repoDigests": ["demo@" + d for d in digests]}

    def node(self, name, images=None, schedulable=True):
        entry = {"schedulable": schedulable}
        if images is not None:
            entry["snapshot"] = name + ".json"
            self.write(self.root / entry["snapshot"], {"images": images})
        self.nodes[name] = entry
        self.write(self.inventory, {"schemaVersion": 1, "cluster": "demo", "nodes": self.nodes})

    def report(self, baseline=None):
        return build_report([self.manifest], self.inventory, baseline)

    def evidence(self):
        return self.report()["imageOverview"][0]["tagDigestEvidence"]

    def cli(self, *flags):
        return subprocess.run([*CLI, "--manifests", str(self.manifest),
            "--inventory", str(self.inventory), *flags], cwd=self.root, capture_output=True,
            text=True, timeout=10, env={**os.environ, "PATH": "", "KUBECONFIG": str(self.root / "absent")})

    def test_complete_equal_sets_retain_every_record_and_normalize_duplicates(self):
        first = self.record(B, A, A, tags=["demo:v1", "index.docker.io/demo:v1"])
        first["repoDigests"].append("other@" + B)
        self.node("node-a", [first, self.record(A)])
        self.node("node-b", [self.record(A, B, tags=[IMAGE])])
        evidence = self.evidence()
        self.assertEqual(evidence["comparison"], "same-digest-evidence")
        self.assertFalse(evidence["knownDifference"])
        self.assertEqual([{k: v for k, v in n.items() if k != "recordComparison"}
                          for n in evidence["nodes"]], [
            {"node": "node-a", "status": "complete", "reason": "Every matching tag record has same-repository repoDigests.",
             "repoDigests": [DA, DB], "records": [
                 {"imageIndex": 0, "repoDigests": [DA, DB]}, {"imageIndex": 1, "repoDigests": [DA]}]},
            {"node": "node-b", "status": "complete", "reason": "Every matching tag record has same-repository repoDigests.",
             "repoDigests": [DA, DB], "records": [{"imageIndex": 0, "repoDigests": [DA, DB]}]}])
        self.assertIn("no runtime content-equivalence or freshness guarantee", evidence["note"])
        self.assertEqual(len(self.report()["imageOverview"][0]["nodes"][0]["matches"]), 3)
        self.assertEqual(evidence["nodes"][0]["recordComparison"]["comparison"], "different-digest-evidence")

    def test_complete_different_sets_are_visible_but_check_and_loads_do_not_change(self):
        self.node("node-a", [self.record(A)])
        self.node("node-b", [self.record(B)])
        result = self.cli("--json", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        evidence = report["imageOverview"][0]["tagDigestEvidence"]
        self.assertEqual(evidence["comparison"], "different-digest-evidence")
        self.assertTrue(evidence["knownDifference"])
        self.assertEqual(report["loadPlan"], [])
        self.assertEqual(report["loadPlanOmissions"], [])
        self.assertEqual(report["summary"]["verdicts"], {"local-available": 2})
        text = self.cli("--check")
        self.assertEqual(text.returncode, 0, text.stderr)
        for phrase in ("Tag repository-digest evidence: different-digest-evidence",
                       "Known difference between complete nodes: true", DA, DB,
                       "no runtime content-equivalence or freshness guarantee"):
            self.assertIn(phrase, text.stdout)

    def test_gaps_stay_unassessed_without_hiding_known_differences_or_policy_warnings(self):
        self.workloads(*[{"name": p.lower(), "image": "demo:v1", "imagePullPolicy": p}
                         for p in ("Always", "IfNotPresent", "Never")])
        self.node("node-a", [self.record(A)])
        self.node("node-b", [self.record(B)])
        self.node("node-c", [self.record(A), self.record()])
        self.node("node-missing", [])
        self.node("node-unknown")
        report = self.report()
        group = report["imageOverview"][0]
        evidence = group["tagDigestEvidence"]
        self.assertEqual(evidence["comparison"], "unassessed")
        self.assertTrue(evidence["knownDifference"])
        self.assertEqual([n["status"] for n in evidence["nodes"]],
                         ["complete", "complete", "unassessed", "unassessed", "unassessed"])
        self.assertEqual(evidence["nodes"][2]["records"],
                         [{"imageIndex": 0, "repoDigests": [DA]}, {"imageIndex": 1, "repoDigests": []}])
        self.assertEqual([n["repoDigests"] for n in evidence["nodes"]], [[DA], [DB], [DA], [], []])
        for node in evidence["nodes"][2:]:
            self.assertTrue(node["reason"])
        self.assertEqual([n["availability"] for n in group["nodes"]],
                         ["present", "present", "present", "missing", "unassessed"])
        self.assertEqual(report["loadPlan"][0]["missingNodes"], ["node-missing"])
        self.assertTrue(report["loadPlan"][0]["requiresPolicyReview"])
        self.assertEqual([u["effectivePullPolicy"] for u in group["uses"]], ["Always", "IfNotPresent", "Never"])
        self.assertIn("Always", group["uses"][0]["policyRisk"])
        self.assertTrue(all("Always" in n["policyRisk"] for n in report["assessments"][0]["nodes"]))
        self.assertEqual(report["summary"]["verdicts"], {"local-available": 6, "missing-required-local": 1,
            "unassessed": 3, "will-request-registry": 4, "would-pull-if-scheduled": 1})
        result = self.cli("--json", "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        text = self.cli()
        self.assertIn("Tag repository-digest evidence: unassessed", text.stdout)
        self.assertIn("Known difference between complete nodes: true", text.stdout)
        self.assertIn("record 1: []", text.stdout)
        self.assertIn("Policy risk: Always", text.stdout)

    def test_ids_other_records_repositories_and_partial_sets_cannot_establish_comparison(self):
        other = self.record(tags=["other:v1"])
        other["repoDigests"] = ["other@" + A]
        separate = self.record(A, tags=[])
        for images, retained in (([self.record(), other, separate], []),
                ([{**self.record(), "repoDigests": ["other@" + A]}, separate], []),
                ([self.record(A), self.record()], [DA]),
                ([self.record(B), self.record()], [DB]),
                ([{**self.record(), "repoDigests": None}], []),
                ([{"id": A, "repoTags": ["demo:v1"]}], [])):
            with self.subTest(images=images):
                self.node("node-a", [self.record(A)])
                self.node("node-b", images)
                evidence = self.evidence()
                self.assertEqual(evidence["comparison"], "unassessed")
                self.assertFalse(evidence["knownDifference"])
                self.assertEqual(evidence["nodes"][1]["status"], "unassessed")
                self.assertEqual(evidence["nodes"][1]["repoDigests"], retained)

    def test_repository_registry_port_and_tag_case_boundaries_stay_exact(self):
        self.node("node-a", [self.record(A), self.record(B, tags=["demo:V1"])])
        record = self.record(tags=["index.docker.io/library/demo:v1"])
        record["repoDigests"] = ["INDEX.DOCKER.IO/demo@" + A,
            "other@" + B, "registry-1.docker.io/library/demo@" + B,
            "docker.io:443/library/demo@" + B]
        self.node("node-b", [record])
        evidence = self.evidence()
        self.assertEqual(evidence["comparison"], "same-digest-evidence")
        self.assertEqual([n["repoDigests"] for n in evidence["nodes"]], [[DA], [DA]])
        self.assertEqual(evidence["nodes"][0]["records"], [{"imageIndex": 0, "repoDigests": [DA]}])

    def test_zero_one_missing_or_excluded_nodes_cannot_prove_equality(self):
        for first, second in ((None, None), ([], []), ([self.record(A)], None),
                              ([self.record(A)], []), ([self.record(A)], [self.record()])):
            with self.subTest(first=first, second=second):
                self.node("node-a", first)
                self.node("node-b", second)
                self.node("node-excluded", [self.record(B)], schedulable=False)
                evidence = self.evidence()
                self.assertEqual(evidence["comparison"], "unassessed")
                self.assertFalse(evidence["knownDifference"])
                self.assertEqual([n["node"] for n in evidence["nodes"]], ["node-a", "node-b"])
        self.nodes.pop("node-b")
        self.node("node-a", [self.record(A)])
        self.assertEqual(self.evidence()["comparison"], "unassessed")

    def test_digest_groups_remain_exact_and_baseline_cannot_supply_current_evidence(self):
        self.workloads({"name": "tag", "image": "demo:v1"},
                       {"name": "digest", "image": "demo:v1@" + A})
        self.node("node-a", [self.record(A)])
        self.node("node-b", [self.record(A)])
        baseline = self.root / "before.json"
        self.write(self.root / "old.json", {"images": [self.record(A)]})
        self.write(baseline, {"schemaVersion": 1, "cluster": "demo", "nodes": {
            name: {"schedulable": True, "snapshot": "old.json"} for name in self.nodes}})
        self.node("node-b", [self.record()])
        report = self.report(baseline)
        groups = {g["image"]: g for g in report["imageOverview"]}
        self.assertEqual(groups[IMAGE]["tagDigestEvidence"]["comparison"], "unassessed")
        self.assertNotIn("tagDigestEvidence", groups[DA])
        self.assertEqual([n["present"] for n in groups[DA]["nodes"]], [True, False])
        self.assertEqual(report["loadPlan"], [])
        self.assertEqual(report["loadPlanOmissions"][0]["missingNodes"], ["node-b"])
        tags = [n["tags"][0] for n in report["inventoryDiff"]["imageChanges"]]
        self.assertEqual([t["comparison"] for t in tags], ["same-digest-evidence", "unassessed"])
        self.assertEqual(tags[1]["afterRepoDigests"], [])
        self.assertEqual(build_report(None, self.inventory, baseline)["imageOverview"], [])

    def test_new_evidence_is_deterministic_offline_and_read_only(self):
        self.node("node-b", [self.record(B)])
        self.node("node-a", [self.record(A), self.record()])
        paths = list(self.root.glob("*.json"))
        before = {p: p.read_bytes() for p in paths}
        expected = self.report()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.Popen", side_effect=AssertionError("process forbidden")), \
                patch("os.system", side_effect=AssertionError("shell forbidden")):
            self.assertEqual(self.report(), expected)
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        first = self.cli("--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, self.cli("--json").stdout)

    def test_record_disagreement_is_explicit_even_when_node_unions_are_equal(self):
        self.node("node-a", [self.record(A), self.record(B)])
        self.node("node-b", [self.record(A, B)])
        evidence = self.evidence()
        # Existing cross-node comparison works, but hides this distinction in its union.
        self.assertEqual(evidence["comparison"], "same-digest-evidence")
        self.assertFalse(evidence["knownDifference"])
        self.assertEqual([n["repoDigests"] for n in evidence["nodes"]], [[DA, DB], [DA, DB]])
        a, b = [n["recordComparison"] for n in evidence["nodes"]]
        self.assertEqual(a["comparison"], "different-digest-evidence")
        self.assertTrue(a["knownDifference"])
        self.assertEqual(b["comparison"], "unassessed")
        self.assertFalse(b["knownDifference"])
        self.assertIn("At least two", b["reason"])
        result = self.cli("--json", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["loadPlan"], [])
        self.assertEqual(report["summary"]["verdicts"], {"local-available": 2})
        text = self.cli("--check")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Within-node tag records: different-digest-evidence", text.stdout)
        self.assertIn("Known difference between complete records: true", text.stdout)
        self.assertIn("no runtime content-equivalence or freshness guarantee", text.stdout)

    def test_record_comparison_preserves_differences_alongside_incomplete_records(self):
        for records, known in (([self.record(A), self.record(B), self.record()], True),
                               ([self.record(A), self.record()], False),
                               ([self.record(), self.record()], False)):
            with self.subTest(records=records):
                self.node("node-a", records)
                self.node("node-b", [self.record(A, B)])
                evidence = self.evidence()
                node = evidence["nodes"][0]
                self.assertEqual(node["status"], "unassessed")
                self.assertEqual(evidence["comparison"], "unassessed")
                self.assertFalse(evidence["knownDifference"])
                self.assertEqual(node["recordComparison"]["comparison"], "unassessed")
                self.assertEqual(node["recordComparison"]["knownDifference"], known)
                self.assertIn("lack same-repository repoDigests", node["recordComparison"]["reason"])
                self.assertEqual(node["records"][-1]["repoDigests"], [])
                text = self.cli()
                self.assertIn("Within-node tag records: unassessed", text.stdout)
                self.assertIn("Known difference between complete records: " + str(known).lower(), text.stdout)

    def test_record_comparison_deduplicates_aliases_and_ignores_unrelated_evidence(self):
        aliases = self.record(A, A, tags=["demo:v1", IMAGE, "index.docker.io/demo:v1"])
        aliases["repoDigests"] += ["index.docker.io/library/demo@" + A, "other@" + B,
                                  "docker.io:443/library/demo@" + B]
        unrelated = self.record(B, tags=["demo:V1", "other:v1"])
        self.node("node-a", [aliases, unrelated, self.record(B, tags=[])])
        row = self.evidence()["nodes"][0]
        self.assertEqual(row["records"], [{"imageIndex": 0, "repoDigests": [DA]}])
        self.assertEqual(row["recordComparison"]["comparison"], "unassessed")
        self.assertFalse(row["recordComparison"]["knownDifference"])
        # Distinct CRI records count even with the same ID; aliases on one do not.
        self.node("node-a", [aliases, self.record(A), unrelated])
        row = self.evidence()["nodes"][0]
        self.assertEqual(row["recordComparison"]["comparison"], "same-digest-evidence")
        self.assertFalse(row["recordComparison"]["knownDifference"])
        self.assertEqual([r["imageIndex"] for r in row["records"]], [0, 1])

    def test_record_comparison_cannot_use_missing_tags_captures_or_excluded_nodes(self):
        self.node("node-a", [self.record(A)])
        self.node("node-missing", [self.record(B, tags=[])])
        self.node("node-unknown")
        self.node("node-excluded", [self.record(A), self.record(B)], schedulable=False)
        evidence = self.evidence()
        self.assertEqual([n["node"] for n in evidence["nodes"]], ["node-a", "node-missing", "node-unknown"])
        for node in evidence["nodes"]:
            self.assertEqual(node["recordComparison"]["comparison"], "unassessed")
            self.assertFalse(node["recordComparison"]["knownDifference"])
            self.assertIn("At least two", node["recordComparison"]["reason"])


if __name__ == "__main__":
    unittest.main()
