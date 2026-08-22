"""Execute COS broker commands."""

from __future__ import annotations

import sys
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
    return 0 if not res.get("errors") else 1


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


_HANDLERS = {
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
