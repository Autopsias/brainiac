"""Execute retrieval evaluation commands."""

from __future__ import annotations

import json

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


def _run_eval(args, ctx) -> int:
    core = ctx.core
    # `eval` is absent from VM_ALLOWED, so this host-only replay branch is
    # reached only after the pre-core trust gate above.  It invokes the
    # engine directly rather than the CLI search path, therefore it can
    # never append a new real-traffic capture record while replaying.
    from .. import querylog

    if args.eval_cmd == "replay":
        try:
            report, thresholds_failed = querylog.replay(
                core,
                args.against,
                fail_under_top1=args.fail_under_top1,
                fail_under_jaccard=args.fail_under_jaccard,
            )
        except (querylog.ReplayDataError, ValueError) as exc:
            payload = {"error": "replay_data", "detail": str(exc)}
            _emit(payload if args.json else f"replay error: {exc}", args.json)
            return 2
        _emit(
            report if args.json else None,
            args.json,
            None if args.json else json.dumps(report, ensure_ascii=False, indent=2),
        )
        return 1 if thresholds_failed else 0


_HANDLERS = {
    "eval": _run_eval,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
