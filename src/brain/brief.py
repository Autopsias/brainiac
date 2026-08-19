"""Morning brief + weekly digest generators (UX-02).

Pure functions — no I/O. The caller (BrainCore) passes pre-collected data;
these assemble and format the output.

The scheduled morning brief is the ONE sanctioned scheduled task and the
guaranteed daily drain FLOOR. The tripwire line surfaces a stalled drain so
it is visible next morning rather than silently losing notes.
"""
from __future__ import annotations

import datetime
from typing import Any

from .brief_render import (
    _NEUTRAL_BRAND,
    _ZONE_ORDER,
    _esc,
    _html_page,
    _maintain_alert_html,
    _section,
    _zone_rank,
    render_brief_html,
)

__all__ = [
    "build_brief", "format_brief", "build_digest", "format_digest",
    "parse_hot_entries", "render_brief_html", "render_digest_html",
    "_NEUTRAL_BRAND", "_ZONE_ORDER", "_esc", "_html_page",
    "_maintain_alert_html", "_section", "_zone_rank",
]


def _today() -> str:
    return datetime.date.today().isoformat()


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def _maintain_alert(maintain_state: dict[str, Any] | None) -> dict[str, Any]:
    """ES-01: pure fold of an already-loaded ``maintain-state.json`` dict into
    the same escalation shape ``brain doctor`` and the notify path use
    (``maintenance.maintain_escalation``) — one set of thresholds, three
    consumers. ``maintain_state=None`` (no state handle threaded through) is
    reported as no-alert rather than doing any I/O here — this module stays
    pure per its own module docstring; the caller (``BrainCore``) is the one
    that loads the file."""
    if not maintain_state:
        return {"escalate": False, "branches": []}
    from . import maintenance as maint

    return maint.maintain_escalation(maintain_state)


