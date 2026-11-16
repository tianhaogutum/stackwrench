"""Optional real runner comparison. Never downloads runners or reads user files."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

JUST = shutil.which("just")
TASK = shutil.which("task")
CLI = [sys.executable, "-m", "stackwrench", "just"]


@unittest.skipUnless(JUST and TASK, "optional live comparison: just and/or task not installed; no download attempted")
class LiveRunnerTests(unittest.TestCase):
    def test_order_dedup_shell_semantics_literals_and_files(self):
        source = '''value := '$HOME $(printf untouched)'
# Synthetic ordered build
build: left right
    printf '%s\\n' '{{value}}'
    export JTB_LOCAL=value
    printf '%s\\n' "${JTB_LOCAL-unset}"
    false; printf '%s\\n' 'no implicit errexit'
    printf '%s\\n' '🤖'
    printf '%s\\n' 'build' >> trace.txt
left: shared
    printf '%s\\n' 'left'
    printf '%s\\n' 'left' >> trace.txt
right: shared
    printf '%s\\n' 'right'
    printf '%s\\n' 'right' >> trace.txt
shared:
    printf '%s\\n' 'shared'
    printf '%s\\n' 'shared' >> trace.txt
'''
        with tempfile.TemporaryDirectory(prefix="jtb-live-") as temp:
            outputs = []
            traces = []
            for runner in ("just", "task"):
                directory = Path(temp) / runner
                directory.mkdir()
                justfile = directory / "justfile"
                justfile.write_text(source, encoding="utf-8")
                if runner == "task":
                    converted = subprocess.run([*CLI, "convert", str(justfile), "-o", str(directory / "Taskfile.yml")],
                                               capture_output=True, text=True, timeout=10)
                    self.assertEqual(converted.returncode, 0, converted.stderr)
                    args = [TASK, "--silent", "--taskfile", "Taskfile.yml"]
                else:
                    args = [JUST, "--justfile", "justfile"]
                actual = subprocess.run(args, cwd=directory, capture_output=True, text=True, timeout=15,
                                        env={"PATH": os.environ.get("PATH", os.defpath),
                                             "HOME": str(directory), "XDG_CONFIG_HOME": str(directory),
                                             "LANG": "C.UTF-8"})
                self.assertEqual(actual.returncode, 0, actual.stderr)
                outputs.append(actual.stdout)
                traces.append((directory / "trace.txt").read_text())
            self.assertEqual(outputs[0], "shared\nleft\nright\n$HOME $(printf untouched)\nunset\nno implicit errexit\n🤖\n")
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(traces, ["shared\nleft\nright\nbuild\n"] * 2)

    def test_installed_acceptance_command(self):
        with tempfile.TemporaryDirectory(prefix="jtb-live-verify-") as temp:
            actual = subprocess.run([*CLI, "verify"], cwd=temp,
                                    capture_output=True, text=True, timeout=130)
        self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)
        report = json.loads(actual.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["suiteVersion"], 2)
        self.assertEqual([case["name"] for case in report["cases"]],
                         ["ordered-diamond", "dependency-failure", "unset-variable-failure"])
        self.assertTrue(all(case["status"] == "passed" for case in report["cases"]))
