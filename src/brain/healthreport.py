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

    # WAT-01 dead-man's switch, lane 1: a stale/missing corpus-invariants row
    # is DEGRADED here in its own right, not only via the doctor row — this
    # report is run ad hoc against THIS vault, while `run_doctor()` only
    # reaches vaults present in the workspace registry.
    from . import invariants as inv

    inv_live = inv.liveness_finding(state, d)
    if inv_live:
        act_now.append(f"{inv_live[1]} — run `brain maintain`")
    for reg in inv.state_regressions(state):
        act_now.append(f"{reg.get('summary')} — see the Corpus invariants section")

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


_INVARIANT_LABELS = {
    "unlinked_sources": "unlinked raw sources",
    "cross_tier_twins": "cross-tier name-twins",
    "cross_tier_duplicates": "cross-tier duplicates (content)",
    "cross_tier_candidates": "cross-tier candidates (undecided)",
    "unguarded_ingests": "ingests admitted unguarded",
    "subfloor_families": "sub-floor supersession families",
    "unreachable_gold": "unreachable gold documents",
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


def _corpus_invariants_html(state: dict[str, Any], trend: list[dict[str, Any]],
                            today: datetime.date | None = None) -> str | None:
    """WAT-01: the four corpus invariants, each against its RATCHETED floor
    (the best value ever recorded — the threshold, not a percentage), plus
    the fold's own liveness and a trend table. ``None`` when the fold has
    never run on this vault."""
    from . import invariants as inv

    entry = state.get(inv.STATE_KEY)
    if not isinstance(entry, dict):
        return None
    metrics = entry.get("metrics")
    if not isinstance(metrics, dict):
        return None
    floors = entry.get("floors") if isinstance(entry.get("floors"), dict) else {}
    regressed = {r.get("metric") for r in inv.state_regressions(state)}

    rows = []
    for name in inv.INVARIANT_METRICS:
        m = metrics.get(name) if isinstance(metrics.get(name), dict) else {}
        value = m.get("value")
        floor = floors.get(name)
        tol = inv.metric_tolerance(name)
        threshold = f"&le; {brief_mod._esc(floor)}" if isinstance(floor, int) else "(baselining)"
        if isinstance(floor, int) and tol:
            threshold += f" (+{tol})"
        extra = ""
        if name == "unlinked_sources":
            extra = (f"{brief_mod._esc(m.get('population', '?'))} in population, "
                     f"{brief_mod._esc(m.get('excluded', 0))} excluded by design")
        elif name == "cross_tier_twins":
            extra = f"of {brief_mod._esc(m.get('pairs', '?'))} name-twin pair(s)"
        elif name in ("cross_tier_duplicates", "cross_tier_candidates"):
            # ENF-03: coverage is reported ON THE ROW, with its denominator,
            # because a conflict count without it is the number s12 rejected.
            extra = (f"coverage {brief_mod._esc(m.get('coverage', '?'))} "
                     f"({brief_mod._esc(m.get('comparable', '?'))}/"
                     f"{brief_mod._esc(m.get('population', '?'))} documents "
                     "comparable)")
            if name == "cross_tier_duplicates":
                # Only the SKIPPED reasons are "excluded by design". Superseded
                # notes are counted by the same shared definition but RETAINED
                # (they still leak), so folding them into one total would
                # overstate what this metric cannot see — the exact
                # "0 except the ones we skip" ambiguity the row exists to deny.
                by_reason = m.get("excluded_by_reason") or {}
                skipped = sum(v for k, v in by_reason.items()
                              if k in inv.CROSS_TIER_SKIP_REASONS)
                extra += (f", {brief_mod._esc(m.get('candidates', '?'))} undecided, "
                          f"{brief_mod._esc(m.get('subfloor', '?'))} sub-floor, "
                          f"{brief_mod._esc(m.get('retained_superseded', 0))} superseded "
                          f"retained, {brief_mod._esc(skipped)} excluded by design")
        elif name == "unguarded_ingests":
            # ENF-04: the RAISES are the proof the guard is alive, and they are
            # broken out PER LEG on purpose — an aggregate cannot tell a clean
            # corpus from a dead leg, which is how ENF-02's 138 filename
            # matches / 0 content matches survived review. `unstamped` keeps
            # the denominator honest: those predate the guard and were never
            # checked, so they are never folded into `clear`.
            legs = m.get("raised_by_leg") or {}
            legs_txt = (", ".join(f"{brief_mod._esc(k)} {brief_mod._esc(v)}"
                                  for k, v in sorted(legs.items()))
                        or "none yet")
            extra = (f"{brief_mod._esc(m.get('raised', '?'))} raised to a twin's "
                     f"tier (by leg: {legs_txt}), "
                     f"{brief_mod._esc(m.get('clear', '?'))} clear, "
                     f"{brief_mod._esc(m.get('subfloor', '?'))} too short to judge, "
                     f"{brief_mod._esc(m.get('unstamped', '?'))} predate the guard, "
                     f"of {brief_mod._esc(m.get('sources', '?'))} raw source(s)")
        elif name == "subfloor_families":
            extra = (f"of {brief_mod._esc(m.get('families', '?'))} supersession "
                     f"family/families, floor {brief_mod._esc(m.get('floor', '?'))}B")
        elif name == "unreachable_gold":
            extra = (f"of {brief_mod._esc(m.get('labels', '?'))} gold label(s), "
                     f"measured {brief_mod._esc(m.get('generated') or 'never')}"
                     if m.get("available") else "no reachability artifact yet")
        if m.get("error"):
            extra = f"ERROR: {brief_mod._esc(m['error'])}"
        cls = ' class="warn"' if name in regressed else ""
        rows.append(
            f"<tr{cls}><td>{brief_mod._esc(_INVARIANT_LABELS[name])}</td>"
            f"<td>{brief_mod._esc('—' if value is None else value)}</td>"
            f"<td>{threshold}</td><td>{extra}</td></tr>"
        )
    head = ("<tr><th>invariant</th><th>now</th><th>threshold (best recorded)</th>"
            "<th>context</th></tr>")
    lines = [f'<table class="tbl">{head}{"".join(rows)}</table>']

    # WAT-01: the report's own date, never the wall clock — a caller that pins
    # `today` (a test, a replay, a backdated render) must get the age AS OF that
    # date, or the liveness line silently disagrees with the rest of the report.
    age = inv.invariants_age_days(state, today)
    limit = inv.max_age_days()
    if age is None or age > limit:
        lines.append(
            f'<p class="warn">&#9888; watchdog liveness: last successful run '
            f'{brief_mod._esc("never" if age is None else str(age) + "d ago")} '
            f'(max {limit}d) — the counts above are UNWATCHED</p>')
    else:
        lines.append(f'<p class="meta">watchdog liveness: last successful run '
                      f'{age}d ago (max {limit}d) &middot; '
                      f'{brief_mod._esc(entry.get("last_run", "?"))}</p>')
    if regressed:
        lines.append('<p class="warn">&#9888; regression(s) this run: '
                      + brief_mod._esc(", ".join(sorted(str(r) for r in regressed)))
                      + '</p>')

    cols = (("ts", "ts"), ("invariant_unlinked_sources", "unlinked"),
            ("invariant_cross_tier_twins", "twins"),
            ("invariant_cross_tier_duplicates", "x-tier dup"),
            ("invariant_cross_tier_candidates", "x-tier undecided"),
            ("invariant_unguarded_ingests", "unguarded"),
            ("invariant_ingest_guard_raises", "guard raises"),
            ("invariant_subfloor_families", "sub-floor"),
            ("invariant_unreachable_gold", "unreachable gold"),
            ("invariant_age_days", "age (d)"))
    trend_rows = [r for r in trend
                  if any(r.get(c) is not None for c, _ in cols if c != "ts")]
    if trend_rows:
        thead = "<tr>" + "".join(f"<th>{lbl}</th>" for _, lbl in cols) + "</tr>"
        tbody = "".join(
            "<tr>" + "".join(f"<td>{brief_mod._esc(r.get(c, ''))}</td>" for c, _ in cols)
            + "</tr>" for r in trend_rows)
        lines.append(f'<table class="tbl">{thead}{tbody}</table>')
    return "".join(lines)


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
