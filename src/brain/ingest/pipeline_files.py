"""Implement ingest filesystem transitions."""
from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any
from ..atomic_temp import write_text_atomic
from ..notes import _SLUG_RE


#: Longest id a reader may splice into ``raw/<id>.md``: a 255-byte path component
#: minus ``.md``. Deliberately above ``notes.MAX_SLUG_BYTES`` (180): ids minted
#: before that cap, and ``<slug>-<ext>`` renditions, are legitimate and longer.
#: The charset is ASCII, so characters equal bytes.
MANIFEST_ID_MAX = 252


def manifest_path(vault: Path) -> Path:
    from .. import config
    from . import pipeline as facade

    return config.brain_runtime_dir(vault) / facade.MANIFEST_RELPATH[0]


def load_manifest(vault: Path) -> dict[str, str]:
    """sha256 -> raw note id. The file sits on the VM-visible mount, so every id
    is untrusted: each reader turns it into ``raw/<id>.md``. Only a bare slug
    (the charset ``safe_slug`` allows: no separator, no leading dot) of at most
    ``MANIFEST_ID_MAX`` characters survives; any other entry reads as absent, so
    its bytes are ingested normally and the next ``save_manifest`` drops it."""
    path = manifest_path(vault)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {sha: nid for sha, nid in data.items() if isinstance(nid, str)
            and len(nid) <= MANIFEST_ID_MAX and _SLUG_RE.fullmatch(nid)}


def save_manifest(vault: Path, manifest: dict[str, str]) -> None:
    path = manifest_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(manifest, indent=2, sort_keys=True))


def failures_path(vault: Path) -> Path:
    from .. import config
    from . import pipeline as facade

    return config.brain_runtime_dir(vault) / facade.FAILURES_RELPATH[0]


def load_failures(vault: Path) -> dict[str, int]:
    path = failures_path(vault)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_failures(vault: Path, failures: dict[str, int]) -> None:
    path = failures_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(failures, indent=2, sort_keys=True))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReadRefused(RuntimeError):
    """The entry is not a regular file we may read. Never ingest it."""


class ReadTooLarge(ReadRefused):
    """The entry is bigger than the caller's cap; the read stopped at it."""


def read_nofollow(path: Path, *, max_bytes: int | None = None, dir_fd: int | None = None) -> bytes:
    """Read a regular file WITHOUT following a symlink at the final component.

    A core copy of ``cos._read_nofollow`` (ADR 0013: ingest runs without COS),
    kept separate for the reason ``core._durability`` gives for its own copy of
    the atomic writer. ``O_NOFOLLOW`` refuses a swapped-in symlink at the
    syscall; Windows has no such flag, so ``is_symlink`` carries it there. The
    cap is enforced on the OPEN DESCRIPTOR, never on a ``stat()`` of the name,
    so every decision is made about the one object actually read (INT-04).
    With ``dir_fd``, ``path`` is a name inside that pinned directory.
    """
    import stat as _stat

    if dir_fd is None and path.is_symlink():
        raise ReadRefused(f"{path.name} is a symlink — refused")
    # O_NONBLOCK: a FIFO planted at the name would otherwise block the open
    # until a writer arrives, before the S_ISREG check below can refuse it
    # (same guard as ``cos._io``). It changes nothing for a regular file.
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ReadRefused(f"{path.name} unreadable: {type(exc).__name__}") from None
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReadRefused(f"{path.name} is not a regular file — refused")
        chunks: list[bytes] = []
        total = 0
        while True:
            b = os.read(fd, 1 << 16)
            if not b:
                return b"".join(chunks)
            total += len(b)
            if max_bytes is not None and total > max_bytes:
                raise ReadTooLarge(f"{path.name} exceeds the {max_bytes}-byte cap")
            chunks.append(b)
    finally:
        os.close(fd)


def content_key(path: Path) -> str:
    """Return the stable content key for retry accounting across renames.

    Read through ``read_nofollow`` on a descriptor: ``path`` is a name in a
    mount-writable directory, so a link or FIFO there must never be opened.
    Raises ``ReadRefused`` for one (and for a file over the ingest cap).
    """
    from . import pipeline as facade

    return sha256_bytes(read_nofollow(path, max_bytes=facade.MAX_INGEST_BYTES))


