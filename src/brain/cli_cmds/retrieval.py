"""Execute ranked retrieval commands."""

from __future__ import annotations

import os
import time

from .. import classification as cls
from .. import cli as shared

_emit = shared._emit
_excluded_note = shared._excluded_note
_filter_dicts = shared._filter_dicts
_freshness_block = shared._freshness_block
_egress_footer = shared._egress_footer
_variant_block = shared._variant_block
_render_variant_block = shared._render_variant_block
_render_explain_hit = shared._render_explain_hit
_render_diagnose = shared._render_diagnose
_capture_rerank_metadata = shared._capture_rerank_metadata


def _search_hits(args, core, capture_enabled: bool):
    """Run the selected single-query or fan-out ranking primitive."""
    trace = None
    fanout = None
    # RET-05: the ORIGINAL query is always variant 0 — every identity and
    # create-safety guarantee is anchored to it (see core.search_multi).
    # With no --variant this branch is not taken at all, so the single-query
    # ranking is untouched, byte for byte.
    variants = [args.query] + list(getattr(args, "variant", None) or [])
    if len(variants) > 1:
        fan_hits, fanout = core.search_multi(
            variants,
            k=args.k,
            rerank=args.rerank,
            rerank_top=args.rerank_top,
            rrf_k=args.rrf_k,
            rerank_gate=args.rerank_gate,
            rerank_fused=args.rerank_fused,
            return_trace=True,
        )
        hits = [hit.to_dict() for hit in fan_hits]
    elif args.explain or capture_enabled:
        trace_hits, trace = core.hybrid_search_with_trace(
            args.query,
            k=args.k,
            rerank=args.rerank,
            rerank_top=args.rerank_top,
            rrf_k=args.rrf_k,
            rerank_gate=args.rerank_gate,
        )
        hits = [hit.to_dict() for hit in trace_hits]
    else:
        hits = [
            h.to_dict()
            for h in core.hybrid_search(
                args.query,
                k=args.k,
                rerank=args.rerank,
                rerank_top=args.rerank_top,
                rrf_k=args.rrf_k,
                rerank_gate=args.rerank_gate,
            )
        ]
    return hits, trace, fanout


def _annotate_explain(
    args, trace, surfaced: list[dict], redacted_ids: set[str]
) -> None:
    """Attach post-egress ranking attribution to visible results."""
    if not args.explain or trace is None:
        return
    for final_rank, hit in enumerate(surfaced, start=1):
        hit["explain"] = trace.explain_for_id(
            hit["id"],
            final_rank,
            redact_identity=hit["id"] in redacted_ids,
        )


def _search_payload(args, trace, fanout, surfaced: list[dict], report: dict) -> dict:
    """Build the post-egress search response payload."""
    if args.explain and trace is not None:
        payload = {
            "query": args.query,
            "ranking": {
                "rrf_k": trace.rrf_k,
                "exact_leg_enabled": trace.exact_leg_enabled,
                "rerank_requested": trace.rerank_requested,
                "rerank_applied": trace.rerank_applied,
                "rerank_gate": trace.rerank_gate,
                "family_collapse": trace.family_collapse,
            },
            "results": surfaced,
            "candidate_digest": trace.compact_digest({hit["id"] for hit in surfaced}),
            "egress": report,
        }
    else:
        payload = {
            "query": args.query,
            "rerank": args.rerank,
            "results": surfaced,
            "egress": report,
        }
    if fanout is not None:
        payload["variants"] = _variant_block(
            fanout,
            {hit["id"] for hit in surfaced},
            explain=args.explain,
        )
    return payload


def _capture_search(
    args,
    ctx,
    querylog,
    capture_started,
    trace,
    fanout,
    surfaced: list[dict],
    redacted_ids: set[str],
) -> None:
    """Append the bounded post-egress query record when capture is enabled."""
    if capture_started is None or fanout is not None:
        return
    capture_top, capture_digest = querylog.projection_from_gated(
        surfaced,
        trace=trace,
        redacted_ids=redacted_ids,
    )
    querylog.capture_post_egress(
        vault=ctx.core.vault,
        role=ctx.role,
        index=ctx.core.index,
        query=args.query,
        mode=args.cmd,
        k=args.k,
        rrf_k=args.rrf_k,
        exact_leg_enabled=bool(getattr(trace, "exact_leg_enabled", False)),
        rerank=_capture_rerank_metadata(ctx.core, trace, args),
        latency_ms=(time.perf_counter() - capture_started) * 1000,
        top=capture_top,
        candidate_digest=capture_digest,
        max_tier=args.max_tier,
    )


