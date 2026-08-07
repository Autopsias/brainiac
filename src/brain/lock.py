"""Single-writer advisory lockfile between the scheduled job and ad-hoc CLI
writes (CC-02).

Mechanism = ``fcntl.flock`` on a sidecar ``writer.lock`` file, held for the
holder's process lifetime -- deliberately NOT a pidfile ("write pid,
kill(pid, 0), break if dead"). PID reuse makes a live unrelated process
read as "holder alive", and check-then-unlink is itself racy. flock is
released by the KERNEL on crash/SIGKILL/process exit, so there is no
stale-lock heuristic to get wrong. The pid written into the file is
metadata for the error message ONLY -- never the liveness authority.

Re-entrant WITHIN one process (``sync`` self-delegates to ``rebuild``; both
must share one lock, not deadlock on it) via a module-level depth counter
keyed by the resolved lock path -- safe because this process is
single-threaded for write verbs (CLI-invoked, one command per process).

Read paths and the VM leg NEVER call this -- they must create no lock file
at all (AGENTS.md: the VM never writes the index).

Windows has no ``flock``. The equivalent is ``msvcrt.locking``, which takes a
MANDATORY byte-range lock rather than an advisory whole-file one -- so it must
target a sentinel byte far past the holder JSON at offset 0, or the kernel
would block ``current_holder`` from ever reading the metadata back. Same
kernel-releases-on-crash property, so the no-stale-heuristic reasoning above
holds on both platforms.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Iterator

# The byte msvcrt locks on Windows. Must sit beyond any holder-metadata write
# (a ~100-byte JSON object at offset 0) because that lock is mandatory, not
# advisory. Locking past EOF is legal and does not extend the file.
_SENTINEL_OFFSET = 1 << 20


class WriterLockBusy(RuntimeError):
    """Raised when the writer lock could not be acquired within the bound.

    ``holder`` is a BEST-EFFORT read of the lockfile's metadata (finding 2,
    2026-07-20 dedup batch) -- it names the pid/verb that most recently wrote
    the file, never a liveness-verified fact. The metadata write itself only
    ever happens AFTER this process's own ``flock`` succeeds (see
    ``writer_lock`` below), so by construction it reflects the CURRENT holder
    at write time; the caveat below exists because reading it is still an
    unsynchronized file read racing a live writer, not because the write
    ordering is wrong.
    """

    def __init__(self, holder: dict[str, Any], verb: str):
        self.holder = holder
        self.verb = verb
        pid = holder.get("pid")
        held_verb = holder.get("verb")
        super().__init__(
            f"writer lock busy: reportedly held by pid={pid} (verb={held_verb}) "
            f"-- could not acquire for {verb!r} within the bound "
            "(holder read best-effort from lockfile metadata; stale metadata possible)"
        )


# path (str) -> depth, for this process only. Re-entrancy is process-local;
# flock itself is process-scoped (not thread-scoped), which matches the
# single-threaded-per-write-verb assumption documented above.
_DEPTH: dict[str, int] = {}
_FD: dict[str, int] = {}


def _read_holder(lock_path: Path) -> dict[str, Any]:
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _open_lock_fd(lock_path: Path) -> int:
    """Open the lockfile read/write, never inheritable, never through a symlink.

    An inherited fd would keep the lock alive after the process that acquired
    it exits -- the exact leaked-lock failure mode this module exists to avoid.
    POSIX spells that ``O_CLOEXEC``; Windows spells it ``O_NOINHERIT`` and also
    needs ``O_BINARY`` so the holder JSON is written without newline
    translation.

    ``O_NOFOLLOW`` because the caller ``ftruncate``s this descriptor to write
    the holder JSON: a symlink planted at the lock name would hand that
    truncation to whatever it points at (the sqlite index, say). Both lock
    files this opens now live off the mount, so nothing should be able to plant
    one -- which is exactly why the primitive itself should not be the weak
    link if that ever stops being true. Windows has no ``O_NOFOLLOW``; there
    the flag resolves to 0 and the location is the whole defence.
    """
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    for name in ("O_CLOEXEC", "O_NOINHERIT", "O_BINARY"):
        flags |= getattr(os, name, 0)
    return os.open(str(lock_path), flags, 0o600)


def _try_lock(fd: int) -> bool:
    """Non-blocking exclusive lock. True on success, False if held elsewhere."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, _SENTINEL_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _SENTINEL_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def vault_writer_lock(vault: Any, *, verb: str,
                      timeout: float | None = None) -> Iterator[None]:
    """The single-writer lock for ``vault``, off the mount (INT-05).

    Every index-mutating verb goes through here, and there is exactly ONE lock:
    ``config.writer_lock_path``. A real host runs two engines (the launchd job
    on a ``BRAIN_BIN``-pinned build, a hand-run ``brain`` off PATH), so they
    only exclude each other once BOTH resolve this path — which makes restaging
    the pinned engine an ordered release step, not something the code can
    absorb. A second lock at the old ``<vault>/.brain/writer.lock`` was tried
    and removed: a lock on the VM-writable mount cannot provide exclusion at
    all (unlink the inode, drop a replacement, and the next holder flocks a
    different file), so it bought nothing while exposing this module's
    ``ftruncate`` to a planted symlink. See the INT-05 appendix in
    ``docs/release-runbook.md`` for the ordering it was standing in for.
    """
    from . import config

    # Acquisition-time diagnosis (INT-05 round 3): if $BRAIN_INDEX_DIR is on the
    # mount, this lock lives in app-data while the INDEX it protects does not.
    config.warn_if_lock_dir_fallback(vault)
    with writer_lock(config.writer_lock_path(vault), verb=verb, timeout=timeout):
        yield


