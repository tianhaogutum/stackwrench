"""Public library entry point. Produces a JSON-compatible, deterministic report."""

import os
from pathlib import Path, PurePosixPath

from .filesystem import inventory, read_rules_file
from .ignore import InputError, decide, parse_rules
from .dimensions import summarize, suggestions, validate_intersections, validate_limits


def _inside(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def analyze(context: str | Path, *, dockerfile: str = "Dockerfile",
            ignore: str | Path | None = None, candidate_ignore: str | Path | None = None,
            max_bytes: int | None = None, largest: int = 10,
            max_dir_bytes: dict[str, int] | None = None,
            max_extension_bytes: dict[str, int] | None = None, top: int = 10,
            max_dir_extension_bytes: dict[tuple[str, str], int] | None = None,
            include_link_targets: bool = True) -> dict:
    """Scan once and evaluate original/candidate rules on the same inventory.

    Rule-file arguments are relative to cwd; dockerfile is relative to context.
    Raises InputError/OSError/UnicodeError on incomplete or unsupported input.
    max_bytes counts included regular-file payload only, excluding controls.
    max_dir_extension_bytes maps (context-relative directory, final suffix)
    tuples to limits, counting only files matching both selectors.
    Set include_link_targets=False to omit even reading symlink text.
    """
    if max_bytes is not None and (not isinstance(max_bytes, int) or max_bytes < 0):
        raise InputError("max_bytes must be a nonnegative integer")
    if not isinstance(largest, int) or largest < 0:
        raise InputError("largest must be a nonnegative integer")
    if type(top) is not int or top < 0:
        raise InputError("top must be a nonnegative integer")
    directory_limits = validate_limits(max_dir_bytes, directory=True)
    extension_limits = validate_limits(max_extension_bytes, directory=False)
    intersection_limits = validate_intersections(max_dir_extension_bytes)
    root = Path(os.path.abspath(context))
    docker_parts = PurePosixPath(dockerfile)
    if (not dockerfile or dockerfile == "." or docker_parts.is_absolute()
            or ".." in docker_parts.parts or "\x00" in dockerfile):
        raise InputError("dockerfile must be a file path relative to context without '..'")
    docker_path = root / docker_parts
    entries = inventory(root, include_link_targets=include_link_targets)
    by_path = {entry.path: entry for entry in entries}
    for directory in sorted(directory_limits.keys() | {pair[0] for pair in intersection_limits}):
        if directory not in by_path or by_path[directory].kind != "directory":
            raise InputError(f"{directory!r}: directory budget must name an existing directory")
    docker_rel = docker_parts.as_posix()
    if docker_rel in by_path and by_path[docker_rel].kind != "file":
        raise InputError(f"{docker_path}: Dockerfile control must be a regular file")
    if docker_rel not in by_path and dockerfile != "Dockerfile":
        raise InputError(f"{docker_path}: selected Dockerfile is missing")

    if ignore is None:
        specific = Path(str(docker_path) + ".dockerignore")
        # A missing nested Dockerfile was rejected above, so its parent exists.
        text = read_rules_file(specific, optional=True)
        if text is not None:
            ignore_path, selection = specific, "dockerfile-specific"
        else:
            ignore_path, selection = root / ".dockerignore", "root"
            text = read_rules_file(ignore_path, optional=True)
            if text is None:
                selection = "none"
    else:
        ignore_path = Path(os.path.abspath(ignore))
        text = read_rules_file(ignore_path)
        selection = "explicit"
    baseline_source = str(ignore_path) if text is not None else None
    baseline_rules = parse_rules(text or "", baseline_source or "<no ignore file>")
    candidate_path = Path(os.path.abspath(candidate_ignore)) if candidate_ignore is not None else None
    candidate_rules = None
    if candidate_path is not None:
        candidate_rules = parse_rules(read_rules_file(candidate_path), str(candidate_path))

    control_roles = {docker_rel: "dockerfile"}
    if baseline_source is not None:
        relative = _inside(ignore_path, root)
        if relative is not None:
            control_roles[relative] = "ignore"

    def evaluate(rules: list, source: str | None) -> dict:
        result_entries = []
        for entry in entries:
            included, rule = decide(entry.path, rules)
            row = {"path": entry.path, "kind": entry.kind,
                   "logical_bytes": entry.size, "included_by_rules": included,
                   "role": control_roles.get(entry.path, "payload"), "last_rule": rule}
            if entry.target is not None:
                row["link_target"] = entry.target
            result_entries.append(row)
        payload_files = [row for row in result_entries if row["kind"] == "file" and row["role"] == "payload"]
        included_files = [row for row in payload_files if row["included_by_rules"]]
        controls = [row for row in result_entries if row["role"] != "payload"]
        symlinks = [row for row in result_entries if row["kind"] == "symlink"]
        total = sum(row["logical_bytes"] for row in included_files)
        contributions, budget = summarize(result_entries, max_bytes, directory_limits, extension_limits,
                                          top, intersection_limits)
        return {
            "rules_source": source,
            "summary": {
                "included_payload_bytes": total,
                "included_payload_files": len(included_files),
                "excluded_payload_bytes": sum(row["logical_bytes"] for row in payload_files) - total,
                "excluded_payload_files": len(payload_files) - len(included_files),
                "control_bytes_on_disk": sum(row["logical_bytes"] for row in controls),
                "control_files": len(controls),
                "included_symlinks": sum(row["included_by_rules"] for row in symlinks),
                "excluded_symlinks": sum(not row["included_by_rules"] for row in symlinks),
                "directories": sum(row["kind"] == "directory" for row in result_entries),
            },
            "budget": budget,
            "contributions": contributions,
            "ignore_suggestions": suggestions(result_entries, contributions, budget, top,
                                              directory_limits, extension_limits, intersection_limits),
            "largest_included_files": sorted(included_files, key=lambda row: (-row["logical_bytes"], row["path"]))[:largest],
            "control_files": controls,
            "entries": result_entries,
        }

    baseline = evaluate(baseline_rules, baseline_source)
    candidate = evaluate(candidate_rules, str(candidate_path)) if candidate_rules is not None else None
    result = {"schema_version": 1, "status": "ok", "context": str(root),
              "dockerfile": docker_rel, "ignore_selection": selection,
              "model": "regular-file logical bytes; controls separate; symlink targets never read",
              "ignore_suggestion_policy": "Independent appended exclusions, never applied; deltas are not additive. Review build inputs. Patterns also affect future matching paths and descendants, including directories named with an extension.",
              "baseline": baseline, "candidate": candidate,
              "comparison": None, "budget_applies_to": "candidate" if candidate else "baseline"}
    if candidate is not None:
        changes = []
        for before, after in zip(baseline["entries"], candidate["entries"]):
            if before["included_by_rules"] != after["included_by_rules"]:
                changes.append({"path": before["path"], "kind": before["kind"],
                                "logical_bytes": before["logical_bytes"], "role": before["role"],
                                "baseline_included": before["included_by_rules"],
                                "candidate_included": after["included_by_rules"],
                                "baseline_last_rule": before["last_rule"],
                                "candidate_last_rule": after["last_rule"]})
        result["comparison"] = {
            "included_payload_bytes_delta": candidate["summary"]["included_payload_bytes"] - baseline["summary"]["included_payload_bytes"],
            "included_payload_files_delta": candidate["summary"]["included_payload_files"] - baseline["summary"]["included_payload_files"],
            "changes": changes,
        }
    selected = candidate if candidate is not None else baseline
    if selected["budget"]["passed"] is False:
        result["status"] = "budget_exceeded"
    return result
