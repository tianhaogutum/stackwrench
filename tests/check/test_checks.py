"""Individual checks on small synthetic repositories."""
from stackwrench.check import docker
from stackwrench.check.compose import json_path_parts

from .conftest import findings, rule_ids


def test_compose_env_unset_variable_without_env_file(repo, check_json):
    repo.write("compose.yaml", """
        services:
          app:
            image: "example.invalid/app:${TAG}"
    """)
    _, document = check_json("--only", "compose-env")
    [finding] = findings(document, "compose-env/unset-substituted-empty")
    assert finding["subject"] == "TAG" and finding["file"] == "compose.yaml" and finding["line"] == 3


def test_compose_env_required_variable(repo, check_json):
    repo.write("compose.yaml", """
        services:
          app:
            image: "example.invalid/app:${TAG:?set TAG}"
    """)
    repo.write(".env", "OTHER=1\n")
    _, document = check_json("--only", "compose-env")
    [finding] = findings(document, "compose-env/required-unset")
    assert finding["subject"] == "TAG" and finding["line"] == 3
    assert "set TAG" not in finding["message"]  # the custom message is never quoted


def test_compose_env_outside_subset_is_info(repo, check_json):
    repo.write("compose.yaml", """
        x-common: &common
          image: example.invalid/app
        services:
          app: *common
    """)
    code, document = check_json("--only", "compose-env")
    assert code == 0
    assert rule_ids(document) == ["compose-env/not-analysed"]


def test_compose_merge_needs_an_override(repo, check_json):
    repo.write("compose.yaml", "services:\n  app:\n    image: example.invalid/app\n")
    _, document = check_json("--only", "compose-merge")
    assert document["findings"] == []
    assert document["skipped"] == [{"check": "compose-merge", "reason": "no Compose override file"}]


def test_compose_merge_reports_unsupported_input(repo, check_json):
    repo.write("compose.yaml", "services:\n  app:\n    image: example.invalid/app\n    build: .\n")
    repo.write("compose.override.yaml", "services:\n  app:\n    image: example.invalid/other\n")
    _, document = check_json("--only", "compose-merge")
    [finding] = findings(document, "compose-merge/not-analysed")
    assert finding["severity"] == "info" and finding["file"] == "compose.yaml" and finding["line"] == 4


def test_podman_missing_dependency_is_an_error(repo, check_json):
    repo.write("compose.yaml", """
        services:
          app:
            build: .
            depends_on:
              - cache
    """)
    code, document = check_json("--only", "podman-audit")
    [finding] = findings(document, "podman-audit/missing-dependency")
    assert finding["severity"] == "error" and finding["subject"] == "app" and finding["line"] == 5
    assert code == 1


def test_json_path_parts():
    assert json_path_parts('$.services.web.depends_on.db') == ["services", "web", "depends_on", "db"]
    assert json_path_parts('$.services["my-app"].ports[0]') == ["services", "my-app", "ports", 0]


def test_context_budget_respects_dockerignore(repo, check_json):
    repo.write("Dockerfile", "FROM python:3.12.1\nCOPY app.py /app/\n")
    repo.write("app.py", "print('hi')\n")
    repo.write(".venv/lib/x.py", "x = 1\n")
    repo.write(".env.example", "A=\n")
    repo.write(".env.local", "A=1\n")
    _, document = check_json("--only", "context-budget")
    assert [f["message"].split(" ")[0] for f in findings(document, "context-budget/junk-included")] == [".venv/"]
    assert [f["message"].split(" ")[0] for f in findings(document, "context-budget/sensitive-file")] == [".env.local"]
    repo.write(".dockerignore", ".venv\n.env.local\n")
    _, document = check_json("--only", "context-budget")
    assert rule_ids(document) == ["context-budget/size"]
    assert "rules: .dockerignore" in document["findings"][0]["message"]


def test_sensitive_names():
    assert docker.is_sensitive(".env") and docker.is_sensitive("conf/.env.production")
    assert docker.is_sensitive("certs/server.key") and docker.is_sensitive("id_ed25519")
    assert not docker.is_sensitive(".env.example") and not docker.is_sensitive("web.env")


def test_cache_replay_staged_copy_is_clean(repo, check_json):
    repo.write("Dockerfile", """
        FROM python:3.12.1-slim
        WORKDIR /app
        COPY requirements.txt ./
        RUN pip install -r requirements.txt
        COPY . .
        CMD ["python", "app.py"]
    """)
    repo.write("requirements.txt", "requests==2.32.3\n")
    repo.write("app.py", "print('hi')\n")
    _, document = check_json("--only", "cache-replay")
    assert document["findings"] == []


