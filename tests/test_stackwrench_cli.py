"""Top-level `stackwrench` dispatcher: overview, version, routing and error paths."""
from pathlib import Path
import subprocess
import sys

import pytest

import stackwrench
from stackwrench import cli

ROOT = Path(__file__).resolve().parents[1]

# Commands whose Python subpackage ships in this repository (`check` is the hero command).
TOOLS = ["check", "compose-env", "compose-merge", "podman-audit", "context-budget", "cache-replay",
         "devcontainer", "pins", "kind-images", "just", "task-audit"]
# Every merged tool prints a "usage: stackwrench <tool>" line for `--help`.
ARGPARSE_TOOLS = [tool for tool in TOOLS if tool != "check"]
# `check` keeps its own flags; every merged tool prints the bare package version.
VERSIONED = ARGPARSE_TOOLS
SUBCOMMANDS = {
    "__SUBCOMMANDS__": [],
}
# Tools with argparse subcommands: an unknown subcommand is a usage error (exit 2).
STRICT_SUBCOMMANDS = ["__STRICT__"]
PACKAGES = {
    "compose-env": "stackwrench.compose_env", "compose-merge": "stackwrench.compose_merge",
    "podman-audit": "stackwrench.podman_audit", "context-budget": "stackwrench.context_budget",
    "cache-replay": "stackwrench.cache_replay", "devcontainer": "stackwrench.devcontainer",
    "pins": "stackwrench.pins", "kind-images": "stackwrench.kind_images",
    "just": "stackwrench.just_bridge", "task-audit": "stackwrench.task_audit",
}
LEGACY_NAMES = [
    ("compose-env-trace", "compose-env"), ("compose-merge-trace", "compose-merge"),
    ("compose-podman-audit", "podman-audit"), ("docker-context-budget", "context-budget"),
    ("docker-cache-replay", "cache-replay"), ("devcontainer-wirecheck", "devcontainer"),
    ("mise-pinbridge", "pins"), ("kind-image-plan", "kind-images"), ("just-task-bridge", "just"),
    ("task-fingerprint-audit", "task-audit"),
]


def run(*args, module=("-m", "stackwrench")):
    return subprocess.run([sys.executable, *module, *args], capture_output=True, text=True, timeout=60)


def _main(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["help"]])
def test_overview_lists_every_group_and_command(args, capsys):
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"stackwrench {stackwrench.__version__} ")
    for group in cli.GROUPS:
        assert f"\n{group}:\n" in out
    for name in TOOLS:
        assert f"\n  {name} " in out


def test_command_table_shape():
    groups = {group: [c.name for c in cli.COMMANDS if c.group == group] for group in cli.GROUPS}
    assert groups == {
        "Check": ["check"],
        "Compose": ["compose-env", "compose-merge", "podman-audit"],
        "Docker builds": ["context-budget", "cache-replay"],
        "Dev environments": ["devcontainer", "pins", "kind-images"],
        "Task runners": ["just", "task-audit"],
    }
    assert {c.name: c.module for c in cli.COMMANDS} == {
        "check": "stackwrench.check.cli",
        **{name: f"{package}.cli" for name, package in PACKAGES.items()},
    }
    assert all(c.group in cli.GROUPS and c.summary for c in cli.COMMANDS)
    assert set(cli.BY_NAME) == {c.name for c in cli.COMMANDS}
    # PyYAML is the one base dependency; no command needs an optional extra today.
    assert {c.name: c.extra for c in cli.COMMANDS if c.extra} == {}
    assert all(bool(c.requires) == bool(c.extra) for c in cli.COMMANDS)


def test_extras_are_declared_in_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for extra in {c.extra for c in cli.COMMANDS if c.extra} | {"test"}:
        assert f"\n{extra} = [" in text, extra
    assert 'dependencies = ["PyYAML>=6.0"]' in text


def test_legacy_table_matches_commands():
    assert dict(LEGACY_NAMES) == cli.LEGACY
    assert all(new.split()[0] in cli.BY_NAME for new in cli.LEGACY.values())


def test_shipped_tools_are_installed():
    for name in ARGPARSE_TOOLS:
        assert cli._installed(cli.BY_NAME[name]), name


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == f"stackwrench {stackwrench.__version__}\n"


def _assert_tool_help(tool, out):
    assert f"stackwrench {tool}" in out
    if tool in ARGPARSE_TOOLS:
        assert f"usage: stackwrench {tool}" in out


@pytest.mark.parametrize("tool", ARGPARSE_TOOLS)
def test_tool_help_uses_new_program_name(tool):
    result = run(tool, "--help")
    assert result.returncode == 0, result.stderr
    _assert_tool_help(tool, result.stdout)


@pytest.mark.parametrize("tool", ARGPARSE_TOOLS)
def test_help_subcommand_routes_to_tool_help(tool, capsys):
    code, out, _ = _main(["help", tool], capsys)
    assert code == 0
    _assert_tool_help(tool, out)


@pytest.mark.parametrize("tool", VERSIONED)
def test_tool_version_is_package_version(tool, capsys):
    code, out, _ = _main([tool, "--version"], capsys)
    assert code == 0
    assert out == f"{stackwrench.__version__}\n"


