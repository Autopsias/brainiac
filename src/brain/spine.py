"""SP-01/SP-02 — commitment spine: event-sourced ledger of everything owed.

HOST-ONLY (same trust posture as ``cos.py``'s hold store / broker queue —
lives under ``<vault>/.brain/cos/host/``, gitignored, never indexed, never
exported, inherits ADR-0003 Ruling e's no-overlay-leak rule). The VM never
reads or writes this database directly; it only ever sees the host-rendered
``shared/spine-summary.md`` projection (mirrors the ``priority-map.md``
pattern in ``cos.py``).

Design (Codex X11 hardening, s08):

- **Stable identity, independent of ``due``.** A commitment's id is a
  deterministic hash of ``(direction, counterparty, topic)`` — NOT ``due``.
  Rescheduling a commitment never mints a new id and never creates a
  duplicate row; the semantic-key dedup the analysis asked for falls out of
  this for free (recording the same ``(direction, counterparty, topic)``
  twice always resolves to the same commitment).
- **Event-sourced.** ``events`` is the only append-only table of record:
  created / rescheduled / completed / cancelled / corrected / reopened. The
  ``commitments`` table is a pure MATERIALIZED PROJECTION, fully rebuilt by
  :func:`_reduce` from the event log after every append — callers never
  ``UPDATE commitments SET status=...`` directly, so "never mutate status/due
  in place" holds by construction, not by convention.
- **Deterministic replay, tolerant of out-of-order / conflicting evidence.**
  Events are sorted by ``(ts, event_id)`` — a late-arriving event with an
  OLDER ``ts`` than already-applied evidence still slots into its correct
  place in the replay and is superseded by any event with a newer ``ts``,
  exactly as if it had arrived on time. Rebuilding from the same event set
  always yields the same state, in any insertion order.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import cos as cos_mod

DIRECTIONS = ("owed_by_me", "owed_to_me")
EVENT_TYPES = ("created", "rescheduled", "completed", "cancelled",
               "corrected", "reopened")
_CLOSED_STATUSES = {"done", "cancelled"}
DEFAULT_AT_RISK_HOURS = 48


def db_path(vault=None) -> Path:
    return cos_mod.host_dir(vault) / "commitments.sqlite"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _json_safe(v: Any) -> Any:
    """The event log is JSON text, but callers hand us values straight out of
    YAML frontmatter, where an unquoted `due: 2026-07-17` parses as a
    `datetime.date` — not JSON-serializable. Normalize dates to ISO at this one
    boundary: every source (ingestion candidates, `brain cos-spine record`,
    calendar follow-ups) routes through record_event, and the reducer/radar
    read `due` back as ISO text either way.
    """
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    return v


def _ts(dt: _dt.datetime | None = None) -> str:
    return (dt or _utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: Any) -> _dt.datetime | None:
    if not s:
        return None
    try:
        out = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s).strip().lower()).strip("-")
    return s or "untitled"


def _topic_of(text: str, topic: str | None) -> str:
    if topic:
        return _slug(topic)
    words = re.findall(r"[A-Za-z0-9]+", text or "")[:6]
    return _slug(" ".join(words)) if words else "untitled"


def semantic_key(direction: str, counterparty: str, topic: str) -> str:
    return f"{direction}|{_slug(counterparty)}|{_slug(topic)}"


def commitment_id_for(direction: str, counterparty: str, topic: str) -> str:
    key = semantic_key(direction, counterparty, topic)
    return "cmt-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _conn(vault=None) -> sqlite3.Connection:
    p = db_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            commitment_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            evidence TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_commitment
        ON events(commitment_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commitments (
            id TEXT PRIMARY KEY,
            direction TEXT NOT NULL,
            counterparty TEXT NOT NULL,
            topic TEXT NOT NULL,
            text TEXT,
            due TEXT,
            source_ref TEXT,
            status TEXT NOT NULL,
            created_ts TEXT,
            updated_ts TEXT
        )
    """)
    try:
        import os as _os
        _os.chmod(p, 0o600)  # nosemgrep: insecure-file-permissions -- host-private ledger, owner-only
    except OSError:
        pass
    return conn


def _reduce(conn: sqlite3.Connection, commitment_id: str) -> dict[str, Any] | None:
    """Deterministic rebuild of ONE commitment's current state from its full
    event history. Replay order: (ts, event_id) — the event_id (insertion
    order) breaks ties on identical timestamps only; a late-arriving event
    with an older ``ts`` is inserted into its correct place in history, not
    appended at the end."""
    rows = conn.execute(
        "SELECT * FROM events WHERE commitment_id = ? ORDER BY ts ASC, event_id ASC",
        (commitment_id,),
    ).fetchall()
    if not rows:
        return None
    state: dict[str, Any] = {
        "id": commitment_id, "direction": None, "counterparty": None,
        "topic": None, "text": None, "due": None, "source_ref": None,
        "status": "open", "created_ts": None, "updated_ts": None,
    }
    for row in rows:
        ev = json.loads(row["evidence"] or "{}")
        kind = row["event"]
        state["updated_ts"] = row["ts"]
        if kind == "created":
            for f in ("direction", "counterparty", "topic", "text", "due", "source_ref"):
                if ev.get(f) is not None:
                    state[f] = ev[f]
            state["status"] = "open"
            if state["created_ts"] is None or row["ts"] < state["created_ts"]:
                state["created_ts"] = row["ts"]
        elif kind == "rescheduled":
            if "due" in ev:
                state["due"] = ev["due"]
        elif kind == "completed":
            state["status"] = "done"
        elif kind == "cancelled":
            state["status"] = "cancelled"
        elif kind == "reopened":
            state["status"] = "open"
        elif kind == "corrected":
            for f in ("text", "counterparty", "due", "source_ref", "topic"):
                if f in ev:
                    state[f] = ev[f]
        # unknown event types are ignored (forward-compat), never fatal
    if state["created_ts"] is None:
        state["created_ts"] = rows[0]["ts"]
    return state


