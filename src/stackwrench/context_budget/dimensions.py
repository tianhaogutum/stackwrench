"""Logical payload contribution groups and matcher-verified append-only advice."""

from .ignore import InputError, decide, parse_rules


NO_EXTENSION = "<none>"


def extension(path: str) -> str:
    """Final suffix, excluding a leading-only or trailing dot, on all Python versions."""
    name = path.rpartition("/")[2]
    index = name.rfind(".")
    return name[index:] if 0 < index < len(name) - 1 else NO_EXTENSION


def validate_limits(limits: dict | None, *, directory: bool) -> dict:
    if limits is None:
        return {}
    if not isinstance(limits, dict):
        raise InputError("dimension budgets must be mappings of selectors to nonnegative bytes")
    for key, value in limits.items():
        if not isinstance(key, str) or not key or "\x00" in key:
            raise InputError("budget selector must be a nonempty string without NUL")
        if directory:
            if any(part in ("", ".", "..") for part in key.split("/")):
                raise InputError("directory budget requires a normalized context-relative directory")
        elif key != NO_EXTENSION and (not key.startswith(".") or "/" in key
                                      or extension("file" + key) != key):
            raise InputError("extension budget requires a final suffix such as .bin or <none>")
        if type(value) is not int or value < 0:
            raise InputError("dimension budget must be a nonnegative integer")
    return dict(limits)


def _payload(row: dict) -> bool:
    return row["kind"] == "file" and row["role"] == "payload" and row["included_by_rules"]


def validate_intersections(limits: dict | None) -> dict:
    if limits is None:
        return {}
    if not isinstance(limits, dict):
        raise InputError("intersection budgets must map (directory, extension) tuples to bytes")
    for pair, value in limits.items():
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise InputError("intersection budget selector must be a (directory, extension) tuple")
        validate_limits({pair[0]: value}, directory=True)
        validate_limits({pair[1]: value}, directory=False)
    return dict(limits)


def _in_intersection(row: dict, directory: str, suffix: str) -> bool:
    return row["path"].startswith(directory + "/") and extension(row["path"]) == suffix


def summarize(entries: list[dict], max_bytes: int | None, directories: dict,
              extensions: dict, top: int, intersections: dict, *,
              budget_file_counts: bool = False) -> tuple[dict, dict]:
    """Directory groups overlap (subtrees); extension groups partition payload."""
    dir_groups = {row["path"]: [0, 0] for row in entries if row["kind"] == "directory"}
    ext_groups = {}
    intersection_groups = dict.fromkeys(intersections, (0, 0))
    total = 0
    for row in entries:
        if not _payload(row):
            continue
        size = row["logical_bytes"]
        total += size
        suffix = extension(row["path"])
        parts = row["path"].split("/")
        for i in range(1, len(parts)):
            directory = "/".join(parts[:i])
            group = dir_groups[directory]
            group[0] += size
            group[1] += 1
            pair = (directory, suffix)
            if pair in intersection_groups:
                before_size, before_count = intersection_groups[pair]
                intersection_groups[pair] = (before_size + size, before_count + 1)
        group = ext_groups.setdefault(suffix, [0, 0])
        group[0] += size
        group[1] += 1

    def contributions(groups, key):
        return [{key: name, "included_payload_bytes": size, "included_payload_files": count}
                for name, (size, count) in sorted(groups.items(), key=lambda item: (-item[1][0], item[0]))[:top]]

    def budgets(groups, limits, key):
        return [{key: name, "included_payload_bytes": groups.get(name, [0, 0])[0],
                 **({"included_payload_files": groups.get(name, [0, 0])[1]} if budget_file_counts else {}),
                 "max_bytes": limit, "passed": groups.get(name, [0, 0])[0] <= limit}
                for name, limit in sorted(limits.items())]

    dir_budgets = budgets(dir_groups, directories, "path")
    ext_budgets = budgets(ext_groups, extensions, "extension")
    intersection_budgets = [
        {"path": directory, "extension": suffix,
         "included_payload_bytes": intersection_groups[(directory, suffix)][0],
         "included_payload_files": intersection_groups[(directory, suffix)][1],
         "max_bytes": limit, "passed": intersection_groups[(directory, suffix)][0] <= limit}
        for (directory, suffix), limit in sorted(intersections.items())]
    checks = [row["passed"] for row in dir_budgets + ext_budgets + intersection_budgets]
    total_passed = total <= max_bytes if max_bytes is not None else None
    if total_passed is not None:
        checks.append(total_passed)
    return ({"directories": contributions(dir_groups, "path"),
             "extensions": contributions(ext_groups, "extension")},
            {"max_bytes": max_bytes, "total_passed": total_passed,
             "directories": dir_budgets, "extensions": ext_budgets,
             "directory_extensions": intersection_budgets,
             "passed": all(checks) if checks else None})


