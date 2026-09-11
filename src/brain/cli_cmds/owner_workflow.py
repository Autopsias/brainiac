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


def _run_interview(args, ctx) -> int:
    import datetime as _dt                                    # noqa: PLC0415
    from pathlib import Path                                  # noqa: PLC0415

    from .. import interview as _iv                           # noqa: PLC0415
    from .. import interview_apply as _ia                     # noqa: PLC0415

    core = ctx.core
    today = _dt.date.fromisoformat(args.date) if args.date else None
    out: dict = {}
    if args.answer is not None:
        if not args.action:
            _emit(None, False, "error: --answer KEY requires --action ACTION")
            return 2
        try:
            out["answer"] = _ia.apply_answer(core, args.answer, args.action,
                                             args.note or "", today)
        except ValueError as exc:
            _emit(None, False, f"error: {exc}")
            return 1
    if args.apply or args.nightly:
        out["apply"] = _ia.apply_answers(core, today)
    if args.generate or args.nightly:
        gen = _iv.generate(core, today)
        out["generate"] = gen
        if args.prompt_out and gen["keys"]:
            rows = [r for r in _iv.open_rows(_iv.read_state(core.vault))
                    if r["key"] in gen["keys"]]
            d = Path(args.prompt_out)
            d.mkdir(parents=True, exist_ok=True)
            (d / "prompt.txt").write_text(_iv.phrase_prompt(core.vault, rows),
                                          encoding="utf-8")
            out["prompt"] = str(d / "prompt.txt")
    if not out:
        out = {"open": _iv.sheet_rows(_iv.read_state(core.vault))}
    if args.json:
        _emit(out, True)
        return 0
    if "open" in out:
        rows = out["open"]
        lines = [f"interview: {len(rows)} open question(s)"]
        for r in rows:
            lines.append(f"[{r['key']}] {r['shape']}: {r['question']}")
        _emit(None, False, "\n".join(lines))
        return 0
    a = out.get("apply") or out.get("answer") or {}
    g = out.get("generate") or {}
    _emit(None, False,
          f"interview: applied {a.get('applied', 0)}, skipped {a.get('skipped', 0)}, "
          f"failed {len(a.get('failed') or [])}; asked {g.get('asked', 0)} "
          f"(room {(g.get('budget') or {}).get('room', 0)}), "
          f"detector errors {len(g.get('errors') or [])}")
    return 0


_HANDLERS = {
    "inbox": _run_inbox,
    "retro": _run_retro,
    "interview": _run_interview,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
