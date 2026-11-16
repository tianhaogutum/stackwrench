from __future__ import annotations

import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

CLI = [sys.executable, "-m", "stackwrench", "pins"]
PROJECT = Path(__file__).resolve().parents[2]
PINS = '[tools]\nnode = "22.14.0"\npython = "3.13.2"\ngo = "1.24.0"\n'


class CliFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stackwrench-pins-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.write("mise.toml", PINS)

    def write(self, path, value):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value.encode() if isinstance(value, str) else value)

    def run_cli(self, command, *args, code=0, root=None):
        result = subprocess.run([*CLI, command, "--root", str(root or self.root), *args],
                                cwd=self.base, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and not p.is_symlink()}

    def assert_blocked(self, expected_code=None, *args):
        before = self.tree()
        check = self.run_cli("check", "--json", *args, code=2)
        report = json.loads(check.stdout)
        self.assertEqual(report["status"], "blocked")
        if expected_code:
            self.assertIn(expected_code, {f["code"] for f in report["findings"]})
        for command in ("plan", "apply"):
            result = self.run_cli(command, *args, code=2)
            self.assertEqual(result.stdout, "")
            self.assertTrue(result.stderr)
        self.assertEqual(self.tree(), before)
        self.assertEqual(list(self.root.rglob(".stackwrench-pins-*.tmp")), [])
        return report

