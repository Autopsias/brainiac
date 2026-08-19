"""Configure SQLite index connections."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .. import config
from ..vectors import SqliteVecBackend


def _load_vector_backend(index: Any, conn: sqlite3.Connection) -> None:
    if not isinstance(index.backend, SqliteVecBackend):
        return
    try:
        index.backend.load_into(conn)
    except Exception:
        pass


def _open_read_only(index: Any) -> sqlite3.Connection:
    # mode=ro means SQLite cannot create the snapshot or a write journal/WAL.
    conn = sqlite3.connect(f"file:{index.db_path}?mode=ro", uri=True)
    _load_vector_backend(index, conn)
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.OperationalError:
        pass
    return conn


def _secure_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            config.secure_file_permissions(sidecar)


def _configure_writable_connection(
    conn: sqlite3.Connection, *, is_file_backed: bool
) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    # M-6: let a competing writer finish rather than failing immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    # CC-01: rebuild/sync own one explicit BEGIN IMMEDIATE transaction. Driver
    # autocommit prevents an implicit DEFERRED transaction from bypassing the
    # busy handler during a lock upgrade.
    conn.isolation_level = None


def open_connection(index: Any) -> sqlite3.Connection:
    """Open the configured SQLite connection once."""
    if index._conn is not None:
        return index._conn
    if index.read_only:
        index._conn = _open_read_only(index)
        return index._conn
    is_file_backed = index.db_path != Path(":memory:")
    if is_file_backed:
        index.db_path.parent.mkdir(parents=True, exist_ok=True)
    index._conn = sqlite3.connect(str(index.db_path))
    if is_file_backed and index.db_path.exists():
        # The index contains bodies up to MNPI; filesystem posture is owner-only.
        config.secure_file_permissions(index.db_path)
    _load_vector_backend(index, index._conn)
    _configure_writable_connection(index._conn, is_file_backed=is_file_backed)
    if is_file_backed:
        _secure_sidecars(index.db_path)
    return index._conn
