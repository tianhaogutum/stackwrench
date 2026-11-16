import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from stackwrench.kind_images.inputs import MAX_BYTES
from stackwrench.kind_images.inventory import read_snapshot
from stackwrench.kind_images.planner import build_report


ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "stackwrench", "kind-images"]
DIGEST = "sha256:" + "a" * 64


def pod(image="demo:v1", policy=None):
    container = {"name": "app", "image": image}
    if policy is not None:
        container["imagePullPolicy"] = policy
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "test"},
            "spec": {"containers": [container]}}


def cri(tags=None, digests=None, image_id=None):
    return {"id": image_id or "sha256:" + "1" * 64, "repoTags": tags or [],
            "repoDigests": digests or [], "size": "123456", "uid": None,
            "username": "", "spec": None, "pinned": False}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "workloads.yaml"
        self.inventory = self.root / "nodes.json"
        self.snapshot_a = self.root / "a.json"
        self.snapshot_b = self.root / "b.json"
        self.inventory_data = {"schemaVersion": 1, "cluster": "demo", "nodes": {
            "demo-worker-a": {"schedulable": True, "snapshot": "a.json"},
            "demo-worker-b": {"schedulable": True, "snapshot": "b.json"},
            "demo-control-plane": {"schedulable": False},
        }}
        self.write_json(self.inventory, self.inventory_data)
        self.write_json(self.snapshot_a, {"images": []})
        self.write_json(self.snapshot_b, {"images": []})
        self.write_manifest(pod())

    def write_json(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def write_manifest(self, *docs):
        self.manifest.write_text(yaml.safe_dump_all(docs), encoding="utf-8")

    def report(self):
        return build_report([self.manifest], self.inventory)

    def cli(self, *args, cwd=None, env=None):
        return subprocess.run([*CLI, *map(str, args)], cwd=cwd or self.root,
                              env=env, capture_output=True, text=True, timeout=10)

    def run_cli(self, *extra):
        return self.cli("--manifests", self.manifest, "--inventory", self.inventory, *extra)

    def assert_bad_cli(self, text=None):
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        if text:
            self.assertIn(text, result.stderr)

    def test_acceptance_e2e_real_shapes_and_expected_matrix(self):
        result = self.cli("--manifests", "examples/kind_images/workloads.yaml", "--inventory",
                          "examples/kind_images/nodes.json", "--json", cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"], {
            "containers": 8, "nodeChecks": 16,
            "verdicts": {"local-available": 9, "missing-required-local": 2,
                         "will-request-registry": 2, "would-pull-if-scheduled": 3},
            "unassessed": 1, "localUsePossibleEverywhere": False})
        items = report["assessments"]
        self.assertEqual([(item["kind"], item["container"], item["path"]) for item in items], [
            ("Deployment", "prepare", "spec.template.spec.initContainers[0]"),
            ("Deployment", "app", "spec.template.spec.containers[0]"),
            ("StatefulSet", "db", "spec.template.spec.containers[0]"),
            ("DaemonSet", "agent", "spec.template.spec.containers[0]"),
            ("Job", "pinned", "spec.template.spec.containers[0]"),
            ("CronJob", "prepare", "spec.jobTemplate.spec.template.spec.initContainers[0]"),
            ("CronJob", "backup", "spec.jobTemplate.spec.template.spec.containers[0]"),
            ("Pod", "app", "spec.containers[0]"),
        ])
        expected = [
            ["local-available", "missing-required-local"],
            ["will-request-registry", "will-request-registry"],
            ["local-available", "would-pull-if-scheduled"],
            ["local-available", "local-available"],
            ["local-available", "would-pull-if-scheduled"],
            ["local-available", "would-pull-if-scheduled"],
            ["local-available", "local-available"],
            ["local-available", "missing-required-local"],
        ]
        for item, expected_verdicts in zip(items, expected):
            self.assertEqual([node["node"] for node in item["nodes"]], ["demo-worker-a", "demo-worker-b"])
            self.assertEqual([node["verdict"] for node in item["nodes"]], expected_verdicts)
        self.assertEqual(items[1]["effectivePullPolicy"], "Always")
        self.assertTrue(items[1]["nodes"][0]["present"])
        self.assertEqual(items[4]["nodes"][0]["matches"], [{"imageIndex": 0, "field": "repoDigests",
                         "reference": "docker.io/library/demo@" + DIGEST}])
        self.assertEqual(report["excludedNodes"], ["demo-control-plane"])
        self.assertEqual(report["unassessed"][0]["kind"], "DemoRollout")
        self.assertEqual(len(report["loadPlan"]), 3)
        self.assertEqual(report["loadPlan"][0]["argv"], ["kind", "load", "docker-image",
            "docker.io/library/db:1", "--name", "demo", "--nodes", "demo-worker-b"])
        self.assertTrue(report["loadPlan"][1]["requiresPolicyReview"])
        self.assertEqual(report["loadPlanOmissions"][0]["missingNodes"], ["demo-worker-b"])

    def test_ready_check_succeeds_and_has_no_load_plan(self):
        result = self.cli("--manifests", ROOT / "examples/kind_images/ready.yaml", "--inventory",
                          ROOT / "examples/kind_images/nodes.json", "--json", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["summary"]["localUsePossibleEverywhere"])
        self.assertEqual(report["loadPlan"], [])

    def test_check_returns_one_with_complete_report_for_blockers(self):
        result = self.run_cli("--json", "--check")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["summary"]["nodeChecks"], 2)

    def test_text_output_contains_node_matrix_and_json_argv(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("demo-worker-b: would-pull-if-scheduled", result.stdout)
        argv_lines = [line for line in result.stdout.splitlines() if line.startswith("[")]
        self.assertEqual(len(argv_lines), 1)
        self.assertEqual(json.loads(argv_lines[0])[-1], "demo-worker-a,demo-worker-b")

    def test_missing_inventory_is_usage_error(self):
        result = self.cli("--manifests", self.manifest)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--inventory", result.stderr)

    def test_multiple_manifest_paths_and_empty_documents(self):
        other = self.root / "other.yaml"
        other.write_text("---\n# blank\n---\n# another blank\n---\n" + yaml.safe_dump(pod("other:v2")))
        result = self.cli("--manifests", self.manifest, other, "--inventory", self.inventory, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["containers"], 2)
        self.assertEqual(report["assessments"][1]["document"], 3)

    def test_policy_matrix_and_explicit_overrides(self):
        cases = [("demo", None, "Always", "will-request-registry"),
                 ("demo:latest", None, "Always", "will-request-registry"),
                 ("demo:v1", None, "IfNotPresent", "local-available"),
                 ("demo:latest", "IfNotPresent", "IfNotPresent", "local-available"),
                 ("demo:v1", "Always", "Always", "will-request-registry"),
                 ("demo:v1", "Never", "Never", "local-available"),
                 ("demo@" + DIGEST, None, "IfNotPresent", "local-available"),
                 ("demo:latest@" + DIGEST, None, "IfNotPresent", "local-available"),
                 ("demo@" + DIGEST, "Always", "Always", "will-request-registry")]
        snapshot = {"images": [cri(["docker.io/library/demo:v1", "demo:latest"], ["demo@" + DIGEST])]}
        self.write_json(self.snapshot_a, snapshot)
        self.write_json(self.snapshot_b, snapshot)
        for image, explicit, policy, verdict in cases:
            with self.subTest(image=image, explicit=explicit):
                self.write_manifest(pod(image, explicit))
                result = self.report()
                item = result["assessments"][0]
                self.assertEqual(item["effectivePullPolicy"], policy)
                self.assertEqual(item["policySource"], "explicit" if explicit else "default-on-create")
                self.assertTrue(all(node["present"] for node in item["nodes"]))
                self.assertEqual([node["verdict"] for node in item["nodes"]], [verdict, verdict])
                self.assertEqual(result["loadPlan"], [])

    def test_absent_repo_digest_cannot_be_established_from_matching_tag_or_id(self):
        self.write_manifest(pod("demo:v1@" + DIGEST, "Never"))
        self.write_json(self.snapshot_a, {"images": [cri(["demo:v1"], image_id=DIGEST)]})
        item = self.report()["assessments"][0]
        self.assertFalse(item["nodes"][0]["present"])
        self.assertEqual(item["nodes"][0]["verdict"], "missing-required-local")
        self.assertIn("No matching repository@digest", item["nodes"][0]["reason"])
        self.assertEqual(self.report()["loadPlan"], [])

    def test_digest_and_repository_must_both_match_exactly(self):
        self.write_manifest(pod("demo@" + DIGEST))
        self.write_json(self.snapshot_a, {"images": [cri(digests=["other@" + DIGEST])]})
        self.write_json(self.snapshot_b, {"images": [cri(digests=["demo@sha256:" + "b" * 64])]})
        self.assertFalse(any(node["present"] for node in self.report()["assessments"][0]["nodes"]))

    def test_tag_cannot_be_established_from_digest_only(self):
        self.write_json(self.snapshot_a, {"images": [cri(digests=["demo@" + DIGEST])]})
        self.assertFalse(self.report()["assessments"][0]["nodes"][0]["present"])

    def test_plan_deduplicates_aliases_and_unions_missing_nodes(self):
        self.write_manifest(pod("demo:v1", "Never"), pod("docker.io/library/demo:v1"),
                            pod("index.docker.io/demo:v1", "Always"))
        result = self.report()
        self.assertEqual(len(result["loadPlan"]), 1)
        self.assertEqual(result["loadPlan"][0]["missingNodes"], ["demo-worker-a", "demo-worker-b"])
        self.assertTrue(result["loadPlan"][0]["requiresPolicyReview"])

    def test_partial_inventory_e2e_keeps_unknown_separate_from_missing_and_policy(self):
        args = ("--manifests", ROOT / "examples/kind_images/partial-workloads.yaml", "--inventory",
                ROOT / "examples/kind_images/partial-nodes.json", "--json", "--check")
        result = self.cli(*args)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schemaVersion"], 2)
        self.assertEqual(report["summary"], {
            "containers": 3, "nodeChecks": 9,
            "verdicts": {"local-available": 2, "missing-required-local": 1,
                         "will-request-registry": 2, "would-pull-if-scheduled": 1,
                         "unassessed": 3},
            "unassessed": 1, "localUsePossibleEverywhere": False})
        self.assertEqual(report["nodes"][-1], {"name": "demo-worker-c", "snapshot": None})
        self.assertEqual([item["node"] for item in report["unassessedNodes"]], ["demo-worker-c"])
        self.assertIn("No snapshot supplied", report["unassessedNodes"][0]["reason"])
        self.assertEqual(report["unassessed"][0]["kind"], "DemoRollout")
        for index, item in enumerate(report["assessments"]):
            with self.subTest(policy=item["effectivePullPolicy"]):
                rows = item["nodes"]
                self.assertEqual([row["availability"] for row in rows], ["present", "missing", "unassessed"])
                self.assertEqual([row["present"] for row in rows], [True, False, None])
                self.assertIn("No snapshot supplied", rows[2]["availabilityReason"])
                self.assertEqual(rows[2]["matches"], [])
                self.assertEqual(rows[2]["verdict"], "unassessed")
                self.assertIn("does not establish", rows[1]["availabilityReason"])
                if index == 0:
                    for row in rows:
                        self.assertIn("Always", row["policyRisk"])
                        self.assertIn("local copy alone", row["policyRisk"])
                else:
                    self.assertTrue(all(row["policyRisk"] is None for row in rows))
        self.assertEqual(len(report["loadPlan"]), 1)
        self.assertEqual(report["loadPlan"][0]["argv"], ["kind", "load", "docker-image",
            "docker.io/library/demo:latest", "--name", "demo", "--nodes", "demo-worker-b"])
        self.assertEqual(report["loadPlan"][0]["missingNodes"], ["demo-worker-b"])
        self.assertTrue(report["loadPlan"][0]["requiresPolicyReview"])
        self.assertEqual(report["loadPlanOmissions"], [])
        self.assertEqual(result.stdout, self.cli(*args).stdout)
        text = self.cli(*args[:-2])
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("UNASSESSED NODE demo-worker-c", text.stdout)
        self.assertIn("Availability: unassessed (present=unknown)", text.stdout)
        self.assertIn("Availability: missing (present=false)", text.stdout)
        self.assertIn("Policy risk: Always", text.stdout)
        paths = [ROOT / "examples/kind_images/partial-workloads.yaml", ROOT / "examples/kind_images/partial-nodes.json",
                 ROOT / "examples/kind_images/snapshots/worker-a.json", ROOT / "examples/kind_images/snapshots/empty.json"]
        before = {path: path.read_bytes() for path in paths}
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.Popen", side_effect=AssertionError("process forbidden")), \
                patch("os.system", side_effect=AssertionError("shell forbidden")):
            self.assertEqual(build_report([paths[0]], paths[1]), report)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_all_snapshots_omitted_does_not_invent_tag_or_digest_loads(self):
        for entry in self.inventory_data["nodes"].values():
            entry.pop("snapshot", None)
        self.write_json(self.inventory, self.inventory_data)
        self.write_manifest(pod("demo:v1", "Always"), pod("demo:latest", "IfNotPresent"),
                            pod("demo@" + DIGEST, "Never"))
        result = self.run_cli("--json", "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["verdicts"], {"unassessed": 6})
        self.assertEqual(len(report["unassessedNodes"]), 2)
        self.assertEqual(report["excludedNodes"], ["demo-control-plane"])
        self.assertEqual(report["loadPlan"], [])
        self.assertEqual(report["loadPlanOmissions"], [])
        for item in report["assessments"]:
            for row in item["nodes"]:
                self.assertIsNone(row["present"])
                self.assertEqual(row["availability"], "unassessed")
        self.assertEqual(self.run_cli("--json").returncode, 0)
        # Missing captures remain visible even without any supported workload.
        self.write_manifest({"apiVersion": "custom.invalid/v1", "kind": "Widget"})
        report = self.report()
        self.assertEqual(len(report["unassessedNodes"]), 2)
        self.assertEqual(report["assessments"], [])
        self.assertFalse(report["summary"]["localUsePossibleEverywhere"])

    def test_explicit_bad_snapshot_is_not_treated_as_an_omitted_capture(self):
        self.inventory_data["nodes"]["demo-worker-a"].pop("snapshot")
        for value in (None, "", 12, "not-found.json"):
            with self.subTest(snapshot=value):
                self.inventory_data["nodes"]["demo-worker-b"]["snapshot"] = value
                self.write_json(self.inventory, self.inventory_data)
                self.assert_bad_cli()
        self.inventory_data["nodes"]["demo-worker-b"]["snapshot"] = "b.json"
        self.write_json(self.inventory, self.inventory_data)
        self.write_json(self.snapshot_b, {"images": None})
        self.assert_bad_cli("images")

    def test_digest_omissions_include_only_captured_missing_nodes(self):
        self.inventory_data["nodes"]["demo-worker-b"].pop("snapshot")
        self.write_json(self.inventory, self.inventory_data)
        self.write_manifest(pod("demo@" + DIGEST, "Never"))
        report = self.report()
        self.assertEqual(report["loadPlan"], [])
        self.assertEqual(report["loadPlanOmissions"][0]["missingNodes"], ["demo-worker-a"])
        self.assertEqual([row["present"] for row in report["assessments"][0]["nodes"]], [False, None])
        self.assertIn("No matching repository@digest", report["assessments"][0]["nodes"][0]["availabilityReason"])

    def test_unknown_kinds_versions_and_list_are_explicitly_unassessed(self):
        docs = [{"apiVersion": "v1", "kind": "List", "items": [pod()]},
                {"apiVersion": "extensions/v1beta1", "kind": "Deployment", "metadata": {"name": "old"}},
                {"apiVersion": "custom.invalid/v1", "kind": "Widget", "metadata": {"name": "custom"}}]
        self.write_manifest(*docs)
        result = self.run_cli("--json", "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["unassessed"]), 3)
        self.assertEqual(report["assessments"], [])
        self.assertFalse(report["summary"]["localUsePossibleEverywhere"])

    def test_ephemeral_image_volume_and_runtime_class_are_not_silently_dropped(self):
        data = pod()
        data["spec"].update({"ephemeralContainers": [{"name": "debug", "image": "debug:v1"}],
                             "volumes": [{"name": "data", "image": {"reference": "data:v1"}}],
                             "runtimeClassName": "sandbox"})
        self.write_manifest(data)
        report = self.report()
        self.assertEqual({item["path"] for item in report["unassessed"]},
                         {"spec.ephemeralContainers", "spec.volumes[0].image", "spec.runtimeClassName"})
        self.assertEqual(report["summary"]["containers"], 1)

    def test_snapshot_null_and_omitted_reference_arrays_are_empty(self):
        self.write_json(self.snapshot_a, {"images": [{"id": "opaque-id", "repoTags": None, "repoDigests": None}]})
        self.write_json(self.snapshot_b, {"images": [{"id": "other-opaque-id"}]})
        self.assertEqual(self.report()["summary"]["verdicts"], {"would-pull-if-scheduled": 2})

    def test_malformed_snapshots_fail_e2e(self):
        invalid = [{}, [], {"images": None}, {"images": {}}, {"images": [None]},
                   {"images": [{}]}, {"images": [{"id": 123}]},
                   {"images": [], "unexpected": 1}, {"images": [{"id": "opaque", "repo_tags": ["demo:v1"]}]},
                   {"images": [cri(tags="demo:v1")]}, {"images": [cri(tags=[12])]},
                   {"images": [cri(tags=["demo@" + DIGEST])]},
                   {"images": [cri(tags=["demo"])]},
                   {"images": [cri(digests=["demo:v1"])]},
                   {"images": [cri(digests=["demo:v1@" + DIGEST])]},
                   {"images": [cri(digests=["demo@sha256:short"])]}]
        for data in invalid:
            with self.subTest(data=data):
                self.write_json(self.snapshot_a, data)
                self.assert_bad_cli()

    def test_malformed_inventory_fail_e2e(self):
        invalid = [{}, [], {**self.inventory_data, "schemaVersion": True},
                   {**self.inventory_data, "schemaVersion": 2},
                   {**self.inventory_data, "cluster": "demo;touch"},
                   {**self.inventory_data, "extra": 1},
                   {**self.inventory_data, "nodes": []},
                   {**self.inventory_data, "nodes": {}},
                   {**self.inventory_data, "nodes": {"worker": {"snapshot": "a.json"}}},
                   {**self.inventory_data, "nodes": {"worker": {"schedulable": "true", "snapshot": "a.json"}}},
                   {**self.inventory_data, "nodes": {"worker": {"schedulable": True, "snapshot": None}}},
                   {**self.inventory_data, "nodes": {"worker": {"schedulable": False}}},
                   {**self.inventory_data, "nodes": {"worker": {"schedulable": True, "snapshot": "a.json", "typo": 1}}},
                   {**self.inventory_data, "nodes": {"bad,name": {"schedulable": True, "snapshot": "a.json"}}}]
        for data in invalid:
            with self.subTest(data=data):
                self.write_json(self.inventory, data)
                self.assert_bad_cli()

    def test_snapshot_spec_metadata_does_not_establish_presence(self):
        self.write_json(self.snapshot_a, {"images": [{"id": "opaque",
                                                     "spec": {"image": "demo:v1"}}]})
        self.assertEqual(read_snapshot(self.snapshot_a), {})

    def test_duplicate_json_keys_fail(self):
        self.snapshot_a.write_text('{"images": [], "images": []}')
        self.assert_bad_cli("duplicate JSON key")

    def test_non_json_numbers_and_truncated_json_fail(self):
        for text in ('{"images": [], "bad": NaN}', '{"images": ['):
            with self.subTest(text=text):
                self.snapshot_a.write_text(text)
                self.assert_bad_cli()

    def test_invalid_policies_and_image_references_fail_e2e(self):
        for value in ("Sometimes", "always", "", None, [], 1):
            with self.subTest(value=value):
                data = pod()
                data["spec"]["containers"][0]["imagePullPolicy"] = value
                self.write_manifest(data)
                self.assert_bad_cli("imagePullPolicy")
        for value in (None, "Demo:v1", "demo@sha256:short", "$(touch injected)"):
            with self.subTest(value=value):
                self.write_manifest(pod(value))
                self.assert_bad_cli()

    def test_documented_failure_example_is_nonzero(self):
        result = self.cli("--manifests", ROOT / "examples/kind_images/invalid-policy.yaml", "--inventory",
                          ROOT / "examples/kind_images/nodes.json", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("imagePullPolicy", result.stderr)

    def test_malformed_supported_workload_paths_and_containers_fail(self):
        invalid = [[], {"kind": "Pod"}, pod()]
        invalid[2]["spec"] = None
        for replacement in (None, {}, [], [None], [{"name": "missing-image"}]):
            data = pod()
            data["spec"]["containers"] = replacement
            invalid.append(data)
        duplicate = pod()
        duplicate["spec"]["initContainers"] = [{"name": "app", "image": "init:v1"}]
        invalid.append(duplicate)
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "broken"}, "spec": {}}
        invalid.append(deployment)
        for data in invalid:
            with self.subTest(data=data):
                self.write_manifest(data)
                self.assert_bad_cli()

    def test_duplicate_yaml_keys_aliases_and_unsafe_tags_are_rejected(self):
        for content, reason in [
            ("apiVersion: v1\nkind: Pod\nkind: Job\n", "duplicate key"),
            ("a: &x [1, 2]\nb: *x\n", "anchors/aliases"),
            ("a: !!python/object/apply:os.system ['touch injected']\n", "constructor"),
            ("[bad: yaml", ""),
            ("1: value\n", "mapping keys must be strings"),
            ("<<: {kind: Pod}\n", "constructor"),
        ]:
            with self.subTest(content=content):
                self.manifest.write_text(content)
                self.assert_bad_cli(reason)
        self.assertFalse((self.root / "injected").exists())

    def test_empty_manifest_is_an_error(self):
        self.manifest.write_text("---\n# no objects\n---\n")
        self.assert_bad_cli("no Kubernetes objects")

    def test_late_invalid_document_does_not_emit_partial_report(self):
        self.manifest.write_text(yaml.safe_dump(pod()) + "\n---\nkind: [unterminated\n")
        self.assert_bad_cli()

    def test_missing_files_and_non_utf8_fail_without_traceback(self):
        self.snapshot_a.unlink()
        self.assert_bad_cli()
        self.snapshot_a.write_bytes(b"\xff\xfe")
        self.assert_bad_cli()

    def test_input_size_is_bounded(self):
        self.snapshot_a.write_bytes(b" " * (MAX_BYTES + 1))
        self.assert_bad_cli("input limit")

    def test_generate_name_is_reported(self):
        data = pod()
        data["metadata"] = {"generateName": "demo-"}
        self.write_manifest(data)
        self.assertEqual(self.report()["assessments"][0]["name"], "demo-*")

    def test_inventory_relative_snapshot_paths_are_independent_of_cwd(self):
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["nodes"]), 2)

    def test_no_process_network_or_script_execution(self):
        data = pod()
        marker = self.root / "EXECUTED"
        data["spec"]["containers"][0]["command"] = ["sh", "-c", f"touch {marker}"]
        self.write_manifest(data)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("subprocess.Popen", side_effect=AssertionError("process forbidden")), \
                patch("os.system", side_effect=AssertionError("shell forbidden")):
            self.assertEqual(self.report()["summary"]["nodeChecks"], 2)
        # Installed CLI works with no external commands in PATH or kubeconfig.
        env = {**os.environ, "PATH": "", "KUBECONFIG": str(self.root / "does-not-exist")}
        result = self.cli("--manifests", self.manifest, "--inventory", self.inventory, "--json", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_analysis_does_not_modify_inputs(self):
        paths = [self.manifest, self.inventory, self.snapshot_a, self.snapshot_b]
        before = {path: path.read_bytes() for path in paths}
        self.assertEqual(self.run_cli("--json").returncode, 0)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_report_is_deterministic(self):
        self.assertEqual(self.run_cli("--json").stdout, self.run_cli("--json").stdout)


if __name__ == "__main__":
    unittest.main()
