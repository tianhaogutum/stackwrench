"""Port of test/wirecheck.test.js (node:test) to pytest."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from stackwrench import __version__
from stackwrench.devcontainer import check

from .helpers import REPO, TMP, UNDEFINED, Result, find, js_stringify, run_cli

EXAMPLES = "examples/devcontainer"


class Fixture:
    def __init__(self, mkdtemp: Any) -> None:
        self.root = mkdtemp("wirecheck-")
        self.write("src/file.txt", "synthetic")

    def write(self, file: str, data: Any) -> str:
        target = os.path.join(self.root, file)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(data if isinstance(data, str) else js_stringify(data, 2))
        return target

    def config(self, data: Any) -> str:
        return self.write(".devcontainer/devcontainer.json", data)

    def compose(self, data: Any) -> str:
        return self.write("compose.yaml", data)

    def run(self, args: list[str] | None = None, raw: bool = False) -> Result:
        result = run_cli(
            [
                "--config",
                os.path.join(self.root, ".devcontainer/devcontainer.json"),
                "--workspace",
                self.root,
                *([] if raw else ["--json"]),
                *(args or []),
            ],
            cwd=TMP,
            env={"PATH": "/nonexistent", "HOME": self.root, "DOCKER_HOST": "tcp://invalid:9999"},
        )
        result.report = None if raw else json.loads(result.stdout)
        return result

    def image(self, extra: dict[str, Any] | None = None) -> str:
        return self.config(
            {
                "image": "synthetic:example",
                "workspaceMount": "type=bind,source=${localWorkspaceFolder},target=/work",
                "workspaceFolder": "/work",
                **(extra or {}),
            }
        )

    def comp(self, extra: dict[str, Any] | None = None) -> str:
        return self.config(
            {"dockerComposeFile": "../compose.yaml", "service": "web", "workspaceFolder": "/work", **(extra or {})}
        )


@pytest.fixture
def f(mkdtemp: Any) -> Fixture:
    return Fixture(mkdtemp)


def codes(result: Result) -> list[str]:
    return [d["code"] for d in result.report["diagnostics"]]


def has_error(result: Result, code: str) -> None:
    assert result.status == 1, result.stdout + result.stderr
    assert result.report["status"] == "error"
    assert code in codes(result), result.stdout


GOOD_COMPOSE = {"services": {"web": {"volumes": [{"type": "bind", "source": ".", "target": "/work"}]}}}


def run_repo(args: list[str]) -> Result:
    return run_cli(args, cwd=REPO)


# Real process E2E tests use a foreign cwd and an unusable PATH/Docker endpoint.
def test_acceptance_example_jsonc_comments_trailing_comma_yaml_complete_wiring_graph() -> None:
    result = run_repo(
        ["--config", f"{EXAMPLES}/.devcontainer/devcontainer.json", "--workspace", EXAMPLES, "--json", "--strict"]
    )
    assert result.status == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    for relation in [
        "compose-document",
        "references-service",
        "compose-build-context",
        "compose-dockerfile",
        "compose-bind-source",
        "uses-volume",
        "workspace-covered",
    ]:
        assert any(e["relation"] == relation for e in report["edges"]), relation
    ids = {n["id"] for n in report["nodes"]}
    for edge in report["edges"]:
        assert edge["from"] in ids
        assert edge["to"] in ids
    assert any(u["location"]["pointer"] == "/name" for u in report["unevaluatedFields"])
    assert any(u["location"]["pointer"] == "/services/db/image" for u in report["unevaluatedFields"])


def test_missing_service_and_run_services_produce_location_to_location_diagnostics(f: Fixture) -> None:
    f.comp({"service": "api", "runServices": ["ghost"]})
    f.compose(GOOD_COMPOSE)
    r = f.run()
    has_error(r, "missing-service")
    ds = [d for d in r.report["diagnostics"] if d["code"] == "missing-service"]
    assert len(ds) == 2
    assert ds[0]["locations"][0]["pointer"] == "/service"
    assert ds[0]["locations"][0]["line"] == 3
    assert ds[0]["locations"][1]["pointer"] == "/services"
    assert ds[0]["locations"][1]["file"].endswith("compose.yaml")
    assert any(e["relation"] == "missing-service" and e["severity"] == "error" for e in r.report["edges"])


def test_multiple_raw_files_fail_with_render_instructions_never_shallow_merge(f: Fixture) -> None:
    f.comp({"dockerComposeFile": ["../compose.yaml", "../override.yaml"]})
    f.compose(GOOD_COMPOSE)
    f.write("override.yaml", {"services": {"extra": {}}})
    r = f.run()
    has_error(r, "unsupported-compose-merge")
    assert re.search(r"config --format json", r.report["diagnostics"][0]["message"])


def test_explicit_pre_rendered_compose_json_handles_ordered_files_and_relative_original_base(f: Fixture) -> None:
    f.comp({"dockerComposeFile": ["../compose.yaml", "../override.yaml"]})
    f.compose("not parsed: [")
    f.write("override.yaml", "not parsed: [")
    f.write("rendered.json", {"name": "synthetic", **GOOD_COMPOSE})
    r = f.run(["--compose-json", os.path.join(f.root, "rendered.json"), "--strict"])
    assert r.status == 0, r.stdout
    assert r.report["scope"]["composeInput"]["format"] == "pre-rendered-json"
    assert len(r.report["scope"]["composeInput"]["orderedFiles"]) == 2
    assert any(n["location"]["file"].endswith("rendered.json") and n["kind"] == "mount" for n in r.report["nodes"])


def test_compose_base_is_explicit_and_confines_normalized_relative_paths(f: Fixture) -> None:
    f.comp()
    f.compose(GOOD_COMPOSE)
    f.write("rendered.json", GOOD_COMPOSE)
    r = f.run(["--compose-json", os.path.join(f.root, "rendered.json"), "--compose-base", os.path.join(f.root, "src")])
    assert r.status == 0, r.stdout
    assert r.report["scope"]["composeInput"]["base"] == os.path.join(f.root, "src")


def test_dev_container_dockerfile_is_relative_to_config_context_independently_relative_to_config(f: Fixture) -> None:
    f.image({"image": UNDEFINED, "build": {"context": "../src", "dockerfile": "Dockerfile", "args": {"INPUT": "synthetic"}}})
    f.write(".devcontainer/Dockerfile", "FROM scratch\n")
    r = f.run(["--strict"])
    assert r.status == 0, r.stdout
    edge = find(r.report["edges"], lambda e: e["relation"] == "dockerfile")
    assert edge["to"] == f"host:{os.path.join(f.root, '.devcontainer/Dockerfile')}"
    assert any(u["location"]["pointer"] == "/build/args" for u in r.report["unevaluatedFields"])


def test_compose_dockerfile_is_relative_to_context_not_config_or_compose_file(f: Fixture) -> None:
    f.comp()
    f.compose({"services": {"web": {**GOOD_COMPOSE["services"]["web"], "build": {"context": "src", "dockerfile": "../build/Dev.Dockerfile"}}}})
    f.write("build/Dev.Dockerfile", "FROM scratch\n")
    r = f.run(["--strict"])
    assert r.status == 0, r.stdout
    target = f"host:{os.path.join(f.root, 'build/Dev.Dockerfile')}"
    assert any(e["relation"] == "compose-dockerfile" and e["to"] == target for e in r.report["edges"])


def test_compose_string_build_and_default_dockerfile(f: Fixture) -> None:
    f.comp()
    f.compose({"services": {"web": {**GOOD_COMPOSE["services"]["web"], "build": "src"}}})
    f.write("src/Dockerfile", "FROM scratch")
    assert f.run(["--strict"]).status == 0


def test_missing_build_context_and_dockerfile_are_errors(f: Fixture) -> None:
    f.image({"image": UNDEFINED, "build": {"context": "../missing", "dockerfile": "NoDockerfile"}})
    r = f.run()
    has_error(r, "missing-host-path")
    assert len([d for d in r.report["diagnostics"] if d["code"] == "missing-host-path"]) == 2


def test_named_and_anonymous_volumes_are_never_looked_up_as_host_paths(f: Fixture) -> None:
    f.comp({"workspaceFolder": "/cache"})
    f.compose({"services": {"web": {"volumes": ["cache:/cache", "/anonymous"]}}, "volumes": {"cache": {}}})
    r = f.run()
    assert r.status == 0, r.stdout
    assert "workspace-content-unknown" in codes(r)
    assert len([n for n in r.report["nodes"] if n["kind"] == "host-path"]) == 1  # Compose file only
    assert f.run(["--strict"]).status == 1


def test_undeclared_named_volume_is_an_error_not_a_missing_host_path(f: Fixture) -> None:
    f.comp()
    f.compose({"services": {"web": {"volumes": ["cache:/work"]}}})
    r = f.run()
    has_error(r, "missing-volume")
    assert "missing-host-path" not in codes(r)


def test_nonexistent_bind_source_fails_even_when_compose_could_auto_create_it(f: Fixture) -> None:
    f.comp()
    f.compose({"services": {"web": {"volumes": ["./absent:/work"]}}})
    r = f.run()
    has_error(r, "missing-host-path")
    assert re.search(r"bind source", r.report["diagnostics"][0]["message"])


def test_workspace_subdirectory_must_exist_under_the_covering_bind(f: Fixture) -> None:
    f.image({"workspaceFolder": "/work/absent"})
    r = f.run()
    has_error(r, "missing-host-path")
    assert any(e["relation"] == "workspace-covered" for e in r.report["edges"])


def test_container_paths_normalize_with_posix_segment_boundaries(f: Fixture) -> None:
    f.image({"workspaceFolder": "/work/src/../src"})
    assert f.run(["--strict"]).status == 0
    f.image({"workspaceFolder": "/worker"})
    r = f.run()
    assert r.status == 0
    assert "workspace-uncovered" in codes(r)
    assert r.report["status"] == "partial"


def test_longest_target_wins_nested_named_volume_hides_parent_bind_contents(f: Fixture) -> None:
    f.image({"workspaceFolder": "/work/src", "mounts": ["type=volume,source=cache,target=/work/src"]})
    r = f.run()
    assert r.status == 0
    assert "workspace-content-unknown" in codes(r)


def test_image_content_and_implicit_workspace_mounts_are_unknown_not_missing_host_errors(f: Fixture) -> None:
    f.config({"image": "synthetic:example", "workspaceFolder": "/inside-image"})
    r = f.run()
    assert r.status == 0
    assert "workspace-uncovered" in codes(r)
    f.config({"image": "synthetic:example"})
    r = f.run()
    assert "implicit-workspace-unknown" in codes(r)


def test_remote_daemon_does_not_treat_local_bind_absence_as_proof_of_remote_failure(f: Fixture) -> None:
    f.image({"workspaceMount": "type=bind,source=${localWorkspaceFolder}/missing,target=/work"})
    r = f.run(["--daemon", "remote"])
    assert r.status == 0, r.stdout
    assert r.report["status"] == "partial"
    assert "remote-bind-unknown" in codes(r)
    assert "missing-host-path" not in codes(r)
    assert re.search(r"No remote filesystem", r.report["scope"]["hostAssumption"])


def test_supported_local_workspace_folder_and_basename_substitutions(f: Fixture) -> None:
    f.image(
        {
            "workspaceMount": "type=bind,source=${localWorkspaceFolder},target=/work/${localWorkspaceFolderBasename}",
            "workspaceFolder": "/work/${localWorkspaceFolderBasename}/src",
        }
    )
    assert f.run(["--strict"]).status == 0


def test_unknown_substitutions_do_not_consult_environment_or_run_a_command(f: Fixture) -> None:
    f.image({"workspaceFolder": "${localEnv:HOME}"})
    has_error(f.run(), "unsupported-substitution")
    f.image({"workspaceMount": "type=bind,source=$(touch MARKER),target=/work"})
    has_error(f.run(), "unsupported-substitution")
    assert not os.path.exists(os.path.join(f.root, "MARKER"))


def test_raw_compose_interpolation_in_a_bind_path_is_explicitly_rejected(f: Fixture) -> None:
    f.comp()
    f.compose({"services": {"web": {"volumes": [{"type": "bind", "source": "${SRC:-.}", "target": "/work"}]}}})
    has_error(f.run(), "unsupported-substitution")


def test_lifecycle_hooks_env_probes_commands_and_healthchecks_are_inert_data(f: Fixture) -> None:
    marker = os.path.join(f.root, "SHOULD_NOT_EXIST")
    f.image(
        {
            "initializeCommand": f"touch {marker}",
            "postCreateCommand": ["touch", marker],
            "userEnvProbe": "loginInteractiveShell",
            "customizations": {"literal": "/* not a comment */ // in a string,"},
        }
    )
    r = f.run()
    assert r.status == 0, r.stdout
    assert not os.path.exists(marker)
    for key in ["/initializeCommand", "/postCreateCommand", "/userEnvProbe", "/customizations"]:
        assert any(u["location"]["pointer"] == key for u in r.report["unevaluatedFields"])
    f.comp()
    f.compose(
        {
            "services": {
                "web": {**GOOD_COMPOSE["services"]["web"], "command": f"touch {marker}", "healthcheck": {"test": f"touch {marker}"}}
            }
        }
    )
    assert f.run().status == 0
    assert not os.path.exists(marker)


def test_jsonc_lexer_preserves_comment_looking_strings(f: Fixture) -> None:
    f.config('{ /* real comment */ "image": "https://example.invalid/*literal*/", "workspaceFolder": "/inside", }')
    assert f.run().status == 0


def test_malformed_jsonc_duplicates_malformed_yaml_and_strict_rendered_json_fail(f: Fixture) -> None:
    f.config('{ "image": "x",,, }')
    has_error(f.run(), "parse-error")
    f.config('{"image":"x","image":"y"}')
    has_error(f.run(), "duplicate-key")
    f.comp()
    f.compose("services: [broken")
    has_error(f.run(), "parse-error")
    f.compose("services:\n  web: {}\n  web: {}\n")
    has_error(f.run(), "parse-error")
    f.compose(GOOD_COMPOSE)
    f.write("rendered.json", '{"services": {"web": {}},}')
    has_error(f.run(["--compose-json", os.path.join(f.root, "rendered.json")]), "parse-error")


def test_yaml_anchors_aliases_tags_and_non_string_keys_fail_explicitly(f: Fixture) -> None:
    f.comp()
    for yaml in ["services: &x\n  web: {}\n", "services:\n  web: !!map {}\n", "services:\n  12: {}\n", "services:\n  web:\n    <<: {}\n"]:
        f.compose(yaml)
        has_error(f.run(), "unsupported-yaml")


def test_path_traversal_and_symlink_escapes_fail_intentionally_broader_workspace_permits_access(f: Fixture) -> None:
    f.image({"workspaceMount": "type=bind,source=../outside,target=/work"})
    has_error(f.run(), "path-escape")
    os.symlink(TMP, os.path.join(f.root, "escape"))
    f.image({"workspaceMount": "type=bind,source=${localWorkspaceFolder}/escape/nonexistent-leaf,target=/work"})
    has_error(f.run(), "path-escape")
    # No outside write: root/src is outside the narrower explicitly declared root/.devcontainer.
    f.config(
        {"image": "x", "workspaceMount": f"type=bind,source={os.path.join(f.root, 'src')},target=/work", "workspaceFolder": "/work"}
    )
    has_error(f.run(["--workspace", os.path.join(f.root, ".devcontainer")]), "path-escape")
    assert f.run().status == 0


def test_config_and_pre_rendered_file_themselves_cannot_escape_workspace_root(f: Fixture) -> None:
    f.comp()
    f.compose(GOOD_COMPOSE)
    has_error(f.run(["--compose-json", os.path.join(f.root, "../outside.json")]), "path-escape")
    has_error(f.run(["--config", os.path.join(f.root, "../outside.json")]), "path-escape")


def test_invalid_mounts_fail_without_silently_dropping_options(f: Fixture) -> None:
    for mount in [
        "type=bind,source=.,target=relative",
        "type=bind,source=.,target=/work,mystery=yes",
        "type=bind,source=.,target=/work,target=/other",
        "type=bind,target=/work",
        "type=npipe,source=x,target=/work",
    ]:
        f.image({"workspaceMount": mount})
        assert f.run().status == 1
    f.comp()
    for mount in [
        "./src:/work:ro:extra",
        "./src:/work:ro,rw",
        {"type": "volume", "source": "cache", "target": "/work", "volume": {"subpath": "x"}},
        {"type": "bind", "source": ".", "target": "/work", "mystery": 3},
    ]:
        f.compose({"services": {"web": {"volumes": [mount]}}, "volumes": {"cache": {}}})
        has_error(f.run(), "invalid-mount")


def test_duplicate_targets_across_config_and_compose_are_refused(f: Fixture) -> None:
    f.comp({"mounts": ["type=volume,source=cache,target=/work"]})
    f.compose(GOOD_COMPOSE)
    has_error(f.run(), "duplicate-mount-target")
    f.image({"mounts": ["type=volume,source=cache,target=/work"]})
    has_error(f.run(), "duplicate-mount-target")


def test_tmpfs_coverage_is_unknown_never_a_host_lookup(f: Fixture) -> None:
    f.comp()
    f.compose({"services": {"web": {"volumes": [{"type": "tmpfs", "target": "/work", "tmpfs": {"size": 1024}}]}}})
    r = f.run()
    assert r.status == 0
    assert "workspace-content-unknown" in codes(r)


def test_unsupported_include_extends_volumes_from_and_inline_builds_are_explicit(f: Fixture) -> None:
    f.comp()
    f.compose({**GOOD_COMPOSE, "include": ["other.yaml"]})
    has_error(f.run(), "unsupported-compose-include")
    for key in ["extends", "volumes_from", "develop"]:
        f.compose({"services": {"web": {key: {}}}})
        has_error(f.run(), "unsupported-compose-wiring")
    f.compose({"services": {"web": {"build": {"context": ".", "dockerfile_inline": "FROM scratch"}}}})
    has_error(f.run(), "unsupported-build")


def test_invalid_modes_field_shapes_and_missing_files_fail(f: Fixture) -> None:
    for config in [
        {},
        {"image": "x", "build": {}},
        {"build": {}},
        {"image": 1},
        {"image": "x", "service": "web"},
        {"image": "x", "mounts": {}},
        {"dockerComposeFile": [], "service": "web"},
    ]:
        f.config(config)
        assert f.run().status == 1
    f.comp()
    f.compose({"services": []})
    has_error(f.run(), "invalid-services")
    f.compose(GOOD_COMPOSE)
    f.comp({"runServices": "web"})
    has_error(f.run(), "invalid-run-services")
    f.comp({"dockerComposeFile": "../absent.yaml"})
    has_error(f.run(), "missing-host-path")


def test_legacy_dockerfile_mode_is_checked_without_executing_or_parsing_the_dockerfile(f: Fixture) -> None:
    f.image({"image": UNDEFINED, "dockerFile": "Dockerfile", "context": ".."})
    f.write(".devcontainer/Dockerfile", "NOT REALLY A DOCKERFILE; touch /do-not-execute")
    assert f.run(["--strict"]).status == 0


def test_cli_text_usage_unknown_option_help_and_version(f: Fixture) -> None:
    f.image()
    text = f.run([], True)
    assert text.status == 0
    assert re.match(r"ok:", text.stdout)
    for args in [[], ["--wrong"], ["--config", "x", "--workspace", ".", "--daemon", "nonsense"]]:
        r = run_cli(args)
        assert r.status == 2
        assert re.search(r"stackwrench devcontainer:", r.stderr)
    assert re.search(r"No commands", run_cli(["--help"]).stdout)
    assert run_cli(["--version"]).stdout.strip() == __version__


def test_library_returns_the_same_report_and_validates_caller_options(f: Fixture) -> None:
    f.image()
    r = check({"config": os.path.join(f.root, ".devcontainer/devcontainer.json"), "workspace": f.root})
    assert r["status"] == "ok"
    assert len(r["nodes"]) > 0
    with pytest.raises(TypeError, match="required"):
        check({})
    with pytest.raises(TypeError, match="daemon"):
        check({"config": "x", "workspace": ".", "daemon": "unknown"})


def test_all_service_definitions_are_checked_including_those_outside_run_services_and_under_profiles(f: Fixture) -> None:
    f.comp({"runServices": ["web"]})
    f.compose({"services": {**GOOD_COMPOSE["services"], "inactive": {"profiles": ["optional"], "volumes": ["./missing:/data"]}}})
    has_error(f.run(), "missing-host-path")


def test_unsupported_mount_affecting_compose_fields_cannot_silently_alter_coverage(f: Fixture) -> None:
    f.comp()
    for key in ["tmpfs", "configs", "secrets"]:
        f.compose({"services": {"web": {**GOOD_COMPOSE["services"]["web"], key: ["/work"]}}})
        has_error(f.run(), "unsupported-compose-wiring")


def test_strict_malformed_shapes_are_not_replaced_with_defaults(f: Fixture) -> None:
    for extra in [
        {"mounts": None},
        {"workspaceFolder": None},
        {"context": "."},
        {"image": UNDEFINED, "build": {"context": None, "dockerfile": "Dockerfile"}},
    ]:
        f.image(extra)
        assert f.run().status == 1
    f.comp()
    f.compose({"services": {"web": {"volumes": None}}})
    has_error(f.run(), "invalid-mount")


def test_external_custom_named_volumes_and_run_args_explicitly_mark_runtime_assumptions_unknown(f: Fixture) -> None:
    f.comp()
    f.compose({**GOOD_COMPOSE, "volumes": {"cache": {"external": True}}})
    r = f.run()
    assert r.status == 0
    assert "volume-runtime-unknown" in codes(r)
    f.image({"runArgs": ["--mount", "type=tmpfs,target=/work"]})
    r = f.run()
    assert r.status == 0
    assert "run-args-unknown" in codes(r)
    assert f.run(["--strict"]).status == 1


def test_relative_remote_contexts_tilde_expansion_and_windows_paths_are_rejected(f: Fixture) -> None:
    for context in ["https://example.invalid/repo.git", "~/src", "C:\\src"]:
        f.image({"image": UNDEFINED, "build": {"context": context, "dockerfile": "Dockerfile"}})
        assert f.run().status == 1


def test_explicit_object_style_dev_container_mounts_support_bind_and_volume(f: Fixture) -> None:
    f.config(
        {
            "image": "x",
            "workspaceFolder": "/work",
            "mounts": [
                {"type": "bind", "source": "${localWorkspaceFolder}", "target": "/work"},
                {"type": "volume", "source": "cache", "target": "/cache"},
            ],
        }
    )
    assert f.run(["--strict"]).status == 0


def test_missing_defaults_have_inferred_locations_rather_than_invented_source_lines(f: Fixture) -> None:
    f.comp({"workspaceFolder": UNDEFINED})
    f.compose({"services": {"web": {"build": {}}}})
    r = f.run()
    has_error(r, "missing-host-path")
    inferred = find(r.report["nodes"], lambda n: n["location"]["pointer"] == "/services/web/build/dockerfile")
    assert inferred["location"]["inferred"] is True
    assert inferred["location"]["line"] == 4


def test_oversized_and_overly_nested_documents_fail_as_input_without_running_anything(f: Fixture) -> None:
    f.config(" " * (2 * 1024 * 1024 + 1))
    has_error(f.run(), "read-error")
    f.config('{"image":"x","metadata":' + "[" * 110 + "0" + "]" * 110 + "}")
    has_error(f.run(), "input-limit")


def test_all_shipped_success_and_failure_examples_replay_with_their_documented_semantics() -> None:
    def run(config: str, extra: list[str] | None = None) -> Result:
        return run_repo(
            ["--config", f"{EXAMPLES}/.devcontainer/{config}", "--workspace", EXAMPLES, "--json", *(extra or [])]
        )

    for config in ["devcontainer.json", "image.json", "build.json"]:
        r = run(config, ["--strict"])
        assert r.status == 0, r.stdout + r.stderr
    r = run("overrides.json", ["--compose-json", f"{EXAMPLES}/compose.rendered.json", "--strict"])
    assert r.status == 0, r.stdout + r.stderr
    r = run("overrides.json")
    assert r.status == 1
    assert any(d["code"] == "unsupported-compose-merge" for d in json.loads(r.stdout)["diagnostics"])
    for config, code in [("missing-service.json", "missing-service"), ("missing-source.json", "missing-host-path")]:
        r = run(config)
        assert r.status == 1
        assert any(d["code"] == code for d in json.loads(r.stdout)["diagnostics"])


def test_workspace_mount_named_volume_appears_in_the_graph_with_its_container_target(f: Fixture) -> None:
    f.image({"workspaceMount": "type=volume,source=workspace-data,target=/work"})
    r = f.run()
    assert r.status == 0
    mount = find(r.report["nodes"], lambda n: n["location"]["pointer"] == "/workspaceMount")
    assert mount["mount"] == {"type": "volume", "source": "workspace-data", "target": "/work"}
    assert any(e["from"] == mount["id"] and e["relation"] == "uses-volume" for e in r.report["edges"])
    assert find(r.report["nodes"], lambda n: n["kind"] == "workspace-folder")["containerPath"] == "/work"


def test_normalized_compose_json_maps_absolute_host_paths_and_standard_long_volume_fields(f: Fixture) -> None:
    f.comp()
    f.compose(GOOD_COMPOSE)
    f.write("src/Dockerfile", "FROM scratch")
    f.write(
        "normalized.json",
        {
            "name": "synthetic",
            "services": {
                "web": {
                    "build": {"context": os.path.join(f.root, "src"), "dockerfile": "Dockerfile"},
                    "networks": {"default": None},
                    "volumes": [
                        {"type": "bind", "source": f.root, "target": "/work", "bind": {"create_host_path": True}, "read_only": False},
                        {"type": "volume", "source": "cache", "target": "/cache", "volume": {}},
                    ],
                }
            },
            "volumes": {"cache": {"name": "synthetic_cache"}},
            "networks": {"default": {"name": "synthetic_default"}},
        },
    )
    r = f.run(["--compose-json", os.path.join(f.root, "normalized.json"), "--strict"])
    assert r.status == 0, r.stdout
    target = f"host:{os.path.join(f.root, 'src/Dockerfile')}"
    assert any(e["relation"] == "compose-dockerfile" and e["to"] == target for e in r.report["edges"])
    assert any(u["location"]["pointer"] == "/networks" for u in r.report["unevaluatedFields"])


def test_compose_short_interpolation_is_diagnosed_before_colons_are_parsed_as_mount_delimiters(f: Fixture) -> None:
    f.comp()
    for mount in ["${SOURCE:-.}:/work", "${CACHE}:/work"]:
        f.compose({"services": {"web": {"volumes": [mount]}}})
        has_error(f.run(), "unsupported-substitution")


# Round 2 contract: slash-equivalent targets must select the same mount and conflict.
def test_trailing_slashes_cannot_bypass_nested_volume_precedence_in_either_input_format(f: Fixture) -> None:
    for compose in [False, True]:
        for target in ["/work/src", "/work/src/", "/work//src/./"]:
            for folder in ["/work/src", "/work/src/"]:
                if compose:
                    f.comp({"workspaceFolder": folder})
                    f.compose({"services": {"web": {"volumes": [".:/work", f"cache:{target}"]}}, "volumes": {"cache": {}}})
                else:
                    f.image({"workspaceFolder": folder, "mounts": [f"type=volume,source=cache,target={target}"]})
                r = f.run(["--strict"])
                assert r.status == 1, r.stdout
                assert r.report["status"] == "partial"
                assert "workspace-content-unknown" in codes(r), r.stdout
                assert not any(e["relation"] == "workspace-bind-directory" for e in r.report["edges"])
                assert find(r.report["nodes"], lambda n: n["kind"] == "workspace-folder")["containerPath"] == "/work/src"
                assert any(
                    (n.get("mount") or {}).get("type") == "volume" and n["mount"]["target"] == "/work/src"
                    for n in r.report["nodes"]
                )


def test_slash_equivalent_targets_are_duplicate_mounts_within_and_across_input_sources(f: Fixture) -> None:
    f.image({"mounts": ["type=volume,source=cache,target=/work/"]})
    has_error(f.run(), "duplicate-mount-target")
    f.comp()
    f.compose({"services": {"web": {"volumes": [".:/work", "cache:/work/"]}}, "volumes": {"cache": {}}})
    has_error(f.run(), "duplicate-mount-target")
    f.comp({"mounts": ["type=volume,source=cache,target=/work/"]})
    f.compose(GOOD_COMPOSE)
    has_error(f.run(), "duplicate-mount-target")


def test_root_container_target_remains_root_and_still_covers_a_nested_workspace(f: Fixture) -> None:
    f.image({"workspaceMount": "type=bind,source=${localWorkspaceFolder},target=////", "workspaceFolder": "/src/"})
    r = f.run(["--strict"])
    assert r.status == 0, r.stdout
    assert find(r.report["nodes"], lambda n: n["location"]["pointer"] == "/workspaceMount")["mount"]["target"] == "/"
    assert find(r.report["nodes"], lambda n: n["kind"] == "workspace-folder")["containerPath"] == "/src"


# Explain contract: source fields + physical resolution bases + destinations/outcomes.
def test_explain_identifies_independent_config_relative_build_paths_and_workspace_mount_mapping(f: Fixture) -> None:
    f.image({"image": UNDEFINED, "build": {"context": "../src", "dockerfile": "Dockerfile"}, "workspaceFolder": "/work/src"})
    f.write(".devcontainer/Dockerfile", "FROM scratch")
    r = f.run(["--explain", "--strict"], True)
    assert r.status == 0, r.stdout + r.stderr
    assert re.search(r"Wiring explanations:", r.stdout)
    assert "#/build/context" in r.stdout
    assert f"base: {os.path.join(f.root, '.devcontainer')}" in r.stdout
    assert f"target: {os.path.join(f.root, 'src')}" in r.stdout
    assert "#/workspaceMount" in r.stdout
    assert "bind ${localWorkspaceFolder} -> /work" in r.stdout or f"bind {f.root} -> /work" in r.stdout
    assert f"target: {os.path.join(f.root, 'src')}" in r.stdout
    json_run = f.run(["--explain", "--strict"])
    assert json_run.status == 0, json_run.stdout
    context = find(json_run.report["explanations"], lambda e: e["location"]["pointer"] == "/build/context")
    assert context["base"] == os.path.join(f.root, ".devcontainer")
    assert context["target"] == os.path.join(f.root, "src")
    assert context["status"] == "resolved"


def test_explain_locates_missing_services_compose_relative_paths_and_escaping_context_failures(f: Fixture) -> None:
    f.comp({"service": "missing", "runServices": ["web"]})
    f.compose({"services": {"web": {"build": {"context": "../outside", "dockerfile": "Dockerfile"}, "volumes": [".:/work"]}}})
    r = f.run(["--explain"], True)
    assert r.status == 1, r.stdout + r.stderr
    for field in ["#/service", "#/services/web/build/context", "#/services/web/volumes/0", "#/services/web/build/dockerfile"]:
        assert field in r.stdout, r.stdout
    assert f"base: {f.root}" in r.stdout
    assert re.search(r"invalid.*references-service", r.stdout)
    assert re.search(r"missing-service", r.stdout)
    assert re.search(r"path-escape", r.stdout)
    assert re.search(r"not-evaluated.*compose-dockerfile", r.stdout)
    json_run = f.run(["--explain"])
    context = find(json_run.report["explanations"], lambda e: e["location"]["pointer"] == "/services/web/build/context")
    assert context["base"] == f.root
    assert context["status"] == "invalid"
    assert any(reason["code"] == "path-escape" for reason in context["reasons"])
    ref = find(json_run.report["explanations"], lambda e: e["location"]["pointer"] == "/runServices/0")
    assert ref["status"] == "resolved"
    assert ref["targetLocation"]["pointer"] == "/services/web"


def test_explain_keeps_volume_coverage_and_fields_outside_wiring_explicitly_unknown_or_unevaluated(f: Fixture) -> None:
    f.image({"workspaceMount": "type=volume,source=cache,target=/work/", "postCreateCommand": "DO NOT EXECUTE"})
    r = f.run(["--explain", "--strict"], True)
    assert r.status == 1, r.stdout + r.stderr
    assert re.search(r"unknown.*workspace-coverage", r.stdout)
    assert re.search(r"workspace-content-unknown", r.stdout)
    assert re.search(r"not-evaluated.*postCreateCommand", r.stdout)
    assert "DO NOT EXECUTE" not in r.stdout


def test_explain_reports_a_workspace_mount_escape_at_its_declaration_and_skips_the_mapping(f: Fixture) -> None:
    f.image({"workspaceMount": "type=bind,source=${localWorkspaceFolder}/src,target=/work"})
    control = f.run(["--explain", "--strict"])
    assert control.status == 0, control.stdout
    for relation in ["workspace-coverage", "workspace-bind-directory"]:
        assert find(control.report["explanations"], lambda e: e["relation"] == relation)["base"] == os.path.join(f.root, "src")
    f.image({"workspaceMount": "type=bind,source=../outside,target=/work"})
    r = f.run(["--explain"])
    has_error(r, "path-escape")
    source = find(r.report["explanations"], lambda e: e["relation"] == "bind-source")
    assert source["location"]["pointer"] == "/workspaceMount"
    assert source["base"] == f.root
    assert source["status"] == "invalid"
    assert any(reason["code"] == "path-escape" for reason in source["reasons"])
    mapping = find(r.report["explanations"], lambda e: e["relation"] == "workspace-coverage")
    assert mapping["status"] == "not-evaluated"
    assert mapping["targetLocation"]["pointer"] == "/workspaceMount"


def test_explain_preserves_final_model_locations_and_uses_original_or_explicit_compose_bases(f: Fixture) -> None:
    f.comp()
    f.compose(GOOD_COMPOSE)
    f.write("Dockerfile", "FROM scratch")
    f.write("src/Dockerfile", "FROM scratch")
    snapshot = f.write(
        "snapshots/final.json", {"services": {"web": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "volumes": [".:/work"]}}}
    )
    for base in [f.root, os.path.join(f.root, "src")]:
        r = f.run(["--compose-json", snapshot, *([] if base == f.root else ["--compose-base", base]), "--explain", "--strict"])
        assert r.status == 0, r.stdout
        for relation in ["compose-build-context", "compose-dockerfile", "compose-bind-source"]:
            e = find(r.report["explanations"], lambda e: e["relation"] == relation)
            assert e["location"]["file"] == snapshot
            assert e["service"] == "web"
            assert e["base"] == base
            assert e["target"] == (os.path.join(base, "Dockerfile") if relation == "compose-dockerfile" else base)
            assert e["status"] == "resolved"
        assert re.search(r"original override provenance is unavailable", r.report["scope"]["composeInput"]["provenance"])


def test_explain_distinguishes_context_success_from_dockerfile_failure_at_a_shared_string_build_field(f: Fixture) -> None:
    f.comp()
    f.compose({"services": {"web": {**GOOD_COMPOSE["services"]["web"], "build": "src"}}})
    r = f.run(["--explain"])
    has_error(r, "missing-host-path")
    entries = [e for e in r.report["explanations"] if e["location"]["pointer"] == "/services/web/build"]
    assert find(entries, lambda e: e["relation"] == "compose-build-context")["status"] == "resolved"
    dockerfile = find(entries, lambda e: e["relation"] == "compose-dockerfile")
    assert dockerfile["status"] == "invalid"
    assert dockerfile["base"] == os.path.join(f.root, "src")
    assert any(reason["code"] == "missing-host-path" for reason in dockerfile["reasons"])


def test_explain_cannot_turn_remote_bind_unknowns_into_resolved_coverage_or_change_exit_policy(f: Fixture) -> None:
    f.image()
    normal = f.run(["--daemon", "remote"])
    explained = f.run(["--daemon", "remote", "--explain"])
    assert explained.status == 0, explained.stdout
    assert explained.report == normal.report
    for relation in ["bind-source", "workspace-coverage", "workspace-bind-directory"]:
        e = find(explained.report["explanations"], lambda e: e["relation"] == relation)
        assert e["status"] == "unknown"
        assert any(reason["code"] == "remote-bind-unknown" for reason in e["reasons"])
    assert f.run(["--daemon", "remote", "--explain", "--strict"]).status == 1


def test_explain_marks_a_filesystem_interrupted_path_check_unevaluated_instead_of_resolved(f: Fixture) -> None:
    os.symlink("loop", os.path.join(f.root, "loop"))
    f.image({"workspaceMount": "type=bind,source=${localWorkspaceFolder}/loop,target=/work"})
    r = f.run(["--explain"])
    has_error(r, "filesystem-error")
    source = find(r.report["explanations"], lambda e: e["relation"] == "bind-source")
    assert source["location"]["pointer"] == "/workspaceMount"
    assert source["status"] == "not-evaluated"
    assert any(reason["code"] == "evaluation-interrupted" for reason in source["reasons"])
