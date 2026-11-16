"""Small POSIX glob subset, bounded within a symlink-free project boundary."""
from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

from .formats import AuditError

MAX_FILES = 10000
MAX_BYTES = 256 * 1024 * 1024


def validate_pattern(pattern: str):
    if not pattern or pattern.startswith("/") or any(c in pattern for c in "\\[]{}!~:\x00"):
        raise AuditError("source/output path outside supported relative glob syntax")
    parts = pattern.split("/")
    if any(p in ("", ".", "..") for p in parts) or any("**" in p and p != "**" for p in parts) or parts[-1] == "**":
        raise AuditError("source/output path has forbidden components or terminal **")
    return parts


def expand(root: Path, pattern: str) -> list[Path]:
    parts = validate_pattern(pattern)
    found = set()
    visited = 0

    def visit(base, index):
        nonlocal visited
        visited += 1
        if visited > 100000:
            raise AuditError("source glob exceeds traversal limit")
        if index == len(parts):
            if base.is_file():
                found.add(base)
            return
        part = parts[index]
        if part == "**":
            visit(base, index + 1)
        for child in sorted(base.iterdir()):
            if part != "**" and not fnmatch.fnmatchcase(child.name, part):
                continue
            if child.is_symlink():
                raise AuditError("symlink encountered in source expansion; not followed")
            if part == "**":
                if child.is_dir():
                    visit(child, index)
            elif index == len(parts) - 1:
                if child.is_file():
                    found.add(child)
                elif not any(c in pattern for c in "*?"):
                    raise AuditError("literal source is not a regular file")
            elif child.is_dir():
                visit(child, index + 1)

    try:
        visit(root, 0)
    except OSError:
        raise AuditError("cannot traverse source paths") from None
    if not found:
        raise AuditError("source pattern matched no regular files")
    if len(found) > MAX_FILES:
        raise AuditError("source file count limit exceeded")
    return sorted(found)


def hash_sources(root: Path, patterns: list[str]) -> dict[str, str]:
    files = set()
    for pattern in patterns:
        files.update(expand(root, pattern))
        if len(files) > MAX_FILES:
            raise AuditError("source file count limit exceeded")
    result = {}
    total = 0
    for file in sorted(files):
        # A second boundary check catches ordinary replacement between expansion and reading.
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            raise AuditError("source escapes project root or is a symlink")
        try:
            before = file.stat()
            if not file.is_file():
                raise AuditError("source is not a regular file")
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                while block := stream.read(65536):
                    total += len(block)
                    if total > MAX_BYTES:
                        raise AuditError("source contents exceed 256 MiB audit limit")
                    digest.update(block)
            after = file.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise AuditError("source changed while hashing; retry on a stable tree")
        except OSError:
            raise AuditError("cannot read source file") from None
        result[file.relative_to(root).as_posix()] = digest.hexdigest()
    return result
