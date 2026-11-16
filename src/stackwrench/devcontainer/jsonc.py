"""JSON with comments, ported from Microsoft's ``jsonc-parser`` 3.3.1 (MIT).

Only the API used by the Dev Container checker is reproduced: ``parse_tree``
(``parseTree``) with its fault-tolerant error recovery, ``get_node_value``,
``find_node_at_location`` and ``print_parse_error_code``. Scanner and parser
follow the upstream state machine token for token, so error codes and the
offset of the first error match. Offsets index the Python string (code
points); callers convert them to UTF-16 columns. See
``licenses/devcontainer/jsonc-parser-LICENSE.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .jsutil import combine_surrogates, js_object

# SyntaxKind
OPEN_BRACE, CLOSE_BRACE, OPEN_BRACKET, CLOSE_BRACKET, COMMA, COLON = 1, 2, 3, 4, 5, 6
NULL_KEYWORD, TRUE_KEYWORD, FALSE_KEYWORD, STRING_LITERAL, NUMERIC_LITERAL = 7, 8, 9, 10, 11
LINE_COMMENT, BLOCK_COMMENT, LINE_BREAK, TRIVIA, UNKNOWN, EOF = 12, 13, 14, 15, 16, 17

# ScanError
SCAN_NONE, SCAN_END_OF_COMMENT, SCAN_END_OF_STRING, SCAN_END_OF_NUMBER = 0, 1, 2, 3
SCAN_INVALID_UNICODE, SCAN_INVALID_ESCAPE, SCAN_INVALID_CHARACTER = 4, 5, 6

# ParseErrorCode
INVALID_SYMBOL, INVALID_NUMBER_FORMAT, PROPERTY_NAME_EXPECTED, VALUE_EXPECTED = 1, 2, 3, 4
COLON_EXPECTED, COMMA_EXPECTED, CLOSE_BRACE_EXPECTED, CLOSE_BRACKET_EXPECTED = 5, 6, 7, 8
END_OF_FILE_EXPECTED, INVALID_COMMENT_TOKEN, UNEXPECTED_END_OF_COMMENT = 9, 10, 11
UNEXPECTED_END_OF_STRING, UNEXPECTED_END_OF_NUMBER, INVALID_UNICODE = 12, 13, 14
INVALID_ESCAPE_CHARACTER, INVALID_CHARACTER = 15, 16

_ERROR_NAMES = {
    INVALID_SYMBOL: "InvalidSymbol",
    INVALID_NUMBER_FORMAT: "InvalidNumberFormat",
    PROPERTY_NAME_EXPECTED: "PropertyNameExpected",
    VALUE_EXPECTED: "ValueExpected",
    COLON_EXPECTED: "ColonExpected",
    COMMA_EXPECTED: "CommaExpected",
    CLOSE_BRACE_EXPECTED: "CloseBraceExpected",
    CLOSE_BRACKET_EXPECTED: "CloseBracketExpected",
    END_OF_FILE_EXPECTED: "EndOfFileExpected",
    INVALID_COMMENT_TOKEN: "InvalidCommentToken",
    UNEXPECTED_END_OF_COMMENT: "UnexpectedEndOfComment",
    UNEXPECTED_END_OF_STRING: "UnexpectedEndOfString",
    UNEXPECTED_END_OF_NUMBER: "UnexpectedEndOfNumber",
    INVALID_UNICODE: "InvalidUnicode",
    INVALID_ESCAPE_CHARACTER: "InvalidEscapeCharacter",
    INVALID_CHARACTER: "InvalidCharacter",
}


def print_parse_error_code(code: int) -> str:
    return _ERROR_NAMES.get(code, "<unknown ParseErrorCode>")


@dataclass
class ParseError:
    error: int
    offset: int
    length: int


@dataclass(eq=False)
class Node:
    type: str
    offset: int
    length: int = -1
    value: Any = None
    children: list["Node"] | None = None
    parent: "Node | None" = field(default=None, repr=False)
    colon_offset: int | None = None


def _is_white_space(ch: int) -> bool:
    return ch == 32 or ch == 9


def _is_line_break(ch: int) -> bool:
    return ch == 10 or ch == 13


def _is_digit(ch: int) -> bool:
    return 48 <= ch <= 57


class _Scanner:
    """``createScanner(text, false)``: every token, trivia included."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.len = len(text)
        self.pos = 0
        self.value = ""
        self.token_offset = 0
        self.token = UNKNOWN
        self.scan_error = SCAN_NONE

    def code(self, pos: int) -> int:
        # charCodeAt() past the end is NaN: never equal to anything.
        return ord(self.text[pos]) if 0 <= pos < self.len else -1

    def scan_hex_digits(self, count: int) -> int:
        digits = 0
        value = 0
        while digits < count:
            ch = self.code(self.pos)
            if 48 <= ch <= 57:
                value = value * 16 + ch - 48
            elif 65 <= ch <= 70:
                value = value * 16 + ch - 65 + 10
            elif 97 <= ch <= 102:
                value = value * 16 + ch - 97 + 10
            else:
                break
            self.pos += 1
            digits += 1
        if digits < count:
            value = -1
        return value

    def scan_number(self) -> str:
        text = self.text
        start = self.pos
        if self.code(self.pos) == 48:
            self.pos += 1
        else:
            self.pos += 1
            while self.pos < self.len and _is_digit(self.code(self.pos)):
                self.pos += 1
        if self.pos < self.len and self.code(self.pos) == 46:
            self.pos += 1
            if self.pos < self.len and _is_digit(self.code(self.pos)):
                self.pos += 1
                while self.pos < self.len and _is_digit(self.code(self.pos)):
                    self.pos += 1
            else:
                self.scan_error = SCAN_END_OF_NUMBER
                return text[start : self.pos]
        end = self.pos
        if self.pos < self.len and self.code(self.pos) in (69, 101):
            self.pos += 1
            # Upstream precedence: (pos < length && ch === '+') || ch === '-'.
            if (self.pos < self.len and self.code(self.pos) == 43) or self.code(self.pos) == 45:
                self.pos += 1
            if self.pos < self.len and _is_digit(self.code(self.pos)):
                self.pos += 1
                while self.pos < self.len and _is_digit(self.code(self.pos)):
                    self.pos += 1
                end = self.pos
            else:
                self.scan_error = SCAN_END_OF_NUMBER
        return text[start:end]

    def scan_string(self) -> str:
        text = self.text
        result: list[str] = []
        start = self.pos
        while True:
            if self.pos >= self.len:
                result.append(text[start : self.pos])
                self.scan_error = SCAN_END_OF_STRING
                break
            ch = self.code(self.pos)
            if ch == 34:
                result.append(text[start : self.pos])
                self.pos += 1
                break
            if ch == 92:
                result.append(text[start : self.pos])
                self.pos += 1
                if self.pos >= self.len:
                    self.scan_error = SCAN_END_OF_STRING
                    break
                ch2 = self.code(self.pos)
                self.pos += 1
                if ch2 == 34:
                    result.append('"')
                elif ch2 == 92:
                    result.append("\\")
                elif ch2 == 47:
                    result.append("/")
                elif ch2 == 98:
                    result.append("\b")
                elif ch2 == 102:
                    result.append("\f")
                elif ch2 == 110:
                    result.append("\n")
                elif ch2 == 114:
                    result.append("\r")
                elif ch2 == 116:
                    result.append("\t")
                elif ch2 == 117:
                    ch3 = self.scan_hex_digits(4)
                    if ch3 >= 0:
                        result.append(chr(ch3))
                    else:
                        self.scan_error = SCAN_INVALID_UNICODE
                else:
                    self.scan_error = SCAN_INVALID_ESCAPE
                start = self.pos
                continue
            if 0 <= ch <= 0x1F:
                if _is_line_break(ch):
                    result.append(text[start : self.pos])
                    self.scan_error = SCAN_END_OF_STRING
                    break
                # Mark as error but continue with the string.
                self.scan_error = SCAN_INVALID_CHARACTER
            self.pos += 1
        return combine_surrogates("".join(result))

    def scan(self) -> int:
        self.token = self._scan_next()
        return self.token

    def _scan_next(self) -> int:
        text = self.text
        self.value = ""
        self.scan_error = SCAN_NONE
        self.token_offset = self.pos
        if self.pos >= self.len:
            self.token_offset = self.len
            return EOF
        code = self.code(self.pos)
        if _is_white_space(code):
            while True:
                self.pos += 1
                code = self.code(self.pos)
                if not _is_white_space(code):
                    break
            self.value = text[self.token_offset : self.pos]
            return TRIVIA
        if _is_line_break(code):
            self.pos += 1
            if code == 13 and self.code(self.pos) == 10:
                self.pos += 1
            self.value = text[self.token_offset : self.pos]
            return LINE_BREAK
        if code == 123:
            self.pos += 1
            return OPEN_BRACE
        if code == 125:
            self.pos += 1
            return CLOSE_BRACE
        if code == 91:
            self.pos += 1
            return OPEN_BRACKET
        if code == 93:
            self.pos += 1
            return CLOSE_BRACKET
        if code == 58:
            self.pos += 1
            return COLON
        if code == 44:
            self.pos += 1
            return COMMA
        if code == 34:
            self.pos += 1
            self.value = self.scan_string()
            return STRING_LITERAL
        if code == 47:
            start = self.pos - 1
            if self.code(self.pos + 1) == 47:
                self.pos += 2
                while self.pos < self.len:
                    if _is_line_break(self.code(self.pos)):
                        break
                    self.pos += 1
                self.value = text[max(start, 0) : self.pos]
                return LINE_COMMENT
            if self.code(self.pos + 1) == 42:
                self.pos += 2
                safe_length = self.len - 1
                closed = False
                while self.pos < safe_length:
                    ch = self.code(self.pos)
                    if ch == 42 and self.code(self.pos + 1) == 47:
                        self.pos += 2
                        closed = True
                        break
                    self.pos += 1
                    if _is_line_break(ch) and ch == 13 and self.code(self.pos) == 10:
                        self.pos += 1
                if not closed:
                    self.pos += 1
                    self.scan_error = SCAN_END_OF_COMMENT
                self.value = text[max(start, 0) : self.pos]
                return BLOCK_COMMENT
            self.value = "/"
            self.pos += 1
            return UNKNOWN
        if code == 45:
            self.value = "-"
            self.pos += 1
            if self.pos == self.len or not _is_digit(self.code(self.pos)):
                return UNKNOWN
            # A minus followed by a digit falls through to the number scanner.
            self.value += self.scan_number()
            return NUMERIC_LITERAL
        if _is_digit(code):
            self.value = self.scan_number()
            return NUMERIC_LITERAL
        # Literals and unknown symbols: read the full word.
        while self.pos < self.len and _is_unknown_content_character(code):
            self.pos += 1
            code = self.code(self.pos)
        if self.token_offset != self.pos:
            self.value = text[self.token_offset : self.pos]
            if self.value == "true":
                return TRUE_KEYWORD
            if self.value == "false":
                return FALSE_KEYWORD
            if self.value == "null":
                return NULL_KEYWORD
            return UNKNOWN
        self.value = text[self.pos]
        self.pos += 1
        return UNKNOWN


