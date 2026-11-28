"""Workspace sweep into the inbox (WSP-01)."""
from __future__ import annotations

import datetime as datetime
import errno
import itertools as itertools
import json as json
import logging as logging
import re as re
import shlex as shlex
import shutil
import stat
from pathlib import Path
from typing import Any
import os
import time


# ---------------------------------------------------------------------------
# Workspace sweep (WSP-01, 2026-07-11). A live working folder (e.g. an
# an Obsidian workspace folder) accumulates hundreds of session artifacts
# with no lifecycle. The sweep gives them one: a SETTLED file (mtime older
# than the age gate — nobody is editing it any more) is MOVED into the
# vault's `inbox/`, where the standard ingest drain archives the original
# immutably under `raw/originals/`, signs + writes the `raw/` note (with a
# filename-derived `document_date`), the next sync embeds it, and the
# monthly graphify wires it into the discovery graph. Content already
# ingested dedups by content hash (parked in the inbox duplicate dir), so
# the sweep is idempotent and never double-ingests.
#
# Scope rules (deliberately dumb): TOP-LEVEL FILES ONLY — subdirectories are
# other systems' machine state (skill packages, archives, trust runs) and are
# never touched; dotfiles are skipped; a source that is a symlink, and a symlink
# file inside a source, are never followed (F11: the Cowork VM can plant one
# in the workspace folder); the destination inbox and its vault dir are pinned
# the same way and refused when they are links (F201). Actively-edited files have a fresh
# mtime and are skipped by the age gate. Configuration: the sweep only runs
# when dirs are configured ($BRAIN_WORKSPACE_SWEEP_DIRS, os.pathsep-separated;
# $BRAIN_WORKSPACE_SWEEP_AGE_DAYS, default 14) — no config, no sweep.
# ---------------------------------------------------------------------------
WORKSPACE_SWEEP_DIRS_ENV = "BRAIN_WORKSPACE_SWEEP_DIRS"
WORKSPACE_SWEEP_AGE_ENV = "BRAIN_WORKSPACE_SWEEP_AGE_DAYS"
WORKSPACE_SWEEP_DEFAULT_AGE_DAYS = 14


def workspace_sweep_config() -> tuple[list[tuple[Path, int | None]], int]:
    """Configured sweep sources + default age gate. Empty list = disabled.

    Each $BRAIN_WORKSPACE_SWEEP_DIRS entry is ``path`` or ``path=N`` — the
    per-dir age override (2026-07-11, round-5 benchmark): a CAPTURE folder
    (an inbox / a meetings drop folder) holds FINAL documents that
    settle in a day, while a WORKING folder needs the long gate so
    in-progress files are never swept. One global age starved the capture
    folders by a week; ``path=1`` fixes that per source."""

    raw = os.environ.get(WORKSPACE_SWEEP_DIRS_ENV, "").strip()
    dirs: list[tuple[Path, int | None]] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path_part, sep, age_part = entry.rpartition("=")
        if sep and age_part.isdigit():
            # age 0 is legal: same-day capture sweep (a 15-minute
            # write-settle guard still applies inside sweep_workspace).
            dirs.append((Path(path_part).expanduser(), int(age_part)))
        else:
            dirs.append((Path(entry).expanduser(), None))
    try:
        age = int(os.environ.get(WORKSPACE_SWEEP_AGE_ENV, ""))
    except ValueError:
        age = WORKSPACE_SWEEP_DEFAULT_AGE_DAYS
    if age < 1:
        age = WORKSPACE_SWEEP_DEFAULT_AGE_DAYS
    return dirs, age


def _open_dir_nofollow(d: Path) -> int | None:
    """Pin ``d`` as an open directory handle, refusing a symlink as its last
    component (ELOOP/ENOTDIR). The sweep source sits in a folder the untrusted
    Cowork VM can write, so it can swap ``d`` for a link to any owner folder at
    any time (F11); every list/stat/move below goes through this handle, so a
    swap AFTER the open cannot redirect it. ``None`` where the OS has no
    directory handles (Windows): the caller's ``is_symlink`` check carries it."""
    if not hasattr(os, "O_DIRECTORY"):
        return None
    return os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


class _InboxRefused(Exception):
    """The inbox, or the vault dir above it, is a symlink or was just swapped for one (F201)."""


