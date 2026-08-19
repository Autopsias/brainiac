"""Execute owner-facing summary commands."""

from __future__ import annotations

import sys

from .. import cli as shared

_emit = shared._emit


def _html_refused(args, ctx) -> bool:
    """Refuse the brief/digest file-egress surface on the VM leg."""
    if not getattr(args, "html", False) or ctx.role != ctx.config.ROLE_VM:
        return False
    msg = {
        "error": "role_forbidden",
        "role": ctx.role,
        "cmd": f"{args.cmd} --html",
        "detail": f"'{args.cmd} --html' writes a file — host-only; the VM leg "
        "is read+draft only and never gains a filesystem write surface.",
    }
    _emit(
        msg
        if args.json
        else f"refused: '{args.cmd} --html' is host-only (role=vm cannot write files). "
        "Run it on the host.",
        args.json,
    )
    return True


def _run_capture(args, ctx) -> int:
    core = ctx.core
    content = args.content if args.content is not None else sys.stdin.read()
    try:
        res = core.capture(
            content,
            note_id=args.id,
            note_type=args.note_type,
            classification=args.classification,
            reason=args.reason,
        )
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"capture failed ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    if args.json:
        _emit(res, True)
    elif res.get("signed"):
        _emit(
            None,
            False,
            f"captured {res['id']} -> {res['path']} (signed=True, indexed=True)",
        )
    else:
        _emit(
            None,
            False,
            f"draft staged {res['id']} -> {res['draft']} "
            f"(signed=False — VM; host drain will sign + index)",
        )
    return 0


def _run_brief(args, ctx) -> int:
    core = ctx.core
    if _html_refused(args, ctx):
        return 4
    if getattr(args, "html", False):
        res = core.brief_html(
            max_recent=args.n, drain=not args.no_drain, max_tier=args.max_tier
        )
        if args.json:
            _emit(res, True)
        else:
            _emit(
                None,
                False,
                f"brief HTML written -> {res['path']} (latest: {res['latest_path']})",
            )
        return 0
    res = core.brief(max_recent=args.n, drain=not args.no_drain, max_tier=args.max_tier)
    if args.json:
        _emit(res, True)
    else:
        from ..brief import format_brief

        _emit(None, False, format_brief(res))
    return 0


def _run_digest(args, ctx) -> int:
    core = ctx.core
    if _html_refused(args, ctx):
        return 4
    if getattr(args, "html", False):
        res = core.digest_html(days=args.days, max_tier=args.max_tier)
        if args.json:
            _emit(res, True)
        else:
            _emit(
                None,
                False,
                f"digest HTML written -> {res['path']} (latest: {res['latest_path']})",
            )
        return 0
    res = core.digest(days=args.days, max_tier=args.max_tier)
    if args.json:
        _emit(res, True)
    else:
        from ..brief import format_digest

        _emit(None, False, format_digest(res))
    return 0


def _run_health_report(args, ctx) -> int:
    core = ctx.core
    res = core.health_report()
    if args.json:
        _emit(res, True)
    else:
        _emit(
            None,
            False,
            f"health report [{res['verdict']}] written -> {res['path']}"
            + (
                f" ({len(res['act_now'])} item(s) need attention)"
                if res["act_now"]
                else ""
            ),
        )
    return 0 if res["verdict"] != "BROKEN" else 1


def _run_graph_report(args, ctx) -> int:
    core = ctx.core
    res = core.graph_report()
    if args.json:
        _emit(res, True)
    else:
        _emit(
            None,
            False,
            f"graph report written -> {res['path']} "
            f"(gen {res['graph_generation']}, {res['nodes']} nodes, "
            f"{res['edges']} edges, {res['points']} points)",
        )
    return 0


_HANDLERS = {
    "capture": _run_capture,
    "brief": _run_brief,
    "digest": _run_digest,
    "health-report": _run_health_report,
    "graph-report": _run_graph_report,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
