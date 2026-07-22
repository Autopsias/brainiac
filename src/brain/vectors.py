"""Dense-vector backend ADAPTER INTERFACE + a fallback backend.

CORE-01 hardening (r2-codex): "Define an adapter interface with a fallback
vector backend BEFORE any retrieval code is written." sqlite-vec is pre-v1
(breaking changes expected) and may fail to load on a locked Windows install or
an unbuilt Cowork VM. The retrieval layer therefore depends ONLY on the
``VectorBackend`` protocol, never on sqlite-vec directly.

Two backends ship:
  * ``SqliteVecBackend``    — sqlite-vec ``vec0`` virtual table (fast ANN). Used
                              when the extension loads.
  * ``BruteForceBackend``   — vectors stored as BLOBs in a plain table; cosine
                              computed in Python. No extension, works with ANY
                              sqlite build (including a future SQLCipher build),
                              correct everywhere. The guaranteed fallback.

``get_backend()`` selects sqlite-vec when available and degrades to brute force
otherwise — the caller's retrieval code is identical either way.
"""
from __future__ import annotations

import math
import sqlite3
import struct
from typing import Protocol, Sequence, runtime_checkable


def pack_vector(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


# SQLite's SQLITE_MAX_VARIABLE_NUMBER defaults to 999 on older builds (newer
# ones raise it to ~32766) -- an unchunked `IN (?,?,...)` over a whole
# corpus's chunk rowids can blow either ceiling on a real vault (finding 4,
# 2026-07-20 dedup batch: graphify crashed with "too many SQL variables" on
# a ~2.5k-note vault). 500 stays safely under both.
_IN_CLAUSE_BATCH = 500


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@runtime_checkable
class VectorBackend(Protocol):
    """The only contract the retrieval layer is allowed to depend on."""

    name: str

    def setup(self, conn: sqlite3.Connection, dim: int) -> None: ...
    def upsert(self, conn: sqlite3.Connection, rowid: int, vec: Sequence[float]) -> None: ...
    def delete(self, conn: sqlite3.Connection, rowid: int) -> None: ...
    def delete_all(self, conn: sqlite3.Connection) -> None: ...
    def search(
        self, conn: sqlite3.Connection, query: Sequence[float], k: int
    ) -> list[tuple[int, float]]:
        """Return [(rowid, similarity 0..1)] best-first."""

    def get_vectors(
        self, conn: sqlite3.Connection, rowids: Sequence[int]
    ) -> dict[int, list[float]]:
        """Fetch stored vectors for the given chunk rowids (missing rowids are
        simply absent from the result). Used by retrieval-time diversity /
        near-duplicate suppression, which needs candidate-vs-candidate cosine
        without re-embedding."""


class SqliteVecBackend:
    name = "sqlite-vec"

    def __init__(self) -> None:
        import sqlite_vec  # noqa: F401  (import-time check; raises if absent)

        self._sqlite_vec = sqlite_vec
        self._dim = 0

    @staticmethod
    def available() -> bool:
        try:
            import sqlite_vec  # noqa: F401

            return True
        except Exception:
            return False

    @staticmethod
    def loadable() -> bool:
        """True iff the native ``vec0`` extension actually DLOPENS — not just that
        the Python wrapper imports. A packaged build can ship the ``sqlite_vec``
        Python module but omit the native ``vec0.dylib``/``.so``/``.dll`` (observed
        S10: macOS PyInstaller bundle). ``available()`` (import-only) returns True
        in that case but ``setup()`` then crashes on dlopen — so the ``auto``
        selector probes a real load here and degrades to brute-force on failure."""
        try:
            import sqlite3 as _sq

            be = SqliteVecBackend()
            con = _sq.connect(":memory:")
            try:
                be.load_into(con)
                return True
            finally:
                con.close()
        except Exception:
            return False

    def load_into(self, conn: sqlite3.Connection) -> None:
        conn.enable_load_extension(True)
        self._sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    def setup(self, conn: sqlite3.Connection, dim: int) -> None:
        self._dim = dim
        self.load_into(conn)
        conn.execute("DROP TABLE IF EXISTS vec_index")
        conn.execute(f"CREATE VIRTUAL TABLE vec_index USING vec0(embedding float[{dim}])")

    def upsert(self, conn: sqlite3.Connection, rowid: int, vec: Sequence[float]) -> None:
        conn.execute("DELETE FROM vec_index WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
            (rowid, pack_vector(vec)),
        )

    def delete(self, conn: sqlite3.Connection, rowid: int) -> None:
        conn.execute("DELETE FROM vec_index WHERE rowid = ?", (rowid,))

    def delete_all(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM vec_index")

    def search(self, conn, query, k):
        # Express k as an ``AND k = ?`` MATCH constraint, NOT a bound ``LIMIT ?``.
        # Newer sqlite (>=~3.41) pushes a parameterised LIMIT into vec0's query
        # planner so ``ORDER BY distance LIMIT ?`` works, but older builds (e.g.
        # the Cowork device VM's bundled sqlite) do not, and vec0 then raises
        # "A LIMIT or 'k = ?' constraint is required on vec0 knn queries". The
        # ``k = ?`` form is sqlite-vec's canonical KNN API and is version-robust.
        rows = conn.execute(
            "SELECT rowid, distance FROM vec_index "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (pack_vector(query), k),
        ).fetchall()
        # vec0 default metric is L2; convert to a 0..1 similarity for a uniform
        # contract with the brute-force backend.
        return [(int(r[0]), 1.0 / (1.0 + float(r[1]))) for r in rows]

    def get_vectors(self, conn, rowids):
        if not rowids:
            return {}
        out: dict[int, list[float]] = {}
        ids = [int(r) for r in rowids]
        for i in range(0, len(ids), _IN_CLAUSE_BATCH):
            batch = ids[i:i + _IN_CLAUSE_BATCH]
            qmarks = ",".join("?" * len(batch))
            for rowid, blob in conn.execute(
                f"SELECT rowid, embedding FROM vec_index WHERE rowid IN ({qmarks})",
                tuple(batch),
            ):
                out[int(rowid)] = unpack_vector(blob)
        return out


class BruteForceBackend:
    """Pure-Python fallback. Stores vectors as BLOBs in a normal table and ranks
    by cosine in Python. Slower at scale but correct and dependency-free; the
    SQLCipher-safe path (no loadable extension required)."""

    name = "brute-force"

    def setup(self, conn: sqlite3.Connection, dim: int) -> None:
        conn.execute("DROP TABLE IF EXISTS vec_blob")
        conn.execute(
            "CREATE TABLE vec_blob (rowid INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
        )

    def upsert(self, conn: sqlite3.Connection, rowid: int, vec: Sequence[float]) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO vec_blob(rowid, embedding) VALUES (?, ?)",
            (rowid, pack_vector(vec)),
        )

    def delete(self, conn: sqlite3.Connection, rowid: int) -> None:
        conn.execute("DELETE FROM vec_blob WHERE rowid = ?", (rowid,))

    def delete_all(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM vec_blob")

    def search(self, conn, query, k):
        scored = [
            (int(rowid), cosine(query, unpack_vector(blob)))
            for rowid, blob in conn.execute("SELECT rowid, embedding FROM vec_blob")
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

    def get_vectors(self, conn, rowids):
        if not rowids:
            return {}
        out: dict[int, list[float]] = {}
        ids = [int(r) for r in rowids]
        for i in range(0, len(ids), _IN_CLAUSE_BATCH):
            batch = ids[i:i + _IN_CLAUSE_BATCH]
            qmarks = ",".join("?" * len(batch))
            for rowid, blob in conn.execute(
                f"SELECT rowid, embedding FROM vec_blob WHERE rowid IN ({qmarks})",
                tuple(batch),
            ):
                out[int(rowid)] = unpack_vector(blob)
        return out


def get_backend(prefer: str = "auto") -> VectorBackend:
    """Adapter selection. ``auto`` uses sqlite-vec if loadable, else brute force.

    ``prefer`` may also be ``"sqlite-vec"`` (raises if unavailable) or
    ``"brute-force"`` (forces the fallback — used by tests and SQLCipher mode).
    """
    if prefer == "brute-force":
        return BruteForceBackend()
    if prefer == "sqlite-vec":
        return SqliteVecBackend()
    # auto — probe a REAL extension load (not just the import) so a packaged
    # build that ships the wrapper but omits the native vec0 lib degrades to
    # brute-force instead of crashing at setup() (S10 finding).
    if SqliteVecBackend.available() and SqliteVecBackend.loadable():
        try:
            return SqliteVecBackend()
        except Exception:
            pass
    return BruteForceBackend()