class _PinnedInbox:
    """The sweep destination, opened as a directory handle at the first real move.

    The Cowork VM writes the shared mount, so it can swap ``<vault>/inbox`` (or
    the vault dir) for a link to any owner folder. The vault dir is opened
    ``O_NOFOLLOW`` and the inbox relative to it, so a link swapped in after the
    lstat check is refused by the kernel (``_InboxRefused``), never followed; the
    name choice, the rename and the copy fallback all go through the handle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def pin(self) -> int:
        if self.fd is not None:
            return self.fd
        self.path.parent.mkdir(parents=True, exist_ok=True)  # mkdir never follows a link
        vfd = self._open(self.path.parent)
        try:
            try:
                os.mkdir(self.path.name, dir_fd=vfd)
            except FileExistsError:
                pass
            ifd = self._open(self.path.name, vfd)
        finally:
            os.close(vfd)
        self.fd = ifd
        return ifd

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @staticmethod
    def _open(name: Path | str, dir_fd: int | None = None) -> int:
        try:
            return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _InboxRefused(str(name)) from exc
            raise


def _unique_name_at(ifd: int, name: str) -> str:
    """First free name in the pinned inbox (``stem.N.suffix``, as ``unique_destination``);
    a planted link, even a dangling one, counts as taken."""
    stem, suffix, cand, i = Path(name).stem, Path(name).suffix, name, 0
    while True:
        try:
            os.stat(cand, dir_fd=ifd, follow_symlinks=False)
        except FileNotFoundError:
            return cand
        i += 1
        cand = f"{stem}.{i}{suffix}"


def _copy_at(dfd: int, name: str, ifd: int, dst: str) -> None:
    """Copy ``name`` out of ``dfd`` into the pinned inbox ``ifd`` as a NEW file."""
    # O_NONBLOCK: a FIFO swapped in for the file must not block the open.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    with os.fdopen(fd, "rb") as src:
        sst = os.fstat(fd)
        if not stat.S_ISREG(sst.st_mode):
            raise shutil.SpecialFileError(f"`{name}` is not a regular file")
        # The source's own mode (umask can only narrow it): a 0600 file
        # must not turn group/world-readable inside the inbox.
        ofd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      stat.S_IMODE(sst.st_mode), dir_fd=ifd)
        try:
            with os.fdopen(ofd, "wb") as out:
                shutil.copyfileobj(src, out)
                out.flush()
                os.utime(ofd, ns=(sst.st_atime_ns, sst.st_mtime_ns))  # the inode, not a link
        except BaseException:
            try:  # never leave a truncated file for the ingest drain
                os.unlink(dst, dir_fd=ifd)
            except FileNotFoundError:
                pass
            raise


def _move_at(dfd: int, name: str, ifd: int, dst: str) -> None:
    """Move ``name`` out of the pinned directory ``dfd`` to ``dst`` in the pinned
    inbox ``ifd``; a symlink swapped in for the file moves AS a link and is never
    read through."""
    try:
        os.rename(name, dst, src_dir_fd=dfd, dst_dir_fd=ifd)
    except OSError:  # cross-device: copy through no-follow handles, then unlink
        _copy_at(dfd, name, ifd, dst)
        os.unlink(name, dir_fd=dfd)


def _sweep_dir(d: Path, dfd: int | None, inbox: _PinnedInbox, cutoff: float,
               dry_run: bool, report: dict[str, Any]) -> None:
    """Sweep the settled top-level files of ``d`` (open as ``dfd``) into ``inbox``."""
    from .ingest.pipeline import _move, _unique_dest

    from .ingest.handlers import handler_for

    for name in sorted(os.listdir(d if dfd is None else dfd)):
        p = d / name
        try:  # lstat: a symlink file is not a regular file, so it is skipped
            st = os.stat(p if dfd is None else name, dir_fd=dfd,
                         follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or name.startswith("."):
            continue
        if handler_for(p) is None:
            # Machine artifacts (.py, .json, .tsv, …) have no ingest
            # handler — sweeping them only detours through quarantine
            # (measured: 344 on the first real sweep). Leave them where
            # they live; the count keeps the skip honest.
            report["skipped_unsupported"] += 1
            continue
        if st.st_mtime > cutoff:
            report["skipped_active"] += 1
            continue
        if dry_run:
            report["swept"].append({"file": str(p), "would_move": True})
            continue
        try:
            if dfd is None:
                inbox.path.mkdir(parents=True, exist_ok=True)
                _move(p, _unique_dest(inbox.path, name))
            else:
                ifd = inbox.pin()
                _move_at(dfd, name, ifd, _unique_name_at(ifd, name))
            report["swept"].append({"file": str(p)})
        except OSError as exc:
            report["errors"].append({"file": str(p), "error": str(exc)})


def sweep_workspace(
    dirs: list[Path] | list[tuple[Path, int | None]], inbox: Path, age_days: int,
    now: float | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Move settled top-level files from ``dirs`` into ``inbox``.

    ``dirs`` entries are ``Path`` (use the global ``age_days``) or
    ``(Path, age)`` tuples (per-dir override; ``None`` = global). Pure file
    motion — classification/signing/dedup all happen downstream in the
    ingest drain. Collisions uniquify (never clobber an inbox file).
    A source that is a symlink is refused (``refused_dirs``), never followed;
    so is an inbox (or vault dir) that is one (``refused_inbox``).
    Returns an honest report; a missing dir is reported, never raised."""

    base_now = now if now is not None else time.time()
    report: dict[str, Any] = {
        "swept": [], "skipped_active": 0, "skipped_unsupported": 0,
        "missing_dirs": [], "refused_dirs": [], "refused_inbox": [],
        "errors": [], "age_days": age_days, "dry_run": dry_run,
    }
    if inbox.is_symlink() or inbox.parent.is_symlink():  # F201: never move through it
        report["refused_inbox"].append(str(inbox))
        return report
    dest = _PinnedInbox(inbox)
    for entry in dirs:
        d, dir_age = entry if isinstance(entry, tuple) else (entry, None)
        eff_age = dir_age if dir_age is not None else age_days
        # age 0 = capture-inbox mode: sweep same-day, but never a file
        # younger than 15 minutes (write-settle guard against partial copies).
        cutoff = base_now - (900.0 if eff_age == 0 else eff_age * 86400.0)
        if d.is_symlink():  # F11: refused, never followed; reported by the fold
            report["refused_dirs"].append(str(d))
            continue
        if not d.is_dir():
            report["missing_dirs"].append(str(d))
            continue
        try:
            dfd = _open_dir_nofollow(d)
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            report["refused_dirs"].append(str(d))  # swapped for a link just now
            continue
        try:
            _sweep_dir(d, dfd, dest, cutoff, dry_run, report)
        except _InboxRefused:  # swapped for a link after the check above
            report["refused_inbox"].append(str(inbox))
            break
        finally:
            if dfd is not None:
                os.close(dfd)
            dest.close()
    return report

# Cross-section binds, deferred past this module's own defs.
