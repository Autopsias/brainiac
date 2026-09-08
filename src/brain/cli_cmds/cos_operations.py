"""Execute COS broker commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .. import cli as shared

_emit = shared._emit


def _run_cos_broker(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_broker_fold()
    except Exception as exc:  # RoleError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-broker refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    if args.json:
        _emit(res, True)
    else:
        claimed = res.get("claimed", {}) or {}
        consumed = res.get("consumed", {}) or {}
        batch = res.get("batch", {}) or {}
        _emit(
            None,
            False,
            f"cos-broker: claimed={len(claimed.get('claimed', []))} "
            f"rejected={len(claimed.get('rejected', []))} "
            f"accepted->capture-inbox={len(consumed.get('accepted', []))} "
            f"holds-released={len(res.get('holds_released', []))} "
            f"batch-enqueued={batch.get('enqueued', False)} "
            f"errors={len(res.get('errors', []))}",
        )
    return 0 if not _fatal_broker_errors(res) else 1


# The broker's stage-isolated fold mixes MUTATING claim/consume work with
# read-only enrichment (version links, grounding pack, spine render, gc).
# Only a mutating-stage failure is worth a non-zero exit: the nightly chain
# treats any broker exit != 0 as a dead session and abandons the night's
# remaining batches, and on 2026-08-31 three chains were lost to an
# OperationalError in the version_links ENRICHMENT stage while every claim
# and consume had succeeded. Enrichment errors stay visible in the JSON
# `errors` list and in maintain's blocked items — they just do not cost the
# chain its next batch.
_FATAL_BROKER_STAGES = frozenset(
    {"run_validity", "claimed", "batch_expired", "proposals_expired", "consumed"}
)


def _fatal_broker_errors(res: dict[str, Any]) -> list[str]:
    return [
        e
        for e in res.get("errors", [])
        if e.split(":", 1)[0] in _FATAL_BROKER_STAGES
    ]


def _run_cos_correct(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_correct(args.round_, args.msg_key, args.bucket, args.tier)
    except Exception as exc:  # RoleError / ValueError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-correct refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"correction recorded: round={res['round']} msg={res['msg_key']} "
        f"-> {res['corrected_bucket']}/{res['corrected_tier']}",
        args.json,
    )
    return 0


def _run_cos_evidence(args, ctx) -> int:
    core = ctx.core
    try:
        if args.action == "sign":
            missing = [
                f
                for f in ("bundle_version", "model_version", "dataset_window")
                if not getattr(args, f)
            ]
            if missing:
                raise ValueError(f"sign requires --{missing[0].replace('_', '-')}")
            from pathlib import Path as _P

            res = core.cos_evidence_sign(
                bundle_version=args.bundle_version,
                model_version=args.model_version,
                dataset_window=args.dataset_window,
                files=[_P(f) for f in args.files],
                name=args.name,
            )
        else:
            if not args.dir:
                raise ValueError("verify requires --dir")
            res = core.cos_evidence_verify(args.dir)
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-evidence refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    if args.action == "verify":
        _emit(
            res
            if args.json
            else f"evidence: {'VALID' if res['ok'] else 'INVALID'} "
            f"({len(res['errors'])} error(s))"
            + ("".join(f"\n  - {e}" for e in res["errors"])),
            args.json,
        )
        return 0 if res["ok"] else 1
    _emit(res if args.json else f"signed evidence bundle -> {res['dir']}", args.json)
    return 0


def _run_cos_priority_map(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_priority_map(max_tier=args.max_tier)
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-priority-map refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"priority map -> {res['path']} ({res['people']} people, "
        f"{res['companies']} companies, {res['withheld']} withheld at "
        f"max-tier={res['max_tier']})",
        args.json,
    )
    return 0


def _run_cos_report(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_report()
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-report refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"cos-report: rounds={res['rounds_completed']} "
        f"verdicts={res['verdicts']} corrections={res['corrections']} "
        f"overall-bucket-precision={res['overall_bucket_precision']}",
        args.json,
    )
    return 0


def _run_cos_ingest_sweep(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.cos_ingest_sweep(
            downloads_dir=args.downloads_dir, dry_run=args.dry_run
        )
    except Exception as exc:  # RoleError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-ingest-sweep refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"cos-ingest-sweep{' (dry-run)' if args.dry_run else ''}: "
        f"moved={len(res['moved'])} unmatched={len(res['unmatched'])} "
        f"refused={len(res['refused'])} "
        f"already-claimed={res['already_claimed']}",
        args.json,
    )
    return 0


def _run_cos_hold(args, ctx) -> int:
    core = ctx.core
    try:
        if args.action == "add":
            if not getattr(args, "not_before", None):
                raise ValueError("add requires --not-before <ISO timestamp>")
            content = args.content if args.content is not None else sys.stdin.read()
            res: Any = core.cos_hold_add(
                content, not_before=args.not_before, ident=args.id
            )
            human = (
                f"held {res['id']} until {res['not_before']} "
                f"(unsigned; enters capture-inbox only after expiry)"
            )
        elif args.action == "list":
            res = {"holds": core.cos_hold_list()}
            human = (
                "\n".join(
                    f"{h.get('id')}  not_before={h.get('not_before')}  due={h.get('due')}"
                    for h in res["holds"]
                )
                or "no holds"
            )
        elif args.action == "cancel":
            if not args.id:
                raise ValueError("cancel requires --id")
            undo = core.cos_hold_undo(args.id)
            res = {**undo, "cancelled": undo["undone"]}
            if undo["undone"]:
                human = (
                    f"undo of {undo['id']} from state "
                    f"{undo['state_before']}: {undo['action']}"
                )
                if undo.get("demoted"):
                    human += (
                        f" (category {undo['demoted']['category']} "
                        f"demoted from auto-ingest)"
                    )
            else:
                human = (
                    f"nothing to undo for {undo['id']} (state={undo['state_before']})"
                )
        else:  # release-due
            released = core.cos_hold_release_due()
            res = {"released": released}
            human = (
                f"released {len(released)} due hold(s) into the "
                f"approved queue (host-only; signed on the next drain)"
                if released
                else "no due holds"
            )
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-hold refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(res if args.json else human, args.json)
    if args.action == "cancel" and not res.get("cancelled"):
        return 1
    return 0


def _run_cos_spine(args, ctx) -> int:
    core = ctx.core
    try:
        if args.action == "record":
            res = core.cos_spine_record(
                event=args.event,
                direction=args.direction,
                counterparty=args.counterparty,
                text=args.text,
                topic=args.topic,
                due=args.due,
                source_ref=args.source_ref,
                note=args.note,
                commitment_id=args.commitment_id,
            )
            human = (
                f"{res['id']}: {args.event} -> status={res['status']} "
                f"due={res.get('due')}"
            )
        elif args.action == "radar":
            res = core.cos_spine_radar()
            human = (
                f"late={len(res['late'])} at_risk={len(res['at_risk'])}"
                + "".join(
                    f"\n  LATE  {r['id']} {r['counterparty']} due={r['due']}"
                    for r in res["late"]
                )
                + "".join(
                    f"\n  RISK  {r['id']} {r['counterparty']} due={r['due']}"
                    for r in res["at_risk"]
                )
            )
        elif args.action == "grounding-pack":
            res = core.cos_grounding_pack()
            human = (
                f"rendered {res['path']} (documents={res['documents']} "
                f"requested={res['requested']} missing={len(res['missing'])})"
            )
        else:  # render
            res = core.cos_spine_render()
            human = f"rendered {res['path']} (open={res['open']} late={res['late']} at_risk={res['at_risk']})"
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-spine refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(res if args.json else human, args.json)
    return 0


def _run_cos_standing_approval(args, ctx) -> int:
    from .. import cos

    core = ctx.core
    if args.accept_all and args.clear:
        _emit("give exactly ONE of --accept-all or --clear", args.json)
        return 3
    try:
        core._require_host("record a standing ingestion approval")
        if args.accept_all:
            if not (args.reason or "").strip():
                raise ValueError(
                    "--reason is required: a standing approval removes a human "
                    "gate, and the record must say on whose words it stands")
            res = {"state": "recorded",
                   "record": cos.set_standing_approval(core.vault, reason=args.reason)}
        elif args.clear:
            res = {"state": "cleared" if cos.clear_standing_approval(core.vault)
                   else "none-recorded"}
        else:
            rec = cos.standing_approval(core.vault)
            res = {"state": "recorded" if rec else "none-recorded", "record": rec}
        res["path"] = str(cos.standing_approval_path(core.vault))
    except Exception as exc:  # RoleError / HostPathUnsafe / ValueError -> fail closed
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-standing-approval refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    if args.json:
        _emit(res, True)
    elif res["state"] == "recorded" and res.get("record"):
        _emit(f"standing approval ACTIVE: every ingestion batch is answered "
              f"{cos.STANDING_ANSWER!r} on enqueue (recorded "
              f"{res['record'].get('recorded')} — {res['record'].get('reason')}). "
              f"Batches are still signed and still consumed with their content "
              f"CAS; clear it with --clear to restore the manual gate.", False)
    else:
        _emit(f"no standing approval: every ingestion batch waits for the "
              f"owner's answer ({res['path']})", False)
    return 0


def _run_cos_feedback(args, ctx) -> int:
    """PEN 1 of FB-02, on either transport.

    Both inputs are read NO-FOLLOW. `--from-sheet` names a file the owner
    saved, normally under `<vault>/.brain/cos/sheets/`, which is on the
    VM-visible mount — so a symlink planted at a predictable sheet name must
    not redirect this read at a host file. `--from-marks` names a download,
    which is the owner's own Downloads folder and not a lane in CONTEXT.md's
    sense, but it is read through the same helper for the same reason: the
    path is typed at a shell and nothing about it is trusted.

    Everything then goes through `validate_marks` and the record's own closed
    row shapes, and the sheet's `sheet_id` is checked against the consumed
    ledger so the browser's second save of the same download is refused.
    """
    from .. import cos
    from ..cos import feedback_cli, feedback_marks

    core = ctx.core
    try:
        core._require_host("record owner feedback from a sheet")
        if args.from_marks:
            raw = cos._read_nofollow(Path(args.from_marks))
            # STATED CEILING: no `labels=`. A marks FILE carries the answers
            # and not the page, so the label taxonomy the sheet legended is
            # gone by the time this reads it — `validate_marks` shape-checks
            # every label value and refuses an unknown JUDGMENT or DRAFT word
            # (those vocabularies are the judge's own and live in code), but a
            # label it cannot cross-check against that sheet's own list is
            # accepted as written. `--from-sheet` still has the page, so it
            # passes the vocabulary and gets the stricter check. Closing this
            # means putting the taxonomy in the marks file, which is a schema
            # change, not a validator change.
            res = feedback_marks.record_marks(core.vault,
                                              json.loads(raw.decode("utf-8")))
        else:
            html = cos._read_nofollow(Path(args.from_sheet)).decode(
                "utf-8", "replace")
            res = feedback_cli.record_from_sheet(core.vault, html)
    except Exception as exc:  # RoleError / OSError / FeedbackRowInvalid
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos-feedback refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    if args.json:
        _emit(res, True)
    else:
        _emit(
            None,
            False,
            f"cos-feedback: marked={res['threads_marked']} "
            f"unmarked={res['threads_unmarked']} "
            f"thread-rulings={res['thread_rulings']} "
            f"released={len(res['released'])} "
            f"rules={len(res['rules'])} confirmed={len(res['confirmed'])} "
            f"contradicted={len(res['contradicted'])} "
            f"revoked={len(res['revoked'])} "
            f"unreadable-ledger-lines={res['unreadable']} -> {res['path']}",
        )
    return 0


def _run_cos_sheet(args, ctx) -> int:
    """Build the sheet; ``--publish`` is the path-only Artifact handoff."""
    from ..cos import sheet

    core = ctx.core
    try:
        core._require_host("build the COS morning sheet")
        res = (
            sheet.publishable_sheet(core.vault, date=args.date)
            if args.publish
            else sheet.write_sheet(core.vault, date=args.date)
        )
    except Exception as exc:
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"cos sheet refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    if args.json:
        _emit(res, True)
    elif args.publish:
        # The session feeds this exact file to the Artifact tool with
        # capabilities={"artifact": {}} (also declared in JSON and the page's
        # meta element). The same result names `mail_summary_path` for the
        # attended summary-mail handoff; this command itself sends nothing.
        _emit(res["path"], False)
    else:
        counts = res["state"]["counts"]
        _emit(
            None,
            False,
            f"morning sheet: {counts['total']} thread(s), "
            f"{counts['archived']} archived, {counts['ingested']} ingested, "
            f"{counts['drafted']} drafted, {counts['held']} held -> "
            f"{res['path']}",
        )
    return 0


_HANDLERS = {
    "cos": _run_cos_sheet,
    "cos-feedback": _run_cos_feedback,
    "cos-standing-approval": _run_cos_standing_approval,
    "cos-broker": _run_cos_broker,
    "cos-correct": _run_cos_correct,
    "cos-evidence": _run_cos_evidence,
    "cos-priority-map": _run_cos_priority_map,
    "cos-report": _run_cos_report,
    "cos-ingest-sweep": _run_cos_ingest_sweep,
    "cos-hold": _run_cos_hold,
    "cos-spine": _run_cos_spine,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