def contribution_changes(baseline: list[dict], current: list[dict], top: int) -> dict:
    """Changed saved groups, ordered by absolute byte delta then selector.

    Callers validate both complete inventories first. At most len(entries)
    directory or suffix groups exist, so summarize returns all groups here;
    truncation must happen after joining both sides and computing deltas.
    """
    result = {"directories": [], "extensions": []}
    if top == 0:
        return result
    before = summarize(baseline, None, {}, {}, len(baseline), {})[0]
    after = summarize(current, None, {}, {}, len(current), {})[0]
    for dimension, key in (("directories", "path"), ("extensions", "extension")):
        old = {row[key]: row for row in before[dimension]}
        new = {row[key]: row for row in after[dimension]}
        rows = []
        for selector in old.keys() | new.keys():
            row = {key: selector}
            for unit in ("bytes", "files"):
                field = "included_payload_" + unit
                b = old.get(selector, {}).get(field, 0)
                c = new.get(selector, {}).get(field, 0)
                row["baseline_" + field] = b
                row["current_" + field] = c
                row[field + "_delta"] = c - b
            if row["included_payload_bytes_delta"] or row["included_payload_files_delta"]:
                rows.append(row)
        result[dimension] = sorted(rows, key=lambda r: (-abs(r["included_payload_bytes_delta"]), r[key]))[:top]
    return result


def _literal(value: str) -> str | None:
    # Line delimiters, parser-trimmed suffixes and control whitespace cannot be
    # represented safely by this matcher. Omit advice, never approximate names.
    if (not value or value[0].isspace() or value[-1].isspace() or value.startswith("\ufeff")
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        return None
    return "".join("\\" + ch if ch in "*?[]!#\\ -^" else ch for ch in value)


def suggestions(entries: list[dict], contributions: dict, budget: dict, top: int,
                directories: dict, extensions: dict, intersections: dict) -> list[dict]:
    """Evaluate each proposed final exclusion independently on the same inventory.

    Earlier rules cannot override an appended exclusion. Testing it only against
    currently included entries is equivalent to reevaluating the full rule list.
    No input file is ever opened or written here.
    """
    if budget["passed"] is not False or top == 0:
        return []
    choices = {}

    def add(selector, kind, prefix=""):
        literal = _literal(selector)
        if literal is not None:
            choices.setdefault(prefix + literal, {"kind": kind, "selector": selector})

    for row in contributions["directories"] + [row for row in budget["directories"] if not row["passed"]]:
        add(row["path"], "directory_subtree")
    for row in contributions["extensions"] + [row for row in budget["extensions"] if not row["passed"]]:
        if row["extension"] != NO_EXTENSION:
            add(row["extension"], "extension_pattern", "**/*")
    files = sorted((row for row in entries if _payload(row)),
                   key=lambda row: (-row["logical_bytes"], row["path"]))[:top]
    for row in files:
        add(row["path"], "file_path")
    for row in budget["directory_extensions"]:
        if row["passed"]:
            continue
        directory, suffix = row["path"], row["extension"]
        if suffix == NO_EXTENSION:
            # No supported glob expresses suffix-less files exactly. Offer
            # bounded literal files even when this pair is below top-N groups.
            matches = sorted((entry for entry in entries if _payload(entry)
                              and _in_intersection(entry, directory, suffix)),
                             key=lambda entry: (-entry["logical_bytes"], entry["path"]))[:top]
            for entry in matches:
                add(entry["path"], "file_path")
        else:
            literal_dir, literal_suffix = _literal(directory), _literal(suffix)
            if literal_dir is not None and literal_suffix is not None:
                choices.setdefault(literal_dir + "/**/*" + literal_suffix,
                                   {"kind": "directory_extension_pattern", "selector": directory,
                                    "extension": suffix})

    results = []
    for pattern, scope in sorted(choices.items()):
        try:
            rules = parse_rules(pattern, "<suggestion>")
        except (InputError, UnicodeError):
            continue
        if len(rules) != 1 or rules[0].negated:
            continue
        changed = []
        after = []
        for row in entries:
            if row["included_by_rules"] and not decide(row["path"], rules)[0]:
                changed.append({"path": row["path"], "kind": row["kind"], "role": row["role"],
                                "logical_payload_bytes_delta": -row["logical_bytes"] if _payload(row) else 0})
                after.append({**row, "included_by_rules": False})
            else:
                after.append(row)
        delta = sum(row["logical_payload_bytes_delta"] for row in changed)
        if delta >= 0:
            continue
        _, budget_after = summarize(after, budget["max_bytes"], directories, extensions, 0, intersections)
        # Advice must reduce at least one exceeded limit, even if one exclusion
        # cannot satisfy every limit. Do not recommend unrelated build inputs.
        helps = budget["total_passed"] is False
        for dimension in ("directories", "extensions", "directory_extensions"):
            helps |= any(not before["passed"] and after["included_payload_bytes"] < before["included_payload_bytes"]
                         for before, after in zip(budget[dimension], budget_after[dimension]))
        if not helps:
            continue
        results.append({"append_pattern": pattern, "scope": scope,
                        "logical_payload_bytes_delta": delta,
                        "included_payload_files_delta": -sum(_payload(row) and not decide(row["path"], rules)[0]
                                                             for row in entries),
                        "changed_entries": changed, "budget_after": budget_after})
    return sorted(results, key=lambda row: (row["logical_payload_bytes_delta"], row["append_pattern"]))[:top]