_SLUG_SANITIZE = re.compile(r"[^A-Za-z0-9]+")


def slugify_stem(stem: str) -> str:
    cleaned = _SLUG_SANITIZE.sub("-", stem).strip("-").lower()
    if cleaned:
        return cleaned
    # LOW-02: a stem with no A-Za-z0-9 at all (a Portuguese/Japanese/etc.
    # filename) collapsed to the same bare "file" for every such drop, so
    # two of them on one day collided on one id (`<date>-file`). An 8-char
    # sha256 prefix of the ORIGINAL stem keeps different non-Latin
    # filenames apart while staying short and filesystem-safe.
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
    return f"file-{digest}"


_ARCHIVE_NAME_SANITIZE = re.compile(r'[\x00-\x1f\x7f:"\\]')


def sanitize_archive_name(name: str) -> str:
    """Return a filename safe for immutable storage and YAML provenance."""
    cleaned = _ARCHIVE_NAME_SANITIZE.sub("_", name)
    return cleaned or "file"


class UnsafeVaultDir(OSError):
    """A vault-internal directory is a symlink; ingest refuses to write through it."""


def refuse_symlinked_dirs(directory: Path, levels: int = 3) -> None:
    """Refuse when any of the last ``levels`` components of ``directory`` is a symlink.

    The inbox, quarantine, processing and archive trees can be shaped by an
    untrusted writer when the vault is co-located with the VM. ``mkdir`` and
    ``rename`` follow a planted directory link, so the host would create or
    move files outside the vault. ``lstat`` each component instead (SEC F5/F32).
    A missing component is fine: ``mkdir`` creates it as a real directory.
    This is a check, not a guard: a link can still be swapped in before the
    use. Where ``dir_fd`` exists, act through ``pinned_parent`` instead.
    """
    current = directory
    for _ in range(levels):
        if current.is_symlink():
            raise UnsafeVaultDir(f"{current.name} is a symlink — refused")
        if current.parent == current:
            break
        current = current.parent


# Search-only (O_SEARCH, macOS; O_PATH, Linux), as the by-path calls needed. Elsewhere
# O_RDONLY also needs the read bit, so a write+search-only directory is refused there.
_DIR_FLAGS = (getattr(os, "O_SEARCH", None) or getattr(os, "O_PATH", os.O_RDONLY)) | getattr(
    os, "O_DIRECTORY", 0)
_HAS_DIR_FD = os.open in os.supports_dir_fd


def _open_dir_nofollow(parent_fd: int, name: str, create: bool = True) -> int:
    """Open the real directory ``name`` inside ``parent_fd``, creating it if ``create``."""
    flags = _DIR_FLAGS | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, dir_fd=parent_fd)
            return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        # A symlink fails ELOOP or ENOTDIR, by platform and open flags.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR) and stat.S_ISLNK(
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
        ):
            raise UnsafeVaultDir(f"{name} is a symlink — refused") from None
        raise


@contextlib.contextmanager
def pinned_parent(path: Path, levels: int, create: bool = True) -> Generator[tuple[Path, int | None]]:
    """Yield ``(name, dir_fd)`` for acting on ``path`` inside its parent directory.

    The last ``levels`` components of the parent are each opened ``O_NOFOLLOW``
    relative to the one above, so refusing a symlink and using the directory are
    ONE operation on one inode: a link swapped in afterwards cannot redirect an
    ``openat``/``renameat`` made through ``dir_fd`` (F45). A symlink there raises
    ``UnsafeVaultDir``; a missing component is created as a real directory when
    ``create``, else it is an error; components above them are followed, as before.
    Pass ``name`` with ``dir_fd`` to ``os.open``/``os.rename``. Where ``dir_fd`` is
    unsupported (Windows), or ``levels`` is 0, it is the old check-then-use.
    """
    if levels < 1 or not _HAS_DIR_FD:
        refuse_symlinked_dirs(path.parent, levels)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        yield path, None
        return
    parts = path.parent.parts
    depth = min(levels, len(parts) - bool(path.parent.anchor))
    base = Path(*parts[: len(parts) - depth])
    if create:
        base.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(base, _DIR_FLAGS)
    for n, name in enumerate(parts[len(parts) - depth:], 1):
        try:
            child = _open_dir_nofollow(dir_fd, name, create)
        except NotADirectoryError:
            if create and n == depth:  # what mkdir(exist_ok=True) said of a plain file
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), name) from None
            raise
        finally:
            os.close(dir_fd)
        dir_fd = child
    try:
        yield Path(path.name), dir_fd
    finally:
        os.close(dir_fd)


