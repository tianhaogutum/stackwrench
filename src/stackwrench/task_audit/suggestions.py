"""Conservative declaration candidates, never shell evaluation or Taskfile writes."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import stat
from pathlib import Path

import yaml

from .audit import IDENTIFIER, audit_document
from .formats import AuditError, load_taskfile
from .sources import validate_pattern

EXPANSION = re.compile(r"\$(?:\{(" + IDENTIFIER + r")\}|(" + IDENTIFIER + r"))")
ASSIGNMENT = re.compile(r"^(" + IDENTIFIER + r")=")
PYTHON_INTERPRETER = re.compile(r"python(?:3(?:\.[0-9]+)*)?")
SCOPE_COMMANDS = {"cd", "eval", "exec", "export", "unset", "set", "read", "for", "while", "until",
                  "if", "then", "else", "fi", "do", "done", "case", "esac", "function", ".", "source",
                  "sh", "bash", "dash", "zsh", "fish", "env", "sudo", "xargs", "printenv",
                  "python", "python3", "perl", "ruby", "node", "awk", "sed"}
BOUNDARIES = [
    "Candidates are review heuristics: shell locals, custom commands/functions and embedded languages can cause false positives or missed inputs.",
    "Only simple shell words, single/double quotes, escapes, comments and semicolon/newline separators are scanned; complex commands remain uncertain.",
    "Only cat/wc/sort/head/tail file operands, cmp/comm paired file operands and uniq input operands without options (except --), literal < input redirections, and literal python/python3/versioned python3 or node script-file operands are source candidates; output operands/redirects and arbitrary program arguments are excluded.",
    "Only the literal command name wc is recognized. It allows a single initial -- and ignores stdin -; other options, including --files0-from and attached values, withhold the command entries. After --, dash-leading filenames are literal operands; repeated -- is unsupported. File lists are never read.",
    "Only the literal command name sort is recognized. It allows a single initial -- and ignores stdin -; all other options, including -o/--output, key/field/separator options and attached values, withhold the command entries. After --, dash-leading filenames are literal operands; repeated/noninitial -- is unsupported. Option values and output targets are never inferred.",
    "Only the literal command name uniq is recognized, with zero to two operands after an optional single initial --: first input, second output. Stdin - is ignored; after -- dash-leading names are literal. Other options, repeated/noninitial --, excess operands and dynamic input/output withhold the command entries. Positional output is never normalized, inspected or inferred as a source.",
    "Only the literal command name head is recognized. It allows a single initial -- and ignores stdin -; all other options, including -n/-c, attached values and obsolete numeric forms, withhold the command entries. After --, dash-leading filenames are literal operands; repeated/noninitial -- is unsupported. Option values are never inferred.",
    "Only the literal command name tail is recognized. It allows a single initial -- and ignores stdin -; all other options, including -n/-c/-f/--follow, attached values and obsolete signed numeric forms, withhold the command entries. Plus-leading operands before -- also withhold entries to avoid obsolete count ambiguity. After --, dash/plus-leading names are literal operands; repeated/noninitial -- is unsupported. Option values and follow-mode dependencies are never inferred.",
    "Only the literal command name cmp is recognized, with exactly two literal operands after an optional single initial --. Stdin - in either position is ignored; dash-leading names after -- are literal. Other options, including -n/-i/-s/-l and long/attached forms, repeated/noninitial --, other arities and dynamic operands withhold the command entries. No option values, byte comparison or command success are inferred.",
    "Only the literal command name comm is recognized, with exactly two literal operands after an optional single initial --. Stdin - in either position is ignored; dash-leading names after -- are literal. Other options, including -1/-2/-3, --check-order/--nocheck-order/--output-delimiter and attached forms, repeated/noninitial --, other arities and dynamic operands withhold the command entries. No option values, sortedness, locale, content, order or command success are inferred.",
    "Python and Node.js script calls allow only an optional -- before the script; other interpreter options (including Python -c/-m and Node.js -e/-p/--eval/--print) and stdin stay uncertain. Script imports, file reads and other transitive inputs are not modeled, even after declaring the script.",
    "A single leading ./ is removed from literal cat/wc/sort/head/tail/cmp/comm file operands, uniq input operands, < input redirections and script-file operands before safety checks and declaration coverage; repeated prefixes, internal dot/empty components and traversal stay unsupported. Declared source/output patterns keep their strict path rules.",
    "Candidate sources must already be regular files beneath root without symlink components; paths are metadata, and candidate contents are not read.",
    "Merge fragment entries into existing task sources/env; do not replace those mappings. REVIEW_REQUIRED is a placeholder to replace with a reviewed value, never an inferred secret.",
    "Declaring env removes missing-env-declaration only; it does not prove native fingerprint coverage or remove check's potential-fingerprint-gap heuristic.",
]


@dataclass
class Word:
    text: str = ""
    refs: set[str] = field(default_factory=set)
    operator: bool = False


def scan(command):
    """Tokenize a deliberately small shell subset, retaining active expansions.

    Unsupported syntax rejects the whole command. No partially parsed expansion
    is promoted into a complete dependency assertion.
    """
    if "{{" in command or "}}" in command:
        return [], "template-expression"
    words, word, quote, started = [], Word(), None, False
    index = 0

    def flush():
        nonlocal word, started
        if started:
            words.append(word)
        word, started = Word(), False

    while index < len(command):
        char = command[index]
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            started = True
        elif char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            started = True
        elif char == "\\" and quote != "'":
            if index + 1 == len(command):
                return [], "malformed-shell-word"
            following = command[index + 1]
            if quote == '"' and following not in '$`"\\\n':
                pass  # In double quotes, preserve a backslash before an ordinary character.
            else:
                index += 1
                if following == "\n":
                    index += 1
                    continue
            word.text += command[index]
            started = True
        elif char == "$" and quote != "'":
            match = EXPANSION.match(command, index)
            if not match:
                return [], "dynamic-or-indirect-expansion"
            word.text += match.group()
            word.refs.add(match.group(1) or match.group(2))
            index = match.end() - 1
            started = True
        elif char == "`" and quote != "'":
            return [], "dynamic-or-indirect-expansion"
        elif not quote and char == "#" and not started:
            newline = command.find("\n", index)
            index = newline if newline >= 0 else len(command)
            continue
        elif not quote and char in "\n;<>|&()":
            if char in "<>" and started and word.text.isdecimal():
                return [], "file-descriptor-redirection"
            flush()
            if char in "|&()" or (index + 1 < len(command) and command[index + 1] == char and char != "\n"):
                return [], "complex-shell-control-or-redirection"
            words.append(Word(";" if char == "\n" else char, operator=True))
        elif not quote and char.isspace():
            flush()
        else:
            if not quote and char in "*?[]{}~":
                return [], "glob-or-shell-expression"
            word.text += char
            started = True
        index += 1
    if quote:
        return [], "malformed-shell-word"
    flush()
    return words, None


def source_operands(words):
    """Return file reads, a blocking reason, and nonblocking uncertainty.

    A literal interpreter script is a direct input, but its transitive inputs remain
    unknown. Any blocking segment still withholds the whole command's entries.
    """
    segments, segment = [], []
    for word in words + [Word(";", operator=True)]:
        if word.operator and word.text == ";":
            if segment:
                segments.append(segment)
            segment = []
        else:
            segment.append(word)
    paths, uncertainty = [], []
    for segment in segments:
        args, index = [], 0
        while index < len(segment):
            word = segment[index]
            if word.operator:
                if index + 1 >= len(segment) or segment[index + 1].operator:
                    return [], "complex-shell-control-or-redirection", []
                target = segment[index + 1]
                if word.text == "<":
                    paths.append(target)
                index += 2
                continue
            args.append(word)
            index += 1
        while args and ASSIGNMENT.match(args[0].text):
            args.pop(0)
        if not args:
            continue
        executable = args[0].text.rsplit("/", 1)[-1]
        if args[0].refs:
            return [], "shell-scope-or-indirect-command", []
        if PYTHON_INTERPRETER.fullmatch(executable) or executable == "node":
            operands = args[1:]
            if operands and operands[0].text == "--":
                operands = operands[1:]
            if not operands or operands[0].text == "-" or operands[0].text.startswith("-"):
                return [], "interpreter-mode-not-modeled", []
            paths.append(operands[0])
            if "script-transitive-inputs-not-modeled" not in uncertainty:
                uncertainty.append("script-transitive-inputs-not-modeled")
        elif executable in SCOPE_COMMANDS:
            return [], "shell-scope-or-indirect-command", []
        if args[0].text == "cat":
            operands = args[1:]
            if operands and operands[0].text == "--":
                operands = operands[1:]
            elif any(arg.text.startswith("-") and arg.text != "-" for arg in operands):
                return [], "cat-options-not-modeled", []
            paths.extend(arg for arg in operands if arg.text != "-")
        if args[0].text in {"wc", "sort", "uniq", "head", "tail", "cmp", "comm"}:
            operands = args[1:]
            if operands and operands[0].text == "--":
                operands = operands[1:]
                # The delimiter ends options; another delimiter is outside this subset.
                if any(arg.text == "--" for arg in operands):
                    return [], f"{args[0].text}-options-not-modeled", []
            elif any((arg.text.startswith("-") and arg.text != "-") or
                     (args[0].text == "tail" and arg.text.startswith("+")) for arg in operands):
                return [], f"{args[0].text}-options-not-modeled", []
            if args[0].text in {"cmp", "comm"} and len(operands) != 2:
                # Paired reads only; extra cmp operands can also be skip counts.
                return [], f"{args[0].text}-operand-count-not-modeled", []
            if args[0].text == "uniq":
                if len(operands) > 2:
                    return [], "uniq-operand-count-not-modeled", []
                if len(operands) == 2 and operands[1].refs:
                    return [], "dynamic-output-operand", []
                # Only the first operand is read. Do not normalize or inspect output.
                operands = operands[:1]
            paths.extend(arg for arg in operands if arg.text != "-")
    if any(path.refs for path in paths):
        return [], "dynamic-source-operand", []
    # Normalize recognized literal reads once, after option and dynamic checks.
    # Remaining dots/empty parts still fail the shared safety validator.
    return [Word(path.text.removeprefix("./")) for path in paths], None, uncertainty


def regular_candidate(root, path):
    # Use metadata only. Never open candidate files, FIFOs or symlink targets.
    try:
        parts = validate_pattern(path)
        if any(char in path for char in "*?\r\n"):
            return False
        current = root
        for index, part in enumerate(parts):
            current = current / part
            mode = current.lstat().st_mode
            if not (stat.S_ISREG(mode) if index == len(parts) - 1 else stat.S_ISDIR(mode)):
                return False
        return True
    except (AuditError, OSError, ValueError):
        return False


def suggest(taskfile: Path, root: Path, env=None, selected=None):
    data = load_taskfile(taskfile)
    audited = audit_document(data, root, env, selected)
    root = root.resolve()
    tasks = {}
    for name, audit_task in audited["tasks"].items():
        raw = data["tasks"][name]
        task = raw if isinstance(raw, dict) else {"cmds": [raw] if isinstance(raw, str) else raw}
        scans, locals_, uncertain = [], set(), []
        locals_.update(value["name"] for value in audit_task["inputs"].values()
                       if value["classification"] == "possible-shell-local")
        for index, command in enumerate(task.get("cmds", [])):
            path = ["tasks", name]
            if not isinstance(raw, str):
                path += (["cmds"] if isinstance(raw, dict) else []) + [index]
            location = {"path": path}
            if not isinstance(command, str):
                scans.append((location, [], [], "structured-command", []))
                continue
            words, reason = scan(command)
            locals_.update(match.group(1) for word in words if (match := ASSIGNMENT.match(word.text)))
            paths, path_reason, notes = source_operands(words) if not reason else ([], None, [])
            scans.append((location, words, paths, reason or path_reason, notes))
        # Unsupported declarations, includes, dir, etc. can change the meaning or
        # declaration coverage. Retain findings and withhold actionable snippets.
        unassessed = [entry for entry in audited["findings"] + audit_task["findings"]
                      if entry["severity"] == "unassessed" and entry["code"] not in
                      {"unsupported-shell-expansion", "possible-shell-local"}]
        context_unknown = any(entry["code"] in {"unsupported-field", "unsupported-variable-name", "templated-path"}
                              for entry in unassessed)
        sources, environment = {}, {}
        for location, words, paths, reason, notes in scans:
            if reason or context_unknown:
                uncertain.append({"location": location, "reason": reason or "unsupported-task-context"})
                continue
            uncertain.extend({"location": location, "reason": note} for note in notes)
            for variable in sorted(set().union(*(word.refs for word in words))):
                if variable in locals_:
                    uncertain.append({"location": location, "reason": "possible-shell-local", "reference": f"shell:{variable}"})
                elif variable not in task.get("env", {}) and variable not in data.get("env", {}):
                    locations = environment.setdefault(variable, [])
                    if location not in locations:
                        locations.append(location)
            for operand in paths:
                if operand.text in audit_task["source_hashes"]:
                    continue
                if not regular_candidate(root, operand.text):
                    uncertain.append({"location": location, "reason": "source-path-not-safe-regular-literal"})
                else:
                    locations = sources.setdefault(operand.text, [])
                    if location not in locations:
                        locations.append(location)
        source_list = [{"path": path, "locations": locations} for path, locations in sorted(sources.items())]
        env_list = [{"name": variable, "locations": locations} for variable, locations in sorted(environment.items())]
        fragment = {}
        if source_list:
            fragment["sources"] = sorted(sources)
        if env_list:
            fragment["env"] = {variable: "REVIEW_REQUIRED" for variable in sorted(environment)}
        advisories = ([{"code": "missing-source-declaration", "path": item["path"]} for item in source_list] +
                      [{"code": "missing-env-declaration", "name": item["name"]} for item in env_list])
        tasks[name] = {"sources": source_list, "env": env_list, "advisories": advisories,
                       "uncertain": uncertain, "unassessed_findings": unassessed,
                       "yaml_fragment": yaml.safe_dump({"tasks": {name: fragment}}, sort_keys=True) if fragment else None}
    return {"kind": "task-fingerprint-audit.suggestions", "schema_version": 1, "read_only": True,
            "values_redacted": True, "dependency_graph_complete": False, "native_execution_decision": "unknown",
            "false_positive_boundaries": BOUNDARIES, "tasks": tasks}


def has_suggestion_concerns(report):
    return any(task["advisories"] or task["uncertain"] or task["unassessed_findings"] for task in report["tasks"].values())


def render_suggestions(report, escape):
    lines = ["Read-only declaration suggestions; dependency graph complete: false; native Task execution: unknown"]
    lines.extend(f"Boundary: {boundary}" for boundary in report["false_positive_boundaries"])
    chunks = [escape(lines)]
    for name, task in report["tasks"].items():
        lines = [f"{name}:"]
        for field_name, key in (("sources", "path"), ("env", "name")):
            for item in task[field_name]:
                code = "missing-source-declaration" if field_name == "sources" else "missing-env-declaration"
                locations = ", ".join(str(location["path"]) for location in item["locations"])
                lines.append(f"  {code}: {item[key]}; command locations: {locations}")
        for item in task["uncertain"]:
            lines.append(f"  uncertain: {item['reason']}; {item['location']['path']}")
        for item in task["unassessed_findings"]:
            lines.append(f"  unassessed: {item['code']}: {item['message']}")
        if task["yaml_fragment"]:
            lines.append("  Merge this YAML fragment after review (replace REVIEW_REQUIRED):")
        chunks.append(escape(lines))
        if task["yaml_fragment"]:
            # PyYAML already escapes control characters in scalar values. Keep
            # its syntax intact so text-mode fragments are directly copyable.
            chunks.append(task["yaml_fragment"].rstrip())
    return "\n".join(chunks)
