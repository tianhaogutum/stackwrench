"""An intentionally small grammar, not a general-purpose just parser.

Only literal assignments, documentation comments, parameter-free recipes and
bare prior dependencies are recognized. Shell text is data throughout.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
import shlex

NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
ASSIGNMENT = re.compile(rf"({NAME})[ \t]*:=[ \t]*(.*)")
HEADER = re.compile(rf"({NAME})[ \t]*:[ \t]*(.*?)[ \t]*(?:#.*)?$")
REFERENCE = re.compile(rf"[ \t]*({NAME})[ \t]*\Z")
MAX_INPUT_BYTES = 1_048_576
MAX_RECIPES = 512
MAX_EXPANDED_CHARS = 1_048_576
MAX_TOTAL_EXPANDED_CHARS = 8_388_608


def _source_byte_limit(value: int | None) -> int:
    if value is None:
        return MAX_INPUT_BYTES
    if type(value) is not int or not 1 <= value <= MAX_INPUT_BYTES:
        raise ValueError(f"max_source_bytes must be an integer from 1 to {MAX_INPUT_BYTES}")
    return value


def _source_limit_message(value: int | None) -> str:
    return "input exceeds 1 MiB" if value is None else f"input exceeds --max-source-bytes {value}"


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    line: int
    message: str


@dataclass(frozen=True)
class Variable:
    name: str
    line: int
    value: str


@dataclass(frozen=True)
class Command:
    line: int
    source: str
    expanded: str

    @property
    def task_command(self) -> str:
        # Task has an embedded shell with different defaults. Invoke the just
        # default explicitly. POSIX quoting prevents the outer shell expanding
        # dollars, quotes, redirects or command substitutions in the payload.
        return "sh -cu " + shlex.quote(self.expanded)


@dataclass
class Recipe:
    name: str
    line: int
    dependencies: list[str]
    description: str | None
    commands: list[Command] = field(default_factory=list)
    indent: str | None = None


@dataclass
class Analysis:
    source: str
    variables: dict[str, Variable] = field(default_factory=dict)
    recipes: dict[str, Recipe] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    traces: dict[str, list[str]] = field(default_factory=dict)
    source_sha256: str | None = None
    requested_roots: list[str] | None = None
    included_recipes: list[str] | None = None
    max_source_bytes: int | None = None

    @property
    def supported(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    @property
    def scoped_recipes(self) -> list[Recipe]:
        if self.included_recipes is None:
            return list(self.recipes.values())
        return [self.recipes[name] for name in self.included_recipes]

    def report(self) -> dict:
        default = None
        if self.supported:
            first = next(iter(self.recipes))
            if self.requested_roots is None:
                default = {"sourceRecipe": first, "task": "default", "synthetic": first != "default"}
            elif "default" in self.included_recipes:
                default = {"sourceRecipe": "default", "task": "default", "synthetic": False}
        report = {
            "formatVersion": 1,
            "source": self.source,
            "sourceSha256": self.source_sha256,
            "renderedTaskfileSha256": hashlib.sha256(
                render_taskfile(self).encode("utf-8")).hexdigest() if self.supported else None,
            "supported": self.supported,
            "graph": {r.name: r.dependencies for r in self.scoped_recipes},
            "mappings": {
                "variables": [dict(asdict(v), strategy="inline-literal")
                              for v in self.variables.values()],
                "recipes": [
                    {"sourceRecipe": r.name, "task": r.name, "line": r.line,
                     "description": r.description,
                     "dependencies": r.dependencies,
                     "commands": [dict(asdict(c), taskCommand=c.task_command)
                                  for c in r.commands]}
                    for r in self.scoped_recipes
                ] if self.supported else [],
                "default": default,
            },
            "traces": self.traces if self.supported else {},
            "diagnostics": [asdict(d) for d in self.diagnostics],
        }
        if self.max_source_bytes is not None:
            report["maxSourceBytes"] = self.max_source_bytes
        if self.requested_roots is not None:
            report["selection"] = {
                "requestedRoots": self.requested_roots,
                "includedRecipes": self.included_recipes,
                "omittedRecipes": [name for name in self.recipes if name not in self.included_recipes]
                if self.included_recipes is not None else None,
                "sourceDefaultRecipe": next(iter(self.recipes), None),
                "validationScope": "full-source",
            }
        return report


class Unsupported(ValueError):
    pass


def _literal(expression: str) -> str:
    """Read a single just string token and optional trailing comment."""
    if not expression or expression[0] not in "\"'":
        raise Unsupported("expected one quoted literal; expressions, backticks and shell-expanded strings are unsupported")
    quote = expression[0]
    if expression.startswith(quote * 3):
        raise Unsupported("multiline/triple-quoted strings are unsupported")
    result: list[str] = []
    i = 1
    while i < len(expression):
        char = expression[i]
        if char == quote:
            rest = expression[i + 1:].lstrip(" \t")
            if rest and not rest.startswith("#"):
                raise Unsupported("only a single literal is supported; no concatenation or expressions")
            value = "".join(result)
            if "`" in value:
                raise Unsupported("backticks are outside the supported subset")
            if "{{" in value:
                raise Unsupported("literal '{{' would collide with Task templating")
            return value
        if char == "\\" and quote == '"':
            i += 1
            if i == len(expression):
                raise Unsupported("unterminated string escape")
            escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
            if expression[i] in escapes:
                result.append(escapes[expression[i]])
            elif expression[i:].startswith("u{"):
                end = expression.find("}", i + 2)
                digits = expression[i + 2:end] if end != -1 else ""
                if not re.fullmatch(r"[0-9A-Fa-f]{1,6}", digits):
                    raise Unsupported("expected a Unicode escape like \\u{1F916}")
                codepoint = int(digits, 16)
                if not (0 < codepoint <= 0x10FFFF) or 0xD800 <= codepoint <= 0xDFFF:
                    raise Unsupported("invalid Unicode scalar or NUL")
                result.append(chr(codepoint))
                i = end
            else:
                raise Unsupported("unsupported string escape")
        else:
            result.append(char)
        i += 1
    raise Unsupported("unterminated literal; multiline strings are unsupported")


def _interpolate(command: str, variables: dict[str, Variable]) -> str:
    result: list[str] = []
    size = 0
    i = 0
    while i < len(command):
        start = command.find("{{", i)
        if start == -1:
            result.append(command[i:])
            size += len(command) - i
            break
        result.append(command[i:start])
        size += start - i
        end = command.find("}}", start + 2)
        match = REFERENCE.fullmatch(command[start + 2:end]) if end != -1 else None
        if not match:
            raise Unsupported("interpolation must be {{name}}; expressions and escaped '{{{{' are unsupported")
        name = match[1]
        if name not in variables:
            raise Unsupported(f"undefined literal variable {name!r}")
        result.append(variables[name].value)
        size += len(variables[name].value)
        if size > MAX_EXPANDED_CHARS:
            raise Unsupported("expanded command exceeds 1 Mi characters")
        i = end + 2
    if size > MAX_EXPANDED_CHARS:
        raise Unsupported("expanded command exceeds 1 Mi characters")
    expanded = "".join(result)
    if "{{" in expanded:
        raise Unsupported("expanded '{{' would collide with Task templating")
    if "`" in expanded:
        raise Unsupported("backticks are outside the supported subset (including after interpolation)")
    return expanded


def _graph(analysis: Analysis) -> None:
    for recipe in analysis.recipes.values():
        for dependency in recipe.dependencies:
            if dependency not in analysis.recipes:
                analysis.diagnostics.append(Diagnostic(
                    "error", "missing-dependency", recipe.line,
                    f"recipe {recipe.name!r} depends on missing recipe {dependency!r}"))
    if not analysis.supported:
        return
    # Iterative DFS: source order, left-to-right dependencies, once per recipe.
    # Each trace is a standalone invocation, never a parallel dependency list.
    for root in analysis.recipes:
        visited: set[str] = set()
        active: list[str] = []
        active_set: set[str] = set()
        order: list[str] = []
        stack = [(root, False)]
        while stack:
            name, leaving = stack.pop()
            if leaving:
                active.pop()
                active_set.remove(name)
                visited.add(name)
                order.append(name)
                continue
            if name in visited:
                continue
            if name in active_set:
                cycle = active[active.index(name):] + [name]
                analysis.diagnostics.append(Diagnostic(
                    "error", "dependency-cycle", analysis.recipes[name].line,
                    "dependency cycle: " + " -> ".join(cycle)))
                return
            active.append(name)
            active_set.add(name)
            stack.append((name, True))
            stack.extend((dep, False) for dep in reversed(analysis.recipes[name].dependencies))
        analysis.traces[root] = order


def _selection_roots(recipes: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if recipes is None:
        return None
    if (not isinstance(recipes, (list, tuple)) or not recipes
            or any(not isinstance(name, str) for name in recipes)):
        raise ValueError("recipes must be a nonempty list or tuple of strings, or None")
    return list(dict.fromkeys(recipes))


def _select(analysis: Analysis) -> None:
    # This runs only after full parsing, interpolation and graph validation.
    if analysis.requested_roots is None or not analysis.supported:
        return
    for name in analysis.requested_roots:
        if name not in analysis.recipes:
            analysis.diagnostics.append(Diagnostic(
                "error", "recipe-selection", 1,
                f"selected recipe {name!r} must be a nonempty exact existing recipe name"))
    if not analysis.supported:
        return
    included = {name for root in analysis.requested_roots for name in analysis.traces[root]}
    analysis.included_recipes = [name for name in analysis.recipes if name in included]
    analysis.traces = {name: order for name, order in analysis.traces.items() if name in included}
    omitted = [name for name in analysis.recipes if name not in included]
    analysis.diagnostics.append(Diagnostic(
        "info", "recipe-selection", 1,
        "entire source and dependency graph validated; selected closure includes "
        f"{len(included)} recipes; omitted recipes: {', '.join(omitted) or '(none)'}; "
        "invoke included tasks by name; no synthetic default added"))


def analyze(text: str, source: str = "<string>", *,
            recipes: list[str] | tuple[str, ...] | None = None,
            max_source_bytes: int | None = None) -> Analysis:
    """Validate all inert text, then optionally select roots and their closure."""
    limit = _source_byte_limit(max_source_bytes)
    analysis = Analysis(source, requested_roots=_selection_roots(recipes),
                        max_source_bytes=max_source_bytes)

    def error(code: str, line: int, message: str) -> None:
        analysis.diagnostics.append(Diagnostic("error", code, line, message))

    source_bytes = text.encode("utf-8")
    if len(source_bytes) > limit:
        error("input-limit", 1, _source_limit_message(max_source_bytes))
        return analysis
    analysis.source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    text = text.replace("\r\n", "\n")
    for line, raw in enumerate(text.split("\n"), 1):
        if any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in raw):
            error("control-character", line, "unsupported control character (use LF or CRLF line endings)")
    if not analysis.supported:
        return analysis
    current: Recipe | None = None
    pending_docs: list[str] = []
    pending_commands: list[tuple[Recipe, int, str]] = []
    for line, raw in enumerate(text.split("\n"), 1):
        if not raw.strip(" \t"):
            pending_docs = []
            continue
        if raw[0] in " \t":
            pending_docs = []
            if current is None:
                error("orphan-body", line, "indented line without a supported recipe header")
                continue
            indentation = raw[:len(raw) - len(raw.lstrip(" \t"))]
            command = raw[len(indentation):]
            if " " in indentation and "\t" in indentation:
                error("indentation", line, "mixed space/tab indentation is unsupported")
            if current.indent is None:
                current.indent = indentation
            elif current.indent != indentation:
                error("indentation", line, "all command lines must use the same exact indentation")
            if command.startswith("#!"):
                error("script-recipe", line, "shebang/script recipes are unsupported")
            elif command.startswith(("@", "-", "+")):
                error("command-prefix", line, "quiet, error-ignoring and other command prefixes are unsupported")
            elif command.rstrip().endswith("\\"):
                error("continuation", line, "backslash line continuations are unsupported")
            else:
                pending_commands.append((current, line, command))
            continue
        current = None
        if raw.startswith("#"):
            pending_docs.append(raw[1:].lstrip(" \t"))
            continue
        assignment = ASSIGNMENT.fullmatch(raw)
        if assignment:
            name, expression = assignment.groups()
            try:
                value = _literal(expression)
                if name in analysis.variables:
                    error("duplicate-variable", line, f"duplicate variable {name!r}")
                else:
                    analysis.variables[name] = Variable(name, line, value)
            except Unsupported as exc:
                error("unsupported-variable", line, str(exc))
            pending_docs = []
            continue
        header = HEADER.fullmatch(raw)
        if header:
            name, tail = header.groups()
            dependencies = re.split(r"[ \t]+", tail) if tail else []
            if any(not re.fullmatch(NAME, dep) for dep in dependencies):
                error("unsupported-dependency", line, "dependencies must be bare names; no arguments, expressions or subsequent dependencies")
            elif name.startswith("_"):
                error("private-recipe", line, "private recipe names starting with '_' are unsupported")
            elif name in analysis.recipes:
                error("duplicate-recipe", line, f"duplicate recipe {name!r}")
            elif len(analysis.recipes) >= MAX_RECIPES:
                error("recipe-limit", line, "at most 512 recipes are supported")
            else:
                description = "\n".join(pending_docs) if pending_docs else None
                if description and "{{" in description:
                    error("description-template", line, "'{{' in descriptions collides with Task templating")
                current = Recipe(name, line, dependencies, description)
                analysis.recipes[name] = current
            pending_docs = []
            continue
        error("unsupported-syntax", line,
              "unsupported top-level syntax: only literal assignments and parameter-free recipe headers are accepted")
        pending_docs = []
    total_expanded = 0
    for recipe, line, command in pending_commands:
        try:
            expanded = _interpolate(command, analysis.variables)
            total_expanded += len(expanded)
            if total_expanded > MAX_TOTAL_EXPANDED_CHARS:
                error("expansion-limit", line, "total expanded commands exceed 8 Mi characters")
                break
            recipe.commands.append(Command(line, command, expanded))
        except Unsupported as exc:
            error("unsupported-interpolation", line, str(exc))
    if not analysis.recipes:
        error("no-recipes", 1, "no supported recipes found")
    elif "default" in analysis.recipes and next(iter(analysis.recipes)) != "default":
        error("default-conflict", analysis.recipes["default"].line,
              "a non-first recipe named 'default' conflicts with Task's default entry point")
    _graph(analysis)
    _select(analysis)
    if analysis.supported:
        analysis.diagnostics.extend([
            Diagnostic("warning", "echo-format", 1,
                       "Task echoes the sh wrapper differently; command stdout and order are preserved under the documented environment assumptions"),
            Diagnostic("warning", "runner-environment", 1,
                       "not evaluated: runner-injected environment, CLI overrides, platform-specific sh, external tools and Task rc configuration"),
            Diagnostic("info", "metadata-loss", 1,
                       "standalone comments and assignment formatting are not retained; literals are inlined and Task listing rules differ"),
        ])
    return analysis


def render_dot(analysis: Analysis) -> str:
    """Validated recipe identities and prerequisite → dependent edges only.

    Nodes follow source order; edges follow dependent source order, then
    dependency order, with repeated pairs emitted once. No synthetic default.
    JSON quoting is DOT-safe for the supported ASCII recipe-name grammar.
    """
    if not analysis.supported:
        raise ValueError("unsupported input: inspect analysis diagnostics; no partial graph")
    recipes = analysis.scoped_recipes
    lines = ["digraph recipes {"]
    lines.extend(f"  {json.dumps(recipe.name)};" for recipe in recipes)
    for recipe in recipes:
        for dependency in dict.fromkeys(recipe.dependencies):
            lines.append(f"  {json.dumps(dependency)} -> {json.dumps(recipe.name)};")
    return "\n".join([*lines, "}", ""])


def render_taskfile(analysis: Analysis) -> str:
    """Return JSON-form YAML 1.2, a Taskfile v3 with no dynamic templates."""
    if not analysis.supported:
        raise ValueError("unsupported input: inspect analysis diagnostics; no partial Taskfile")
    tasks: dict = {}
    first = next(iter(analysis.recipes))
    if first != "default" and analysis.requested_roots is None:
        tasks["default"] = {"cmds": [{"task": first}]}
    for recipe in analysis.scoped_recipes:
        task = {"cmds": ([{"task": dep} for dep in recipe.dependencies]
                         + [c.task_command for c in recipe.commands])}
        if recipe.description is not None:
            task["desc"] = recipe.description
        tasks[recipe.name] = task
    # JSON is a YAML 1.2 subset. This also quotes YAML booleans and punctuation
    # safely without a third-party emitter or implicit scalar interpretation.
    content = json.dumps({"version": "3", "run": "once", "tasks": tasks},
                         ensure_ascii=False, indent=2)
    # YAML rejects JSON surrogate-pair escapes and normalizes raw NEL/LS/PS.
    # Emit supplementary characters as UTF-8, escape Unicode line separators.
    for char in ("".join(chr(c) for c in range(0x7F, 0xA0))
                 + "\u2028\u2029\ufffe\uffff"):
        content = content.replace(char, "\\u" + format(ord(char), "04x"))
    return content + "\n"
