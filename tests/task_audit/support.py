"""Shared helpers for running ``stackwrench task-audit`` in a subprocess."""

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "task_audit"
CLI = [sys.executable, "-m", "stackwrench", "task-audit"]


def cli_env(env=None):
    """Return ``env`` (default: the current environment) with the source checkout importable."""
    result = dict(os.environ if env is None else env)
    result["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "src"), result.get("PYTHONPATH", "")) if part)
    return result
