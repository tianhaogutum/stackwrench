from __future__ import annotations

import json
import os
import unittest

from .test_cli import CliFixture

WORKFLOW = """# synthetic workflow; never executed
name: preserve 20.11.0
on: push
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      node-version: '20.11.0' # not an action input
    steps:
      - uses: actions/checkout@v4 # unknown action is skipped
      - name: Node
        uses: actions/setup-node@v4 # keep action ref
        with:
          node-version: '20.11.0' # 保留
          cache: npm
      - with:
          python-version: "3.12.8"
        uses: 'actions/setup-python@v5'
      - uses: actions/setup-go@0123456789abcdef0123456789abcdef01234567
        with:
          go-version: 1.23.4
      - run: |
          echo 'node-version: 20.11.0'
          uses: actions/setup-node@v4
          with:
            node-version: 9.9.9
"""
PATH = ".github/workflows/ci.yml"


def workflow(step):
    return "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n" + step


class ActionsTests(CliFixture):
    def test_version_file_only_refusals_identify_the_file_input_key(self):
        for tool in ("node", "python", "go"):
            for value in ("'${{ inputs.file }}'", "'22.14.0'", "''"):
                for inline in (False, True):
                    with self.subTest(tool=tool, value=value, inline=inline):
                        key = f"{tool}-version-file"
                        inputs = (f"        with: {{'{key}': {value}}}\n" if inline else
                                  f"        with:\n          '{key}': {value}\n")
                        text = workflow(f"      - uses: actions/setup-{tool}@v4\n" + inputs)
                        row = self.assert_flow_blocked(text, "unsupported_action_pin", 7 if inline else 8)
                        self.assertEqual(row["declaration"],
                                         f"jobs.test.steps[0] actions/setup-{tool}@v4 with.{key}")
                        self.assertEqual((row["actual"], row["oldValue"], row["evaluated"], row["newValue"]),
                                         ("<missing>", "<missing>", False, None))
                        explanation = self.run_cli("plan", "--explain", "--workflow", PATH, code=2).stdout
                        self.assertIn(f'{PATH}:{row["line"]}: blocked: "<missing>" -> null', explanation)
                        self.assertIn(row["reason"], explanation)

    def test_version_file_only_refusals_use_the_file_input_line(self):
        for tool in ("node", "python", "go"):
            for value in ("'${{ inputs.file }}'", "'22.14.0'", "''"):
                for inline in (False, True):
                    with self.subTest(tool=tool, value=value, inline=inline):
                        inputs = (f"        with: {{{tool}-version-file: {value}}}\n" if inline else
                                  f"        with:\n          {tool}-version-file: {value}\n")
                        text = workflow(f"      - uses: actions/setup-{tool}@v4\n" + inputs)
                        row = self.assert_flow_blocked(text, "unsupported_action_pin", 7 if inline else 8)
                        self.assertEqual((row["kind"], row["tool"], row["actual"], row["oldValue"]),
                                         ("actions-version", tool, "<missing>", "<missing>"))
                        self.assertIn("indirection is unsupported", row["reason"])

    def test_setup_input_location_precedence_is_preserved(self):
        for tool in ("node", "python", "go"):
            key = f"{tool}-version"
            cases = [
                # Actual version nodes win, including empty and structured ones.
                (f"        with:\n          {key}: '20.11.0'\n          {key}-file: file\n", 8),
                (f"        with:\n          {key}-file: file\n          {key}: '20.11.0'\n", 9),
                (f"        with:\n          {key}-file: file\n          {key}:\n", 9),
                (f"        with:\n          {key}-file: file\n          {key}:\n            - 20.11.0\n", 10),
                (f"        with: {{{key}-file: file, {key}: ''}}\n", 7),
                (f"        with: {{{key}: '20.11.0', {key}-file: file}}\n", 7),
                # No corresponding input: retain the uses location.
                ("", 6), ("        with: {}\n", 6),
                ("        with: {cache: npm}\n", 6),
                ("        with: {other-version-file: file}\n", 6),
            ]
            for inputs, line in cases:
                with self.subTest(tool=tool, inputs=inputs):
                    text = workflow(f"      - uses: actions/setup-{tool}@v4\n" + inputs)
                    row = self.assert_flow_blocked(text, "unsupported_action_pin", line)
                    self.assertEqual(row["declaration"],
                                     f"jobs.test.steps[0] actions/setup-{tool}@v4 with.{key}")
            # A bare with is a scalar, not an empty mapping; retain its shape error.
            row = self.assert_flow_blocked(workflow(f"      - uses: actions/setup-{tool}@v4\n        with:\n"),
                                           "unsupported_workflow", 7)
            self.assertEqual(row["declaration"], "workflow YAML")

    def test_flow_quoted_expressions_are_opaque_and_preserved(self):
        values = ["['${{ inputs.note }}', \"uses: actions/setup-go@v5\", 'go-version: 9.9.9']",
                  '[ "${{ format(\'note,{0}:#[x]\', inputs.note) }}",\tpush, ] # keep',
                  "['literal ${{ unfinished',]", "['${{ inputs.event }}']"]
        for ending in ("\n", "\r\n"):
            for value in values:
                with self.subTest(ending=ending, value=value):
                    text = ("on: [push, pull_request] # café\n"
                            f"metadata: {value}\n"
                            "jobs:\n  test:\n    steps:\n"
                            "      - uses: actions/setup-node@v4\n"
                            "        with: {node-version: '20.11.0', cache: '${{ inputs.cache }}'} # keep")
                    self.write(PATH, text.replace("\n", ending))
                    before = self.tree()
                    args = ("--workflow", PATH)
                    check = json.loads(self.run_cli("check", "--json", *args, code=1).stdout)
                    rows = [d for d in check["declarations"] if d["path"] == PATH]
                    self.assertEqual([(d["tool"], d["actual"], d["newValue"], d["line"])
                                      for d in rows], [("node", "20.11.0", "22.14.0", 7)])
                    marker = "\n\\ No newline at end of file\n"
                    old = "        with: {node-version: '20.11.0', cache: '${{ inputs.cache }}'} # keep"
                    expected = (f"--- a/{PATH}\n+++ b/{PATH}\n@@ -4,4 +4,4 @@\n"
                                f"   test:{ending}     steps:{ending}"
                                f"       - uses: actions/setup-node@v4{ending}"
                                "-" + old + marker + "+" + old.replace("20.11.0", "22.14.0") + marker)
                    plan = json.loads(self.run_cli("plan", "--json", *args).stdout)
                    self.assertEqual((plan["applicable"], plan["changedFiles"], plan["diff"]),
                                     (True, [PATH], expected))
                    self.assertEqual("".join(f["diff"] for f in plan["files"]), expected)
                    self.assertEqual(next(f["diff"] for f in plan["files"] if f["path"] == PATH), expected)
                    # run_cli translates CRLF; the installed replay checks raw plain bytes.
                    self.assertEqual(self.run_cli("plan", *args).stdout, expected.replace("\r\n", "\n"))
                    self.assertEqual(self.tree(), before)
                    self.run_cli("apply", *args)
                    self.assertEqual((self.root / PATH).read_bytes(),
                                     before[PATH].replace(b"node-version: '20.11.0'", b"node-version: '22.14.0'"))
                    self.run_cli("check", *args)
                    info = (self.root / PATH).stat()
                    self.assertEqual(self.run_cli("apply", *args).stdout, "No changes.\n")
                    now = (self.root / PATH).stat()
                    self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def test_inline_quoted_expressions_are_opaque_except_for_setup_pins(self):
        for ending, cache in [("\n", "'${{ inputs.cache }}'"),
                              ("\r\n", '"${{ inputs.cache }}"'),
                              ("\r\n", '"${{ format(\'cache,{0}:#\', inputs.cache) }}"')]:
            with self.subTest(ending=ending, cache=cache):
                text = ("on: [push, pull_request] # café\n"
                        "metadata: {uses: actions/setup-go@v5, go-version: 9.9.9}\n"
                        "jobs:\n  test:\n    steps:\n"
                        "      - uses: actions/setup-node@v4\n"
                        f"        with: {{node-version: '20.11.0', cache: {cache}}} # keep")
                self.write(PATH, text.replace("\n", ending))
                before = self.tree()
                args = ("--workflow", PATH)
                check = json.loads(self.run_cli("check", "--json", *args, code=1).stdout)
                rows = [d for d in check["declarations"] if d["path"] == PATH]
                self.assertEqual([(d["tool"], d["actual"], d["newValue"], d["line"])
                                  for d in rows], [("node", "20.11.0", "22.14.0", 7)])
                marker = "\n\\ No newline at end of file\n"
                old = f"        with: {{node-version: '20.11.0', cache: {cache}}} # keep"
                expected = (f"--- a/{PATH}\n+++ b/{PATH}\n@@ -4,4 +4,4 @@\n"
                            f"   test:{ending}     steps:{ending}"
                            f"       - uses: actions/setup-node@v4{ending}"
                            "-" + old + marker + "+" + old.replace("20.11.0", "22.14.0") + marker)
                plan = json.loads(self.run_cli("plan", "--json", *args).stdout)
                self.assertEqual((plan["applicable"], plan["changedFiles"], plan["diff"]),
                                 (True, [PATH], expected))
                self.assertEqual("".join(f["diff"] for f in plan["files"]), expected)
                self.assertEqual(next(f["diff"] for f in plan["files"] if f["path"] == PATH), expected)
                self.assertEqual(self.run_cli("plan", *args).stdout, expected.replace("\r\n", "\n"))
                self.assertEqual(self.tree(), before)
                self.run_cli("apply", *args)
                self.assertEqual((self.root / PATH).read_bytes(),
                                 before[PATH].replace(b"node-version: '20.11.0'", b"node-version: '22.14.0'"))
                self.run_cli("check", *args)
                info = (self.root / PATH).stat()
                self.assertEqual(self.run_cli("apply", *args).stdout, "No changes.\n")
                now = (self.root / PATH).stat()
                self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def test_inline_quoted_dynamic_setup_pins_remain_unevaluated(self):
        for tool in ("node", "python", "go"):
            for quote in ("'", '"'):
                with self.subTest(tool=tool, quote=quote):
                    value = quote + "${{ matrix." + tool + " }}" + quote
                    text = workflow(f"      - uses: actions/setup-{tool}@v4\n"
                                    f"        with: {{{tool}-version: {value}, cache: '${{{{ inputs.cache }}}}'}}\n")
                    row = self.assert_flow_blocked(text, "unsupported_action_pin", 7)
                    self.assertEqual((row["kind"], row["tool"], row["actual"]),
                                     ("actions-version", tool, "${{ matrix." + tool + " }}"))

    def test_inline_with_exact_diff_apply_and_noop(self):
        text = ("on: [push, pull_request] # café\n"
                "jobs:\n  test:\n    steps:\n"
                "      - uses: actions/setup-node@v4\n"
                "        with: {node-version: '20.11.0', cache: npm} # keep")
        for ending in ("\n", "\r\n"):
            with self.subTest(ending=ending):
                self.write(PATH, text.replace("\n", ending))
                before = self.tree()
                args = ("--workflow", PATH)
                check = json.loads(self.run_cli("check", "--json", *args, code=1).stdout)
                rows = [d for d in check["declarations"] if d["path"] == PATH]
                self.assertEqual([(d["kind"], d["tool"], d["actual"], d["newValue"], d["line"])
                                  for d in rows], [("actions-version", "node", "20.11.0", "22.14.0", 6)])
                marker = "\n\\ No newline at end of file\n"
                expected = (f"--- a/{PATH}\n+++ b/{PATH}\n@@ -3,4 +3,4 @@\n"
                            f"   test:{ending}     steps:{ending}"
                            f"       - uses: actions/setup-node@v4{ending}"
                            "-        with: {node-version: '20.11.0', cache: npm} # keep" + marker +
                            "+        with: {node-version: '22.14.0', cache: npm} # keep" + marker)
                plan = json.loads(self.run_cli("plan", "--json", *args).stdout)
                self.assertEqual((plan["applicable"], plan["changedFiles"], plan["diff"]),
                                 (True, [PATH], expected))
                self.assertEqual("".join(f["diff"] for f in plan["files"]), expected)
                self.assertEqual(next(f["diff"] for f in plan["files"] if f["path"] == PATH), expected)
                self.assertEqual(self.run_cli("plan", *args).stdout, expected.replace("\r\n", "\n"))
                self.assertEqual(self.tree(), before)
                self.run_cli("apply", *args)
                self.assertEqual((self.root / PATH).read_bytes(),
                                 before[PATH].replace(b"20.11.0", b"22.14.0"))
                self.run_cli("check", *args)
                info = (self.root / PATH).stat()
                self.assertEqual(self.run_cli("apply", *args).stdout, "No changes.\n")
                now = (self.root / PATH).stat()
                self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def test_inline_with_grammar_and_only_actual_setup_inputs(self):
        for tool, old, new, value in [
            ("node", "20.11.0", "22.14.0", "{node-version: '20.11.0'}"),
            ("python", "3.12.8", "3.13.2", '{ "python-version":\t"3.12.8", cache: pip, } # keep'),
            ("go", "1.23.4", "1.24.0", "{'go-version': 1.23.4, cache: true}"),
        ]:
            with self.subTest(tool=tool):
                text = ("on: [push, pull_request]\n"
                        "metadata: {uses: actions/setup-go@v5, go-version: 9.9.9, "
                        "with: 'node-version: 9.9.9', note: \"${{ inputs.note }},#:[literal]\"}\n"
                        "empty: {}\njobs:\n  test:\n"
                        "    env:\n      uses: actions/setup-node@v4\n"
                        "      with: {node-version: 9.9.9}\n"
                        "    steps:\n"
                        f"      - 'with': {value}\n"
                        f'        "uses": actions/setup-{tool}@v4\n'
                        "        env: {node-version: 9.9.9}\n"
                        '      - run: echo\n        env: ["uses: actions/setup-go@v5", "go-version: 9.9.9"]\n'
                        "      - uses: other/setup-node@v4\n        with: {node-version: '${{ matrix.node }}'}\n")
                self.write(PATH, text)
                plan = json.loads(self.run_cli("plan", "--json", "--workflow", PATH).stdout)
                rows = [d for d in plan["declarations"] if d["path"] == PATH]
                self.assertEqual([(d["kind"], d["tool"], d["actual"], d["line"]) for d in rows],
                                 [("actions-version", tool, old, 10),
                                  ("actions-other", None, "other/setup-node@v4", 15)])
                self.assertEqual(rows[0]["newValue"], new)
                self.assertIsNone(rows[1]["newValue"])
                self.run_cli("apply", "--workflow", PATH)
                self.assertEqual((self.root / PATH).read_bytes(), text.replace(old, new).encode())

    def test_malformed_or_complex_inline_mappings_block_the_batch(self):
        values = ["{node-version: '20.11.0'", "{node-version: 20.11.0}}",
                  "{node-version: 20.11.0 cache: npm}", "{,node-version: 20.11.0}",
                  "{node-version: 20.11.0,,cache: npm}", "{node-version: 20.11.0,,}",
                  "{node-version: 20.11.0, 'node-version': 9.9.9}",
                  '{cache: npm, "cache": yarn, node-version: 20.11.0}',
                  "{node-version: [20.11.0]}", "{node-version: {version: 20.11.0}}",
                  "{node-version: *pin}", "{node-version: &pin 20.11.0}",
                  "{node-version: !tag 20.11.0}", "{<<: *pins}",
                  "{node-version: ${{ matrix.node }}}",
                  r'{node-version: "20\u002e11.0"}', "{node-version: '20.11.0}",
                  "{node-version: '20.11.0''9.9.9'}", "{node-version: }", "{node-version}",
                  "{node-version:20.11.0}", '{"node-version":"20.11.0"}',
                  "{node-version: 20.11.0} suffix", "{node-version: 20.11.0}#comment",
                  "{node-version: 20.11.0 # comment}",
                  "{node-version: 20.11.0,\n          cache: npm}",
                  "{node-version: 20.11.0, cache: [npm]}",
                  "{node-version: 20.11.0, cache: ${{ inputs.cache }}}",
                  r"{node-version: 20.11.0, cache: '${{ inputs.ca\che }}'}",
                  "{node-version: 20.11.0, cache: '${{ inputs.cache }}'extra}",
                  "{node-version: 20.11.0, cache: '${{ inputs.cache }}\n'}",
                  "{node-version: 20.11.0, cache: ['${{ inputs.cache }}']}"]
        for value in values:
            with self.subTest(value=value):
                text = workflow(f"      - uses: actions/setup-node@v4\n        with: {value}\n")
                self.assert_flow_blocked(text, "unsupported_workflow", 7)

    def test_inline_unsafe_pins_and_required_block_shapes_block_the_batch(self):
        for tool in ("node", "python", "go"):
            for value in (f"{{{tool}-version: 20}}", f"{{{tool}-version: 'v20.11.0'}}",
                          f"{{{tool}-version: '^20.11.0'}}", f"{{{tool}-version: '20.11.0-rc.1'}}",
                          f"{{{tool}-version: ''}}", "{}", "{cache: npm}",
                          f"{{{tool}-version: 20.11.0, {tool}-version-file: '.tool-versions'}}",
                          f"{{{tool}-version: 20.11.0, {tool}-version-file: '${{{{ inputs.file }}}}'}}"):
                with self.subTest(tool=tool, value=value):
                    text = workflow(f"      - uses: actions/setup-{tool}@v4\n        with: {value}\n")
                    row = self.assert_flow_blocked(text, "unsupported_action_pin", 7 if tool + "-version:" in value else 6)
                    self.assertEqual((row["kind"], row["tool"]), ("actions-version", tool))
        for text, line in [
            ("jobs: {test: ignored}\n", 1),
            ("jobs:\n  test: {steps: ignored}\n", 2),
            ("jobs:\n  test:\n    steps: {uses: actions/setup-node@v4}\n", 3),
            (workflow("      - {uses: actions/setup-node@v4, node-version: 20.11.0}\n"), 6),
            (workflow("      - uses: {action: actions/setup-node@v4}\n"), 6),
            (workflow("      - uses: actions/setup-node@v4\n        with: {node-version: 20.11.0}\n") +
             "  reuse:\n    uses: {workflow: owner/repo/.github/workflows/build.yml@main}\n", 9),
        ]:
            with self.subTest(text=text):
                self.assert_flow_blocked(text, "unsupported_workflow", line)

    def test_flat_flow_lists_are_opaque_and_preserved(self):
        events = ["[push, pull_request]", "[ push , 'pull_request', ] # keep",
                  '["push",\t"pull_request"]', "[]", "[ ]", "[push,]",
                  "['push,#]: value', \"workflow_dispatch\"]"]
        for value in events:
            with self.subTest(value=value):
                text = (f"on: {value}\n"
                        'metadata: ["uses: actions/setup-python@v5", \'with:\', '
                        "'python-version: 9.9.9', actions/setup-go@v5]\n"
                        "jobs:\n  test:\n    steps:\n"
                        "      - uses: actions/setup-node@v4\n        with:\n"
                        "          node-version: '20.11.0' # keep\n"
                        '        env:\n          DECOYS: ["node-version: 9.9.9", "[with]", "# literal"]')
                # Exercise byte offsets with both CRLF and a missing final newline.
                self.write(PATH, text.replace("\n", "\r\n"))
                before = self.tree()
                args = ("--workflow", PATH)
                check = json.loads(self.run_cli("check", "--json", *args, code=1).stdout)
                rows = [d for d in check["declarations"] if d["path"] == PATH]
                self.assertEqual([(d["kind"], d["tool"], d["actual"], d["line"])
                                  for d in rows], [("actions-version", "node", "20.11.0", 8)])
                expected = (f"--- a/{PATH}\n+++ b/{PATH}\n@@ -5,6 +5,6 @@\n"
                            "     steps:\r\n       - uses: actions/setup-node@v4\r\n         with:\r\n"
                            "-          node-version: '20.11.0' # keep\r\n"
                            "+          node-version: '22.14.0' # keep\r\n"
                            '         env:\r\n           DECOYS: ["node-version: 9.9.9", "[with]", "# literal"]'
                            "\n\\ No newline at end of file\n")
                plan = json.loads(self.run_cli("plan", "--json", *args).stdout)
                self.assertEqual((plan["applicable"], plan["changedFiles"], plan["diff"]),
                                 (True, [PATH], expected))
                self.assertEqual(next(f["diff"] for f in plan["files"] if f["path"] == PATH), expected)
                # run_cli uses universal newlines for the plain CLI output.
                self.assertEqual(self.run_cli("plan", *args).stdout, expected.replace("\r\n", "\n"))
                self.assertEqual(self.tree(), before)
                self.run_cli("apply", *args)
                self.assertEqual((self.root / PATH).read_bytes(),
                                 before[PATH].replace(b"node-version: '20.11.0'", b"node-version: '22.14.0'"))
                self.run_cli("check", *args)
                info = (self.root / PATH).stat()
                self.assertEqual(self.run_cli("apply", *args).stdout, "No changes.\n")
                now = (self.root / PATH).stat()
                self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def assert_flow_blocked(self, text, code, line):
        self.write(PATH, text)
        self.write(".nvmrc", "20.11.0\n")
        paths = [self.root / p for p in self.tree()]
        before = {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_mode) for p in paths}
        args = ("--workflow", PATH)
        check = self.assert_blocked(code, *args)
        finding = next(f for f in check["findings"] if f["path"] == PATH)
        self.assertEqual((finding["code"], finding["line"]), (code, line))
        row = next(d for d in check["declarations"] if d["path"] == PATH and not d["evaluated"])
        self.assertEqual((row["newValue"], row["line"]), (None, line))
        plan = json.loads(self.run_cli("plan", "--json", *args, code=2).stdout)
        self.assertEqual((plan["declarations"], plan["findings"]), (check["declarations"], check["findings"]))
        self.assertEqual((plan["applicable"], plan["changedFiles"], plan["diff"]), (False, [], ""))
        self.assertTrue(all(f["diff"] == "" for f in plan["files"]))
        self.assertEqual({p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_mode) for p in paths}, before)
        return row

    def test_malformed_or_complex_flow_lists_block_the_batch(self):
        values = ["[push", "[push]]", "[push pull_request]", "[,push]", "[push,,pull_request]",
                  "[push,,]", "[[push]]", "[{push: true}]", "[push: true]", "[*events]",
                  "[&event push]", "[!event push]", "[${{ matrix.event }}]",
                  r'["pu\u0073h"]', "['push]", "['it''s']",
                  "['${{ inputs.note }}'suffix]", "['${{ inputs.note }}' 'push']",
                  r"['${{ inputs.no\te }}']", "['${{ inputs.note }}\n']",
                  "[['${{ inputs.note }}']]", "['${{ inputs.note }}',,]",
                  "[push] suffix", "[push]#comment", "[push, # no closing bracket",
                  "[push,\n  pull_request]", "[push#embedded]"]
        for value in values:
            with self.subTest(value=value):
                text = workflow("      - uses: actions/setup-node@v4\n        with:\n"
                                "          node-version: '20.11.0'\n").replace("on: push", f"on: {value}")
                self.assert_flow_blocked(text, "unsupported_workflow", 1)

    def test_flow_setup_versions_and_structural_lists_are_never_expanded(self):
        for tool in ("node", "python", "go"):
            for value in ("[20.11.0]", "['20.11.0', '22.14.0']", "[]",
                          "['${{ matrix.version }}']", '["${{ matrix.version }}"]'):
                with self.subTest(tool=tool, value=value):
                    text = workflow(f"      - uses: actions/setup-{tool}@v4\n        with:\n"
                                    f"          {tool}-version: {value}\n").replace("on: push", "on: [push, pull_request]")
                    row = self.assert_flow_blocked(text, "unsupported_action_pin", 8)
                    self.assertEqual((row["kind"], row["tool"], row["actual"]),
                                     ("actions-version", tool, value))
        for text, line in [
            ("['${{ inputs.workflow }}']\n", 1),
            ("on: [push]\njobs: [test]\n", 2),
            ("jobs:\n  test: ['${{ inputs.job }}']\n", 2),
            ("on: [push]\njobs:\n  test:\n    steps: [run]\n", 4),
            (workflow("      - uses: actions/setup-node@v4\n        with: [20.11.0]\n"), 7),
            (workflow("      - uses: [actions/setup-node@v4]\n"), 6),
            (workflow("      - uses: ['${{ inputs.action }}']\n"), 6),
            (workflow("      - uses: actions/setup-node@v4\n        with: ['${{ inputs.with }}']\n"), 7),
        ]:
            with self.subTest(text=text):
                self.assert_flow_blocked(text, "unsupported_workflow", line)

    def test_reusable_workflow_structured_uses_blocks_the_batch(self):
        cases = [
            ("[owner/repo/.github/workflows/build.yml@main]", 10),
            ("[]", 10),
            ('["uses: actions/setup-go@v5", "go-version: 9.9.9"]', 10),
            ("['${{ inputs.workflow }}']", 10),
            ("\n      - owner/repo/.github/workflows/build.yml@main", 11),
            ("\n      node-version: '20.11.0'", 11),
            ("|\n      owner/repo/.github/workflows/build.yml@main", 10),
            (">-\n      owner/repo/.github/workflows/build.yml@main", 10),
        ]
        for value, line in cases:
            with self.subTest(value=value):
                text = workflow("      - uses: actions/setup-node@v4\n        with:\n"
                                "          node-version: '20.11.0'\n").replace(
                                    "on: push", "on: [push, pull_request]")
                text += f"  reuse:\n    uses: {value}\n"
                row = self.assert_flow_blocked(text, "unsupported_workflow", line)
                self.assertEqual(row["kind"], "workflow")
                self.assertIn("reusable workflow uses must be a scalar", row["reason"])

    def test_reusable_workflow_scalar_uses_stays_skipped(self):
        for value in ("owner/repo/.github/workflows/build.yml@main",
                      "'owner/repo/.github/workflows/build.yml@main'",
                      '"${{ inputs.workflow }}"'):
            with self.subTest(value=value):
                text = workflow("      - uses: actions/setup-node@v4\n        with:\n"
                                "          node-version: '20.11.0'\n").replace(
                                    "on: push", "on: [push, pull_request]")
                text += f"  reuse:\n    uses: {value}\n"
                self.write(PATH, text)
                before = self.tree()
                args = ("--workflow", PATH)
                plan = json.loads(self.run_cli("plan", "--json", *args).stdout)
                rows = [d for d in plan["declarations"] if d["path"] == PATH]
                self.assertEqual([d["kind"] for d in rows], ["actions-version", "actions-other"])
                reuse = rows[1]
                self.assertEqual((reuse["line"], reuse["status"], reuse["evaluated"], reuse["newValue"]),
                                 (10, "skipped", False, None))
                self.assertEqual(reuse["actual"], value.strip("\"'"))
                self.assertTrue(plan["applicable"])
                self.assertEqual(self.tree(), before)
                self.run_cli("apply", *args)
                self.assertEqual((self.root / PATH).read_bytes(),
                                 before[PATH].replace(b"node-version: '20.11.0'", b"node-version: '22.14.0'"))

    def test_explicit_three_runtime_diff_apply_and_preserved_bytes(self):
        self.write(PATH, WORKFLOW.replace("\n", "\r\n").rstrip("\r\n"))
        os.chmod(self.root / PATH, 0o640)
        before = self.tree()
        # Nothing discovers workflows implicitly.
        self.run_cli("check", "--json")
        args = ("--workflow", PATH)
        report = json.loads(self.run_cli("check", "--json", *args, code=1).stdout)
        rows = [d for d in report["declarations"] if d["kind"] == "actions-version"]
        self.assertEqual([(r["tool"], r["actual"], r["expected"], r["line"], r["sourceLine"])
                          for r in rows], [("node", "20.11.0", "22.14.0", 16, 2),
                                          ("python", "3.12.8", "3.13.2", 19, 3),
                                          ("go", "1.23.4", "1.24.0", 23, 4)])
        unknown = next(d for d in report["declarations"] if d["kind"] == "actions-other")
        self.assertEqual((unknown["status"], unknown["evaluated"], unknown["newValue"]),
                         ("skipped", False, None))
        plan = json.loads(self.run_cli("plan", "--json", *args).stdout)
        self.assertEqual(plan["changedFiles"], [PATH])
        self.assertIn("-          node-version: '20.11.0' # 保留", plan["diff"])
        self.assertIn("+          node-version: '22.14.0' # 保留", plan["diff"])
        self.assertEqual(self.tree(), before)
        self.run_cli("apply", *args)
        expected = before[PATH].replace("node-version: '20.11.0' # 保留".encode(), "node-version: '22.14.0' # 保留".encode())
        expected = expected.replace(b'python-version: "3.12.8"', b'python-version: "3.13.2"')
        expected = expected.replace(b"go-version: 1.23.4", b"go-version: 1.24.0")
        self.assertEqual((self.root / PATH).read_bytes(), expected)
        self.assertEqual((self.root / PATH).stat().st_mode & 0o777, 0o640)
        self.run_cli("check", "--json", *args)
        info = (self.root / PATH).stat()
        self.assertEqual(self.run_cli("apply", *args).stdout, "No changes.\n")
        now = (self.root / PATH).stat()
        self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def test_unsupported_versions_and_indirect_inputs_block_entire_batch(self):
        cases = ["'${{ matrix.node }}'", "${{ inputs.node }}", "20", "20.11", "'lts/*'",
                 "'^20.11.0'", "'v20.11.0'", "'20.11.0-rc.1'", "'20.11.0+build'", "020.11.0",
                 "|\n            20.11.0\n", "\n            - 20.11.0\n"]
        for value in cases:
            with self.subTest(value=value):
                self.write(PATH, workflow("      - uses: actions/setup-node@v4\n        with:\n"
                                          f"          node-version: {value}\n"))
                self.write(".nvmrc", "20.11.0\n")
                report = self.assert_blocked("unsupported_action_pin", "--workflow", PATH)
                row = next(d for d in report["declarations"] if d["kind"] == "actions-version")
                self.assertEqual((row["evaluated"], row["newValue"]), (False, None))
                plan = json.loads(self.run_cli("plan", "--json", "--workflow", PATH, code=2).stdout)
                self.assertEqual((plan["applicable"], plan["changedFiles"], plan["diff"]), (False, [], ""))
        for tool in ("node", "python", "go"):
            self.write(PATH, workflow(f"      - uses: actions/setup-{tool}@v4\n        with:\n"
                                      f"          {tool}-version: '20.11.0'\n"
                                      f"          {tool}-version-file: .tool-versions\n"))
            self.assert_blocked("unsupported_action_pin", "--workflow", PATH)
        self.write(PATH, workflow("      - uses: actions/setup-node@v4\n"))
        self.assert_blocked("unsupported_action_pin", "--workflow", PATH)

    def test_only_explicit_action_types_match_and_missing_mise_pin_blocks(self):
        for action in ("other/setup-node@v4", "actions/setup-node-extra@v4", "./actions/setup-node",
                       "docker://node:20.11.0", "${{ matrix.action }}"):
            self.write(PATH, workflow(f"      - uses: {action}\n        with:\n          node-version: '20.11.0'\n"))
            report = json.loads(self.run_cli("check", "--json", "--workflow", PATH).stdout)
            row = next(d for d in report["declarations"] if d["path"] == PATH)
            self.assertEqual((row["evaluated"], row["status"], row["tool"]), (False, "skipped", None))
            self.assertEqual(self.run_cli("apply", "--workflow", PATH).stdout, "No changes.\n")
        self.write("mise.toml", '[tools]\npython = "3.13.2"\n')
        self.write(PATH, workflow("      - uses: actions/setup-node@v4\n        with:\n          node-version: '20.11.0'\n"))
        self.assert_blocked("missing_pin", "--workflow", PATH)

    def test_unsupported_yaml_is_explicitly_unevaluated_and_never_writes(self):
        cases = [
            workflow("      - uses: actions/setup-node@v4\n        with: {node-version: {version: '20.11.0'}}\n"),
            workflow("      - uses: actions/setup-node@v4\n        with:\n          node-version: '20.11.0'\n          node-version: '9.9.9'\n"),
            workflow("      - uses: actions/setup-node@v4\n        with: *pins\n"),
            workflow("      - uses: actions/setup-node@v4\n        with:\n          <<: *pins\n          node-version: '20.11.0'\n"),
            WORKFLOW + "---\njobs:\n  other: ignored\n",
            WORKFLOW.replace("node-version: '20.11.0' # 保留", 'node-version: "20\\u002e11.0"'),
            WORKFLOW.replace("    steps:", "\tsteps:"),
            WORKFLOW.replace("      - name: Node", "       - name: Node"),
            "on: push\njobs:\n  test:\n    steps: ${{ matrix.steps }}\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.write(PATH, text)
                self.write(".nvmrc", "20.11.0\n")
                report = self.assert_blocked("unsupported_workflow", "--workflow", PATH)
                row = next(d for d in report["declarations"] if d["kind"] == "workflow")
                self.assertFalse(row["evaluated"])
                self.assertIsNone(row["newValue"])

    def test_mixed_literal_and_matrix_steps_block_without_partial_workflow_edits(self):
        text = WORKFLOW + """
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node }}
"""
        self.write(PATH, text)
        before = self.tree()
        report = self.assert_blocked("unsupported_action_pin", "--workflow", PATH)
        rows = [d for d in report["declarations"] if d["kind"] == "actions-version"]
        self.assertEqual([d["status"] for d in rows], ["drift", "drift", "drift", "blocked"])
        plan = json.loads(self.run_cli("plan", "--json", "--workflow", PATH, code=2).stdout)
        self.assertEqual((plan["diff"], plan["changedFiles"]), ("", []))
        self.assertIn("not evaluated", rows[-1]["reason"])
        self.assertEqual(self.tree(), before)

    def test_block_shapes_quoted_keys_and_nested_decoys(self):
        marker = self.base / "must-not-exist"
        text = f"""on: push
jobs:
  test:
    strategy:
      matrix:
        node:
          - '20.11.0'
          - '22.14.0'
    steps:
      - run: >-
          touch {marker}
          uses: actions/setup-go@v5
          with:
            go-version: '9.9.9'
      -
        'with':
          "node-version": "20.11.0" # literal, no matrix expansion
        "uses": actions/setup-node@v4
  reuse:
    uses: owner/repo/.github/workflows/test.yml@main
"""
        self.write(PATH, text)
        report = json.loads(self.run_cli("plan", "--json", "--workflow", PATH).stdout)
        rows = [d for d in report["declarations"] if d["kind"] == "actions-version"]
        self.assertEqual([(d["tool"], d["actual"]) for d in rows], [("node", "20.11.0")])
        self.run_cli("apply", "--workflow", PATH)
        self.assertEqual((self.root / PATH).read_text(),
                         text.replace('"node-version": "20.11.0"', '"node-version": "22.14.0"'))
        self.assertFalse(marker.exists())

    def test_deep_workflow_nesting_returns_a_diagnostic(self):
        nested = "meta:\n" + "".join("  " * n + "child:\n" for n in range(1, 70))
        self.write(PATH, nested + WORKFLOW)
        report = self.assert_blocked("unsupported_workflow", "--workflow", PATH)
        self.assertIn("64 levels", report["findings"][0]["message"])

    def test_selection_safety_repeatable_order_and_default_exclusion(self):
        self.write(PATH, WORKFLOW)
        other = ".github/workflows/other.yaml"
        self.write(other, WORKFLOW)
        self.write(".github/workflows/unselected.yml", "not: [supported]\n")
        first = self.run_cli("plan", "--json", "--workflow", PATH, "--workflow", other).stdout
        self.assertEqual(first, self.run_cli("plan", "--json", "--workflow", other, "--workflow", PATH).stdout)
        self.assert_blocked("duplicate_path", "--workflow", PATH, "--workflow", "./" + PATH)
        self.assert_blocked("duplicate_path", "--workflow", "mise.toml")
        self.assert_blocked("unsafe_path", "--workflow", "../outside.yml")
        self.assert_blocked("missing_file", "--workflow", "absent.yml")
        (self.root / "link.yml").symlink_to(self.root / PATH)
        self.assert_blocked("unsafe_path", "--workflow", "link.yml")


if __name__ == "__main__":
    unittest.main()
