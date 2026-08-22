"""Execute COS capture commands."""

from __future__ import annotations

import json
import sys

from .. import cli as shared

_emit = shared._emit


def _run_cos_propose(args, ctx) -> int:
    core = ctx.core
    content = args.content if args.content is not None else sys.stdin.read()
    try:
        if args.kind == "correction":
            res = core.cos_propose_correction(json.loads(content))
        else:
            res = core.cos_propose(content, ident=args.id)
    except (ValueError, TypeError) as exc:  # unsafe id / bad payload -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-propose refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"dropped unsigned {args.kind} -> {res.get('proposal') or res.get('drop')} "
        f"(the host broker + owner inbox gate what gets signed)",
        args.json,
    )
    return 0


def _run_cos_run_begin(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_run_begin(
            run_id=args.run_id, lane=args.lane, skill_path=args.skill,
            attended=bool(getattr(args, "attended", False)),
        )
    except Exception as exc:  # RoleError / unresolvable lane -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-run-begin refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"run {res['run_id']} begun: {res.get('bundle_version')} "
        f"(ext {res.get('extraction_rules_version')}) from {res['skill_path']} "
        f"[{res['skill_sha256'][:12]}…]",
        args.json,
    )
    return 0


def _run_cos_corpus_check(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_corpus_check(args.run_id)
    except Exception as exc:  # NoBodiesToJudge / RoleError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-corpus-check REFUSED ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"cos-corpus-check: {res['judgeable']} of {res['rows']} row(s) "
        f"carry body text ({res['bodyless']} bodyless) — judging may "
        f"proceed over the {res['judgeable']} bodied row(s)",
        args.json,
    )
    return 0


def _run_cos_corpus_append(args, ctx) -> int:
    core = ctx.core
    try:
        if bool(args.conversation_id) == bool(args.bodyless):
            raise ValueError(
                "give exactly ONE of --conversation-id (one thread whose "
                "body was opened, its text on stdin) or --bodyless (the "
                "threads that were enumerated and never opened)"
            )
        if args.conversation_id:
            text = args.text if args.text is not None else sys.stdin.read()
            if not text.strip():
                # A row asserting an opened body with nothing in it is
                # exactly run 65's shape — a read that did not happen,
                # recorded as one that did.
                raise ValueError(
                    f"no message text for {args.conversation_id!r}. A row "
                    f"claiming an opened body with nothing in it is a read "
                    f"that did not happen; use --bodyless for a thread "
                    f"that was never opened."
                )
            rows = [
                {
                    "conversation_id": args.conversation_id,
                    "text": text,
                    "sender": args.sender,
                    "sent": args.sent,
                    "subject": args.subject,
                    "read_lane": args.read_lane,
                    "body_opened": True,
                }
            ]
        else:
            rows = [
                {
                    "conversation_id": c,
                    "text": "",
                    "read_lane": args.read_lane,
                    "body_opened": False,
                }
                for c in args.bodyless
            ]
        res = core.cos_corpus_append(args.run_id, rows)
    except Exception as exc:  # CorpusRefused / RoleError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-corpus-append REFUSED ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"cos-corpus-append: {res['appended']} row(s) -> {res['run']} "
        f"({res['chars']} chars of message text)",
        args.json,
    )
    return 0


def _run_cos_corpus_close(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_corpus_close(args.run_id)
    except Exception as exc:  # CorpusClosed / RoleError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-corpus-close REFUSED ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"cos-corpus-close: {res['run']} closed with {res['rows']} row(s) "
        f"— read-only from here; retention deletes it whole",
        args.json,
    )
    return 0


def _run_cos_corpus_reopen(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_corpus_reopen(args.run_id)
    except Exception as exc:  # CorpusRefused / RoleError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-corpus-reopen REFUSED ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"cos-corpus-reopen: {res['run']} is open again — {res['reason']}"
        f". The false close stays on the file; keep appending, then "
        f"close for real.",
        args.json,
    )
    return 0


_HANDLERS = {
    "cos-propose": _run_cos_propose,
    "cos-run-begin": _run_cos_run_begin,
    "cos-corpus-check": _run_cos_corpus_check,
    "cos-corpus-append": _run_cos_corpus_append,
    "cos-corpus-close": _run_cos_corpus_close,
    "cos-corpus-reopen": _run_cos_corpus_reopen,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