def _is_unknown_content_character(code: int) -> bool:
    if _is_white_space(code) or _is_line_break(code):
        return False
    return code not in (125, 93, 123, 91, 34, 58, 44, 47)


def _js_number(token: str) -> float | int:
    """``Number(tokenValue)`` for scanner-produced numeric literals."""
    text = token
    if text.lstrip("-").isdigit():
        value = int(text)
        if abs(value) <= 2**53:
            return value
    try:
        result = float(text)
    except ValueError:
        return float("nan")
    return result


def _node_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    return "object"


def _visit(text: str, visitor: "_TreeBuilder", allow_trailing_comma: bool, disallow_comments: bool) -> None:
    scanner = _Scanner(text)

    def on_error(error: int) -> None:
        visitor.on_error(error, scanner.token_offset, scanner.pos - scanner.token_offset)

    def scan_next() -> int:
        while True:
            token = scanner.scan()
            err = scanner.scan_error
            if err == SCAN_INVALID_UNICODE:
                handle_error(INVALID_UNICODE)
            elif err == SCAN_INVALID_ESCAPE:
                handle_error(INVALID_ESCAPE_CHARACTER)
            elif err == SCAN_END_OF_NUMBER:
                handle_error(UNEXPECTED_END_OF_NUMBER)
            elif err == SCAN_END_OF_COMMENT:
                if not disallow_comments:
                    handle_error(UNEXPECTED_END_OF_COMMENT)
            elif err == SCAN_END_OF_STRING:
                handle_error(UNEXPECTED_END_OF_STRING)
            elif err == SCAN_INVALID_CHARACTER:
                handle_error(INVALID_CHARACTER)
            if token in (LINE_COMMENT, BLOCK_COMMENT):
                if disallow_comments:
                    handle_error(INVALID_COMMENT_TOKEN)
            elif token == UNKNOWN:
                handle_error(INVALID_SYMBOL)
            elif token in (TRIVIA, LINE_BREAK):
                pass
            else:
                return token

    def handle_error(error: int, skip_until_after: tuple[int, ...] = (), skip_until: tuple[int, ...] = ()) -> None:
        on_error(error)
        if skip_until_after or skip_until:
            token = scanner.token
            while token != EOF:
                if token in skip_until_after:
                    scan_next()
                    break
                if token in skip_until:
                    break
                token = scan_next()

    def offset_length() -> tuple[int, int]:
        return scanner.token_offset, scanner.pos - scanner.token_offset

    def parse_string(is_value: bool) -> bool:
        value = scanner.value
        if is_value:
            visitor.on_literal_value(value, *offset_length())
        else:
            visitor.on_object_property(value, *offset_length())
        scan_next()
        return True

    def parse_literal() -> bool:
        token = scanner.token
        if token == NUMERIC_LITERAL:
            value: Any = _js_number(scanner.value)
            if isinstance(value, float) and value != value:
                handle_error(INVALID_NUMBER_FORMAT)
                value = 0
            visitor.on_literal_value(value, *offset_length())
        elif token == NULL_KEYWORD:
            visitor.on_literal_value(None, *offset_length())
        elif token == TRUE_KEYWORD:
            visitor.on_literal_value(True, *offset_length())
        elif token == FALSE_KEYWORD:
            visitor.on_literal_value(False, *offset_length())
        else:
            return False
        scan_next()
        return True

    def parse_property() -> bool:
        if scanner.token != STRING_LITERAL:
            handle_error(PROPERTY_NAME_EXPECTED, (), (CLOSE_BRACE, COMMA))
            return False
        parse_string(False)
        if scanner.token == COLON:
            visitor.on_separator(":", *offset_length())
            scan_next()
            if not parse_value():
                handle_error(VALUE_EXPECTED, (), (CLOSE_BRACE, COMMA))
        else:
            handle_error(COLON_EXPECTED, (), (CLOSE_BRACE, COMMA))
        return True

    def parse_object() -> bool:
        visitor.on_object_begin(*offset_length())
        scan_next()
        needs_comma = False
        while scanner.token not in (CLOSE_BRACE, EOF):
            if scanner.token == COMMA:
                if not needs_comma:
                    handle_error(VALUE_EXPECTED)
                visitor.on_separator(",", *offset_length())
                scan_next()
                if scanner.token == CLOSE_BRACE and allow_trailing_comma:
                    break
            elif needs_comma:
                handle_error(COMMA_EXPECTED)
            if not parse_property():
                handle_error(VALUE_EXPECTED, (), (CLOSE_BRACE, COMMA))
            needs_comma = True
        visitor.on_object_end(*offset_length())
        if scanner.token != CLOSE_BRACE:
            handle_error(CLOSE_BRACE_EXPECTED, (CLOSE_BRACE,), ())
        else:
            scan_next()
        return True

    def parse_array() -> bool:
        visitor.on_array_begin(*offset_length())
        scan_next()
        needs_comma = False
        while scanner.token not in (CLOSE_BRACKET, EOF):
            if scanner.token == COMMA:
                if not needs_comma:
                    handle_error(VALUE_EXPECTED)
                visitor.on_separator(",", *offset_length())
                scan_next()
                if scanner.token == CLOSE_BRACKET and allow_trailing_comma:
                    break
            elif needs_comma:
                handle_error(COMMA_EXPECTED)
            if not parse_value():
                handle_error(VALUE_EXPECTED, (), (CLOSE_BRACKET, COMMA))
            needs_comma = True
        visitor.on_array_end(*offset_length())
        if scanner.token != CLOSE_BRACKET:
            handle_error(CLOSE_BRACKET_EXPECTED, (CLOSE_BRACKET,), ())
        else:
            scan_next()
        return True

    def parse_value() -> bool:
        token = scanner.token
        if token == OPEN_BRACKET:
            return parse_array()
        if token == OPEN_BRACE:
            return parse_object()
        if token == STRING_LITERAL:
            return parse_string(True)
        return parse_literal()

    scan_next()
    if scanner.token == EOF:
        handle_error(VALUE_EXPECTED)
        return
    if not parse_value():
        handle_error(VALUE_EXPECTED)
        return
    if scanner.token != EOF:
        handle_error(END_OF_FILE_EXPECTED)


