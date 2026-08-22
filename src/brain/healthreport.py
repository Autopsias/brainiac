"""``brain health-report`` — one static, self-contained HTML health page
rendered from EXISTING maintain/status/health-history data (no new data
collection). HOST-broker only (see ``BrainCore.health_report`` /
``brain.cli``): wired into the nightly ``brain maintain`` umbrella so a
fresh report exists after every non-dry run, and linked from the
chief-of-staff morning brief.

Verdict-embedding contract (read this before parsing the report): the
rendered page carries the verdict word TWO ways — in the ``<title>`` tag
(human-readable) and, authoritatively, as an HTML comment
``<!-- verdict: HEALTHY|DEGRADED|BROKEN -->`` placed as the very first thing
inside ``<body>``. A caller (e.g. the chief-of-staff skill assembling the
morning brief) should grep that comment rather than parse the title, which
is free-form.

Same split as ``brain.brief``: this module has a data-collection half
(``collect_health_report_data`` — touches ``core.status()``, maintain-state,
health-history.jsonl, and ``brain doctor``'s report; best-effort, never
raises) and a pure-render half (``render_health_report_html`` — takes the
already-assembled dict, does no I/O). Reuses ``brain.brief``'s HTML shell
(``_html_page``/``_section``/``_esc``) so the report matches the existing
brief/digest look-and-feel instead of inventing new CSS.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from . import brief as brief_mod

VERDICT_HEALTHY = "HEALTHY"
VERDICT_DEGRADED = "DEGRADED"
VERDICT_BROKEN = "BROKEN"

_VERDICT_COLOR = {
    VERDICT_HEALTHY: "#059669",
    VERDICT_DEGRADED: "#b45309",
    VERDICT_BROKEN: "#dc2626",
}

# Trend table depth (spec: "last ~14 rows").
TREND_ROWS = 14

# field evidence, 2026-07-20: doctor's own vocabulary already draws this
# line — only STALE/UNKNOWN gate (ADR-0005 Ruling 2); UNMANAGED is
# EXPLICITLY "deliberate choice, not a fault" (e.g. `$BRAIN_EMBEDDER=hash`
# chosen on purpose), and MANUAL_REQUIRED/NOT_DETECTABLE are expected/benign
# on every run. So the report reuses doctor's own current/stale split
# verbatim instead of re-deriving a second "non-gating issue" notion that
# would just relabel deliberate choices as warnings.


def _read_trend_rows(vault: Path, *, limit: int = TREND_ROWS) -> list[dict[str, Any]]:
    """Last ``limit`` records from ``<vault>/.brain/health-history.jsonl``,
    newest first. Missing file -> ``[]``. Malformed lines are skipped, never
    raised (``parse_recommendation_lines`` already tolerates that)."""
    from . import config as _config
    from . import maintenance as maint

    path = _config.health_history_path(vault)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = maint.parse_recommendation_lines(text)
    rows.sort(key=lambda r: str(r.get("ts") or ""))
    return list(reversed(rows))[:limit]


def _branches_table_html(state: dict[str, Any], escalation: dict[str, Any]) -> str:
    escalated = {b["branch"] for b in escalation.get("branches", [])}
    branch_names = sorted(k for k in state if not str(k).startswith("_") and isinstance(state.get(k), dict))
    if not branch_names:
        return '<p class="empty">no maintain runs recorded yet</p>'
    rows = []
    for name in branch_names:
        entry = state.get(name) or {}
        cls = ' class="warn"' if name in escalated else ""
        err = brief_mod._esc(entry.get("error")) if entry.get("error") else ""
        rows.append(
            f"<tr{cls}><td>{brief_mod._esc(name)}</td>"
            f"<td>{brief_mod._esc(entry.get('status', ''))}</td>"
            f"<td>{brief_mod._esc(entry.get('last_run', ''))}</td>"
            f"<td>{brief_mod._esc(entry.get('last_attempt', ''))}</td>"
            f"<td>{brief_mod._esc(entry.get('consecutive_failures', 0))}</td>"
            f"<td>{brief_mod._esc(entry.get('consecutive_skips', 0))}</td>"
            f"<td>{err}</td></tr>"
        )
    head = ("<tr><th>branch</th><th>status</th><th>last_run</th><th>last_attempt</th>"
            "<th>consec. failures</th><th>consec. skips</th><th>error</th></tr>")
    return f'<table class="tbl">{head}{"".join(rows)}</table>'


def _index_snapshot_html(status: dict[str, Any]) -> str:
    idx = status.get("index") if isinstance(status.get("index"), dict) else {}
    snap = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}
    if idx.get("error"):
        return f'<p class="warn">&#9888; index: {brief_mod._esc(idx["error"])}</p>'
    age_s = snap.get("age_seconds")
    age_h = round(age_s / 3600, 1) if isinstance(age_s, (int, float)) else "?"
    lines = [
        f'<p>{brief_mod._esc(idx.get("notes", "?"))} notes &middot; '
        f'{brief_mod._esc(idx.get("chunks", "?"))} chunks &middot; '
        f'schema v{brief_mod._esc(idx.get("schema_version", "?"))} &middot; '
        f'embed model {brief_mod._esc(idx.get("embed_model", "?"))}</p>',
        f'<p>snapshot generation {brief_mod._esc(snap.get("generation", "?"))} '
        f'&middot; age {brief_mod._esc(age_h)}h</p>',
        f'<p>pending drafts: {brief_mod._esc(status.get("pending_drafts", "?"))}</p>',
    ]
    return "".join(lines)


def _graph_explorer_link_html(vault: Path) -> str:
    """GRA-01: a `file://` link to the `brain graph-report` explorer page,
    plus its build generation/built-at, when the page exists. Best-effort —
    a missing page (never run yet) or an unreadable graph.json yields no
    link/no error, never a placeholder or a raised exception."""
    from . import config as _config

    html_path = _config.graph_dir(vault) / "graph-explorer.html"
    if not html_path.is_file():
        return ""
    line = f'<p><a href="file://{brief_mod._esc(str(html_path))}">Open graph explorer</a>'
    try:
        graph = json.loads(_config.graph_json_path(vault).read_text(encoding="utf-8"))
        gen = graph.get("generation")
        built_at = graph.get("built_at")
        if gen is not None or built_at is not None:
            line += f' &middot; gen {brief_mod._esc(gen)} (built {brief_mod._esc(built_at)})'
    except (OSError, ValueError):
        pass
    return line + "</p>"


def _graph_hygiene_html(state: dict[str, Any], trend: list[dict[str, Any]], vault: Path) -> str | None:
    """GRH-01: the `graph_hygiene` weekly fold's metrics + a simple trend.
    Returns ``None`` (render nothing, not even an empty section) when the
    branch has never run yet — an older vault, or one on a build that
    predates this fold, must not show a placeholder for data that plain
    doesn't exist here."""
    entry = state.get("graph_hygiene")
    if not isinstance(entry, dict):
        return None
    metrics = entry.get("metrics")
    if not isinstance(metrics, dict):
        return None

    lines = [
        f'<p>{brief_mod._esc(metrics.get("knowledge_note_count", "?"))} knowledge-layer notes '
        f'&middot; {brief_mod._esc(metrics.get("orphan_count", "?"))} orphan(s) '
        f'&middot; {brief_mod._esc(metrics.get("island_count", "?"))} connected component(s) '
        f'&middot; {brief_mod._esc(metrics.get("dangling_target_count", "?"))} dangling link target(s)</p>',
        f'<p class="meta">last run: {brief_mod._esc(entry.get("last_run", "?"))}</p>',
        _graph_explorer_link_html(vault),
    ]
    # LNK-03b: `kl_orphans` is recorded on EVERY run (daily cheap counter),
    # while `graph_orphans`/islands/dangling are Wednesday-only (the full
    # weekly fold) — union the two so the table shows daily granularity in
    # between the sparser weekly rows, still one table, no new section.
    rows = [r for r in trend if r.get("graph_orphans") is not None
            or r.get("kl_orphans") is not None]
    if rows:
        head = ("<tr><th>ts</th><th>orphans (weekly)</th><th>kl_orphans (daily)</th>"
                "<th>islands</th><th>dangling</th></tr>")
        body = "".join(
            "<tr>"
            f"<td>{brief_mod._esc(r.get('ts', ''))}</td>"
            f"<td>{brief_mod._esc(r.get('graph_orphans', ''))}</td>"
            f"<td>{brief_mod._esc(r.get('kl_orphans', ''))}</td>"
            f"<td>{brief_mod._esc(r.get('graph_islands', ''))}</td>"
            f"<td>{brief_mod._esc(r.get('graph_dangling', ''))}</td>"
            "</tr>"
            for r in rows
        )
        lines.append(f'<table class="tbl">{head}{body}</table>')
    return "".join(lines)