def _render_search(
    args,
    payload: dict,
    surfaced: list[dict],
    report: dict,
    fanout,
    notice: str | None,
    freshness: dict | None,
) -> str:
    """Render the human search response."""
    if args.explain and fanout is None:
        lines = [line for hit in surfaced for line in _render_explain_hit(hit)]
    else:
        lines = [
            f"[{hit['source']}] {hit['id']}  <{hit.get('type') or '?'}>"
            f"  ({hit['classification'] or 'UNLABELLED'})"
            f"  {hit.get('date') or 'undated'}  "
            f"{hit['score'] if hit.get('score') is not None else 'redacted'}"
            f"\n    {hit['snippet']}"
            for hit in surfaced
        ]
    footer = _egress_footer(report)
    if fanout is not None:
        footer += "\n" + "\n".join(_render_variant_block(payload["variants"]))
    if notice:
        footer += f"\n-- {notice}"
    if freshness and freshness.get("hint"):
        footer += f"\n-- {freshness['hint']}"
    return "\n".join(lines + [footer]) if lines else footer


def _run_search(args, ctx) -> int:
    """Coordinate retrieval, egress, observability, and presentation."""
    from .. import querylog

    core = ctx.core
    # S02/CS-01: expose a cold hash-placeholder index as FTS-only rather than
    # letting an agent conclude the vault is thin.
    embedder_pending = core.embedder_pending()
    capture_enabled = querylog.capture_requested(ctx.role)
    capture_started = time.perf_counter() if capture_enabled else None
    hits, trace, fanout = _search_hits(args, core, capture_enabled)
    surfaced, report = _filter_dicts(hits, args.max_tier)
    # ADR-0008: identity ownership is computed before egress, but its
    # create/no-create conclusion must be finalized after the gate so a
    # withheld collision can only yield the conservative ``unknown`` enum.
    identity_redacted_ids = core.annotate_create_safety(
        args.query,
        surfaced,
        args.max_tier,
    )
    freshness = _freshness_block(core, surfaced, args.max_tier)
    notice = (
        "embedder pending — dense/semantic ranking is skipped (FTS-only "
        "results) until the real model is applied to this index; run "
        "`brain warmup` then `brain sync`."
        if embedder_pending
        else None
    )
    _annotate_explain(args, trace, surfaced, identity_redacted_ids)
    payload = _search_payload(args, trace, fanout, surfaced, report)
    _capture_search(
        args,
        ctx,
        querylog,
        capture_started,
        trace,
        fanout,
        surfaced,
        identity_redacted_ids,
    )
    if args.json:
        if freshness:
            payload["freshness"] = freshness
        if notice:
            payload["embedder_notice"] = notice
        _emit(payload, True)
    else:
        _emit(
            None,
            False,
            _render_search(
                args,
                payload,
                surfaced,
                report,
                fanout,
                notice,
                freshness,
            ),
        )
    return 0


def _run_diagnose(args, ctx) -> int:
    core = ctx.core
    # The trace-returning call executes the same production candidate cut,
    # scoring, suppression and reranking as search.  Only after that work
    # is complete do we inspect the requested target out of band.
    trace_hits, trace = core.hybrid_search_with_trace(
        args.query,
        k=args.k,
        rerank=args.rerank,
        rerank_top=args.rerank_top,
        rrf_k=args.rrf_k,
    )
    hits = [hit.to_dict() for hit in trace_hits]
    surfaced, report = _filter_dicts(hits, args.max_tier)
    core.annotate_create_safety(args.query, surfaced, args.max_tier)
    final_ranks = {hit["id"]: rank for rank, hit in enumerate(surfaced, start=1)}
    diagnosis = core.diagnose_target(
        args.query,
        args.target,
        max_tier=args.max_tier,
        trace=trace,
        final_rank=final_ranks.get(args.target),
    )
    if args.json:
        payload = {**diagnosis, "egress": report}
        # The strict withheld response intentionally omits query/target
        # metadata beyond the public sentinel and aggregate gate count.
        if diagnosis.get("verdict") != "withheld":
            payload = {"query": args.query, **payload}
        _emit(payload, True)
    else:
        _emit(None, False, _render_diagnose(diagnosis, report))
    return 0