def build_brief(
    *,
    index_stats: dict[str, Any],
    recent_notes: list[dict[str, Any]],
    pending_before_drain: int,
    drain_result: dict[str, Any],
    snapshot_age_hours: float | None,
    max_recent: int = 5,
    maintain_state: dict[str, Any] | None = None,
    cos_liveness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the morning brief data structure.

    Tripwire logic:
    - ``pending_before_drain > 0`` AND ``drain_result.promoted == 0``
      AND ``drain_result.skipped > 0`` → stalled drain: emit tripwire line.
    - ``drain_result.promoted > 0`` → drain ran successfully, tripwire cleared.
    - ``pending_before_drain == 0`` → nothing to drain, clean.
    """
    drain_promoted = int(drain_result.get("promoted", 0))
    drain_skipped = int(drain_result.get("skipped", 0))
    drain_stalled = (
        pending_before_drain > 0 and drain_promoted == 0 and drain_skipped > 0
    )

    snap_age: str | None = None
    if snapshot_age_hours is not None:
        if snapshot_age_hours < 1:
            snap_age = f"{int(snapshot_age_hours * 60)}m"
        elif snapshot_age_hours < 24:
            snap_age = f"{snapshot_age_hours:.1f}h"
        else:
            snap_age = f"{snapshot_age_hours / 24:.1f}d"

    tripwire: str | None = None
    drain_note: str | None = None
    if drain_stalled:
        tripwire = (
            f"{pending_before_drain} captures pending · "
            "last successful drain: stalled (no key?)"
        )
    elif drain_promoted > 0:
        drain_note = f"drained {drain_promoted} capture(s)"

    maintain_alert = _maintain_alert(maintain_state)

    return {
        "date": _today(),
        "notes": int(index_stats.get("notes", 0)),
        "chunks": int(index_stats.get("chunks", 0)),
        "pending_before_drain": pending_before_drain,
        "drain": {
            "promoted": drain_promoted,
            "skipped": drain_skipped,
            "stalled": drain_stalled,
        },
        "snapshot_age": snap_age,
        "recent": recent_notes[:max_recent],
        "tripwire": tripwire,
        "drain_note": drain_note,
        "maintain_alert": maintain_alert,
        # LIVENESS (not failure): an unanswered COS ingestion batch breaks
        # nothing, it just silently re-kills the ingestion funnel behind the
        # one-open-batch backpressure. It has to be VISIBLE somewhere daily.
        "cos_liveness": cos_liveness or None,
    }


def format_brief(brief: dict[str, Any]) -> str:
    """Human-readable morning brief. Quiet — no plumbing noise."""
    lines = [f"brain brief · {brief['date']}"]
    lines.extend(_maintain_alert_lines(brief.get("maintain_alert")))
    lines.append(f"  {brief['notes']} notes  {brief['chunks']} chunks")

    tw = brief.get("tripwire")
    dn = brief.get("drain_note")
    if tw:
        lines.append(f"  ⚠ {tw}")
    elif dn:
        lines.append(f"  ✓ {dn}")
    elif brief.get("pending_before_drain", 0) == 0:
        lines.append("  ✓ no pending captures")

    live = brief.get("cos_liveness") or {}
    if live.get("alert"):
        lines.append(f"  ⚠ {live['alert_text']}")
    elif live.get("pending_behind_backpressure"):
        lines.append(f"  {live['pending_behind_backpressure']} COS candidate(s) "
                     f"waiting behind an open batch")
    # B8: pattern auto-capture is deliberately suspended until the producer
    # stamps category + extraction_rules_version (S07). Say what it costs, so
    # the funnel going quiet is never mistaken for the funnel being idle.
    if live.get("unstamped_batched"):
        lines.append(f"  {live['unstamped_batched']} COS candidate(s) sent to the "
                     f"owner batch for a missing category/ruleset stamp "
                     f"(pattern auto-capture suspended until the producer stamps them)")
    # INS-01: a nightly run the host validator scored INVALID/INCONCLUSIVE
    # against its own artifacts. Same loudness as the two lines above.
    if live.get("run_validity_text"):
        lines.append(f"  ⚠ {live['run_validity_text']}")
    # STA-01: candidates parked because the host cannot attribute them to a
    # VALID run. Loud here for the same reason the line above is.
    if live.get("quarantine_text"):
        lines.append(f"  ⚠ {live['quarantine_text']}")

    snap = brief.get("snapshot_age")
    if snap:
        lines.append(f"  snapshot age: {snap}")

    if brief.get("recent"):
        lines.append("  recent:")
        for n in brief["recent"]:
            lines.append(
                f"    {str(n.get('updated', ''))[:10]}  {n.get('id', '')}  "
                f"({n.get('classification') or 'UNLABELLED'})"
            )

    return "\n".join(lines)


def _maintain_alert_lines(maintain_alert: dict[str, Any] | None) -> list[str]:
    """Shared text-brief/digest banner (ES-01) — one visible warning line per
    escalated branch, always first, so a stale brief/digest is never silent."""
    if not maintain_alert or not maintain_alert.get("escalate"):
        return []
    lines = []
    for b in maintain_alert.get("branches", []):
        lines.append(
            f"  ⚠ MAINTENANCE ALERT: branch '{b['branch']}' — "
            f"{'; '.join(b['reasons'])} — data may be stale, run `brain doctor`"
        )
    return lines


def build_digest(
    *,
    index_stats: dict[str, Any],
    recent_notes: list[dict[str, Any]],
    days: int = 7,
    maintain_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the weekly digest data structure."""
    cutoff = _days_ago(days)
    in_period = [n for n in recent_notes if str(n.get("updated") or "") >= cutoff]
    return {
        "date": _today(),
        "period_days": days,
        "period_start": cutoff,
        "notes_total": int(index_stats.get("notes", 0)),
        "notes_in_period": len(in_period),
        "notes": in_period[:20],
        "maintain_alert": _maintain_alert(maintain_state),
    }


def format_digest(digest: dict[str, Any]) -> str:
    """Human-readable weekly digest. Quiet."""
    lines = [
        f"brain digest · {digest['date']} (past {digest['period_days']}d)",
    ]
    lines.extend(_maintain_alert_lines(digest.get("maintain_alert")))
    lines.append(
        f"  {digest['notes_total']} notes total  "
        f"  {digest['notes_in_period']} in period",
    )
    if digest.get("notes"):
        lines.append("  added/updated:")
        for n in digest["notes"]:
            lines.append(
                f"    {str(n.get('updated', ''))[:10]}  {n.get('id', '')}  "
                f"({n.get('classification') or 'UNLABELLED'})"
            )
    return "\n".join(lines)


def parse_hot_entries(text: str) -> list[str]:
    """Pull the header line of each ``hot.md`` entry (``## <date> — <title>``)
    into a flat display list, oldest-first (hot.md is append-only). A caller
    wanting the most-recent head takes ``[-n:]``."""
    return [line[3:].strip() for line in (text or "").splitlines()
            if line.strip().startswith("## ")]


def render_digest_html(
    digest: dict[str, Any], *, brand: dict[str, Any] | None = None,
) -> str:
    """Render the branded HTML weekly digest (AUT-03).

    Importance framing is generic — zone bucket (projects > areas > resources
    > archive), then classification tier, then recency — never an
    owner-specific score. ``digest["notes"]`` is expected already
    egress-gated by the caller.
    """
    from . import classification as cls

    brand = brand or _NEUTRAL_BRAND
    title = brand.get("title") or _NEUTRAL_BRAND["title"]
    # A digest-specific neutral title reads better than reusing the brief's,
    # but an owner-branded title (present=True) should stay as authored.
    if not brand.get("present") and title == _NEUTRAL_BRAND["title"]:
        title = "Brain Digest"
    owner = brand.get("owner_name")
    accent = brand.get("accent_color") or _NEUTRAL_BRAND["accent_color"]

    notes = list(digest.get("notes") or [])
    notes.sort(key=lambda n: str(n.get("updated") or ""), reverse=True)
    notes.sort(key=lambda n: cls.rank(n.get("classification")), reverse=True)
    notes.sort(key=lambda n: _zone_rank(n.get("zone")))

    subtitle = f" for {_esc(owner)}" if owner else ""
    header = (
        f'<header class="brief-header"><h1>{_esc(title)}</h1>'
        f'<p class="meta">Weekly digest &middot; {_esc(digest.get("date", ""))} '
        f'(past {_esc(digest.get("period_days", 7))}d){subtitle}</p></header>'
    )

    if notes:
        rows = "".join(
            f'<li><span class="zone">{_esc(n.get("zone") or "—")}</span> '
            f'<span class="id">{_esc(n.get("id", ""))}</span> '
            f'<span class="title">{_esc(n.get("title") or "")}</span> '
            f'<span class="tag">{_esc(n.get("classification") or "UNLABELLED")}</span> '
            f'<span class="date">{_esc(str(n.get("updated", ""))[:10])}</span></li>'
            for n in notes
        )
        notes_html = f'<ul class="list">{rows}</ul>'
    else:
        notes_html = '<p class="empty">nothing entered the brain this period</p>'

    summary = (
        f'<p>{_esc(digest.get("notes_total", 0))} notes total &middot; '
        f'{_esc(digest.get("notes_in_period", 0))} in the past '
        f'{_esc(digest.get("period_days", 7))} day(s), since {_esc(digest.get("period_start", ""))}</p>'
    )

    alert_html = _maintain_alert_html(digest.get("maintain_alert"))
    body = header + alert_html + _section("This week", summary + notes_html)
    return _html_page(title=f"{title} · {digest.get('date', '')}", accent=accent, body=body)
