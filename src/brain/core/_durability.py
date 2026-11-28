"""Durable file primitives for the supersession transaction facade."""
from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

from ..lock import _atomic_temp_path

def _fsync_dir_strict(d: Path) -> None:
    """fsync a directory ENTRY and RAISE if it fails.

    ``cos._fsync_dir`` swallows every ``OSError`` from both the open and the
    fsync. That is the right call for a best-effort flush, and the wrong one
    to build a durability DECISION on: ``supersede`` unlinks its crash journal
    on the strength of "both notes are on disk", and a silently-failed fsync
    means it isn't. One extra directory fsync is microseconds; a silent one is
    a lost rollback record (adversarial review round 3, 2026-08-10)."""
    if os.name == "nt":
        return          # Windows has no directory descriptors to sync
    dfd = os.open(d, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)

def _write_atomic_durable(
    path: Path, data: bytes, *, mode: int, dir_fd: int | None = None
) -> None:
    """Replace ``path`` with ``data`` atomically and DURABLY, or raise.

    Same shape as ``cos._write_atomic`` — unpredictable temp name,
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` (a pre-created symlink at the temp name is
    how a predictable ``<target>.tmp`` gets an attacker's file overwritten),
    regular-file check, short-write loop, ``fsync``, ``os.replace``, parent
    fsync. It is a SEPARATE function on purpose, and the reason is layering,
    not preference: a vault note write must not route through a
    chief-of-staff helper. Pointing ``write_note`` at ``cos._write_atomic``
    made every note write intercept a COS *test double*
    (``test_cos_approved_queue.py`` monkeypatches that symbol to inject a
    staging crash), so an unrelated COS test started crashing the drain. A
    shared primitive whose substitutions are scoped to one subsystem is not
    actually shared.

    Unlike ``cos._fsync_dir``, the parent fsync RAISES on failure —
    ``supersede`` unlinks its crash journal on the strength of "both notes are
    on disk", and a silently-failed fsync means they are not.

    ``dir_fd``: an open handle on ``path.parent`` (see ``_open_dir_pinned``).
    The temp create, the replace, the cleanup and the directory fsync then all
    go through THAT handle by bare name, so a parent swapped for a link after
    the caller's containment check cannot redirect the write (F72). ``None``
    (the default) keeps the path-based calls unchanged."""
    import stat as _stat

    tmp = _atomic_temp_path(path)
    pinned = dir_fd is not None
    ref = tmp.name if pinned else tmp
    kw = {"dir_fd": dir_fd} if pinned else {}
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(ref, flags, 0o600, **kw)
    closed = False
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"refusing to write {tmp.name}: not a regular file")
        view = memoryview(data)
        while view:                       # os.write may write only part of it
            n = os.write(fd, view)
            if n <= 0:
                raise OSError(f"write made no progress on {tmp.name} "
                              f"({len(view)} bytes left)")
            view = view[n:]
        os.fsync(fd)
        if mode != 0o600:
            os.fchmod(fd, mode)           # on the FD — the name is never re-resolved
        os.close(fd)
        closed = True
        if pinned:   # the leaf is replaced, never followed, as with os.replace(tmp, path)
            os.replace(ref, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.replace(tmp, path)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if pinned:
                os.unlink(ref, dir_fd=dir_fd)
            else:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if dir_fd is None:
        _fsync_dir_strict(path.parent)
    else:
        os.fsync(dir_fd)                  # strict, like _fsync_dir_strict

# Pinning needs directory handles and dir_fd on open/rename (not Windows).
# ``os.replace`` is absent from ``supports_dir_fd`` but takes src/dst_dir_fd
# wherever ``os.rename`` does.
_CAN_PIN_DIR = (hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
                and os.rename in os.supports_dir_fd)

# Search-only directory handle (``x`` without ``r``): O_SEARCH (macOS, BSDs) or
# Linux O_PATH; ``None`` where neither exists. Used for ANCESTORS only, and only
# after a plain O_RDONLY open was refused, so a readable path opens exactly as
# before on every platform. Safe on Linux because every open also carries
# O_DIRECTORY|O_NOFOLLOW: O_PATH|O_NOFOLLOW alone would hand back an fd for a
# symlink itself, but with O_DIRECTORY a link fails ENOTDIR (the swap signal).
_SEARCH_ONLY = getattr(os, "O_SEARCH", getattr(os, "O_PATH", None))

def _open_dir_at(name: str, dir_fd: int | None, ancestor: bool) -> int:
    flags = os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, os.O_RDONLY | flags, dir_fd=dir_fd)
    except PermissionError:
        # A plain path walk needs only search permission on an ancestor (a
        # root-owned 0711 /data); the FINAL directory is fsynced, so it keeps
        # O_RDONLY and its refusal, as before.
        if not (ancestor and _SEARCH_ONLY is not None):
            raise
        return os.open(name, _SEARCH_ONLY | flags, dir_fd=dir_fd)