@pytest.mark.parametrize("command,sub", [(c, s) for c, subs in SUBCOMMANDS.items() for s in subs])
def test_subcommand_help_uses_new_program_name(command, sub, capsys):
    code, out, err = _main([command, sub, "--help"], capsys)
    assert code == 0, err
    assert f"usage: stackwrench {command} {sub}" in out


@pytest.mark.parametrize("tool,package", sorted(PACKAGES.items()))
def test_subpackage_main_routes_through_dispatcher(tool, package):
    result = run("--help", module=("-m", package))
    assert result.returncode == 0, result.stderr
    assert f"usage: stackwrench {tool}" in result.stdout


@pytest.mark.parametrize("tool", STRICT_SUBCOMMANDS)
def test_tool_exit_codes_pass_through(tool):
    bad = run(tool, "no-such-subcommand")
    assert bad.returncode == 2 and f"usage: stackwrench {tool}" in bad.stderr
    assert "Traceback" not in bad.stderr


def test_unknown_command_suggests_close_match(capsys):
    assert cli.main(["compose-evn"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'compose-evn'" in err and "did you mean 'compose-env'?" in err


def test_unknown_legacy_typo_suggests_new_command(capsys):
    assert cli.main(["mise-pinbrige"]) == 2
    assert "did you mean 'pins'?" in capsys.readouterr().err


@pytest.mark.parametrize("legacy,new", LEGACY_NAMES)
def test_unknown_legacy_name_points_to_new_command(legacy, new, capsys):
    assert cli.main([legacy]) == 2
    assert f"{legacy} is now 'stackwrench {new}'" in capsys.readouterr().err


def test_unknown_command_without_match(capsys):
    assert cli.main(["zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'zzzzzz'" in err and "did you mean" not in err


def test_unknown_option(capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "unknown option '--frobnicate'" in capsys.readouterr().err


def test_missing_module_reports_not_available(monkeypatch, capsys):
    ghost = cli.Command("ghost", "stackwrench.ghost_tool.cli", "not shipped", "Check")
    monkeypatch.setitem(cli.BY_NAME, "ghost", ghost)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ghost))
    overview = cli.overview()
    assert "ghost" in overview and "(coming soon)" in overview
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("stackwrench: command 'ghost' is not available") and "Traceback" not in err


def test_missing_optional_dependency_prints_install_hint(monkeypatch, capsys):
    command = cli.Command("ghost", "stackwrench.pins.cli", "needs a dependency", "Check",
                          requires=("ghostdep",), extra="ghostextra")
    monkeypatch.setitem(cli.BY_NAME, "ghost", command)

    def missing(name):
        raise ModuleNotFoundError("No module named 'ghostdep.sub'", name="ghostdep.sub")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err == ("stackwrench ghost needs ghostdep: pipx install 'stackwrench[ghostextra]' "
                   "(or pip install 'stackwrench[ghostextra]')\n")


def test_unrelated_module_error_is_not_masked(monkeypatch):
    command = cli.Command("ghost", "stackwrench.pins.cli", "needs a dependency", "Check",
                          requires=("ghostdep",), extra="ghostextra")
    monkeypatch.setitem(cli.BY_NAME, "ghost", command)

    def missing(name):
        raise ModuleNotFoundError("No module named 'other'", name="other")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["ghost"])


# Simulate an install with a dependency blocked: a None entry in sys.modules makes the import fail.
_WITHOUT = ("import sys\nfor name in {blocked!r}: sys.modules[name] = None\n"
            "from stackwrench.cli import main\nraise SystemExit(main({argv!r}))\n")


def test_missing_extra_exits_2_with_one_line_hint(tmp_path):
    # Exercise the subprocess path with a declared ghost requirement on a real command.
    script = ("import sys\nsys.modules['ghostdep'] = None\n"
              "from stackwrench import cli\n"
              "cmd = cli.Command('pins', 'stackwrench.pins.cli', 'x', 'Dev environments',"
              " requires=('ghostdep',), extra='ghostextra')\n"
              "cli.BY_NAME['pins'] = cmd\n"
              "import importlib\n"
              "def missing(name):\n"
              "    import ghostdep\n"
              "cli.importlib.import_module = missing\n"
              "raise SystemExit(cli.main(['pins', '--help']))\n")
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 2
    assert result.stderr.startswith("stackwrench pins needs ghostdep: pipx install 'stackwrench[ghostextra]'")
    assert result.stderr.count("\n") == 1 and "Traceback" not in result.stderr


def test_commands_do_not_import_optional_packages_eagerly(tmp_path):
    # Blocking PyYAML must never break the dispatcher overview itself (tools load lazily).
    result = subprocess.run([sys.executable, "-c", _WITHOUT.format(blocked=["yaml"], argv=[])],
                            cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("stackwrench ")


@pytest.mark.parametrize("name", TOOLS)
def test_shipped_command_is_listed_without_coming_soon(name, capsys):
    if not cli._installed(cli.BY_NAME[name]):
        pytest.skip(f"{name} has not landed yet")
    assert cli.main([]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith(f"  {name} "))
    assert "(coming soon)" not in line