def _remediation_html(state: dict[str, Any], trend: list[dict[str, Any]]) -> str | None:
    """REG-04: what the automatic repair branches did, and whether they work.

    Returns ``None`` — render nothing, not an empty section — on a vault where
    the fold has never run, exactly like ``_graph_hygiene_html``. Reads the
    ``_remediation`` projection in ``maintain-state.json``; the AUTHORITATIVE
    per-branch state is host-private and deliberately not displayed from here.

    This section exists because suppression makes the lane invisible: once a
    finding a live branch owns stops bannering, hot.md is the only other
    record, and AGENTS.md 9 defines hot.md as a log nobody has to open. A
    working feature and a deleted one must not look the same."""
    entry = state.get("_remediation")
    if not isinstance(entry, dict):
        return None
    branches = entry.get("branches")
    if not isinstance(branches, dict) or not branches:
        return None
    head = ("<tr><th>branch</th><th>mode</th><th>targets</th><th>healed</th>"
            "<th>skipped</th><th>remaining</th></tr>")
    # The opening tag is joined with `+`, NOT by sitting next to the f-strings:
    # implicit string concatenation binds TIGHTER than a conditional expression,
    # so `A if c else B f"..." f"..."` puts every cell inside the else branch and
    # a warn row renders as a bare `<tr class="warn">` with no cells and no
    # closing tag — exactly the row this section exists to show (llm-review,
    # 2026-08-21).
    body = "".join(
        ("<tr class=\"warn\">" if (r.get("remaining") or 0) else "<tr>")
        + f"<td>{brief_mod._esc(name)}</td>"
        f"<td>{brief_mod._esc(r.get('mode', '?'))}</td>"
        f"<td>{brief_mod._esc(r.get('targets', '?'))}</td>"
        f"<td>{brief_mod._esc(r.get('healed', '?'))}</td>"
        f"<td>{brief_mod._esc(r.get('skipped', '?'))}</td>"
        f"<td>{brief_mod._esc(r.get('remaining', '?'))}</td>"
        "</tr>"
        for name, r in sorted(branches.items()) if isinstance(r, dict)
    )
    lines = [f'<table class="tbl">{head}{body}</table>',
             f'<p class="meta">last run: {brief_mod._esc(entry.get("last_run", "?"))}'
             ' &middot; a branch still in <code>shadow</code> mode writes nothing '
             'and promotes itself after three proving runs</p>']
    rows = [r for r in trend if r.get("remediation_healed") is not None]
    if rows:
        thead = ("<tr><th>ts</th><th>healed</th><th>remaining</th>"
                 "<th>branches in shadow</th></tr>")
        tbody = "".join(
            "<tr>"
            f"<td>{brief_mod._esc(r.get('ts', ''))}</td>"
            f"<td>{brief_mod._esc(r.get('remediation_healed', ''))}</td>"
            f"<td>{brief_mod._esc(r.get('remediation_remaining', ''))}</td>"
            f"<td>{brief_mod._esc(r.get('remediation_shadow', ''))}</td>"
            "</tr>"
            for r in rows
        )
        lines.append(f'<table class="tbl">{thead}{tbody}</table>')
    return "".join(lines)


