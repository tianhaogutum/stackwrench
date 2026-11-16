"""Descriptor-relative Linux traversal. Never follow a symlink target."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .ignore import InputError


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    size: int
    target: str | None = None


def _check_mode(info: os.stat_result, path: str, directory: bool = False) -> None:
    # Also fail closed under root instead of calling a mode-000 file readable.
    if not info.st_mode & 0o444 or (directory and not info.st_mode & 0o111):
        raise InputError(f"{path}: unreadable path (missing read/search permission bits)")


def open_directory(path: Path) -> int:
    """Open every component with NOFOLLOW, including the supplied root."""
    absolute = os.path.abspath(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(absolute).parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _check_mode(os.fstat(fd), absolute, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular_file(path: Path, *, optional: bool = False,
                      limit: int, label: str) -> str | None:
    """Read a bounded regular UTF-8 file with no symlink components."""
    parent_fd = open_directory(path.parent)
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        except FileNotFoundError:
            if optional:
                return None
            raise
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise InputError(f"{path}: {label} must be regular (no symlinks/devices)")
            _check_mode(info, str(path))
            if info.st_size > limit:
                raise InputError(f"{path}: {label} exceeds {limit} byte limit")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise InputError(f"{path}: {label} grew beyond {limit} bytes")
            after = os.fstat(stream.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                raise InputError(f"{path}: {label} changed while reading")
            return data.decode("utf-8", errors="strict")
    finally:
        os.close(parent_fd)


def read_rules_file(path: Path, optional: bool = False) -> str | None:
    return read_regular_file(path, optional=optional, limit=4 * 1024 * 1024,
                             label="ignore file")


def inventory(root: Path, *, include_link_targets: bool = True) -> list[Entry]:
    entries = []
    root_fd = open_directory(root)

    def visit(directory_fd: int, prefix: str) -> None:
        before = os.fstat(directory_fd)
        _check_mode(before, prefix or str(root), True)
        for name in sorted(os.listdir(directory_fd)):
            path = prefix + name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(name, dir_fd=directory_fd) if include_link_targets else None
                entries.append(Entry(path, "symlink", 0, target))
            elif stat.S_ISDIR(mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    if (info.st_dev, info.st_ino) != (os.fstat(child).st_dev, os.fstat(child).st_ino):
                        raise InputError(f"{path}: directory changed during scan")
                    entries.append(Entry(path, "directory", 0))
                    visit(child, path + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                _check_mode(info, path)
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                try:
                    current = os.fstat(fd)
                    if not stat.S_ISREG(current.st_mode) or (
                        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
                    ) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                        raise InputError(f"{path}: file changed during scan")
                    entries.append(Entry(path, "file", current.st_size))
                finally:
                    os.close(fd)
            else:
                raise InputError(f"{path}: unsupported filesystem entry (device, socket or FIFO)")
        after = os.fstat(directory_fd)
        if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
            raise InputError(f"{prefix or root}: directory changed during scan")

    try:
        visit(root_fd, "")
    finally:
        os.close(root_fd)
    return sorted(entries, key=lambda entry: entry.path)
