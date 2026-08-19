"""Execute version-chain commands."""

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


def _run_supersede(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.supersede(args.old_id, args.new_id, reason=args.reason)
    except Exception as exc:  # RoleError / ValueError / KeyUnavailable -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"supersede refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"superseded {res['old_id']} -> {res['new_id']} (both sides signed)",
        args.json,
    )
    return 0


def _run_unsupersede(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.unsupersede(args.old_id, args.new_id, reason=args.reason)
    except Exception as exc:  # RoleError / ValueError / KeyUnavailable -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"unsupersede refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    # Report what actually happened. `unsupersede` repairs the successor
    # OPPORTUNISTICALLY (`new_write` stays None when nothing on that side
    # named old_id), so "both sides signed" was a false audit assurance
    # precisely on the malformed one-sided chains this verb exists for
    # (adversarial review round 3, 2026-08-10).
    if res.get("new_write"):
        how = (
            f"both sides signed: dropped "
            f"{', '.join(res.get('cleared_keys') or []) or 'no keys'} "
            f"from {res['new_id']}"
        )
    else:
        kept = res.get("new_previous_version_kept")
        how = f"ONE side signed ({res['old_id']} only) — {res['new_id']} " + (
            f"names {kept!r} as its predecessor, not {res['old_id']}, "
            "so it was left untouched"
            if kept
            else f"never named {res['old_id']} as its predecessor, so "
            "there was nothing to clear"
        )
    _emit(
        res if args.json else f"unlinked {res['old_id']} -> {res['new_id']} ({how})",
        args.json,
    )
    return 0


_HANDLERS = {
    "supersede": _run_supersede,
    "unsupersede": _run_unsupersede,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
