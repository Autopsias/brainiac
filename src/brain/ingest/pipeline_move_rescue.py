"""Finish a failed pinned rename as ``shutil.move`` did, through the pinned directory (F45)."""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path

from .pipeline_files import UnsafeVaultDir, _open_dir_nofollow


def _copy_then_unlink(src: Path, name: Path, dir_fd: int, failure: OSError) -> None:
    """``shutil.copy2`` then ``os.unlink``, creating the copy ``O_EXCL|O_NOFOLLOW`` inside the pin.

    The copy gets the mode and times of ``src``; ``copy2``'s extended attributes
    and BSD file flags are not carried. A name already taken is unlinked first, as
    ``rename`` would replace it, where ``copy2`` wrote into it (through a link too).
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # O_NONBLOCK: a FIFO swapped in at the source must not block the open
    src_fd = os.open(src, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            raise failure
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
        try:
            out_fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:  # replace the entry, never write into the inode behind it
            os.unlink(name, dir_fd=dir_fd)
            out_fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        try:
            with os.fdopen(out_fd, "wb") as out, os.fdopen(src_fd, "rb", closefd=False) as fh:
                shutil.copyfileobj(fh, out)
                out.flush()  # the times are set last, or the closing flush would move them
                os.fchmod(out.fileno(), stat.S_IMODE(st.st_mode))
                os.utime(out.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=dir_fd)
            raise
    finally:
        os.close(src_fd)
    os.unlink(src)  # by path, as shutil.move did: the source side is not pinned


def _rename_or_copy(src: Path, name: Path, dir_fd: int, failure: OSError) -> None:
    """``shutil.move``'s second rename, then its rescue: recreate a link, or copy a file."""
    try:
        os.rename(src, name, dst_dir_fd=dir_fd)
        return
    except OSError:
        pass
    kind = os.lstat(src).st_mode
    if stat.S_ISLNK(kind):
        os.symlink(os.readlink(src), name, dir_fd=dir_fd)
        os.unlink(src)
    elif stat.S_ISREG(kind):
        _copy_then_unlink(src, name, dir_fd, failure)
    else:
        raise failure


def finish_failed_rename(src: Path, name: Path, dir_fd: int, failure: OSError) -> None:
    """What ``shutil.move`` did after ``os.rename(src, name)`` failed, written to ``dir_fd``.

    Same order as ``shutil.move``: a destination directory takes ``src`` under
    its own name, then the rename is tried once more, then a link is recreated or
    a file is copied and unlinked. A directory (or any other kind) ``src`` is not
    copied: ``shutil.move`` would copy the tree by path, past the pin, so
    ``failure`` is raised instead.
    """
    try:
        sub_fd = _open_dir_nofollow(dir_fd, name.name, False)
    except (FileNotFoundError, NotADirectoryError, UnsafeVaultDir):
        return _rename_or_copy(src, name, dir_fd, failure)
    try:
        try:
            os.stat(src.name, dir_fd=sub_fd)
        except FileNotFoundError:
            _rename_or_copy(src, Path(src.name), sub_fd, failure)
        else:
            raise shutil.Error(f"Destination path '{name}/{src.name}' already exists")
    finally:
        os.close(sub_fd)