class _TreeBuilder:
    """The ``parseTree`` visitor."""

    def __init__(self, errors: list[ParseError]) -> None:
        self.errors = errors
        self.current = Node("array", -1, -1, children=[])  # artificial root

    def _ensure_property_complete(self, end_offset: int) -> None:
        if self.current.type == "property":
            self.current.length = end_offset - self.current.offset
            assert self.current.parent is not None
            self.current = self.current.parent

    def _on_value(self, node: Node) -> Node:
        assert self.current.children is not None
        self.current.children.append(node)
        return node

    def on_object_begin(self, offset: int, length: int) -> None:
        self.current = self._on_value(Node("object", offset, -1, parent=self.current, children=[]))

    def on_object_property(self, name: str, offset: int, length: int) -> None:
        self.current = self._on_value(Node("property", offset, -1, parent=self.current, children=[]))
        assert self.current.children is not None
        self.current.children.append(Node("string", offset, length, value=name, parent=self.current))

    def on_object_end(self, offset: int, length: int) -> None:
        self._ensure_property_complete(offset + length)
        self.current.length = offset + length - self.current.offset
        assert self.current.parent is not None
        self.current = self.current.parent
        self._ensure_property_complete(offset + length)

    def on_array_begin(self, offset: int, length: int) -> None:
        self.current = self._on_value(Node("array", offset, -1, parent=self.current, children=[]))

    def on_array_end(self, offset: int, length: int) -> None:
        self.current.length = offset + length - self.current.offset
        assert self.current.parent is not None
        self.current = self.current.parent
        self._ensure_property_complete(offset + length)

    def on_literal_value(self, value: Any, offset: int, length: int) -> None:
        self._on_value(Node(_node_type(value), offset, length, value=value, parent=self.current))
        self._ensure_property_complete(offset + length)

    def on_separator(self, sep: str, offset: int, length: int) -> None:
        if self.current.type == "property":
            if sep == ":":
                self.current.colon_offset = offset
            elif sep == ",":
                self._ensure_property_complete(offset)

    def on_error(self, error: int, offset: int, length: int) -> None:
        self.errors.append(ParseError(error, offset, length))


