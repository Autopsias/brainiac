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


def collect_health_report_data(core: Any, *, today: datetime.date | None = None) -> dict[str, Any]:
    """Gather every render input from data the engine already collects
    elsewhere. Read-only, best-effort per section — one section failing
    (e.g. doctor raising on an odd install) degrades that section, never
    aborts the whole report."""
    from . import __version__ as engine_version
    from . import doctor as brain_doctor
    from . import maintenance as maint

    d = today or datetime.date.today()
    vault = Path(core.vault)

    try:
        state = core._load_maintain_state()
    except Exception:
        state = {}
    escalation = maint.maintain_escalation(state, d)

    try:
        doctor_report = brain_doctor.run_doctor()
    except Exception as exc:  # noqa: BLE001 — doctor failing must not sink the report
        doctor_report = {"ok": False, "rows": [], "stale_count": None,
                          "error": f"{type(exc).__name__}: {exc}"}
    doctor_rows = doctor_report.get("rows") or []
    doctor_gating = [r for r in doctor_rows if r.get("status") in ("stale", "unknown")]

    try:
        status = core.status()
    except Exception as exc:  # noqa: BLE001
        status = {"error": f"{type(exc).__name__}: {exc}"}

    trend_rows = _read_trend_rows(vault)

    act_now: list[str] = []
    for b in escalation.get("branches", []):
        act_now.append(
            f"maintain branch '{b['branch']}': {'; '.join(b['reasons'])} — "
            f"run `brain doctor` / check ~/.brain/logs/"
        )
    # dedup: run_doctor() re-checks per registered workspace, so one vault
    # registered twice yields identical rows
    seen_rows: set[tuple] = set()
    for r in doctor_gating:
        key = (r.get("surface"), r.get("status"), r.get("detail"))
        if key in seen_rows:
            continue
        seen_rows.add(key)
        act_now.append(
            f"doctor: {r.get('surface')} is {r.get('status')} — "
            f"{r.get('detail')} — run `brain doctor`"
        )
    idx = status.get("index") if isinstance(status.get("index"), dict) else {}
    if isinstance(idx, dict) and idx.get("error"):
        act_now.append(f"index unreadable: {idx['error']} — run `brain status`")

    quarantine_growing = False
    if len(trend_rows) >= 2:
        cur_q, prev_q = trend_rows[0].get("quarantine"), trend_rows[1].get("quarantine")
        if isinstance(cur_q, (int, float)) and isinstance(prev_q, (int, float)) and cur_q > prev_q:
            quarantine_growing = True
            act_now.append(
                f"quarantine growing ({prev_q} -> {cur_q}) — "
                f"inspect `vault/inbox/_quarantine/`"
            )

    snap = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}
    age_s = snap.get("age_seconds") if isinstance(snap, dict) else None
    stale_hours = maint.DEFAULT_OFFHOST_DAILY_STALE_HOURS  # reuse — no new magic number
    stale_snapshot = False
    if isinstance(age_s, (int, float)) and (age_s / 3600) > stale_hours:
        stale_snapshot = True
        act_now.append(
            f"snapshot age {age_s / 3600:.1f}h (> {stale_hours}h) — "
            f"run `brain sync --publish`"
        )

    escalated_branches = {b["branch"] for b in escalation.get("branches", [])}
    for branch, entry in state.items():
        if str(branch).startswith("_") or not isinstance(entry, dict) or branch in escalated_branches:
            continue
        skips = int(entry.get("consecutive_skips", 0) or 0)
        # s05 contract: a short skip streak is a legitimate long write holding
        # the lock (e.g. a 90-min rebuild ~= 2 hourly skips) and must stay
        # SILENT; only a streak at the escalation threshold pages. Escalated
        # branches are already reported above via branch_escalation.
        if skips >= maint.SKIP_ESCALATE_THRESHOLD:
            act_now.append(
                f"branch '{branch}': {skips} consecutive writer-busy skip(s) — "
                f"run `brain doctor`"
            )

    # BROKEN = the system is failing to do work (maintain escalation: repeated
    # failures, a stuck writer lock, stale liveness). Doctor STALE rows alone —
    # e.g. a staged workspace a version behind — mean "wants attention, still
    # working": DEGRADED. Field lesson 2026-07-20: version-drift rows drove a
    # BROKEN banner while Cowork search worked fine.
    if escalation.get("escalate"):
        verdict = VERDICT_BROKEN
    elif act_now:
        verdict = VERDICT_DEGRADED
    else:
        verdict = VERDICT_HEALTHY

    return {
        "date": d.isoformat(),
        "verdict": verdict,
        "act_now": act_now,
        "escalation": escalation,
        "doctor": {"ok": doctor_report.get("ok"), "stale_count": doctor_report.get("stale_count"),
                   "error": doctor_report.get("error")},
        "state": state,
        "status": status,
        "trend": trend_rows,
        "vault": str(vault),
        "engine_version": engine_version,
    }


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
