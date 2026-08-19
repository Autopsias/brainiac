"""Execute index-storage commands."""

from __future__ import annotations

import os
import sys

from .. import classification as cls
from .. import cli as shared

_emit = shared._emit
_excluded_note = shared._excluded_note
_filter_dicts = shared._filter_dicts


def _run_draft_capture(args, ctx) -> int:
    core = ctx.core
    content = args.content if args.content is not None else sys.stdin.read()
    try:
        res = core.draft_capture(content, ident=args.id, is_source=args.source)
    except ValueError as exc:  # unsafe id / traversal -> fail closed (C-1)
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"draft refused ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"staged draft {res['id']} -> {res['draft']} "
        f"(signed={res['signed']}, indexed={res['indexed']}); "
        f"host drain will sign + index + snapshot",
        args.json,
    )
    return 0


def _run_rebuild(args, ctx) -> int:
    core = ctx.core
    if getattr(args, "progress", False):
        os.environ["BRAIN_PROGRESS"] = "1"
    try:
        res = core.rebuild(json_mode=args.json)
    except Exception as exc:  # H-4: no raw tracebacks from maintenance cmds
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"rebuild failed ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"indexed {res['indexed']} notes ({res['chunks']} chunks) via "
        f"{res['backend']} [{res['embed_model']} d={res['embed_dim']}] -> {res['db']}"
        + _excluded_note(res),
        args.json,
    )
    return 0


def _run_warmup(args, ctx) -> int:
    core = ctx.core
    if getattr(args, "progress", False):
        os.environ["BRAIN_PROGRESS"] = "1"
    try:
        res = core.warmup(json_mode=args.json)
    except Exception as exc:  # H-4: no raw tracebacks from maintenance cmds
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"warmup failed ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else (
            f"embedder {res['model_id']} already cached "
            if res["already_cached"]
            else f"downloaded embedder {res['model_id']} "
        )
        + f"({res['elapsed_s']}s). Run `brain sync` to apply it to the index "
        + "if `brain status` shows embedder: pending.",
        args.json,
    )
    return 0


def _run_sync(args, ctx) -> int:
    core = ctx.core
    if getattr(args, "progress", False):
        os.environ["BRAIN_PROGRESS"] = "1"
    try:
        res = core.sync(
            drain=not args.no_drain, publish=args.publish, json_mode=args.json
        )
    except Exception as exc:  # H-4
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"sync failed ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    # C8: sync's "ingest" sub-report carries the identical promoted-note
    # list (with real classifications) that `brain ingest --json` already
    # routes through the egress gate — sync --json printed it RAW, a
    # second content-returning surface bypassing the single chokepoint.
    ingest_res = res.get("ingest") or {}
    if ingest_res.get("processed"):
        surfaced, egress_report = _filter_dicts(
            ingest_res["processed"], cls.DEFAULT_MAX_TIER
        )
        ingest_res["processed"] = surfaced
        ingest_res["egress"] = egress_report
    # E4: "duplicates" carries `existing_id` — a real note id (of a note
    # that may sit above the max tier) — so it is exactly as much a
    # content-returning surface as "processed" and must go through the
    # same gate, not leak raw.
    if ingest_res.get("duplicates"):
        dup_surfaced, dup_egress = _filter_dicts(
            ingest_res["duplicates"], cls.DEFAULT_MAX_TIER
        )
        ingest_res["duplicates"] = dup_surfaced
        ingest_res["duplicates_egress"] = dup_egress
    if args.json:
        _emit(res, True)
    else:
        d = res.get("drain", {})
        snap = res.get("snapshot")
        tail = f"; snapshot gen {snap['generation']}" if snap else ""
        reb = res.get("rebased", 0)
        reb_note = (
            f"; vault root changed — rebased {reb} path(s), no re-embed" if reb else ""
        )
        _emit(
            None,
            False,
            f"sync [{res['mode']}]: +{res.get('added', 0)} ~{res.get('updated', 0)} "
            f"-{res.get('deleted', 0)} ={res.get('unchanged', 0)} "
            f"({res['chunks']} chunks); drained {d.get('promoted', 0)} "
            f"(skipped {d.get('skipped', 0)})" + reb_note + tail + _excluded_note(res),
        )
    return 0


def _run_snapshot(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.publish_snapshot(args.dest)
    except Exception as exc:  # H-4
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"snapshot failed ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    _emit(
        res
        if args.json
        else f"published snapshot gen {res['generation']} "
        f"({res['notes']} notes, {res['chunks']} chunks) -> {res['snapshot_db']}",
        args.json,
    )
    return 0