def _persist(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """Whole-row replace from the reduced state — NEVER a targeted
    field-level UPDATE. This is what makes "never mutate status/due in
    place" true structurally: the only write path is replay-then-replace."""
    conn.execute(
        "INSERT OR REPLACE INTO commitments "
        "(id, direction, counterparty, topic, text, due, source_ref, status, "
        " created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (state["id"], state["direction"], state["counterparty"], state["topic"],
         state["text"], state["due"], state["source_ref"], state["status"],
         state["created_ts"], state["updated_ts"]),
    )
    conn.commit()


def record_event(vault, *, event: str, direction: str | None = None,
                  counterparty: str | None = None, text: str | None = None,
                  topic: str | None = None, due: str | None = None,
                  source_ref: str | None = None, note: str | None = None,
                  ts: str | None = None,
                  commitment_id: str | None = None) -> dict[str, Any]:
    """Append ONE event and return the commitment's rebuilt current state.

    Identity resolution: pass ``commitment_id`` directly, or supply
    ``direction`` + ``counterparty`` + (``topic`` or ``text``) — the same
    deterministic id is recomputed either way (stable across reschedules,
    since ``due`` never enters the key). ``event="created"`` requires
    direction/counterparty/text; every other event type just needs enough to
    resolve the id — the corrected/rescheduled fields carried in ``evidence``
    are applied by the reducer, never written directly."""
    if event not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event!r} (expected one of {EVENT_TYPES})")
    if direction is not None and direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")

    if commitment_id is None:
        if not (direction and counterparty and (topic or text)):
            raise ValueError(
                "need commitment_id, or direction+counterparty+(topic|text) "
                "to resolve one")
        commitment_id = commitment_id_for(direction, counterparty,
                                          _topic_of(text or "", topic))
    if event == "created" and not (direction and counterparty and text):
        raise ValueError("event='created' requires direction, counterparty, text")

    evidence: dict[str, Any] = {}
    for k, v in (("direction", direction), ("counterparty", counterparty),
                 ("text", text), ("topic", _topic_of(text or "", topic) if text or topic else None),
                 ("due", due), ("source_ref", source_ref), ("note", note)):
        if v is not None:
            evidence[k] = _json_safe(v)

    conn = _conn(vault)
    try:
        conn.execute(
            "INSERT INTO events (commitment_id, ts, event, evidence) VALUES (?,?,?,?)",
            (commitment_id, ts or _ts(), event, json.dumps(evidence, sort_keys=True)),
        )
        conn.commit()
        state = _reduce(conn, commitment_id)
        assert state is not None  # we just inserted an event for it
        _persist(conn, state)
        return state
    finally:
        conn.close()


