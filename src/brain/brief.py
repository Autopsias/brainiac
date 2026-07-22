"""Morning brief + weekly digest generators (UX-02).

Pure functions — no I/O. The caller (BrainCore) passes pre-collected data;
these assemble and format the output.

The scheduled morning brief is the ONE sanctioned scheduled task and the
guaranteed daily drain FLOOR. The tripwire line surfaces a stalled drain so
it is visible next morning rather than silently losing notes.
"""
from __future__ import annotations

import datetime
import html as _html
from typing import Any


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


# ---------------------------------------------------------------------------
# HTML brief/digest renderers (AUT-01/AUT-03, ADR-0003 Ruling c).
#
# PURE RENDER ONLY: every function below takes an already-assembled,
# already-egress-gated data structure and formats it. No index queries, no
# note reads, no overlay reads, no filesystem access of any kind — the caller
# (BrainCore.brief_html / digest_html) does every read and every
# egress.apply_gate call BEFORE handing data in here. This is what makes a
# renderer smoke test possible with plain dicts and no fixtures.
#
# Every piece of dynamic text (note ids/titles, snippets, hot-queue lines,
# recommendation text, overlay brand fields) goes through ``_esc`` before it
# touches the returned string. No <script>, no inline event handlers, no
# external assets — self-contained, light+dark safe via
# ``prefers-color-scheme``.
# ---------------------------------------------------------------------------
_NEUTRAL_BRAND: dict[str, Any] = {
    "present": False, "title": "Brain Brief", "owner_name": None,
    "accent_color": "#2563eb",
}
_ZONE_ORDER = ("projects", "areas", "resources", "archive")


def _esc(value: Any) -> str:
    """Centralised HTML-escaping chokepoint — every dynamic value rendered
    into the brief/digest HTML MUST pass through this (codex-verify-r2)."""
    return _html.escape(str(value if value is not None else ""), quote=True)


def _section(title: str, inner_html: str) -> str:
    """``inner_html`` must already be composed of ``_esc``-safe pieces — this
    helper only escapes the section title, never the body it is handed."""
    return f'<section class="card"><h2>{_esc(title)}</h2>{inner_html}</section>'


def _zone_rank(zone: Any) -> int:
    z = str(zone or "").strip().lower()
    return _ZONE_ORDER.index(z) if z in _ZONE_ORDER else len(_ZONE_ORDER)


def _html_page(*, title: str, accent: str, body: str) -> str:
    """The shared self-contained HTML5 shell: inline CSS only, zero external
    assets, zero <script>, light+dark via ``prefers-color-scheme``."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
  :root {{ --accent: {_esc(accent)}; --bg: #ffffff; --fg: #111827; --muted: #6b7280;
           --card: #f9fafb; --border: #e5e7eb; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #0b0f19; --fg: #e5e7eb; --muted: #9ca3af; --card: #131a29; --border: #232a3b; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--fg);
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
          line-height: 1.5; }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  header.brief-header h1 {{ margin: 0 0 0.25rem; color: var(--accent); font-size: 1.6rem; }}
  header.brief-header .meta {{ margin: 0 0 1.5rem; color: var(--muted); font-size: 0.9rem; }}
  section.card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px;
                  padding: 1rem 1.25rem; margin-bottom: 1rem; }}
  section.card h2 {{ margin: 0 0 0.6rem; font-size: 1.05rem; border-left: 4px solid var(--accent);
                     padding-left: 0.5rem; }}
  h3 {{ font-size: 0.95rem; margin: 0.8rem 0 0.4rem; }}
  ul.list {{ list-style: none; margin: 0; padding: 0; }}
  ul.list li {{ padding: 0.35rem 0; border-bottom: 1px solid var(--border); font-size: 0.92rem;
                overflow-wrap: anywhere; }}
  ul.list li:last-child {{ border-bottom: none; }}
  .id, .zone {{ font-weight: 600; }}
  .tag {{ display: inline-block; font-size: 0.75rem; color: var(--muted); border: 1px solid var(--border);
          border-radius: 999px; padding: 0.05rem 0.5rem; margin-left: 0.3rem; }}
  .date {{ color: var(--muted); font-size: 0.85rem; }}
  .ok {{ color: #059669; }}
  .warn {{ color: #b45309; font-weight: 600; }}
  .empty {{ color: var(--muted); font-style: italic; }}
</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>
"""


def _maintain_alert_html(maintain_alert: dict[str, Any] | None) -> str:
    """ES-01 HTML banner — same escalation data as the text banner, shared
    by ``render_brief_html`` and ``render_digest_html``. Every dynamic value
    goes through ``_esc`` (codex-verify-r2 chokepoint)."""
    if not maintain_alert or not maintain_alert.get("escalate"):
        return ""
    items = "".join(
        f'<li><span class="id">{_esc(b["branch"])}</span> — '
        f'{_esc("; ".join(b["reasons"]))}</li>'
        for b in maintain_alert.get("branches", [])
    )
    return (
        '<p class="warn">&#9888; MAINTENANCE ALERT — data below may be stale; '
        'run <code>brain doctor</code>:</p>'
        f'<ul class="list">{items}</ul>'
    )


