"""Execute owner-decision commands."""

from __future__ import annotations


from .. import cli as shared

_emit = shared._emit


def _run_inbox(args, ctx) -> int:
    core = ctx.core
    if args.answer is not None:
        if not args.value:
            _emit(None, False, "error: --answer KEY requires --value TEXT")
            return 2
        matched = core.answer_question(args.answer, args.value)
        if args.json:
            _emit({"answered": matched, "key": args.answer}, True)
        else:
            _emit(
                None,
                False,
                (
                    f"recorded answer to {args.answer}"
                    if matched
                    else f"no open question with key {args.answer}"
                ),
            )
        return 0 if matched else 1
    questions = core.open_questions()
    if args.json:
        _emit({"open": questions, "count": len(questions)}, True)
    elif not questions:
        _emit(None, False, "inbox: 0 owner decisions pending.")
    else:
        lines = [f"{len(questions)} owner decision(s) pending:\n"]
        for q in questions:
            lines.append(f"[{q.get('key')}] {q.get('question')}")
            if q.get("context"):
                lines.append(f"    context: {q['context']}")
            for opt in q.get("options", []):
                mark = " (default)" if opt == q.get("default") else ""
                lines.append(f"    - {opt}{mark}")
            lines.append(
                f"    answer: brain inbox --answer {q.get('key')} --value '<option>'\n"
            )
        _emit(None, False, "\n".join(lines))
    return 0


def _run_retro(args, ctx) -> int:
    core = ctx.core
    res = core.retro()
    if args.json:
        _emit(res, True)
    else:
        fnd = res["findings"]
        if not fnd:
            _emit(None, False, "retro: no engine failure signatures found.")
        else:
            lines = [f"retro: {len(fnd)} signature(s) found:"]
            for sig, ev in fnd.items():
                lines.append(f"  - {sig}: {len(ev)} instance(s)")
            if res["feedback_written"]:
                lines.append(
                    f"wrote engine-feedback: {', '.join(res['feedback_written'])}"
                )
            _emit(None, False, "\n".join(lines))
    return 0


_HANDLERS = {
    "inbox": _run_inbox,
    "retro": _run_retro,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
