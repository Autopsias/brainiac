"""Execute signed-ingest commands."""

from __future__ import annotations

import sys

from .. import classification as cls
from .. import cli as shared

_emit = shared._emit
_filter_dicts = shared._filter_dicts


def _run_ingest(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.ingest_dropzone(dry_run=args.dry_run)
    except Exception as exc:  # RoleError -> fail closed, zero side effects
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"ingest refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    # Egress (ADR-0003 Ruling 8): the report lists promoted note ids +
    # classifications, so it joins the content-returning surface — route
    # the processed list through the same gate as curate/integrity.
    if not args.dry_run and res.get("processed"):
        surfaced, egress_report = _filter_dicts(
            res["processed"],  # each entry already carries its real
            # promoted-note classification (pipeline.py)
            cls.DEFAULT_MAX_TIER,
        )
        res["processed"] = surfaced
        res["egress"] = egress_report
    # E4: "duplicates" carries `existing_id` (a real note id, possibly
    # above max tier) via `existing_id`/`classification` — C8 only routed
    # "processed" through the gate, leaving this sub-list to bypass it.
    if not args.dry_run and res.get("duplicates"):
        dup_surfaced, dup_egress = _filter_dicts(
            res["duplicates"], cls.DEFAULT_MAX_TIER
        )
        res["duplicates"] = dup_surfaced
        res["duplicates_egress"] = dup_egress
    if args.json:
        _emit(res, True)
    else:
        _emit(
            None,
            False,
            f"ingest [dry_run={res['dry_run']}]: "
            f"processed={len(res.get('processed', []))} "
            f"quarantined={len(res.get('quarantined', []))} "
            f"duplicates={len(res.get('duplicates', []))} "
            f"skipped={len(res.get('skipped', []))}",
        )
    return 0


def _run_ingest_transcript(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.ingest_transcript(
            args.path,
            origin=args.origin,
            language=args.language,
            document_date=args.document_date,
            classification=args.classification,
        )
    except Exception as exc:  # RoleError -> fail closed, zero side effects
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"ingest-transcript refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    # Egress (ADR-0003 Ruling 8, mirrors `ingest`): a fresh promotion's
    # result carries a real note id + classification, so it joins the
    # content-returning surface even though it is a single dict, not a
    # list — reuse the same gate via a one-element wrap.
    if res.get("ok") and not res.get("duplicate") and res.get("id"):
        surfaced, egress_report = _filter_dicts([res], cls.DEFAULT_MAX_TIER)
        res = (
            surfaced[0] if surfaced else {"withheld": True, "reason": "above max-tier"}
        )
        res["egress"] = egress_report
    if args.json:
        _emit(res, True)
    else:
        if not res.get("ok"):
            _emit(None, False, f"ingest-transcript failed: {res.get('reason')}")
        elif res.get("duplicate"):
            _emit(
                None,
                False,
                f"ingest-transcript: duplicate of raw/{res.get('existing_id')}.md",
            )
        else:
            _emit(
                None,
                False,
                f"ingest-transcript: {res.get('note')} (origin={args.origin})",
            )
    return 0 if res.get("ok", True) else 3


def _run_write(args, ctx) -> int:
    core = ctx.core
    content = args.content if args.content is not None else sys.stdin.read()
    try:
        res = core.write_note(args.relpath, content, reason=args.reason)
    except Exception as exc:  # KeyUnavailable / ValueError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"write refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(res if args.json else f"wrote {res['written']} (audited)", args.json)
    return 0


def _run_audit_key(args, ctx) -> int:
    from .. import audit

    try:
        res = audit.provision_signing_key()
    except Exception as exc:  # KeyUnavailable -> report, don't traceback
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"audit key: unavailable ({exc})",
            args.json,
        )
        return 1
    _emit(
        res
        if args.json
        else f"audit key: {res['status']} ({res.get('source') or res.get('store')})",
        args.json,
    )
    return 0


def _run_verify_audit(args, ctx) -> int:
    core = ctx.core
    res = core.verify_audit(check_content=args.check_content)
    text = (
        f"audit chain: {res['status']} ({res['entries_checked']} entries, "
        f"{len(res['errors'])} errors)"
    )
    # INT-02: never let a signature-only pass read as a content all-clear.
    unexplained = res.get("content_drift_unexplained", 0)
    text += (
        f"\ncontent drift: {res.get('content_drift_count', 0)} note(s) changed "
        f"since signing, {unexplained} unexplained"
    )
    if not args.check_content:
        text += " — run `brain verify-audit --check-content --json` for the list"
    elif res.get("content_drift"):
        for rec in res["content_drift"]:
            mark = rec.get("disposition") or "UNEXPLAINED"
            text += f"\n  {mark:<24} {rec['issue']:<14} {rec['path']}"
    _emit(res if args.json else text, args.json)
    return 0 if res["status"] in ("ok", "empty") else 1


def _run_anchor(args, ctx) -> int:
    core = ctx.core
    res = core.anchor_chain(args.anchor_dir)
    rec = res["record"]
    _emit(
        res
        if args.json
        else f"anchored head {rec['head'][:16]}… @ {rec['entry_count']} entries "
        f"-> {res['anchor_log']}",
        args.json,
    )
    return 0


def _run_verify_anchor(args, ctx) -> int:
    core = ctx.core
    res = core.verify_anchor(args.anchor_dir)
    _emit(
        res
        if args.json
        else f"anchor: {res['status']} ({res['checked']} records checked, "
        f"{len(res['divergences'])} divergences)",
        args.json,
    )
    return 0 if res["status"] in ("ok", "no-anchor") else 1


def _run_backup(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.backup(args.dest, encrypt=not args.no_encrypt)
    except Exception as exc:  # EncryptionKeyUnavailable etc. -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"backup refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"backup ({'encrypted' if res['encrypted'] else 'PLAINTEXT'}) "
        f"{res['files']} files -> {res['archive']} "
        f"(sha256 {res['plaintext_sha256'][:16]}…)",
        args.json,
    )
    return 0


def _run_restore(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.restore(args.archive, args.dest)
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"restore failed ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"restored {res['files']} files -> {res['dest']} "
        f"(sha256 {res['plaintext_sha256'][:16]}…)",
        args.json,
    )
    return 0


_HANDLERS = {
    "ingest": _run_ingest,
    "ingest-transcript": _run_ingest_transcript,
    "write": _run_write,
    "audit-key": _run_audit_key,
    "verify-audit": _run_verify_audit,
    "anchor": _run_anchor,
    "verify-anchor": _run_verify_anchor,
    "backup": _run_backup,
    "restore": _run_restore,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