def test_cache_replay_multi_stage_is_not_analysed(repo, check_json):
    repo.write("Dockerfile", "FROM golang:1.23.2 AS build\nCOPY . .\nRUN go build\nFROM scratch\n")
    repo.write("main.go", "package main\n")
    _, document = check_json("--only", "cache-replay")
    assert rule_ids(document) == ["cache-replay/not-analysed"]


def test_model_text_blanks_metadata_and_keeps_lines():
    text = "# syntax=docker/dockerfile:1\nFROM node:22.14.0\nEXPOSE 3000\nLABEL a=b \\\n  c=d\nRUN true\n"
    assert docker.model_text(text).split("\n") == ["", "FROM node:22.14.0", "", "", "", "RUN true", ""]


def test_pins_drift_in_version_file(repo, check_json):
    repo.write("mise.toml", "[tools]\npython = \"3.12.7\"\nterraform = \"1.9.8\"\n")
    repo.write(".python-version", "3.12.1\n")
    _, document = check_json("--only", "pins")
    [drift] = findings(document, "pins/drift")
    assert drift["file"] == ".python-version" and drift["line"] == 1
    assert "says 3.12.1; mise.toml:2 pins 3.12.7" in drift["message"]
    [skipped] = findings(document, "pins/tools-not-compared")
    assert skipped["message"].startswith("terraform in mise.toml not compared")


def test_pins_floating_tag(repo, check_json):
    repo.write("mise.toml", "[tools]\nnode = \"22.14.0\"\n")
    repo.write("Dockerfile", "FROM node:22-alpine\nFROM postgres:16 AS db\n")
    _, document = check_json("--only", "pins")
    assert rule_ids(document, "Dockerfile") == ["pins/floating-tag"]


def test_pins_tool_versions_only(repo, check_json):
    repo.write(".tool-versions", "nodejs 22.14.0\n")
    _, document = check_json("--only", "pins")
    assert rule_ids(document) == ["pins/tool-versions-not-read"]


def test_just_cycle_and_unparsed_recipe(repo, check_json):
    repo.write("justfile", """
        a: b
            echo a

        b: a
            echo b
    """)
    _, document = check_json("--only", "just")
    assert rule_ids(document) == ["just/dependency-cycle"]
    repo.write("justfile", """
        build target='web':
            echo {{target}}

        deploy: build
            echo deploy
    """)
    _, document = check_json("--only", "just")
    # `build` exists but has parameters: not a missing dependency, only outside the bridge's subset.
    assert rule_ids(document) == ["just/outside-subset"]


def test_task_audit_external_env_input(repo, check_json):
    repo.write("Taskfile.yml", """
        version: '3'
        tasks:
          build:
            sources: [main.go]
            cmds:
              - 'go build -tags "$BUILD_TAGS" -o "$HOME/bin/app"'
    """)
    repo.write("main.go", "package main\n")
    _, document = check_json("--only", "task-audit")
    [finding] = findings(document, "task-audit/input-not-in-sources")
    assert "$BUILD_TAGS" in finding["message"] and "$HOME" not in finding["message"]
    assert finding["line"] == 3


def test_kind_without_snapshots(repo, check_json):
    repo.write("kind-config.yaml", """
        kind: Cluster
        apiVersion: kind.x-k8s.io/v1alpha4
        nodes:
          - role: control-plane
    """)
    repo.write("k8s/web.yaml", """
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: web
        spec:
          template:
            spec:
              containers:
                - name: web
                  image: example.invalid/web:latest
    """)
    _, document = check_json("--only", "kind-images")
    [always] = findings(document, "kind-images/always-pull")
    assert always["file"] == "k8s/web.yaml" and always["subject"] == "Deployment/web"
    assert findings(document, "kind-images/needs-snapshots")


def test_missing_tool_package_is_one_info_finding(demo, check_json, monkeypatch):
    from stackwrench.check import devenv

    def missing():
        raise ModuleNotFoundError("no port", name="stackwrench.devcontainer.check")

    monkeypatch.setattr(devenv, "devcontainer_api", missing)
    _, document = check_json("--only", "devcontainer")
    assert rule_ids(document) == ["devcontainer/unavailable"]
