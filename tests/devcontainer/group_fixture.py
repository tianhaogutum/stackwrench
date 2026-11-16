"""Shared fixture for the nocopy, subpath, propagation and create_host_path tests.

The four original test files each defined this fixture with only the mount
template, the top-level ``volumes`` section and the pointer prefix rule
differing; those are parameters here.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable

from .helpers import TMP, Result, entries, js_stringify, line_column, run_cli, write

ABSENT: Any = object()
POINTER = "/services/web/volumes/0"


def _yaml_value(value: Any, indent: int) -> str:
    # Block-style nested YAML has its own token coordinates, independent of JSON.
    if isinstance(value, dict) and value:
        return "\n" + "\n".join(f"{' ' * indent}{k}: {js_stringify(v)}" for k, v in entries(value))
    return " " + js_stringify(value)


class GroupFixture:
    def __init__(
        self,
        mkdtemp: Any,
        prefix: str,
        kind: str,
        mount: dict[str, Any],
        top_volumes: bool,
        group_of: Callable[[str, dict[str, Any]], str],
    ) -> None:
        self.kind = kind
        self.root = mkdtemp(prefix)
        self.config = os.path.join(self.root, "devcontainer.json")
        self.document = os.path.join(self.root, "compose.yaml" if kind == "yaml" else "final.json")
        self.pointer = POINTER
        self.mount = mount = {k: v for k, v in mount.items() if v is not ABSENT}
        self.group_of = group_of
        model: dict[str, Any] = {"services": {"web": {"volumes": [mount]}}}
        if top_volumes:
            model["volumes"] = {"cache": {}}
        if kind == "yaml":
            self.text = (
                "services:\n  web:\n    volumes:\n      - "
                + "\n        ".join(f"{k}:{_yaml_value(v, 10)}" for k, v in entries(mount))
                + ("\nvolumes:\n  cache: {}\n" if top_volumes else "\n")
            )
        else:
            self.text = js_stringify(model, 2) + "\n"
        write(self.config, js_stringify({"dockerComposeFile": "compose.yaml", "service": "web", "workspaceFolder": "/work"}))
        if kind == "snapshot":
            write(os.path.join(self.root, "compose.yaml"), "not parsed: [")
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if kind == "snapshot":
            self.options["composeJson"] = self.document

    def at(self, field: str) -> dict[str, Any]:
        token = f"{field if self.kind == 'yaml' else js_stringify(field)}:"
        assert token in self.text
        end = self.text.find(token) + len(token)
        offset = end + len(re.match(r"\s*", self.text[end:]).group(0))
        return {"file": self.document, "pointer": f"{self.pointer}/{self.group_of(field, self.mount)}{field}", **line_column(self.text, offset)}

    def run(self, *args: str) -> Result:
        r = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.kind == "snapshot" else []), *args],
            cwd=TMP,
        )
        assert r.stderr == ""
        return r


def short_option_text(kind: str, scalar: str, top_volumes: bool) -> str:
    """Text of the ``*-like short mount options`` tests."""
    if kind == "yaml":
        return f'services:\n  web:\n    volumes:\n      - "{scalar}"\n' + ("volumes:\n  cache: {}\n" if top_volumes else "")
    model: dict[str, Any] = {"services": {"web": {"volumes": [scalar]}}}
    if top_volumes:
        model["volumes"] = {"cache": {}}
    return js_stringify(model, 2)