def _open_dir_pinned(d: Path) -> int:
    """Open the ABSOLUTE, already-resolved directory ``d`` as an fd, walking
    from ``/`` one component at a time with ``O_NOFOLLOW``.

    ``write_note`` resolves its target before signing, so no component of ``d``
    is a link; this is the same statement made race-free. A component swapped
    for a link afterwards fails here (ELOOP, or ENOTDIR on macOS/Linux) instead
    of carrying a signed write elsewhere (F72)."""
    parts = d.parts
    fd = _open_dir_at(parts[0], None, len(parts) > 1)
    try:
        for i in range(1, len(parts)):
            nxt = _open_dir_at(parts[i], fd, i < len(parts) - 1)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd

def _write_note_durable(target: Path, content: str) -> None:
    """Atomically replace ``target`` with ``content``, durably.

    ``0o644``: a vault note is ordinary readable content, not host-private
    state (the 0o600 default belongs to the COS queues and the crash journal).

    ``target`` is expected to be an absolute, RESOLVED path (``write_note``
    resolves it before signing); the write is then pinned to its parent
    directory handle so nothing re-resolves the path after the containment
    check (F72). A relative path, or a platform without directory handles,
    writes by path as before."""
    data = content.encode("utf-8")
    if not (_CAN_PIN_DIR and target.is_absolute()):
        return _write_atomic_durable(target, data, mode=0o644)
    try:
        dfd = _open_dir_pinned(target.parent)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"write target moved during signing: {target.name}") from exc
        raise
    try:
        _write_atomic_durable(target, data, mode=0o644, dir_fd=dfd)
    finally:
        os.close(dfd)

class SupersedeNotDurable(RuntimeError):
    """This platform cannot prove the crash journal reached the disk, so the
    supersession is refused before anything is signed.

    The journal is the ONLY record of both notes' pre-transaction bytes, and
    ``supersede`` writes two signed notes on the strength of it. Where the
    write cannot be shown durable, the failure mode is a signed half-chain
    with no rollback record — strictly worse than not superseding at all
    (adversarial review round 4, 2026-08-10). Windows is the case that trips
    it: no directory descriptor to fsync, and CPython's ``os.replace`` passes
    ``MOVEFILE_REPLACE_EXISTING`` without ``MOVEFILE_WRITE_THROUGH``, so the
    move is not guaranteed to reach disk before it returns. Refusing by name
    is the fallback the review named; a durable Windows replace is a real fix
    that needs a Windows host to test, and this refusal is what says so out
    loud instead of pretending the guarantee holds.
    """

def _require_durable_replace(what: str) -> None:
    """Raise :class:`SupersedeNotDurable` unless an atomic replace on this
    platform can be PROVEN to have reached the disk."""
    if os.name == "nt":
        raise SupersedeNotDurable(
            f"refusing to {what}: durability cannot be established on this "
            f"platform (os.name={os.name!r}). Windows has no directory fsync "
            f"and CPython's os.replace is not write-through, so a power loss "
            f"can lose the crash journal that a signed half-chain would be "
            f"rolled back from. Nothing was written.")

def _mkdir_durable(
    d: Path, *, fsync_dir: Callable[[Path], None] = _fsync_dir_strict
) -> None:
    """``mkdir(parents=True, exist_ok=True)``, with every directory entry it
    actually CREATES fsynced into its own parent.

    ``_write_atomic_durable`` fsyncs the file and the directory it lands in,
    which is enough only when that directory already existed. On the FIRST
    transaction after the journal store is created, the directory entry itself
    is not durably anchored, so a power loss can take the whole journal with
    it (adversarial review round 4). Fsyncing the ancestry costs microseconds
    once."""
    created: list[Path] = []
    p = d
    while not p.exists():
        created.append(p)
        if p.parent == p:
            break
        p = p.parent
    d.mkdir(parents=True, exist_ok=True)
    for made in reversed(created):        # shallowest first
        fsync_dir(made.parent)
