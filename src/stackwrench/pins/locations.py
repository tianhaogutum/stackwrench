"""Locate tool keys in TOML already validated by tomllib; never resolve values.

Strings and comments are opaque tokens so task bodies cannot supply locations.
This small walk tracks table/dotted/inline keys, including quoted key components.
It is iterative, including when skipping arrays and nested inline values.
"""

from __future__ import annotations

import re
import tomllib

BARE = re.compile(r"[^\s\[\]=.,{}#\"']+")


def tokens(text: str):
    position, line = 0, 1
    while position < len(text):
        start = position
        char = text[position]
        if char in " \t\r":
            position += 1
            continue
        if char == "#":
            end = text.find("\n", position)
            position = len(text) if end < 0 else end
            continue
        if char in "\"'":
            triple = text.startswith(char * 3, position)
            position += 3 if triple else 1
            while position < len(text):
                if char == '"' and text[position] == "\\":
                    position += 2
                elif text[position] == char:
                    end = position
                    while end < len(text) and text[end] == char:
                        end += 1
                    if not triple or end - position >= 3:
                        position = position + 1 if not triple else end
                        break
                    position = end
                else:
                    position += 1
        elif char in "\n[]=.,{}":
            position += 1
        else:
            match = BARE.match(text, position)
            position += len(match[0])
        token = text[start:position]
        yield token, line
        line += token.count("\n")


def tool_lines(text: str) -> dict[str, int]:
    stream = iter(tokens(text))
    locations: dict[str, int] = {}
    table: tuple[str, ...] = ()

    def key(first, terminator):
        parts = []
        token, number = first
        while token != terminator:
            if token != ".":
                # tomllib handles escapes in quoted keys; values were validated
                # by the caller, so this only decodes a single key component.
                parts.append(next(iter(tomllib.loads(token + " = 0"))))
            token, _ = next(stream)
        return tuple(parts), number

    def record(path, number):
        if len(path) >= 2 and path[0] == "tools":
            locations.setdefault(path[1], number)

    for token, number in stream:
        if token == "\n":
            continue
        if token == "[":
            first = next(stream)
            array = first[0] == "["
            path, _ = key(next(stream) if array else first, "]")
            if array:
                next(stream)  # second closing bracket
            table = path
            record(path, number)
            continue
        path, number = key((token, number), "=")
        path = table + path
        record(path, number)
        # Walk only inline tables; other values are opaque until a delimiter.
        # Arrays are skipped as a unit (including their nested inline tables).
        containers = []
        needs_key = False
        for value, value_line in stream:
            if needs_key and value not in ("}", "\n"):
                child, child_line = key((value, value_line), "=")
                path = containers[-1] + child
                record(path, child_line)
                needs_key = False
            elif value == "[":
                depth = 1
                for inner, _ in stream:
                    depth += (inner == "[") - (inner == "]")
                    if not depth:
                        break
            elif value == "{":
                containers.append(path)
                needs_key = True
            elif value == "}":
                containers.pop()
                needs_key = False
            elif value == "," and containers:
                needs_key = True
            elif value == "\n" and not containers:
                break
    return locations
