from __future__ import annotations

import json
import subprocess
import unittest

from .test_cli import CLI, CliFixture


class PlanTests(CliFixture):
    def test_explanation_shows_existing_identities_for_all_setup_input_states(self):
        for tool, ref, pin, old in (("node", "v4", "22.14.0", "20.11.0"),
                                    ("python", "v5", "3.13.2", "3.12.8"),
                                    ("go", "v5", "1.24.0", "1.23.4")):
            for inline in (False, True):
                with self.subTest(tool=tool, inline=inline):
                    key = f"{tool}-version"
                    action = f"actions/setup-{tool}@{ref}"
                    workflow = "jobs:\n  test:\n    steps:\n"
                    inputs = [f"{key}: '{pin}'", f"{key}: '{old}'",
                              f"{key}-file: '${{{{ inputs.file }}}}'",
                              f"{key}: '{old}', {key}-file: 'unread.txt'",
                              None, f"{key}: '', {key}-file: 'unread.txt'"]
                    for fields in inputs:
                        workflow += f"      - uses: {action}\n"
                        if fields:
                            workflow += (f"        with: {{{fields}}}\n" if inline else
                                         "        with:\n" + "".join(
                                             f"          {field}\n" for field in fields.split(", ")))
                    workflow += ("      - uses: actions/checkout@v4\n"
                                 "      - uses: '${{ inputs.action }}'\n"
                                 "  shared:\n    uses: org/repo/.github/workflows/ci.yml@main\n")
                    self.write("ci.yml", workflow)
                    self.write(".nvmrc", "20.11.0\n")
                    before = self.tree()
                    identities = {p: (self.root / p).stat() for p in before}
                    args = ("--workflow", "ci.yml")
                    report = json.loads(self.run_cli("plan", "--json", *args, code=2).stdout)
                    rows = next(f["declarations"] for f in report["files"] if f["path"] == "ci.yml")
                    labels = [f"jobs.test.steps[{i}] {action} with.{key}" + ("-file" if i == 2 else "")
                              for i in range(6)]
                    labels += ["jobs.test.steps[6] actions/checkout@v4",
                               "jobs.test.steps[7] ${{ inputs.action }}", "jobs.shared.uses"]
                    self.assertEqual([d["declaration"] for d in rows], labels)
                    self.assertEqual([d["status"] for d in rows],
                                     ["match", "drift", "blocked", "blocked", "blocked", "blocked",
                                      "skipped", "skipped", "skipped"])
                    self.assertEqual((rows[2]["actual"], rows[2]["oldValue"], rows[2]["newValue"],
                                      rows[2]["evaluated"]), ("<missing>", "<missing>", None, False))
                    self.assertEqual((report["applicable"], report["changedFiles"], report["diff"]),
                                     (False, [], ""))
                    self.assertTrue(all(f["diff"] == "" for f in report["files"]))
                    text = self.run_cli("plan", "--explain", *args, code=2)
                    self.assertEqual(text.stderr, "")
                    # Literal row contract, plus all existing identities in JSON order.
                    self.assertIn(f'ci.yml:{rows[0]["line"]}: match: "{pin}" -> "{pin}" '
                                  f'(source mise.toml:{rows[0]["sourceLine"]} [tools].{tool}); '
                                  'exact version matches mise pin; declaration: '
                                  f'"{labels[0]}"\n', text.stdout)
                    self.assertEqual([line.rsplit("; declaration: ", 1)[1]
                                      for line in text.stdout.splitlines() if "; declaration: " in line],
                                     [json.dumps(d["declaration"], ensure_ascii=True)
                                      for d in report["declarations"]])
                    self.assertEqual(self.run_cli("plan", *args, code=2).stdout, "")
                    self.assertEqual(self.run_cli("apply", *args, code=2).stdout, "")
                    self.assertEqual(self.tree(), before)
                    for path, info in identities.items():
                        now = (self.root / path).stat()
                        self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def test_explanation_quotes_identities_and_includes_structural_rows(self):
        self.write(".nvmrc", "22.14.0\n")
        self.write("Dockerfile", "FROM node:20.11.0 AS build\nFROM build\nFROM scratch\n")
        self.write("ci.yml", 'jobs:\n  test:\n    steps:\n      - uses: \'vendor/café"tool@v1\'\n')
        self.write("bad.yml", "jobs: [test]\n")
        args = ("--workflow", "ci.yml", "--workflow", "bad.yml")
        before = self.tree()
        report = json.loads(self.run_cli("plan", "--json", *args, code=2).stdout)
        text = self.run_cli("plan", "--explain", *args, code=2).stdout
        labels = [json.loads(line.rsplit("; declaration: ", 1)[1])
                  for line in text.splitlines() if "; declaration: " in line]
        self.assertEqual(labels, [d["declaration"] for d in report["declarations"]])
        self.assertIn('bad.yml:1: blocked: "<unsupported workflow>" -> null; '
                      'jobs must be a block mapping; declaration: "workflow YAML"\n', text)
        self.assertIn('bad.yml:1: unsupported_workflow: jobs must be a block mapping\n', text)
        self.assertIn('declaration: "jobs.test.steps[0] vendor/caf\\u00e9\\"tool@v1"\n', text)
        self.assertEqual(text, self.run_cli("plan", "--explain", "--workflow", "bad.yml",
                                           "--workflow", "ci.yml", code=2).stdout)
        self.assertEqual(self.tree(), before)

    def test_per_file_diffs_cover_all_adapters_and_compose_in_path_order(self):
        workflow_path = ".github/workflows/ci.yml"
        workflow = ("on: push\njobs:\n  test:\n    steps:\n"
                    "      - uses: actions/setup-node@v4\n        with:\n"
                    "          node-version: '20.11.0' # keep\n")
        self.write(workflow_path, workflow)
        self.write(".nvmrc", "22.14.0\n")  # A matching target has no diff.
        self.write(".node-version", "20.11.0\n")
        self.write("Dockerfile", "FROM node:20.11.0-alpine AS build\nFROM build\n")
        self.write("other.yml", "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n")
        args = ("--workflow", "other.yml", "--workflow", workflow_path)
        before = self.tree()
        identities = {p: (self.root / p).stat() for p in before}
        report = json.loads(self.run_cli("plan", "--json", *args).stdout)
        expected = {
            workflow_path: (f"--- a/{workflow_path}\n+++ b/{workflow_path}\n@@ -4,4 +4,4 @@\n"
                            "     steps:\n       - uses: actions/setup-node@v4\n         with:\n"
                            "-          node-version: '20.11.0' # keep\n"
                            "+          node-version: '22.14.0' # keep\n"),
            ".node-version": ("--- a/.node-version\n+++ b/.node-version\n@@ -1 +1 @@\n"
                              "-20.11.0\n+22.14.0\n"),
            "Dockerfile": ("--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1,2 +1,2 @@\n"
                           "-FROM node:20.11.0-alpine AS build\n"
                           "+FROM node:22.14.0-alpine AS build\n FROM build\n"),
            ".nvmrc": "", "mise.toml": "", "other.yml": "",
        }
        self.assertTrue(report["applicable"])
        self.assertEqual({f["path"]: f["diff"] for f in report["files"]}, expected)
        aggregate = "".join(expected[path] for path in sorted(expected))
        self.assertEqual(report["diff"], aggregate)
        self.assertEqual(self.run_cli("plan", *args).stdout, aggregate)
        reverse = ("--workflow", workflow_path, "--workflow", "other.yml")
        self.assertEqual(report, json.loads(self.run_cli("plan", "--json", *reverse).stdout))
        self.assertEqual(report["changedFiles"], sorted(p for p, diff in expected.items() if diff))
        self.assertEqual(self.tree(), before)
        for path, info in identities.items():
            now = (self.root / path).stat()
            self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def test_per_file_diffs_preserve_crlf_and_missing_final_newline(self):
        self.write(".node-version", b"20.11.0")
        self.write(".nvmrc", " \tv20.11.0 # café\r\n")
        # An unchanged EOF context line still needs the no-newline marker.
        self.write("Dockerfile", "# 保留\r\nFROM node:20.11.0-alpine\r\nRUN echo keep")
        before = self.tree()
        report = json.loads(self.run_cli("plan", "--json").stdout)
        expected = {
            ".node-version": ("--- a/.node-version\n+++ b/.node-version\n@@ -1 +1 @@\n"
                              "-20.11.0\n\\ No newline at end of file\n"
                              "+22.14.0\n\\ No newline at end of file\n"),
            ".nvmrc": ("--- a/.nvmrc\n+++ b/.nvmrc\n@@ -1 +1 @@\n"
                       "- \tv20.11.0 # café\r\n+ \tv22.14.0 # café\r\n"),
            "Dockerfile": ("--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1,3 +1,3 @@\n"
                           " # 保留\r\n-FROM node:20.11.0-alpine\r\n"
                           "+FROM node:22.14.0-alpine\r\n RUN echo keep\n"
                           "\\ No newline at end of file\n"),
            "mise.toml": "",
        }
        self.assertEqual({f["path"]: f["diff"] for f in report["files"]}, expected)
        aggregate = "".join(expected[path] for path in sorted(expected))
        self.assertEqual(report["diff"], aggregate)
        # text=True would normalize CRLF and conceal a broken byte contract.
        plain = subprocess.run([*CLI, "plan", "--root", str(self.root)],
                               cwd=self.base, capture_output=True, timeout=10)
        self.assertEqual((plain.returncode, plain.stderr, plain.stdout), (0, b"", aggregate.encode()))
        self.assertEqual(self.tree(), before)
        self.run_cli("apply")
        self.assertEqual(self.tree(), {p: data.replace(b"20.11.0", b"22.14.0")
                                      for p, data in before.items()})
        clean = json.loads(self.run_cli("plan", "--json").stdout)
        self.assertEqual((clean["diff"], clean["changedFiles"]), ("", []))
        self.assertTrue(all(f["diff"] == "" for f in clean["files"]))

    def test_every_file_diff_is_empty_when_any_selected_input_blocks(self):
        self.write(".nvmrc", "20.11.0\r\n")
        self.write("Dockerfile", "FROM node:20.11.0\n")
        cases = [
            (".python-version", "^3.13\n", (), "unsupported_pin"),
            ("bad.Dockerfile", "FROM node:latest\n", ("--dockerfile", "bad.Dockerfile"), "unsupported_image"),
            ("ci.yml", "jobs:\n  test:\n    steps:\n      - uses: actions/setup-node@v4\n"
             "        with:\n          node-version: ${{ matrix.node }}\n",
             ("--workflow", "ci.yml"), "unsupported_action_pin"),
            ("ci.yml", "on: [push,,pull_request]\n", ("--workflow", "ci.yml"), "unsupported_workflow"),
            (None, None, ("--workflow", "absent.yml"), "missing_file"),
            (None, None, ("--version-file", ".nvmrc", "--version-file", ".nvmrc"), "duplicate_path"),
            ("mise.toml", '[tools]\nnode = "^22"\n', (), "unsupported_pin"),
        ]
        original_source = (self.root / "mise.toml").read_bytes()
        for path, value, args, code in cases:
            with self.subTest(code=code, value=value):
                if path:
                    self.write(path, value)
                before = self.tree()
                report = json.loads(self.run_cli("plan", "--json", *args, code=2).stdout)
                self.assertIn(code, {f["code"] for f in report["findings"]})
                self.assertEqual((report["applicable"], report["changedFiles"], report["diff"]), (False, [], ""))
                self.assertTrue(report["files"])
                self.assertTrue(all(f["diff"] == "" for f in report["files"]))
                if code != "duplicate_path" and path != "mise.toml":
                    candidate = next(d for d in report["declarations"] if d["path"] == ".nvmrc")
                    self.assertEqual(candidate["status"], "drift")
                self.assertEqual(self.run_cli("plan", *args, code=2).stdout, "")
                self.assertEqual(self.run_cli("apply", *args, code=2).stdout, "")
                self.assertEqual(self.tree(), before)
                if path == "mise.toml":
                    self.write(path, original_source)
                elif path:
                    (self.root / path).unlink()

    def test_mixed_explanation_is_complete_read_only_and_blocked(self):
        self.write(".nvmrc", "22.14.0\n")
        self.write(".python-version", "^3.13\n")
        self.write("Dockerfile", "# synthetic\nFROM node:20.11.0-alpine AS build\n"
                   "FROM python:3 AS py\nFROM build AS final\nFROM scratch\n")
        before = self.tree()
        identities = {p: (self.root / p).stat() for p in before}
        result = self.run_cli("plan", "--json", code=2)
        report = json.loads(result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["applicable"])
        self.assertEqual(report["changedFiles"], [])
        self.assertEqual(report["diff"], "")
        files = {f["path"]: f for f in report["files"]}
        self.assertEqual(list(files), sorted(before))
        node, floating, stage, scratch = files["Dockerfile"]["declarations"]
        self.assertEqual((node["status"], node["line"], node["oldValue"], node["newValue"],
                          node["sourcePath"], node["sourceLine"]),
                         ("drift", 2, "20.11.0", "22.14.0", "mise.toml", 2))
        self.assertEqual((floating["status"], floating["line"], floating["actual"],
                          floating["expected"], floating["newValue"], floating["sourceLine"]),
                         ("blocked", 3, "python:3", "3.13.2", None, 3))
        self.assertIn("exact", floating["reason"])
        self.assertEqual([stage["status"], scratch["status"]], ["skipped", "skipped"])
        self.assertEqual(files[".nvmrc"]["declarations"][0]["status"], "match")
        version = files[".python-version"]["declarations"][0]
        self.assertEqual((version["status"], version["oldValue"], version["newValue"], version["line"]),
                         ("blocked", "^3.13", None, 1))
        terminal = self.run_cli("plan", "--explain", code=2)
        self.assertEqual(terminal.stderr, "")
        self.assertIn("Dockerfile:2: drift", terminal.stdout)
        self.assertIn("mise.toml:2 [tools].node", terminal.stdout)
        self.assertIn("Dockerfile:3: blocked", terminal.stdout)
        self.assertIn("Dockerfile:4: skipped", terminal.stdout)
        self.assertIn(".nvmrc:1: match", terminal.stdout)
        self.assertIn("Batch blocked", terminal.stdout)
        self.assertEqual(self.tree(), before)
        for path, info in identities.items():
            now = (self.root / path).stat()
            self.assertEqual((info.st_ino, info.st_mtime_ns), (now.st_ino, now.st_mtime_ns))

    def test_plan_order_does_not_depend_on_selection_order(self):
        self.write("a/Dockerfile", "FROM node:20.11.0\n")
        self.write("z/Dockerfile", "FROM python:3.12.8\n")
        self.write("a/.nvmrc", "20.11.0\n")
        self.write("z/.go-version", "1.23.0\n")
        def args(reverse):
            pairs = [("--dockerfile", "a/Dockerfile"), ("--dockerfile", "z/Dockerfile"),
                     ("--version-file", "a/.nvmrc"), ("--version-file", "z/.go-version")]
            return [arg for pair in (reversed(pairs) if reverse else pairs) for arg in pair]
        first = self.run_cli("plan", "--json", *args(False)).stdout
        self.assertEqual(first, self.run_cli("plan", "--json", *args(True)).stdout)
        report = json.loads(first)
        self.assertTrue(report["applicable"])
        self.assertEqual(report["changedFiles"], sorted(report["changedFiles"]))
        self.assertEqual(report["diff"], self.run_cli("plan", *args(False)).stdout)
        self.assertEqual(self.run_cli("plan", "--explain", *args(False)).stdout,
                         self.run_cli("plan", "--explain", *args(True)).stdout)

    def test_source_locations_handle_toml_syntax_and_ignore_decoys(self):
        configs = [
            ('# node="0.0.0"\n[tools]\n"node" = "22.14.0"\n', 3),
            ('["tools"]\n\'node\' = "22.14.0"\n', 2),
            ('tools . "node" = "22.14.0"\n', 1),
            ('tools = { "node" = "22.14.0" }\n', 1),
            ('[tasks.fake]\nrun = """\n[tools]\nnode="0.0.0"\n"""\n'
             '[tools]\nnode = "22.14.0"\n', 7),
            ('[env]\nnode="0.0.0"\n[tools]\nnode = """22.14.0"""\n', 4),
            ('tools = { node = "22.14.0", python = { version = "3.13.2" } }\n', 1),
            ('meta = { date = 2026-11-11, time = 07:32:00, float = 3.14, flag = true, '
             'nested = [ {node="0.0.0"}, 3.14 ] }\n'
             '[tools]\nnode = "22.14.0"\n', 3),
            ('[[tasks.test]]\nnode="0.0.0"\n[tools]\n"n\\u006fde" = "22.14.0"\n', 4),
            ("message = '''\n[tools]\nnode=\"0.0.0\"\n'''\n"
             '[tools]\nnode = "22.14.0"\n', 6),
        ]
        self.write(".nvmrc", "20.11.0\n")
        for config, line in configs:
            with self.subTest(config=config):
                self.write("mise.toml", config)
                report = json.loads(self.run_cli("plan", "--json", code=2 if "version =" in config else 0).stdout)
                row = next(d for d in report["declarations"] if d["path"] == ".nvmrc")
                self.assertEqual(row["sourceLine"], line)
                source = next(d for d in report["declarations"] if d["kind"] == "mise" and d["tool"] == "node")
                self.assertEqual(source["line"], line)

    def test_invalid_source_and_missing_inputs_still_have_explanations(self):
        self.write("mise.toml", '[tools]\nnode = "^22"\n')
        self.write(".nvmrc", "20.11.0\n")
        report = json.loads(self.run_cli("plan", "--json", "--dockerfile", "absent", code=2).stdout)
        source, target = [next(d for d in report["declarations"] if d["path"] == path)
                          for path in ("mise.toml", ".nvmrc")]
        self.assertEqual((source["status"], source["line"], source["newValue"]), ("blocked", 2, None))
        self.assertEqual((target["status"], target["sourceLine"], target["expected"]), ("blocked", 2, None))
        missing = next(f for f in report["files"] if f["path"] == "absent")
        self.assertEqual(missing["findings"][0]["code"], "missing_file")

    def test_non_string_source_values_are_reported_without_json_errors(self):
        for value in ("2026-11-11", "07:32:00", "1979-05-27T07:32:00Z", "inf", "nan",
                      "3.14", "true", '{ version = "22.14.0" }', '["22.14.0"]'):
            with self.subTest(value=value):
                self.write("mise.toml", f"[tools]\nnode = {value}\n")
                report = json.loads(self.run_cli("plan", "--json", code=2).stdout)
                source = report["declarations"][0]
                self.assertEqual((source["status"], source["line"], source["newValue"]),
                                 ("blocked", 2, None))
                self.assertIsInstance(source["oldValue"], str)

    def test_clean_plan_and_argument_errors(self):
        self.write(".nvmrc", "22.14.0\n")
        report = json.loads(self.run_cli("plan", "--json").stdout)
        self.assertEqual((report["status"], report["applicable"], report["diff"], report["changedFiles"]),
                         ("clean", True, "", []))
        self.assertIn("Clean plan", self.run_cli("plan", "--explain").stdout)
        result = self.run_cli("plan", "--json", "--explain", code=2)
        self.assertIn("not allowed", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unsafe_images_never_propose_or_infer_versions(self):
        for image, tool in (("node:latest", "node"), ("node:${VERSION}", "node"),
                            ("python@sha256:" + "a" * 64, "python"),
                            ("registry.test/node:22.14.0", None), ("${BASE}", None)):
            with self.subTest(image=image):
                self.write("Dockerfile", f"FROM {image}\n")
                report = json.loads(self.run_cli("plan", "--json", code=2).stdout)
                row = next(d for d in report["declarations"] if d["kind"] == "docker-image")
                self.assertEqual((row["actual"], row["newValue"], row["status"], row["tool"]),
                                 (image, None, "blocked", tool))
                if tool is None:
                    self.assertIsNone(row["expected"])
                    self.assertIsNone(row["sourceLine"])


if __name__ == "__main__":
    unittest.main()
