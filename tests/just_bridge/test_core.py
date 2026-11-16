import json
from pathlib import Path
import shlex
import subprocess
import unittest

from stackwrench.just_bridge import analyze, render_taskfile
from stackwrench.just_bridge.core import MAX_INPUT_BYTES

ROOT = Path(__file__).resolve().parents[2]


class ParserTests(unittest.TestCase):
    def require_supported(self, source):
        result = analyze(source)
        self.assertTrue(result.supported, result.report()["diagnostics"])
        return result

    def reject(self, source, code, line=None):
        result = analyze(source)
        self.assertFalse(result.supported)
        errors = [d for d in result.diagnostics if d.code == code]
        self.assertTrue(errors, result.report())
        if line is not None:
            self.assertEqual(errors[0].line, line)
        with self.assertRaisesRegex(ValueError, "no partial"):
            render_taskfile(result)
        self.assertEqual(result.report()["mappings"]["recipes"], [])
        self.assertEqual(result.report()["traces"], {})

    def test_real_fixture_graph_mappings_and_structure(self):
        result = self.require_supported((ROOT / "examples/just_bridge/justfile").read_text())
        report = result.report()
        self.assertEqual(report["graph"], {"build": ["clean", "check"],
                                          "clean": ["prepare"], "check": ["prepare"], "prepare": []})
        self.assertEqual(report["traces"]["build"], ["prepare", "clean", "check", "build"])
        self.assertEqual(report["traces"]["check"], ["prepare", "check"])
        build = report["mappings"]["recipes"][0]
        self.assertEqual(build["commands"][0]["line"], 7)
        self.assertEqual(build["commands"][0]["expanded"], "printf '%s\\n' 'building demo'")
        self.assertEqual(build["commands"][1]["expanded"], "printf '%s\\n' '$HOME $(printf untouched)'")
        doc = json.loads(render_taskfile(result))
        self.assertEqual(set(doc), {"version", "run", "tasks"})
        self.assertEqual(doc["version"], "3")
        self.assertEqual(doc["run"], "once")
        self.assertEqual(doc["tasks"]["default"], {"cmds": [{"task": "build"}]})
        self.assertEqual(doc["tasks"]["build"]["desc"], "Build the synthetic demo after ordered setup")
        self.assertEqual(doc["tasks"]["build"]["cmds"][:2], [{"task": "clean"}, {"task": "check"}])
        for task in doc["tasks"].values():
            self.assertLessEqual(set(task), {"cmds", "desc"})
            self.assertIsInstance(task["cmds"], list)
            for command in task["cmds"]:
                if isinstance(command, dict):
                    self.assertEqual(set(command), {"task"})
                    self.assertIn(command["task"], doc["tasks"])
                else:
                    self.assertIsInstance(command, str)
                    self.assertEqual(shlex.split(command)[:2], ["sh", "-cu"])
        self.assertTrue(any(d.severity == "warning" for d in result.diagnostics))

    def test_inline_literals_not_task_variables(self):
        source = "word := 'hi # $HOME' # comment\ntext := \"a\\t\\\"b\\\\\\u{1F916}\"\na:\n\techo '{{word}}' '{{ text }}'\n"
        result = self.require_supported(source)
        self.assertEqual(result.variables["text"].value, 'a\t"b\\🤖')
        self.assertEqual(result.recipes["a"].commands[0].expanded, "echo 'hi # $HOME' 'a\t\"b\\🤖'")
        self.assertNotIn("vars", json.loads(render_taskfile(result)))
        self.assertIn("🤖", render_taskfile(result))
        self.assertNotIn("\\ud83e", render_taskfile(result))

    def test_forward_variables_and_dependency_references(self):
        result = self.require_supported("build: clean\n  echo '{{late}}'\nlate := 'value'\nclean:\n  echo clean\n")
        self.assertEqual(result.traces["build"], ["clean", "build"])
        self.assertEqual(result.recipes["build"].commands[0].expanded, "echo 'value'")

    def test_diamond_and_duplicate_dependencies_run_once(self):
        result = self.require_supported("root: left right left\nleft: base\nright: base\nbase:\n")
        self.assertEqual(result.traces["root"], ["base", "left", "right", "root"])
        commands = json.loads(render_taskfile(result))["tasks"]["root"]["cmds"]
        self.assertEqual(commands, [{"task": "left"}, {"task": "right"}, {"task": "left"}])

    def test_deep_graph_is_iterative(self):
        source = "\n".join(f"r{i}: r{i+1}" for i in range(500)) + "\nr500:\n"
        result = self.require_supported(source)
        self.assertEqual(result.traces["r0"], [f"r{i}" for i in range(500, -1, -1)])

    def test_default_recipe_no_alias(self):
        doc = json.loads(render_taskfile(self.require_supported("default:\n  echo hi\n")))
        self.assertEqual(list(doc["tasks"]), ["default"])
        self.assertFalse(self.require_supported("default:\n").report()["mappings"]["default"]["synthetic"])

    def test_names_with_yaml_scalar_meanings_stay_strings(self):
        doc = json.loads(render_taskfile(self.require_supported("yes: on\non:\n  echo ': # false'\n")))
        self.assertEqual(doc["tasks"]["yes"]["cmds"], [{"task": "on"}])
        self.assertEqual(shlex.split(doc["tasks"]["on"]["cmds"][0])[2], "echo ': # false'")

    def test_crlf_blank_lines_and_doc_comments(self):
        source = "# orphan\r\n\r\n# one\r\n# two\r\na: # header\r\n  echo a\r\n\r\n  # shell comment\r\n\r\nb:\r\n\techo b\r\n"
        result = self.require_supported(source)
        self.assertEqual(result.recipes["a"].description, "one\ntwo")
        self.assertEqual(result.recipes["a"].commands[1].expanded, "# shell comment")
        self.assertIsNone(result.recipes["b"].description)

    def test_separate_variable_namespace(self):
        result = self.require_supported("build := 'literal'\nbuild:\n  echo '{{build}}'\n")
        self.assertEqual(result.recipes["build"].commands[0].expanded, "echo 'literal'")

    def test_newline_unicode_separators_and_quoting_roundtrip(self):
        source = 'x := "a\\nb\\r\\t\\u{2028}\\u{85}"\na:\n  printf "%s" \'{{x}}\'\n'
        result = self.require_supported(source)
        output = render_taskfile(result)
        self.assertNotIn("\u2028", output)
        self.assertIn("\\u2028", output)
        command = json.loads(output)["tasks"]["a"]["cmds"][0]
        self.assertEqual(shlex.split(command)[2], 'printf "%s" \'a\nb\r\t\u2028\u0085\'')

    def test_yaml_nonprintables_are_escaped_but_semantically_unchanged(self):
        for char in ["\u0080", "\u009f", "\ufffe", "\uffff"]:
            with self.subTest(char=repr(char)):
                source = f"x := '{char}'\na:\n  printf '%s' '{{{{x}}}}'\n"
                result = self.require_supported(source)
                output = render_taskfile(result)
                self.assertNotIn(char, output)
                self.assertEqual(shlex.split(json.loads(output)["tasks"]["a"]["cmds"][0])[2],
                                 f"printf '%s' '{char}'")

    def test_non_ascii_whitespace_is_not_silently_discarded(self):
        self.reject("\u0085\na:\n", "unsupported-syntax", 1)
        self.reject("a:\u00a0b\nb:\n", "unsupported-dependency", 1)
        self.reject("x := 'ok'\u00a0# comment\na:\n", "unsupported-variable", 1)

    def test_cycle(self):
        self.reject("a: b\nb: c\nc: a\n", "dependency-cycle", 1)
        self.reject("a: a\n", "dependency-cycle", 1)

    def test_duplicates_and_missing_dependency(self):
        self.reject("a:\na:\n", "duplicate-recipe", 2)
        self.reject("x := 'a'\nx := 'b'\na:\n", "duplicate-variable", 2)
        self.reject("a: missing\n", "missing-dependency", 1)

    def test_unsupported_variable_expressions(self):
        for expression in ["`touch marker`", 'x"$HOME"', 'f"hi"', 'env_var("KEY")',
                           "'a' + 'b'", "other", "if true { 'x' } else { 'y' }",
                           '"bad\\q"', '"\\u{0}"', '"\\u{D800}"', '"\\u{110000}"',
                           "'''multi'''", "'unterminated", "'literal {{braces}}'", "'`literal`'"]:
            with self.subTest(expression=expression):
                self.reject(f"x := {expression}\na:\n", "unsupported-variable", 1)

    def test_unsupported_top_level_grammar(self):
        for syntax in ["a who:", "a *args:", "[private]", "import 'x'", "mod foo",
                       "set shell := ['bash', '-c']", "alias b := a", "export X := 'x'",
                       "a $ARG:", "who ?= 'x'", "é:", "@a:"]:
            with self.subTest(syntax=syntax):
                self.reject(syntax + "\na:\n", "unsupported-syntax", 1)

    def test_unsupported_dependencies(self):
        for dependencies in ["(b 'x')", "b && c", "{{var}}", "b, c", "b *args"]:
            with self.subTest(dependencies=dependencies):
                self.reject(f"a: {dependencies}\nb:\n", "unsupported-dependency", 1)

    def test_unsupported_interpolation(self):
        for command in ["echo {{undefined}}", "echo {{'literal'}}", "echo {{x + 'y'}}",
                        "echo {{{{braces}}", "echo {{incomplete", "echo `touch marker`"]:
            with self.subTest(command=command):
                self.reject("a:\n  " + command + "\n", "unsupported-interpolation", 2)

    def test_unsupported_command_features(self):
        for command, code in [("#!/bin/sh", "script-recipe"), ("@echo hi", "command-prefix"),
                              ("-false", "command-prefix"), ("+echo hi", "command-prefix"),
                              ("echo hi \\" , "continuation")]:
            with self.subTest(command=command):
                self.reject("a:\n  " + command + "\n", code, 2)

    def test_indentation_errors(self):
        self.reject("a:\n \techo hi\n", "indentation", 2)
        self.reject("a:\n  echo one\n   echo two\n", "indentation", 3)
        self.reject("a:\n  echo one\n\techo two\n", "indentation", 3)
        self.reject("  echo orphan\na:\n", "orphan-body", 1)

    def test_default_private_and_description_template_rejections(self):
        self.reject("a:\ndefault:\n", "default-conflict", 2)
        self.reject("_private:\na:\n", "private-recipe", 1)
        self.reject("# {{.TASK}}\na:\n", "description-template", 2)

    def test_no_recipes_and_control_characters(self):
        self.reject("# only a comment\n", "no-recipes", 1)
        for char in ["\0", "\r", "\x01", "\x7f"]:
            with self.subTest(char=repr(char)):
                self.reject("a:\n  echo " + char + "middle\n", "control-character", 2)

    def test_resource_limits(self):
        self.reject("#" * (MAX_INPUT_BYTES + 1), "input-limit", 1)
        self.reject("\n".join(f"r{i}:" for i in range(513)), "recipe-limit", 513)
        source = "v := '" + "x" * 2000 + "'\na:\n  echo " + "{{v}}" * 600 + "\n"
        self.reject(source, "unsupported-interpolation", 3)
        source = "v := '" + "x" * 500_000 + "'\na:\n" + "  echo {{v}}\n" * 18
        self.reject(source, "expansion-limit")

    def test_rejected_example(self):
        self.reject((ROOT / "examples/just_bridge/rejected.justfile").read_text(), "unsupported-variable", 2)