@contextlib.contextmanager
def writer_lock(lock_path: Path, *, verb: str,
                timeout: float | None = None) -> Iterator[None]:
    """Acquire the single-writer lock for ``verb``, re-entrantly.

    ``timeout`` bounds the wait (default ``$BRAIN_WRITER_LOCK_SECONDS`` or
    30s; tests use a short override). On timeout, raises ``WriterLockBusy``
    naming the current holder (pid + verb) read from the lock file.
    """
    key = str(lock_path)
    depth = _DEPTH.get(key, 0)
    if depth > 0:
        # Already held by this process (re-entrant call, e.g. sync -> rebuild).
        _DEPTH[key] = depth + 1
        try:
            yield
        finally:
            _DEPTH[key] = _DEPTH[key] - 1
        return

    bound = timeout if timeout is not None else float(
        os.environ.get("BRAIN_WRITER_LOCK_SECONDS", "30")
    )
    # Creation happens HERE, at acquisition — never in the name-resolution
    # helper that every index-mutating verb (and the read-side liveness probe)
    # calls just to learn the path.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError, ImportError):
        from .config import secure_file_permissions

        secure_file_permissions(lock_path.parent, 0o700)

    fd = _open_lock_fd(lock_path)
    start = time.monotonic()
    acquired = False
    delay = 0.05
    try:
        while True:
            if _try_lock(fd):
                acquired = True
                break
            elapsed = time.monotonic() - start
            if elapsed >= bound:
                holder = _read_holder(lock_path)
                raise WriterLockBusy(holder, verb)
            time.sleep(min(delay, max(0.0, bound - elapsed)) * (0.5 + random.random()))
            delay = min(delay * 2, 2.0)

        # Seek-then-write rather than os.pwrite (absent on Windows). Offset 0
        # is deliberately OUTSIDE the Windows sentinel lock range so
        # `current_holder` can still read this back while the lock is held.
        info = {"pid": os.getpid(), "verb": verb, "started": time.time()}
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info).encode("utf-8"))
        _FD[key] = fd
        _DEPTH[key] = 1
        try:
            yield
        finally:
            _DEPTH[key] = 0
            del _FD[key]
    finally:
        if acquired:
            _unlock(fd)
        os.close(fd)


def current_holder(lock_path: Path) -> dict[str, Any]:
    """Best-effort read of who holds (or last held) the lock -- for status/
    error messages only, never as a liveness authority."""
    return _read_holder(lock_path)
