from __future__ import annotations

import shutil
import tempfile
from typing import Callable, Iterator

import pytest


@pytest.fixture
def mkdtemp() -> Iterator[Callable[[str], str]]:
    """``fs.mkdtempSync(path.join(os.tmpdir(), prefix))`` with cleanup after the test."""
    made: list[str] = []

    def make(prefix: str) -> str:
        root = tempfile.mkdtemp(prefix=prefix, dir=tempfile.gettempdir())
        made.append(root)
        return root

    yield make
    for root in made:
        shutil.rmtree(root, ignore_errors=True)
