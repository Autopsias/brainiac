"""Execute note-navigation commands."""

from __future__ import annotations


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


def _run_graph_expand(args, ctx) -> int:
    core = ctx.core
    res = core.graph_expand(
        args.seeds,
        depth=args.depth,
        k=args.k,
        use_ppr=not args.no_ppr,
        use_inferred=getattr(args, "use_inferred", False),
    )
    # Egress-gate the DISCOVERY candidates: a withheld note must not leak via
    # the graph surface either. Filter on each candidate's classification.
    surfaced, report = _filter_dicts(res.get("results", []), args.max_tier)
    res["results"] = surfaced
    res["egress"] = report
    if args.json:
        _emit(res, True)
    else:
        lines = [
            f"[graph] {h['id']}  ({h['classification'] or 'UNLABELLED'})  "
            f"hops={h.get('hops')}  ppr={h.get('ppr')}"
            for h in surfaced
        ]
        head = (
            f"-- DISCOVERY-ONLY (non-authoritative); seeds="
            f"{res.get('resolved_seeds')}; method={res.get('method')}"
        )
        footer = _egress_footer(report)
        _emit(None, False, "\n".join([head] + lines + [footer]))
    return 0


def _run_get(args, ctx) -> int:
    core = ctx.core
    note = core.get(args.id)
    items = [note] if note else []
    surfaced, report = _filter_dicts(items, args.max_tier)
    if not note:
        _emit(
            {"error": "not_found", "id": args.id}
            if args.json
            else f"not found: {args.id}",
            args.json,
        )
        return 1
    if not surfaced:
        msg = {"error": "withheld_by_egress_filter", "id": args.id, "egress": report}
        _emit(
            msg
            if args.json
            else f"withheld by egress filter: {args.id} "
            f"(classification={note.get('classification') or 'UNLABELLED'}, "
            f"max-tier={args.max_tier})",
            args.json,
        )
        return 2
    _emit(
        surfaced[0]
        if args.json
        else f"# {surfaced[0]['title']}  ({surfaced[0]['classification']})\n{surfaced[0]['body']}",
        args.json,
    )
    return 0


def _run_recent(args, ctx) -> int:
    core = ctx.core
    items = core.recent(limit=args.n)
    surfaced, report = _filter_dicts(items, args.max_tier)
    if args.json:
        _emit({"results": surfaced, "egress": report}, True)
    else:
        lines = [
            f"{it['updated']}  {it['id']}  ({it['classification'] or 'UNLABELLED'})"
            for it in surfaced
        ]
        lines.append(_egress_footer(report))
        _emit(None, False, "\n".join(lines))
    return 0


_HANDLERS = {
    "graph-expand": _run_graph_expand,
    "get": _run_get,
    "read": _run_get,
    "recent": _run_recent,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
