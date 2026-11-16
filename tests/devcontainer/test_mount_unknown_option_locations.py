"""Port of test/mount-unknown-option-locations.test.js (node:test) to pytest."""

from __future__ import annotations

from typing import Any

import pytest

from stackwrench.devcontainer import InputError, check, compose_mount, dev_mount

from .option_fixture import OptionFixture, assert_option_failure

PREFIX = "wirecheck-unknown-option-"
BIND = {"type": "bind", "source": ".", "target": "/work"}


def fixture(mkdtemp: Any, kind: str, mount: Any, service: str = "web") -> OptionFixture:
    return OptionFixture(mkdtemp, PREFIX, kind, mount, service)


@pytest.mark.parametrize("kind", ["devcontainer", "yaml", "snapshot"])
def test_unknown_top_level_options_use_exact_value_tokens_and_escaped_pointers(mkdtemp: Any, kind: str) -> None:
    for key, value in [("mystery", True), ("a~/b", False), ("__proto__", None), ("constructor", {}), ("optionLocation", []), ("sourceLocation", "opaque"), ("type/source", 42)]:
        assert_option_failure(fixture(mkdtemp, kind, {**BIND, key: value}, "web/~"), [key], f"Unsupported mount option {key}")


@pytest.mark.parametrize("kind", ["devcontainer", "yaml", "snapshot"])
def test_first_unknown_follows_object_keys_order_before_option_validation(mkdtemp: Any, kind: str) -> None:
    for unknown, first in [({"zebra": True, "alpha": False}, "zebra"), ({"alpha": False, "zebra": True}, "alpha"), ({"9": True, "2": False, "alpha": True}, "2")]:
        assert_option_failure(
            fixture(mkdtemp, kind, {**BIND, "read_only": 42, "consistency": 42, **unknown}), [first], f"Unsupported mount option {first}"
        )


@pytest.mark.parametrize("kind", ["devcontainer", "yaml", "snapshot"])
def test_expansion_failures_keep_priority_over_unknown_options(mkdtemp: Any, kind: str) -> None:
    compose = kind != "devcontainer"
    message = (
        "Unresolved dollar expansion in a wiring field; Compose variables require pre-rendered JSON"
        if compose
        else "Unsupported substitution ${env:UNREAD}; no host environment is read"
    )
    f = fixture(mkdtemp, kind, {**BIND, "mystery": True, "source": "${env:UNREAD}"})
    assert_option_failure(f, ["source"], message, "unsupported-substitution")
    assert_option_failure(fixture(mkdtemp, kind, {**BIND, "mystery": ""}), ["mystery"], "Expected a nonempty string without NUL", "invalid-field")


@pytest.mark.parametrize("kind", ["devcontainer", "yaml", "snapshot"])
def test_accepted_known_options_preserve_identity_and_workspace_verdicts(mkdtemp: Any, kind: str) -> None:
    for mount in [
        {**BIND, "read_only": True, "consistency": "cached", **({} if kind == "devcontainer" else {"bind": {"propagation": "rprivate", "create_host_path": False, "selinux": "z"}})},
        {"type": "volume", "source": "cache", "target": "/work", **({} if kind == "devcontainer" else {"volume": {"nocopy": True}})},
        {"type": "tmpfs", "target": "/work", **({} if kind == "devcontainer" else {"tmpfs": {"size": 1024, "mode": 448}})},
    ]:
        f = fixture(mkdtemp, kind, mount)
        report = check(f.options)
        assert report["status"] == ("ok" if mount["type"] == "bind" else "partial")
        assert [d["code"] for d in report["diagnostics"]] == ([] if mount["type"] == "bind" else ["workspace-content-unknown"])
        assert next(n for n in report["nodes"] if n["kind"] == "mount")["targetLocation"] == f.at(["target"])
        assert f.run("--json", "--strict").status == (0 if mount["type"] == "bind" else 1)


@pytest.mark.parametrize("kind", ["yaml", "snapshot"])
def test_unknown_nested_bind_volume_tmpfs_fields_identify_their_own_value_tokens(mkdtemp: Any, kind: str) -> None:
    for group in ["bind", "volume", "tmpfs"]:
        for key, value in [("mystery", True), ("a~/b", None), ("__proto__", False), ("constructor", {}), ("optionLocation", []), ("empty", "")]:
            source = {"source": "."} if group == "bind" else {"source": "cache"} if group == "volume" else {}
            mount = {"type": group, **source, "target": "/work", group: {key: value}}
            assert_option_failure(fixture(mkdtemp, kind, mount, "web/~"), [group, key], f"Unsupported mount option {key}")


@pytest.mark.parametrize("kind", ["yaml", "snapshot"])
def test_nested_unknown_preserves_structure_option_and_policy_ordering(mkdtemp: Any, kind: str) -> None:
    for changes, parts, message in [
        ({"read_only": 42, "volume": {"mystery": True}}, ["read_only"], "read_only must be boolean"),
        ({"consistency": 42, "volume": {"mystery": True}}, ["consistency"], "Unsupported consistency value"),
        ({"bind": {"mystery": True}}, ["bind"], "bind options require type=bind and an object"),
        ({"volume": None}, ["volume"], "volume options require type=volume and an object"),
        ({"volume": {"nocopy": 42, "subpath": "sub", "zebra": False, "alpha": True}}, ["volume", "zebra"], "Unsupported mount option zebra"),
        ({"volume": {"nocopy": 42, "subpath": "sub"}}, ["volume", "subpath"], "volume.subpath is not evaluated"),
        (
            {"type": "bind", "source": ".", "bind": {"propagation": "shared", "create_host_path": 42, "mystery": True}, "volume": {"earlier_in_text": True}},
            ["bind", "mystery"],
            "Unsupported mount option mystery",
        ),
        (
            {"type": "bind", "source": ".", "volume": {"mystery": True}, "bind": {"propagation": "shared"}},
            ["bind", "propagation"],
            "Only default rprivate bind propagation is supported",
        ),
    ]:
        assert_option_failure(fixture(mkdtemp, kind, {"type": "volume", "source": "cache", "target": "/work", **changes}), parts, message)


def test_unknown_scalar_mount_options_retain_enclosing_evidence(mkdtemp: Any) -> None:
    for kind in ["devcontainer", "yaml", "snapshot"]:
        scalar = "type=bind,source=.,target=/work,mystery=true" if kind == "devcontainer" else ".:/work:mystery"
        assert_option_failure(
            fixture(mkdtemp, kind, scalar),
            None,
            "Unsupported mount option mystery" if kind == "devcontainer" else "Unsupported or conflicting short mount options",
        )


def test_unknown_options_without_matching_parser_metadata_retain_enclosing_fallback() -> None:
    location = {"file": "synthetic.json", "pointer": "/mounts/0", "line": 5, "column": 5}
    for parse in [dev_mount, compose_mount]:
        for fields in [None, {}, {"type": {**location, "pointer": "/mounts/0/type"}}]:
            mounts = [{**BIND, "mystery": True}, *([{**BIND, "bind": {"mystery": True}}] if parse is compose_mount else [])]
            for mount in mounts:
                with pytest.raises(InputError) as caught:
                    parse(mount, location, fields)
                assert caught.value.code == "invalid-mount"
                assert caught.value.message == "Unsupported mount option mystery"
                assert caught.value.location == location
