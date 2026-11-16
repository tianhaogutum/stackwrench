"""``stackwrench check`` command line: the README demo, formats, exit codes and selection."""
import json
import re

import pytest

from stackwrench import __version__, cli as dispatcher
from stackwrench.check.cli import parse_size
from stackwrench.check.registry import AREAS, CHECKS

from .conftest import findings, rule_ids

SUMMARY = re.compile(r"^\d+ files? · \d+ errors? · \d+ warnings? · \d+ info$", re.MULTILINE)


def test_help_uses_program_name(check):
    code, out, _ = check("--help")
    assert code == 0
    assert "usage: stackwrench check" in out
    assert "GITHUB_STEP_SUMMARY" in out


def test_dispatcher_routes_to_check(demo, capsys):
    assert dispatcher.main(["check", "--color", "never"]) == 1
    assert SUMMARY.search(capsys.readouterr().out)


def test_list_checks_names_every_check_by_area(check):
    code, out, _ = check("--list-checks")
    assert code == 0
    for area in AREAS:
        assert f"\n{area}:\n" in out
    for item in CHECKS:
        assert re.search(rf"^  {re.escape(item.name)}\s", out, re.MULTILINE), item.name
    assert "uses: stackwrench compose-env (trace)" in out


def test_check_names_match_subcommands():
    assert [c.name for c in CHECKS] == ["compose-env", "compose-merge", "podman-audit", "context-budget",
                                        "cache-replay", "devcontainer", "pins", "kind-images", "just", "task-audit"]
    names = {command.name for command in dispatcher.COMMANDS}
    assert {c.name for c in CHECKS} <= names


def test_readme_demo_output(demo, check):
    """The lines shown (illustratively) in assets/check-demo.svg and the README."""
    code, out, err = check("--color", "never")
    assert code == 1 and err == ""
    assert out.startswith("stackwrench check  .  (10 files: 3 Compose files, 1 .env file, 1 Dev Container config, "
                          "1 mise config, 1 version file, 1 Dockerfile, 1 Taskfile, 1 justfile)\n")
    for area in AREAS:
        assert f"\n{area}\n" in out
    expected = [
        r"warn   compose-env/env-file-not-interpolated +API_URL: \$\{API_URL\} is not set for interpolation "
        r"\(\.env does not define it\), so `environment: API_URL` becomes \"\" and hides API_URL from "
        r"web\.env line 2  line 12$",
        r"warn   compose-env/empty-overrides-value +LOG_LEVEL: `environment:` sets LOG_LEVEL to an empty string "
        r"\(empty is not unset\), which wins over LOG_LEVEL from web\.env line 3  line 13$",
        r"warn   compose-env/inherited-unset +SENTRY_DSN: `SENTRY_DSN:` passes SENTRY_DSN through from the "
        r"environment, but \.env does not set it",
        r"info   compose-env/dotenv-unused +FEATURE_FLAGS is set in \.env but not used by compose\.yaml",
        r"warn   podman-audit/healthy-gate-hang +web: waits for db with condition: service_healthy; "
        r"podman-compose 1\.6\.0 was reported to hang",
        r"warn   podman-audit/healthy-gate-without-healthcheck +web: waits for db to be healthy, but db declares "
        r"no healthcheck",
        r"warn   podman-audit/start-interval +redis: healthcheck\.start_interval:",
        r"warn   compose-merge/ports-merged +services\.redis\.ports: infra/compose\.override\.yaml adds 6380:6379, "
        r"and 6379:6379 from infra/compose\.yaml:7 stays published",
        r"info   compose-merge/command-replaced +services\.redis\.command: the value from infra/compose\.yaml:5 "
        r"is replaced, not appended: redis-server --appendonly yes  line 4$",
        r"warn   cache-replay/broad-copy-before-run +COPY \. \. copies 15 files before RUN npm install \(line 4\): "
        r"editing any one of them, such as src/server\.js, re-runs it  line 3$",
        r"warn   context-budget/junk-included +node_modules/ is sent with build context \. \(1 file, 64 B\): "
        r"there is no \.dockerignore$",
        r"warn   context-budget/sensitive-file +\.env is sent with build context \.",
        r"info   context-budget/size +build context \.: 14 files, 4\.3 kB \(no \.dockerignore\)",
        r"error  devcontainer/missing-host-path +Missing file: \.devcontainer/Dockerfile  line 6$",
        r"error  devcontainer/missing-host-path +Missing bind source: \.npm-cache  line 11$",
        r"warn   pins/drift +node: `FROM node:20\.11\.1-alpine` uses node 20\.11\.1; mise\.toml:3 pins 22\.14\.0  "
        r"line 1$",
        r"info   pins/summary +2 pins compared with mise\.toml: 1 match, 1 drift$",
        r"warn   task-audit/sources-match-nothing +test: a `sources` pattern matches no files",
        r"warn   task-audit/input-not-in-sources +build: runs only when its sources change, but its commands "
        r"also use \{\{\.VERSION\}\}",
        r"warn   task-audit/outputs-without-inputs +release-notes: generates dist/CHANGELOG\.txt but declares no "
        r"sources or status",
        r"error  just/missing-dependency +recipe 'deploy' depends on missing recipe 'biuld': just refuses to run "
        r"any recipe in this file  line 12$",
    ]
    for pattern in expected:
        assert re.search(r"^    " + pattern, out, re.MULTILINE), pattern
    assert "Not applicable here: kind-images (no kind cluster config)" in out
    assert "\n10 files · 3 errors · 14 warnings · 4 info\n" in out
    assert "\x1b[" not in out


