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
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Iterator


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


@contextlib.contextmanager
def writer_lock(lock_path: Path, *, verb: str, timeout: float | None = None) -> Iterator[None]:
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
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    import fcntl  # POSIX only (macOS/Linux host + Cowork Linux VM) -- see AGENTS.md host/VM split

    # O_CLOEXEC: a child process (e.g. an embedding subprocess launchd ->
    # brain -> embedder) must NEVER inherit this fd. flock binds to the open
    # file description, so an inherited fd would keep the lock alive after
    # the parent that acquired it exits -- reproducing the exact
    # leaked-lock / silent-32-nights failure mode this session exists to fix.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    start = time.monotonic()
    acquired = False
    delay = 0.05
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                elapsed = time.monotonic() - start
                if elapsed >= bound:
                    holder = _read_holder(lock_path)
                    raise WriterLockBusy(holder, verb)
                time.sleep(min(delay, max(0.0, bound - elapsed)) * (0.5 + random.random()))
                delay = min(delay * 2, 2.0)

        info = {"pid": os.getpid(), "verb": verb, "started": time.time()}
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(info).encode("utf-8"), 0)
        _FD[key] = fd
        _DEPTH[key] = 1
        try:
            yield
        finally:
            _DEPTH[key] = 0
            del _FD[key]
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def current_holder(lock_path: Path) -> dict[str, Any]:
    """Best-effort read of who holds (or last held) the lock -- for status/
    error messages only, never as a liveness authority."""
    return _read_holder(lock_path)
