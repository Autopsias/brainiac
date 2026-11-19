"""Read-only snapshot publisher (r2-codex; pairs with S06 host/VM split).

The **authoritative writable index lives on the HOST** under %LOCALAPPDATA% /
Application Support (WAL, single-writer). The Cowork VM must NEVER write it.
Instead the host publishes a **read-only snapshot** that the VM mounts read-only
in ``./.brain/``.

Three correctness properties this module guarantees:

1. **Atomic publish.** The snapshot DB is written to a temp file then
   ``os.replace``d into place (atomic on POSIX & Windows for same-filesystem
   moves). A reader never sees a half-written index. The manifest is written the
   same way and last, so a present manifest always describes a complete DB.

2. **Generation id.** Each publish increments a monotonic ``generation`` and
   records it (plus age inputs, counts, embed model/dim, sha256) in
   ``snapshot.manifest.json``. ``brain status`` reports the generation + age so a
   VM session can tell whether its read-only view is fresh or stale.

3. **Signed.** The manifest carries an Ed25519 signature by the HOST audit key.
   The DB is built, hashed and signed in a host-private file and only then copied
   into the (possibly VM-writable) snapshot dir, so ``check_signed_snapshot``
   (used by ``restore-index``) can tell a snapshot this host published from one
   the VM swapped in. The VM's own read path does not verify it (A-13).

WAL caveat (documented, S06): SQLCipher + WAL over VirtioFS with two concurrent
Cowork sessions on one mounted index risks lock contention / corruption. The
snapshot is therefore a *copy*, published by the single host writer — the VM
never opens the authoritative WAL DB. The index stays rebuildable-from-markdown,
so a corrupt snapshot is always recoverable by re-publishing.
"""
from __future__ import annotations

import base64
import logging
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from . import config
from .dbretry import with_write_retry

logger = logging.getLogger(__name__)

SNAPSHOT_DB = "index.snapshot.sqlite"
MANIFEST = "snapshot.manifest.json"


@dataclass
class SnapshotManifest:
    generation: int
    created_epoch: float
    created_iso: str
    source_db: str
    snapshot_db: str
    sha256: str
    bytes: int
    notes: int
    chunks: int
    embed_model: str | None
    embed_dim: str | None
    schema_version: str | None
    # VULN-3387: notes removed from THIS copy because their current bytes
    # match nothing the audit chain signed and no disposition explains them.
    withheld_drift: int = 0
    # Base64 Ed25519 signature by the HOST audit key over every other field
    # (canonical JSON, same scheme as exceptions.json); "" = published unsigned.
    signature: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _finalize_readonly(db_path: Path) -> None:
    """Convert the copied snapshot to a self-contained rollback-journal DB.

    The source index runs in WAL mode; a raw copy carries WAL in its header, so a
    pure read-only consumer (the VM, opening ``mode=ro`` on a possibly read-only
    mount) would need ``-wal``/``-shm`` sidecars it cannot create. Checkpointing
    then switching to ``journal_mode=DELETE`` and closing leaves a single,
    self-contained file the VM can open read-only with NO sidecars — the property
    the host/VM split depends on.
    """
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    # Remove any residual sidecars so the published snapshot is one file.
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass


def _read_counts_and_meta(db_path: Path) -> dict:
    out = {"notes": 0, "chunks": 0, "embed_model": None, "embed_dim": None,
           "schema_version": None}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out["notes"] = int(con.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        try:
            out["chunks"] = int(con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        except sqlite3.OperationalError:
            out["chunks"] = 0
        for k in ("embed_model", "embed_dim", "schema_version"):
            r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            out[k] = r[0] if r else None
    finally:
        con.close()
    return out


def read_manifest(dest_dir: Path) -> SnapshotManifest | None:
    p = Path(dest_dir) / MANIFEST
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        # Tolerate keys a newer engine added; check_signed_snapshot reads the raw dict.
        return SnapshotManifest(**{k: v for k, v in d.items() if k in SnapshotManifest.__dataclass_fields__})
    except Exception:  # fail-open: unreadable manifest reads as no snapshot
        logger.warning("snapshot manifest unreadable; treated as no snapshot", exc_info=True)
        return None


def _untrusted_reason(dest_dir: Path, db_path: Path) -> str | None:
    try:
        raw = json.loads((dest_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "snapshot manifest is missing or unreadable"
    if not isinstance(raw, dict) or not raw.get("signature"):
        return "snapshot manifest is unsigned"
    try:
        from . import audit

        pem = audit.public_key_pem().decode("utf-8")
    except Exception:  # noqa: BLE001 — no resolvable key is "not verified"
        return "no host signing key available to verify the snapshot"
    from .exceptions_verify import _verify_signature

    if not _verify_signature(raw, pem):
        return "snapshot manifest signature is invalid"
    try:
        digest = _sha256_file(db_path)
    except OSError:
        return "snapshot DB is unreadable"
    if digest != raw.get("sha256"):
        return "snapshot DB does not match its signed sha256"
    return None


def check_signed_snapshot(dest_dir: Path, db_path: Path) -> None:
    """HOST-side: raise ``ValueError`` unless ``db_path`` is a snapshot THIS host
    published, i.e. the manifest in ``dest_dir`` is signed by this host's own
    audit key and its ``sha256`` equals the bytes at ``db_path``.

    The key is ``audit.public_key_pem()``, never the pin or a key in the
    manifest: on a co-located Cowork workspace the VM can rewrite every file on
    the mount, so only a signature it cannot mint proves the DB is the one the
    host published. Fails closed: no key, no manifest, no signature."""
    reason = _untrusted_reason(Path(dest_dir), Path(db_path))
    if reason:
        raise ValueError(f"refusing to restore an untrusted snapshot: {reason} ({db_path})")


def stage_signed_snapshot(db_path: Path, stage: Path) -> None:
    """Copy ``db_path`` to the host-private ``stage`` and verify the COPY.

    The mount file can be swapped between a first ``check_signed_snapshot`` and
    the copy, so what gets installed must be the bytes checked here. On any
    failure ``stage`` is removed and the error propagates."""
    try:
        shutil.copy2(db_path, stage)
        check_signed_snapshot(Path(db_path).parent, stage)
    except BaseException:
        Path(stage).unlink(missing_ok=True)
        raise


def _withhold_paths(db: Path, paths: set[str]) -> int:
    """Remove ``paths``' notes from the SNAPSHOT COPY only (VULN-3387). The
    authoritative index is untouched — verify-audit/doctor keep reporting the
    drift on the host; this only stops an out-of-band edit reaching the
    untrusted read-only VM leg ahead of triage. Mirrors the index's own
    ``_delete_note`` row teardown (chunks, FTS, aliases, notes) plus the
    vector table when the backend keeps one in-DB."""
    con = sqlite3.connect(str(db))
    try:
        # The withhold set carries the chain's vault-RELATIVE paths;
        # notes.path is the scanner's resolved absolute form — match by
        # exact-or-relative-tail so neither spelling convention drops a row.
        wanted = {
            rowid for rowid, p in con.execute("SELECT rowid, path FROM notes")
            if p in paths or any(p.endswith("/" + w) for w in paths)
        }
        has_vec = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='vec_index'"
        ).fetchone() is not None
        if has_vec:
            # `vec_index` is a vec0 VIRTUAL table, so a bare sqlite3 connection
            # cannot even DELETE one row from it: SQLite raises `no such module:
            # vec0` while PREPARING the statement. Every other connection reaches
            # this DB through BrainIndex, which loads the extension; this one is
            # opened raw. The branch below runs ONLY on a vault that has
            # untriaged drift, so it had never executed in the suite either --
            # the withhold test builds its index on BruteForceBackend, which
            # creates no `vec_index` at all.
            #
            # Measured 2026-08-31 on a live vault: ONE untriaged drifted note
            # made every `brain snapshot` fail with `no such module: vec0` --
            # 15 consecutive hourly runs, the daily maintain branch recording
            # that exact error each time -- while a vault with zero drift
            # published normally off the same build. A load failure ABORTS the publish deliberately -- the
            # alternative is a snapshot that drops the withheld note's rows but
            # keeps its embeddings, which is the leak VULN-3387 exists to stop.
            from .vectors import SqliteVecBackend

            SqliteVecBackend().load_into(con)
        for rowid in sorted(wanted):
            chunk_ids = [
                int(c[0]) for c in con.execute(
                    "SELECT rowid FROM chunks WHERE note_rowid=?", (rowid,)
                ).fetchall()
            ]
            if has_vec:
                for crid in chunk_ids:
                    con.execute("DELETE FROM vec_index WHERE rowid=?", (crid,))
            con.execute("DELETE FROM chunks WHERE note_rowid=?", (rowid,))
            con.execute("DELETE FROM notes_fts WHERE rowid=?", (rowid,))
            con.execute("DELETE FROM aliases WHERE note_rowid=?", (rowid,))
            con.execute("DELETE FROM notes WHERE rowid=?", (rowid,))
        con.commit()
        return len(wanted)
    finally:
        con.close()


def _checkpoint_source(source_db: Path) -> None:
    """Checkpoint the WAL into the main DB before snapshotting so the copy is a
    self-contained, consistent point-in-time (no dependence on -wal/-shm).
    CC-02/[HARDENED:adv-r1-codex]: this used to swallow OperationalError
    and copy anyway -- a busy checkpoint (another writer mid-transaction)
    would silently publish a snapshot that OMITS uncheckpointed WAL
    writes. Now bounded-retried (same helper as every other write path),
    and a checkpoint that still can't complete ABORTS the publish rather
    than copying a possibly-incomplete DB. Callers hold the CC-02 writer
    lock around this call, so real contention here should be rare."""
    con = sqlite3.connect(str(source_db))
    con.isolation_level = None

    def _checkpoint() -> None:
        # `PRAGMA wal_checkpoint` reports contention by RETURNING busy=1 -- it
        # does NOT raise. Executing it without reading the result row therefore
        # swallows exactly the failure this block exists to catch, and the
        # shutil.copy2 in the caller then copies the main DB WITHOUT its -wal,
        # losing every page that still lived only there. Re-raise as a
        # lock-shaped error so with_write_retry retries it and, past its bound,
        # aborts the publish. Mirrors the same check on the rebuild swap path
        # in index.py::_swap_staging.
        busy, log_frames, _checkpointed = con.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        # log_frames == -1 means the DB is simply not in WAL mode: nothing to
        # checkpoint, nothing to lose. Only a busy checkpoint or frames left
        # behind after a TRUNCATE is a real failure.
        if busy or (log_frames or 0) > 0:
            raise sqlite3.OperationalError(
                f"database is busy: wal_checkpoint(TRUNCATE) did not fully "
                f"truncate (busy={busy}, log_frames={log_frames}) -- a reader is "
                f"very likely still holding a transaction open against the "
                f"index, so publishing now would copy a DB whose newest pages "
                f"live only in its WAL"
            )

    try:
        with_write_retry(_checkpoint, conn=con)
    finally:
        con.close()


def _unlink_with_sidecars(db: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)


def _sweep_stale_temps(dest_dir: Path) -> int:
    """Delete temp copies an EARLIER publish left behind, and report the count.

    Publishing is serialised by the CC-02 writer lock, so no other publish can
    hold a temp file while this one runs: every `.tmp.*` here is dead. The
    `finally` below now prevents new ones, but a vault that already accumulated
    them would carry them forever otherwise -- they are full-size copies of the
    index, so 20 of them on a 6 MB index is 123 MB of invisible litter.
    """
    swept = 0
    for stale in dest_dir.glob(SNAPSHOT_DB + ".tmp.*"):
        try:
            stale.unlink()
            swept += 1
        except OSError:  # a permission or race failure is not worth aborting on
            pass
    return swept


def publish_snapshot(
    source_db: Path, dest_dir: Path, *, withhold_paths: set[str] | None = None
) -> SnapshotManifest:
    """Atomically publish ``source_db`` as a read-only snapshot in ``dest_dir``.

    Returns the new manifest. ``generation`` = previous generation + 1 (1 on the
    first publish). ``withhold_paths`` (VULN-3387) removes those notes from the
    COPY before it becomes the snapshot — see ``_withhold_paths``.
    """
    source_db = Path(source_db)
    dest_dir = Path(dest_dir)
    if not source_db.is_file():
        raise FileNotFoundError(f"source index not found: {source_db}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    prev = read_manifest(dest_dir)
    generation = (prev.generation + 1) if prev else 1

    _checkpoint_source(source_db)

    final_db = dest_dir / SNAPSHOT_DB
    _sweep_stale_temps(dest_dir)
    # Everything the manifest hashes and signs is built in a HOST-PRIVATE file
    # beside the live index, never in ``dest_dir``: on a co-located Cowork
    # workspace that directory is VM-writable, so a DB finished there could be
    # swapped before it is hashed and the host would sign the VM's bytes.
    staged = source_db.with_name(source_db.name + ".snapshot-stage")
    tmp_db = dest_dir / (SNAPSHOT_DB + f".tmp.{os.getpid()}.{generation}")
    # The `finally` covers the COPY and the withhold, not just the swap. It
    # guarded only `os.replace` until 2026-08-31, so any failure inside
    # `_withhold_paths` orphaned a full-size copy of the index. Measured that
    # day on a live vault: 20 leftovers of 6 MB each -- 123 MB, one per hourly
    # run since the withhold started raising at 22:55 the night before -- and every one of them invisible,
    # because the run that made it reported only its own exception.
    _unlink_with_sidecars(staged)  # a stale -wal would replay onto the new copy
    try:
        shutil.copy2(source_db, staged)
        withheld = _withhold_paths(staged, withhold_paths) if withhold_paths else 0
        # Make the snapshot a self-contained rollback-journal DB BEFORE tightening
        # its permissions, so a VM read-only open needs no -wal/-shm sidecars.
        _finalize_readonly(staged)
        cm = _read_counts_and_meta(staged)
        sha256, size = _sha256_file(staged), staged.stat().st_size
        # Owner-only (0600), NOT the previous world-readable 0444 -- the snapshot
        # can carry note bodies up to and including MNPI-tier content, and a
        # shared/multi-user machine is exactly the case the classification gate
        # cannot protect against (it is an egress *decision*, not containment; see
        # docs/operations/egress-provider-posture.md §2). Real read-only
        # enforcement is the ``mode=ro`` + ``PRAGMA query_only=ON`` SQLite connection
        # (BrainIndex.conn, read_only=True), NOT the filesystem bit, so this change
        # does not weaken the write-protection guarantee -- it only removes the
        # world-readable exposure. O_EXCL: never write through a name the mount
        # already holds (a planted symlink).
        fd = os.open(tmp_db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, config.SECURE_FILE_MODE)
        with os.fdopen(fd, "wb") as out, staged.open("rb") as src:
            shutil.copyfileobj(src, out, 1 << 20)
        config.secure_file_permissions(tmp_db)
        os.replace(tmp_db, final_db)  # atomic swap
    finally:
        tmp_db.unlink(missing_ok=True)
        _unlink_with_sidecars(staged)

    now = time.time()
    manifest = SnapshotManifest(
        generation=generation,
        created_epoch=now,
        created_iso=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + "Z",
        source_db=str(source_db),
        snapshot_db=str(final_db),
        sha256=sha256,
        bytes=size,
        notes=cm["notes"],
        chunks=cm["chunks"],
        embed_model=cm["embed_model"],
        embed_dim=cm["embed_dim"],
        schema_version=cm["schema_version"],
        withheld_drift=withheld,
    )
    try:
        from . import audit

        key, _source = audit.resolve_signing_key()
        unsigned = {k: v for k, v in manifest.to_dict().items() if k != "signature"}
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest.signature = base64.b64encode(key.sign(canonical)).decode("ascii")
    except Exception as exc:  # noqa: BLE001 — publish unsigned; restore-index refuses it
        logger.warning("snapshot manifest published unsigned: %s", exc)
    # Write manifest LAST, atomically — a present manifest always implies a
    # complete DB at the recorded generation.
    tmp_man = dest_dir / (MANIFEST + f".tmp.{os.getpid()}")
    tmp_man.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_man, dest_dir / MANIFEST)
    return manifest


def snapshot_status(dest_dir: Path) -> dict:
    """Report snapshot generation + age for ``brain status`` (VM-side)."""
    m = read_manifest(dest_dir)
    if m is None:
        return {"snapshot": "absent", "dest": str(dest_dir)}
    age_s = max(0.0, time.time() - m.created_epoch)
    return {
        "snapshot": "present",
        "generation": m.generation,
        "created_iso": m.created_iso,
        "age_seconds": round(age_s, 1),
        "age_human": _human_age(age_s),
        "notes": m.notes,
        "chunks": m.chunks,
        "embed_model": m.embed_model,
        "embed_dim": m.embed_dim,
        "sha256": m.sha256,
        "dest": str(dest_dir),
    }


def _human_age(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    m = s // 60
    if m < 90:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h"
    return f"{h // 24}d"