_INVARIANT_LABELS = {
    "unlinked_sources": "unlinked raw sources",
    "cross_tier_twins": "cross-tier name-twins",
    "cross_tier_duplicates": "cross-tier duplicates (content)",
    "cross_tier_candidates": "cross-tier candidates (undecided)",
    "unguarded_ingests": "ingests admitted unguarded",
    "subfloor_families": "sub-floor supersession families",
    "unreachable_gold": "unreachable gold documents",
    "unsigned_notes": "notes with no audit-chain entry",
}


def _report_date(data: dict[str, Any]) -> datetime.date | None:
    """The date the report was COLLECTED for, parsed back out of ``data``.

    ``collect_health_report_data`` already records it as ``data["date"]``.
    Render-side age arithmetic must use it rather than the wall clock, or a
    pinned/backdated render disagrees with the rest of its own page."""
    try:
        return datetime.date.fromisoformat(str(data.get("date")))
    except (TypeError, ValueError):
        return None


def _curated_coverage_html(state: dict[str, Any]) -> str | None:
    """CUR-01: how much of the vault carries REAL supersession frontmatter,
    and — reported separately, never added to it — how many notes are sitting
    in a propose-only version-link family the owner has not answered yet.
    ``None`` when this vault has never run the fold."""
    daily = state.get("daily")
    cov = daily.get("curated_coverage") if isinstance(daily, dict) else None
    if not isinstance(cov, dict):
        return None
    notes = cov.get("notes", 0)
    linked = cov.get("linked", 0)
    pct = f"{float(cov.get('ratio', 0.0)) * 100:.1f}%"
    return (
        f'<p><strong>{brief_mod._esc(pct)}</strong> curated coverage '
        f'&middot; {brief_mod._esc(linked)} of {brief_mod._esc(notes)} note(s) '
        f'carry supersession frontmatter</p>'
        f'<p class="meta">{brief_mod._esc(cov.get("family_members_unresolved", 0))} '
        f'note(s) in {brief_mod._esc(cov.get("proposals_awaiting_owner", 0))} '
        f'proposed-but-unresolved version-link family/families — counted '
        f'separately, never as covered.</p>')


