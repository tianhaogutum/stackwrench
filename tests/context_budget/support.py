"""Shared helpers for running ``stackwrench context-budget`` in a subprocess."""

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
CLI = [sys.executable, "-m", "stackwrench", "context-budget"]
# Make the source checkout importable even when the subprocess runs from a temporary cwd.
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(
    part for part in (str(SRC), os.environ.get("PYTHONPATH", "")) if part)}
