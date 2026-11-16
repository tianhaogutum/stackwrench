"""Original static dependency model; no Docker client or command execution."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from fnmatch import fnmatchcase
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import stat


class InputError(ValueError):
    """The input cannot be evaluated within the documented model."""


MAX_INPUT_BYTES = 5 * 1024 * 1024
KINDS = {"content", "mtime", "add", "delete"}
HISTORY_MODES = ("cumulative-history", "cumulative-cold")
MODES = ("independent-warm", "cumulative-history", "independent-cold", "cumulative-cold")
COLD_MISS_WHY = ("Empty synthetic cache for this independent scenario; "
                 "every instruction, including FROM, is assumed to miss.")
COLD_START_MISS_WHY = ("Empty synthetic cache for the first ordered scenario; "
                       "every instruction, including FROM, is assumed to miss.")
ASSUMPTIONS = [
    "Base image identity, Dockerfile text, build arguments and environment remain fixed within each replay.",
    "Mutable FROM tags are assumed unchanged; image identities are never resolved or fetched.",
    "Candidate semantic equivalence is a human assumption, not proven by this tool.",
    "The supplied directory is the complete effective local COPY inventory; no ignore rules are applied.",
    "Instruction states depend linearly on their parent state and selected COPY inputs; RUN text is never executed.",
]
UNMODELED = [
    "Actual BuildKit cache keys/availability, eviction, deduplication, graph optimization and build timing",
    "Cache/bind mounts, secret values, SSH mounts and other RUN flags (rejected)",
    "External image mutation, network side effects, clock and nondeterministic RUN behavior",
    "Directory metadata, file permission/ownership changes and symlinks (symlinks are rejected)",
    "Dockerignore rules, context packing, multi-stage builds and instruction edits between replays",
]


def read_text(path: Path) -> str:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise InputError(f"{path}: text input must be a regular file")
    if info.st_size > MAX_INPUT_BYTES:
        raise InputError(f"{path}: text input exceeds {MAX_INPUT_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def load_json(path: Path):
    def object_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise InputError(f"{path}: duplicate JSON key {key!r}")
            obj[key] = value
        return obj

    def bad_constant(value):
        raise InputError(f"{path}: non-finite JSON number {value}")

    try:
        return json.loads(read_text(path), object_pairs_hook=object_pairs,
                          parse_constant=bad_constant)
    except ValueError as exc:
        raise InputError(f"{path}: invalid JSON: {exc}") from exc


def exact_keys(obj, required: set[str], label: str):
    if not isinstance(obj, dict) or set(obj) != required:
        raise InputError(f"{label}: expected exactly fields {sorted(required)}")


def clean_path(value: str, *, pattern=False, destination=False) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InputError(f"invalid path {value!r}")
    if any(c in value for c in ("\\", "$", ":")):
        raise InputError(f"unsupported expansion, remote/Windows path or escape in {value!r}")
    if "//" in value or ".." in value.split("/"):
        raise InputError(f"ambiguous or escaping path {value!r}")
    if value.startswith("/") and not destination:
        raise InputError(f"absolute source/event path is unsupported: {value!r}")
    if any(c in value for c in "[]{}") or "**" in value:
        raise InputError(f"ambiguous pattern {value!r}: brackets, braces and ** are unsupported")
    if not pattern and any(c in value for c in "*?"):
        raise InputError(f"wildcards are not allowed here: {value!r}")
    if value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/") or ("/" if destination else ".")
    if any(part == "." for part in value.split("/")) and value != ".":
        raise InputError(f"ambiguous path {value!r}")
    return value


@dataclass(frozen=True)
class Instruction:
    id: str
    line: int
    op: str
    argument: str
    sources: tuple[str, ...] = ()
    destination: str | None = None

    def public(self, weight):
        return {"id": self.id, "line": self.line, "op": self.op,
                "argument": self.argument, "sources": list(self.sources), "destination": self.destination,
                "weightModelUnits": weight}


def logical_lines(text: str):
    buffer = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            if re.match(r"#\s*(syntax|escape|check)\s*=", stripped, re.I):
                raise InputError(f"line {number}: Dockerfile parser directives are unsupported")
            continue
        if not buffer:
            start = number
            if stripped == "\\":
                raise InputError(f"line {number}: standalone continuation is unsupported")
        piece = raw.rstrip() if buffer else stripped
        if piece.endswith("\\"):
            if piece.endswith("\\\\"):
                raise InputError(f"line {number}: ambiguous trailing backslashes")
            buffer += piece[:-1]
        else:
            yield start, buffer + piece
            buffer = ""
    if buffer:
        raise InputError(f"line {start}: unfinished continuation")


def words(argument: str) -> list[str]:
    try:
        return shlex.split(argument, comments=False, posix=True)
    except ValueError as exc:
        raise InputError(f"invalid quoted arguments: {exc}") from exc


def json_words(argument: str) -> list[str]:
    try:
        items = json.loads(argument)
    except ValueError as exc:
        raise InputError(f"invalid JSON instruction arguments: {exc}") from exc
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x for x in items):
        raise InputError("JSON instruction arguments must be a nonempty array of nonempty strings")
    return items


def validate_image(image: str):
    """Accept common literal registry/repository[:tag][@sha256:digest] forms."""
    if image == "scratch":
        return
    if "@" in image:
        if image.count("@") != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.split("@", 1)[1]):
            raise InputError("FROM only supports a literal sha256 digest with 64 lowercase hex digits")
        image = image.split("@", 1)[0]
    parts = image.split("/")
    if ":" in parts[-1]:
        name, tag = parts[-1].split(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise InputError("FROM has an unsupported image tag")
        parts[-1] = name
    component = r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*"
    for index, part in enumerate(parts):
        if index == 0 and len(parts) > 1 and ":" in part:
            hostname, port = part.split(":", 1)
            if not re.fullmatch(component, hostname) or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
                raise InputError("FROM has an unsupported registry host/port")
        elif not re.fullmatch(component, part):
            raise InputError("FROM requires a lowercase literal repository path (no URL, IPv6 or expansion)")


def parse_dockerfile(path: Path) -> tuple[Instruction, ...]:
    return _parse_dockerfile(path, read_text(path))


def _parse_dockerfile(path: Path, content: str) -> tuple[Instruction, ...]:
    result = []
    allowed = {"FROM", "WORKDIR", "COPY", "RUN", "ENV", "ARG"}
    if any(ord(c) < 32 and c not in "\n\r\t" for c in content):
        raise InputError(f"{path}: unsupported control character in Dockerfile")
    for line, text in logical_lines(content):
        match = re.fullmatch(r"([A-Za-z]+)\s+(.+)", text)
        if not match:
            raise InputError(f"{path}:{line}: expected instruction and nonempty arguments")
        op, arg = match[1].upper(), match[2].strip()
        try:
            if op not in allowed:
                raise InputError(f"unsupported instruction {op}")
            if not result and op != "FROM":
                raise InputError("the first instruction must be FROM; global ARG is unsupported")
            if op == "FROM":
                if result:
                    raise InputError("multi-stage Dockerfiles are unsupported")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", arg):
                    raise InputError("FROM requires one literal image, without flags, alias or expansion")
                validate_image(arg)
            sources = ()
            destination = None
            if op == "COPY":
                if arg.startswith("--") or "\\" in arg or "$" in arg:
                    raise InputError("COPY flags, escapes and variable expansions are unsupported")
                items = json_words(arg) if arg.startswith("[") else words(arg)
                if len(items) < 2:
                    raise InputError("COPY requires local source(s) and a destination")
                if any(x.startswith("--") for x in items):
                    raise InputError("COPY flags are unsupported")
                sources = tuple(clean_path(x, pattern=True) for x in items[:-1])
                clean_path(items[-1], destination=True)
                destination = items[-1]
                if len(sources) > 1 and not items[-1].endswith("/"):
                    raise InputError("multiple COPY sources require a destination ending in /")
            elif op == "WORKDIR":
                if len(words(arg)) != 1 or not arg.startswith("/") or any(c in arg for c in "\"'"):
                    raise InputError("WORKDIR requires one unquoted absolute literal path")
                clean_path(arg, destination=True)
                if any(c.isspace() for c in arg):
                    raise InputError("WORKDIR whitespace is unsupported")
            elif op in {"ENV", "ARG"}:
                if "$" in arg or "\\" in arg:
                    raise InputError(f"{op} requires literal values without expansions or escapes")
                items = words(arg)
                if not items or (op == "ARG" and len(items) != 1):
                    raise InputError(f"{op} requires literal NAME=value assignments")
                if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", item) for item in items):
                    raise InputError(f"{op} requires literal NAME=value assignments")
            elif op == "RUN":
                if arg.startswith("--") or "<<" in arg:
                    raise InputError("RUN flags/mounts and heredocs are unsupported")
                if arg.startswith("["):
                    json_words(arg)
            result.append(Instruction(f"I{len(result) + 1:03d}", line, op, arg, sources, destination))
        except InputError as exc:
            raise InputError(f"{path}:{line}: {exc}") from exc
    if not result:
        raise InputError(f"{path}: empty Dockerfile")
    return tuple(result)


@dataclass(frozen=True)
class Inventory:
    files: frozenset[str]
    directories: frozenset[str]


def inventory_from_files(files) -> Inventory:
    files = frozenset(files)
    directories = {"."}
    for file in files:
        parts = file.split("/")
        directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
    if files & directories:
        raise InputError("file/directory path conflict in scenario inventory")
    return Inventory(files, frozenset(directories))


def scan_context(root: Path, dockerfiles: tuple[Path, ...]) -> Inventory:
    if root.is_symlink() or not root.is_dir():
        raise InputError(f"{root}: context must be a directory, not a symlink")
    for path in [root / ".dockerignore", *(p.with_name(p.name + ".dockerignore") for p in dockerfiles)]:
        if path.exists() or path.is_symlink():
            raise InputError(f"{path}: ignore rules are unsupported; supply a prefiltered inventory directory")
    files, directories = set(), {"."}
    # Path.walk never follows symlinks. Every entry is checked with lstat.
    def scan_error(error):
        raise error

    for parent, dirnames, filenames in root.walk(on_error=scan_error, follow_symlinks=False):
        for name in sorted(dirnames + filenames):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            clean_path(relative)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise InputError(f"{relative}: context symlinks are unsupported")
            if stat.S_ISDIR(mode):
                directories.add(relative)
            elif stat.S_ISREG(mode):
                files.add(relative)
            else:
                raise InputError(f"{relative}: only regular context files and directories are supported")
    return Inventory(frozenset(files), frozenset(directories))


def matches(pattern: str, path: str) -> bool:
    left, right = pattern.split("/"), path.split("/")
    return len(left) == len(right) and all(fnmatchcase(r, p) for p, r in zip(left, right))


def copy_files(instruction: Instruction, inventory: Inventory) -> dict[str, tuple[str, ...]]:
    result = {}
    for source in instruction.sources:
        selected = set()
        resources = ({"."} if source == "." else
                     {p for p in inventory.files | inventory.directories if p != "." and matches(source, p)})
        if len(resources) > 1 and not instruction.destination.endswith("/"):
            raise InputError(f"{instruction.id}: COPY wildcard selects multiple resources; destination must end in /")
        for path in resources:
            if path in inventory.files:
                selected.add(path)
            if path in inventory.directories:
                selected.update(f for f in inventory.files if path == "." or f.startswith(path + "/"))
        if not selected:
            raise InputError(f"{instruction.id}: COPY source {source!r} matches no regular files; empty/missing sources are unevaluated")
        result[source] = tuple(sorted(selected))
    return result


def validate_revision_id(value, label: str):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise InputError(f"{label}: revisionId must be 1-128 ASCII letters/digits/_.-")


def parse_scenarios(data, inventory: Inventory, *, mode="independent-warm") -> list[dict]:
    fields = {"schemaVersion", "scenarios"}
    if isinstance(data, dict) and "initialRevisionIds" in data:
        fields.add("initialRevisionIds")
    exact_keys(data, fields, "changes")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        raise InputError("changes: schemaVersion must be integer 1")
    if "initialRevisionIds" in data:
        if mode not in HISTORY_MODES:
            raise InputError("initialRevisionIds requires cumulative-history or cumulative-cold mode")
        declarations = data["initialRevisionIds"]
        if not isinstance(declarations, dict):
            raise InputError("initialRevisionIds: expected a path to revision ID object")
        for path, revision in declarations.items():
            if clean_path(path) != path or path == ".":
                raise InputError("initialRevisionIds: paths must be canonical relative file paths")
            if path not in inventory.files:
                raise InputError(f"initialRevisionIds: path must be an existing regular file: {path!r}")
            validate_revision_id(revision, "initialRevisionIds")
    if not isinstance(data["scenarios"], list) or not data["scenarios"]:
        raise InputError("changes: scenarios must be a nonempty array")
    seen_ids = set()
    for scenario in data["scenarios"]:
        exact_keys(scenario, {"id", "changes"}, "scenario")
        sid = scenario["id"]
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", sid) or sid in seen_ids:
            raise InputError("scenario IDs must be unique, 1-80 ASCII letters/digits/_.-")
        seen_ids.add(sid)
        if not isinstance(scenario["changes"], list):
            raise InputError(f"{sid}: changes must be an array")
        seen_paths = set()
        for change in scenario["changes"]:
            fields = {"path", "kind"}
            if isinstance(change, dict) and "revisionId" in change:
                fields.add("revisionId")
            exact_keys(change, fields, f"{sid} change")
            path = clean_path(change["path"])
            if path != change["path"] or path == ".":
                raise InputError(f"{sid}: event paths must be canonical relative file paths")
            if not isinstance(change["kind"], str) or change["kind"] not in KINDS:
                raise InputError(f"{sid}: event kind must be one of {sorted(KINDS)}")
            if "revisionId" in change:
                if mode not in HISTORY_MODES:
                    raise InputError(f"{sid}: revisionId requires cumulative-history or cumulative-cold mode")
                if change["kind"] not in {"content", "add"}:
                    raise InputError(f"{sid}: revisionId is supported only on content/add events")
                validate_revision_id(change["revisionId"], sid)
            if path in seen_paths:
                raise InputError(f"{sid}: duplicate event path {path!r}")
            seen_paths.add(path)
            if change["kind"] == "add":
                if path in inventory.files or path in inventory.directories:
                    raise InputError(f"{sid}: add path already exists: {path}")
            elif path not in inventory.files:
                raise InputError(f"{sid}: unknown change path {path!r}; new paths require kind=add")
        updated = scenario_inventory(inventory, scenario)
        if mode in HISTORY_MODES:
            inventory = updated
    return data["scenarios"]


def scenario_inventory(inventory: Inventory, scenario: dict) -> Inventory:
    files = set(inventory.files)
    for change in scenario["changes"]:
        if change["kind"] == "delete":
            files.remove(change["path"])
        elif change["kind"] == "add":
            files.add(change["path"])
    generated = inventory_from_files(files)
    # Directories persist even when emptied; directory events are unsupported.
    if generated.files & inventory.directories:
        raise InputError(f"{scenario['id']}: added file conflicts with existing directory")
    return Inventory(generated.files, generated.directories | inventory.directories)


def parse_weights(data, instructions: dict[str, tuple[Instruction, ...]]) -> dict:
    result = {side: {i.id: 1 if i.op == "RUN" else 0 for i in items}
              for side, items in instructions.items()}
    exact_keys(data, {"schemaVersion", "before", "after"}, "weights")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        raise InputError("weights: schemaVersion must be integer 1")
    for side in instructions:
        if not isinstance(data[side], dict):
            raise InputError(f"weights.{side}: expected an instruction ID to number object")
        for key, value in data[side].items():
            if key not in result[side]:
                raise InputError(f"weights.{side}: unknown instruction ID {key!r}")
            if type(value) not in (int, float) or not 0 <= value <= 1e12 or not math.isfinite(value):
                raise InputError(f"weights.{side}.{key}: expected finite number in [0, 1000000000000]")
            result[side][key] = value
    return result


def instruction_keys(instructions, inventory: Inventory, revisions: dict[str, int | str]) -> list[str]:
    """Synthetic linear state identifiers; never Docker/BuildKit cache keys.

    Integer revisions identify initial/anonymous states; caller-supplied string
    IDs are a separate namespace scoped to each path, not file hashes. A COPY
    key includes its selected paths/revisions and every key includes its parent.
    """
    parent = "linear-copy-history-v1"
    keys = []
    for instruction in instructions:
        selection = ([(source, [(path, revisions[path]) for path in paths])
                      for source, paths in copy_files(instruction, inventory).items()]
                     if instruction.op == "COPY" else [])
        payload = [parent, instruction.op, instruction.argument, selection]
        parent = hashlib.sha256(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        keys.append(parent)
    return keys


def explain_steps(instructions, invalid_ids, triggers, first, *, synthetic: bool, cold=False, cold_start=False) -> list[dict]:
    """Explain every modeled decision, including unaffected and retained states."""
    steps = []
    invalid = set(invalid_ids)
    root = invalid_ids[0] if invalid_ids else None
    previous = None
    for instruction in instructions:
        matched = [t for t in triggers if t["copyId"] == instruction.id]
        affected = instruction.id in invalid
        cause = previous if affected and previous in invalid else None
        if cold or cold_start:
            decision = "synthetic-miss"
            why = COLD_START_MISS_WHY if cold_start else COLD_MISS_WHY
            cause = None
        elif affected:
            decision = "synthetic-miss" if synthetic else "invalidated"
            if matched:
                why = "Declared changes match this COPY's inputs."
            else:
                why = f"Parent {cause} is invalidated downstream of COPY {root} in the linear model."
            if synthetic:
                why += " The resulting synthetic state is absent from the retained cache."
        elif synthetic:
            decision = "synthetic-hit"
            why = "Reuses a retained synthetic instruction state; external factors remain unknown."
        else:
            decision = "unaffected"
            why = (f"Precedes first affected COPY {first.id}; no modeled invalidation."
                   if first else "No declared content/add/delete change is selected by any COPY; external factors remain unknown.")
        steps.append({"instructionId": instruction.id, "line": instruction.line,
                      "op": instruction.op, "argument": instruction.argument,
                      "decision": decision, "why": why, "matchedChanges": matched,
                      "causedByInstructionId": cause})
        previous = instruction.id
    return steps


def candidate_comparison(before: str, after: str) -> dict:
    # Keep source lines (including comments/continuations), and mark unterminated
    # lines so the unified diff remains readable instead of gluing +/- records.
    diff = unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                        fromfile="before/Dockerfile", tofile="after/Dockerfile")
    return {"source": "caller-supplied", "format": "unified-diff",
            "diff": "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                            for line in diff),
            "requiresSemanticEquivalenceConfirmation": True,
            "notice": "User must confirm the candidate reordering is semantically equivalent before adopting it; "
                      "RUN behavior, install scripts and COPY overwrites are not analyzed."}


def replay(instructions, initial: Inventory, scenario: dict, weights, *,
           cache: dict[str, dict] | None = None, revisions: dict[str, int | str] | None = None,
           previous_keys: list[str] | None = None, cold=False, cold_start=False) -> dict:
    updated = scenario_inventory(initial, scenario)
    changed = {c["path"] for c in scenario["changes"] if c["kind"] != "mtime"}
    triggers, copied_paths = [], set()
    for i in instructions:
        if i.op != "COPY":
            continue
        before, after = copy_files(i, initial), copy_files(i, updated)
        copied_paths.update(p for paths in before.values() for p in paths)
        copied_paths.update(p for paths in after.values() for p in paths)
        for source in i.sources:
            for path in sorted(changed & (set(before[source]) | set(after[source]))):
                change = next(c for c in scenario["changes"] if c["path"] == path)
                triggers.append({**change, "copyId": i.id, "source": source})
    first = next((i for i in instructions if any(t["copyId"] == i.id for t in triggers)), None)
    invalid = instructions[instructions.index(first):] if first else ()
    cache_report = None
    state_origins = None
    state_reuse = None
    if cold:
        # COPY selections above must be valid in both inventories even when
        # empty cache availability, rather than changed inputs, causes misses.
        # No state is shared across scenarios or between the two Dockerfiles.
        invalid = instructions
        cache_report = {"hitInstructionIds": [], "missInstructionIds": [i.id for i in instructions],
                        "entryCountBefore": 0, "entryCountAfter": len(instructions)}
    elif cache is not None:
        keys = instruction_keys(instructions, updated, revisions)
        invalid = tuple(i for i, key in zip(instructions, keys) if key not in cache)
        cache_report = {"hitInstructionIds": [i.id for i, key in zip(instructions, keys) if key in cache],
                        "missInstructionIds": [i.id for i in invalid], "entryCountBefore": len(cache)}
        # Capture origins before this build adds misses. Hits never replace the
        # first producer; each Dockerfile owns its own state-to-origin mapping.
        state_origins = [cache.get(key) for key in keys]
        state_reuse = [None if key not in cache else "previous-build"
                       if previous_keys is not None and key == previous_keys[index] else "retained-history"
                       for index, key in enumerate(keys)]
        for key in keys:
            cache.setdefault(key, {"kind": "scenario", "scenarioId": scenario["id"]})
        cache_report["entryCountAfter"] = len(cache)
        # Advance this side's last build even on hits, keeping first origins intact.
        if previous_keys is not None:
            previous_keys[:] = keys
    invalid_ids = [i.id for i in invalid]
    reasons = []
    for index, instruction in enumerate(invalid):
        if cold or cold_start:
            reasons.append({"instructionId": instruction.id,
                            "because": ("empty synthetic cache (cumulative-cold)" if cold_start else
                                        "empty synthetic cache (independent-cold)"), "causedBy": []})
            continue
        reasons.append({"instructionId": instruction.id,
                        "because": (("synthetic cache miss for changed COPY input" if cache is not None else "changed COPY input")
                                    if index == 0 else "downstream of invalidated instruction"),
                        "causedBy": ([t for t in triggers if t["copyId"] == instruction.id] if index == 0 else [invalid[index - 1].id])})
    result = {"firstAffectedCopy": first.id if first else None,
            "firstInvalidatedCopy": next((i.id for i in invalid if i.op == "COPY"), None),
            "invalidatedInstructionIds": invalid_ids,
            "invalidatedRunIds": [i.id for i in invalid if i.op == "RUN"],
            "invalidatedModelUnits": sum(weights[i.id] for i in invalid),
            "triggers": triggers, "reasonChain": reasons,
            "steps": explain_steps(instructions, invalid_ids, triggers, first, synthetic=cache is not None, cold=cold, cold_start=cold_start),
            "ignoredMtimePaths": sorted(c["path"] for c in scenario["changes"] if c["kind"] == "mtime"),
            "uncopiedChangePaths": sorted(changed - copied_paths)}
    if cache_report is not None:
        result["cache"] = cache_report
    if cache is not None and not cold:
        result["syntheticReuseCounts"] = {"previousBuild": state_reuse.count("previous-build"),
                                          "retainedHistory": state_reuse.count("retained-history"),
                                          "misses": state_reuse.count(None)}
        # Preserve instruction order and the existing cost arithmetic, so the
        # miss category equals invalidatedModelUnits even for fractional weights.
        result["syntheticReuseModelUnits"] = {
            name: sum(weights[i.id] for i, reuse in zip(instructions, state_reuse) if reuse == category)
            for name, category in (("previousBuild", "previous-build"),
                                   ("retainedHistory", "retained-history"), ("misses", None))}
        for step, origin, reuse in zip(result["steps"], state_origins, state_reuse):
            step["syntheticStateOrigin"] = origin
            step["syntheticStateReuse"] = reuse
    return result


def compare(before: Path, after: Path, changes: Path, context: Path, weights: Path | None = None, *,
            mode: str = "independent-warm", scenario: str | None = None,
            select_scenarios: list[str] | tuple[str, ...] | None = None) -> dict:
    if mode not in MODES:
        raise InputError(f"mode must be one of {MODES}")
    if scenario is not None and select_scenarios is not None:
        raise InputError("scenario and select_scenarios are mutually exclusive")
    if scenario is not None and (not isinstance(scenario, str) or
                                 not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", scenario)):
        raise InputError("scenario filter must be 1-80 ASCII letters/digits/_.-")
    selected_ids = None
    if select_scenarios is not None:
        if not isinstance(select_scenarios, (list, tuple)) or not select_scenarios:
            raise InputError("select_scenarios must be a nonempty list or tuple of unique scenario IDs")
        selected_ids = set()
        for sid in select_scenarios:
            if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", sid):
                raise InputError("select_scenarios IDs must be 1-80 ASCII letters/digits/_.-")
            if sid in selected_ids:
                raise InputError(f"select_scenarios has duplicate scenario ID: {sid!r}")
            selected_ids.add(sid)
    texts = {side: read_text(path) for side, path in (("before", before), ("after", after))}
    instructions = {side: _parse_dockerfile(path, texts[side])
                    for side, path in (("before", before), ("after", after))}
    inventory = scan_context(context, (before, after))
    changes_data = load_json(changes)
    scenarios = parse_scenarios(changes_data, inventory, mode=mode)
    fixed_inputs = {".dockerignore"}
    for dockerfile in (before, after):
        for path in (dockerfile, dockerfile.with_name(dockerfile.name + ".dockerignore")):
            try:
                fixed_inputs.add(path.resolve().relative_to(context.resolve()).as_posix())
            except ValueError:
                pass
    for change_set in scenarios:
        for change in change_set["changes"]:
            if change["kind"] != "mtime" and change["path"] in fixed_inputs:
                raise InputError(f"{change_set['id']}: Dockerfile/ignore-file changes are unsupported: {change['path']}")
    costs = parse_weights(load_json(weights) if weights else
                          {"schemaVersion": 1, "before": {}, "after": {}}, instructions)
    results = []
    current = inventory
    revisions: dict[str, int | str] = {path: 0 for path in inventory.files}
    # Initial declarations share the event-ID namespace for the same path.
    # Apply declarations before computing keys; declarations never create cold cache entries.
    revisions.update(changes_data.get("initialRevisionIds", {}))
    caches = {}
    previous_keys = {}
    if mode in HISTORY_MODES:
        for side in instructions:
            try:
                # Validate every initial COPY even when the cache starts empty.
                initial_keys = instruction_keys(instructions[side], inventory, revisions)
                previous_keys[side] = initial_keys if mode == "cumulative-history" else []
                caches[side] = ({key: {"kind": "initial-warm"} for key in initial_keys}
                                if mode == "cumulative-history" else {})
            except InputError as exc:
                raise InputError(f"initial inventory / {side}: {exc}") from exc
    for step, change_set in enumerate(scenarios, 1):
        updated = scenario_inventory(current, change_set)
        if mode in HISTORY_MODES:
            for change in change_set["changes"]:
                if change["kind"] == "delete":
                    del revisions[change["path"]]
                elif change["kind"] in {"content", "add"}:
                    revision = change.get("revisionId", step)
                    if change["kind"] == "content" and revision == revisions[change["path"]]:
                        raise InputError(f"{change_set['id']}: content event has unchanged revisionId for {change['path']}; "
                                         "omit the event for unchanged content")
                    revisions[change["path"]] = revision
        item = {"id": change_set["id"], "changes": change_set["changes"],
                "inventoryBeforeFileCount": len(current.files), "inventoryAfterFileCount": len(updated.files)}
        if mode in HISTORY_MODES:
            # Name the shared build order before presentation filtering, not the
            # first producer of any instruction state in either separate cache.
            item["previousBuild"] = (({"kind": "initial-warm"} if mode == "cumulative-history" else None)
                                     if step == 1 else {"kind": "scenario", "scenarioId": scenarios[step - 2]["id"]})
        for side in instructions:
            try:
                item[side] = replay(instructions[side], current, change_set, costs[side],
                                    cache=caches.get(side), revisions=revisions,
                                    previous_keys=previous_keys.get(side), cold=mode == "independent-cold",
                                    cold_start=mode == "cumulative-cold" and step == 1)
            except InputError as exc:
                raise InputError(f"{change_set['id']} / {side}: {exc}") from exc
        item["savedModelUnits"] = item["before"]["invalidatedModelUnits"] - item["after"]["invalidatedModelUnits"]
        results.append(item)
        if mode in HISTORY_MODES:
            current = updated
    # Selection is presentation only: preceding states and later failures must
    # be evaluated exactly as in an unfiltered report before narrowing results.
    selection = {}
    if scenario is not None:
        results = [item for item in results if item["id"] == scenario]
        if not results:
            raise InputError(f"unknown scenario ID: {scenario!r}")
        selection = {"selection": {"scenarioId": scenario, "evaluatedScenarioCount": len(scenarios),
                                   "displayedScenarioCount": len(results)}}
    elif selected_ids is not None:
        unknown = selected_ids - {item["id"] for item in results}
        if unknown:
            raise InputError(f"unknown scenario ID(s): {', '.join(sorted(unknown))}")
        results = [item for item in results if item["id"] in selected_ids]
        selection = {"selectedScenarios": {"scenarioIds": [item["id"] for item in results],
                                            "evaluatedScenarioCount": len(scenarios),
                                            "displayedScenarioCount": len(results)}}
    total_before = sum(r["before"]["invalidatedModelUnits"] for r in results)
    total_after = sum(r["after"]["invalidatedModelUnits"] for r in results)
    totals = {"beforeModelUnits": total_before, "afterModelUnits": total_after,
              "savedModelUnits": total_before - total_after}
    if mode in HISTORY_MODES:
        # Aggregate only displayed scenarios, retaining separate Dockerfiles.
        # Sum per-scenario categories in order, as for the existing cost totals.
        for field in ("syntheticReuseCounts", "syntheticReuseModelUnits"):
            totals[field] = {
                side: {category: sum(item[side][field][category] for item in results)
                       for category in ("previousBuild", "retainedHistory", "misses")}
                for side in ("before", "after")}
        totals["syntheticMissesByInstruction"] = {}
        totals["syntheticHistoryReusesByInstruction"] = {}
        totals["syntheticHistoryReusesByOrigin"] = {}
        for side in ("before", "after"):
            misses = {i.id: {"instructionId": i.id, "missCount": 0, "missedModelUnits": 0,
                             "missedScenarioIds": []}
                      for i in instructions[side]}
            for item in results:
                for instruction_id in item[side]["cache"]["missInstructionIds"]:
                    misses[instruction_id]["missCount"] += 1
                    misses[instruction_id]["missedScenarioIds"].append(item["id"])
                    # Add once per displayed miss in scenario order. Regrouping
                    # by instruction can round differently from scenario totals.
                    misses[instruction_id]["missedModelUnits"] += costs[side][instruction_id]
            totals["syntheticMissesByInstruction"][side] = list(misses.values())
            reuses = {i.id: {"instructionId": i.id, "reuseCount": 0, "reusedModelUnits": 0,
                             "reusedScenarioIds": []}
                      for i in instructions[side]}
            origin_reuses = {}
            for item in results:
                for step in item[side]["steps"]:
                    if step["decision"] == "synthetic-hit" and step["syntheticStateReuse"] == "retained-history":
                        record = reuses[step["instructionId"]]
                        record["reuseCount"] += 1
                        record["reusedScenarioIds"].append(item["id"])
                        # Match displayed decisions, including zero-weight reuse.
                        record["reusedModelUnits"] += costs[side][step["instructionId"]]
                        origin = step["syntheticStateOrigin"]
                        # None names the warm origin; scenario IDs are strings.
                        # Use first-producer evidence without mutating the cache.
                        producer = origin.get("scenarioId")
                        origin_record = origin_reuses.setdefault(producer,
                            {"origin": origin, "reuseCount": 0, "reusedModelUnits": 0})
                        origin_record["reuseCount"] += 1
                        origin_record["reusedModelUnits"] += costs[side][step["instructionId"]]
            totals["syntheticHistoryReusesByInstruction"][side] = list(reuses.values())
            # Producer order follows the full input, not first displayed reuse.
            totals["syntheticHistoryReusesByOrigin"][side] = [
                origin_reuses[producer] for producer in [None] + [s["id"] for s in scenarios]
                if producer in origin_reuses]
    mode_assumptions = (["Each scenario starts from the same supplied inventory and a warm cache for each Dockerfile separately.",
                         "All instructions after the first affected COPY are conservatively invalidated in a linear model."]
                        if mode == "independent-warm" else [
                            "Scenarios advance in array order; each successful modeled build updates inventory and its Dockerfile's separate cache.",
                            "Each cache starts warm for the initial inventory, retains all synthetic instruction keys without eviction, and is discarded after this report.",
                            "Content/add events without revisionId create fresh synthetic revisions; mtime is ignored. initialRevisionIds label initial content; unlisted initial files remain anonymous.",
                            "A revisionId asserts immutable content identity for one path within this report; reuse can restore retained synthetic states. Contents/hashes are never read or verified.",
                        ])
    if mode == "independent-cold":
        mode_assumptions = [
            "Each scenario starts from the same supplied inventory and an empty synthetic cache for each Dockerfile separately.",
            "Every instruction, including FROM, is assumed to miss; entries from each modeled build are discarded after that scenario.",
            "Cold misses are synthetic cache assumptions, not Docker pulls, network work, real BuildKit cache keys or timing.",
        ]
    if mode == "cumulative-cold":
        mode_assumptions[1] = ("Each cache starts empty, retains only synthetic instruction states produced by scenarios "
                               "without eviction, and is discarded after this report.")
        mode_assumptions.append("Cold misses are synthetic cache assumptions, not Docker pulls, network work, "
                                "real BuildKit cache keys or timing.")
    initial_declarations = ({"initialRevisionIds": changes_data["initialRevisionIds"]}
                            if "initialRevisionIds" in changes_data else {})
    return {"schemaVersion": 1, "mode": mode, **selection, **initial_declarations,
            "model": "linear-copy-history-v1" if mode in HISTORY_MODES else "linear-copy-v1", "units": "model_units",
            "semanticEquivalenceProven": False, "measuredBuildSeconds": None,
            "actualCacheHitProven": False,
            "candidateComparison": candidate_comparison(texts["before"], texts["after"]),
            "assumptions": mode_assumptions + ASSUMPTIONS, "unmodeled": UNMODELED,
            "inventoryFileCount": len(inventory.files),
            "dockerfiles": {side: {"path": str(path), "image": instructions[side][0].argument,
                                    "mutableFrom": instructions[side][0].argument != "scratch" and
                                    not bool(re.fullmatch(r".+@sha256:[0-9a-f]{64}", instructions[side][0].argument)),
                                    "instructions": [i.public(costs[side][i.id]) for i in instructions[side]]}
                            for side, path in (("before", before), ("after", after))},
            "scenarios": results,
            "totals": totals}
