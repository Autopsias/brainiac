"""The bytes-join RECORD — the second claim, written where a reader can see it.

SPLIT OUT OF ``_attachment_join`` ON SIZE (2026-09-05), not on a new idea. The
gate module decides whether a thread's files are in the vault; this one WRITES
DOWN the joins it found, and nothing reads this file to decide anything. Every
consumer recomputes the chain through ``attachment_lane_context``, so the
record is evidence and never authority — which is exactly why it can live in
its own module without a trust story of its own.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._io import _append_jsonl, _read_jsonl
from ._layout import _ts
from ._attachment_join import attachment_lane_context


def record_attachment_joins(vault, *, since_days: int | None = None,
                            today: _dt.date | None = None) -> dict[str, Any]:
    """Append the BYTES-JOIN claim for every run still in reach. IDEMPOTENT.

    THE SECOND CLAIM, and it is deliberately not a second mailbox category.
    A second category would share the undo key ``<cid>|categorize`` with the
    priority chip (``cos_mutate_plan_marks`` documents the collision), and
    losing an undo row is the one failure the mutation lane exists to prevent.
    A record on the host says the same thing to any reader that asks, with no
    mailbox write at all — and it says MORE than a chip can: which file, which
    content hash, which note.

    THE SIGNATURE ARRIVES AFTER THE NIGHT THAT OFFERED THE FILE, so this walks
    a window rather than one run: the fetch stages the bytes, the sweep
    quarantines them, the owner answers his batch, and a LATER maintenance
    drain signs the note. ``since_days`` (default ``$BRAIN_COS_SINCE_DAYS``,
    14) bounds the window on the run id's own date, the same knob and the same
    reading as the ingestion mark's catch-up.

    Idempotency is on ``(manifest line key, content hash)`` — the line says
    which offer this is, the hash says which bytes. Re-running writes nothing.
    """
    from ._attachment_store import (                             # noqa: PLC0415
        attachment_joins_path, ingest_manifest_dir)

    if since_days is None:
        try:
            since_days = int(os.environ.get(CATCH_UP_DAYS_ENV)
                             or DEFAULT_CATCH_UP_DAYS)
        except ValueError:
            since_days = DEFAULT_CATCH_UP_DAYS
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    cutoff = ((today - _dt.timedelta(days=max(int(since_days), 0))).isoformat()
              if since_days and since_days > 0 else "")
    runs: set[str] = set()
    for mf in sorted(ingest_manifest_dir(vault).glob("manifest-*.jsonl")):
        for entry in _read_jsonl(mf):
            rid = str(entry.get("msg_key") or "").split(":", 1)[0]
            if rid and rid[:10] >= cutoff:
                runs.add(rid)
    path = attachment_joins_path(vault)
    seen = {_join_key(r) for r in _read_jsonl(path)}
    written = 0
    for rid in sorted(runs):
        for row in attachment_lane_context(vault, rid)["joins"]:
            key = _join_key(row)
            if key in seen:
                continue
            seen.add(key)
            _append_jsonl(path, {**row, "ts": _ts()}, vault=vault)
            written += 1
    # ponytail: the read-check-append is NOT atomic — two overlapping callers
    # can both see a key absent and both append it. Not fixed with a lock
    # around the whole update, because `_append_jsonl` takes the per-ledger
    # flock itself and nesting the same lock on a second fd deadlocks. Two
    # things make the race cost nothing instead: `brain maintain` (the only
    # scheduled caller) holds the single-writer lock for its whole run, so two
    # of them cannot overlap; and `attachment_joins` DE-DUPLICATES on read, so
    # a duplicate row from a hand-run overlap changes no count a reader sees.
    # Upgrade path if this ever needs to be exact: a `lock_fd` parameter on
    # `_append_jsonl` so one acquisition can span a batch.
    return {"runs": len(runs), "recorded": written, "path": str(path),
            "total": len(seen)}


def _join_key(row: dict[str, Any]) -> tuple[str, str]:
    """The idempotency key of one join row: WHICH OFFER, and WHICH BYTES."""
    return (str(row.get("manifest_line_key") or ""),
            str(row.get("sha256") or ""))


def attachment_joins(vault, run_id: str | None = None) -> list[dict[str, Any]]:
    """Every recorded bytes-join claim, optionally for ONE run.

    DE-DUPLICATED ON READ, first row wins. The ledger is append-only and the
    writer's check-then-append is not atomic (see `record_attachment_joins`),
    so this is where a duplicate stops being visible.
    """
    from ._attachment_store import attachment_joins_path         # noqa: PLC0415

    out, seen = [], set()
    for r in _read_jsonl(attachment_joins_path(vault)):
        key = _join_key(r)
        if key in seen:
            continue
        seen.add(key)
        if run_id is None or str(r.get("run") or "") == run_id:
            out.append(r)
    return out

__all__ = ['_join_key', 'record_attachment_joins', 'attachment_joins']
