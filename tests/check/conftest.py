"""Shared helpers for the ``stackwrench check`` tests. Every repository here is synthetic."""
import json
from pathlib import Path
import shutil
import sys
import textwrap

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stackwrench.check import cli  # noqa: E402

DEMO = Path(__file__).resolve().parents[2] / "examples" / "check"


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A private copy of examples/check, with the working directory inside it."""
    target = tmp_path / "demo"
    shutil.copytree(DEMO, target)
    monkeypatch.chdir(target)
    return target


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An empty repository folder; ``repo.write(path, text)`` adds a dedented file."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)

    class Repo:
        path = root

        def write(self, name: str, text: str = "") -> Path:
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
            return file

    return Repo()


@pytest.fixture
def check(capsys):
    """Run ``stackwrench check`` in-process; return (exit code, stdout, stderr)."""
    def run(*argv):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:  # argparse usage errors and --help
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


@pytest.fixture
def check_json(check):
    """Run with --format json and return (exit code, parsed document)."""
    def run(*argv):
        code, out, err = check(*argv, "--format", "json")
        assert err == "", err
        return code, json.loads(out)
    return run


def rule_ids(document, file=None):
    return sorted(f["ruleId"] for f in document["findings"] if file is None or f["file"] == file)


def findings(document, rule_id):
    return [f for f in document["findings"] if f["ruleId"] == rule_id]