def _write_new(path: Path, data: bytes, levels: int, create: bool) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    with pinned_parent(path, levels, create) as (name, dir_fd):
        fd = os.open(name, flags, 0o644, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=dir_fd)
            raise


def write_new_nofollow(path: Path, data: bytes, levels: int) -> None:
    """Create ``path`` with ``O_EXCL|O_NOFOLLOW``, ``levels`` of its directory pinned; never replace."""
    _write_new(path, data, levels, True)


def write_sidecar(path: Path, text: str, levels: int) -> None:
    """Write an advisory ``.reason.txt``; a pre-planted name is left untouched."""
    try:
        _write_new(path, text.encode("utf-8"), levels, False)  # its directory must exist
    except FileExistsError:
        pass


def move_path(src: Path, dest: Path, levels: int = 0) -> None:
    """Move ``src`` to ``dest``; ``levels`` of ``dest``'s directory are pinned.

    ``levels=0`` is the unchecked move. Pinned, a failed rename (``EXDEV`` too)
    is finished as ``shutil.move`` finished it, but through the pinned directory
    (``pipeline_move_rescue``): into a destination directory, else a link is
    recreated or a file is copied and unlinked. A directory ``src`` is not
    copied (that would leave the pin): the rename error is raised.
    """
    with pinned_parent(dest, levels) as (name, dir_fd):
        try:
            os.rename(src, name, dst_dir_fd=dir_fd)
        except OSError as exc:
            if dir_fd is None:
                shutil.move(str(src), str(dest))
            else:
                from .pipeline_move_rescue import finish_failed_rename

                finish_failed_rename(src, name, dir_fd, exc)


def claim_path(path: Path, processing_dir: Path) -> Path | None:
    """Atomically claim a drop, returning ``None`` when another worker won."""
    dest = processing_dir / path.name
    i = 0
    try:
        while dest.exists():
            i += 1
            dest = processing_dir / f"{path.stem}.{i}{path.suffix}"
        with pinned_parent(dest, 2) as (name, dir_fd):
            try:
                os.rename(path, name, dst_dir_fd=dir_fd)
            except OSError:
                return None
            with contextlib.suppress(OSError):
                os.utime(name, None, dir_fd=dir_fd)
    except PermissionError:
        # An unsearchable processing dir only failed the exists/rename; an ancestor raised.
        if processing_dir.is_dir():
            return None
        raise
    return dest


