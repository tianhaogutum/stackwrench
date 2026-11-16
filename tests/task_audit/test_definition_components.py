"""Per-field definition attribution through the installed, non-executing CLI."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from .support import CLI, cli_env


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class DefinitionComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.taskfile = self.root / "Taskfile.yml"
        (self.root / "base.txt").write_text("private source contents")
        self.serial = 0

    def run_cli(self, *args, code=0):
        result = subprocess.run([*CLI, *map(str, args)], cwd=self.root, env=cli_env(), text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def snapshot(self, task, **root_fields):
        self.taskfile.write_text(yaml.safe_dump({"version": "3", **root_fields,
                                               "tasks": {"build": task}}))
        self.serial += 1
        path = self.root / f"snapshot-{self.serial}.json"
        self.run_cli("snapshot", self.taskfile, "--output", path)
        return path

    def record(self, path):
        return json.loads(path.read_text())["tasks"]["build"]

    def comparison(self, before, after):
        report = json.loads(self.run_cli("compare", before, after, "--json").stdout)
        self.assertFalse(report["cache_reuse_authorized"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        return report["tasks"]["build"]

    def legacy(self, path):
        report = json.loads(path.read_text())
        record = report["tasks"]["build"]
        record.pop("definition_subfields", None)
        record.pop("definition_subfields_digest", None)
        record.pop("definition_entries", None)
        record.pop("definition_entries_digest", None)
        del record["definition_components"]
        del record["definition_components_digest"]
        legacy = self.root / f"legacy-{path.name}"
        legacy.write_text(json.dumps(report))
        return legacy

    def test_snapshots_hash_fields_without_exposing_or_executing_text(self):
        marker = self.root / "NEVER"
        task = {"cmds": [f"touch {marker}; echo command-private"],
                "sources": ["base.txt"], "generates": ["uncreated.txt"],
                "status": [f"touch {marker}; echo status-private"],
                "vars": {"UNUSED": "vars-private"}, "env": {"UNUSED": "env-private"},
                "desc": "desc-private", "summary": "summary-private",
                "custom": {"nested": "unknown-private"}}
        before = self.snapshot(task)
        after = self.snapshot(dict(reversed(list(task.items()))))
        record = self.record(before)
        definition = {k: v for k, v in task.items() if k not in {"vars", "env", "desc", "summary"}}
        components = {k: sha(v) for k, v in definition.items()}
        self.assertEqual(record["definition_components"], components)
        self.assertEqual(record["definition_digest"], sha(definition))
        self.assertEqual(record["definition_components_digest"], sha({
            "definition_digest": sha(definition), "components": components}))
        self.assertEqual(before.read_bytes(), after.read_bytes())
        self.assertEqual(record["assessment"], "partial")
        self.assertEqual(record["native_status"], "unknown")
        delta = self.comparison(before, after)
        self.assertTrue(delta["definition_changes_complete"])
        self.assertEqual(delta["definition_changes"], [])
        for output in (before.read_text(), self.run_cli("check", self.taskfile, "--json").stdout,
                       self.run_cli("compare", before, after, "--json").stdout,
                       self.run_cli("compare", before, after).stdout):
            for secret in (str(marker), "command-private", "status-private", "private source contents",
                           "vars-private", "env-private", "desc-private", "summary-private", "unknown-private"):
                self.assertNotIn(secret, output)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "uncreated.txt").exists())

    def test_each_definition_field_is_attributed_without_source_content_change(self):
        baseline = {"cmds": ["echo command-one"], "sources": ["base.txt"],
                    "generates": ["one.out"], "status": ["echo status-one"]}
        before = self.snapshot(baseline)
        for field, value in {"cmds": ["echo command-two"], "sources": ["*.txt"],
                             "generates": ["two.out"], "status": ["echo status-two"]}.items():
            with self.subTest(field=field):
                after = self.snapshot({**baseline, field: value})
                delta = self.comparison(before, after)
                self.assertTrue(delta["definition_changes_complete"])
                self.assertEqual(delta["definition_changes"], [{"field": field, "change": "changed",
                    "before_digest": sha(baseline[field]), "after_digest": sha(value)}])
                self.assertEqual(delta["reasons"], ["task-definition"])
                self.assertTrue(delta["definition_changed"])
                self.assertTrue(delta["advisory_changed"])
                self.assertFalse(delta["source_only_changed"])
                self.assertEqual(delta["source_changes"], [])
                self.assertEqual(delta["input_changes"], [])
                text = self.run_cli("compare", before, after).stdout
                self.assertIn(f"definition: {field} (changed)", text)
                self.assertNotIn("command-one", text)
                self.assertNotIn("status-two", text)

    def test_absent_empty_order_and_unsupported_field_changes(self):
        before = self.snapshot({"status": [], "cmds": ["echo first", "echo second"],
                                "custom": {"token": "old-private"}})
        after = self.snapshot({"cmds": ["echo second", "echo first"], "sources": [],
                               "custom": {"token": "new-private"}})
        delta = self.comparison(before, after)
        self.assertEqual([(c["field"], c["change"]) for c in delta["definition_changes"]],
                         [("cmds", "changed"), ("custom", "changed"), ("sources", "added"), ("status", "removed")])
        added, removed = delta["definition_changes"][2:]
        self.assertIsNone(added["before_digest"])
        self.assertEqual(added["after_digest"], sha([]))
        self.assertEqual(removed["before_digest"], sha([]))
        self.assertIsNone(removed["after_digest"])
        self.assertEqual(delta["assessment_after"], "partial")
        reverse = self.comparison(after, before)["definition_changes"]
        self.assertEqual([c["change"] for c in reverse], ["changed", "changed", "removed", "added"])

    def test_metadata_and_values_are_excluded_and_shorthands_normalize(self):
        before = self.snapshot({"cmds": ["echo {{.V}}"], "vars": {"V": "one"},
                                "env": {"UNUSED": "one"}, "desc": "old", "summary": "old"})
        after = self.snapshot({"cmds": ["echo {{.V}}"], "vars": {"V": "two"},
                               "env": {"UNUSED": "two"}, "desc": "new", "summary": "new"})
        delta = self.comparison(before, after)
        self.assertFalse(delta["definition_changed"])
        self.assertEqual(delta["definition_changes"], [])
        self.assertEqual(delta["input_changes"][0]["changed_fields"], ["value_digest"])
        self.assertEqual(self.record(before)["definition_components"], {"cmds": sha(["echo {{.V}}"])})
        string = self.snapshot("echo literal")
        array = self.snapshot(["echo literal"])
        mapping = self.snapshot({"cmds": ["echo literal"]})
        self.assertEqual(string.read_bytes(), array.read_bytes())
        self.assertEqual(array.read_bytes(), mapping.read_bytes())
        empty = self.snapshot({})
        self.assertEqual(self.record(empty)["definition_components"], {})
        self.assertEqual(self.comparison(empty, empty)["definition_changes"], [])

    def test_legacy_and_mixed_snapshots_use_explicit_unavailable_attribution(self):
        before = self.snapshot({"cmds": ["echo one"]})
        after = self.snapshot({"cmds": ["echo two"]})
        legacy_before, legacy_after = self.legacy(before), self.legacy(after)
        for first, second, changed in ((legacy_before, legacy_after, True),
                                       (legacy_before, after, True), (before, legacy_after, True),
                                       (before, legacy_before, False), (legacy_before, before, False)):
            with self.subTest(first=first.name, second=second.name):
                delta = self.comparison(first, second)
                self.assertFalse(delta["definition_changes_complete"])
                self.assertIsNone(delta["definition_changes"])
                self.assertEqual(delta["definition_changed"], changed)
                self.assertEqual(delta["advisory_changed"], changed)
                self.assertEqual(delta["change"], "changed" if changed else "unchanged")
                self.assertIn("definition fields: unavailable (legacy snapshot)",
                              self.run_cli("compare", first, second).stdout)

    def test_added_removed_tasks_have_component_membership(self):
        before = self.snapshot({"cmds": ["echo one"], "status": []})
        after = self.root / "other.json"
        self.taskfile.write_text("version: '3'\ntasks: {other: {}}\n")
        self.run_cli("snapshot", self.taskfile, "--output", after)
        for first, second, change in ((before, after, "removed"), (after, before, "added")):
            delta = self.comparison(first, second)
            self.assertTrue(delta["definition_changes_complete"])
            self.assertEqual([(c["field"], c["change"]) for c in delta["definition_changes"]],
                             [("cmds", change), ("status", change)])
            self.assertTrue(all(c[f"{'after' if change == 'removed' else 'before'}_digest"] is None
                                for c in delta["definition_changes"]))
        legacy = self.legacy(before)
        self.assertIsNone(self.comparison(legacy, after)["definition_changes"])

    def test_malformed_components_fail_closed_without_excerpts(self):
        valid_path = self.snapshot({"cmds": ["echo private"], "sources": ["base.txt"]})
        valid = json.loads(valid_path.read_text())
        self.assertIn("definition_components", valid["tasks"]["build"])
        malformed = []
        for field in ("definition_components", "definition_components_digest"):
            case = copy.deepcopy(valid)
            del case["tasks"]["build"][field]
            malformed.append(case)
        for components in (None, [], "secret-invalid", {"cmds": "secret-invalid"},
                           {"cmds": "F" * 64}, {"cmds": 1}, {"vars": "0" * 64},
                           {"cmds": "0" * 64}, {}):
            case = copy.deepcopy(valid)
            case["tasks"]["build"]["definition_components"] = components
            malformed.append(case)
        for checksum in (None, [], "secret-invalid", "0" * 64):
            case = copy.deepcopy(valid)
            case["tasks"]["build"]["definition_components_digest"] = checksum
            malformed.append(case)
        for index, case in enumerate(malformed):
            with self.subTest(index=index):
                path = self.root / "bad.json"
                path.write_text(json.dumps(case))
                result = self.run_cli("compare", valid_path, path, "--json", code=2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("secret-invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()