class CliTests(CliFixture):
    def test_complete_three_language_e2e_and_idempotence(self):
        shutil.copytree(PROJECT / "examples/pins/project", self.root, dirs_exist_ok=True)
        before = self.tree()
        report = json.loads(self.run_cli("check", "--json", code=1).stdout)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(report["status"], "drift")
        self.assertEqual(len(report["declarations"]), 11)
        drift = [item for item in report["declarations"] if item["mismatch"]]
        self.assertEqual(len(drift), 7)
        self.assertEqual({item["tool"] for item in drift}, {"node", "python", "go"})
        node = next(item for item in drift if item["kind"] == "docker-image")
        self.assertEqual((node["path"], node["line"], node["source"], node["actual"], node["expected"]),
                         ("Dockerfile", 2, "mise.toml:[tools].node", "20.11.0", "22.14.0"))
        diff = self.run_cli("plan").stdout
        self.assertIn("-FROM --platform=linux/amd64 node:20.11.0-alpine AS app # retain this comment\n", diff)
        self.assertIn("+FROM --platform=linux/amd64 node:22.14.0-alpine AS app # retain this comment\n", diff)
        self.assertIn("+FROM python:3.13.2-slim-bookworm AS python_runtime\n", diff)
        self.assertIn("+FROM golang:1.24.0-alpine3.21 AS go_build\n", diff)
        self.assertIn("+v22.14.0 # synthetic Node pin\n", diff)
        self.assertEqual(self.tree(), before, "check and plan must not mutate")
        self.assertEqual(self.run_cli("apply").stdout, "Updated 5 file(s).\n")
        after = self.tree()
        for path, data in before.items():
            expected = data if path == "mise.toml" else data.replace(b"20.11.0", b"22.14.0").replace(b"3.12.8", b"3.13.2").replace(b"1.23.4", b"1.24.0")
            self.assertEqual(after[path], expected, path)
        clean = json.loads(self.run_cli("check", "--json").stdout)
        self.assertEqual(clean["status"], "clean")
        self.assertFalse(clean["mismatch"])
        self.assertEqual(clean["changedFiles"], [])
        stats = {path: (self.root / path).stat() for path in after}
        self.assertEqual(self.run_cli("plan").stdout, "")
        self.assertEqual(self.run_cli("apply").stdout, "No changes.\n")
        self.assertEqual(self.tree(), after)
        for path, info in stats.items():
            current = (self.root / path).stat()
            self.assertEqual((info.st_ino, info.st_mtime_ns), (current.st_ino, current.st_mtime_ns))

    def test_exact_diff_without_final_newline(self):
        self.write(".node-version", b"20.11.0")
        self.assertEqual(self.run_cli("plan").stdout,
                         "--- a/.node-version\n+++ b/.node-version\n@@ -1 +1 @@\n"
                         "-20.11.0\n\\ No newline at end of file\n"
                         "+22.14.0\n\\ No newline at end of file\n")
        self.run_cli("apply")
        self.assertEqual((self.root / ".node-version").read_bytes(), b"22.14.0")

    def test_crlf_indentation_unicode_comments_and_modes(self):
        docker = "# café\r\n\tfRoM\t--platform=$BUILDPLATFORM\tnode:20.11.0-alpine3.21\tAs\tapp\t# 保留\r\nRUN echo '20.11.0'\r\n"
        version = " \tv20.11.0 \t# 保留\r\n"
        self.write("Dockerfile", docker)
        self.write(".nvmrc", version)
        os.chmod(self.root / "Dockerfile", 0o750)
        self.run_cli("apply")
        self.assertEqual((self.root / "Dockerfile").read_bytes(),
                         docker.replace("node:20.11.0", "node:22.14.0").encode())
        self.assertEqual((self.root / ".nvmrc").read_bytes(), version.replace("20.11.0", "22.14.0").encode())
        self.assertEqual((self.root / "Dockerfile").stat().st_mode & 0o777, 0o750)
        self.run_cli("check")

    def test_explicit_nested_selection_replaces_defaults(self):
        self.write("mise.toml", "invalid = [")
        self.write("Dockerfile", "FROM unknown:latest\n")
        self.write(".nvmrc", "lts/*\n")
        self.write("config/pins.toml", PINS)
        self.write("containers/one", "FROM node:20.11.0\n")
        self.write("containers/two", "FROM golang:1.23.0-alpine\n")
        self.write("web/.node-version", "20.11.0\n")
        self.write("api/.go-version", "1.23.0\n")
        args = ("--mise", "config/pins.toml", "--dockerfile", "containers/one", "--dockerfile", "containers/two",
                "--version-file", "web/.node-version", "--version-file", "api/.go-version")
        self.run_cli("apply", *args)
        self.run_cli("check", *args)
        self.assertEqual((self.root / "Dockerfile").read_text(), "FROM unknown:latest\n")
        self.assertEqual((self.root / ".nvmrc").read_text(), "lts/*\n")
        self.assertIn("22.14.0", (self.root / "containers/one").read_text())
        self.assertIn("1.24.0", (self.root / "containers/two").read_text())

    def test_default_selection_never_recurses(self):
        self.write("nested/Dockerfile", "FROM ${UNTRUSTED}\n")
        self.write("nested/.nvmrc", "lts/*\n")
        report = json.loads(self.run_cli("check", "--json").stdout)
        self.assertEqual(len(report["declarations"]), 3)
        self.assertEqual(self.run_cli("apply").stdout, "No changes.\n")

    def test_tasks_and_shell_text_are_never_executed(self):
        marker = self.base / "must-not-exist"
        self.write("mise.toml", PINS + f'\n[tasks.bad]\nrun = "touch {marker}"\n')
        self.write("Dockerfile", f"FROM node:20.11.0\nRUN touch {marker}\n")
        self.run_cli("plan")
        self.run_cli("apply")
        self.assertFalse(marker.exists())
        self.write(".nvmrc", f"$(touch {marker})\n")
        self.assert_blocked("unsupported_pin")
        self.assertFalse(marker.exists())

    def test_scratch_and_prior_stage_are_explicitly_unevaluated(self):
        self.write("Dockerfile", "FROM node:22.14.0 AS app\nFROM APP AS second\nFROM scratch\n")
        report = json.loads(self.run_cli("check", "--json").stdout)
        stages = [d for d in report["declarations"] if d["kind"] == "docker-stage"]
        self.assertEqual([d["actual"] for d in stages], ["APP", "scratch"])
        self.assertTrue(all(d["expected"] is None and not d["evaluated"] for d in stages))

    def test_non_from_continuation_is_not_a_declaration(self):
        self.write("Dockerfile", "FROM node:20.11.0 AS first\nRUN echo anything \\\n  FROM python:9.9.9-not-an-image\nFROM first AS second\n")
        self.run_cli("apply")
        data = (self.root / "Dockerfile").read_text()
        self.assertIn("node:22.14.0", data)
        self.assertIn("FROM python:9.9.9-not-an-image", data)

    def test_unsupported_source_versions_and_complex_values(self):
        for value in ('"22"', '"22.14"', '"latest"', '"lts"', '"lts/*"', '"^22.14.0"',
                      '"22.14.0 || 23.0.0"', '"v22.14.0"', '"22.14.0-rc.1"', '"22.14.0+build"',
                      '"022.14.0"', '22', '["22.14.0"]', '{ version = "22.14.0" }'):
            with self.subTest(value=value):
                self.write("mise.toml", f"[tools]\nnode = {value}\n")
                self.write(".nvmrc", "20.11.0\n")
                self.assert_blocked("unsupported_pin")

    def test_unknown_source_tool(self):
        self.write("mise.toml", PINS + 'ruby = "3.4.0"\n')
        self.write(".nvmrc", "20.11.0\n")
        self.assert_blocked("unsupported_tool")

    def test_malformed_toml_leaves_every_file_unchanged(self):
        self.write("mise.toml", '[tools]\nnode = "22.14.0\n')
        self.write("Dockerfile", "FROM node:20.11.0-alpine\n")
        self.write(".nvmrc", "20.11.0\n")
        report = self.assert_blocked("invalid_input")
        finding = next(f for f in report["findings"] if f["path"] == "mise.toml")
        message = finding["message"]
        self.assertIn("TOML", message)
        self.assertIn("line 2", message)
        self.assertEqual(finding["code"], "invalid_input")

    def test_missing_or_empty_tools_table(self):
        for config in ("", "[tools]\n", '[env]\nX="1"\n', 'tools = "node"\n'):
            with self.subTest(config=config):
                self.write("mise.toml", config)
                self.assert_blocked("invalid_input")

    def test_excessive_toml_nesting_is_a_located_error(self):
        self.write("mise.toml", '[tools]\nnode = ' + '[' * 1500 + '"22.14.0"' + ']' * 1500)
        report = self.assert_blocked("invalid_input")
        self.assertEqual(report["findings"][0]["path"], "mise.toml")
        self.assertIn("nesting", report["findings"][0]["message"])

    def test_target_without_source_pin_is_unevaluated(self):
        self.write("mise.toml", '[tools]\nnode = "22.14.0"\n')
        self.write("Dockerfile", "FROM python:3.12.8\n")
        report = self.assert_blocked("missing_pin")
        row = next(d for d in report["declarations"] if d["kind"] == "docker-image")
        self.assertFalse(row["evaluated"])
        self.assertIsNone(row["expected"])

    def test_invalid_last_file_blocks_earlier_valid_edits(self):
        self.write("Dockerfile", "FROM node:20.11.0-alpine\n")
        self.write(".nvmrc", "20.11.0\n")
        self.write(".go-version", "1.24\n")
        self.assert_blocked("unsupported_pin")

    def test_unsupported_version_file_contents(self):
        for value in ("lts/*\n", "22\n", "22.14\n", "22.14.0-rc.1\n", "22.14.0+abc\n",
                      "22.14.0\n20.11.0\n", "\n22.14.0\n", "", "02.14.0\n"):
            with self.subTest(value=value):
                self.write(".node-version", value)
                self.assert_blocked("unsupported_pin")

    def test_digest_image_refusal(self):
        for image in ("node:20.11.0-alpine@sha256:" + "a" * 64, "node@sha256:" + "b" * 64):
            with self.subTest(image=image):
                self.write("Dockerfile", f"FROM {image} AS app # leave digest alone\n")
                self.write(".nvmrc", "20.11.0\n")
                self.assert_blocked("digest_pinned")

    def test_dynamic_image_refusal(self):
        for image in ("node:${NODE_VERSION}-alpine", "${BASE}", "node:$NODE"):
            with self.subTest(image=image):
                self.write("Dockerfile", f"ARG NODE_VERSION=20.11.0\nFROM {image}\n")
                self.assert_blocked("dynamic_image")

    def test_unknown_floating_registry_images_and_forward_stages(self):
        for image in ("node", "node:latest", "node:22", "node:22.14", "node:v22.14.0", "node:022.14.0",
                      "ubuntu:24.04", "docker.io/library/node:22.14.0", "registry.test/node:22.14.0", "future"):
            with self.subTest(image=image):
                self.write("Dockerfile", f"FROM {image}\n")
                self.assert_blocked("unsupported_image")

    def test_malformed_or_unsupported_from(self):
        for line in ("FROM", "FROM node:20.11.0 AS", "FROM node:20.11.0 extra", "FROM --unknown=x node:20.11.0",
                     "FROM --platform node:20.11.0", "FROM node:20.11.0#missing-space", "FROM node:20.11.0 \\\n AS app"):
            with self.subTest(line=line):
                self.write("Dockerfile", line + "\n")
                self.assert_blocked("unsupported_from")

    def test_heredoc_refused_without_interpreting_its_body(self):
        self.write("Dockerfile", "FROM node:20.11.0\nRUN <<EOF\nFROM python:9.9.9\nEOF\n")
        self.assert_blocked("unsupported_heredoc")

    def test_nondefault_escape_refused(self):
        self.write("Dockerfile", "# escape=`\nFROM node:20.11.0\n")
        self.assert_blocked("unsupported_escape")

    def test_default_escape_and_commented_from(self):
        self.write("Dockerfile", "# escape=\\\n# FROM ubuntu:latest\nFROM node:22.14.0\n")
        self.run_cli("check")

    def test_no_from_and_duplicate_alias_refused(self):
        self.write("Dockerfile", "# comment only\n")
        self.assert_blocked("missing_from")
        self.write("Dockerfile", "FROM node:20.11.0 AS app\nFROM app AS APP\n")
        self.assert_blocked("duplicate_stage")

    def test_symlink_input_source_and_parent_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        target = outside / ".node-version"
        target.write_text("20.11.0\n")
        (self.root / ".node-version").symlink_to(target)
        self.assert_blocked("unsafe_path")
        self.assertEqual(target.read_text(), "20.11.0\n")
        (self.root / ".node-version").unlink()
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        self.assert_blocked("unsafe_path", "--version-file", "linked/.node-version")
        (self.root / "mise.toml").unlink()
        (outside / "mise.toml").write_text(PINS)
        (self.root / "mise.toml").symlink_to(outside / "mise.toml")
        self.assert_blocked("unsafe_path")

    def test_dangling_symlink_default_is_not_silently_skipped(self):
        (self.root / "Dockerfile").symlink_to(self.base / "absent")
        self.assert_blocked("unsafe_path")

    def test_root_and_root_ancestor_symlinks_refused(self):
        (self.base / "link").symlink_to(self.root, target_is_directory=True)
        for path in (self.base / "link", self.base / "link" / "nested"):
            with self.subTest(path=path):
                report = json.loads(self.run_cli("check", "--json", root=path, code=2).stdout)
                self.assertEqual(report["findings"][0]["code"], "unsafe_path")

    def test_absolute_and_parent_traversal_paths_refused(self):
        self.write(".node-version", "20.11.0\n")
        for path in (str(self.root / ".node-version"), "../project/.node-version", "nested/../.node-version"):
            with self.subTest(path=path):
                self.assert_blocked("unsafe_path", "--version-file", path)

    def test_duplicate_paths_and_roles_refused(self):
        self.write("Dockerfile", "FROM node:20.11.0\n")
        self.assert_blocked("duplicate_path", "--dockerfile", "Dockerfile", "--dockerfile", "./Dockerfile")
        self.assert_blocked("duplicate_path", "--dockerfile", "mise.toml")

    def test_missing_source_explicit_target_and_root(self):
        self.assert_blocked("missing_file", "--version-file", "missing/.node-version")
        (self.root / "mise.toml").unlink()
        self.assert_blocked("missing_file")
        report = json.loads(self.run_cli("check", "--json", root=self.base / "missing", code=2).stdout)
        self.assertEqual(report["status"], "blocked")

    def test_unknown_version_filename(self):
        self.write(".tool-versions", "node 22.14.0\n")
        self.assert_blocked("unsupported_file", "--version-file", ".tool-versions")

    def test_hard_link_and_non_regular_files_refused(self):
        self.write(".nvmrc", "20.11.0\n")
        os.link(self.root / ".nvmrc", self.base / "other-link")
        self.assert_blocked("unsafe_path")
        (self.root / ".nvmrc").unlink()
        (self.root / "Dockerfile").mkdir()
        self.assert_blocked("unsafe_path")
        (self.root / "Dockerfile").rmdir()
        os.mkfifo(self.root / "Dockerfile")
        # Avoid reading the FIFO in the test's tree snapshot helper.
        result = self.run_cli("apply", code=2)
        self.assertIn("regular file", result.stderr)

    def test_input_size_and_encoding_limits(self):
        for value in (b"\xff\n", b"20.11.0\x00\n", b"20.11.0\r", b"\xef\xbb\xbf20.11.0\n",
                      b"20.11.0\v", "20.11.0\u2028".encode(), b"x" * (1024 * 1024 + 1)):
            with self.subTest(value=value[:30]):
                self.write(".nvmrc", value)
                self.assert_blocked("invalid_input")

    def test_cli_help_and_missing_arguments(self):
        result = subprocess.run([*CLI, "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("{check,plan,apply}", result.stdout)
        self.assertTrue(result.stdout.startswith("usage: stackwrench pins"))
        result = subprocess.run([*CLI, "apply"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--root", result.stderr)
        self.assertIn("usage: stackwrench pins apply", result.stderr)

    def test_version_prints_bare_stackwrench_version(self):
        from stackwrench import __version__
        result = subprocess.run([*CLI, "--version"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), __version__)

    def test_human_check_exposes_existing_drift_identities_only(self):
        self.write(".nvmrc", "v20.11.0 # keep\r\n")
        docker = 'FROM node:20.11.0-alpine AS build # café "keep"'
        self.write("Dockerfile", docker + "\nFROM build\nFROM scratch\n")
        runtime_rows = [
            f"Dockerfile:1: node: 20.11.0 -> 22.14.0 (source mise.toml:[tools].node; location mise.toml:2); "
            f"declaration: {json.dumps(docker, ensure_ascii=True)}",
            '.nvmrc:1: node: 20.11.0 -> 22.14.0 (source mise.toml:[tools].node; location mise.toml:2); declaration: ".nvmrc"',
        ]
        workflow_rows = {}
        for inline, path in ((False, "block.yml"), (True, "inline.yml")):
            lines = ["jobs:", "  runtimes:", "    steps:"]
            rows = []
            for index, (tool, old, pin) in enumerate((("node", "20.11.0", "22.14.0"),
                                                      ("python", "3.12.8", "3.13.2"),
                                                      ("go", "1.23.4", "1.24.0"))):
                lines.append(f"      - uses: actions/setup-{tool}@v4")
                if inline:
                    lines.append(f"        with: {{{tool}-version: '{old}', cache: '${{{{ inputs.cache }}}}'}}")
                else:
                    lines.extend(["        with:", f"          {tool}-version: '{old}'"])
                label = f"jobs.runtimes.steps[{index}] actions/setup-{tool}@v4 with.{tool}-version"
                rows.append(f"{path}:{len(lines)}: {tool}: {old} -> {pin} "
                            f"(source mise.toml:[tools].{tool}; location mise.toml:{index + 2}); "
                            f"declaration: {json.dumps(label)}")
            lines.extend(["      - uses: actions/setup-python@v5",
                          "        with: {python-version: '3.13.2'}",
                          "      - uses: actions/checkout@v4",
                          "      - uses: '${{ inputs.action }}'",
                          "  shared:", "    uses: org/repo/.github/workflows/reuse.yml@main"])
            self.write(path, "\n".join(lines) + "\n")
            workflow_rows[path] = rows
        before = self.tree()
        stats = {p: (self.root / p).stat() for p in before}
        for paths in (("inline.yml", "block.yml"), ("block.yml", "inline.yml")):
            with self.subTest(paths=paths):
                args = tuple(arg for path in paths for arg in ("--workflow", path))
                report = json.loads(self.run_cli("check", "--json", *args, code=1).stdout)
                self.assertEqual(len([d for d in report["declarations"] if d["mismatch"]]), 8)
                expected = "\n".join(runtime_rows + workflow_rows[paths[0]] + workflow_rows[paths[1]] +
                                     ["Drift detected."]) + "\n"
                result = self.run_cli("check", *args, code=1)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, expected)
                # Text keeps the existing selection order; JSON keeps its sorted order.
                self.assertEqual(sorted(json.loads(line.rsplit("; declaration: ", 1)[1])
                                        for line in result.stdout.splitlines()[:-1]),
                                 sorted(d["declaration"] for d in report["declarations"] if d["mismatch"]))
        self.assertEqual(self.tree(), before)
        for path, info in stats.items():
            now = (self.root / path).stat()
            self.assertEqual((info.st_ino, info.st_mtime_ns, info.st_mode),
                             (now.st_ino, now.st_mtime_ns, now.st_mode))

    def test_human_check_uses_existing_source_locations_for_toml_key_forms(self):
        configs = [
            '# decoy node="0.0.0"\n\n["tools"]\n"node" = "22.14.0"\n'
            '# python="0.0.0"\n\'python\' = "3.13.2"\n\ngo = "1.24.0"\n',
            '# decoy tools.node="0.0.0"\n\n\n tools . "node" = "22.14.0"\n'
            '# comment\ntools.python = "3.13.2"\n\ntools.go = "1.24.0"\n',
        ]
        self.write("Dockerfile", "# retain\nFROM node:20.11.0-alpine\n"
                   "FROM python:3.12.8-slim\nFROM golang:1.23.4\n")
        for name, value in ((".nvmrc", "v20.11.0 # keep\r\n"),
                            (".python-version", "3.12.8\n"), (".go-version", "1.23.4")):
            self.write(name, value)
        expected = (
            'Dockerfile:2: node: 20.11.0 -> 22.14.0 (source config/pins.toml:[tools].node; location config/pins.toml:4); declaration: "FROM node:20.11.0-alpine"\n'
            'Dockerfile:3: python: 3.12.8 -> 3.13.2 (source config/pins.toml:[tools].python; location config/pins.toml:6); declaration: "FROM python:3.12.8-slim"\n'
            'Dockerfile:4: go: 1.23.4 -> 1.24.0 (source config/pins.toml:[tools].go; location config/pins.toml:8); declaration: "FROM golang:1.23.4"\n'
            '.nvmrc:1: node: 20.11.0 -> 22.14.0 (source config/pins.toml:[tools].node; location config/pins.toml:4); declaration: ".nvmrc"\n'
            '.python-version:1: python: 3.12.8 -> 3.13.2 (source config/pins.toml:[tools].python; location config/pins.toml:6); declaration: ".python-version"\n'
            '.go-version:1: go: 1.23.4 -> 1.24.0 (source config/pins.toml:[tools].go; location config/pins.toml:8); declaration: ".go-version"\n'
            'Drift detected.\n'
        )
        for config in configs:
            with self.subTest(config=config):
                self.write("config/pins.toml", config)
                before = self.tree()
                report = json.loads(self.run_cli("check", "--mise", "config/pins.toml", "--json", code=1).stdout)
                self.assertEqual({(d["tool"], d["sourceLine"]) for d in report["declarations"]},
                                 {("node", 4), ("python", 6), ("go", 8)})
                result = self.run_cli("check", "--mise", "config/pins.toml", code=1)
                self.assertEqual((result.stdout, result.stderr), (expected, ""))
                self.assertEqual(self.tree(), before)

    def test_human_check_unknown_source_line_is_explicit(self):
        from stackwrench.pins.cli import main
        from stackwrench.pins.core import Report

        # Simulate absent location metadata; the renderer must not consult source
        # text or borrow the declaration's target line as a mise location.
        report = Report(str(self.root), "config/pins.toml")
        report.declaration(path=".nvmrc", line=17, tool="node", kind="version-file",
                           declaration=".nvmrc", expected="22.14.0", actual="20.11.0")
        before = report.as_dict()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("stackwrench.pins.cli.inspect", return_value=report), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["check", "--root", str(self.root)]), 1)
        self.assertEqual(stdout.getvalue(),
                         '.nvmrc:17: node: 20.11.0 -> 22.14.0 '
                         '(source config/pins.toml:[tools].node; location config/pins.toml:?); '
                         'declaration: ".nvmrc"\nDrift detected.\n')
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(report.as_dict(), before)

    def test_human_check_blocked_actions_identities_follow_existing_input_precedence(self):
        self.write(".nvmrc", "20.11.0\n")
        # Supported quoted/dotted keys, comments and blank lines remain physical.
        self.write("config/pins.toml", '# source\n\ntools."node" = "22.14.0"\n# python\n"tools".python = "3.13.2"\n\n\n\'tools\'.\'go\' = "1.24.0"\n')
        args = []
        expected_rows = {}
        for shape in ("block", "inline"):
            lines = ["jobs:", "  test:", "    steps:", "      - uses: actions/checkout@v4"]
            path = f"{shape}.yml"
            for tool in ("node", "python", "go"):
                key = f"{tool}-version"
                for state, fields in (
                    ("range", [(key, "'20.x'")]),
                    ("file", [(key + "-file", "'${{ inputs.file }}'")]),
                    ("both", [(key + "-file", "'missing.txt'"), (key, "'1.2.3'")]),
                    ("empty", [(key + "-file", "'missing.txt'"), (key, "''")]),
                    ("missing", []),
                ):
                    index = sum(line.startswith("      - uses:") for line in lines)
                    lines.append(f"      - uses: actions/setup-{tool}@v4")
                    uses_line = len(lines)
                    if fields:
                        if shape == "inline":
                            lines.append("        with: {" + ", ".join(f"{k}: {v}" for k, v in fields) + "}")
                        else:
                            lines.append("        with:")
                            lines.extend(f"          {k}: {v}" for k, v in fields)
                    input_key = key + "-file" if state == "file" else key
                    line = (uses_line if not fields else len(lines) if shape == "inline" else
                            next(i for i, text in enumerate(lines, 1)
                                 if i > uses_line and text.startswith(f"          {input_key}:")))
                    identity = f"jobs.test.steps[{index}] actions/setup-{tool}@v4 with.{input_key}"
                    reason = (f"{key}-file indirection is unsupported; no version files are followed"
                              if state in ("file", "both", "empty") else
                              f"{key} requires a literal exact major.minor.patch; matrices, expressions, ranges and structured values are not evaluated")
                    expected_rows[path, line] = (identity, reason, tool,
                                                {"range": "20.x", "both": "1.2.3", "empty": ""}.get(state, "<missing>"))
            self.write(path, "\n".join(lines) + "\n")
            args.extend(("--workflow", path))
        # Structural failures and skipped unknown actions never acquire a pin identity.
        self.write("structural.yml", "jobs: [test]\n")
        args.extend(("--workflow", "structural.yml"))
        before = self.tree()
        stats = {p: (self.root / p).stat() for p in before}
        for workflows in (args, ["--workflow", "structural.yml", "--workflow", "inline.yml", "--workflow", "block.yml"]):
            selected = ["--mise", "config/pins.toml", *workflows]
            with self.subTest(selected=selected):
                report = json.loads(self.run_cli("check", "--json", *selected, code=2).stdout)
                pins = [d for d in report["declarations"] if d["kind"] == "actions-version"]
                self.assertEqual(len(pins), 30)
                for d in pins:
                    identity, reason, tool, actual = expected_rows[d["path"], d["line"]]
                    self.assertEqual((d["declaration"], d["reason"], d["tool"], d["actual"]),
                                     (identity, reason, tool, actual))
                    self.assertEqual((d["evaluated"], d["status"], d["newValue"], d["mismatch"]),
                                     (False, "blocked", None, False))
                    self.assertEqual((d["sourcePath"], d["sourceLine"]), ("config/pins.toml", {"node": 3, "python": 5, "go": 8}[tool]))
                finding_lines = []
                # Human finding order follows selection, unlike sorted JSON.
                for path in workflows[1::2]:
                    for f in report["findings"]:
                        if f["path"] != path:
                            continue
                        text = f"{path}:{f['line']}: {f['code']}: {f['message']}"
                        if (path, f["line"]) in expected_rows:
                            tool = expected_rows[path, f["line"]][2]
                            source_line = {"node": 3, "python": 5, "go": 8}[tool]
                            text += f" (source config/pins.toml:[tools].{tool}; location config/pins.toml:{source_line})"
                            text += "; declaration: " + json.dumps(expected_rows[path, f["line"]][0])
                            if f["code"] == "unsupported_action_pin":
                                text += "; value: " + json.dumps(expected_rows[path, f["line"]][3], ensure_ascii=True)
                        finding_lines.append(text + "\n")
                result = self.run_cli("check", *selected, code=2)
                self.assertEqual((result.stdout, result.stderr), ("", "".join(finding_lines)))
                plan = json.loads(self.run_cli("plan", "--json", *selected, code=2).stdout)
                self.assertEqual((plan["changedFiles"], plan["diff"], plan["applicable"]), ([], "", False))
                self.assertTrue(all(f["diff"] == "" for f in plan["files"]))
                plain = self.run_cli("plan", *selected, code=2)
                applied = self.run_cli("apply", *selected, code=2)
                expected_plain = "".join(line.split(" (source ")[0] + "\n" for line in result.stderr.splitlines())
                self.assertEqual((plain.stdout, plain.stderr), ("", expected_plain))
                self.assertEqual((applied.stdout, applied.stderr), ("", expected_plain))
        self.assertEqual(self.tree(), before)
        for path, info in stats.items():
            now = (self.root / path).stat()
            self.assertEqual((info.st_ino, info.st_mtime_ns, info.st_mode),
                             (now.st_ino, now.st_mtime_ns, now.st_mode))
        # A missing source pin is also a pin finding; both findings may share one declaration.
        self.write("mise.toml", '[tools]\nnode = "22.14.0"\n')
        self.write("missing.yml", "jobs:\n  test:\n    steps:\n      - uses: actions/setup-go@v5\n        with: {go-version: '1.x'}\n")
        result = self.run_cli("check", "--workflow", "missing.yml", code=2)
        label = ' (source mise.toml:[tools].go; location mise.toml:?); declaration: "jobs.test.steps[0] actions/setup-go@v5 with.go-version"\n'
        self.assertEqual((result.stdout, result.stderr), ("",
            "missing.yml:5: missing_pin: no supported exact [tools].go pin in mise.toml" + label +
            "missing.yml:5: unsupported_action_pin: go-version requires a literal exact major.minor.patch; matrices, expressions, ranges and structured values are not evaluated" + label[:-1] + '; value: "1.x"\n'))

    def test_human_check_pin_identity_requires_unique_exact_located_blocked_action(self):
        from stackwrench.pins.cli import main
        from stackwrench.pins.core import Report

        report = Report(str(self.root), "mise.toml")
        label = 'jobs.test.steps[0] actions/setup-node@v4 with.node-version "café"'

        def add(line, *, path="ci.yml", kind="actions-version", evaluated=False, identity=label):
            report.declaration(path=path, line=line, tool="node", kind=kind,
                               declaration=identity, expected="22.14.0", actual="20.x", evaluated=evaluated)

        # One eligible row plus ineligible colocated rows remains uniquely associated.
        add(20)
        add(20, kind="workflow")
        add(20, evaluated=True)
        add(20, kind="actions-other")
        add(10)
        add(10, identity="another declaration")
        add(11)
        add(11)  # Identical duplicate labels are still ambiguous.
        add(12, path="other.yml")
        add(14)
        add(None)
        add(16, kind="workflow")
        add(17, evaluated=True)
        add(18)
        cases = [(20, "unsupported_action_pin", True), (10, "unsupported_action_pin", False),
                 (11, "unsupported_action_pin", False), (12, "unsupported_action_pin", False),
                 (13, "unsupported_action_pin", False), (None, "unsupported_action_pin", False),
                 (16, "unsupported_action_pin", False), (17, "unsupported_action_pin", False),
                 (18, "unsupported_workflow", False), (20, "missing_pin", True),
                 (20, "io_error", False)]
        expected = ""
        for line, code, associated in cases:
            report.issue("ci.yml", code, "safety reason", line)
            expected += f"ci.yml{':' + str(line) if line is not None else ''}: {code}: safety reason"
            expected += (" (source mise.toml:[tools].node; location mise.toml:?); declaration: " +
                         json.dumps(label, ensure_ascii=True) if associated else "")
            if associated and code == "unsupported_action_pin":
                expected += "; value: " + json.dumps("20.x", ensure_ascii=True)
            expected += "\n"
        # Explicit null, like an absent line, cannot establish an association.
        report.findings.append({"path": "ci.yml", "line": None, "code": "unsupported_action_pin", "message": "null line"})
        expected += "ci.yml:None: unsupported_action_pin: null line\n"
        original = json.dumps(report.plan_dict(), sort_keys=True)
        for command in ("check", "plan", "apply"):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("stackwrench.pins.cli.inspect", return_value=report), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main([command, "--root", str(self.root)]), 2)
            self.assertEqual(stdout.getvalue(), "")
            unadorned = "".join(line.split(" (source ")[0] + "\n" for line in expected.splitlines())
            self.assertEqual(stderr.getvalue(), expected if command == "check" else unadorned)
            self.assertEqual(json.dumps(report.plan_dict(), sort_keys=True), original)

    def test_human_check_blocked_action_source_metadata_is_used_without_inference(self):
        from stackwrench.pins.cli import main
        from stackwrench.pins.core import Report

        report = Report(str(self.root), "config/pins.toml")
        # No source lines loaded: a target line must never become a source line.
        for line, tool in ((17, "node"), (18, "python"), (19, "go")):
            report.declaration(path="ci.yml", line=line, tool=tool, kind="actions-version",
                               declaration=f"with.{tool}-version", expected=None,
                               actual="<missing>", evaluated=False)
            report.issue("ci.yml", "missing_pin", "missing source pin", line)
        # Use each row's own existing metadata, not report.mise or source_lines.
        report.declarations[1].update(source="other.toml:[tools].python", sourcePath="other.toml", sourceLine=42)
        report.declarations[2].update(source=None, sourcePath=None)
        original = json.dumps(report.plan_dict(), sort_keys=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("stackwrench.pins.cli.inspect", return_value=report), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["check", "--root", str(self.root)]), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(),
            'ci.yml:17: missing_pin: missing source pin (source config/pins.toml:[tools].node; location config/pins.toml:?); declaration: "with.node-version"\n'
            'ci.yml:18: missing_pin: missing source pin (source other.toml:[tools].python; location other.toml:42); declaration: "with.python-version"\n'
            'ci.yml:19: missing_pin: missing source pin; declaration: "with.go-version"\n')
        self.assertEqual(json.dumps(report.plan_dict(), sort_keys=True), original)

    def test_human_check_clean_and_blocked_contracts_stay_exact(self):
        self.write(".nvmrc", "22.14.0\n")
        self.write("valid.yml", "jobs:\n  test:\n    steps:\n"
                   "      - uses: actions/setup-node@v4\n        with: {node-version: '22.14.0'}\n"
                   "      - uses: actions/checkout@v4\n")
        clean = self.run_cli("check", "--workflow", "valid.yml")
        self.assertEqual((clean.stdout, clean.stderr), ("All evaluated declarations match.\n", ""))
        self.write(".nvmrc", "20.11.0\n")
        for refusal in ("jobs:\n  test:\n    steps:\n      - uses: actions/setup-go@v5\n"
                        "        with: {go-version-file: '${{ inputs.file }}'}\n", "jobs: [test]\n"):
            with self.subTest(refusal=refusal):
                self.write("refusal.yml", refusal)
                args = ("--workflow", "valid.yml", "--workflow", "refusal.yml")
                before = self.tree()
                stats = {p: (self.root / p).stat() for p in before}
                report = json.loads(self.run_cli("check", "--json", *args, code=2).stdout)
                expected = "".join(f"{f['path']}:{f['line']}: {f['code']}: {f['message']}\n"
                                   for f in report["findings"])
                if report["findings"][0]["code"] == "unsupported_action_pin":
                    expected = expected.rstrip("\n") + ' (source mise.toml:[tools].go; location mise.toml:4); declaration: "jobs.test.steps[0] actions/setup-go@v5 with.go-version-file"; value: "<missing>"\n'
                blocked = self.run_cli("check", *args, code=2)
                self.assertEqual((blocked.stdout, blocked.stderr), ("", expected))
                plan = json.loads(self.run_cli("plan", "--json", *args, code=2).stdout)
                self.assertEqual((plan["changedFiles"], plan["diff"], plan["applicable"]), ([], "", False))
                self.assertTrue(all(f["diff"] == "" for f in plan["files"]))
                self.assertEqual(self.run_cli("apply", *args, code=2).stdout, "")
                self.assertEqual(self.tree(), before)
                for path, info in stats.items():
                    now = (self.root / path).stat()
                    self.assertEqual((info.st_ino, info.st_mtime_ns, info.st_mode),
                                     (now.st_ino, now.st_mtime_ns, now.st_mode))

    def test_human_check_blocked_runtime_and_docker_pin_navigation(self):
        pins = '# decoy node="0.0.0"\n\ntools."node" = "22.14.0"\n# comment\n"tools".python = "3.13.2"\n\ntools.go = "1.24.0"\n'
        docker = ['# image checks']
        for image in ('node', 'python', 'golang'):
            docker.extend([f'FROM {image}@sha256:' + 'a' * 64,
                           f'FROM {image}:${{VERSION}}', f'FROM {image}:latest',
                           f'FROM {image}:1.2.3'])
        docker.extend(['FROM ubuntu:24.04', 'FROM ${BASE}', 'FROM other@sha256:abc',
                       'FROM scratch', 'FROM node:latest AS same', 'FROM node:latest AS same',
                       'FROM node:1.2.3 extra words'])
        self.write('Dockerfile', '\n'.join(docker) + '\n')
        for name in ('.node-version', '.python-version', '.go-version'):
            self.write(name, 'latest\r\n')
        self.write('.nvmrc', '20.11.0\n')  # Valid drift must remain suppressed.
        self.write('unknown-version', '1.2.3\n')
        self.write('ci.yml', "jobs:\n  test:\n    steps:\n      - uses: actions/setup-node@v4\n        with: {node-version: '22.x'}\n")
        versions = ['.go-version', '.python-version', '.node-version', '.nvmrc', 'unknown-version', 'absent/.nvmrc']
        for config in (pins, 'tools.node = "22.14.0"\n', 'tools.python = "3.13.2"\n',
                       pins.replace('22.14.0', '22.x')):
            self.write('config/pins.toml', config)
            for paths in (versions, list(reversed(versions))):
                with self.subTest(config=config, paths=paths):
                    args = ['--mise', 'config/pins.toml', '--workflow', 'ci.yml']
                    args += [arg for path in paths for arg in ('--version-file', path)]
                    before = self.tree()
                    report = json.loads(self.run_cli('check', '--json', *args, code=2).stdout)
                    expected = []
                    # Findings keep the source, Docker, selected runtime, workflow order.
                    for path in ['config/pins.toml', 'Dockerfile', *paths, 'ci.yml']:
                        for f in sorted((f for f in report['findings'] if f['path'] == path),
                                        key=lambda f: (f.get('line', 0), f['code'] == 'duplicate_stage')):
                            text = path + (f":{f['line']}" if 'line' in f else '') + f": {f['code']}: {f['message']}"
                            if f['code'] in ('missing_pin', 'unsupported_pin', 'digest_pinned', 'dynamic_image', 'unsupported_image', 'unsupported_action_pin') and path != 'config/pins.toml':
                                row, = [d for d in report['declarations'] if d['path'] == path and d['line'] == f['line']]
                                self.assertEqual((row['status'], row['newValue']), ('blocked', None))
                                if path == 'Dockerfile':
                                    self.assertEqual(row['declaration'], docker[f['line'] - 1])
                                if row['tool'] is None:
                                    self.assertIsNone(row['source'])
                                    self.assertIsNone(row['sourcePath'])
                                else:
                                    self.assertEqual(row['source'], f"config/pins.toml:[tools].{row['tool']}")
                                    if config == pins:
                                        self.assertEqual(row['sourceLine'], {'node': 3, 'python': 5, 'go': 7}[row['tool']])
                                    source_line = row['sourceLine'] if row['sourceLine'] is not None else '?'
                                    text += f" (source {row['source']}; location {row['sourcePath']}:{source_line})"
                                text += '; declaration: ' + json.dumps(row['declaration'], ensure_ascii=True)
                                if f['code'] in ('unsupported_pin', 'unsupported_action_pin'):
                                    text += '; value: ' + json.dumps(row['oldValue'], ensure_ascii=True)
                            elif f['code'] == 'unsupported_pin':
                                row, = [d for d in report['declarations'] if d['path'] == path and d['line'] == f['line']]
                                self.assertEqual((row['kind'], row['status']), ('mise', 'blocked'))
                                text += '; value: ' + json.dumps(row['oldValue'], ensure_ascii=True)
                            expected.append(text + '\n')
                    result = self.run_cli('check', *args, code=2)
                    self.assertEqual((result.stdout, result.stderr), ('', ''.join(expected)))
                    plan = json.loads(self.run_cli('plan', '--json', *args, code=2).stdout)
                    self.assertEqual((plan['changedFiles'], plan['diff'], plan['applicable']), ([], '', False))
                    self.assertTrue(all(f['diff'] == '' for f in plan['files']))
                    self.assertEqual((plan['declarations'], plan['findings']), (report['declarations'], report['findings']))
                    self.run_cli('apply', *args, code=2)
                    self.assertEqual(self.tree(), before)

    def test_human_check_runtime_pin_association_and_metadata_boundaries(self):
        from stackwrench.pins.cli import main
        from stackwrench.pins.core import Report

        report = Report(str(self.root), 'config/pins.toml')
        expected, plain = [], []
        line = 10
        for kind, codes in [('version-file', ('unsupported_pin', 'missing_pin')),
                            ('docker-image', ('digest_pinned', 'dynamic_image', 'unsupported_image', 'missing_pin'))]:
            for code in codes:
                for state in ('unique', 'duplicate', 'duplicate-label', 'other-path', 'other-line',
                              'null-line', 'absent-line', 'wrong-kind', 'match', 'drift',
                              'wrong-code', 'no-tool', 'no-source', 'no-source-path', 'own-source'):
                    with self.subTest(kind=kind, code=code, state=state):
                        line += 1
                        label = f'{kind} "café" {state}'
                        row_args = dict(path='target', line=line, tool='node', kind=kind,
                                        declaration=label, actual='latest', expected='22.14.0', evaluated=False)
                        if state == 'other-path':
                            row_args['path'] = 'other'
                        if state == 'other-line':
                            row_args['line'] += 1000
                        if state in ('null-line', 'absent-line'):
                            row_args['line'] = None
                        if state == 'wrong-kind':
                            row_args['kind'] = 'mise'
                        if state in ('match', 'drift'):
                            row_args.update(evaluated=True, actual='22.14.0' if state == 'match' else '1.2.3')
                        if state == 'no-tool':
                            row_args['tool'] = None
                        report.declaration(**row_args)
                        row = report.declarations[-1]
                        if state == 'no-source':
                            row['source'] = None
                        if state == 'no-source-path':
                            row['sourcePath'] = None
                        if state == 'own-source':
                            row.update(source='other.toml:[tools].node', sourcePath='other.toml', sourceLine=42)
                        if state.startswith('duplicate'):
                            report.declaration(**(row_args | {'declaration': label if state == 'duplicate-label' else 'different'}))
                        # Colocated structural and non-blocked rows cannot become candidates.
                        if state == 'unique':
                            report.declaration(**(row_args | {'kind': 'workflow'}))
                            report.declaration(**(row_args | {'evaluated': True}))
                        finding_code = 'unsupported_from' if state == 'wrong-code' else code
                        report.issue('target', finding_code, 'original reason', None if state == 'absent-line' else line)
                        if state == 'null-line':
                            report.findings[-1]['line'] = None
                        finding = report.findings[-1]
                        text = 'target' + (f":{finding['line']}" if 'line' in finding else '') + f': {finding_code}: original reason'
                        plain.append(text + '\n')
                        if state in ('unique', 'no-tool', 'no-source', 'no-source-path', 'own-source'):
                            if state in ('unique', 'own-source'):
                                text += (' (source other.toml:[tools].node; location other.toml:42)' if state == 'own-source' else
                                         ' (source config/pins.toml:[tools].node; location config/pins.toml:?)')
                            text += '; declaration: ' + json.dumps(label, ensure_ascii=True)
                            if finding_code == 'unsupported_pin':
                                text += '; value: ' + json.dumps('latest', ensure_ascii=True)
                        elif state == 'wrong-kind' and finding_code == 'unsupported_pin':
                            # A colocated mise row is an allowed unsupported_pin kind: value only, never source or identity.
                            text += '; value: ' + json.dumps('latest', ensure_ascii=True)
                        expected.append(text + '\n')
        original = json.dumps(report.plan_dict(), sort_keys=True)
        for command in ('check', 'plan', 'apply'):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('stackwrench.pins.cli.inspect', return_value=report), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main([command, '--root', str(self.root)]), 2)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''.join(expected if command == 'check' else plain)))
            self.assertEqual(json.dumps(report.plan_dict(), sort_keys=True), original)

    def test_human_check_rejected_values_use_existing_strings_at_unique_locations(self):
        from stackwrench.pins.cli import main
        from stackwrench.pins.core import Report

        for kind, code in [('mise', 'unsupported_pin'), ('version-file', 'unsupported_pin'),
                           ('actions-version', 'unsupported_action_pin')]:
            for state in ('unique', 'empty', 'sentinel', 'duplicate', 'duplicate-label',
                          'duplicate-nonstring', 'other-path', 'other-line', 'null-line',
                          'absent-line', 'no-row', 'wrong-kind', 'wrong-code', 'match', 'drift',
                          'null-value', 'absent-value', 'typed-value', 'no-source'):
                with self.subTest(kind=kind, state=state):
                    report = Report(str(self.root), 'mise.toml')
                    value = {'empty': '', 'sentinel': '<missing>'}.get(state, 'old "café"\\path\n\t\r\x01')
                    row_args = dict(path='pin', line=7, kind=kind, tool='node', declaration='identity',
                                    expected='22.14.0', actual='do not use actual', evaluated=False)
                    if state == 'other-path':
                        row_args['path'] = 'elsewhere'
                    if state == 'other-line':
                        row_args['line'] = 8
                    if state in ('null-line', 'absent-line'):
                        row_args['line'] = None
                    if state == 'wrong-kind':
                        row_args['kind'] = 'workflow'
                    if state in ('match', 'drift'):
                        row_args.update(evaluated=True, actual='22.14.0' if state == 'match' else '1.2.3')
                    if state != 'no-row':
                        report.declaration(**row_args)
                        row = report.declarations[-1]
                        row['oldValue'] = None if state == 'null-value' else ['typed'] if state == 'typed-value' else value
                        if state == 'absent-value':
                            del row['oldValue']
                        if state == 'no-source':
                            row.update(source=None, sourcePath=None)
                        if state.startswith('duplicate'):
                            report.declaration(**(row_args | {'declaration': 'identity' if state == 'duplicate-label' else 'other'}))
                            if state == 'duplicate-nonstring':
                                report.declarations[-1]['oldValue'] = None
                        # Ineligible colocated rows cannot cause ambiguity.
                        if state == 'unique':
                            report.declaration(**(row_args | {'kind': 'workflow'}))
                            report.declaration(**(row_args | {'evaluated': True}))
                    finding_code = 'invalid_input' if state == 'wrong-code' else code
                    report.issue('pin', finding_code, 'original reason', None if state == 'absent-line' else 7)
                    if state == 'null-line':
                        report.findings[-1]['line'] = None
                    base = ('pin' + (':None' if state == 'null-line' else '' if state == 'absent-line' else ':7') +
                            f': {finding_code}: original reason')
                    associated = state in ('unique', 'empty', 'sentinel', 'null-value', 'absent-value', 'typed-value', 'no-source')
                    expected = base
                    if associated and kind != 'mise':
                        if state != 'no-source':
                            expected += ' (source mise.toml:[tools].node; location mise.toml:?)'
                        expected += '; declaration: "identity"'
                    if state in ('unique', 'empty', 'sentinel', 'no-source'):
                        expected += '; value: ' + json.dumps(value, ensure_ascii=True)
                    original = json.dumps(report.as_dict(), sort_keys=True)
                    for command in ('check', 'plan', 'apply'):
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with patch('stackwrench.pins.cli.inspect', return_value=report), redirect_stdout(stdout), redirect_stderr(stderr):
                            self.assertEqual(main([command, '--root', str(self.root)]), 2)
                        self.assertEqual((stdout.getvalue(), stderr.getvalue()),
                                         ('', (expected if command == 'check' else base) + '\n'))
                        self.assertEqual(json.dumps(report.as_dict(), sort_keys=True), original)

    def test_human_check_rejected_values_cover_sources_runtimes_and_actions(self):
        for source in (PINS, 'tools.node = "latest"\ntools.python = ["3.13.2"]\ntools.go = {version = "1.24.0"}\n',
                       'tools.node = ""\ntools.python = "café\\n\\t\\\""\ntools.go = 123\n'):
            with self.subTest(source=source):
                self.write('mise.toml', source)
                for name, value in [('.node-version', 'latest\r\n'), ('.python-version', '3.x\n'),
                                    ('.go-version', '1.24'), ('.nvmrc', '"café"\nnext\tline\n')]:
                    self.write(name, value)
                args = []
                for shape in ('block', 'inline'):
                    lines = ['jobs:', '  test:', '    steps:']
                    for tool in ('node', 'python', 'go'):
                        key = tool + '-version'
                        for fields in [[(key, "'3.x'")], [(key, "'${{ matrix.version }}'")],
                                       [(key, "''")], [(key + '-file', "'never-read.txt'")], [],
                                       [(key, "'café\"'")], [(key + '-file', "'never-read.txt'"), (key, "'1.2.3'")]]:
                            lines.append(f'      - uses: actions/setup-{tool}@v5')
                            if fields:
                                if shape == 'inline':
                                    lines.append('        with: {' + ', '.join(f'{k}: {v}' for k, v in fields) + '}')
                                else:
                                    lines.append('        with:')
                                    lines.extend(f'          {k}: {v}' for k, v in fields)
                        if shape == 'block':
                            for value in ('[3.12.0]', '|\n            opaque text', '\n            nested: value', '\n            - 3.12.0'):
                                lines.extend([f'      - uses: actions/setup-{tool}@v5', '        with:', f'          {key}: {value}'])
                    self.write(shape + '.yml', '\n'.join(lines) + '\n')
                    args += ['--workflow', shape + '.yml']
                self.write('bad.yml', 'jobs: [test]\n')
                self.write('bad/.node-version', b'\x00')
                args += ['--workflow', 'bad.yml', '--version-file', '.node-version', '--version-file', '.python-version',
                         '--version-file', '.go-version', '--version-file', '.nvmrc', '--version-file', 'bad/.node-version',
                         '--version-file', 'missing/.nvmrc']
                report = json.loads(self.run_cli('check', '--json', *args, code=2).stdout)
                result = self.run_cli('check', *args, code=2)
                self.assertEqual(result.stdout, '')
                lines = result.stderr.splitlines()
                self.assertEqual(len(lines), len(report['findings']))
                for f in report['findings']:
                    prefix = f['path'] + (f":{f['line']}" if 'line' in f else '') + f": {f['code']}: {f['message']}"
                    line, = [line for line in lines if line.startswith(prefix)]
                    if f['code'] in ('unsupported_pin', 'unsupported_action_pin'):
                        d, = [d for d in report['declarations'] if (d['path'], d['line']) == (f['path'], f['line'])]
                        self.assertEqual(d['status'], 'blocked')
                        self.assertTrue(line.endswith('; value: ' + json.dumps(d['oldValue'], ensure_ascii=True)), line)
                        if d['kind'] == 'mise':
                            self.assertEqual(line, prefix + '; value: ' + json.dumps(d['oldValue'], ensure_ascii=True))
                    else:
                        self.assertNotIn('; value:', line)
                # A file-only row exposes the existing sentinel, never the indirect path/content.
                self.assertIn('; value: "<missing>"', result.stderr)
                self.assertFalse((self.root / 'never-read.txt').exists())

    def test_human_check_and_clean_plan(self):
        self.write(".nvmrc", "20.11.0\n")
        self.assertIn("20.11.0 -> 22.14.0", self.run_cli("check", code=1).stdout)
        self.run_cli("apply")
        self.assertIn("match", self.run_cli("check").stdout)
        self.assertEqual(self.run_cli("plan").stdout, "")


if __name__ == "__main__":
    unittest.main()
