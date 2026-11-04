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
