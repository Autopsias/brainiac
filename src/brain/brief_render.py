"""Render brief HTML blocks."""
from __future__ import annotations

import html as _html
from typing import Any


_NEUTRAL_BRAND: dict[str, Any] = {
    "present": False, "title": "Brain Brief", "owner_name": None,
    "accent_color": "#2563eb",
}
_ZONE_ORDER = ("projects", "areas", "resources", "archive")


def _esc(value: Any) -> str:
    """Escape one dynamic value before it enters the HTML document."""
    return _html.escape(str(value if value is not None else ""), quote=True)


def _section(title: str, inner_html: str) -> str:
    """Wrap trusted block markup in an escaped titled card."""
    return f'<section class="card"><h2>{_esc(title)}</h2>{inner_html}</section>'


def _zone_rank(zone: Any) -> int:
    zone_name = str(zone or "").strip().lower()
    return _ZONE_ORDER.index(zone_name) if zone_name in _ZONE_ORDER else len(_ZONE_ORDER)


def _html_page(*, title: str, accent: str, body: str) -> str:
    """Build the shared self-contained HTML5 page shell."""
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
    """Render the shared maintenance-alert block."""
    if not maintain_alert or not maintain_alert.get("escalate"):
        return ""
    items = "".join(
        f'<li><span class="id">{_esc(branch["branch"])}</span> — '
        f'{_esc("; ".join(branch["reasons"]))}</li>'
        for branch in maintain_alert.get("branches", [])
    )
    return (
        '<p class="warn">&#9888; MAINTENANCE ALERT — data below may be stale; '
        'run <code>brain doctor</code>:</p>'
        f'<ul class="list">{items}</ul>'
    )


def _render_brief_header(
    brief: dict[str, Any], title: str, owner: Any,
) -> str:
    subtitle = f" for {_esc(owner)}" if owner else ""
    return (
        f'<header class="brief-header"><h1>{_esc(title)}</h1>'
        f'<p class="meta">Morning brief &middot; {_esc(brief.get("date", ""))}{subtitle}</p>'
        "</header>"
    )


def _render_autoresearch_line(autoresearch: dict[str, Any] | None) -> str:
    if not autoresearch or not autoresearch.get("stale"):
        return ""
    if autoresearch.get("never_run"):
        return (
            '<p class="warn">&#9888; autoresearch has never run yet — '
            "the quarterly self-tuning convention has not started.</p>"
        )
    return (
        f'<p class="warn">&#9888; last autoresearch run was '
        f'{_esc(autoresearch.get("age_days"))} day(s) ago '
        f'({_esc(autoresearch.get("last_run"))}) — overdue for its quarterly poke.</p>'
    )


def _render_pending_section(brief: dict[str, Any]) -> str:
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
    return _section("Pending captures", pending_html)


def _render_recent_section(brief: dict[str, Any]) -> str:
    recent = brief.get("recent") or []
    if recent:
        rows = "".join(
            f'<li><span class="id">{_esc(note.get("id", ""))}</span> '
            f'<span class="tag">{_esc(note.get("classification") or "UNLABELLED")}</span> '
            f'<span class="date">{_esc(str(note.get("updated", ""))[:10])}</span></li>'
            for note in recent
        )
        recent_html = f'<ul class="list">{rows}</ul>'
    else:
        recent_html = '<p class="empty">no recent notes</p>'
    return _section("Notes added / updated", recent_html)


def _render_revisit_section(
    stale_links: list[dict[str, Any]], revisit_sample: list[dict[str, Any]],
) -> str:
    stale_items = "".join(
        f'<li>stale link: <span class="id">{_esc((stale.get("from") or {}).get("id", ""))}</span> '
        f'&rarr; <span class="target">{_esc(stale.get("target_text", ""))}</span> '
        f'<span class="tag">{_esc(stale.get("reason", ""))}</span></li>'
        for stale in stale_links[:10]
    )
    revisit_items = "".join(
        f'<li>revisit: <span class="id">{_esc(revisit.get("id", ""))}</span> '
        f'(updated {_esc(str(revisit.get("updated", ""))[:10])}, age '
        f'{_esc(revisit.get("age_days", ""))}d, score '
        f'{_esc(revisit.get("score", ""))})</li>'
        for revisit in revisit_sample[:10]
    )
    if stale_items or revisit_items:
        revisit_html = f'<ul class="list">{stale_items}{revisit_items}</ul>'
    else:
        revisit_html = '<p class="empty">nothing overdue for a re-read</p>'
    return _section("What needs a re-read", revisit_html)


def _render_recommendations_section(
    open_recommendations: list[dict[str, Any]], hot_head: list[str],
) -> str:
    recommendation_items = "".join(
        f'<li>{_esc((recommendation.get("text") or "").splitlines()[0][:120] if recommendation.get("text") else recommendation.get("id", ""))} '
        f'<span class="tag">{_esc(recommendation.get("status", "open"))}</span></li>'
        for recommendation in open_recommendations[:10]
    )
    hot_items = "".join(f"<li>{_esc(item)}</li>" for item in hot_head[:5])
    parts: list[str] = []
    if recommendation_items:
        parts.append(
            f'<h3>Open recommendations</h3><ul class="list">{recommendation_items}</ul>'
        )
    if hot_items:
        parts.append(f'<h3>Hot queue (recent)</h3><ul class="list">{hot_items}</ul>')
    recommendations_html = "".join(parts) or '<p class="empty">nothing queued</p>'
    return _section("Open recommendations", recommendations_html)


def _render_stats_section(brief: dict[str, Any]) -> str:
    stats_html = (
        f'<p>{_esc(brief.get("notes", 0))} notes &middot; '
        f'{_esc(brief.get("chunks", 0))} chunks</p>'
        f'<p>snapshot age: {_esc(brief.get("snapshot_age") or "n/a")}</p>'
    )
    return _section("Index health", stats_html)


def render_brief_html(
    brief: dict[str, Any], *,
    stale_links: list[dict[str, Any]] | None = None,
    revisit_sample: list[dict[str, Any]] | None = None,
    open_recommendations: list[dict[str, Any]] | None = None,
    hot_head: list[str] | None = None,
    autoresearch: dict[str, Any] | None = None,
    brand: dict[str, Any] | None = None,
) -> str:
    """Render the branded HTML morning brief from gated data."""
    brand = brand or _NEUTRAL_BRAND
    title = brand.get("title") or _NEUTRAL_BRAND["title"]
    owner = brand.get("owner_name")
    accent = brand.get("accent_color") or _NEUTRAL_BRAND["accent_color"]
    stale_links = stale_links or []
    revisit_sample = revisit_sample or []
    open_recommendations = open_recommendations or []
    hot_head = hot_head or []

    header = _render_brief_header(brief, title, owner)
    alert_html = _maintain_alert_html(brief.get("maintain_alert"))
    maintenance_line = _render_autoresearch_line(autoresearch)
    body = (
        header
        + alert_html
        + maintenance_line
        + _render_pending_section(brief)
        + _render_recent_section(brief)
        + _render_revisit_section(stale_links, revisit_sample)
        + _render_recommendations_section(open_recommendations, hot_head)
        + _render_stats_section(brief)
    )
    return _html_page(title=f"{title} · {brief.get('date', '')}", accent=accent, body=body)
