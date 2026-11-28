"""Durable-write temp-name helper and the one write-then-replace helper."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

from .lock import _atomic_temp_path

atomic_temp_path = _atomic_temp_path


def _fsync_dir(d: Path) -> None:
    """fsync a directory so a rename in it survives a crash; unsupported is fine."""
    if os.name == "nt":
        return  # no directory descriptors on Windows
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise
    finally:
        os.close(fd)


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to a sibling temp file, fsync, then ``os.replace`` it in.

    The temp file is removed on any error, so the target keeps its old content.
    """
    tmp = atomic_temp_path(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)  # after the replace: the target must not be unlinked on failure


def write_json_atomic(path: Path, obj: Any) -> None:
    """``write_text_atomic`` of ``json.dumps(obj, indent=2)`` plus a newline."""
    write_text_atomic(path, json.dumps(obj, indent=2) + "\n")


def _fsync_dir_strict(d: Path) -> None:
    """fsync a directory entry and raise on failure (ADR-0015 §1.3)."""
    if os.name == "nt":
        return
    dfd = os.open(d, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_bytes_durable(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace ``path`` with ``data`` atomically and durably, or raise.

    Uses an unpredictable sibling temp name opened with
    ``O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW``, verifies ``S_ISREG``, loops
    on short writes, calls ``fsync`` and ``fchmod`` on the open fd, atomically
    replaces ``path``, and strictly ``fsync``s the parent directory.
    """
    import stat as _stat

    tmp = atomic_temp_path(path)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(tmp, flags, 0o600)
    closed = False
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"refusing to write {tmp.name}: not a regular file")
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise OSError(
                    f"write made no progress on {tmp.name} ({len(view)} bytes left)"
                )
            view = view[n:]
        if os.name != "nt":
            os.fchmod(fd, mode)
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.replace(tmp, path)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_dir_strict(path.parent)
