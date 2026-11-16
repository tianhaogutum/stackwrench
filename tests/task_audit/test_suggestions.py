"""Installed CLI declaration suggestions, using local synthetic Taskfile v3 fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from .support import CLI, EXAMPLES, cli_env


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.taskfile = self.root / "Taskfile.yml"
        (self.root / "config file.txt").write_text("synthetic-file-secret")
        (self.root / "covered.txt").write_text("already declared")

    def write(self, commands, **fields):
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "build": {"cmds": commands, **fields}}}))

    def run_cli(self, *args, code=0, env=None):
        result = subprocess.run([*CLI, "suggest", str(self.taskfile), "--root", str(self.root),
                                 *map(str, args)], text=True, capture_output=True, timeout=15, env=cli_env(env))
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def report(self, *args, code=0, env=None):
        return json.loads(self.run_cli("--json", *args, code=code, env=env).stdout)

    def test_suggest_candidates_locations_and_roundtrip_fragment(self):
        self.write(["cat 'config file.txt' covered.txt; echo \"$MODE\" ${MODE}",
                    "cat < 'config file.txt'"], sources=["covered.txt"])
        original = self.taskfile.read_bytes()
        report = self.report("--strict", code=1)
        task = report["tasks"]["build"]
        source, = task["sources"]
        env, = task["env"]
        self.assertEqual(source["path"], "config file.txt")
        self.assertEqual(env["name"], "MODE")
        self.assertEqual([loc["path"] for loc in source["locations"]],
                         [["tasks", "build", "cmds", 0], ["tasks", "build", "cmds", 1]])
        self.assertEqual(env["locations"], [{"path": ["tasks", "build", "cmds", 0]}])
        self.assertEqual({f["code"] for f in task["advisories"]},
                         {"missing-source-declaration", "missing-env-declaration"})
        self.assertTrue(report["read_only"])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertTrue(report["false_positive_boundaries"])
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(task["yaml_fragment"])["tasks"]["build"]
        self.assertEqual(fragment["env"], {"MODE": "REVIEW_REQUIRED"})
        self.assertEqual(fragment["sources"], ["config file.txt"])
        data = yaml.safe_load(self.taskfile.read_text())
        data["tasks"]["build"]["sources"] += fragment["sources"]
        data["tasks"]["build"]["env"] = fragment["env"]
        self.taskfile.write_text(yaml.safe_dump(data))
        fixed = self.report()["tasks"]["build"]
        self.assertEqual(fixed["sources"], [])
        self.assertEqual(fixed["env"], [])
        self.assertEqual(fixed["advisories"], [])
        # The original audit's structural heuristic is deliberately separate.
        checked = subprocess.run([*CLI, "check", str(self.taskfile), "--json"], env=cli_env(),
                                 text=True, capture_output=True, check=True)
        self.assertIn("potential-fingerprint-gap", checked.stdout)

    def test_suggest_shell_quotes_scopes_and_redaction(self):
        self.write([r'''echo '$LITERAL' \$ESCAPED "$MODE" ${MODE} $DECLARED $VAR_ONLY''',
                    "echo nothing # $COMMENT",
                    "TMP=synthetic-command-secret; echo $TMP"],
                   env={"DECLARED": "synthetic-env-secret"}, vars={"VAR_ONLY": "not-exported"})
        env_file = self.root / "env.json"
        env_file.write_text('{"MODE":"synthetic-snapshot-secret"}')
        result = self.run_cli("--json", "--env-snapshot", env_file,
                              env=dict(os.environ, HOST_ONLY="synthetic-host-secret"))
        task = json.loads(result.stdout)["tasks"]["build"]
        self.assertEqual([item["name"] for item in task["env"]], ["MODE", "VAR_ONLY"])
        self.assertTrue(any(item["reason"] == "possible-shell-local" for item in task["uncertain"]))
        for secret in ("synthetic-command-secret", "synthetic-env-secret", "synthetic-snapshot-secret",
                       "synthetic-host-secret", "synthetic-file-secret", "not-exported"):
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, self.run_cli().stdout)
        self.assertEqual(result.stdout, self.run_cli("--json", "--env-snapshot", env_file).stdout)
        self.assertIn("missing-env-declaration", self.run_cli().stdout)
        self.assertIn("cmds", self.run_cli().stdout)

    def test_suggest_dynamic_indirect_and_unsupported_commands_remain_uncertain(self):
        marker = self.root / "NEVER"
        commands = ["cat $FILE", "echo ${!INDIRECT}", "echo ${MODE:-fallback}",
                    f"cat $(touch {marker})", "echo `date`", "cat {{.FILE}}",
                    "/bin/sh -c 'cat covered.txt; echo $HIDDEN'", "eval '$HIDDEN'",
                    "cat <<'EOF'\n$BODY\nEOF", "cat 'unterminated", "cd sub; cat covered.txt",
                    {"cmd": "cat covered.txt", "silent": True}]
        self.write(commands, status=[f"touch {marker}"], vars={"FILE": {"sh": f"touch {marker}"}})
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual({item["location"]["path"][-1] for item in task["uncertain"]
                          if item.get("location")}, set(range(len(commands))))
        self.assertFalse(marker.exists())

    def test_suggest_source_boundaries_and_existing_glob_coverage(self):
        (self.root / "alias.txt").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        self.write(["cat covered.txt", "cat absent.txt", "cat alias.txt", "cat linkdir/covered.txt",
                    "cat ../outside.txt", "cat /etc/passwd", "cat *.txt", "cat -n covered.txt",
                    "echo output > 'config file.txt'"], sources=["covered.*"])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertTrue(task["uncertain"])
        self.assertEqual(task["yaml_fragment"], None)

    def test_suggest_copyable_text_escapes_control_characters_and_safe_yaml(self):
        name = 'quoted"task\x1b\nname'
        data = {"version": "3", "tasks": {name: {"cmds": ['echo "$MODE"']}}}
        self.taskfile.write_text(yaml.safe_dump(data))
        result = self.run_cli().stdout
        self.assertNotIn("\x1b", result)
        fragment = result.split("  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1]
        self.assertEqual(yaml.safe_load(fragment), {"tasks": {name: {"env": {"MODE": "REVIEW_REQUIRED"}}}})

    def test_suggest_literal_shell_syntax_is_not_an_indirect_input(self):
        self.write([r'''echo '${NOT_EXPANDED:-fallback}' '`date`' \$ESCAPED'''])
        task = self.report("--strict")["tasks"]["build"]
        self.assertEqual(task["env"], [])
        self.assertEqual(task["uncertain"], [])
        self.assertEqual(task["unassessed_findings"], [])

    def test_suggest_fd_and_embedded_environment_reads_are_uncertain(self):
        (self.root / "2").write_text("not an operand")
        self.write(["cat 2<covered.txt", "printenv MODE", "python3 -c 'import os'",
                    "TMP=$(date)", "echo $TMP"])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(len(task["uncertain"]), 5)

    def test_suggest_selection_shorthand_failures_and_no_overwrite_option(self):
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": "echo $ONE", "list": ["echo $TWO"], "ignored": {"sources": ["absent"]}}}))
        report = self.report("--task", "string", "--task", "list")
        self.assertEqual(set(report["tasks"]), {"string", "list"})
        self.assertEqual(report["tasks"]["string"]["env"][0]["locations"],
                         [{"path": ["tasks", "string"]}])
        self.assertEqual(report["tasks"]["list"]["env"][0]["locations"],
                         [{"path": ["tasks", "list", 0]}])
        self.run_cli("--task", "unknown", code=2)
        self.run_cli("--json", code=2)  # Existing declared missing sources stay errors.
        self.run_cli("--task", "string", "--output", self.taskfile, code=2)
        self.taskfile.write_text("version: '3'\ntasks: {broken: [synthetic-secret\n")
        result = self.run_cli("--json", code=2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("synthetic-secret", result.stderr)

    def test_suggest_global_declarations_and_unknown_task_context(self):
        data = {"version": "3", "env": {"MODE": "synthetic-declared"}, "tasks": {
            "build": {"cmds": ["cat covered.txt; echo $MODE"], "sources": ["covered.*"]}}}
        self.taskfile.write_text(yaml.safe_dump(data))
        task = self.report("--strict")["tasks"]["build"]
        self.assertEqual(task["advisories"], [])
        data["tasks"]["build"]["dir"] = "subdirectory"
        self.taskfile.write_text(yaml.safe_dump(data))
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"][0]["reason"], "unsupported-task-context")
        self.assertIn("unsupported-field", {entry["code"] for entry in task["unassessed_findings"]})

    def test_suggest_repository_example(self):
        self.taskfile = EXAMPLES / "suggestions.yml"
        report = self.report("--root", self.taskfile.parent, "--task", "review")
        task = report["tasks"]["review"]
        self.assertEqual([entry["path"] for entry in task["sources"]], ["base.txt"])
        self.assertEqual([entry["name"] for entry in task["env"]], ["MODE"])
        self.assertEqual(task["uncertain"], [])

    def test_leading_dot_input_normalization_dedup_locations_and_redaction(self):
        self.write(['cat "./config file.txt"; cat < "./config file.txt"; echo "$MODE"',
                    r"cat -- ./config\ file.txt 'config file.txt'; echo ${MODE}",
                    "< './config file.txt' cat"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        locs = [{"path": ["tasks", "build", "cmds", i]} for i in range(3)]
        self.assertEqual(task["sources"], [{"path": "config file.txt", "locations": locs}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": locs[:2]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-file-secret", "synthetic-env-secret"):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())

    def test_leading_dot_input_coverage_and_reviewed_fragment_merge(self):
        commands = ['cat "./config file.txt"; cat < "./config file.txt"; echo "$MODE"']
        for pattern in ("config file.txt", "config*.txt"):
            with self.subTest(pattern=pattern):
                self.write(commands, sources=[pattern], env={"MODE": "reviewed"})
                task = self.report("--strict")["tasks"]["build"]
                for key in ("sources", "env", "advisories", "uncertain", "unassessed_findings"):
                    self.assertEqual(task[key], [])
                self.assertIsNone(task["yaml_fragment"])
        self.write(commands, sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        before = self.report("--strict", code=1)["tasks"]["build"]
        fragment_text = self.run_cli().stdout.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1]
        fragment = yaml.safe_load(fragment_text)["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(before["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["config file.txt"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        after = reviewed["tasks"]["build"]
        for key in ("sources", "env", "advisories", "uncertain", "unassessed_findings"):
            self.assertEqual(after[key], [])
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(declarations["sources"], ["covered.txt", "config file.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_leading_dot_input_shorthand_locations(self):
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": "cat './config file.txt'", "list": ["cat < './config file.txt'"]}}))
        tasks = self.report()["tasks"]
        for name, path in (("string", ["tasks", "string"]), ("list", ["tasks", "list", 0])):
            self.assertEqual(tasks[name]["sources"], [{"path": "config file.txt", "locations": [{"path": path}]}])
            self.assertEqual(tasks[name]["uncertain"], [])

    def test_leading_dot_input_unsafe_paths_and_strict_declaration_patterns(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", ""]
        for prefix in ("cat", "cat <"):
            with self.subTest(prefix=prefix):
                self.write([f"{prefix} '{path}'" for path in paths], sources=["covered.txt"])
                task = self.report("--strict", code=1)["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["advisories"], [])
                self.assertEqual(task["uncertain"], [
                    {"location": {"path": ["tasks", "build", "cmds", i]},
                     "reason": "source-path-not-safe-regular-literal"} for i in range(len(paths))])
        for field in ("sources", "generates"):
            for pattern in ("./covered.txt", "././covered.txt", ".//covered.txt", "dir/./file",
                            "dir//file", "dir/../covered.txt", "/absolute", ""):
                with self.subTest(field=field, pattern=pattern):
                    self.write(["cat ./covered.txt"], **{field: [pattern]})
                    self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_leading_dot_input_options_dynamic_context_and_output_exclusion(self):
        commands = ['cat -n ./covered.txt "$MODE"', 'cat --number ./covered.txt "$MODE"',
                    'cat ./covered.txt -n "$MODE"', 'cat ./covered.txt ./$FILE; echo "$MODE"',
                    'cat < ./$FILE; echo "$MODE"', 'cat ./covered.txt | echo "$MODE"',
                    'cat ./covered.txt && echo "$MODE"', "sh -c 'cat ./covered.txt; echo $MODE'",
                    'env cat ./covered.txt "$MODE"', 'cat 2<./covered.txt; echo "$MODE"',
                    'cat {{.FILE}}; echo "$MODE"', {"cmd": "cat ./covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        for context in ("dir", "includes"):
            data = {"version": "3", "tasks": {"build": {"cmds": ["cat ./covered.txt; echo $MODE",
                                                                            "cat < ./covered.txt; echo $MODE"]}}}
            if context == "dir":
                data["tasks"]["build"]["dir"] = "elsewhere"
            else:
                data["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(data))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual([u["reason"] for u in task["uncertain"]], ["unsupported-task-context"] * 2)
        (self.root / "-n").write_text("synthetic-option-name-secret")
        self.write(["echo ./covered.txt > './config file.txt'", "cat -", "cat -- ./-n",
                    "echo < ./covered.txt"])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [
            {"path": "-n", "locations": [{"path": ["tasks", "build", "cmds", 2]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "build", "cmds", 3]}]}])
        self.assertEqual(task["uncertain"], [])

    def test_leading_dot_input_candidates_use_metadata_only_and_never_execute(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        marker = self.root / "NEVER"
        self.write(['cat "./config file.txt"; cat < "./config file.txt"; echo "$MODE"'],
                   status=[f"touch {marker}"])
        original_open = Path.open
        opened = []

        def document_only_open(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", document_only_open), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        self.assertEqual(report["tasks"]["build"]["sources"], [
            {"path": "config file.txt", "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        self.assertFalse(marker.exists())

    def test_uniq_input_output_normalized_locations_redaction_and_roundtrip(self):
        for name in ("output.txt", "ignored.txt"):
            (self.root / name).write_text("synthetic-output-secret")
        command = 'uniq "./config file.txt" "output.txt"; uniq -- "config file.txt"; echo "$MODE"'
        self.write([command], sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        loc = {"path": ["tasks", "build", "cmds", 0]}
        self.assertEqual(task["sources"], [{"path": "config file.txt", "locations": [loc]}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [loc]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-file-secret", "synthetic-env-secret", "synthetic-output-secret",
                       "output.txt", "ignored.txt"):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(text.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1])["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(task["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["config file.txt"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(reviewed["tasks"]["build"], {"sources": [], "env": [], "advisories": [],
                         "uncertain": [], "unassessed_findings": [], "yaml_fragment": None})
        self.assertEqual(declarations["sources"], ["covered.txt", "config file.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_uniq_coverage_shorthand_and_stdin_operand_arity(self):
        for pattern in ("config file.txt", "config*.txt"):
            with self.subTest(pattern=pattern):
                self.write(['uniq "./config file.txt" covered.txt'], sources=[pattern])
                task = self.report("--strict")["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["uncertain"], [])
                self.assertIsNone(task["yaml_fragment"])
        (self.root / "-c").write_text("synthetic-dash-secret")
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": r"uniq ./config\ file.txt covered.txt; uniq 'config file.txt'",
            "list": ["uniq -- './config file.txt' covered.txt", "uniq 'config file.txt'"],
            "stdin": ["uniq", "uniq --", "uniq -", "uniq -- -", "uniq - covered.txt",
                      "uniq -- - -c"],
            "dash": ["uniq -- -c -o", "uniq ./-c", "uniq -- ./-c -"],
            "outside": ['echo "config file.txt"', '/usr/bin/uniq "config file.txt"']}}))
        tasks = self.report()["tasks"]
        for name, locs in (("string", [["tasks", "string"]]),
                           ("list", [["tasks", "list", 0], ["tasks", "list", 1]])):
            self.assertEqual(tasks[name]["sources"], [{"path": "config file.txt",
                             "locations": [{"path": path} for path in locs]}])
        for name in ("stdin", "outside"):
            self.assertEqual(tasks[name]["sources"], [])
            self.assertEqual(tasks[name]["uncertain"], [])
        self.assertEqual(tasks["dash"]["sources"], [{"path": "-c", "locations": [
            {"path": ["tasks", "dash", i]} for i in range(3)]}])
        self.assertEqual(tasks["dash"]["uncertain"], [])

    def test_uniq_options_and_excess_operands_withhold_entire_command(self):
        options = ["-c covered.txt", "--count covered.txt", "-d covered.txt", "--help", "--version",
                   "-f covered.txt", "--skip-fields=covered.txt", "-s covered.txt", "-w covered.txt",
                   "covered.txt -c", "covered.txt --", "-- -- covered.txt", "-- covered.txt --"]
        operands = ["covered.txt output.txt ignored.txt", "-- covered.txt output.txt ignored.txt",
                    "- output.txt ignored.txt"]
        commands = [f'cat "config file.txt"; uniq {arg}; echo "$MODE"' for arg in options + operands]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "uniq-options-not-modeled" if i < len(options)
                         else "uniq-operand-count-not-modeled"} for i in range(len(commands))])

    def test_uniq_dynamic_input_output_control_and_context_withhold_command(self):
        commands = ['uniq $FILE covered.txt; echo "$MODE"', 'uniq -- "${FILE}"',
                    'cat covered.txt; uniq "config file.txt" "$OUTPUT"; echo "$MODE"',
                    'uniq -- - ./$OUTPUT; echo "$MODE"', '$RUNNER covered.txt; echo "$MODE"',
                    '"$BIN/uniq" covered.txt', 'uniq $(touch NEVER)', 'uniq {{.FILE}}',
                    'uniq covered.txt | echo "$MODE"', 'uniq covered.txt && echo "$MODE"',
                    "sh -c 'uniq covered.txt; echo $MODE'", 'env uniq covered.txt; echo "$MODE"',
                    'uniq covered.txt; cat $FILE', 'uniq 2<covered.txt; echo "$MODE"',
                    {"cmd": "uniq covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        self.assertEqual([u["reason"] for u in task["uncertain"][:4]],
                         ["dynamic-source-operand"] * 2 + ["dynamic-output-operand"] * 2)
        self.assertFalse((self.root / "NEVER").exists())
        for field in ("dir", "includes"):
            data = {"version": "3", "tasks": {"build": {"cmds": ['uniq covered.txt; echo "$MODE"']}}}
            if field == "dir":
                data["tasks"]["build"]["dir"] = "elsewhere"
            else:
                data["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(data))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                             "reason": "unsupported-task-context"}])

    def test_uniq_unsafe_inputs_and_strict_declaration_patterns(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", "", "*.txt"]
        self.write([f"uniq '{path}' output.txt" for path in paths], sources=["covered.txt"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"], [
            {"location": {"path": ["tasks", "build", "cmds", i]},
             "reason": "source-path-not-safe-regular-literal"} for i in range(len(paths))])
        for field in ("sources", "generates"):
            for pattern in ("./covered.txt", "dir/./file", "dir//file", "dir/../covered.txt"):
                with self.subTest(field=field, pattern=pattern):
                    self.write(["uniq ./covered.txt output.txt"], **{field: [pattern]})
                    self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_uniq_output_is_never_a_candidate_or_inspected(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        (self.root / "output.txt").write_text("synthetic-output-secret")
        (self.root / "alias").symlink_to(self.root / "output.txt")
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        outputs = ["output.txt", "./output.txt", "././output.txt", "../outside", "/absolute",
                   "dir/./file", "dir//file", "alias", "dir", "pipe", "missing", "", "*.txt"]
        self.write([f"uniq './config file.txt' '{output}'" for output in outputs])
        original_open, original_lstat = Path.open, Path.lstat

        def document_only_open(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate/output contents must not be opened")
            return original_open(path, *args, **kwargs)

        def input_only_lstat(path, *args, **kwargs):
            self.assertEqual(path, self.root / "config file.txt", "output must not be inspected")
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "open", document_only_open), \
                patch.object(Path, "lstat", input_only_lstat), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            task = suggest(self.taskfile, self.root)["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": "config file.txt", "locations": [
            {"path": ["tasks", "build", "cmds", i]} for i in range(len(outputs))]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse((self.root / "NEVER").exists())

    def test_wc_literal_operands_normalized_locations_redaction_and_roundtrip(self):
        (self.root / "ignored.txt").write_text("synthetic-ignored-secret")
        command = 'wc "./config file.txt"; wc -- "config file.txt"; echo "$MODE"'
        self.write([command], sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        loc = {"path": ["tasks", "build", "cmds", 0]}
        self.assertEqual(task["sources"], [{"path": "config file.txt", "locations": [loc]}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [loc]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-file-secret", "synthetic-env-secret", "synthetic-ignored-secret"):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(text.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1])["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(task["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["config file.txt"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(reviewed["tasks"]["build"], {"sources": [], "env": [], "advisories": [],
                         "uncertain": [], "unassessed_findings": [], "yaml_fragment": None})
        self.assertEqual(declarations["sources"], ["covered.txt", "config file.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_wc_literal_glob_coverage_shorthand_and_multiple_operands(self):
        for pattern in ("config file.txt", "config*.txt"):
            with self.subTest(pattern=pattern):
                self.write(['wc "./config file.txt"; wc -- "config file.txt"'], sources=[pattern])
                task = self.report("--strict")["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["uncertain"], [])
                self.assertIsNone(task["yaml_fragment"])
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": r"wc ./config\ file.txt; wc 'config file.txt'",
            "list": ["wc -- './config file.txt' covered.txt", "wc 'config file.txt'"]}}))
        tasks = self.report()["tasks"]
        self.assertEqual(tasks["string"]["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "string"]}]}])
        self.assertEqual(tasks["list"]["sources"], [
            {"path": "config file.txt", "locations": [{"path": ["tasks", "list", 0]},
                                                       {"path": ["tasks", "list", 1]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "list", 0]}]}])

    def test_wc_options_withhold_entire_command_without_file_list_inference(self):
        options = ["-l covered.txt", "--lines covered.txt", "-cl covered.txt", "--help", "--version",
                   "--files0-from covered.txt", "--files0-from=covered.txt", "--files0-from=-",
                   "--total=always covered.txt", "covered.txt -c", "covered.txt --", "-- -- covered.txt"]
        self.write([f'cat "config file.txt"; wc {option}; echo "$MODE"' for option in options])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "wc-options-not-modeled"} for i in range(len(options))])

    def test_wc_dynamic_control_context_and_argument_output_exclusion(self):
        commands = ['wc ./covered.txt $FILE; echo "$MODE"', '$RUNNER ./covered.txt; echo "$MODE"',
                    '"$BIN/wc" ./covered.txt', 'wc -- "${FILE}"', 'wc $(touch NEVER)',
                    'wc {{.FILE}}', 'wc covered.txt | echo "$MODE"', 'wc covered.txt && echo "$MODE"',
                    "sh -c 'wc covered.txt; echo $MODE'", 'env wc covered.txt; echo "$MODE"',
                    'wc covered.txt; cat $FILE', 'wc 2<covered.txt; echo "$MODE"',
                    {"cmd": "wc covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        for field in ("dir", "includes"):
            doc = {"version": "3", "tasks": {"build": {"cmds": ['wc covered.txt; echo "$MODE"']}}}
            if field == "dir":
                doc["tasks"]["build"]["dir"] = "elsewhere"
            else:
                doc["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(doc))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                             "reason": "unsupported-task-context"}])
        (self.root / "-l").write_text("synthetic-dash-file-secret")
        self.write(['wc', 'wc -', 'wc -- -', 'wc -- -l', 'wc ./-l',
                    'wc covered.txt > "config file.txt"', 'echo "config file.txt"',
                    '/usr/bin/wc "config file.txt"'])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [
            {"path": "-l", "locations": [{"path": ["tasks", "build", "cmds", 3]},
                                           {"path": ["tasks", "build", "cmds", 4]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "build", "cmds", 5]}]}])
        self.assertEqual(task["uncertain"], [])

    def test_wc_unsafe_paths_and_strict_declarations(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", "", "*.txt"]
        self.write([f"wc '{path}'" for path in paths], sources=["covered.txt"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "source-path-not-safe-regular-literal"} for i in range(len(paths))])
        for field in ("sources", "generates"):
            self.write(["wc ./covered.txt"], **{field: ["./covered.txt"]})
            self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_wc_metadata_only_no_file_list_reads_or_execution(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        self.write(['wc "./config file.txt"; echo "$MODE"', 'wc --files0-from covered.txt'],
                   status=["touch NEVER"])
        original_open = Path.open
        opened = []

        def document_only(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate/file-list contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", document_only), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        task = report["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 1]},
                         "reason": "wc-options-not-modeled"}])
        self.assertFalse((self.root / "NEVER").exists())

    def test_head_literal_operands_normalized_locations_redaction_and_roundtrip(self):
        (self.root / "ignored.txt").write_text("synthetic-ignored-secret")
        command = 'head "./config file.txt"; head -- "config file.txt"; echo "$MODE"'
        self.write([command], sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        loc = {"path": ["tasks", "build", "cmds", 0]}
        self.assertEqual(task["sources"], [{"path": "config file.txt", "locations": [loc]}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [loc]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-file-secret", "synthetic-env-secret", "synthetic-ignored-secret"):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(text.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1])["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(task["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["config file.txt"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(reviewed["tasks"]["build"], {"sources": [], "env": [], "advisories": [],
                         "uncertain": [], "unassessed_findings": [], "yaml_fragment": None})
        self.assertEqual(declarations["sources"], ["covered.txt", "config file.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_head_literal_glob_coverage_shorthand_and_multiple_operands(self):
        for pattern in ("config file.txt", "config*.txt"):
            with self.subTest(pattern=pattern):
                self.write(['head "./config file.txt"; head -- "config file.txt"'], sources=[pattern])
                task = self.report("--strict")["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["uncertain"], [])
                self.assertIsNone(task["yaml_fragment"])
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": r"head ./config\ file.txt; head 'config file.txt'",
            "list": ["head -- './config file.txt' covered.txt", "head 'config file.txt'"]}}))
        tasks = self.report()["tasks"]
        self.assertEqual(tasks["string"]["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "string"]}]}])
        self.assertEqual(tasks["list"]["sources"], [
            {"path": "config file.txt", "locations": [{"path": ["tasks", "list", 0]},
                                                       {"path": ["tasks", "list", 1]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "list", 0]}]}])

    def test_head_options_withhold_entire_command_without_option_value_inference(self):
        (self.root / "ignored.txt").write_text("synthetic-option-output-secret")
        options = ["-n ignored.txt covered.txt", "--lines ignored.txt covered.txt",
                   "--lines=ignored.txt covered.txt", "-nignored.txt covered.txt",
                   "-c ignored.txt covered.txt", "--bytes ignored.txt covered.txt",
                   "--bytes=ignored.txt covered.txt", "-cignored.txt covered.txt",
                   "-n 10 covered.txt", "-n10 covered.txt", "-c10 covered.txt",
                   "-10 covered.txt", "-10c covered.txt", "-q covered.txt", "-v covered.txt",
                   "--quiet covered.txt", "--verbose covered.txt", "--help", "--version",
                   "covered.txt -n ignored.txt", "covered.txt --", "- -- covered.txt",
                   "-- -- covered.txt", "-- covered.txt --"]
        self.write([f'cat "config file.txt"; head {option}; echo "$MODE"' for option in options])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "head-options-not-modeled"} for i in range(len(options))])

    def test_head_dynamic_control_context_and_argument_output_exclusion(self):
        commands = ['head ./covered.txt $FILE; echo "$MODE"', '$RUNNER ./covered.txt; echo "$MODE"',
                    '"$BIN/head" ./covered.txt', 'head -- "${FILE}"', 'head $(touch NEVER)',
                    'head {{.FILE}}', 'head covered.txt | echo "$MODE"', 'head covered.txt && echo "$MODE"',
                    "sh -c 'head covered.txt; echo $MODE'", 'env head covered.txt; echo "$MODE"',
                    'head covered.txt; cat $FILE', 'head 2<covered.txt; echo "$MODE"',
                    {"cmd": "head covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        for field in ("dir", "includes"):
            doc = {"version": "3", "tasks": {"build": {"cmds": ['head covered.txt; echo "$MODE"']}}}
            if field == "dir":
                doc["tasks"]["build"]["dir"] = "elsewhere"
            else:
                doc["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(doc))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                             "reason": "unsupported-task-context"}])
        (self.root / "-l").write_text("synthetic-dash-file-secret")
        self.write(['head', 'head -', 'head -- -', 'head -- -l -', 'head ./-l',
                    'head covered.txt > "config file.txt"', 'echo "config file.txt"',
                    '/usr/bin/head "config file.txt"'])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [
            {"path": "-l", "locations": [{"path": ["tasks", "build", "cmds", 3]},
                                           {"path": ["tasks", "build", "cmds", 4]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "build", "cmds", 5]}]}])
        self.assertEqual(task["uncertain"], [])

    def test_head_unsafe_paths_and_strict_declarations(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", "", "*.txt"]
        self.write([f"head '{path}'" for path in paths], sources=["covered.txt"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "source-path-not-safe-regular-literal"} for i in range(len(paths))])
        for field in ("sources", "generates"):
            for pattern in ("./covered.txt", "dir/./file", "dir//file", "dir/../covered.txt"):
                with self.subTest(field=field, pattern=pattern):
                    self.write(["head ./covered.txt"], **{field: [pattern]})
                    self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_head_metadata_only_no_option_value_reads_or_execution(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        (self.root / "ignored.txt").write_text("synthetic-option-secret")
        self.write(['head "./config file.txt" > ignored.txt; echo "$MODE"', 'head -n ignored.txt covered.txt'],
                   status=["touch NEVER"])
        original_open, original_lstat = Path.open, Path.lstat
        opened, inspected = [], []

        def document_only(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate/option-output contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        def candidate_only(path, *args, **kwargs):
            self.assertEqual(path, self.root / "config file.txt", "option/output metadata must not be inspected")
            inspected.append(path)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "open", document_only), \
                patch.object(Path, "lstat", candidate_only), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        self.assertEqual(inspected, [self.root / "config file.txt"])
        task = report["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 1]},
                         "reason": "head-options-not-modeled"}])
        self.assertFalse((self.root / "NEVER").exists())

    def test_cmp_pair_normalized_locations_redaction_and_roundtrip(self):
        for name in ("left file.txt", "right.txt", "ignored.txt"):
            (self.root / name).write_text("synthetic-pair-secret")
        command = 'cmp "./left file.txt" right.txt; cmp -- "left file.txt" ./right.txt; echo "$MODE"'
        self.write([command], sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        loc = {"path": ["tasks", "build", "cmds", 0]}
        self.assertEqual(task["sources"], [{"path": name, "locations": [loc]}
                         for name in ("left file.txt", "right.txt")])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [loc]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-pair-secret", "synthetic-env-secret", "ignored.txt", command):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(text.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1])["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(task["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["left file.txt", "right.txt"],
                                    "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(reviewed["tasks"]["build"], {"sources": [], "env": [], "advisories": [],
                         "uncertain": [], "unassessed_findings": [], "yaml_fragment": None})
        self.assertEqual(declarations["sources"], ["covered.txt", "left file.txt", "right.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_cmp_coverage_shorthand_stdin_and_dash_literal_pairs(self):
        for patterns in (["config file.txt", "covered.txt"], ["config*.txt", "covered.*"]):
            with self.subTest(patterns=patterns):
                self.write(['cmp "./config file.txt" ./covered.txt'], sources=patterns)
                task = self.report("--strict")["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["uncertain"], [])
                self.assertIsNone(task["yaml_fragment"])
        (self.root / "-n").write_text("synthetic-dash-secret")
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": r"cmp ./config\ file.txt covered.txt; cmp 'config file.txt' ./covered.txt",
            "list": ['cmp -- "./config file.txt" covered.txt', 'cmp "config file.txt" covered.txt'],
            "stdin": ['cmp - -', 'cmp -- - -', 'cmp - "./config file.txt"',
                      'cmp "config file.txt" -', 'cmp -- - "config file.txt"',
                      'cmp -- "config file.txt" -'],
            "dash": ['cmp -- -n -', 'cmp -- - -n', 'cmp ./-n ./-n'],
            "outside": ['echo "config file.txt"', '/usr/bin/cmp "config file.txt" covered.txt',
                        'mycmp "config file.txt" covered.txt']}}))
        tasks = self.report()["tasks"]
        for name, locs in (("string", [["tasks", "string"]]),
                           ("list", [["tasks", "list", 0], ["tasks", "list", 1]])):
            self.assertEqual(tasks[name]["sources"], [{"path": path,
                             "locations": [{"path": loc} for loc in locs]}
                             for path in ("config file.txt", "covered.txt")])
        self.assertEqual(tasks["stdin"]["sources"], [{"path": "config file.txt", "locations": [
            {"path": ["tasks", "stdin", i]} for i in range(2, 6)]}])
        self.assertEqual(tasks["dash"]["sources"], [{"path": "-n", "locations": [
            {"path": ["tasks", "dash", i]} for i in range(3)]}])
        self.assertEqual(tasks["outside"]["sources"], [])
        self.assertTrue(all(task["uncertain"] == [] for task in tasks.values()))

    def test_cmp_options_and_other_arities_withhold_entire_command(self):
        (self.root / "ignored.txt").write_text("synthetic-option-secret")
        options = ["-n ignored.txt covered.txt", "-nignored.txt covered.txt",
                   "--bytes ignored.txt covered.txt", "--bytes=ignored.txt covered.txt",
                   "-i ignored.txt covered.txt", "-iignored.txt covered.txt",
                   "--ignore-initial ignored.txt covered.txt", "--ignore-initial=ignored.txt covered.txt",
                   "-s covered.txt ignored.txt", "--silent covered.txt ignored.txt",
                   "--quiet covered.txt ignored.txt", "-l covered.txt ignored.txt",
                   "--verbose covered.txt ignored.txt", "-b covered.txt ignored.txt",
                   "--print-bytes covered.txt ignored.txt", "--help", "--version",
                   "covered.txt -n ignored.txt", "covered.txt --", "- --",
                   "-- -- covered.txt", "-- covered.txt --"]
        operands = ["", "--", "covered.txt", "-- covered.txt", "-", "-- -",
                    "covered.txt ignored.txt 10", "-- covered.txt ignored.txt 10 20"]
        self.write([f'cat "config file.txt"; cmp {arg}; echo "$MODE"' for arg in options + operands])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "cmp-options-not-modeled" if i < len(options)
                         else "cmp-operand-count-not-modeled"} for i in range(len(options + operands))])

    def test_cmp_dynamic_control_context_and_argument_output_exclusion(self):
        commands = ['cat covered.txt; cmp $FILE covered.txt; echo "$MODE"',
                    'cmp covered.txt "$FILE"; echo "$MODE"', 'cmp -- - ./$FILE; echo "$MODE"',
                    '$RUNNER covered.txt covered.txt; echo "$MODE"', '"$BIN/cmp" covered.txt covered.txt',
                    'cmp $(touch NEVER) covered.txt', 'cmp {{.FILE}} covered.txt',
                    'cmp covered.txt covered.txt | echo "$MODE"',
                    'cmp covered.txt covered.txt && echo "$MODE"',
                    "sh -c 'cmp covered.txt covered.txt; echo $MODE'",
                    'env cmp covered.txt covered.txt; echo "$MODE"',
                    'sudo cmp covered.txt covered.txt', 'cmp covered.txt covered.txt; cat $FILE',
                    'cmp - covered.txt 2<covered.txt; echo "$MODE"',
                    {"cmd": "cmp covered.txt covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        self.assertEqual([u["reason"] for u in task["uncertain"][:3]], ["dynamic-source-operand"] * 3)
        self.assertFalse((self.root / "NEVER").exists())
        for field in ("dir", "includes"):
            doc = {"version": "3", "tasks": {"build": {"cmds": ['cmp covered.txt covered.txt; echo "$MODE"']}}}
            if field == "dir":
                doc["tasks"]["build"]["dir"] = "elsewhere"
            else:
                doc["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(doc))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                             "reason": "unsupported-task-context"}])
        self.write(['cmp covered.txt covered.txt > "config file.txt"', 'echo "config file.txt"'])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": "covered.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        self.assertEqual(task["uncertain"], [])

    def test_cmp_unsafe_paths_in_either_position_and_strict_declarations(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", "", "*.txt"]
        commands = [command for path in paths for command in
                    (f"cmp '{path}' covered.txt", f"cmp covered.txt '{path}'")]
        self.write(commands, sources=["covered.txt"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "source-path-not-safe-regular-literal"} for i in range(len(commands))])
        # A rejected path does not invalidate a separate safe literal partner.
        self.write(['cmp ./missing "./config file.txt"'])
        self.assertEqual(self.report()["tasks"]["build"]["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        for field in ("sources", "generates"):
            for pattern in ("./covered.txt", "dir/./file", "dir//file", "dir/../covered.txt"):
                with self.subTest(field=field, pattern=pattern):
                    self.write(["cmp ./covered.txt ./covered.txt"], **{field: [pattern]})
                    self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_cmp_metadata_only_no_option_output_inspection_or_execution(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        (self.root / "ignored.txt").write_text("synthetic-option-output-secret")
        self.write(['cmp "./config file.txt" covered.txt > ignored.txt; echo "$MODE"',
                    'cmp -n ignored.txt covered.txt', 'cmp ignored.txt',
                    'cmp covered.txt "$FILE"'], status=["touch NEVER"])
        original_open, original_lstat = Path.open, Path.lstat
        opened, inspected = [], []

        def document_only(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate/option/output contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        def candidate_only(path, *args, **kwargs):
            self.assertIn(path, [self.root / "config file.txt", self.root / "covered.txt"],
                          "option/output metadata must not be inspected")
            inspected.append(path)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "open", document_only), \
                patch.object(Path, "lstat", candidate_only), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        self.assertEqual(inspected, [self.root / "config file.txt", self.root / "covered.txt"])
        task = report["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": name,
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}
                         for name in ("config file.txt", "covered.txt")])
        self.assertEqual([u["reason"] for u in task["uncertain"]],
                         ["cmp-options-not-modeled", "cmp-operand-count-not-modeled", "dynamic-source-operand"])
        self.assertFalse((self.root / "NEVER").exists())

    def test_comm_pair_normalized_locations_redaction_and_roundtrip(self):
        for name in ("left file.txt", "right.txt", "ignored.txt"):
            (self.root / name).write_text("synthetic-pair-secret")
        command = 'comm "./left file.txt" right.txt; comm -- "left file.txt" ./right.txt; echo "$MODE"'
        self.write([command], sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        loc = {"path": ["tasks", "build", "cmds", 0]}
        self.assertEqual(task["sources"], [{"path": name, "locations": [loc]}
                         for name in ("left file.txt", "right.txt")])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [loc]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-pair-secret", "synthetic-env-secret", "ignored.txt", command):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(text.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1])["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(task["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["left file.txt", "right.txt"],
                                    "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(reviewed["tasks"]["build"], {"sources": [], "env": [], "advisories": [],
                         "uncertain": [], "unassessed_findings": [], "yaml_fragment": None})
        self.assertEqual(declarations["sources"], ["covered.txt", "left file.txt", "right.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_comm_coverage_shorthand_stdin_and_dash_literal_pairs(self):
        for patterns in (["config file.txt", "covered.txt"], ["config*.txt", "covered.*"]):
            with self.subTest(patterns=patterns):
                self.write(['comm "./config file.txt" ./covered.txt'], sources=patterns)
                task = self.report("--strict")["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["uncertain"], [])
                self.assertIsNone(task["yaml_fragment"])
        (self.root / "-1").write_text("synthetic-dash-secret")
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": r"comm ./config\ file.txt covered.txt; comm 'config file.txt' ./covered.txt",
            "list": ['comm -- "./config file.txt" covered.txt', 'comm "config file.txt" covered.txt'],
            "stdin": ['comm - -', 'comm -- - -', 'comm - "./config file.txt"',
                      'comm "config file.txt" -', 'comm -- - "config file.txt"',
                      'comm -- "config file.txt" -'],
            "dash": ['comm -- -1 -', 'comm -- - -1', 'comm ./-1 ./-1'],
            "outside": ['echo "config file.txt"', '/usr/bin/comm "config file.txt" covered.txt',
                        'mycomm "config file.txt" covered.txt']}}))
        tasks = self.report()["tasks"]
        for name, locs in (("string", [["tasks", "string"]]),
                           ("list", [["tasks", "list", 0], ["tasks", "list", 1]])):
            self.assertEqual(tasks[name]["sources"], [{"path": path,
                             "locations": [{"path": loc} for loc in locs]}
                             for path in ("config file.txt", "covered.txt")])
        self.assertEqual(tasks["stdin"]["sources"], [{"path": "config file.txt", "locations": [
            {"path": ["tasks", "stdin", i]} for i in range(2, 6)]}])
        self.assertEqual(tasks["dash"]["sources"], [{"path": "-1", "locations": [
            {"path": ["tasks", "dash", i]} for i in range(3)]}])
        self.assertEqual(tasks["outside"]["sources"], [])
        self.assertTrue(all(task["uncertain"] == [] for task in tasks.values()))

    def test_comm_options_and_other_arities_withhold_entire_command(self):
        (self.root / "ignored.txt").write_text("synthetic-option-secret")
        options = ["-1 covered.txt ignored.txt", "-2 covered.txt ignored.txt",
                   "-3 covered.txt ignored.txt", "-12 covered.txt ignored.txt",
                   "-123 covered.txt ignored.txt", "-1ignored.txt covered.txt",
                   "--check-order covered.txt ignored.txt", "--check-order=ignored.txt covered.txt",
                   "--nocheck-order covered.txt ignored.txt", "--nocheck-order=ignored.txt covered.txt",
                   "--output-delimiter ignored.txt covered.txt right.txt",
                   "--output-delimiter=ignored.txt covered.txt", "-z covered.txt ignored.txt",
                   "--zero-terminated covered.txt ignored.txt", "--total covered.txt ignored.txt",
                   "--help", "--version", "covered.txt -1 ignored.txt", "covered.txt --", "- --",
                   "-- -- covered.txt", "-- covered.txt --"]
        operands = ["", "--", "covered.txt", "-- covered.txt", "-", "-- -",
                    "covered.txt ignored.txt extra", "-- covered.txt ignored.txt extra extra"]
        self.write([f'cat "config file.txt"; comm {arg}; echo "$MODE"' for arg in options + operands])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "comm-options-not-modeled" if i < len(options)
                         else "comm-operand-count-not-modeled"} for i in range(len(options + operands))])

    def test_comm_dynamic_control_context_and_argument_output_exclusion(self):
        commands = ['cat covered.txt; comm $FILE covered.txt; echo "$MODE"',
                    'comm covered.txt "$FILE"; echo "$MODE"', 'comm -- - ./$FILE; echo "$MODE"',
                    'cat covered.txt; comm "${FILE}-" covered.txt; echo "$MODE"',
                    'comm covered.txt "-${FILE}"; echo "$MODE"',
                    'comm "${FILE}--" covered.txt covered.txt; echo "$MODE"',
                    'comm -- "${FILE}-" covered.txt; echo "$MODE"',
                    '$RUNNER covered.txt covered.txt; echo "$MODE"', '"$BIN/comm" covered.txt covered.txt',
                    'comm $(touch NEVER) covered.txt', 'comm {{.FILE}} covered.txt',
                    'comm covered.txt covered.txt | echo "$MODE"',
                    'comm covered.txt covered.txt && echo "$MODE"',
                    "sh -c 'comm covered.txt covered.txt; echo $MODE'",
                    'env comm covered.txt covered.txt; echo "$MODE"',
                    'sudo comm covered.txt covered.txt', 'comm covered.txt covered.txt; cat $FILE',
                    'comm - covered.txt 2<covered.txt; echo "$MODE"',
                    {"cmd": "comm covered.txt covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        self.assertEqual([u["reason"] for u in task["uncertain"][:3]], ["dynamic-source-operand"] * 3)
        self.assertFalse((self.root / "NEVER").exists())
        for field in ("dir", "includes"):
            doc = {"version": "3", "tasks": {"build": {"cmds": ['comm covered.txt covered.txt; echo "$MODE"']}}}
            if field == "dir":
                doc["tasks"]["build"]["dir"] = "elsewhere"
            else:
                doc["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(doc))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                             "reason": "unsupported-task-context"}])
        self.write(['comm covered.txt covered.txt > "config file.txt"', 'echo "config file.txt"'])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": "covered.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        self.assertEqual(task["uncertain"], [])

    def test_comm_unsafe_paths_in_either_position_and_strict_declarations(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", "", "*.txt"]
        commands = [command for path in paths for command in
                    (f"comm '{path}' covered.txt", f"comm covered.txt '{path}'")]
        self.write(commands, sources=["covered.txt"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "source-path-not-safe-regular-literal"} for i in range(len(commands))])
        # A rejected path does not invalidate a separate safe literal partner.
        self.write(['comm ./missing "./config file.txt"'])
        self.assertEqual(self.report()["tasks"]["build"]["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        for field in ("sources", "generates"):
            for pattern in ("./covered.txt", "dir/./file", "dir//file", "dir/../covered.txt"):
                with self.subTest(field=field, pattern=pattern):
                    self.write(["comm ./covered.txt ./covered.txt"], **{field: [pattern]})
                    self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_comm_metadata_only_no_option_output_inspection_or_execution(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        (self.root / "ignored.txt").write_text("synthetic-option-output-secret")
        self.write(['comm "./config file.txt" covered.txt > ignored.txt; echo "$MODE"',
                    'comm --output-delimiter=ignored.txt covered.txt', 'comm ignored.txt',
                    'comm covered.txt "$FILE"'], status=["touch NEVER"])
        original_open, original_lstat = Path.open, Path.lstat
        opened, inspected = [], []

        def document_only(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate/option/output contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        def candidate_only(path, *args, **kwargs):
            self.assertIn(path, [self.root / "config file.txt", self.root / "covered.txt"],
                          "option/output metadata must not be inspected")
            inspected.append(path)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "open", document_only), \
                patch.object(Path, "lstat", candidate_only), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        self.assertEqual(inspected, [self.root / "config file.txt", self.root / "covered.txt"])
        task = report["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": name,
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}
                         for name in ("config file.txt", "covered.txt")])
        self.assertEqual([u["reason"] for u in task["uncertain"]],
                         ["comm-options-not-modeled", "comm-operand-count-not-modeled", "dynamic-source-operand"])
        self.assertFalse((self.root / "NEVER").exists())

    def test_tail_literal_operands_normalized_locations_redaction_and_roundtrip(self):
        (self.root / "ignored.txt").write_text("synthetic-ignored-secret")
        command = 'tail "./config file.txt"; tail -- "config file.txt"; echo "$MODE"'
        self.write([command], sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        loc = {"path": ["tasks", "build", "cmds", 0]}
        self.assertEqual(task["sources"], [{"path": "config file.txt", "locations": [loc]}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [loc]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-file-secret", "synthetic-env-secret", "synthetic-ignored-secret"):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(text.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1])["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(task["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["config file.txt"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(reviewed["tasks"]["build"], {"sources": [], "env": [], "advisories": [],
                         "uncertain": [], "unassessed_findings": [], "yaml_fragment": None})
        self.assertEqual(declarations["sources"], ["covered.txt", "config file.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_tail_literal_glob_coverage_shorthand_and_multiple_operands(self):
        for pattern in ("config file.txt", "config*.txt"):
            with self.subTest(pattern=pattern):
                self.write(['tail "./config file.txt"; tail -- "config file.txt"'], sources=[pattern])
                task = self.report("--strict")["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["uncertain"], [])
                self.assertIsNone(task["yaml_fragment"])
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": r"tail ./config\ file.txt; tail 'config file.txt'",
            "list": ["tail -- './config file.txt' covered.txt", "tail 'config file.txt'"]}}))
        tasks = self.report()["tasks"]
        self.assertEqual(tasks["string"]["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "string"]}]}])
        self.assertEqual(tasks["list"]["sources"], [
            {"path": "config file.txt", "locations": [{"path": ["tasks", "list", 0]},
                                                       {"path": ["tasks", "list", 1]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "list", 0]}]}])

    def test_tail_options_withhold_entire_command_without_option_value_inference(self):
        (self.root / "ignored.txt").write_text("synthetic-option-output-secret")
        options = ["-n ignored.txt covered.txt", "--lines ignored.txt covered.txt",
                   "--lines=ignored.txt covered.txt", "-nignored.txt covered.txt",
                   "-c ignored.txt covered.txt", "--bytes ignored.txt covered.txt",
                   "--bytes=ignored.txt covered.txt", "-cignored.txt covered.txt",
                   "-n 10 covered.txt", "-n10 covered.txt", "-c10 covered.txt",
                   "-10 covered.txt", "-10c covered.txt", "+10 covered.txt", "+10c covered.txt",
                   "+c covered.txt", "covered.txt +10", "'+10' covered.txt",
                   "-f covered.txt", "-F covered.txt", "--follow covered.txt",
                   "--follow=name covered.txt", "--follow=descriptor covered.txt",
                   "--retry covered.txt", "--pid ignored.txt covered.txt",
                   "--pid=ignored.txt covered.txt", "-s ignored.txt covered.txt", "-q covered.txt", "-v covered.txt",
                   "--quiet covered.txt", "--verbose covered.txt", "--help", "--version",
                   "covered.txt -n ignored.txt", "covered.txt --", "- -- covered.txt",
                   "-- -- covered.txt", "-- covered.txt --"]
        self.write([f'cat "config file.txt"; tail {option}; echo "$MODE"' for option in options])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "tail-options-not-modeled"} for i in range(len(options))])

    def test_tail_dynamic_control_context_and_argument_output_exclusion(self):
        commands = ['tail ./covered.txt $FILE; echo "$MODE"', '$RUNNER ./covered.txt; echo "$MODE"',
                    '"$BIN/tail" ./covered.txt', 'tail -- "${FILE}"', 'tail $(touch NEVER)',
                    'tail {{.FILE}}', 'tail covered.txt | echo "$MODE"', 'tail covered.txt && echo "$MODE"',
                    "sh -c 'tail covered.txt; echo $MODE'", 'env tail covered.txt; echo "$MODE"',
                    'tail covered.txt; cat $FILE', 'tail 2<covered.txt; echo "$MODE"',
                    {"cmd": "tail covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        for field in ("dir", "includes"):
            doc = {"version": "3", "tasks": {"build": {"cmds": ['tail covered.txt; echo "$MODE"']}}}
            if field == "dir":
                doc["tasks"]["build"]["dir"] = "elsewhere"
            else:
                doc["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(doc))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                             "reason": "unsupported-task-context"}])
        for name in ("-l", "+10", "+name"):
            (self.root / name).write_text("synthetic-signed-file-secret")
        self.write(['tail', 'tail --', 'tail -', 'tail -- -', 'tail -- -l +10 +name -',
                    'tail ./-l ./+10 ./+name', 'tail covered.txt > "config file.txt"',
                    'echo "config file.txt"', '/usr/bin/tail "config file.txt"',
                    'mytail "config file.txt"'])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [
            {"path": name, "locations": [{"path": ["tasks", "build", "cmds", 4]},
                                          {"path": ["tasks", "build", "cmds", 5]}]}
            for name in ("+10", "+name", "-l")] + [
            {"path": "covered.txt", "locations": [{"path": ["tasks", "build", "cmds", 6]}]}])
        self.assertEqual(task["uncertain"], [])

    def test_tail_unsafe_paths_and_strict_declarations(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", "", "*.txt"]
        self.write([f"tail '{path}'" for path in paths], sources=["covered.txt"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "source-path-not-safe-regular-literal"} for i in range(len(paths))])
        for field in ("sources", "generates"):
            for pattern in ("./covered.txt", "dir/./file", "dir//file", "dir/../covered.txt"):
                with self.subTest(field=field, pattern=pattern):
                    self.write(["tail ./covered.txt"], **{field: [pattern]})
                    self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_tail_metadata_only_no_option_value_reads_or_execution(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        (self.root / "ignored.txt").write_text("synthetic-option-secret")
        self.write(['tail "./config file.txt" > ignored.txt; echo "$MODE"',
                    'tail -n ignored.txt covered.txt', 'tail +10 ignored.txt',
                    'tail --follow ignored.txt'],
                   status=["touch NEVER"])
        original_open, original_lstat = Path.open, Path.lstat
        opened, inspected = [], []

        def document_only(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate/option-output contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        def candidate_only(path, *args, **kwargs):
            self.assertEqual(path, self.root / "config file.txt", "option/output metadata must not be inspected")
            inspected.append(path)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "open", document_only), \
                patch.object(Path, "lstat", candidate_only), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        self.assertEqual(inspected, [self.root / "config file.txt"])
        task = report["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "tail-options-not-modeled"} for i in (1, 2, 3)])
        self.assertFalse((self.root / "NEVER").exists())

    def test_sort_literal_operands_normalized_locations_redaction_and_roundtrip(self):
        (self.root / "ignored.txt").write_text("synthetic-ignored-secret")
        command = 'sort "./config file.txt"; sort -- "config file.txt"; echo "$MODE"'
        self.write([command], sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        original = self.taskfile.read_bytes()
        result = self.run_cli("--json", "--strict", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        loc = {"path": ["tasks", "build", "cmds", 0]}
        self.assertEqual(task["sources"], [{"path": "config file.txt", "locations": [loc]}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [loc]}])
        self.assertEqual(task["uncertain"], [])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", "--strict", code=1).stdout)
        text = self.run_cli("--strict", code=1).stdout
        self.assertEqual(text, self.run_cli("--strict", code=1).stdout)
        for secret in ("synthetic-file-secret", "synthetic-env-secret", "synthetic-ignored-secret"):
            self.assertNotIn(secret, result.stdout + text)
        self.assertEqual(original, self.taskfile.read_bytes())
        fragment = yaml.safe_load(text.split(
            "  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1])["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(task["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["config file.txt"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        declarations = data["tasks"]["build"]
        declarations["sources"] += fragment["sources"]
        declarations["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        reviewed = self.report("--strict")
        self.assertFalse(reviewed["dependency_graph_complete"])
        self.assertEqual(reviewed["tasks"]["build"], {"sources": [], "env": [], "advisories": [],
                         "uncertain": [], "unassessed_findings": [], "yaml_fragment": None})
        self.assertEqual(declarations["sources"], ["covered.txt", "config file.txt"])
        self.assertEqual(declarations["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_sort_literal_glob_coverage_shorthand_and_multiple_operands(self):
        for pattern in ("config file.txt", "config*.txt"):
            with self.subTest(pattern=pattern):
                self.write(['sort "./config file.txt"; sort -- "config file.txt"'], sources=[pattern])
                task = self.report("--strict")["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["uncertain"], [])
                self.assertIsNone(task["yaml_fragment"])
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": r"sort ./config\ file.txt; sort 'config file.txt'",
            "list": ["sort -- './config file.txt' covered.txt", "sort 'config file.txt'"]}}))
        tasks = self.report()["tasks"]
        self.assertEqual(tasks["string"]["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "string"]}]}])
        self.assertEqual(tasks["list"]["sources"], [
            {"path": "config file.txt", "locations": [{"path": ["tasks", "list", 0]},
                                                       {"path": ["tasks", "list", 1]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "list", 0]}]}])

    def test_sort_options_withhold_entire_command_without_option_output_inference(self):
        (self.root / "ignored.txt").write_text("synthetic-option-output-secret")
        options = ["-o ignored.txt covered.txt", "--output ignored.txt covered.txt",
                   "--output=ignored.txt covered.txt", "-oignored.txt covered.txt",
                   "-k covered.txt", "--key=covered.txt", "-k1,2 covered.txt",
                   "-t ignored.txt covered.txt", "--field-separator=ignored.txt covered.txt",
                   "-T ignored.txt covered.txt", "--temporary-directory=ignored.txt covered.txt",
                   "--files0-from ignored.txt", "--files0-from=ignored.txt", "-n covered.txt",
                   "-r covered.txt", "--help", "--version", "covered.txt -o ignored.txt",
                   "covered.txt --", "-- -- covered.txt", "-- covered.txt --"]
        self.write([f'cat "config file.txt"; sort {option}; echo "$MODE"' for option in options])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "sort-options-not-modeled"} for i in range(len(options))])

    def test_sort_dynamic_control_context_and_argument_output_exclusion(self):
        commands = ['sort ./covered.txt $FILE; echo "$MODE"', '$RUNNER ./covered.txt; echo "$MODE"',
                    '"$BIN/sort" ./covered.txt', 'sort -- "${FILE}"', 'sort $(touch NEVER)',
                    'sort {{.FILE}}', 'sort covered.txt | echo "$MODE"', 'sort covered.txt && echo "$MODE"',
                    "sh -c 'sort covered.txt; echo $MODE'", 'env sort covered.txt; echo "$MODE"',
                    'sort covered.txt; cat $FILE', 'sort 2<covered.txt; echo "$MODE"',
                    {"cmd": "sort covered.txt", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]], list(range(len(commands))))
        for field in ("dir", "includes"):
            doc = {"version": "3", "tasks": {"build": {"cmds": ['sort covered.txt; echo "$MODE"']}}}
            if field == "dir":
                doc["tasks"]["build"]["dir"] = "elsewhere"
            else:
                doc["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(doc))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                             "reason": "unsupported-task-context"}])
        (self.root / "-l").write_text("synthetic-dash-file-secret")
        self.write(['sort', 'sort -', 'sort -- -', 'sort -- -l', 'sort ./-l',
                    'sort covered.txt > "config file.txt"', 'echo "config file.txt"',
                    '/usr/bin/sort "config file.txt"'])
        task = self.report()["tasks"]["build"]
        self.assertEqual(task["sources"], [
            {"path": "-l", "locations": [{"path": ["tasks", "build", "cmds", 3]},
                                           {"path": ["tasks", "build", "cmds", 4]}]},
            {"path": "covered.txt", "locations": [{"path": ["tasks", "build", "cmds", 5]}]}])
        self.assertEqual(task["uncertain"], [])

    def test_sort_unsafe_paths_and_strict_declarations(self):
        (self.root / "alias").symlink_to(self.root / "covered.txt")
        (self.root / "linkdir").symlink_to(self.root, target_is_directory=True)
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        paths = ["././covered.txt", ".//covered.txt", "./dir/../covered.txt", "./dir/./file",
                 "./dir//file", "./../covered.txt", "../covered.txt", str(self.root / "covered.txt"),
                 "./alias", "./linkdir/covered.txt", "./pipe", "./dir", "./missing", "./", "", "*.txt"]
        self.write([f"sort '{path}'" for path in paths], sources=["covered.txt"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", i]},
                         "reason": "source-path-not-safe-regular-literal"} for i in range(len(paths))])
        for field in ("sources", "generates"):
            self.write(["sort ./covered.txt"], **{field: ["./covered.txt"]})
            self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_sort_metadata_only_no_option_output_reads_or_execution(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        (self.root / "ignored.txt").write_text("synthetic-option-secret")
        self.write(['sort "./config file.txt"; echo "$MODE"', 'sort -o covered.txt ignored.txt'],
                   status=["touch NEVER"])
        original_open = Path.open
        opened = []

        def document_only(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "candidate/option-output contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", document_only), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution attempted")):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        task = report["tasks"]["build"]
        self.assertEqual(task["sources"], [{"path": "config file.txt",
                         "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])
        self.assertEqual(task["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 1]},
                         "reason": "sort-options-not-modeled"}])
        self.assertFalse((self.root / "NEVER").exists())

    def python_fixture(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "build file.py").write_text(
            "from pathlib import Path\nimport hidden_module\n"
            "Path('NEVER').touch()\nPath('transitive.txt').read_text()\n# synthetic-script-secret\n")
        (self.root / "config.json").write_text("synthetic-argument-secret")

    def test_python_literal_script_locations_arguments_and_nonexecution(self):
        self.python_fixture()
        commands = ["python3 'scripts/build file.py' config.json \"$MODE\"",
                    "/usr/bin/python3.12 -- 'scripts/build file.py'",
                    "python scripts/build\\ file.py; python3.13.1 'scripts/build file.py'"]
        self.write(commands)
        original = self.taskfile.read_bytes()
        result = self.run_cli("--strict", "--json", code=1)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        locations = [{"path": ["tasks", "build", "cmds", i]} for i in range(3)]
        self.assertEqual(task["sources"], [{"path": "scripts/build file.py", "locations": locations}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": locations[:1]}])
        self.assertEqual(task["uncertain"], [
            {"location": location, "reason": "script-transitive-inputs-not-modeled"}
            for location in locations])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--strict", "--json", code=1).stdout)
        self.assertEqual(original, self.taskfile.read_bytes())
        self.assertFalse((self.root / "NEVER").exists())
        for secret in ("synthetic-script-secret", "synthetic-argument-secret", "hidden_module", "transitive.txt"):
            self.assertNotIn(secret, result.stdout)

    def test_python_fragment_roundtrip_preserves_declarations_and_uncertainty(self):
        self.python_fixture()
        self.write(["python3 'scripts/build file.py' \"$MODE\""],
                   sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        before = self.report("--strict", code=1)["tasks"]["build"]
        text = self.run_cli().stdout
        self.assertIn("script-transitive-inputs-not-modeled", text)
        fragment_text = text.split("  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1]
        fragment = yaml.safe_load(fragment_text)["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(before["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["scripts/build file.py"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        task = data["tasks"]["build"]
        task["sources"] += fragment["sources"]
        task["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        after = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(after["sources"], [])
        self.assertEqual(after["env"], [])
        self.assertEqual(after["advisories"], [])
        self.assertIsNone(after["yaml_fragment"])
        self.assertEqual(after["uncertain"], before["uncertain"])
        self.assertEqual(task["sources"], ["covered.txt", "scripts/build file.py"])
        self.assertEqual(task["env"]["KEEP"], "synthetic-env-secret")

    def test_python_unsupported_modes_and_dynamic_forms_withhold_entire_command(self):
        self.python_fixture()
        for command in [
            "python3 -c 'import os' $MODE", "python3 -m build $MODE",
            "python3 -I 'scripts/build file.py' $MODE", "python3 -W ignore 'scripts/build file.py'",
            "python3 -", "python3", "python3 --", "python3 -- -",
            "python3 $SCRIPT $MODE", "python3 \"${SCRIPT}\"", "$RUNNER 'scripts/build file.py'",
            '"$BIN/python3" \'scripts/build file.py\'', "python${VERSION} 'scripts/build file.py'",
            "python3 $(echo script.py)", "python3 {{.SCRIPT}}",
            "python3 'scripts/build file.py'; cat $OTHER",
            "python3 'scripts/build file.py' | cat covered.txt",
            "env python3 'scripts/build file.py'", "ruby 'scripts/build file.py'",
        ]:
            with self.subTest(command=command):
                self.write([command])
                task = self.report("--strict", code=1)["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["env"], [])
                self.assertEqual(task["advisories"], [])
                self.assertIsNone(task["yaml_fragment"])
                self.assertTrue(task["uncertain"])

    def test_python_safe_path_and_task_context_boundaries(self):
        self.python_fixture()
        (self.root / "alias.py").symlink_to(self.root / "scripts" / "build file.py")
        (self.root / "linkdir").symlink_to(self.root / "scripts", target_is_directory=True)
        os.mkfifo(self.root / "pipe.py")
        for path in ["missing.py", "alias.py", "linkdir/build file.py", "scripts", "pipe.py",
                     "../outside.py", "/etc/passwd", "", "scripts/*.py"]:
            with self.subTest(path=path):
                self.write(["python3 " + "'" + path + "'"])
                task = self.report("--strict", code=1)["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertTrue(task["uncertain"])
        self.write(["python3 'scripts/build file.py' $MODE"], dir="elsewhere")
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertTrue(task["unassessed_findings"])

    def test_python_covered_globs_and_shorthand_locations(self):
        self.python_fixture()
        self.write(["python3 'scripts/build file.py'"], sources=["scripts/*.py"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["advisories"], [])
        self.assertEqual([item["reason"] for item in task["uncertain"]],
                         ["script-transitive-inputs-not-modeled"])
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": "python3 'scripts/build file.py'", "list": ["python 'scripts/build file.py'"]}}))
        tasks = self.report("--strict", code=1)["tasks"]
        for name, location in [("string", ["tasks", "string"]), ("list", ["tasks", "list", 0])]:
            self.assertEqual(tasks[name]["sources"][0]["locations"], [{"path": location}])
            self.assertEqual(tasks[name]["uncertain"][0]["location"], {"path": location})


    def leading_dot_fixture(self):
        self.node_fixture()
        (self.root / "scripts" / "build.py").write_text(
            "from pathlib import Path; Path('NEVER').touch() # synthetic-python-secret\n")

    def test_leading_dot_script_normalization_locations_dedup_and_redaction(self):
        self.leading_dot_fixture()
        self.write(['node ./scripts/build.js "$MODE" config.json',
                    'node scripts/build.js; node -- ./scripts/build.js "$MODE"',
                    "python3 './scripts/build.py' $MODE",
                    "'/usr/bin/python3.12' -- ./scripts/build.py; python scripts/build.py",
                    "node './scripts/build file' $MODE"])
        original = self.taskfile.read_bytes()
        env_file = self.root / "env.json"
        env_file.write_text('{"MODE":"synthetic-snapshot-secret"}')
        args = ("--strict", "--env-snapshot", env_file)
        result = self.run_cli("--json", *args, code=1,
                              env=dict(os.environ, HOST_ONLY="synthetic-host-secret"))
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        locs = [{"path": ["tasks", "build", "cmds", i]} for i in range(5)]
        self.assertEqual(task["sources"], [
            {"path": "scripts/build file", "locations": locs[4:]},
            {"path": "scripts/build.js", "locations": locs[:2]},
            {"path": "scripts/build.py", "locations": locs[2:4]}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [locs[i] for i in (0, 1, 2, 4)]}])
        self.assertEqual(task["uncertain"], [
            {"location": loc, "reason": "script-transitive-inputs-not-modeled"} for loc in locs])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["values_redacted"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--json", *args, code=1).stdout)
        text = self.run_cli(*args, code=1).stdout
        self.assertEqual(text, self.run_cli(*args, code=1).stdout)
        for secret in ("synthetic-python-secret", "synthetic-script-secret", "synthetic-snapshot-secret",
                       "synthetic-host-secret", "HOST_ONLY", "config.json", "hidden-module", "HIDDEN"):
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, text)
        self.assertEqual(original, self.taskfile.read_bytes())
        self.assertFalse((self.root / "NEVER").exists())

    def test_leading_dot_script_declared_literals_and_globs_cover_normalized_paths(self):
        self.leading_dot_fixture()
        for sources in (["scripts/build.js", "scripts/build.py"], ["scripts/*.js", "scripts/*.py"]):
            with self.subTest(sources=sources):
                self.write(["node ./scripts/build.js", "python3 -- ./scripts/build.py"], sources=sources)
                task = self.report("--strict", code=1)["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["advisories"], [])
                self.assertIsNone(task["yaml_fragment"])
                self.assertEqual(task["uncertain"], [
                    {"location": {"path": ["tasks", "build", "cmds", i]},
                     "reason": "script-transitive-inputs-not-modeled"} for i in range(2)])

    def test_leading_dot_script_shorthand_locations(self):
        self.leading_dot_fixture()
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": "node ./scripts/build.js", "list": ["python ./scripts/build.py"]}}))
        tasks = self.report("--strict", code=1)["tasks"]
        for name, path, location in [("string", "scripts/build.js", ["tasks", "string"]),
                                     ("list", "scripts/build.py", ["tasks", "list", 0])]:
            self.assertEqual(tasks[name]["sources"], [{"path": path, "locations": [{"path": location}]}])
            self.assertEqual(tasks[name]["uncertain"], [{"location": {"path": location},
                                                       "reason": "script-transitive-inputs-not-modeled"}])

    def test_leading_dot_script_reviewed_fragment_merge_retains_uncertainty(self):
        self.leading_dot_fixture()
        for executable, script in [("node", "scripts/build.js"), ("python3", "scripts/build.py")]:
            with self.subTest(executable=executable):
                self.write([f'{executable} ./{script} "$MODE" config.json'],
                           sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
                before = self.report("--strict", code=1)["tasks"]["build"]
                text = self.run_cli().stdout
                fragment_text = text.split("  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1]
                fragment = yaml.safe_load(fragment_text)["tasks"]["build"]
                self.assertEqual(fragment, {"sources": [script], "env": {"MODE": "REVIEW_REQUIRED"}})
                self.assertEqual(fragment, yaml.safe_load(before["yaml_fragment"])["tasks"]["build"])
                data = yaml.safe_load(self.taskfile.read_text())
                task = data["tasks"]["build"]
                task["sources"] += fragment["sources"]
                task["env"].update(fragment["env"])
                self.taskfile.write_text(yaml.safe_dump(data))
                after = self.report("--strict", code=1)["tasks"]["build"]
                for key in ("sources", "env", "advisories", "unassessed_findings"):
                    self.assertEqual(after[key], [])
                self.assertIsNone(after["yaml_fragment"])
                self.assertEqual(after["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                                                      "reason": "script-transitive-inputs-not-modeled"}])
                self.assertEqual(after["uncertain"], before["uncertain"])
                self.assertEqual(task["sources"], ["covered.txt", script])
                self.assertEqual(task["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})

    def test_leading_dot_script_path_safety_and_unchanged_other_path_rules(self):
        self.leading_dot_fixture()
        (self.root / "alias").symlink_to(self.root / "scripts" / "build.js")
        (self.root / "linkdir").symlink_to(self.root / "scripts", target_is_directory=True)
        os.mkfifo(self.root / "pipe")
        paths = ["././scripts/build.js", ".//scripts/build.js", "./scripts/./build.js",
                 "./scripts//build.js", "./scripts/../scripts/build.js", "./../outside.js",
                 "../scripts/build.js", str(self.root / "scripts/build.js"),
                 "./alias", "./linkdir/build.js", "./pipe", "./scripts", "./missing", "./", ""]
        for executable in ("node", "python3"):
            with self.subTest(executable=executable):
                # Even an equivalent declared safe path must not bypass path validation.
                self.write([f"{executable} '{path}'" for path in paths], sources=["scripts/build.js"])
                task = self.report("--strict", code=1)["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["advisories"], [])
                self.assertEqual([u["location"]["path"][-1] for u in task["uncertain"]
                                  if u["reason"] == "source-path-not-safe-regular-literal"], list(range(len(paths))))
        for pattern in ("./scripts/build.js", "scripts/./build.js", "scripts//build.js"):
            with self.subTest(pattern=pattern):
                self.write(["node ./scripts/build.js"], sources=[pattern])
                self.assertEqual(self.run_cli("--json", code=2).stdout, "")

    def test_leading_dot_script_blocking_modes_and_context_remain_uncertain(self):
        self.leading_dot_fixture()
        commands = []
        for executable in ("node", "python3"):
            commands += [f'{executable} -I ./scripts/build.js "$MODE"',
                         f'{executable} -- -- ./scripts/build.js "$MODE"',
                         f'{executable} ./scripts/$SCRIPT "$MODE"',
                         f'"$BIN/{executable}" ./scripts/build.js "$MODE"',
                         f'env {executable} ./scripts/build.js "$MODE"',
                         f'sh -c \'{executable} ./scripts/build.js "$MODE"\'',
                         f'{executable} ./scripts/build.js "$MODE" && echo ok',
                         f'{executable} ./scripts/build.js "$MODE" | cat covered.txt',
                         f'{executable} ./scripts/build.js "$MODE"; cat $OTHER']
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertEqual({u["location"]["path"][-1] for u in task["uncertain"]}, set(range(len(commands))))
        for context in ("dir", "includes"):
            data = {"version": "3", "tasks": {"build": {"cmds": [
                'node ./scripts/build.js "$MODE"', 'python3 ./scripts/build.py "$MODE"']}}}
            if context == "dir":
                data["tasks"]["build"]["dir"] = "elsewhere"
            else:
                data["includes"] = {"other": "other.yml"}
            self.taskfile.write_text(yaml.safe_dump(data))
            task = self.report("--strict", code=1)["tasks"]["build"]
            self.assertEqual(task["sources"], [])
            self.assertEqual(task["env"], [])
            self.assertEqual([u["reason"] for u in task["uncertain"]], ["unsupported-task-context"] * 2)

    def test_leading_dot_script_candidates_use_metadata_only(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        self.leading_dot_fixture()
        self.write(['node ./scripts/build.js "$MODE" config.json', 'python3 ./scripts/build.py "$MODE"'])
        original_open = Path.open

        def document_only_open(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "script candidates must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", document_only_open):
            task = suggest(self.taskfile, self.root)["tasks"]["build"]
        self.assertEqual([s["path"] for s in task["sources"]], ["scripts/build.js", "scripts/build.py"])
        self.assertFalse((self.root / "NEVER").exists())

    def node_fixture(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "build.js").write_text(
            "require('fs').writeFileSync('NEVER', 'synthetic-script-secret');\n"
            "require('hidden-module'); import('hidden-import');\n"
            "process.env.HIDDEN; require('fs').readFileSync('transitive.txt');\n")
        (self.root / "scripts" / "build file").write_text("synthetic-extensionless-secret")
        (self.root / "config.json").write_text("synthetic-argument-secret")

    def test_node_literal_script_locations_arguments_redaction_and_nonexecution(self):
        self.node_fixture()
        self.write(['node scripts/build.js "$MODE" config.json',
                    "'/usr/bin/node' -- scripts/build.js; node scripts/build.js",
                    'node scripts/build\\ file "$MODE" \'$LITERAL\''])
        original = self.taskfile.read_bytes()
        env_file = self.root / "env.json"
        env_file.write_text('{"MODE":"synthetic-snapshot-secret"}')
        host = dict(os.environ, HOST_ONLY="synthetic-host-secret")
        result = self.run_cli("--strict", "--json", "--env-snapshot", env_file, code=1, env=host)
        report = json.loads(result.stdout)
        task = report["tasks"]["build"]
        locations = [{"path": ["tasks", "build", "cmds", i]} for i in range(3)]
        self.assertEqual(task["sources"], [
            {"path": "scripts/build file", "locations": locations[2:]},
            {"path": "scripts/build.js", "locations": locations[:2]}])
        self.assertEqual(task["env"], [{"name": "MODE", "locations": [locations[0], locations[2]]}])
        self.assertEqual(task["uncertain"], [
            {"location": loc, "reason": "script-transitive-inputs-not-modeled"} for loc in locations])
        self.assertFalse(report["dependency_graph_complete"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["values_redacted"])
        self.assertEqual(report["native_execution_decision"], "unknown")
        self.assertEqual(result.stdout, self.run_cli("--strict", "--json", "--env-snapshot", env_file,
                                                    code=1).stdout)
        text = self.run_cli("--env-snapshot", env_file, env=host).stdout
        self.assertEqual(text, self.run_cli("--env-snapshot", env_file).stdout)
        for secret in ("hidden-module", "hidden-import", "HIDDEN", "transitive.txt", "config.json",
                       "synthetic-script-secret", "synthetic-extensionless-secret", "synthetic-argument-secret",
                       "synthetic-snapshot-secret", "synthetic-host-secret", "HOST_ONLY", "LITERAL"):
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, text)
        self.assertEqual(original, self.taskfile.read_bytes())
        self.assertFalse((self.root / "NEVER").exists())

    def test_node_reviewed_fragment_merge_preserves_entries_and_transitive_warning(self):
        self.node_fixture()
        self.write(['node scripts/build.js "$MODE" config.json'],
                   sources=["covered.txt"], env={"KEEP": "synthetic-env-secret"})
        before = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual({item["code"] for item in before["advisories"]},
                         {"missing-source-declaration", "missing-env-declaration"})
        text = self.run_cli().stdout
        fragment_text = text.split("  Merge this YAML fragment after review (replace REVIEW_REQUIRED):\n")[1]
        fragment = yaml.safe_load(fragment_text)["tasks"]["build"]
        self.assertEqual(fragment, yaml.safe_load(before["yaml_fragment"])["tasks"]["build"])
        self.assertEqual(fragment, {"sources": ["scripts/build.js"], "env": {"MODE": "REVIEW_REQUIRED"}})
        data = yaml.safe_load(self.taskfile.read_text())
        task = data["tasks"]["build"]
        task["sources"] += fragment["sources"]
        task["env"].update(fragment["env"])
        self.taskfile.write_text(yaml.safe_dump(data))
        after = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(after["advisories"], [])
        self.assertEqual(after["sources"], [])
        self.assertEqual(after["env"], [])
        self.assertIsNone(after["yaml_fragment"])
        self.assertEqual(after["uncertain"], [{"location": {"path": ["tasks", "build", "cmds", 0]},
                                              "reason": "script-transitive-inputs-not-modeled"}])
        self.assertEqual(after["uncertain"], before["uncertain"])
        self.assertEqual(task["sources"], ["covered.txt", "scripts/build.js"])
        self.assertEqual(task["env"], {"KEEP": "synthetic-env-secret", "MODE": "REVIEW_REQUIRED"})
        self.assertFalse((self.root / "NEVER").exists())

    def test_node_modes_dynamic_operands_and_shell_controls_withhold_entire_command(self):
        self.node_fixture()
        modes = ["-e 'process.env.HIDDEN'", "-p '1+1'", "--eval '1+1'", "--print '1+1'",
                 "--eval=1+1", "--print=1+1", "-e1+1", "-p1+1", "--require hidden-module scripts/build.js",
                 "--import hidden-module scripts/build.js", "--inspect scripts/build.js",
                 "--input-type=module scripts/build.js", "--version", "--help", "--check scripts/build.js",
                 "-", "", "--", "-- -", "-- --eval", "$SCRIPT", '"${SCRIPT}"']
        commands = ["node " + mode + " $MODE" for mode in modes if mode not in ("", "--")]
        commands += ["node", "node --", "$RUNNER scripts/build.js $MODE", '"$BIN/node" scripts/build.js $MODE',
                     "no${SUFFIX} scripts/build.js $MODE", "node $(echo script.js) $MODE", "node {{.SCRIPT}} $MODE",
                     "node scripts/build.js $MODE; cat $OTHER", "cat covered.txt; node -e '1' $MODE",
                     "node scripts/build.js $MODE | cat covered.txt", "node scripts/build.js $MODE && echo ok",
                     "env node scripts/build.js $MODE", "sudo node scripts/build.js $MODE",
                     "sh -c 'node scripts/build.js $MODE'", "cd sub; node scripts/build.js $MODE",
                     {"cmd": "node scripts/build.js $MODE", "silent": True}]
        self.write(commands)
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        self.assertEqual(task["advisories"], [])
        self.assertIsNone(task["yaml_fragment"])
        self.assertEqual({item["location"]["path"][-1] for item in task["uncertain"]}, set(range(len(commands))))
        for index in range(len(modes) - 2):
            self.assertIn(task["uncertain"][index]["reason"],
                          {"interpreter-mode-not-modeled", "dynamic-source-operand"})
        self.assertFalse((self.root / "NEVER").exists())

    def test_node_safe_file_and_unsupported_task_context_boundaries(self):
        self.node_fixture()
        (self.root / "alias.js").symlink_to(self.root / "scripts" / "build.js")
        (self.root / "linkdir").symlink_to(self.root / "scripts", target_is_directory=True)
        os.mkfifo(self.root / "pipe.js")
        paths = ["missing.js", "alias.js", "linkdir/build.js", "scripts", "pipe.js", "../outside.js",
                 "/etc/passwd", "", "scripts/*.js"]
        self.write(["node '" + path + "'" for path in paths])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["sources"], [])
        self.assertEqual(task["env"], [])
        for i in range(len(paths)):
            self.assertIn({"location": {"path": ["tasks", "build", "cmds", i]},
                           "reason": "source-path-not-safe-regular-literal"}, task["uncertain"])
        for context in ("dir", "includes"):
            with self.subTest(context=context):
                data = {"version": "3", "tasks": {"build": {"cmds": ['node scripts/build.js "$MODE"']}}}
                if context == "dir":
                    data["tasks"]["build"]["dir"] = "elsewhere"
                else:
                    data["includes"] = {"other": "other.yml"}
                self.taskfile.write_text(yaml.safe_dump(data))
                task = self.report("--strict", code=1)["tasks"]["build"]
                self.assertEqual(task["sources"], [])
                self.assertEqual(task["env"], [])
                self.assertIsNone(task["yaml_fragment"])
                self.assertEqual(task["uncertain"][0]["reason"], "unsupported-task-context")
                self.assertTrue(task["unassessed_findings"])

    def test_node_declared_globs_and_shorthand_locations(self):
        self.node_fixture()
        self.write(["node scripts/build.js"], sources=["scripts/*.js"])
        task = self.report("--strict", code=1)["tasks"]["build"]
        self.assertEqual(task["advisories"], [])
        self.assertEqual([item["reason"] for item in task["uncertain"]],
                         ["script-transitive-inputs-not-modeled"])
        self.taskfile.write_text(yaml.safe_dump({"version": "3", "tasks": {
            "string": "node scripts/build.js", "list": ["/usr/bin/node -- scripts/build.js"]}}))
        tasks = self.report("--strict", code=1)["tasks"]
        for name, location in [("string", ["tasks", "string"]), ("list", ["tasks", "list", 0])]:
            self.assertEqual(tasks[name]["sources"], [{"path": "scripts/build.js", "locations": [{"path": location}]}])
            self.assertEqual(tasks[name]["uncertain"], [{"location": {"path": location},
                                                       "reason": "script-transitive-inputs-not-modeled"}])

    def test_node_undeclared_candidates_are_not_opened(self):
        from unittest.mock import patch
        from stackwrench.task_audit.suggestions import suggest

        self.node_fixture()
        self.write(['node scripts/build.js "$MODE" config.json'])
        opened = []
        original_open = Path.open

        def document_only_open(path, *args, **kwargs):
            self.assertEqual(path, self.taskfile, "suggestion candidate contents must not be opened")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", document_only_open):
            report = suggest(self.taskfile, self.root)
        self.assertEqual(opened, [self.taskfile])
        self.assertEqual(report["tasks"]["build"]["sources"], [
            {"path": "scripts/build.js", "locations": [{"path": ["tasks", "build", "cmds", 0]}]}])


if __name__ == "__main__":
    unittest.main()