def render_brief_html(
    brief: dict[str, Any],
    *,
    stale_links: list[dict[str, Any]] | None = None,
    revisit_sample: list[dict[str, Any]] | None = None,
    open_recommendations: list[dict[str, Any]] | None = None,
    hot_head: list[str] | None = None,
    autoresearch: dict[str, Any] | None = None,
    brand: dict[str, Any] | None = None,
) -> str:
    """Render the branded HTML morning brief (AUT-01).

    Sections: pending capture drafts, notes added/updated, revisit/stale
    sample, open recommendations + hot-queue head, index health (snapshot
    age + stats). All list arguments default to empty — a bare ``brief``
    dict (from ``build_brief``) still renders a valid, complete page.
    """
    brand = brand or _NEUTRAL_BRAND
    title = brand.get("title") or _NEUTRAL_BRAND["title"]
    owner = brand.get("owner_name")
    accent = brand.get("accent_color") or _NEUTRAL_BRAND["accent_color"]

    stale_links = stale_links or []
    revisit_sample = revisit_sample or []
    open_recommendations = open_recommendations or []
    hot_head = hot_head or []

    subtitle = f" for {_esc(owner)}" if owner else ""
    header = (
        f'<header class="brief-header"><h1>{_esc(title)}</h1>'
        f'<p class="meta">Morning brief &middot; {_esc(brief.get("date", ""))}{subtitle}</p>'
        f"</header>"
    )

    alert_html = _maintain_alert_html(brief.get("maintain_alert"))

    maintenance_line = ""
    if autoresearch and autoresearch.get("stale"):
        if autoresearch.get("never_run"):
            maintenance_line = (
                '<p class="warn">&#9888; autoresearch has never run yet — '
                "the quarterly self-tuning convention has not started.</p>"
            )
        else:
            maintenance_line = (
                f'<p class="warn">&#9888; last autoresearch run was '
                f'{_esc(autoresearch.get("age_days"))} day(s) ago '
                f'({_esc(autoresearch.get("last_run"))}) — overdue for its quarterly poke.</p>'
            )

    pending = int(brief.get("pending_before_drain", 0) or 0)
    drain = brief.get("drain") or {}
    if drain.get("stalled"):
        pending_html = f'<p class="warn">&#9888; {_esc(brief.get("tripwire", ""))}</p>'
    elif brief.get("drain_note"):
        pending_html = f'<p class="ok">&#10003; {_esc(brief["drain_note"])}</p>'
    elif pending == 0:
        pending_html = '<p class="ok">&#10003; no pending captures</p>'
    else:
        pending_html = f"<p>{_esc(pending)} capture(s) pending</p>"
    sec_pending = _section("Pending captures", pending_html)

    recent = brief.get("recent") or []
    if recent:
        rows = "".join(
            f'<li><span class="id">{_esc(n.get("id", ""))}</span> '
            f'<span class="tag">{_esc(n.get("classification") or "UNLABELLED")}</span> '
            f'<span class="date">{_esc(str(n.get("updated", ""))[:10])}</span></li>'
            for n in recent
        )
        recent_html = f'<ul class="list">{rows}</ul>'
    else:
        recent_html = '<p class="empty">no recent notes</p>'
    sec_recent = _section("Notes added / updated", recent_html)

    stale_items = "".join(
        f'<li>stale link: <span class="id">{_esc((s.get("from") or {}).get("id", ""))}</span> '
        f'&rarr; <span class="target">{_esc(s.get("target_text", ""))}</span> '
        f'<span class="tag">{_esc(s.get("reason", ""))}</span></li>'
        for s in stale_links[:10]
    )
    revisit_items = "".join(
        f'<li>revisit: <span class="id">{_esc(r.get("id", ""))}</span> '
        f'(updated {_esc(str(r.get("updated", ""))[:10])}, age {_esc(r.get("age_days", ""))}d, '
        f'score {_esc(r.get("score", ""))})</li>'
        for r in revisit_sample[:10]
    )
    if stale_items or revisit_items:
        revisit_html = f'<ul class="list">{stale_items}{revisit_items}</ul>'
    else:
        revisit_html = '<p class="empty">nothing overdue for a re-read</p>'
    sec_revisit = _section("What needs a re-read", revisit_html)

    rec_items = "".join(
        f'<li>{_esc((r.get("text") or "").splitlines()[0][:120] if r.get("text") else r.get("id", ""))} '
        f'<span class="tag">{_esc(r.get("status", "open"))}</span></li>'
        for r in open_recommendations[:10]
    )
    hot_items = "".join(f"<li>{_esc(h)}</li>" for h in hot_head[:5])
    recs_parts = []
    if rec_items:
        recs_parts.append(f'<h3>Open recommendations</h3><ul class="list">{rec_items}</ul>')
    if hot_items:
        recs_parts.append(f'<h3>Hot queue (recent)</h3><ul class="list">{hot_items}</ul>')
    recs_html = "".join(recs_parts) or '<p class="empty">nothing queued</p>'
    sec_recs = _section("Open recommendations", recs_html)

    stats_html = (
        f'<p>{_esc(brief.get("notes", 0))} notes &middot; {_esc(brief.get("chunks", 0))} chunks</p>'
        f'<p>snapshot age: {_esc(brief.get("snapshot_age") or "n/a")}</p>'
    )
    sec_stats = _section("Index health", stats_html)

    body = (
        header + alert_html + maintenance_line + sec_pending + sec_recent + sec_revisit
        + sec_recs + sec_stats
    )
    return _html_page(title=f"{title} · {brief.get('date', '')}", accent=accent, body=body)


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
