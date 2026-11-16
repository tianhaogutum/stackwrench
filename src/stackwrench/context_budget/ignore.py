"""Independent Linux Docker-ignore subset, not Python fnmatch/gitignore.

See tests/test_ignore.py and README.md for the conformance boundary.
"""

from dataclasses import dataclass
import posixpath
import re


class InputError(ValueError):
    """Input cannot be confidently evaluated."""


def _character_class(pattern: str, start: int) -> tuple[str, int]:
    i = start + 1
    negative = i < len(pattern) and pattern[i] == "^"
    i += int(negative)
    atoms = []
    while i < len(pattern) and pattern[i] != "]":
        ch = pattern[i]
        if ch == "\\":
            i += 1
            if i == len(pattern):
                raise InputError("trailing escape in character class")
            ch = pattern[i]
        elif ch in "[-":
            raise InputError("unescaped '[' or '-' in character class")
        if ch == "/":
            raise InputError("slash in character class is unsupported")
        i += 1
        if i < len(pattern) and pattern[i] == "-":
            i += 1
            if i == len(pattern) or pattern[i] == "]":
                raise InputError("character range needs an endpoint; escape a literal '-'")
            end = pattern[i]
            if end == "\\":
                i += 1
                if i == len(pattern):
                    raise InputError("trailing escape in character range")
                end = pattern[i]
            if end in "/[" or ord(end) < ord(ch):
                raise InputError("invalid character range")
            atoms.append(re.escape(ch) + "-" + re.escape(end))
            i += 1
        else:
            atoms.append(re.escape(ch))
    if i == len(pattern) or not atoms:
        raise InputError("unterminated or empty character class")
    # Negated Go classes cannot match a path separator.
    return "[" + ("^/" if negative else "") + "".join(atoms) + "]", i + 1


def compile_pattern(pattern: str) -> re.Pattern:
    """Compile anchored POSIX patterns; ** must occupy a whole component."""
    result = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 2] == "**":
                after = i + 2
                if (i and pattern[i - 1] != "/") or (
                    after < len(pattern) and pattern[after] != "/"
                ):
                    raise InputError("'**' must be a whole path component (embedded/repeated stars unsupported)")
                if after < len(pattern):
                    result.append("(?:[^/]+/)*")
                    i = after + 1
                else:
                    result.append(".*")
                    i = after
            else:
                result.append("[^/]*")
                i += 1
        elif ch == "?":
            result.append("[^/]")
            i += 1
        elif ch == "[":
            compiled, i = _character_class(pattern, i)
            result.append(compiled)
        elif ch == "]":
            raise InputError("unmatched ']'; escape a literal closing bracket")
        elif ch == "\\":
            i += 1
            if i == len(pattern):
                raise InputError("trailing escape")
            if pattern[i] not in "*?[]!#\\ -^":
                raise InputError("unsupported escape; only literal glob/comment characters may be escaped")
            result.append(re.escape(pattern[i]))
            i += 1
        else:
            result.append(re.escape(ch))
            i += 1
    return re.compile("".join(result), re.DOTALL)


@dataclass(frozen=True)
class Rule:
    source: str
    line: int
    raw: str
    pattern: str
    negated: bool
    regex: re.Pattern

    def provenance(self, matched_path: str) -> dict:
        return {"source": self.source, "line": self.line, "raw": self.raw,
                "pattern": self.pattern, "negated": self.negated,
                "matched_path": matched_path}


def parse_rules(text: str, source: str) -> list[Rule]:
    rules = []
    # Only LF/CRLF are line delimiters, not Python's other Unicode separators.
    for number, raw in enumerate(text.removeprefix("\ufeff").split("\n"), 1):
        raw = raw.removesuffix("\r")
        if len(raw.encode("utf-8")) > 65535:
            raise InputError(f"{source}:{number}: ignore line exceeds 65535 bytes")
        if raw.startswith("#"):
            continue
        if any(ch in raw for ch in "\x1c\x1d\x1e\x1f"):
            raise InputError(f"{source}:{number}: unsupported control whitespace in pattern")
        if not raw.strip():
            continue
        pattern = raw.strip()
        negated = pattern.startswith("!")
        if negated:
            pattern = pattern[1:].strip()
            if not pattern:
                raise InputError(f"{source}:{number}: bare '!' is invalid")
        if "\x00" in pattern:
            raise InputError(f"{source}:{number}: NUL in pattern")
        # POSIX normpath retains exactly two leading slashes; Docker does not.
        pattern = posixpath.normpath(pattern.lstrip("/") if pattern.startswith("/") else pattern)
        if pattern == ".":
            continue
        if pattern == ".." or pattern.startswith("../"):
            raise InputError(f"{source}:{number}: pattern escapes context after cleaning")
        if not negated and pattern.startswith("!"):
            raise InputError(f"{source}:{number}: cleaning exposes a leading '!'; write the normalized rule explicitly")
        try:
            regex = compile_pattern(pattern)
        except (InputError, re.error) as exc:
            raise InputError(f"{source}:{number}: {exc}") from exc
        rules.append(Rule(source, number, raw, pattern, negated, regex))
    return rules


def decide(path: str, rules: list[Rule]) -> tuple[bool, dict | None]:
    """Evaluate all rules on the path and ancestors; last match wins.

    A bare name matches at the context root, not at arbitrary depth. An
    excluded parent does not prevent a later rule from including its child.
    """
    parts = path.split("/")
    candidates = [path] + ["/".join(parts[:i]) for i in range(1, len(parts))]
    included, last = True, None
    for rule in rules:
        for candidate in candidates:
            if rule.regex.fullmatch(candidate):
                included = rule.negated
                last = rule.provenance(candidate)
                break
    return included, last
