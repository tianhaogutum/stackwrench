"""Digest-only positional definition attribution through the CLI."""
import copy
import json
import unittest

from . import test_definition_components as components

sha = components.sha


class DefinitionEntryTests(unittest.TestCase):
    setUp = components.DefinitionComponentTests.setUp
    run_cli = components.DefinitionComponentTests.run_cli
    snapshot = components.DefinitionComponentTests.snapshot
    record = components.DefinitionComponentTests.record
    comparison = components.DefinitionComponentTests.comparison

    def change(self, field, index, old, new):
        return {"location": [field, index],
                "change": "added" if old is None else "removed" if new is None else "changed",
                "before_digest": sha(old) if old is not None else None,
                "after_digest": sha(new) if new is not None else None}

    def without_entries(self, path, *, components_too=False):
        data = json.loads(path.read_text())
        record = data["tasks"]["build"]
        for key in ("definition_subfields", "definition_subfields_digest", "definition_entries", "definition_entries_digest"):
            record.pop(key, None)
        if components_too:
            for key in ("definition_components", "definition_components_digest"):
                record.pop(key)
        target = self.root / f"legacy-{components_too}-{path.name}"
        target.write_text(json.dumps(data))
        return target

    def test_only_edited_command_or_status_position_is_identified(self):
        baseline = {"cmds": ["echo first-private", "echo second-private"],
                    "status": ["echo status-private", "echo other-private"]}
        before = self.snapshot(baseline)
        for field in ("cmds", "status"):
            with self.subTest(field=field):
                edited = copy.deepcopy(baseline)
                edited[field][1] = "echo edited-private"
                after = self.snapshot(edited)
                self.taskfile.unlink()  # Comparison needs no original definition.
                delta = self.comparison(before, after)
                self.assertEqual(delta["definition_entry_changes"], [
                    self.change(field, 1, baseline[field][1], edited[field][1])])
                self.assertTrue(delta["definition_entry_changes_complete"])
                self.assertEqual([c["field"] for c in delta["definition_changes"]], [field])
                self.assertTrue(delta["definition_changed"])
                self.assertTrue(delta["advisory_changed"])
                self.assertFalse(delta["source_only_changed"])
                text = self.run_cli("compare", before, after).stdout
                self.assertIn(f"definition entry: [{field}, 1] (changed)", text)
                self.assertIn(sha(baseline[field][1]), text)
                self.assertIn(sha(edited[field][1]), text)
                self.assertNotIn("private", text)

    def test_append_remove_reorder_and_numeric_position_order(self):
        before = self.snapshot({"cmds": ["echo a", "echo b", "echo a"], "status": ["true"]})
        after = self.snapshot({"cmds": ["echo b", "echo a", "echo a", "echo c"],
                               "status": ["false", "true"]})
        expected = [self.change("cmds", 0, "echo a", "echo b"),
                    self.change("cmds", 1, "echo b", "echo a"),
                    self.change("cmds", 3, None, "echo c"),
                    self.change("status", 0, "true", "false"),
                    self.change("status", 1, None, "true")]
        self.assertEqual(self.comparison(before, after)["definition_entry_changes"], expected)
        reverse = self.comparison(after, before)["definition_entry_changes"]
        self.assertEqual(reverse, [{**c, "change": "removed" if c["change"] == "added" else "changed",
                                    "before_digest": c["after_digest"], "after_digest": c["before_digest"]}
                                   for c in expected])
        short = self.snapshot({"cmds": ["echo a", "echo a"]})
        self.assertEqual(self.comparison(before, short)["definition_entry_changes"], [
            self.change("cmds", 1, "echo b", "echo a"), self.change("cmds", 2, "echo a", None),
            self.change("status", 0, "true", None)])
        many = self.snapshot({"cmds": [f"echo {i}" for i in range(12)]})
        empty = self.snapshot({})
        self.assertEqual([c["location"][1] for c in self.comparison(empty, many)["definition_entry_changes"]],
                         list(range(12)))

    def test_presence_context_distinguishes_tasks_fields_entries_and_legacy(self):
        absent_field = self.snapshot({})
        empty_field = self.snapshot({"cmds": [], "status": []})
        delta = self.comparison(absent_field, empty_field)
        self.assertEqual(delta["definition_entry_changes"], [])
        self.assertEqual(delta["definition_entry_context_before"], {"state": "available", "fields": []})
        self.assertEqual(delta["definition_entry_context_after"], {"state": "available", "fields": ["cmds", "status"]})
        nonempty = self.snapshot({"cmds": ["true"], "status": []})
        self.assertEqual(self.comparison(empty_field, nonempty)["definition_entry_changes"],
                         [self.change("cmds", 0, None, "true")])
        other = self.root / "other.json"
        self.taskfile.write_text("version: '3'\ntasks: {other: {}}\n")
        self.run_cli("snapshot", self.taskfile, "--output", other)
        for first, second, side, change in ((nonempty, other, "after", "removed"),
                                           (other, nonempty, "before", "added")):
            delta = self.comparison(first, second)
            self.assertTrue(delta["definition_entry_changes_complete"])
            self.assertEqual(delta[f"definition_entry_context_{side}"], {"state": "task-absent", "fields": None})
            self.assertEqual(delta["definition_entry_changes"][0]["change"], change)
        for legacy in (self.without_entries(nonempty), self.without_entries(nonempty, components_too=True)):
            for first, second in ((nonempty, legacy), (legacy, nonempty), (legacy, legacy), (legacy, other)):
                delta = self.comparison(first, second)
                self.assertIsNone(delta["definition_entry_changes"])
                self.assertFalse(delta["definition_entry_changes_complete"])
                side = "before" if first == legacy else "after"
                self.assertEqual(delta[f"definition_entry_context_{side}"],
                                 {"state": "metadata-unavailable", "fields": None})
                if second != other:
                    self.assertEqual(delta["change"], "unchanged")
                    self.assertFalse(delta["advisory_changed"])
                self.assertIn("definition entries: unavailable (legacy snapshot)",
                              self.run_cli("compare", first, second).stdout)
        # Field-level attribution survives the lack of entry metadata.
        legacy = self.without_entries(nonempty)
        self.assertTrue(self.comparison(legacy, nonempty)["definition_changes_complete"])

    def test_entries_are_redacted_deterministic_opaque_and_formulas_unchanged(self):
        marker = self.root / "NEVER"
        task = {"cmds": [f"touch {marker}; echo private", {"cmd": "echo structured-private", "silent": True}],
                "status": [f"touch {marker}; echo status-private"], "sources": ["base.txt"],
                "env": {"UNUSED": "env-private"}, "vars": {"UNUSED": "vars-private"},
                "desc": "desc-private", "custom": {"secret": "opaque-private"}}
        before = self.snapshot(task)
        repeat = self.snapshot(dict(reversed(list(task.items()))))
        self.assertEqual(before.read_bytes(), repeat.read_bytes())
        record = self.record(before)
        entries = {field: list(map(sha, task[field])) for field in ("cmds", "status")}
        self.assertEqual(record["definition_entries"], entries)
        self.assertEqual(record["definition_entries_digest"], sha({
            "definition_components_digest": record["definition_components_digest"], "entries": entries}))
        definition = {k: v for k, v in task.items() if k not in {"vars", "env", "desc", "summary"}}
        self.assertEqual(record["definition_digest"], sha(definition))
        model = json.loads(before.read_text())["model"]
        self.assertEqual(record["source_only_fingerprint"], sha({"model": model + "/sources-only", "sources": record["source_hashes"]}))
        self.assertEqual(record["advisory_fingerprint"], sha({"model": model, "sources": record["source_hashes"],
            "definition": sha(definition), "inputs": {}}))
        self.assertEqual(record["assessment"], "partial")
        self.assertIn("unsupported-command", [f["code"] for f in record["findings"]])
        after = self.snapshot({**task, "cmds": [task["cmds"][0], {"cmd": "echo edited-private", "silent": True}]})
        self.assertEqual(self.comparison(before, after)["definition_entry_changes"], [
            self.change("cmds", 1, task["cmds"][1], {"cmd": "echo edited-private", "silent": True})])
        for output in (before.read_text(), self.run_cli("check", self.taskfile, "--json").stdout,
                       self.run_cli("compare", before, after, "--json").stdout,
                       self.run_cli("compare", before, after).stdout):
            for secret in (str(marker), "private", "private source contents"):
                self.assertNotIn(secret, output)
        self.assertFalse(marker.exists())
        string, array, mapping = (self.snapshot("true"), self.snapshot(["true"]), self.snapshot({"cmds": ["true"]}))
        self.assertEqual(string.read_bytes(), array.read_bytes())
        self.assertEqual(array.read_bytes(), mapping.read_bytes())

    def test_malformed_entries_rejected_without_excerpts_even_with_recomputed_checksum(self):
        valid_path = self.snapshot({"cmds": ["echo private"], "status": ["true"]})
        valid = json.loads(valid_path.read_text())
        self.assertIn("definition_entries", valid["tasks"]["build"])
        cases = []
        for key in ("definition_entries", "definition_entries_digest", "definition_components", "definition_components_digest"):
            data = copy.deepcopy(valid)
            del data["tasks"]["build"][key]
            cases.append(data)
        invalid_maps = (None, [], "secret-invalid", {}, {"cmds": []},
                        {"cmds": ["0" * 64], "status": ["secret-invalid"]},
                        {"cmds": ["F" * 64], "status": [sha("true")]},
                        {"cmds": [None], "status": [sha("true")]},
                        {"cmds": "secret-invalid", "status": [sha("true")]},
                        {"cmds": [], "status": [sha("true")]},
                        {"cmds": [sha("echo private")], "status": []},
                        {"cmds": [sha("echo private")], "status": [sha("true")], "sources": []})
        for entries in invalid_maps:
            data = copy.deepcopy(valid)
            record = data["tasks"]["build"]
            record["definition_entries"] = entries
            record["definition_entries_digest"] = sha({
                "definition_components_digest": record["definition_components_digest"], "entries": entries})
            cases.append(data)
        for checksum in (None, [], "secret-invalid", "0" * 64):
            data = copy.deepcopy(valid)
            data["tasks"]["build"]["definition_entries_digest"] = checksum
            cases.append(data)
        for mutation in ("entry", "component-binding"):
            data = copy.deepcopy(valid)
            record = data["tasks"]["build"]
            if mutation == "entry":
                record["definition_entries"]["cmds"][0] = "0" * 64
            else:
                record["definition_components"]["cmds"] = "0" * 64
                record["definition_components_digest"] = sha({"definition_digest": record["definition_digest"],
                                                              "components": record["definition_components"]})
            cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(index=index):
                bad = self.root / "bad.json"
                bad.write_text(json.dumps(data))
                result = self.run_cli("compare", valid_path, bad, "--json", code=2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("secret-invalid", result.stderr)
                self.assertNotIn("private", result.stderr)


if __name__ == "__main__":
    unittest.main()
