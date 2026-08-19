"""COS correction operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._io import _read_jsonl
from ._layout import _ts, corrections_db_path, verdict_drop_dir

def _corrections_conn(vault) -> sqlite3.Connection:
    p = corrections_db_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS correction_events ("
        " round INTEGER NOT NULL,"
        " msg_key TEXT NOT NULL,"
        " corrected_bucket TEXT NOT NULL,"
        " corrected_tier TEXT NOT NULL,"
        " ts TEXT NOT NULL,"
        " actor TEXT NOT NULL,"
        " PRIMARY KEY (round, msg_key))")
    config.secure_file_permissions(p)
    return conn

def known_ledger_keys(vault) -> set[tuple[int, str]] | None:
    """(round, msg_key) pairs from the VM's shadow-ledger drop, or ``None``
    when no ledger file exists at all (then every key is unknown)."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    if not files:
        return None
    keys: set[tuple[int, str]] = set()
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                keys.add((r, k))
    return keys

def record_correction(vault, round_: int, msg_key: str, bucket: str, tier: str,
                      *, actor: str, ts: str | None = None) -> dict[str, Any]:
    """Append ONE correction event. Append-only (no update/delete path exists);
    rejects a duplicate (round, msg_key) and any key not present in the shadow
    ledger. ``actor`` records the HUMAN act this row is attributed to."""
    if not isinstance(round_, int):
        raise ValueError("round must be an integer")
    ledger = known_ledger_keys(vault)
    if ledger is None:
        raise ValueError("unknown key: no shadow ledger present in verdict-drop/ "
                         "— corrections must reference a ledgered (round, msg_key)")
    if (round_, msg_key) not in ledger:
        raise ValueError(f"unknown key: ({round_}, {msg_key!r}) is not in the shadow ledger")
    conn = _corrections_conn(vault)
    try:
        with conn:
            conn.execute(
                "INSERT INTO correction_events "
                "(round, msg_key, corrected_bucket, corrected_tier, ts, actor) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (round_, msg_key, bucket, tier, ts or _ts(), actor))
    except sqlite3.IntegrityError:
        raise ValueError(f"duplicate key: a correction for ({round_}, {msg_key!r}) "
                         "already exists (the store is append-only)") from None
    finally:
        conn.close()
    return {"round": round_, "msg_key": msg_key, "corrected_bucket": bucket,
            "corrected_tier": tier, "actor": actor}

def list_corrections(vault) -> list[dict[str, Any]]:
    if not corrections_db_path(vault).exists():
        return []
    conn = _corrections_conn(vault)
    try:
        rows = conn.execute(
            "SELECT round, msg_key, corrected_bucket, corrected_tier, ts, actor "
            "FROM correction_events ORDER BY ts").fetchall()
    finally:
        conn.close()
    cols = ("round", "msg_key", "corrected_bucket", "corrected_tier", "ts", "actor")
    return [dict(zip(cols, r)) for r in rows]

def shadow_ledger_entries(vault) -> list[dict[str, Any]]:
    """All verdict rows from the VM's shadow-ledger drop, deduped by
    (round, msg_key) — the last write wins (same-night re-run idempotency)."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                by_key[(r, k)] = e
    return list(by_key.values())

__all__ = ['_corrections_conn', 'known_ledger_keys', 'record_correction', 'list_corrections', 'shadow_ledger_entries']
