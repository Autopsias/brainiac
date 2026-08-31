"""One spelling for a path, and one narrow lock per file.

A LEAF module: stdlib only, imports nothing from ``brain`` at module level.
It exists because ``workspaces``, ``provision`` and ``provision_wire`` all
need the same two primitives, and the previous arrangement --- each reaching
into the other through function-local imports --- made the "who owns this"
question unanswerable and the import order load-bearing.

**Path identity.** macOS ``readdir`` returns NFD where a JSON config stores
NFC, so one accented path in two spellings compares unequal after
``realpath`` alone: a check-then-act wire acts twice and the registry grows a
duplicate row for a vault it already holds. Every comparison and every stored
path goes through :func:`real_key`.
"""
from __future__ import annotations

import hashlib
import os
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — Windows has no flock
    _fcntl = None  # type: ignore[assignment]

# How long the lockfile fallback waits for a holder to finish. Generous: the
# critical sections it guards are one JSON or plist read-modify-write.
_LOCK_TIMEOUT_S = 60.0
_LOCK_POLL_S = 0.02


def nfc(value: Any) -> str:
    """NFC text — the one Unicode spelling this codebase compares."""
    return unicodedata.normalize("NFC", str(value))


def real_key(path: Any) -> str:
    """The canonical comparison/storage form of a path: ``~`` expanded,
    symlinks resolved, NFC-normalised. THE key — ``workspaces._key``,
    ``provision._registered`` and the wire lookups all use this one, so a row
    written by any lane is found by every other.

    KNOWN LIMIT, accepted (2026-08-30 review): the NFC step is unconditional,
    so on a filesystem that does NOT fold normalisation (ext4, NTFS) two
    genuinely distinct paths differing only in Unicode normalisation would key
    the same. Reaching it takes both spellings existing as separate directories
    on one machine; making it platform-conditional would reopen the NFD/NFC
    miss this exists to close, on the platform that actually has it.
    """
    return nfc(os.path.realpath(os.path.expanduser(str(path))))


def same_path(a: Any, b: Any) -> bool:
    """Do two path spellings name the same place? An EMPTY side never does.

    ``os.path.realpath("")`` is the process CWD, so ``same_path("", wanted)``
    answered True for every caller that happened to be sitting in ``wanted`` ---
    which let wire 5 certify a Claude Desktop entry carrying no ``BRAIN_VAULT``
    at all, and would have let a blank registry ``snapshot_dir`` read as
    current. Absence is not a path; it compares equal to nothing, itself
    included.
    """
    if not str(a or "").strip() or not str(b or "").strip():
        return False
    return real_key(a) == real_key(b)


def canonical(path: Any) -> Path:
    """:func:`real_key` as a ``Path``."""
    return Path(real_key(path))


@contextmanager
def exclusive_create_lock(lock: Path, *, timeout: float = _LOCK_TIMEOUT_S
                          ) -> Iterator[None]:
    """A lock made of the lockFILE itself — the portable fallback for platforms
    with no ``flock``.

    ``O_CREAT|O_EXCL`` is atomic on every filesystem this ships to, so the
    process that creates the file holds the lock and everyone else spins until
    it is unlinked. Nothing here reaches for ``msvcrt.locking``: this repo has
    no Windows CI signal, and an unverified API is not a lock.

    ponytail: a holder that is SIGKILLed leaves the file behind and the next
    caller waits out ``timeout`` and then raises, naming the file to delete —
    deliberately louder than silently stealing a lock somebody may still hold.
    If stale locks ever become routine, write the pid into the file and reap on
    a dead one.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"waited {timeout:g}s for the lock at {lock}. If no other "
                    f"`brain` is running, delete that file.") from None
            time.sleep(_LOCK_POLL_S)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:  # pragma: no cover — already gone
            pass


@contextmanager
def file_lock(vault: Any, target: Path) -> Iterator[None]:
    """Exclusive lock around ONE file's read-modify-write. The Claude Desktop
    config and the launchd plist had no lock of their own, so two provisions
    landing together kept whichever write finished last. The lockfile lives in
    the host-private lock dir (off any Cowork mount), keyed on the target.

    Windows has no ``flock`` and this used to ``yield`` there — an unlocked
    section wearing a lock's name, on a platform that has Claude Desktop and
    runs ``provision-local``: two concurrent provisions both read the old
    ``mcpServers`` map and only the last entry survived. The fallback above is
    a real lock.
    """
    from .config_hostpaths import host_lock_dir

    key = hashlib.sha256(nfc(target).encode("utf-8")).hexdigest()[:12]
    lock = host_lock_dir(vault, create=True) / f"provision-{key}.lock"
    if _fcntl is None:
        with exclusive_create_lock(lock):
            yield
        return
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)
