"""Execute note-navigation commands."""

from __future__ import annotations


from .. import cli as shared

_emit = shared._emit
_excluded_note = shared._excluded_note
_filter_dicts = shared._filter_dicts
_freshness_block = shared._freshness_block
_egress_footer = shared._egress_footer
_concealment_notice = shared._concealment_notice
_header_line = shared._header_line
_variant_block = shared._variant_block
_render_variant_block = shared._render_variant_block
_render_explain_hit = shared._render_explain_hit
_render_diagnose = shared._render_diagnose
_capture_rerank_metadata = shared._capture_rerank_metadata


def _run_graph_expand(args, ctx) -> int:
    """Wikilink/PPR discovery rows. DELIBERATELY UNSTAMPED, not an oversight.

    A discovery row is a POINTER — id, classification, hop count, PPR score —
    and carries no note content at all, so there is nothing here for concealed
    text to reach a reader through. The verdict rides on the `get` or `search`
    that follows, which is where the body actually arrives. Adding a column to
    the walk would mean joining `notes` for every candidate at every hop to
    print a value beside no text. Recorded here because a reviewer read the
    absence as a gap (adversarial review N1, 2026-09-04).

    The human render below still calls the shared notice: if a future change
    ever puts the field on these rows, the warning is already wired.
    """
    core = ctx.core
    res = core.graph_expand(
        args.seeds,
        depth=args.depth,
        k=args.k,
        use_ppr=not args.no_ppr,
        use_inferred=getattr(args, "use_inferred", False),
        max_tier=args.max_tier,  # broker parity: above-ceiling notes never enter the walk
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
            + (f"\n{_concealment_notice(h)}" if _concealment_notice(h) else "")
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
    surfaced, _report = _filter_dicts(items, args.max_tier)
    if not surfaced:
        # Absent and withheld answer the SAME way (broker parity, owner-approved
        # 2026-09-01): a capped caller must not learn that an id exists above
        # its ceiling, nor its tier. The host read record still counts the
        # withheld note; only the caller-visible answer is normalised. Until
        # then this exited 2 with the classification in the message.
        _emit(
            {"error": "not_found", "id": args.id}
            if args.json
            else f"not found: {args.id}",
            args.json,
        )
        return 1
    # The notice goes ABOVE the body, not below it: `get` prints the whole
    # note, and a warning after a long body is a warning nobody reads.
    notice = _concealment_notice(surfaced[0])
    _emit(
        surfaced[0]
        if args.json
        else (f"# {surfaced[0]['title']}  ({surfaced[0]['classification']})\n"
              + (notice.strip() + "\n" if notice else "")
              + surfaced[0]['body']),
        args.json,
    )
    return 0


def _run_recent(args, ctx) -> int:
    core = ctx.core
    items = core.recent(
        limit=args.n,
        include_retired=getattr(args, "include_retired", False),
    )
    surfaced, report = _filter_dicts(items, args.max_tier)
    if args.json:
        _emit({"results": surfaced, "egress": report}, True)
    else:
        lines = [
            f"{it['updated']}  {it['id']}  ({it['classification'] or 'UNLABELLED'})"
            + _header_line(it)
            + (f"\n{_concealment_notice(it)}" if _concealment_notice(it) else "")
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