def _run_dossier(args, ctx) -> int:
    core = ctx.core
    role = ctx.role
    from .. import querylog

    capture_enabled = querylog.capture_requested(role)
    capture_started = time.perf_counter() if capture_enabled else None
    res = core.dossier(args.query, k=args.k)
    decisions, drep = _filter_dicts(res["decisions"], args.max_tier)
    sources, srep = _filter_dicts(res["sources"], args.max_tier)
    # The targeted decision-layer probe can add a hit outside hybrid's
    # normal pool. Finalize the same post-egress identity conclusion over
    # both dossier layers so a withheld identity owner stays unknown.
    core.annotate_create_safety(args.query, decisions + sources, args.max_tier)
    report = {
        "total": drep["total"] + srep["total"],
        "surfaced": drep["surfaced"] + srep["surfaced"],
        "withheld": drep["withheld"] + srep["withheld"],
        "withheld_unlabelled_default_deny": drep["withheld_unlabelled_default_deny"]
        + srep["withheld_unlabelled_default_deny"],
        "max_tier": args.max_tier,
    }
    if (
        report["withheld"] > 0
        and args.max_tier != cls.TIERS[-1]
        and not shared._SUPPRESS_ELEVATION_HINT
    ):
        report["hint"] = (
            f"{report['withheld']} note(s) withheld above the "
            f"{args.max_tier} cap — re-run with a higher --max-tier."
        )
    freshness = _freshness_block(core, decisions + sources, args.max_tier)
    payload = {
        "query": res["query"],
        "decisions": decisions,
        "sources": sources,
        "retired_excluded": res["retired_excluded"],
        "egress": report,
    }
    if freshness:
        payload["freshness"] = freshness
    if capture_enabled and capture_started is not None:
        # A dossier composes hybrid candidates with a targeted decision
        # probe, so it has no one production trace to expose.  The shared
        # serializer still produces the bounded S03-compatible final-list
        # digest from its gated decision/source response.
        capture_top, capture_digest = querylog.projection_from_gated(
            decisions + sources,
        )
        querylog.capture_post_egress(
            vault=core.vault,
            role=role,
            index=core.index,
            query=args.query,
            mode="dossier",
            k=args.k,
            rrf_k=60,
            exact_leg_enabled=os.environ.get("BRAIN_EXACT_LEG_ENABLED", "1")
            .strip()
            .lower()
            not in {"0", "false", "no", "off"},
            rerank={"requested": False, "applied": False, "model": None, "top_n": 0},
            latency_ms=(time.perf_counter() - capture_started) * 1000,
            top=capture_top,
            candidate_digest=capture_digest,
            max_tier=args.max_tier,
        )
    if args.json:
        _emit(payload, True)
    else:
        lines = [f"== decision layer ({len(decisions)}) =="]
        for h in decisions:
            lines.append(
                f"  {h['id']}  ({h['classification']})  {h.get('date') or 'undated'}"
            )
            for x in h.get("tensions", []):
                ident = x.get("identity", "")
                caveat = (
                    " [identity: %s — title/calendar-derived, weigh accordingly]"
                    % ident
                    if ident and ident not in ("content-verified", "filename")
                    else ""
                )
                lines.append(
                    f"    !! newer source post-dates this decision: "
                    f"{x['id']} ({x['date']}){caveat} — report the tension, "
                    f"never promote the proposal"
                )
        lines.append(f"== sources under consideration ({len(sources)}) ==")
        lines += [
            f"  {h['id']}  <{h.get('type') or '?'}>  {h.get('date') or 'undated'}"
            for h in sources
        ]
        if res["retired_excluded"]:
            lines.append(f"-- {res['retired_excluded']} retired version(s) excluded")
        footer = _egress_footer(report)
        if freshness and freshness.get("hint"):
            footer += f"\n-- {freshness['hint']}"
        _emit(None, False, "\n".join(lines + [footer]))
    return 0


def _run_grep(args, ctx) -> int:
    core = ctx.core
    items = core.grep(args.pattern, k=args.k, regex=args.regex)
    surfaced, report = _filter_dicts(items, args.max_tier)
    if args.json:
        _emit({"pattern": args.pattern, "results": surfaced, "egress": report}, True)
    else:
        lines = [
            f"{h['id']} ({h['classification'] or 'UNLABELLED'}) "
            f"x{h['match_count']}\n    {h['snippet']}"
            for h in surfaced
        ]
        footer = _egress_footer(report)
        _emit(None, False, "\n".join(lines + [footer]) if lines else footer)
    return 0


def _run_bases_query(args, ctx) -> int:
    core = ctx.core
    filters: dict[str, str] = {}
    for clause in args.where:
        if "=" in clause:
            key, val = clause.split("=", 1)
            filters[key.strip()] = val.strip()
    items = core.bases_query(
        filters, k=args.k, latest_only=args.latest_only, as_of=args.as_of
    )
    surfaced, report = _filter_dicts(items, args.max_tier)
    if args.json:
        _emit({"filters": filters, "results": surfaced, "egress": report}, True)
    else:
        lines = [
            f"{h['id']}  type={h.get('type', '?')}  ({h['classification'] or 'UNLABELLED'})"
            for h in surfaced
        ]
        footer = _egress_footer(report)
        _emit(None, False, "\n".join(lines + [footer]) if lines else footer)
    return 0


_HANDLERS = {
    "search": _run_search,
    "hybrid-search": _run_search,
    "diagnose": _run_diagnose,
    "dossier": _run_dossier,
    "grep": _run_grep,
    "bases-query": _run_bases_query,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
