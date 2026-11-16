"""Port of test/mount-structural-option-locations.test.js (node:test) to pytest."""

from __future__ import annotations

import json
from typing import Any

import pytest

from stackwrench.devcontainer import InputError, check, compose_mount

from .helpers import UNDEFINED
from .option_fixture import OptionFixture, assert_option_failure

PREFIX = "wirecheck-structural-option-"
KINDS = ["yaml", "snapshot"]
GROUPS = ["bind", "volume", "tmpfs"]


def fixture(mkdtemp: Any, kind: str, mount: Any, service: str = "web", style: str = "block") -> OptionFixture:
    return OptionFixture(mkdtemp, PREFIX, kind, mount, service, style)


def mount_for(mount_type: Any) -> dict[str, Any]:
    source = {"source": "."} if mount_type == "bind" else {"source": "cache"} if mount_type == "volume" else {}
    return {"type": mount_type, **source, "target": "/work"}


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("kind", KINDS)
def test_non_object_group_options_reject_at_exact_group_value_tokens(mkdtemp: Any, kind: str, group: str) -> None:
    for value in [None, [], [False], True, False, 42, 0, "invalid"]:
        assert_option_failure(fixture(mkdtemp, kind, {**mount_for(group), group: value}), [group], f"{group} options require type={group} and an object")


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("kind", KINDS)
def test_wrong_type_for_group_options_identifies_the_supplied_group_token(mkdtemp: Any, kind: str, group: str) -> None:
    for mount_type in ["bind", "volume", "tmpfs", "unsupported", UNDEFINED]:
        if mount_type == group:
            continue
        for value in [{}, {"mystery": True}]:
            assert_option_failure(
                fixture(mkdtemp, kind, {**mount_for(mount_type), group: value}, "web/~"), [group], f"{group} options require type={group} and an object"
            )


@pytest.mark.parametrize("kind", KINDS)
def test_group_structural_failures_retain_earlier_validation_and_fixed_traversal_order(mkdtemp: Any, kind: str) -> None:
    unresolved = "Unresolved dollar expansion in a wiring field; Compose variables require pre-rendered JSON"
    cases: list[tuple[dict[str, Any], list[str], str, str]] = [
        ({"source": "${UNRESOLVED}", "mystery": True, "read_only": 42}, ["source"], unresolved, "unsupported-substitution"),
        ({"volume": ""}, ["volume"], "Expected a nonempty string without NUL", "invalid-field"),
        ({"volume": "${UNRESOLVED}"}, ["volume"], unresolved, "unsupported-substitution"),
        ({"mystery": False, "read_only": 42}, ["mystery"], "Unsupported mount option mystery", "invalid-mount"),
        ({"read_only": 42, "consistency": 42}, ["read_only"], "read_only must be boolean", "invalid-mount"),
        ({"consistency": 42}, ["consistency"], "Unsupported consistency value", "invalid-mount"),
        # bind appears after the other groups; validation still starts with bind.
        ({"tmpfs": None, "volume": None, "bind": None}, ["bind"], "bind options require type=bind and an object", "invalid-mount"),
        ({"tmpfs": None, "volume": None}, ["volume"], "volume options require type=volume and an object", "invalid-mount"),
        ({"type": "bind", "source": ".", "bind": {"mystery": True}, "volume": None}, ["bind", "mystery"], "Unsupported mount option mystery", "invalid-mount"),
        ({"type": "bind", "source": ".", "bind": {"propagation": "shared"}}, ["bind", "propagation"], "Only default rprivate bind propagation is supported", "invalid-mount"),
        ({"type": "bind", "source": ".", "bind": {"create_host_path": 42}}, ["bind", "create_host_path"], "create_host_path must be boolean", "invalid-mount"),
        ({"volume": {"nocopy": 42, "subpath": "sub", "mystery": True}}, ["volume", "mystery"], "Unsupported mount option mystery", "invalid-mount"),
        ({"volume": {"nocopy": 42, "subpath": "sub"}}, ["volume", "subpath"], "volume.subpath is not evaluated", "invalid-mount"),
        ({"volume": {"nocopy": 42}, "tmpfs": None}, ["volume", "nocopy"], "nocopy must be boolean", "invalid-mount"),
        ({"source": 42, "target": "relative"}, ["volume"], "volume options require type=volume and an object", "invalid-mount"),
    ]
    for changes, parts, message, code in cases:
        assert_option_failure(fixture(mkdtemp, kind, {**mount_for("volume"), "volume": None, **changes}), parts, message, code)


@pytest.mark.parametrize("kind", KINDS)
def test_omitted_and_known_group_objects_retain_identity_and_workspace_verdicts(mkdtemp: Any, kind: str) -> None:
    for mount_type in GROUPS:
        known = (
            {"propagation": "rprivate", "create_host_path": False, "selinux": "z"}
            if mount_type == "bind"
            else {"nocopy": True}
            if mount_type == "volume"
            else {"size": 1024, "mode": 448}
        )
        for options in [UNDEFINED, {}, known]:
            f = fixture(
                mkdtemp,
                kind,
                {**mount_for(mount_type), "read_only": True, "consistency": "cached", **({} if options is UNDEFINED else {mount_type: options})},
            )
            report = check(f.options)
            assert [d["code"] for d in report["diagnostics"]] == ([] if mount_type == "bind" else ["workspace-content-unknown"])
            node = next(n for n in report["nodes"] if n["kind"] == "mount")
            assert node["id"] == f"{f.document}#{f.pointer}"
            assert node["targetLocation"] == f.at(["target"])
            assert node["sourceLocation"] == (None if mount_type == "tmpfs" else f.at(["source"]))
            for flags, exit in [([], 0), (["--strict"], 0 if mount_type == "bind" else 1)]:
                json_run = f.run("--json", *flags)
                assert json_run.status == exit
                assert json.loads(json_run.stdout) == report


@pytest.mark.parametrize("kind", KINDS)
def test_group_like_short_options_retain_enclosing_scalar_policy(mkdtemp: Any, kind: str) -> None:
    assert_option_failure(fixture(mkdtemp, kind, ".:/work:bind"), None, "Unsupported or conflicting short mount options")


def test_yaml_wrong_type_flow_mappings_point_at_the_group_opening_token(mkdtemp: Any) -> None:
    for group in GROUPS:
        mount_type = "bind" if group == "volume" else "volume"
        assert_option_failure(
            fixture(mkdtemp, "yaml", {**mount_for(mount_type), group: {"mystery": True}}, "web", "flow"),
            [group],
            f"{group} options require type={group} and an object",
        )


def test_structural_group_errors_without_matching_metadata_retain_enclosing_fallback() -> None:
    location = {"file": "synthetic.yaml", "pointer": "/services/web/volumes/0", "line": 4, "column": 9}
    for fields in [None, {}, {"target": {**location, "pointer": f"{location['pointer']}/target"}}, {"optionLocation": lambda parts: None}]:
        for group in GROUPS:
            for value in [None, [], 42, {}]:
                mount_type = "unsupported" if isinstance(value, dict) else group
                with pytest.raises(InputError) as caught:
                    compose_mount({**mount_for(mount_type), group: value}, location, fields)
                assert caught.value.code == "invalid-mount"
                assert caught.value.message == f"{group} options require type={group} and an object"
                assert caught.value.location == location
