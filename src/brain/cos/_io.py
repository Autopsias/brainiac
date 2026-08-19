"""COS durable I/O operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403

def _reserve_exclusive(path: Path, *, mode: int = MODE_HOST_PRIVATE) -> None:
    """Claim ``path`` for this process, or raise ``FileExistsError``. NO content.

    THE THIRD SANCTIONED RAW WRITER, and it is deliberately the smallest one
    that can exist: create-if-absent, nothing written, nothing truncated.
    `_write_atomic` cannot do this job — it publishes through a rename, which
    REPLACES whatever is there, so two racing callers both "succeed" and the
    loser silently overwrites the winner. Reserving a name needs the one
    operation the filesystem serialises, and that is `O_CREAT|O_EXCL`.

    `O_NOFOLLOW` for the same reason every other opener in this module carries
    it (INT-05): these paths are mount-resident, and a pre-created symlink at
    the name would otherwise be followed. `O_EXCL|O_CREAT` already refuses a
    symlink — even a dangling one — so this is belt and braces on a path where
    the cost of being wrong is a host process writing through an attacker's
    link. It is `getattr`-ed (like every OTHER opener here — `_write_atomic`
    below) because Windows is a supported host and has no `O_NOFOLLOW`; a bare
    `os.O_NOFOLLOW` raised `AttributeError` there before a manifest was ever
    written, and `O_EXCL|O_CREAT` still refuses an existing symlink at the name
    so the fallback loses nothing.

    Sole caller: `write_run_manifest`, reserving the run id — allocated OR
    explicit, since round 6's H-resv — before it writes the manifest into the
    same name (C-lock, 2026-08-13). Listed in
    `tests/test_cos_pathguard.py::_SANCTIONED_RAW_WRITERS` — that guard exists
    to force this paragraph to be written, not to be edited around.
    """
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                     | getattr(os, "O_NOFOLLOW", 0), mode))

def _write_atomic(path: Path, data: bytes, *, mode: int = MODE_HOST_PRIVATE) -> None:
    """Durable, atomic, and never through a symlink — including the TEMP name.

    The temp name used to be the predictable ``<target>.tmp`` opened with a
    plain ``open(..., "wb")``, which FOLLOWS a symlink. Round 2 pointed this
    helper at ``batches.jsonl`` under ``host/proposals/`` — on the shared mount
    — so pre-creating ``batches.jsonl.tmp`` as a link to any host file made the
    host truncate and overwrite that file. Unpredictable name +
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` + a regular-file check closes it: EXCL means
    an existing entry (symlink or not) fails outright, and the random suffix
    means an attacker cannot pre-create the name to begin with.

    The PARENT is fsynced after the rename too: the caller unlinks the only
    other copy (the pending source) straight after, and an unsynced directory
    entry can lose the rename in a crash — durable file contents under a lost
    name is still a lost file."""
    import secrets
    import stat as _stat

    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(tmp, flags, 0o600)   # narrow while it is being written
    closed = False
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"refusing to write {tmp.name}: not a regular file")
        # os.write is NOT guaranteed to write everything it is given. A short
        # write used to be treated as success and then atomically PUBLISHED —
        # and for approved staging the caller deletes the pending copy straight
        # after, so the accepted content would be gone and the truncated queue
        # copy correctly refused by its own anchor. Loop until it is all out.
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                # A zero-progress write leaves `view` unchanged: the loop would
                # spin forever with the temp fd open and the drain hung. Empty
                # input never enters the loop (the memoryview is falsy).
                raise OSError(f"write made no progress on {tmp.name} "
                              f"({len(view)} bytes left)")
            view = view[n:]
        os.fsync(fd)
        if mode != 0o600:
            os.fchmod(fd, mode)     # on the FD, so no name is ever re-resolved
        os.close(fd)
        closed = True
        os.replace(tmp, path)
    except BaseException:
        # EVERY post-open step, close and replace included: a failure in either
        # used to skip cleanup and strand the temp file.
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
    _fsync_dir(path.parent)

def _fsync_dir(d: Path) -> None:
    """Make a directory ENTRY durable (a rename/unlink, not file contents)."""
    try:
        dfd = os.open(d, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return          # Windows has no directory fd; nothing to sync
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)

def _read_nofollow(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read a regular file WITHOUT following a symlink at the final component.

    A swapped-in symlink is the classic way to make a verified path serve
    someone else's bytes; ``O_NOFOLLOW`` refuses it at the syscall. Windows has
    no such flag, so the explicit ``is_symlink`` check carries that platform.

    ``max_bytes`` caps the read on the OPEN DESCRIPTOR (``ApprovedTooLarge``
    past the cap) rather than on a ``stat()`` of the name — INT-04 round 3:
    a caller that stats the path, then opens it again, has re-resolved the
    name twice and can be given two different objects. One open, one buffer,
    every decision made about THAT descriptor."""
    import stat as _stat

    if path.is_symlink():
        raise ApprovedRefused(f"{path.name} is a symlink — refused")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ApprovedRefused(f"{path.name} unreadable: {type(exc).__name__}") from None
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise ApprovedRefused(f"{path.name} is not a regular file — refused")
        chunks: list[bytes] = []
        total = 0
        while True:
            b = os.read(fd, 1 << 16)
            if not b:
                return b"".join(chunks)
            total += len(b)
            if max_bytes is not None and total > max_bytes:
                raise ApprovedTooLarge(
                    f"{path.name} exceeds the {max_bytes}-byte cap")
            chunks.append(b)
    finally:
        os.close(fd)

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out

def _open_append_nofollow(path: Path) -> int:
    """Open ``path`` for append, PROVABLY not through a symlink — or raise.

    Two shapes, and neither has a check-then-open window:

    1. **The ledger does not exist yet: create it EXCLUSIVELY.**
       ``O_CREAT | O_EXCL`` fails outright if anything is already at the name,
       symlink included — the kernel decides, atomically. The previous version
       lstat'd first and, on ``FileNotFoundError``, opened with ``O_CREAT`` and
       skipped the identity comparison entirely, so a symlink inserted between
       the two calls WAS followed: ``fstat`` then saw a perfectly ordinary
       victim file and ledger data was appended outside the ledger. Removing
       the window beats narrowing it.
    2. **It already exists: open it WITHOUT ``O_CREAT``, and verify — always.**
       ``lstat`` the name, refuse a symlink, then confirm on the fd that the
       object is a REGULAR FILE and the very same inode. ``O_NOFOLLOW`` is used
       where it exists, but it is belt, not the check: it refuses a symlink and
       tells you nothing about which file you got, so the branch that had it
       used to return straight from ``os.open`` and accepted a swapped-in
       regular file — the redirection the other branch refused. One path now,
       and no ``0`` fallback: a guard that degrades to nothing while still
       reporting success is the failure mode this work exists to remove.

    A file that is created and then deleted under us bounces between the two
    branches, so the pair is retried a few times before giving up."""
    base = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # A non-regular object must not be able to BLOCK the open: a planted FIFO
    # made `os.open` wait for a reader, which sails straight past the append
    # lock's timeout and wedges the unattended nightly drain. With O_NONBLOCK a
    # writer-side FIFO open fails fast (ENXIO) instead, and the flag is ignored
    # for the regular files this is actually meant to open.
    nonblock = getattr(os, "O_NONBLOCK", 0)
    for _attempt in range(5):
        try:
            # (1) atomic create — never follows a link, no window to race
            return os.open(path, base | os.O_CREAT | os.O_EXCL | nofollow,
                           MODE_HOST_PRIVATE)
        except FileExistsError:
            pass
        # (2) it exists: ONE verification path, whether or not the kernel
        #     helped. `O_NOFOLLOW` refuses a symlink; it says nothing about
        #     WHICH file you got or whether it is a regular file at all, so the
        #     branch that had it used to return straight from `os.open` and
        #     accepted a swapped-in regular file — the very redirection the
        #     other branch refused.
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue                    # deleted under us — retry the create
        if _stat_mod.S_ISLNK(st.st_mode):
            raise OSError(f"refusing to append to {path.name}: it is a symlink")
        before = (st.st_dev, st.st_ino)
        try:
            fd = os.open(path, base | nofollow | nonblock, MODE_HOST_PRIVATE)
        except FileNotFoundError:
            continue
        try:
            after = os.fstat(fd)
            if not _stat_mod.S_ISREG(after.st_mode):
                raise OSError(
                    f"refusing to append to {path.name}: not a regular file")
            if (after.st_dev, after.st_ino) != before:
                raise OSError(
                    f"refusing to append to {path.name}: it was replaced "
                    f"between the check and the open")
        except BaseException:
            os.close(fd)
            raise
        return fd
    raise OSError(f"refusing to append to {path.name}: it kept appearing and "
                  f"disappearing under us")

def _append_lock_path(ledger: Path, vault: Any = None) -> Path:
    """The off-mount lock file serializing appends to ``ledger``.

    HOST-PRIVATE, in the same app-data location the approved queue proved out —
    the one thing in this whole hardening arc that never came back, because it
    was MOVED rather than guarded. Keyed by a hash of the ledger's absolute
    name (``abspath``, not ``resolve``: a symlinked ledger must not silently
    share a lock with its target), so two ledgers never collide."""
    key = hashlib.sha256(str(os.path.abspath(ledger)).encode("utf-8")).hexdigest()[:16]
    # `create=True`: this IS the acquisition path (its caller opens the fd on
    # the next line), which is where directory creation belongs now that
    # `host_lock_dir` is pure name resolution. The vault is threaded through
    # (2026-08-18): `host_lock_dir`'s off-mount proof resolves the vault root,
    # and without it every ledger append raised VaultNotFoundError when the
    # process ran with an explicit --vault from a non-vault cwd — the lock KEY
    # needs no vault, but the proof does.
    return config.host_lock_dir(vault, create=True) / f"{key}.lock"

def _append_jsonl(path: Path, entry: dict[str, Any], vault: Any = None) -> None:
    """Append ONE record to a mount-resident ledger, without following a link.

    ``path.open("a")`` follows a symlink at the final name, so a link planted at
    a ledger path redirected host-written ledger data. These files are appended
    on nearly every fold and can grow large, so rewriting them atomically would
    be the wrong trade — an append that provably does not follow a link is
    (`_open_append_nofollow`).

    RECORD ATOMICITY IS THE LOCK'S JOB, NOT ``O_APPEND``'S. ``O_APPEND`` puts
    each individual ``os.write`` at EOF; it says nothing about a whole logical
    JSON line. A short write makes the loop below issue a second write, and a
    concurrent fold can land its own bytes in between — a corrupt or lost
    ledger row. So the ENTIRE record, retries included, is serialized on a
    per-ledger lock (the same portable primitive ``lock.py`` uses, which is why
    it is imported rather than re-implemented).

    THE LOCK LIVES OFF THE MOUNT (`_append_lock_path`). A lock file beside its
    ledger was itself attacker-reachable: unlink the inode while writer A holds
    it, drop a new file at the same name, and writer B locks the NEW inode and
    proceeds concurrently — which puts the interleaving back. No amount of
    checking at open time fixes that; not being reachable does."""
    from ..lock import _open_lock_fd, _try_lock, _unlock

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    lock_fd = _open_lock_fd(_append_lock_path(path, vault))
    try:
        deadline = time.monotonic() + _APPEND_LOCK_SECONDS
        while not _try_lock(lock_fd):
            if time.monotonic() >= deadline:
                raise OSError(f"could not serialize an append to {path.name} "
                              f"within {_APPEND_LOCK_SECONDS}s")
            time.sleep(0.01)
        fd = _open_append_nofollow(path)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError(f"append made no progress on {path.name}")
                view = view[n:]
        finally:
            os.close(fd)
        _unlock(lock_fd)
    finally:
        os.close(lock_fd)

__all__ = ['_reserve_exclusive', '_write_atomic', '_fsync_dir', '_read_nofollow', '_read_jsonl', '_open_append_nofollow', '_append_lock_path', '_append_jsonl']