def unique_destination(target_dir: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    dest = target_dir / name
    i = 0
    while dest.exists():
        i += 1
        dest = target_dir / f"{stem}.{i}{suffix}"
    return dest


def sweep_stale_processing(
    processing_dir: Path,
    inbox: Path,
    *,
    vault: Path,
    quarantine_dir: Path,
    failures: dict[str, int],
) -> None:
    """Return abandoned claims to the inbox or quarantine repeated crashes."""
    import stat as _stat

    from . import pipeline as facade

    if not processing_dir.is_dir():
        return
    # A planted link for ``_processing`` (or the inbox) would point this sweep's
    # read and rename at host files outside the vault. Do nothing and do not
    # raise: the drain's own symlink scan quarantines the link, so ingest goes on.
    if processing_dir.is_symlink() or processing_dir.parent.is_symlink():
        return
    now = _dt.datetime.now().timestamp()
    touched = False
    for stuck in list(processing_dir.iterdir()):
        try:
            st = stuck.lstat()  # the entry itself, never a link's target
        except OSError:
            continue
        if now - st.st_mtime < facade.STALE_PROCESSING_SECONDS:
            continue
        if _stat.S_ISLNK(st.st_mode):
            # Never a legitimate claim (the inbox scan skips links). Hand the link
            # itself back so the normal scan quarantines it; no hash, no count.
            move_path(stuck, unique_destination(inbox, stuck.name))
            continue
        if not _stat.S_ISREG(st.st_mode):
            continue
        try:
            key = content_key(stuck)
        except (OSError, ReadRefused):
            key = stuck.name
        count = failures.get(key, 0) + 1
        failures[key] = count
        touched = True
        if count >= facade.MAX_INGEST_FAILURES:
            quarantine_claim(
                stuck,
                quarantine_dir,
                "repeated_ingest_failure",
                [
                    f"swept from stale {facade.PROCESSING_DIRNAME}/ {count} time(s) — "
                    "process likely died mid-extraction (crash-death is "
                    "indistinguishable from poison after N attempts); giving up"
                ],
            )
            failures.pop(key, None)
        else:
            move_path(stuck, unique_destination(inbox, stuck.name))
    if touched:
        save_failures(vault, failures)


def create_exclusive_or_collision(
    dest: Path,
    data: bytes,
    known_sha: str | None = None,
) -> str:
    """Create ``dest`` without replacement, reporting idempotency or collision."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    with pinned_parent(dest, 3) as (name, dir_fd):
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileExistsError:
            try:
                existing = read_nofollow(name, dir_fd=dir_fd)
            except ReadRefused:
                return "collision"
            if existing == data:
                return "idempotent"
            data_sha = known_sha if known_sha is not None else sha256_bytes(data)
            return "idempotent" if sha256_bytes(existing) == data_sha else "collision"
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=dir_fd)
            raise
    return "written"


def scratch_directory(vault: Path) -> Path:
    """Return a scratch directory proven outside every VM-visible root."""
    from .. import config

    try:
        return config.proven_off_mount(Path(tempfile.gettempdir()), vault,
                                       what="ingest scratch dir")
    except config.HostPathUnsafe:
        directory = config.proven_off_mount(
            config.host_private_base() / "ingest-scratch",
            vault,
            what="ingest scratch dir (fallback)",
        )
        directory.mkdir(parents=True, exist_ok=True)
        config.secure_file_permissions(directory, 0o700)
        return directory


def extract_verified_buffer(
    handler: Any,
    data: bytes,
    suffix: str,
    vault: Path,
) -> Any:
    """Run a handler over a host-private copy of the verified bytes."""
    fd, tmp = tempfile.mkstemp(
        suffix=suffix or "",
        prefix="brain-ingest-",
        dir=scratch_directory(vault),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return handler.extract(Path(tmp))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def existing_note_classification(vault: Path, existing_id: str) -> str | None:
    """Return the tier of an already-ingested raw note for safe reporting."""
    note_path = vault / "raw" / f"{existing_id}.md"
    if not note_path.is_file():
        return None
    from .. import frontmatter as fm

    try:
        meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    value = meta.get("classification")
    return str(value) if value else None


def set_aside_operational(claimed: Path, inbox: Path, declared: str) -> None:
    """Consume an operational artifact without admitting it as knowledge."""
    from . import pipeline as facade

    dest_dir = inbox / facade.OPERATIONAL_DIRNAME / declared
    dest = unique_destination(dest_dir, claimed.name)
    move_path(claimed, dest, 3)
    write_sidecar(
        dest_dir / f"{dest.name}.reason.txt",
        f"skipped_reason: operational_artifact:{declared}\n"
        "- this document declares its own operational type; it is the vault's\n"
        "  output, not knowledge about the business\n"
        "- set BRAIN_INGEST_ALLOW_OPERATIONAL=1 and re-drop it to override\n",
        3,
    )


def quarantine_claim(
    claimed: Path,
    quarantine_dir: Path,
    reason: str,
    warnings: list[str],
) -> None:
    """Move a failed claim into a collision-safe quarantine sink."""
    dest_dir = quarantine_dir / reason
    dest = unique_destination(dest_dir, claimed.name)
    move_path(claimed, dest, 3)
    report_lines = [f"quarantine_reason: {reason}"] + [f"- {w}" for w in warnings]
    write_sidecar(dest_dir / f"{dest.name}.reason.txt", "\n".join(report_lines) + "\n", 3)