def parse_tree(
    text: str,
    errors: list[ParseError],
    *,
    allow_trailing_comma: bool = False,
    disallow_comments: bool = False,
) -> Node | None:
    """``parseTree(text, errors, options)``; appends to ``errors`` in order."""
    builder = _TreeBuilder(errors)
    _visit(text, builder, allow_trailing_comma, disallow_comments)
    # Upstream reads currentParent.children[0]; after a complete visit that is
    # the artificial root.
    children = builder.current.children
    result = children[0] if children else None
    if result is not None:
        result.parent = None
    return result


def find_node_at_location(root: Node | None, path: list[str | int]) -> Node | None:
    if root is None:
        return None
    node = root
    for segment in path:
        if isinstance(segment, str):
            if node.type != "object" or node.children is None:
                return None
            found = False
            for prop in node.children:
                if prop.children is not None and len(prop.children) == 2 and prop.children[0].value == segment:
                    node = prop.children[1]
                    found = True
                    break
            if not found:
                return None
        else:
            index = segment
            if node.type != "array" or index < 0 or node.children is None or index >= len(node.children):
                return None
            node = node.children[index]
    return node


def get_node_value(node: Node) -> Any:
    if node.type == "array":
        assert node.children is not None
        return [get_node_value(child) for child in node.children]
    if node.type == "object":
        assert node.children is not None
        return js_object(
            (prop.children[0].value, get_node_value(prop.children[1]))
            for prop in node.children
            if prop.children is not None and len(prop.children) > 1
        )
    if node.type in ("null", "string", "number", "boolean"):
        return node.value
    return None
