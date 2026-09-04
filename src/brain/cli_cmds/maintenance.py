"""Execute scheduled maintenance commands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import maintenance as maint
from .. import cli as shared

_emit = shared._emit
_filter_dicts = shared._filter_dicts


def _run_check(args, ctx) -> int:
    core = ctx.core
    res = core.check(dry_run=args.dry_run)
    if args.json:
        _emit(res, True)
    else:
        head = f"check [dry_run={res['dry_run']}]"
        _emit(
            None, False, head + "\n" + maint.render_outcomes_markdown(res["outcomes"])
        )
    return 0


def _run_health(args, ctx) -> int:
    core = ctx.core
    res = core.health()
    if args.json:
        _emit(res, True)
    else:
        st = res.get("selftest", {})
        head = (
            f"health: probe_ok={st.get('probe_ok')} "
            f"backend={st.get('vector_backend')} model={st.get('embed_model')}"
        )
        _emit(
            None, False, head + "\n" + maint.render_outcomes_markdown(res["outcomes"])
        )
    return 0


def _run_curate(args, ctx) -> int:
    core = ctx.core
    res = core.curate(dry_run=args.dry_run, k=args.k)
    surfaced, report = _filter_dicts(res["unclassified_notes"], args.max_tier)
    action_required = [
        maint.action_required_item(
            f"{n['id']} has a missing/invalid classification frontmatter value",
            "default-deny would withhold this note (treated as MNPI) until fixed",
            f"add classification: <Tier> to {n['path']}'s frontmatter",
            n["path"],
        )
        for n in surfaced
    ]

    # stale wikilink targets — gate on the FROM note, and the TARGET note
    # too when it resolved (both must clear the cap, same discipline as
    # near_dup's pair gating).
    stale_nodes: dict[str, dict] = {}
    for s in res["stale_links"]:
        stale_nodes[s["from"]["id"]] = s["from"]
        if s.get("target"):
            stale_nodes[s["target"]["id"]] = s["target"]
    surfaced_stale_nodes, stale_report = _filter_dicts(
        list(stale_nodes.values()), args.max_tier
    )
    surfaced_stale_ids = {n["id"] for n in surfaced_stale_nodes}
    gated_stale = [
        s
        for s in res["stale_links"]
        if s["from"]["id"] in surfaced_stale_ids
        and (s.get("target") is None or s["target"]["id"] in surfaced_stale_ids)
    ]
    action_required += [
        maint.action_required_item(
            f"{s['from']['id']} links to {s['target_text']!r} which "
            + (
                "no longer resolves to any note"
                if s["reason"] == "vanished"
                else f"has moved to {s['target']['path']}"
            ),
            "a wikilink whose target vanished or moved to archive/ leads somewhere outdated",
            "repoint the link, update the target, or accept it as an intentional historical reference",
            s["from"]["path"],
        )
        for s in gated_stale
    ]

    # revisit sample — informational triage list, gated the same way.
    surfaced_revisit, revisit_report = _filter_dicts(
        res["revisit_sample"], args.max_tier
    )

    outcomes = maint.build_outcomes(res["auto_fixed"], action_required, [])
    if args.json:
        _emit(
            {
                **res,
                "unclassified_notes": surfaced,
                "stale_links": gated_stale,
                "revisit_sample": surfaced_revisit,
                "egress": report,
                "stale_egress": stale_report,
                "revisit_egress": revisit_report,
                "outcomes": outcomes,
            },
            True,
        )
    else:
        head = (
            f"curate [dry_run={res['dry_run']}] -- {report['surfaced']}/{report['total']} unclassified surfaced, "
            f"{len(gated_stale)} stale link(s), {len(surfaced_revisit)} revisit candidate(s)"
        )
        _emit(None, False, head + "\n" + maint.render_outcomes_markdown(outcomes))
    return 0


def _run_integrity(args, ctx) -> int:
    core = ctx.core
    scan_injection = bool(getattr(args, "injection", False))
    res = core.integrity(
        min_score=args.min_score, k=args.k, scan_injection=scan_injection,
    )
    pairs = res["near_dup_pairs"]
    nodes = {}
    for p in pairs:
        nodes[p["a"]["id"]] = p["a"]
        nodes[p["b"]["id"]] = p["b"]
    surfaced_nodes, report = _filter_dicts(list(nodes.values()), args.max_tier)
    surfaced_ids = {n["id"] for n in surfaced_nodes}
    gated_pairs = [
        p
        for p in pairs
        if p["a"]["id"] in surfaced_ids and p["b"]["id"] in surfaced_ids
    ]
    action_required = [
        maint.action_required_item(
            f"{p['a']['id']} <-> {p['b']['id']} score={p['score']}",
            "de-dup is a human merge/keep judgment, never auto-merged",
            "review both notes; merge or explicitly mark distinct",
            f"{p['a']['path']} | {p['b']['path']}",
        )
        for p in gated_pairs
    ]
    for key in ("anchor_issue", "audit_issue"):
        if res.get(key):
            action_required.insert(0, res[key])
    # Gated for DISPLAY only — a finding names a note, so reporting it to an
    # Internal-capped reader would leak the very thing the gate withholds.
    injection = (
        _filter_dicts(res["injection_rows"], args.max_tier)[0]
        if scan_injection else None
    )
    if injection:
        action_required.extend(
            maint.action_required_item(
                f"{row['id']}: {', '.join(row['markers'])}",
                "text concealed from a human reader but visible to a model is "
                "the shape of an indirect prompt injection",
                "read the note by hand; retire it if the hidden text is not yours",
                row["path"],
            )
            for row in injection
            if row["verdict"] == "conceal"
        )
    reads = res["read_log"]
    if reads.get("bulk_reads"):
        action_required.append(maint.action_required_item(
            f"{reads['bulk_reads']} bulk read(s) in the last {reads['days']} days "
            f"(>= {reads['threshold']} notes surfaced in one call)",
            "a single call surfacing hundreds of notes is a sweep, not a question — "
            "the shape an injected agent leaves when it exfiltrates through the gate",
            "review the rows; if none was yours, treat it as an incident",
            reads.get("dir", ""),
        ))
    outcomes = maint.build_outcomes([], action_required, res["blocked"])
    pair_report = {
        "total_pairs": len(pairs),
        "surfaced_pairs": len(gated_pairs),
        "withheld_pairs": len(pairs) - len(gated_pairs),
        "max_tier": args.max_tier,
    }
    if args.json:
        _emit(
            {
                "ritual": "integrity",
                "min_score": res["min_score"],
                "audit": res["audit"],
                "near_dup_pairs": gated_pairs,
                "egress": pair_report,
                "injection": injection,
                "injection_coverage": res.get("injection_coverage"),
                "read_log": reads,
                "outcomes": outcomes,
            },
            True,
        )
    else:
        head = f"integrity -- {pair_report['surfaced_pairs']}/{pair_report['total_pairs']} near-dup pairs surfaced"
        if injection is not None:
            conceal = sum(1 for r in injection if r["verdict"] == "conceal")
            head += (f"; injection scan: {conceal} concealed, "
                     f"{len(injection) - conceal} flagged")
        head += _coverage_lines(res.get("injection_coverage"))
        _emit(None, False, head + "\n" + maint.render_outcomes_markdown(outcomes))
    return 0


def _coverage_lines(coverage: dict | None) -> str:
    """The concealment-scan census, and the one bucket that is a fault.

    Counts, no note names — printed UNGATED on purpose. This is the only line
    that can say a scan was disabled, failed, or never ran for a lane, because
    such a note has 0 hidden runs and never produces a row (V11, 2026-09-02).

    ``unstamped`` gets its own line because it can only ever be a regression:
    the note WAS assessed and its coverage key is missing anyway. Left inside
    the census row it reads as one more resting number beside ``absent``'s
    four digits, which is how a rising regression stays invisible
    (2026-09-03). ``uninspected`` and ``unaccounted`` joined it on 2026-09-04:
    both used to be ``unknown``, which nothing marked (round-6 review,
    MEDIUM).

    Each fault names WHY, and names the one command that lists the notes it
    counted — the state is per note and in its frontmatter, so a grep over the
    vault is the listing, and a count with no way to reach the notes is what
    the review actually complained about.
    """
    from .. import injection_scan as _isc

    if not coverage:
        return ""
    out = ("\n  concealment-scan coverage: "
           + ", ".join(f"{k}={coverage[k]}" for k in _isc.COVERAGE_BUCKETS))
    faults = {k: coverage.get(k, 0) for k in _isc.REGRESSION_BUCKETS
              if coverage.get(k)}
    for bucket, count in faults.items():
        out += (f"\n  REGRESSION: {count} note(s) {_FAULTS[bucket]} "
                f"({bucket}) — list them with: grep -rl "
                f"'concealment_scan: {bucket}' <vault>/vault/"
                if bucket != "unstamped" else
                f"\n  REGRESSION: {count} note(s) {_FAULTS[bucket]} "
                f"({bucket}) — the ingest pipeline stopped recording "
                "concealment coverage; see docs/ingestion.md")
    return out


#: What each fault bucket MEANS, in the operator's words. One line per entry in
#: ``injection_fold.REGRESSION_BUCKETS``, and the census test holds the two
#: lists against each other so a new bucket cannot arrive without one.
_FAULTS = {
    "unstamped": "were assessed and carry no concealment key at all",
    "uninspected": "admitted text a COVERED source's declared walker never "
                   "reported reading",
    "unaccounted": "carry body text that never went through the coverage "
                   "ledger",
}


def _run_promote_scan(args, ctx) -> int:
    core = ctx.core
    res = core.promote_scan(k=args.k)
    surfaced, report = _filter_dicts(res["candidates"], args.max_tier)
    action_required = [
        maint.action_required_item(
            f"{n['id']} is an un-promoted raw/ source",
            "promotion is a human gate (P-10-style); never automatic",
            "review for promotion into a typed brain/ note (brain capture / brain write)",
            n["path"],
        )
        for n in surfaced
    ]
    outcomes = maint.build_outcomes([], action_required, [])
    if args.json:
        _emit(
            {
                "ritual": "promote-scan",
                "candidates": surfaced,
                "pending_drafts": res["pending_drafts"],
                "egress": report,
                "outcomes": outcomes,
            },
            True,
        )
    else:
        head = (
            f"promote-scan -- {report['surfaced']}/{report['total']} candidates surfaced; "
            f"{res['pending_drafts']} pending draft(s)"
        )
        _emit(None, False, head + "\n" + maint.render_outcomes_markdown(outcomes))
    return 0


def _run_sweep_workspace(args, ctx) -> int:
    core = ctx.core

    env_dirs, env_age = maint.workspace_sweep_config()
    dirs = [Path(d).expanduser() for d in args.dirs] if args.dirs else env_dirs
    age = args.age_days if args.age_days else env_age
    if not dirs:
        _emit(
            {
                "error": "no_dirs",
                "detail": "no workspace dirs: pass --dir or set "
                f"${maint.WORKSPACE_SWEEP_DIRS_ENV}",
            }
            if args.json
            else f"no workspace dirs: pass --dir or set "
            f"${maint.WORKSPACE_SWEEP_DIRS_ENV}",
            args.json,
        )
        return 2
    res = maint.sweep_workspace(
        dirs, Path(core.vault) / "inbox", age, dry_run=args.dry_run
    )
    if args.json:
        _emit(res, True)
    else:
        _emit(
            None,
            False,
            f"sweep-workspace [dry_run={res['dry_run']}] age>{res['age_days']}d: "
            f"{len(res['swept'])} swept, {res['skipped_active']} still active, "
            f"{len(res['missing_dirs'])} missing dir(s), "
            f"{len(res['errors'])} error(s)"
            + (
                "\nnext: `brain sync --publish` (or the nightly) drains "
                "inbox/ into signed raw/ notes"
                if res["swept"] and not res["dry_run"]
                else ""
            ),
        )
    return 0


def _render_maintain(res: dict, maint: Any) -> str:
    """Human rendering of a `maintain` result.

    A SKIPPED run carries no `weekday`/`branches_due`. The caller read both
    unconditionally, so the human path raised KeyError('weekday') in exactly
    the case it had something to report — measured 2026-08-18 against a lock
    held by a dead pid. The --json path was unaffected, which is why the
    nightly never surfaced it."""
    if res.get("skipped"):
        return (f"maintain [dry_run={res['dry_run']}] {res['date']} "
                f"skipped ({res['skipped']}): {res.get('note', '')}")
    head = (f"maintain [dry_run={res['dry_run']}] {res['date']} ({res['weekday']}) "
            f"branches_due={res['branches_due']}")
    return head + "\n" + maint.render_outcomes_markdown(res["outcomes"])


def _run_maintain(args, ctx) -> int:
    core = ctx.core
    parsed_date = None
    if args.date:
        import datetime as _dt

        parsed_date = _dt.date.fromisoformat(args.date)
        # Field bug 1 (2026-07-13): a `brain maintain --date <future>` run
        # against a LIVE vault stamped future-dated hot.md idempotency keys,
        # briefs and digests — which then SUPPRESS the legitimate real run
        # for that date and shadow its outputs. A future --date is only ever
        # a deliberate date-gate exercise; refuse it by default so the leak
        # can't happen by accident. (A stuck OS clock can't be caught here —
        # date.today() would already be wrong — but that produces one bad
        # date, not the observed sequence, which was --date leakage.)
        if parsed_date > _dt.date.today() and not args.allow_future_date:
            _emit(
                None,
                False,
                f"refusing --date {parsed_date.isoformat()}: it is AFTER the "
                f"wall-clock date {_dt.date.today().isoformat()}. A future date "
                f"would poison hot.md/brief/digest for that day and suppress the "
                f"real run. Pass --allow-future-date only for a deliberate "
                f"date-gate exercise on a throwaway vault.",
            )
            return 2
    res = core.maintain(
        dry_run=args.dry_run, today=parsed_date, min_score=args.min_score
    )
    if args.json:
        _emit(res, True)
    else:
        _emit(None, False, _render_maintain(res, maint))
    return 0


def _run_graphify(args, ctx) -> int:
    core = ctx.core
    if getattr(args, "progress", False):
        os.environ["BRAIN_PROGRESS"] = "1"
    res = core.graphify(
        force=args.force,
        dry_run=args.dry_run,
        max_tier=args.max_tier,
        candidate_limit=args.n,
        json_mode=args.json,
    )
    if args.json:
        _emit(res, True)
    elif res.get("skipped"):
        _emit(
            None,
            False,
            f"graphify: skipped ({res['skipped']}) — generation {res.get('generation')}",
        )
    elif res.get("status") in ("build_failed", "invalid_artifact"):
        _emit(
            None,
            False,
            f"graphify: {res['status']} — {res.get('error') or res.get('problems')}",
        )
    else:
        corpus = res["corpus"]
        build = res["build"]
        lines = [
            f"-- DISCOVERY-ONLY (non-authoritative); generation={res.get('generation')} "
            f"published={res.get('published')} dry_run={res.get('dry_run', False)}",
            f"-- notes={corpus['note_count']} explicit={corpus['explicit_edge_count']} "
            f"inferred={corpus['inferred_edge_count']} duration={build['duration_seconds']}s",
        ]
        for c in res.get("candidates", []):
            lines.append(
                f"[graph] {c['from']} <-> {c['to']}  score={c['score']}  {c.get('reason', '')}"
            )
        lines.append(
            f"-- {res['egress']['surfaced']}/{res['egress']['total']} candidates surfaced; "
            f"{res['egress']['withheld']} withheld (max-tier={args.max_tier})"
        )
        _emit(None, False, "\n".join(lines))
    return 0


def _run_shelf(args, ctx) -> int:
    """`brain shelf census` — the single public deliverable enumeration.

    Reads frontmatter through `deliverables_sync.census`, NOT the index: the
    capped `bases-query` surface truncates at k=50 and cannot filter on the
    `deliverable` key, so an acceptance check built on it would falsely pass.
    """
    from .. import deliverables_sync

    result = deliverables_sync.census(ctx.core.vault)
    surfaced, report = _filter_dicts(result["entries"], args.max_tier, key="tier")
    if args.json:
        _emit({**result, "entries": surfaced, "egress": report}, True)
    else:
        lines = [f"{e['project_slug']}/  {e['note_id']}  [{e['tier']}]  "
                 f"{e['payload_kind']}" for e in surfaced]
        # Say the surfaced count AND the total. A bare total over an empty list
        # reads as "the census is broken" rather than "your cap hid them", which
        # is the exact misread the elevation hint exists to prevent.
        head = (f"deliverables census: {len(surfaced)} of {result['count']} "
                f"marked (max-tier={args.max_tier})")
        if report.get("withheld"):
            lines.append(f"-- {report['withheld']} withheld")
        if report.get("hint"):
            lines.append(report["hint"])
        _emit(None, False, "\n".join([head, *lines]))
    return 0


_HANDLERS = {
    "shelf": _run_shelf,
    "check": _run_check,
    "health": _run_health,
    "curate": _run_curate,
    "integrity": _run_integrity,
    "promote-scan": _run_promote_scan,
    "sweep-workspace": _run_sweep_workspace,
    "maintain": _run_maintain,
    "graphify": _run_graphify,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