def get(vault, commitment_id: str) -> dict[str, Any] | None:
    conn = _conn(vault)
    try:
        row = conn.execute("SELECT * FROM commitments WHERE id = ?",
                           (commitment_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def events_for(vault, commitment_id: str) -> list[dict[str, Any]]:
    conn = _conn(vault)
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE commitment_id = ? ORDER BY ts ASC, event_id ASC",
            (commitment_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all(vault, *, status: str | None = None) -> list[dict[str, Any]]:
    conn = _conn(vault)
    try:
        if status:
            rows = conn.execute("SELECT * FROM commitments WHERE status = ? "
                                "ORDER BY due IS NULL, due ASC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM commitments "
                                "ORDER BY due IS NULL, due ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def rebuild_all(vault) -> int:
    """Full re-derivation of every commitment's projection from its event
    log — the disaster-recovery / test path (the projection is a cache;
    the events table is the only thing that must never be lost)."""
    conn = _conn(vault)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT commitment_id FROM events").fetchall()]
        for cid in ids:
            state = _reduce(conn, cid)
            if state:
                _persist(conn, state)
        return len(ids)
    finally:
        conn.close()


def radar(vault, now: _dt.datetime | None = None,
          at_risk_hours: int = DEFAULT_AT_RISK_HOURS) -> dict[str, Any]:
    """SP-02 — aging + at-risk view over OPEN commitments.

    ``late``: due has passed. ``at_risk``: due within ``at_risk_hours`` and
    not yet late. Every row carries ``age_days`` (since its ``created``
    event) so age is visible even for commitments with no ``due`` at all.
    Finer at-risk signals ("no calendar slot", "counterparty silence past
    their reply-latency norm") are layered on top of this list by the
    chief-of-staff skill session, which has calendar/mailbox context this
    engine-side function does not."""
    now = now or _utcnow()
    late: list[dict[str, Any]] = []
    at_risk: list[dict[str, Any]] = []
    horizon = now + _dt.timedelta(hours=at_risk_hours)
    for row in list_all(vault, status="open"):
        created = _parse_ts(row.get("created_ts"))
        age_days = (now - created).total_seconds() / 86400 if created else None
        due = _parse_ts(row.get("due"))
        entry = {**row, "age_days": round(age_days, 1) if age_days is not None else None}
        if due is None:
            continue
        if due <= now:
            late.append(entry)
        elif due <= horizon:
            at_risk.append(entry)
    late.sort(key=lambda r: r["due"])
    at_risk.sort(key=lambda r: r["due"])
    return {"late": late, "at_risk": at_risk, "as_of": _ts(now)}


# -- VM-readable projection (mirrors cos.generate_priority_map) -------------
_TEXT_TRUNCATE = 140


def render_spine_summary(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Host-only render of ``shared/spine-summary.md`` — the ONE-WAY,
    read-only projection the chief-of-staff skill reads for its LATE+RADAR
    section. Never hand-edited; regenerated in full every call."""
    import os as _os

    now = now or _utcnow()
    rep = radar(vault, now)
    open_rows = list_all(vault, status="open")
    lines = [
        "<!-- GENERATED by `brain cos-spine render` — do not hand-edit. -->",
        f"<!-- generated: {_ts(now)} open: {len(open_rows)} "
        f"late: {len(rep['late'])} at_risk: {len(rep['at_risk'])} -->",
        "# Commitment spine — read-only summary", "",
    ]

    def _row(r: dict[str, Any]) -> str:
        text = (r.get("text") or "")[:_TEXT_TRUNCATE]
        due = r.get("due") or "no due date"
        age = r.get("age_days")
        age_s = f", age {age:.1f}d" if age is not None else ""
        return (f"- `{r['id']}` [{r['direction']}] {r['counterparty']} — "
                f"{text} (due {due}{age_s})")

    lines.append(f"## LATE ({len(rep['late'])})")
    if not rep["late"]:
        lines.append("- (none)")
    else:
        lines.extend(_row(r) for r in rep["late"])
    lines.append("")
    lines.append(f"## AT-RISK — due ≤ 48h ({len(rep['at_risk'])})")
    if not rep["at_risk"]:
        lines.append("- (none)")
    else:
        lines.extend(_row(r) for r in rep["at_risk"])
    lines.append("")
    lines.append(f"## OWED — all open ({len(open_rows)})")
    if not open_rows:
        lines.append("- (none)")
    else:
        for r in open_rows:
            lines.append(_row({**r, "age_days": None}))
    out_path = cos_mod.shared_dir(vault) / "spine-summary.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        _os.chmod(out_path, 0o644)  # VM-readable projection
    except OSError:
        pass
    return {"path": str(out_path), "open": len(open_rows),
            "late": len(rep["late"]), "at_risk": len(rep["at_risk"])}


if __name__ == "__main__":  # ponytail: smallest runnable self-check
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        s1 = record_event(vault, event="created", direction="owed_by_me",
                          counterparty="Contoso", text="Send Q2 pack",
                          topic="q2-pack", due="2026-07-20T00:00:00Z",
                          source_ref="cosprop-1", ts="2026-07-13T00:00:00Z")
        cid = s1["id"]
        # Reschedule must NOT change the id (stable identity independent of due).
        s2 = record_event(vault, event="rescheduled", commitment_id=cid,
                          due="2026-07-25T00:00:00Z", ts="2026-07-14T00:00:00Z")
        assert s2["id"] == cid and s2["due"] == "2026-07-25T00:00:00Z"
        # Re-recording the SAME (direction, counterparty, topic) dedups to the
        # same commitment instead of minting a duplicate, even with a
        # different due date and reworded text.
        s3 = record_event(vault, event="created", direction="owed_by_me",
                          counterparty="Contoso", text="Send Q2 pack again",
                          topic="q2-pack", due="2026-08-01T00:00:00Z",
                          ts="2026-07-15T00:00:00Z")
        assert s3["id"] == cid, "semantic-key dedup failed"
        # Out-of-order evidence: an older-ts correction must not clobber the
        # newer reschedule.
        record_event(vault, event="corrected", commitment_id=cid,
                     text="stale correction", ts="2026-07-13T12:00:00Z")
        final = get(vault, cid)
        assert final["due"] == "2026-08-01T00:00:00Z", final
        assert final["text"] == "Send Q2 pack again", final
        rep = radar(vault, now=_dt.datetime(2026, 7, 30, tzinfo=_dt.timezone.utc))
        assert any(r["id"] == cid for r in rep["at_risk"] + rep["late"])
        record_event(vault, event="completed", commitment_id=cid,
                     ts="2026-07-16T00:00:00Z")
        assert get(vault, cid)["status"] == "done"
        print("spine.py self-check OK")
