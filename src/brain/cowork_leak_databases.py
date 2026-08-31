"""Is this file a SQLITE DATABASE, and does it carry note bodies?

The database half of :mod:`brain.cowork_leak_scan`, split out on 2026-08-29
when the module went past the 500-line ratchet. Pure predicates: nothing here
knows what an ``Artifact`` is, which is exactly why it separates cleanly ---
:func:`brain.cowork_leak_scan._scan_databases` is the caller that turns a
``True`` here into a reported finding.
"""
from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# A sqlite database is recognised by its MAGIC BYTES, never by its extension:
# the same round renamed a notes database to ``payload.bin`` and the old
# suffix-matching scan reported clean. Every sqlite file begins with this
# 16-byte string, and the format's minimum page size is 512, so nothing smaller
# can be one. MEASURED warm on the same mount: sniffing every file costs 4.61s,
# sniffing only files >= 512 bytes costs 1.31s, and both find the same 4
# databases.
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_MIN_BYTES = 512


def _looks_like_sqlite(path: Path) -> bool:
    """True if ``path`` opens with the sqlite magic string.

    Cheap, extension-blind, and gated on the format's own minimum size so the
    scan does not open 10,000 one-line text files to read 16 bytes.
    """
    try:
        if path.stat().st_size < _SQLITE_MIN_BYTES:
            return False
        with path.open("rb") as fh:
            return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _sqlite_body_rows(db: Path) -> int | None:
    """Number of rows in ``db`` carrying a non-empty note ``body``.

    ``None`` when ``db`` is not a readable sqlite database with a ``notes``
    table -- an unrelated sqlite file (a COS commitments ledger, a browser
    profile) is not a note-body leak and must not be reported as one.

    Opened read-only through a URI so a live index's WAL is never touched.

    THE PATH IS URI-ENCODED, and that is not cosmetic. An f-string of the raw
    path made `?` a query separator: a database named `payload?.bin` -- a legal
    filename on macOS and Linux -- was truncated to `payload`, the open failed,
    and the scan reported clean while the note rows were still readable.
    Measured 2026-08-30: `_looks_like_sqlite` returned True and this function
    returned None for the same file. `Path.as_uri()` percent-encodes `?`, `#`
    and the rest, so the name can no longer steer the connection string.
    """
    try:
        uri = f"{db.resolve().as_uri()}?mode=ro"
    except (OSError, ValueError):
        return None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name='notes'"
        )
        if cur.fetchone() is None:
            return None
        cur = conn.execute("SELECT COUNT(*) FROM notes WHERE body IS NOT NULL AND body != ''")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _classify_db(db: Path) -> str:
    """``snapshot`` or ``derived_index``, by path.

    Both carry bodies and both are caught; the label is what an operator reads
    to know which remediation applies -- republish elsewhere, or rebuild the
    index under ``$BRAIN_INDEX_DIR``.
    """
    parts = {p.lower() for p in db.parts}
    if "snapshot" in parts or "snapshot" in db.name.lower():
        return "snapshot"
    return "derived_index"

# Sniffing is IO-LATENCY bound, not CPU bound: one `open`+`read(16)` per file,
# and the scan does nothing with the result until every file is classified. On
# the live host, 2026-08-29, over the 6785-file staged workspace:
#
#     serial          59.20s
#     8 threads       23.04s
#     32 threads      15.96s
#
# That is not a micro-optimisation. `brain doctor` gives this scan a 20s budget,
# and a cut-over workspace whose scan never FINISHES can never raise the STALE
# row that is the whole "keep it off" gate --- measured on this host before the
# change: `scan did not finish within 20s`, on a folder that was in fact clean.
# A gate that cannot complete cannot fire.
#
# ponytail: one module-level pool, sized for latency not cores. Threads are
# right here because every worker blocks in `read()` and releases the GIL; a
# process pool would pay fork cost for a 16-byte read. If a workspace ever grows
# past ~100k files, batch the walk instead of widening this.
_SNIFF_WORKERS = int(os.environ.get("BRAIN_MOUNT_SCAN_WORKERS", "32"))
_sniff_pool: "ThreadPoolExecutor | None" = None


def _sqlite_candidates(here: Path, filenames: list[str]) -> list[Path]:
    """The files in ``here`` that open with the sqlite magic bytes.

    Same files, same predicate, same order as the serial loop this replaced ---
    only the waiting overlaps. Order is preserved because callers append
    findings in it and the suite pins the sorted result.
    """
    global _sniff_pool
    paths = [here / name for name in filenames]
    if len(paths) < 4 or _SNIFF_WORKERS <= 1:
        # Not worth a dispatch, and this is the common case: most directories
        # hold a handful of files. Keeps the serial path exercised too.
        return [p for p in paths if _looks_like_sqlite(p)]
    if _sniff_pool is None:
        _sniff_pool = ThreadPoolExecutor(
            max_workers=_SNIFF_WORKERS, thread_name_prefix="brain-leak-sniff")
    hits = _sniff_pool.map(_looks_like_sqlite, paths)
    return [p for p, hit in zip(paths, hits) if hit]
