"""``stackwrench check``: one read-only pass over a repository's local dev stack.

``check`` finds Compose files and their ``.env``, devcontainer.json, mise.toml
and runtime version files, Dockerfiles and their build contexts, justfiles,
Taskfiles and kind cluster configs, then runs the read-only analyses of the
other stackwrench commands on them (compose-env, compose-merge, podman-audit,
context-budget, cache-replay, devcontainer, pins, kind-images, just,
task-audit). It never writes a file, never executes anything from the
repository and never calls docker, podman, kind or Task.

Library use::

    from stackwrench.check import CheckOptions, discover, run_checks
    discovery = discover(".")
    outcome = run_checks(discovery, options=CheckOptions(context_budget=50_000_000))
    for finding in outcome.findings:
        print(finding.severity, finding.rule_id, finding.file, finding.message)
"""
from __future__ import annotations

from stackwrench import __version__
from .discover import BuildContext, ComposeGroup, Discovery, DiscoveryError, Source, discover
from .model import SEVERITIES, Finding
from .registry import AREAS, CHECKS, CheckOptions, Outcome, run_checks, select

__all__ = [
    "AREAS",
    "CHECKS",
    "SEVERITIES",
    "BuildContext",
    "CheckOptions",
    "ComposeGroup",
    "Discovery",
    "DiscoveryError",
    "Finding",
    "Outcome",
    "Source",
    "__version__",
    "discover",
    "run_checks",
    "select",
]
