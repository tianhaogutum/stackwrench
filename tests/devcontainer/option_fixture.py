"""Shared fixture for the unknown-option and structural-option location tests.

Both original test files defined the same fixture (the structural one adds a
``style`` switch for flow-style YAML groups); it lives here once.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from stackwrench.devcontainer import check, to_sarif

from .helpers import TMP, UNDEFINED, Result, assert_valid_sarif, entries, find, js_stringify, line_column, read, run_cli, write


def escape(key: Any) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def yaml_key(key: str) -> str:
    return key if re.fullmatch(r"[a-z_]+", key) else js_stringify(key)


class OptionFixture:
    def __init__(self, mkdtemp: Any, prefix: str, kind: str, mount: Any, service: str = "web", style: str = "block") -> None:
        if isinstance(mount, dict):
            mount = {k: v for k, v in mount.items() if v is not UNDEFINED}
        self.kind = kind
        self.root = mkdtemp(prefix)
        self.config = os.path.join(self.root, "devcontainer.json")
        self.document = self.config if kind == "devcontainer" else os.path.join(self.root, "compose.yaml" if kind == "yaml" else "final.json")
        self.pointer = "/mounts/0" if kind == "devcontainer" else f"/services/{escape(service)}/volumes/0"
        model = (
            {"image": "synthetic:example", "workspaceFolder": "/work", "mounts": [mount]}
            if kind == "devcontainer"
            else {"services": {service: {"volumes": [mount]}}, "volumes": {"cache": {}}}
        )
        if kind == "yaml":
            if isinstance(mount, str):
                body = js_stringify(mount)
            else:
                rendered = []
                for key, value in entries(mount):
                    nested = style == "block" and key in ("bind", "volume", "tmpfs") and isinstance(value, dict) and bool(value)
                    if nested:
                        rendered.append(
                            f"{yaml_key(key)}:\n" + "\n".join(f"          {yaml_key(k)}: {js_stringify(v)}" for k, v in entries(value))
                        )
                    else:
                        rendered.append(f"{yaml_key(key)}: {js_stringify(value)}")
                body = "\n        ".join(rendered)
            self.text = f"services:\n  {yaml_key(service)}:\n    volumes:\n      - " + body + "\nvolumes:\n  cache: {}\n"
        else:
            self.text = ("// JSONC token provenance\n" if kind == "devcontainer" else "") + js_stringify(model, 2) + "\n"
        if kind != "devcontainer":
            write(self.config, js_stringify({"dockerComposeFile": "compose.yaml", "service": service, "workspaceFolder": "/work"}))
        if kind == "snapshot":
            write(os.path.join(self.root, "compose.yaml"), "deliberately unparsed: [")
        write(self.document, self.text)
        self.options: dict[str, Any] = {"config": self.config, "workspace": self.root}
        if kind == "snapshot":
            self.options["composeJson"] = self.document

    def at(self, parts: list[str]) -> dict[str, Any]:
        key = parts[-1]
        token = f"{yaml_key(key) if self.kind == 'yaml' else js_stringify(key)}:"
        assert token in self.text, token
        end = self.text.find(token) + len(token)
        offset = end + len(re.match(r"\s*", self.text[end:]).group(0))
        return {"file": self.document, "pointer": f"{self.pointer}/{'/'.join(escape(p) for p in parts)}", **line_column(self.text, offset)}

    def run(self, *flags: str) -> Result:
        result = run_cli(
            ["--config", self.config, "--workspace", self.root, *(["--compose-json", self.document] if self.kind == "snapshot" else []), *flags],
            cwd=TMP,
        )
        assert result.stderr == ""
        return result


def assert_option_failure(f: OptionFixture, parts: list[str] | None, message: str, code: str = "invalid-mount") -> None:
    json_run = f.run("--json", "--explain")
    assert json_run.status == 1
    report = json.loads(json_run.stdout)
    assert report == check(f.options)
    diagnostic = find(report["diagnostics"], lambda d: d["code"] == code)
    assert diagnostic, js_stringify(report["diagnostics"])
    assert diagnostic["message"] == message
    expected = f.at(parts) if parts else find(report["nodes"], lambda n: n["kind"] == "mount")["location"]
    assert diagnostic["locations"][0] == expected
    assert diagnostic["from"] == f"{f.document}#{expected['pointer']}"
    assert find(report["nodes"], lambda n: n["id"] == diagnostic["from"])["location"] == expected
    mount = find(report["nodes"], lambda n: n["kind"] == "mount")
    assert mount["id"] == f"{f.document}#{f.pointer}"
    assert mount["location"]["pointer"] == f.pointer
    assert not any(e["relation"] == "uses-volume" for e in report["edges"])
    assert not any(e["relation"].endswith("bind-source") for e in report["explanations"])
    explanation = find(report["explanations"], lambda e: e["relation"] == "mount-target")
    assert explanation["status"] == "invalid"
    assert any(r["code"] == code and r["message"] == message for r in explanation["reasons"])
    explained = f.run("--explain")
    assert explained.status == 1
    assert f"{expected['file']}:{expected['line']}:{expected['column']}#{expected['pointer']}" in explained.stdout
    sarif = f.run("--sarif")
    assert sarif.status == 1
    converted = json.loads(sarif.stdout)
    assert_valid_sarif(converted)
    assert converted == to_sarif(report)
    primary = find(converted["runs"][0]["results"], lambda r: r["ruleId"] == code)["locations"][0]
    assert primary["properties"]["jsonPointer"] == expected["pointer"]
    assert not primary["properties"].get("inferred")
    assert primary["physicalLocation"]["region"] == {"startLine": expected["line"], "startColumn": expected["column"]}
    if f.document.endswith("final.json"):
        assert re.search(r"original override provenance is unavailable", report["scope"]["composeInput"]["provenance"])
        assert "compose.yaml" not in js_stringify(converted["runs"][0]["results"])
    assert read(f.document) == f.text
