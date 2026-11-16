"""Image-centric review contracts, including per-use warnings and CLI output."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from stackwrench.kind_images.planner import build_report


ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "kind-images"]
IMAGE = "docker.io/library/demo:latest"
DIGEST = "sha256:" + "a" * 64


class ImageOverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "workloads.yaml"
        self.other = self.root / "other.yaml"
        self.inventory = ROOT / "examples/kind_images/partial-nodes.json"

    def write(self, path, *docs):
        path.write_text(yaml.safe_dump_all(docs), encoding="utf-8")

    def pod(self, image="demo:latest", policy=None):
        container = {"name": "app", "image": image}
        if policy is not None:
            container["imagePullPolicy"] = policy
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "same", "namespace": "team"},
                "spec": {"containers": [container]}}

    def cli(self, manifests, *extra):
        return subprocess.run([*CLI, "--manifests", *map(str, manifests),
                               "--inventory", str(self.inventory), *extra],
                              capture_output=True, text=True, timeout=10, cwd=self.root,
                              env={**os.environ, "PATH": "", "KUBECONFIG": str(self.root / "absent")})

    def test_canonical_group_keeps_every_workload_path_policy_and_node_verdict(self):
        pod = self.pod(policy="Never")
        pod["spec"]["containers"].append({"name": "sidecar", "image": "other:v1"})
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "same"},
            "spec": {"template": {"spec": {
                "initContainers": [{"name": "prepare", "image": "index.docker.io/demo"}],
                "containers": [{"name": "app", "image": IMAGE, "imagePullPolicy": "IfNotPresent"}]}}}}
        cronjob = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "same"},
            "spec": {"jobTemplate": {"spec": {"template": {"spec": {
                "containers": [{"name": "app", "image": "demo:latest", "imagePullPolicy": "Never"}]}}}}}}
        self.write(self.manifest, pod, deployment)
        self.write(self.other, cronjob, self.pod("DOCKER.IO/library/demo:latest", "Always"))
        report = build_report([self.manifest, self.other], self.inventory)
        groups = report["imageOverview"]
        self.assertEqual([g["image"] for g in groups], [IMAGE, "docker.io/library/other:v1"])
        group = groups[0]
        self.assertEqual(group["referenceType"], "tag")
        self.assertNotIn("verdict", group)
        self.assertNotIn("effectivePullPolicy", group)
        self.assertEqual([n["node"] for n in group["nodes"]],
                         ["demo-worker-a", "demo-worker-b", "demo-worker-c"])
        self.assertEqual([n["availability"] for n in group["nodes"]], ["present", "missing", "unassessed"])
        self.assertEqual([n["present"] for n in group["nodes"]], [True, False, None])
        self.assertEqual(group["nodes"][0]["matches"], [{"imageIndex": 0, "field": "repoTags", "reference": IMAGE}])
        self.assertEqual(group["nodes"][2]["matches"], [])
        self.assertIn("No snapshot supplied", group["nodes"][2]["availabilityReason"])
        expected = [
            (0, self.manifest, 1, "v1", "Pod", "team", "spec.containers[0]", "app", "containers",
             "demo:latest", "Never", "explicit"),
            (2, self.manifest, 2, "apps/v1", "Deployment", "default", "spec.template.spec.initContainers[0]",
             "prepare", "initContainers", "index.docker.io/demo", "Always", "default-on-create"),
            (3, self.manifest, 2, "apps/v1", "Deployment", "default", "spec.template.spec.containers[0]",
             "app", "containers", IMAGE, "IfNotPresent", "explicit"),
            (4, self.other, 1, "batch/v1", "CronJob", "default", "spec.jobTemplate.spec.template.spec.containers[0]",
             "app", "containers", "demo:latest", "Never", "explicit"),
            (5, self.other, 2, "v1", "Pod", "team", "spec.containers[0]", "app", "containers",
             "DOCKER.IO/library/demo:latest", "Always", "explicit"),
        ]
        self.assertEqual(len(group["uses"]), len(expected))
        for use, values in zip(group["uses"], expected):
            index, source, document, api, kind, namespace, path, container, field, raw, policy, provenance = values
            with self.subTest(index=index):
                self.assertEqual({k: v for k, v in use.items() if k not in ("policyRisk", "nodeVerdicts")}, {
                    "assessmentIndex": index, "source": str(source), "document": document,
                    "apiVersion": api, "kind": kind, "namespace": namespace, "name": "same", "path": path,
                    "container": container, "containerType": field, "image": raw,
                    "effectivePullPolicy": policy, "policySource": provenance})
                self.assertEqual([n["verdict"] for n in use["nodeVerdicts"]], {
                    "Always": ["will-request-registry", "will-request-registry", "unassessed"],
                    "IfNotPresent": ["local-available", "would-pull-if-scheduled", "unassessed"],
                    "Never": ["local-available", "missing-required-local", "unassessed"],
                }[policy])
                if policy == "Always":
                    self.assertIn("Always", use["policyRisk"])
                else:
                    self.assertIsNone(use["policyRisk"])
                self.assertEqual(use["nodeVerdicts"], [
                    {k: row[k] for k in ("node", "verdict", "reason")}
                    for row in report["assessments"][index]["nodes"]])
        self.assertEqual(report["summary"]["containers"], 6)
        self.assertEqual(report["summary"]["nodeChecks"], 18)
        plan = next(p for p in report["loadPlan"] if p["image"] == IMAGE)
        self.assertEqual(plan["missingNodes"], ["demo-worker-b"])
        self.assertTrue(plan["requiresPolicyReview"])

    def test_cli_partial_example_reviews_availability_once_and_preserves_use_warnings(self):
        manifests = [ROOT / "examples/kind_images/partial-workloads.yaml"]
        result = self.cli(manifests, "--json", "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["imageOverview"]), 1)
        group = report["imageOverview"][0]
        self.assertEqual(group["image"], IMAGE)
        self.assertEqual([u["assessmentIndex"] for u in group["uses"]], [0, 1, 2])
        self.assertEqual([u["path"] for u in group["uses"]], [f"spec.containers[{i}]" for i in range(3)])
        self.assertEqual([u["effectivePullPolicy"] for u in group["uses"]], ["Always", "IfNotPresent", "Never"])
        text = self.cli(manifests)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout.count("Image: " + IMAGE + " (tag)"), 1)
        for state in ("present", "missing", "unassessed"):
            self.assertEqual(text.stdout.count("Availability: " + state + " ("), 1)
        for i, policy in enumerate(("Always", "IfNotPresent", "Never")):
            self.assertIn(f"spec.containers[{i}]", text.stdout)
            self.assertIn(f"{policy} (explicit)", text.stdout)
        self.assertIn("Policy risk: Always", text.stdout)
        for verdict in ("will-request-registry", "would-pull-if-scheduled", "missing-required-local"):
            self.assertIn("demo-worker-b: " + verdict, text.stdout)
        self.assertIn("UNASSESSED DemoRollout/custom", text.stdout)

    def test_exact_reference_boundaries_and_digest_aliases(self):
        images = ["demo:v1", "demo:V1", "demo@" + DIGEST, "demo:v1@" + DIGEST,
                  "other@" + DIGEST, "registry-1.docker.io/library/demo:v1",
                  "docker.io:443/library/demo:v1", "demo@sha256:" + "b" * 64]
        self.write(self.manifest, *[self.pod(image) for image in images])
        report = build_report([self.manifest], ROOT / "examples/kind_images/nodes.json")
        groups = {g["image"]: g for g in report["imageOverview"]}
        self.assertEqual(list(groups), sorted([
            "docker.io/library/demo:v1", "docker.io/library/demo:V1", "docker.io/library/demo@" + DIGEST,
            "docker.io/library/other@" + DIGEST, "registry-1.docker.io/library/demo:v1",
            "docker.io:443/library/demo:v1", "docker.io/library/demo@sha256:" + "b" * 64]))
        digest = groups["docker.io/library/demo@" + DIGEST]
        self.assertEqual(digest["referenceType"], "digest")
        self.assertEqual([u["image"] for u in digest["uses"]], images[2:4])
        self.assertEqual([n["availability"] for n in digest["nodes"]], ["present", "missing"])
        self.assertEqual(digest["nodes"][0]["matches"][0]["field"], "repoDigests")
        # Worker-b has the same CRI ID as the requested digest, but no repoDigest.
        self.assertIn("No matching repository@digest", digest["nodes"][1]["availabilityReason"])
        self.assertFalse(any(n["present"] for n in groups["docker.io/library/other@" + DIGEST]["nodes"]))

    def test_all_missing_captures_remain_unknown_without_loads(self):
        self.inventory = self.root / "nodes.json"
        self.inventory.write_text(json.dumps({"schemaVersion": 1, "cluster": "demo",
            "nodes": {"node-a": {"schedulable": True}}}))
        self.write(self.manifest, self.pod(policy="Always"), self.pod("demo@" + DIGEST, "Never"))
        result = self.cli([self.manifest], "--json", "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        for group in report["imageOverview"]:
            self.assertIsNone(group["nodes"][0]["present"])
            self.assertEqual(group["nodes"][0]["availability"], "unassessed")
            self.assertEqual(group["uses"][0]["nodeVerdicts"][0]["verdict"], "unassessed")
        self.assertIn("Always", report["imageOverview"][0]["uses"][0]["policyRisk"])
        self.assertEqual(report["loadPlan"], [])
        self.assertEqual(report["loadPlanOmissions"], [])

    def test_no_assessed_uses_means_empty_overview_not_snapshot_inventory(self):
        self.write(self.manifest, {"apiVersion": "custom.invalid/v1", "kind": "Widget",
                                   "spec": {"image": "demo:latest"}})
        result = self.cli([self.manifest], "--json", "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["imageOverview"], [])
        self.assertEqual(len(report["unassessed"]), 1)
        self.assertFalse(report["summary"]["localUsePossibleEverywhere"])
        text = self.cli([self.manifest])
        self.assertIn("Image overview:", text.stdout)
        self.assertIn("(no assessed images)", text.stdout)

    def test_late_invalid_use_fails_without_partial_overview(self):
        self.write(self.manifest, self.pod(), self.pod(policy="Sometimes"))
        result = self.cli([self.manifest], "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("document 2.spec.containers[0].imagePullPolicy", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_overview_is_deterministic_offline_and_keeps_duplicate_occurrences(self):
        self.write(self.manifest, self.pod(), self.pod())
        paths = [self.manifest, self.inventory, ROOT / "examples/kind_images/snapshots/worker-a.json",
                 ROOT / "examples/kind_images/snapshots/empty.json"]
        before = {p: p.read_bytes() for p in paths}
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.Popen", side_effect=AssertionError("process forbidden")), \
                patch("os.system", side_effect=AssertionError("shell forbidden")):
            report = build_report([self.manifest, self.manifest], self.inventory)
        uses = report["imageOverview"][0]["uses"]
        self.assertEqual([u["assessmentIndex"] for u in uses], [0, 1, 2, 3])
        self.assertEqual([u["document"] for u in uses], [1, 2, 1, 2])
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        first = self.cli([self.manifest, self.manifest], "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, self.cli([self.manifest, self.manifest], "--json").stdout)


if __name__ == "__main__":
    unittest.main()