def _trend_table_html(trend: list[dict[str, Any]]) -> str:
    if not trend:
        return '<p class="empty">no history yet</p>'
    cols = ("ts", "notes", "chunks", "quarantine", "duplicate", "action_required",
            "snapshot_gen", "selftest_ms", "synthesis_cost_usd")
    head = "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
    rows = "".join(
        "<tr>" + "".join(f"<td>{brief_mod._esc(r.get(c, ''))}</td>" for c in cols) + "</tr>"
        for r in trend
    )
    return f'<table class="tbl">{head}{rows}</table>'


def render_health_report_html(data: dict[str, Any]) -> str:
    """Pure renderer — no I/O. See module docstring for the verdict-embedding
    contract (title tag + the leading ``<!-- verdict: X -->`` comment)."""
    verdict = data.get("verdict", VERDICT_HEALTHY)
    color = _VERDICT_COLOR.get(verdict, _VERDICT_COLOR[VERDICT_HEALTHY])
    date = data.get("date", "")

    verdict_comment = f"<!-- verdict: {verdict} -->"
    banner = (
        f'<div style="background:{color};color:#fff;border-radius:8px;'
        f'padding:0.75rem 1rem;margin-bottom:1rem;font-weight:700;'
        f'font-size:1.1rem;">{brief_mod._esc(verdict)}</div>'
    )
    header = (
        f'<header class="brief-header"><h1>Brain Health Report</h1>'
        f'<p class="meta">{brief_mod._esc(date)}</p></header>'
    )

    sections = []
    act_now = data.get("act_now") or []
    if act_now:
        items = "".join(f"<li>{brief_mod._esc(a)}</li>" for a in act_now)
        sections.append(brief_mod._section("Act now", f'<ul class="list">{items}</ul>'))

    sections.append(brief_mod._section(
        "Maintain branches", _branches_table_html(data.get("state") or {}, data.get("escalation") or {})))
    sections.append(brief_mod._section("Index & snapshot", _index_snapshot_html(data.get("status") or {})))
    graph_hygiene_html = _graph_hygiene_html(
        data.get("state") or {}, data.get("trend") or [], Path(data.get("vault") or "."))
    if graph_hygiene_html is not None:
        sections.append(brief_mod._section("Graph hygiene", graph_hygiene_html))
    invariants_html = _corpus_invariants_html(data.get("state") or {}, data.get("trend") or [],
                                          _report_date(data))
    if invariants_html is not None:
        sections.append(brief_mod._section("Corpus invariants", invariants_html))
    remediation_html = _remediation_html(data.get("state") or {}, data.get("trend") or [])
    if remediation_html is not None:
        sections.append(brief_mod._section("Automatic repairs", remediation_html))
    coverage_html = _curated_coverage_html(data.get("state") or {})
    if coverage_html is not None:
        sections.append(brief_mod._section("Currency coverage", coverage_html))
    sections.append(brief_mod._section("Trend (recent runs)", _trend_table_html(data.get("trend") or [])))

    footer = (
        f'<p class="meta">brain {brief_mod._esc(data.get("engine_version", ""))} &middot; '
        f'generated {brief_mod._esc(datetime.datetime.now().isoformat(timespec="seconds"))} &middot; '
        f'vault {brief_mod._esc(data.get("vault", ""))}</p>'
    )

    body = verdict_comment + banner + header + "".join(sections) + footer
    extra_css = (
        '<style>table.tbl{width:100%;border-collapse:collapse;font-size:0.85rem;}'
        'table.tbl th,table.tbl td{text-align:left;padding:0.3rem 0.5rem;'
        'border-bottom:1px solid var(--border);}'
        'table.tbl tr.warn td{color:#b45309;font-weight:600;}</style>'
    )
    html = brief_mod._html_page(title=f"Brain Health Report — {verdict} — {date}", accent=color, body=body)
    return html.replace("</head>", extra_css + "</head>")


from .healthreport_sections import _corpus_invariants_html as _corpus_invariants_html, collect_health_report_data as collect_health_report_data  # noqa: E402
