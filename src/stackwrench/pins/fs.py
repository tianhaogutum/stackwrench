"""Root-anchored, no-symlink reads and prevalidated, atomic-per-file writes.

Linux/POSIX dir_fd operations are intentional. No shell commands are executed.
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_BYTES = 1024 * 1024


class InputError(Exception):
    def __init__(self, path: str, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.path, self.code = path, code


@dataclass(frozen=True)
class Snapshot:
    path: str
    data: bytes
    identity: tuple[int, ...]
    mode: int


def identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink)


def relative_path(path: str) -> str:
    if not path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise InputError(path, "expected a nonempty root-relative file path", "unsafe_path")
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).is_absolute() or ".." in parts or not parts:
        raise InputError(path, "paths must be relative to root, without '..'", "unsafe_path")
    return str(PurePosixPath(path))


class Root:
    def __init__(self, path: str):
        if ".." in Path(path).parts or "\x00" in path:
            raise InputError(path, "root must not contain '..' or NUL", "unsafe_path")
        self.path = os.path.abspath(path)
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in Path(self.path).parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        except OSError as exc:
            os.close(fd)
            raise InputError(path, f"cannot open root without following symlinks: {exc.strerror}",
                             "unsafe_path") from exc
        self.fd = fd

    def close(self) -> None:
        os.close(self.fd)

    def parent(self, path: str) -> tuple[int, str]:
        parts = PurePosixPath(relative_path(path)).parts
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def read(self, path: str, *, optional: bool = False) -> Snapshot | None:
        path = relative_path(path)
        parent_fd = None
        try:
            parent_fd, name = self.parent(path)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise InputError(path, "input must be a regular file", "unsafe_path")
                if info.st_nlink != 1:
                    raise InputError(path, "hard-linked inputs are refused", "unsafe_path")
                if info.st_size > MAX_BYTES:
                    raise InputError(path, f"input exceeds {MAX_BYTES} bytes")
                data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES or identity(info) != identity(os.fstat(stream.fileno())):
                    raise InputError(path, "input is too large or changed while reading")
                return Snapshot(path, data, identity(info), stat.S_IMODE(info.st_mode))
        except FileNotFoundError as exc:
            if optional:
                return None
            raise InputError(path, "file or parent directory does not exist", "missing_file") from exc
        except OSError as exc:
            raise InputError(path, f"cannot read without following symlinks: {exc.strerror}",
                             "unsafe_path") from exc
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

    def verify(self, snapshots: list[Snapshot]) -> None:
        for original in snapshots:
            current = self.read(original.path)
            if current is None or (current.identity, current.data) != (original.identity, original.data):
                raise InputError(original.path, "file changed since inspection; run again", "concurrent_change")

    def apply(self, snapshots: list[Snapshot], updates: dict[str, bytes]) -> None:
        """Stage all edits, revalidate all inputs, then replace changed files.

        Input errors cannot produce a partial update. On an I/O failure during
        commit, attempt rollback. This is not a multi-file filesystem transaction.
        """
        staged: list[tuple[Snapshot, int, str, str, str]] = []
        replaced: list[tuple[Snapshot, int, str, str, str]] = []
        retained_backups: set[str] = set()
        by_path = {item.path: item for item in snapshots}
        try:
            self.verify(snapshots)
            for path, data in updates.items():
                original = by_path[path]
                parent_fd, name = self.parent(path)
                temp = backup = ""
                try:
                    temp = self._stage(parent_fd, data, original.mode)
                    backup = self._stage(parent_fd, original.data, original.mode)
                except BaseException:
                    for candidate in (temp, backup):
                        if candidate:
                            os.unlink(candidate, dir_fd=parent_fd)
                    os.close(parent_fd)
                    raise
                staged.append((original, parent_fd, name, temp, backup))
            self.verify(snapshots)
            for item in staged:
                original, parent_fd, name, temp, _ = item
                self.verify([original])
                # Also detect a renamed/replaced parent directory before commit.
                fresh_fd, _ = self.parent(original.path)
                try:
                    fresh_info, staged_info = os.fstat(fresh_fd), os.fstat(parent_fd)
                    if (fresh_info.st_dev, fresh_info.st_ino) != (staged_info.st_dev, staged_info.st_ino):
                        raise InputError(original.path, "parent directory changed", "concurrent_change")
                finally:
                    os.close(fresh_fd)
                os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                replaced.append(item)
        except (OSError, InputError) as exc:
            rollback_errors = []
            for original, parent_fd, name, _, backup in reversed(replaced):
                try:
                    os.replace(backup, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except OSError as rollback:
                    retained_backups.add(backup)
                    parent = str(PurePosixPath(original.path).parent)
                    rollback_errors.append(f"{original.path}: {rollback}; original bytes saved in {parent}/{backup}")
            location = f"{exc.path}: " if isinstance(exc, InputError) else ""
            detail = f"apply aborted: {location}{exc}"
            if rollback_errors:
                detail += "; ROLLBACK FAILED; inspect files: " + "; ".join(rollback_errors)
            raise InputError("<apply>", detail, "write_error") from exc
        finally:
            for _, parent_fd, _, temp, backup in staged:
                for candidate in (temp, backup):
                    if candidate in retained_backups:
                        continue
                    try:
                        os.unlink(candidate, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                os.close(parent_fd)

    @staticmethod
    def _stage(parent_fd: int, data: bytes, mode: int) -> str:
        name = f".stackwrench-pins-{secrets.token_hex(12)}.tmp"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), mode)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(name, dir_fd=parent_fd)
            raise
        return name
