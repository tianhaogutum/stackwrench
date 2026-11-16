"""Bounded digest-only structured command attribution through the CLI."""
import copy
import json
import unittest

from . import test_definition_components as components

sha = components.sha


class DefinitionSubfieldTests(unittest.TestCase):
    setUp = components.DefinitionComponentTests.setUp
    run_cli = components.DefinitionComponentTests.run_cli
    record = components.DefinitionComponentTests.record
    comparison = components.DefinitionComponentTests.comparison

    def snapshot(self, task):
        # Repeated equal mappings must serialize literally, without YAML aliases.
        return components.DefinitionComponentTests.snapshot(self, json.loads(json.dumps(task)))

    def change(self, index, key, old, new):
        return {"location": ["cmds", index, key],
                "change": "added" if old is None else "removed" if new is None else "changed",
                "before_digest": sha(old) if old is not None else None,
                "after_digest": sha(new) if new is not None else None}

    def test_only_silent_child_changes_preserving_entry_field_and_uncertainty(self):
        before = self.snapshot({"cmds": [{"cmd": "echo private", "silent": False}]})
        after = self.snapshot({"cmds": [{"cmd": "echo private", "silent": True}]})
        self.taskfile.unlink()
        delta = self.comparison(before, after)
        self.assertEqual(delta["definition_subfield_changes"], [self.change(0, "silent", False, True)])
        self.assertTrue(delta["definition_subfield_changes_complete"])
        self.assertEqual([c["location"] for c in delta["definition_entry_changes"]], [["cmds", 0]])
        self.assertEqual([c["field"] for c in delta["definition_changes"]], ["cmds"])
        self.assertEqual(delta["reasons"], ["task-definition"])
        self.assertFalse(delta["source_only_changed"])
        self.assertTrue(delta["advisory_changed"])
        for side in ("before", "after"):
            self.assertEqual(delta[f"assessment_{side}"], "partial")
            self.assertIn("unsupported-command", [f["code"] for f in delta[f"context_{side}"]["unassessed_findings"]])
        text = self.run_cli("compare", before, after).stdout
        self.assertIn("definition subfield: [cmds, 0, silent] (changed)", text)
        self.assertIn(sha(False), text)
        self.assertIn(sha(True), text)
        self.assertIn("unsupported-command", text)
        self.assertNotIn("private", text)

    def test_key_membership_command_edit_and_numeric_order(self):
        old = [{"cmd": "echo private", "silent": False} for _ in range(12)]
        new = [{"cmd": "echo edited-private", "ignore_error": True, "defer": False} for _ in old]
        before, after = self.snapshot({"cmds": old}), self.snapshot({"cmds": new})
        expected = [self.change(i, k, a, b) for i in range(12) for k, a, b in (
            ("cmd", old[i]["cmd"], new[i]["cmd"]), ("defer", None, False),
            ("ignore_error", None, True), ("silent", False, None))]
        self.assertEqual(self.comparison(before, after)["definition_subfield_changes"], expected)
        reverse = self.comparison(after, before)["definition_subfield_changes"]
        self.assertEqual(reverse, [{**c, "change": {"added": "removed", "removed": "added", "changed": "changed"}[c["change"]],
                                   "before_digest": c["after_digest"], "after_digest": c["before_digest"]} for c in expected])

    def test_opaque_boundary_no_unknown_keys_or_nested_traversal(self):
        opaque = ["echo string-private", {}, {"cmd": 5}, {"cmd": {"nested-private": True}},
                  {"cmd": "echo private", "unknown-private": True}, {"task": "private"}]
        for key in ("silent", "ignore_error", "defer"):
            for val in ("true", 1, None, {}, []):
                opaque.append({"cmd": "echo private", key: val})
        before = self.snapshot({"cmds": opaque, "status": ["echo status-private"]})
        after = self.snapshot({"cmds": list(reversed(opaque)), "status": ["echo edited-private"]})
        self.assertEqual(self.record(before)["definition_subfields"], {"cmds": [None] * len(opaque)})
        delta = self.comparison(before, after)
        self.assertEqual(delta["definition_subfield_changes"], [])
        self.assertTrue(delta["definition_subfield_changes_complete"])
        self.assertTrue(delta["definition_entry_changes"])
        for output in (before.read_text(), self.run_cli("compare", before, after, "--json").stdout,
                       self.run_cli("compare", before, after).stdout):
            self.assertNotIn("private", output)

    def test_type_boundary_and_reorder_use_supported_projection_at_same_position(self):
        entry = {"cmd": "echo private", "silent": False}
        opaque = self.snapshot({"cmds": ["echo private", {**entry, "other-private": True}]})
        supported = self.snapshot({"cmds": [entry, entry]})
        self.assertEqual(self.comparison(opaque, supported)["definition_subfield_changes"],
                         [self.change(i, k, None, v) for i in range(2) for k, v in sorted(entry.items())])
        self.assertTrue(all(c["change"] == "removed" for c in self.comparison(supported, opaque)["definition_subfield_changes"]))
        a, b = {"cmd": "echo a"}, {"cmd": "echo b", "silent": True}
        before = self.snapshot({"cmds": [a, b, a]})
        reordered = self.snapshot({"cmds": [b, a, a, b]})
        self.assertEqual(self.comparison(before, reordered)["definition_subfield_changes"], [
            self.change(0, "cmd", a["cmd"], b["cmd"]), self.change(0, "silent", None, True),
            self.change(1, "cmd", b["cmd"], a["cmd"]), self.change(1, "silent", True, None),
            self.change(3, "cmd", None, b["cmd"]), self.change(3, "silent", None, True)])
        shortened = self.snapshot({"cmds": [a, a]})
        self.assertEqual(self.comparison(before, shortened)["definition_subfield_changes"], [
            self.change(1, "cmd", b["cmd"], a["cmd"]), self.change(1, "silent", True, None),
            self.change(2, "cmd", a["cmd"], None)])

    def test_context_distinguishes_task_field_entry_key_and_opaque_absence(self):
        absent, empty = self.snapshot({}), self.snapshot({"cmds": []})
        opaque = self.snapshot({"cmds": ["true"]})
        supported = self.snapshot({"cmds": [{"cmd": "true"}]})
        for path, fields in ((absent, {}), (empty, {"cmds": []}), (opaque, {"cmds": [None]}),
                             (supported, {"cmds": [["cmd"]]})):
            delta = self.comparison(path, path)
            self.assertEqual(delta["definition_subfield_context_before"], {"state": "available", "fields": fields})
            self.assertEqual(delta["definition_subfield_changes"], [])
        self.assertEqual(self.comparison(absent, empty)["definition_subfield_changes"], [])
        self.assertEqual(self.comparison(empty, supported)["definition_subfield_changes"], [self.change(0, "cmd", None, "true")])
        other = self.root / "other.json"
        self.taskfile.write_text("version: '3'\ntasks: {other: {}}\n")
        self.run_cli("snapshot", self.taskfile, "--output", other)
        for first, second, side, change in ((supported, other, "after", "removed"), (other, supported, "before", "added")):
            delta = self.comparison(first, second)
            self.assertEqual(delta[f"definition_subfield_context_{side}"], {"state": "task-absent", "fields": None})
            self.assertEqual(delta["definition_subfield_changes"][0]["change"], change)

    def test_all_legacy_levels_explicitly_unavailable_never_inferred(self):
        modern = self.snapshot({"cmds": [{"cmd": "echo private", "silent": True}]})
        for level, keys in enumerate((("definition_subfields", "definition_subfields_digest"),
                                      ("definition_entries", "definition_entries_digest"),
                                      ("definition_components", "definition_components_digest"))):
            data = json.loads((modern if level == 0 else legacy).read_text())
            for key in keys:
                del data["tasks"]["build"][key]
            legacy = self.root / f"legacy-{level}.json"
            legacy.write_text(json.dumps(data))
            for before, after in ((modern, legacy), (legacy, modern), (legacy, legacy)):
                delta = self.comparison(before, after)
                self.assertIsNone(delta["definition_subfield_changes"])
                self.assertFalse(delta["definition_subfield_changes_complete"])
                side = "after" if after == legacy else "before"
                self.assertEqual(delta[f"definition_subfield_context_{side}"], {"state": "metadata-unavailable", "fields": None})
                self.assertEqual(delta["change"], "unchanged")
                self.assertFalse(delta["advisory_changed"])
                self.assertIn("definition subfields: unavailable (legacy snapshot)", self.run_cli("compare", before, after).stdout)

    def test_deterministic_redaction_binding_formulas_and_no_execution(self):
        marker = self.root / "NEVER"
        entry = {"cmd": f"touch {marker}; echo private", "silent": False, "ignore_error": True, "defer": False}
        task = {"cmds": [entry], "status": [f"touch {marker}; echo status-private"], "generates": ["never.out"]}
        before = self.snapshot(task)
        repeat = self.snapshot({**task, "cmds": [dict(reversed(list(entry.items())))]})
        self.assertEqual(before.read_bytes(), repeat.read_bytes())
        record = self.record(before)
        subfields = {"cmds": [{k: sha(v) for k, v in entry.items()}]}
        self.assertEqual(record["definition_subfields"], subfields)
        self.assertEqual(record["definition_subfields_digest"], sha({"definition_entries_digest": record["definition_entries_digest"], "subfields": subfields}))
        self.assertEqual(record["definition_digest"], sha(task))
        model = json.loads(before.read_text())["model"]
        self.assertEqual(record["source_only_fingerprint"], sha({"model": model + "/sources-only", "sources": {}}))
        self.assertEqual(record["advisory_fingerprint"], sha({"model": model, "sources": {}, "definition": sha(task), "inputs": {}}))
        self.run_cli("check", self.taskfile, "--strict", "--json", code=1)
        for output in (before.read_text(), self.run_cli("check", self.taskfile, "--json").stdout,
                       self.run_cli("compare", before, repeat, "--json").stdout, self.run_cli("compare", before, repeat).stdout):
            self.assertNotIn(str(marker), output)
            self.assertNotIn("private", output)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "never.out").exists())

    def test_corruption_pairs_shape_and_binding_rejected_with_safe_errors(self):
        valid_path = self.snapshot({"cmds": [{"cmd": "echo private", "silent": True}], "status": []})
        valid = json.loads(valid_path.read_text())
        self.assertIn("definition_subfields", valid["tasks"]["build"])
        cases = []
        for key in ("definition_subfields", "definition_subfields_digest", "definition_entries", "definition_entries_digest",
                    "definition_components", "definition_components_digest"):
            data = copy.deepcopy(valid)
            del data["tasks"]["build"][key]
            cases.append(data)
        for subfields in (None, [], "secret-invalid", {}, {"status": []}, {"cmds": []}, {"cmds": [None, None]},
                          {"cmds": "secret-invalid"}, {"cmds": [[]]}, {"cmds": [{}]}, {"cmds": [{"silent": sha(True)}]},
                          {"cmds": [{"cmd": "F" * 64}]}, {"cmds": [{"cmd": {"nested-private": sha(True)}}]},
                          {"cmds": [{"cmd": sha("echo private"), "unknown-private": sha(True)}]},
                          *({"cmds": [{"cmd": sha("echo private"), key: bad}]} for key in ("silent", "ignore_error", "defer")
                            for bad in (None, True, sha(1), sha("true"), "secret-invalid"))):
            data = copy.deepcopy(valid)
            record = data["tasks"]["build"]
            record["definition_subfields"] = subfields
            record["definition_subfields_digest"] = sha({"definition_entries_digest": record["definition_entries_digest"], "subfields": subfields})
            cases.append(data)
        for checksum in (None, [], "secret-invalid", "0" * 64):
            data = copy.deepcopy(valid)
            data["tasks"]["build"]["definition_subfields_digest"] = checksum
            cases.append(data)
        for mutation in ("child", "entry-binding"):
            data = copy.deepcopy(valid)
            record = data["tasks"]["build"]
            if mutation == "child":
                record["definition_subfields"]["cmds"][0]["silent"] = sha(False)
            else:
                record["definition_entries"]["cmds"][0] = "0" * 64
                record["definition_entries_digest"] = sha({"definition_components_digest": record["definition_components_digest"], "entries": record["definition_entries"]})
            cases.append(data)
        for i, data in enumerate(cases):
            with self.subTest(case=i):
                bad = self.root / "bad.json"
                bad.write_text(json.dumps(data))
                result = self.run_cli("compare", valid_path, bad, "--json", code=2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("private", result.stderr)
                self.assertNotIn("secret-invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()