def test_demo_json_document(demo, check_json):
    code, document = check_json()
    assert code == 1
    assert document["format"] == "stackwrench.check/v1"
    assert document["tool"] == {"name": "stackwrench", "version": __version__}
    assert document["summary"]["files"] == 10
    assert (document["summary"]["error"], document["summary"]["warning"], document["summary"]["info"]) == (3, 14, 4)
    assert document["skipped"] == [{"check": "kind-images", "reason": "no kind cluster config"}]
    assert document["failOn"] == "error" and document["failed"] is True
    areas = {f["ruleId"]: f["area"] for f in document["findings"]}
    assert areas["compose-env/inherited-unset"] == "Compose"
    assert areas["context-budget/sensitive-file"] == "Docker builds"
    assert areas["pins/drift"] == "Dev environments"
    assert areas["just/missing-dependency"] == "Task runners"


def test_demo_never_writes(demo, check):
    before = sorted((p.relative_to(demo).as_posix(), p.stat().st_mtime_ns) for p in demo.rglob("*"))
    for fmt in ("text", "json", "sarif", "markdown"):
        check("--format", fmt)
    after = sorted((p.relative_to(demo).as_posix(), p.stat().st_mtime_ns) for p in demo.rglob("*"))
    assert before == after


def test_sarif_is_2_1_0(demo, check):
    code, out, _ = check("--format", "sarif")
    assert code == 1
    document = json.loads(out)
    assert document["version"] == "2.1.0"
    run = document["runs"][0]
    assert run["tool"]["driver"]["name"] == "stackwrench"
    ids = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert "pins/drift" in ids
    drift = next(r for r in run["results"] if r["ruleId"] == "pins/drift")
    location = drift["locations"][0]["physicalLocation"]
    assert location["artifactLocation"] == {"uri": "Dockerfile", "uriBaseId": "%SRCROOT%"}
    assert location["region"]["startLine"] == 1


def test_markdown_report(demo, check):
    code, out, _ = check("--format", "markdown")
    assert code == 1
    assert out.startswith("## stackwrench check\n\n**Failed** (fail on: error) · 3 errors · 14 warnings · 4 info")
    assert "### Compose" in out and "### Task runners" in out
    assert "<details><summary>" in out


@pytest.mark.parametrize(("fail_on", "expected"), [("error", 1), ("warning", 1), ("never", 0)])
def test_fail_on(demo, check, fail_on, expected):
    code, _, _ = check("--fail-on", fail_on)
    assert code == expected


def test_only_and_skip(demo, check_json):
    code, document = check_json("--only", "pins,compose-merge")
    assert document["checks"] == ["compose-merge", "pins"]
    assert {f["check"] for f in document["findings"]} == {"compose-merge", "pins"}
    assert code == 0  # warnings and info only
    _, document = check_json("--skip", "devcontainer", "--skip", "just")
    assert not [f for f in document["findings"] if f["check"] in ("devcontainer", "just")]


def test_usage_errors_exit_2(repo, check):
    code, _, err = check("--only", "nope")
    assert code == 2 and "unknown check(s): nope" in err
    code, _, err = check("does-not-exist")
    assert code == 2 and "no such directory" in err
    repo.write("file.txt", "x")
    code, _, err = check("file.txt")
    assert code == 2 and "not a directory" in err
    code, _, err = check("--only", "pins", "--skip", "pins")
    assert code == 2 and "leave no check" in err
    code, _, _ = check("--podman-compose-version", "latest")
    assert code == 2


def test_empty_repository_is_clean(repo, check):
    code, out, _ = check("--color", "never")
    assert code == 0
    assert "No dev-stack files found" in out
    assert "\n0 files · 0 errors · 0 warnings · 0 info\n" in out


def test_crash_becomes_internal_finding(demo, check_json, monkeypatch):
    from stackwrench.check import registry

    def boom(context):
        raise RuntimeError("synthetic failure with secret-looking text")

    broken = [registry.Check(c.name, c.area, c.summary, c.uses, boom if c.name == "pins" else c.run, c.applies,
                             c.rules) for c in registry.CHECKS]
    monkeypatch.setattr(registry, "CHECKS", tuple(broken))
    monkeypatch.setattr(registry, "BY_NAME", {c.name: c for c in broken})
    code, document = check_json()
    internal = findings(document, "pins/internal")
    assert len(internal) == 1 and internal[0]["severity"] == "error"
    assert "RuntimeError" in internal[0]["message"] and "secret" not in internal[0]["message"]
    assert findings(document, "just/missing-dependency")  # other checks still ran


def test_verbose_shows_hints(demo, check):
    _, out, _ = check("-v", "--color", "never", "--only", "pins")
    assert "-> `stackwrench pins plan --root .`" in out


@pytest.mark.parametrize(("text", "expected"), [("500000", 500000), ("500kB", 500000), ("50MB", 50_000_000),
                                                ("1MiB", 1048576), ("2.5k", 2500)])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_context_budget_option(demo, check_json):
    _, document = check_json("--only", "context-budget", "--context-budget", "1kB")
    over = findings(document, "context-budget/over-budget")
    assert len(over) == 1
    assert "over the 1.0 kB budget" in over[0]["message"]
