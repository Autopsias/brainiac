"""Read-only snapshot publisher (r2-codex; pairs with S06 host/VM split).

The **authoritative writable index lives on the HOST** under %LOCALAPPDATA% /
Application Support (WAL, single-writer). The Cowork VM must NEVER write it.
Instead the host publishes a **read-only snapshot** that the VM mounts read-only
in ``./.brain/``.

Two correctness properties this module guarantees:

1. **Atomic publish.** The snapshot DB is written to a temp file then
   ``os.replace``d into place (atomic on POSIX & Windows for same-filesystem
   moves). A reader never sees a half-written index. The manifest is written the
   same way and last, so a present manifest always describes a complete DB.

2. **Generation id.** Each publish increments a monotonic ``generation`` and
   records it (plus age inputs, counts, embed model/dim, sha256) in
   ``snapshot.manifest.json``. ``brain status`` reports the generation + age so a
   VM session can tell whether its read-only view is fresh or stale.

WAL caveat (documented, S06): SQLCipher + WAL over VirtioFS with two concurrent
Cowork sessions on one mounted index risks lock contention / corruption. The
snapshot is therefore a *copy*, published by the single host writer — the VM
never opens the authoritative WAL DB. The index stays rebuildable-from-markdown,
so a corrupt snapshot is always recoverable by re-publishing.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from . import config
from .dbretry import with_write_retry

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
        return SnapshotManifest(**d)
    except Exception:
        return None


def publish_snapshot(source_db: Path, dest_dir: Path) -> SnapshotManifest:
    """Atomically publish ``source_db`` as a read-only snapshot in ``dest_dir``.

    Returns the new manifest. ``generation`` = previous generation + 1 (1 on the
    first publish).
    """
    source_db = Path(source_db)
    dest_dir = Path(dest_dir)
    if not source_db.is_file():
        raise FileNotFoundError(f"source index not found: {source_db}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    prev = read_manifest(dest_dir)
    generation = (prev.generation + 1) if prev else 1

    # Checkpoint the WAL into the main DB before snapshotting so the copy is a
    # self-contained, consistent point-in-time (no dependence on -wal/-shm).
    # CC-02/[HARDENED:adv-r1-codex]: this used to swallow OperationalError
    # and copy anyway -- a busy checkpoint (another writer mid-transaction)
    # would silently publish a snapshot that OMITS uncheckpointed WAL
    # writes. Now bounded-retried (same helper as every other write path),
    # and a checkpoint that still can't complete ABORTS the publish rather
    # than copying a possibly-incomplete DB. Callers hold the CC-02 writer
    # lock around this call, so real contention here should be rare.
    con = sqlite3.connect(str(source_db))
    con.isolation_level = None
    try:
        with_write_retry(
            lambda: con.execute("PRAGMA wal_checkpoint(TRUNCATE)"), conn=con
        )
    finally:
        con.close()

    final_db = dest_dir / SNAPSHOT_DB
    tmp_db = dest_dir / (SNAPSHOT_DB + f".tmp.{os.getpid()}.{generation}")
    shutil.copy2(source_db, tmp_db)
    try:
        os.replace(tmp_db, final_db)  # atomic swap
    finally:
        if tmp_db.exists():
            tmp_db.unlink()
    # Make the snapshot a self-contained rollback-journal DB BEFORE tightening
    # its permissions, so a VM read-only open needs no -wal/-shm sidecars.
    _finalize_readonly(final_db)
    # Owner-only (0600), NOT the previous world-readable 0444 -- the snapshot
    # can carry note bodies up to and including MNPI-tier content, and a
    # shared/multi-user machine is exactly the case the classification gate
    # cannot protect against (it is an egress *decision*, not containment; see
    # docs/operations/egress-provider-posture.md §2). Real read-only
    # enforcement is the ``mode=ro`` + ``PRAGMA query_only=ON`` SQLite connection
    # (BrainIndex.conn, read_only=True), NOT the filesystem bit, so this change
    # does not weaken the write-protection guarantee -- it only removes the
    # world-readable exposure.
    config.secure_file_permissions(final_db)

    cm = _read_counts_and_meta(final_db)
    now = time.time()
    manifest = SnapshotManifest(
        generation=generation,
        created_epoch=now,
        created_iso=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + "Z",
        source_db=str(source_db),
        snapshot_db=str(final_db),
        sha256=_sha256_file(final_db),
        bytes=final_db.stat().st_size,
        notes=cm["notes"],
        chunks=cm["chunks"],
        embed_model=cm["embed_model"],
        embed_dim=cm["embed_dim"],
        schema_version=cm["schema_version"],
    )
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
