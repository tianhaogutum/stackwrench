"""Opt-in comparison of built-in fixtures only, using preinstalled POSIX runners.

No source path or command input is accepted. This is an acceptance probe, not a
sandbox or a verifier for arbitrary repositories. Mocked test processes never
constitute evidence of live runner equivalence.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import __version__
from .core import analyze, render_taskfile

EXIT_CODES = {"passed": 0, "failed": 1, "error": 3, "skipped": 4}
TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class Fixture:
    name: str
    source: str
    entrypoint: str | None
    stdout: str
    exit_code: int | None  # None requires a positive shell-specific failure code.
    order: tuple[str, ...]


# These are literal, bounded shell builtins, with writes only to trace.txt in
# disposable fixture directories. Expected observations are independent of the
# converter's computed graph/traces, so two equally wrong runners cannot pass.
FIXTURES = (
    Fixture("ordered-diamond", r"""value := '$HOME $(printf untouched)'
root: left right left
    printf '%s\n' '{{value}}'
    export JTB_LOCAL=value
    printf '%s\n' "${JTB_LOCAL-unset}"
    false; printf '%s\n' 'no implicit errexit'
    printf '%s\n' '🤖'
    printf '%s\n' 'root'
    printf '%s\n' 'root' >> trace.txt
left: shared
    printf '%s\n' 'left'
    printf '%s\n' 'left' >> trace.txt
right: shared
    printf '%s\n' 'right'
    printf '%s\n' 'right' >> trace.txt
shared:
    printf '%s\n' 'shared'
    printf '%s\n' 'shared' >> trace.txt
""", None,
            "shared\nleft\nright\n$HOME $(printf untouched)\nunset\nno implicit errexit\n🤖\nroot\n",
            0, ("shared", "left", "right", "root")),
    Fixture("dependency-failure", r"""root: stop tail
    printf '%s\n' 'root'
    printf '%s\n' 'root' >> trace.txt
stop: shared
    printf '%s\n' 'stop'
    printf '%s\n' 'stop' >> trace.txt
    exit 7
    printf '%s\n' 'unreachable' >> trace.txt
tail:
    printf '%s\n' 'tail'
    printf '%s\n' 'tail' >> trace.txt
shared:
    printf '%s\n' 'shared'
    printf '%s\n' 'shared' >> trace.txt
""", "root", "shared\nstop\n", 7, ("shared", "stop")),
    Fixture("unset-variable-failure", r"""root: stop tail
    printf '%s\n' 'root'
    printf '%s\n' 'root' >> trace.txt
stop: shared
    printf '%s\n' 'stop'
    printf '%s\n' 'stop' >> trace.txt
    unset JTB_UNSET_055; printf '%s\n' "$JTB_UNSET_055"; printf '%s\n' 'after-expansion'; printf '%s\n' 'after-expansion' >> trace.txt
    printf '%s\n' 'later-line'
    printf '%s\n' 'later-line' >> trace.txt
tail:
    printf '%s\n' 'tail'
    printf '%s\n' 'tail' >> trace.txt
shared:
    printf '%s\n' 'shared'
    printf '%s\n' 'shared' >> trace.txt