def _run_restore_index(args, ctx) -> int:
    core = ctx.core
    try:
        res = core.restore_index_from_snapshot(force=args.force, dry_run=args.dry_run)
    except Exception as exc:  # H-4
        _emit(
            {"error": type(exc).__name__, "detail": str(exc)}
            if args.json
            else f"restore-index failed ({type(exc).__name__}): {exc}",
            args.json,
        )
        return 3
    if args.json:
        _emit(res, True)
    elif res.get("dry_run"):
        _emit(
            f"[dry-run] would restore {res['snapshot_notes']} notes from the snapshot "
            f"(live index now: {res['live_notes_before']}) — nothing written",
            False,
        )
    else:
        _emit(
            f"restored index from snapshot: {res['live_notes_after']} notes "
            f"(prior index backed up at {res['backup']})",
            False,
        )
    return 0


def _run_status(args, ctx) -> int:
    core = ctx.core
    res = core.status(args.snapshot_dest)
    if args.json:
        _emit(res, True)
    else:
        ix, sn, ver = (
            res.get("index", {}),
            res.get("snapshot", {}),
            res.get("version", {}),
        )
        emb = res.get("embedder", {})
        emb_line = f"embedder: {emb.get('state', '?')} [{emb.get('model_id', '?')}]"
        if emb.get("state") == "pending":
            hint = emb.get("download_size_hint")
            emb_line += (
                " — run `brain warmup`"
                + (f" ({hint} download)" if hint else "")
                + " then `brain sync` for real semantic search"
            )
        skew_lines = []
        if ver.get("index_newer_than_binary"):
            skew_lines.append(
                f"  WARNING: index schema_version {ver.get('index_schema_version')} > "
                f"binary SCHEMA_VERSION {ver.get('binary_schema_version')} — "
                "index was built by a newer brain; update the engine "
                "(or run `brain sync --rebuild` to force a downgrade)"
            )
        if ver.get("snapshot_newer_than_binary"):
            skew_lines.append(
                f"  WARNING: snapshot schema_version {ver.get('snapshot_schema_version')} > "
                f"binary SCHEMA_VERSION {ver.get('binary_schema_version')} — "
                "snapshot is newer than this CLI; update the engine"
            )
        # LIVENESS (HARDENED:claude-2): an unanswered COS ingestion batch
        # is not an error anywhere — it just quietly re-kills the funnel
        # behind the one-open-batch backpressure. Say so out loud.
        live = (res.get("cos") or {}).get("batch_liveness") or {}
        if live.get("alert"):
            skew_lines.append(f"  WARNING: {live['alert_text']}")
        # R8 (2026-07-30 review): the JSON status and the morning brief both
        # carry `unstamped_batched`, but `brain status` — the primary human
        # diagnostic — printed nothing, so an operator read a healthy status
        # while EVERY candidate was being diverted for a missing stamp.
        if live.get("unstamped_batched"):
            skew_lines.append(
                f"  WARNING: {live['unstamped_batched']} COS candidate(s) sent to "
                f"the owner batch for a missing category/ruleset stamp — "
                f"pattern auto-capture {live.get('pattern_autocapture', 'suspended')}"
            )
        # INS-01: a run the host validator could not certify. Loud here
        # because run 59 skipped its whole self-eval and NOTHING noticed —
        # an instrument that only writes a log is the failure being fixed.
        if live.get("run_validity_text"):
            skew_lines.append(f"  WARNING: {live['run_validity_text']}")
        # STA-01: same treatment for a candidate the host could not
        # attribute to a VALID run — quarantined, never silently bound.
        if live.get("quarantine_text"):
            skew_lines.append(f"  WARNING: {live['quarantine_text']}")
        _emit(
            None,
            False,
            f"brain {ver.get('package_version', '?')}\n"
            f"index: {ix.get('notes', '?')} notes / {ix.get('chunks', '?')} chunks "
            f"[{ix.get('embed_model', '?')} d={ix.get('embed_dim', '?')}]\n"
            f"{emb_line}\n"
            f"snapshot: {sn.get('snapshot', '?')} "
            + (
                f"gen {sn.get('generation')} age {sn.get('age_human')}"
                if sn.get("snapshot") == "present"
                else ""
            )
            + ("\n" + "\n".join(skew_lines) if skew_lines else ""),
        )
    return 0


def _run_project(args, ctx) -> int:
    core = ctx.core
    from ..projection import project_workspace

    res = project_workspace(core.vault, args.dest, max_tier=args.max_tier).to_dict()
    _emit(
        res
        if args.json
        else f"projected {res['copied']} notes (<= {res['max_tier']}) to {res['dest']}; "
        f"excluded {res['excluded']} ({res['excluded_unlabelled']} unlabelled)",
        args.json,
    )
    return 0


_HANDLERS = {
    "draft-capture": _run_draft_capture,
    "rebuild": _run_rebuild,
    "warmup": _run_warmup,
    "sync": _run_sync,
    "snapshot": _run_snapshot,
    "restore-index": _run_restore_index,
    "status": _run_status,
    "project": _run_project,
}

COMMANDS = tuple(_HANDLERS)


def run(args, ctx) -> int:
    return _HANDLERS[args.cmd](args, ctx)