class SyntheticShellTests(unittest.TestCase):
    """Run only our small synthetic shell strings, never user input."""

    def shell(self, command):
        return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=5)

    def command(self, source):
        result = analyze(source)
        self.assertTrue(result.supported, result.report())
        return json.loads(render_taskfile(result))["tasks"]["a"]["cmds"]

    def test_wrapper_preserves_dollars_quotes_and_shell_substitution(self):
        commands = self.command("a:\n  printf '%s\\n' '$HOME $(printf literal)' \"$(printf evaluated)\" \"a'b\"\n")
        actual = self.shell(commands[0])
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout, "$HOME $(printf literal)\nevaluated\na'b\n")

    def test_no_implicit_errexit_inside_one_command(self):
        actual = self.shell(self.command("a:\n  false; printf 'survived\\n'\n")[0])
        self.assertEqual(actual.returncode, 0)
        self.assertEqual(actual.stdout, "survived\n")

    def test_unset_variable_failure_and_independent_shells(self):
        commands = self.command("a:\n  export JTB_SYNTHETIC_055=value\n  unset JTB_SYNTHETIC_055; printf '%s' \"$JTB_SYNTHETIC_055\"\n")
        self.assertEqual(self.shell(commands[0]).returncode, 0)
        self.assertNotEqual(self.shell(commands[1]).returncode, 0)
        commands = self.command("a:\n  export JTB_SYNTHETIC_055=value\n  printf '%s' \"${JTB_SYNTHETIC_055-unset}\"\n")
        self.shell(commands[0])
        self.assertEqual(self.shell(commands[1]).stdout, "unset")