""", "root", "shared\nstop\n", None, ("shared", "stop")),
)


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _run(argv: list[str], directory: Path, environment: dict[str, str]) -> dict:
    stdout, stderr, code, error = b"", b"", None, None
    try:
        result = subprocess.run(argv, cwd=directory, env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=TIMEOUT_SECONDS)
        stdout, stderr, code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = exc.stdout or b"", exc.stderr or b""
        error = f"runner timed out after {TIMEOUT_SECONDS} seconds"
    except OSError as exc:
        error = str(exc)
    return {
        "argv": argv, "exitCode": code,
        "stdout": stdout.decode("utf-8", errors="backslashreplace"),
        "stdoutBase64": _base64(stdout),
        "stderr": stderr.decode("utf-8", errors="backslashreplace"),
        "stderrBase64": _base64(stderr),
        "error": error,
    }


def _checks(case: dict) -> dict:
    just, task = (case["observations"][runner] for runner in ("just", "task"))
    expected = case["expected"]
    return {
        "stdoutEqual": just["stdoutBase64"] == task["stdoutBase64"],
        "exitCodeEqual": just["exitCode"] == task["exitCode"],
        "orderEqual": just["traceBase64"] is not None and just["traceBase64"] == task["traceBase64"],
        "expectedStdout": all(o["stdoutBase64"] == _base64(expected["stdout"].encode("utf-8"))
                              for o in (just, task)),
        "expectedExitCode": all(
            (o["exitCode"] is not None and o["exitCode"] > 0)
            if expected["exitCodePolicy"] == "nonzero" else o["exitCode"] == expected["exitCode"]
            for o in (just, task)),
        "expectedOrder": all(o["traceBase64"] == _base64("".join(n + "\n" for n in expected["order"]).encode())
                             for o in (just, task)),
    }


def verify() -> dict:
    """Return a v1 report; missing tools and execution errors never pass."""
    paths = {name: shutil.which(name) for name in ("just", "task")}
    paths["sh"] = shutil.which("sh", path=os.defpath)
    report = {
        "formatVersion": 1, "suiteVersion": 2, "toolVersion": __version__,
        "status": "skipped", "reason": None,
        "assumptions": {
            "scope": "built-in synthetic fixtures only; no user repository commands",
            "shell": "same POSIX sh from sanitized PATH, each command in sh -cu",
            "shellPath": str(Path(paths["sh"]).resolve()) if paths["sh"] else None,
            "environment": "identical allowlisted environment; empty temporary HOME/XDG directories; no inherited JUST_*/TASK_* variables",
            "workingDirectory": "separate fresh directories with identical fixture content, removed after verification",
            "taskExitCode": "--exit-code preserves the failing shell exit code; default Task error codes are not compared",
            "stderr": "captured for diagnosis, not compared (runner echo/error formatting differs)",
            "timeoutSecondsPerProcess": TIMEOUT_SECONDS,
            "sideEffects": "only temporary fixture/config/cache files; no downloads or external fixture commands",
        },
        "runners": {name: {"path": os.path.abspath(paths[name]) if paths[name] else None,
                           "version": None} for name in ("just", "task")},
        "cases": [{"name": f.name, "source": f.source, "entrypoint": f.entrypoint,
                   "expected": {"stdout": f.stdout, "exitCode": f.exit_code,
                                "exitCodePolicy": "nonzero" if f.exit_code is None else "exact",
                                "order": list(f.order)},
                   "status": "skipped", "reason": None, "checks": None, "observations": {}}
                  for f in FIXTURES],
    }
    missing = [name for name, path in paths.items() if path is None]
    if missing or os.name != "posix":
        report["reason"] = "unverifiable: missing " + ", ".join(missing) if missing else "unverifiable: POSIX host required"
        for case in report["cases"]:
            case["reason"] = report["reason"]
        return report
    try:
        return _verify_installed(report)
    except OSError as exc:
        report.update(status="error", reason=f"unverifiable: fixture I/O failed: {exc}")
        for case in report["cases"]:
            if case["status"] == "skipped":
                case.update(status="error", reason=report["reason"], checks=None)
        return report


def _verify_installed(report: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="jtb-verify-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        environment = {"PATH": os.defpath, "HOME": str(home),
                       "XDG_CONFIG_HOME": str(home), "XDG_CACHE_HOME": str(home),
                       "XDG_DATA_HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        report["assumptions"]["environmentValues"] = environment
        for runner, info in report["runners"].items():
            info["version"] = _run([info["path"], "--version"], root, environment)
        if any(info["version"]["error"] or info["version"]["exitCode"] != 0
               for info in report["runners"].values()):
            report["status"] = "error"
            report["reason"] = "unverifiable: runner --version probe failed"
            for case in report["cases"]:
                case.update(status="error", reason=report["reason"])
            return report
        for fixture, case in zip(FIXTURES, report["cases"]):
            taskfile = render_taskfile(analyze(fixture.source, f"builtin:{fixture.name}"))
            case["taskfile"] = taskfile
            for runner, info in report["runners"].items():
                directory = root / fixture.name / runner
                directory.mkdir(parents=True)
                (directory / "justfile").write_text(fixture.source, encoding="utf-8")
                (directory / "Taskfile.yml").write_text(taskfile, encoding="utf-8")
                flags = (["--no-dotenv", "--justfile", "justfile"] if runner == "just"
                         else ["--silent", "--exit-code", "--taskfile", "Taskfile.yml"])
                argv = [info["path"], *flags, *([fixture.entrypoint] if fixture.entrypoint else [])]
                observation = _run(argv, directory, environment)
                trace_path = directory / "trace.txt"
                trace = trace_path.read_bytes() if trace_path.exists() else None
                observation["traceBase64"] = _base64(trace) if trace is not None else None
                observation["order"] = trace.decode("utf-8", errors="backslashreplace").splitlines() if trace is not None else None
                case["observations"][runner] = observation
            if any(o["error"] for o in case["observations"].values()):
                case.update(status="error", reason="unverifiable: runner execution failed")
            else:
                case["checks"] = _checks(case)
                case["status"] = "passed" if all(case["checks"].values()) else "failed"
                if case["status"] == "failed":
                    case["reason"] = "observable output, exit code or dependency order differs from the fixture contract"
        statuses = {case["status"] for case in report["cases"]}
        report["status"] = "error" if "error" in statuses else "failed" if "failed" in statuses else "passed"
        report["reason"] = None if report["status"] == "passed" else "inspect per-case observations and checks"
    return report
