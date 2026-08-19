"""COS auto-capture operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._attachment_anchors import stage_attachment_hold_authz
from ._attachment_store import _write_attachment_meta, attachment_metas
from ._claims_state import _bound_meta, _pending_metas
from ._hold_store import hold_add
from ._io import _read_nofollow
from ._layout import _ts, _utcnow, proposals_dir
from ._learning_config import _autocap_defaults
from ._learning_ledger import log_defect, record_outcome
from ._routing import _bump_route_stats, _stamp_missing, route_decision
from ._taxonomy import ingest_taxonomy

def auto_capture_fold(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Route every currently-PENDING candidate that passes the BOTH-KEYS policy
    into the hold store (undo-window gated — see the hold store above), instead
    of the next owner-inbox batch. Runs BEFORE ``enqueue_batch`` in the broker
    fold so only non-qualifying candidates ever reach the owner. Never signs
    anything itself, and never bypasses the undo window.

    Under the writer lock (B4): it moves pending files into the hold store
    while the consumer may be promoting from the same directory."""
    with vault_writer_lock(vault, verb="cos-autocapture"):
        return _auto_capture_fold_locked(vault, now or _utcnow())

def _route_text_candidates(vault, now: _dt.datetime, taxonomy: dict[str, Any]
                           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Route pending text candidates."""
    held: list[dict[str, Any]] = []
    explored: list[dict[str, Any]] = []
    unstamped = 0
    batched = 0
    pending = proposals_dir(vault) / "pending"
    for m in _pending_metas(vault):
        nid = m.get("id")
        md = pending / f"{nid}.md"
        if not md.exists():
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            continue
        bound = _bound_meta(vault, nid, body=content)
        decision = route_decision(vault, bound, now=now, taxonomy=taxonomy)
        if decision["decision"] != "auto":
            batched += 1
            if _stamp_missing(bound):
                unstamped += 1
            if decision.get("exploration"):
                explored.append({"id": nid, "category": decision["category"]})
            continue
        cfg = decision["category_gate"].get("config", _autocap_defaults())
        not_before = _ts(now + _dt.timedelta(hours=cfg.get("undo_hours",
                                                            DEFAULT_AUTOCAP_UNDO_HOURS)))
        try:
            hold_add(vault, content, not_before=not_before, ident=nid, evidence=bound)
        except ValueError:
            continue  # a hold already exists for this id — leave it pending
        except Exception as exc:  # noqa: BLE001 — no key / unwritable store
            # Fail CLOSED and stay visible: the candidate keeps its place in
            # pending/ and reaches the owner's next batch instead.
            log_defect(vault, "hold-add-failed",
                       f"{nid}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        # `.md` FIRST, like the other two teardown sites (`_quarantine_claim`'s
        # caller and the journal replay) — review 2026-08-13, round 7. This one
        # alone unlinked the `.json` first, and since the union receipt scan an
        # orphan of EITHER half is an incomplete pair that pins K2 INCONCLUSIVE
        # for every later run with nothing to clean it up. Neither orphan
        # self-heals, so the tie is broken on WHAT is left behind: the `.md` is
        # the candidate's mail-derived BODY and the `.json` is metadata about
        # it. Take the body off the mount first, and make all three teardowns
        # one order so the next reader has one rule to remember.
        md.unlink(missing_ok=True)
        (pending / f"{nid}.json").unlink(missing_ok=True)
        record_outcome(vault, pattern=bound.get("pattern"), ident=nid,
                       outcome="auto-captured",
                       bundle_version=bound.get("bundle_version"), ts=_ts(now),
                       category=bound.get("category"), lane=bound.get("lane"),
                       tier=bound.get("tier"),
                       rules_version=bound.get("rules_version"),
                       kind=bound.get("kind"), answer_mode="auto")
        held.append({"id": nid, "pattern": bound.get("pattern"),
                     "category": bound.get("category"), "lane": bound.get("lane"),
                     "not_before": not_before})
    return held, explored, unstamped, batched


def _route_attachment_candidates(vault, now: _dt.datetime, taxonomy: dict[str, Any]
                                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Route pending attachment candidates."""
    held: list[dict[str, Any]] = []
    explored: list[dict[str, Any]] = []
    unstamped = 0
    batched = 0

    # Attachments take the same policy on their own lane: a graduated
    # attachment category parks the file with a not_before in its host-private
    # quarantine and releases it into vault/inbox/ when the window closes.
    for att in attachment_metas(vault, state="pending"):
        decision = route_decision(vault, att, now=now, taxonomy=taxonomy)
        if decision["decision"] != "auto":
            batched += 1
            if _stamp_missing(att):
                unstamped += 1
            if decision.get("exploration"):
                explored.append({"id": att["id"], "category": decision["category"]})
            continue
        cfg = decision["category_gate"].get("config", _autocap_defaults())
        not_before = _ts(now + _dt.timedelta(hours=cfg.get("undo_hours",
                                                            DEFAULT_AUTOCAP_UNDO_HOURS)))
        # INT-04 (round 2): sign the authorization NOW, over the bytes on disk
        # now — the auto lane has no owner batch to CAS against later, and the
        # sidecar it used to read the hash from sits on the mount beside the
        # payload. Fail CLOSED: no key / unsafe store / unreadable payload ->
        # the candidate stays pending and reaches the owner's next batch.
        try:
            held_sha = hashlib.sha256(_read_nofollow(Path(att["path"]))).hexdigest()
            stage_attachment_hold_authz(
                vault, att["id"], sha256_hex=held_sha, not_before=not_before,
                # the name (and therefore the ingest handler) and the category
                # the demotion re-check runs against are signed HERE, at the
                # one moment the host itself decided this may auto-capture
                filename=str(att.get("filename") or Path(att["path"]).name),
                category=str(att.get("category") or ""), now=now)
        except Exception as exc:  # noqa: BLE001 — no key / unwritable / swapped
            log_defect(vault, "attachment-hold-unauthorized",
                       f"{att['id']}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        att = {**att, "state": "held", "not_before": not_before}
        _write_attachment_meta(vault, att)
        record_outcome(vault, pattern=att.get("pattern"), ident=att["id"],
                       outcome="auto-captured",
                       bundle_version=att.get("bundle_version"), ts=_ts(now),
                       category=att.get("category"), lane=LANE_ATTACHMENT,
                       tier=att.get("tier"), rules_version=att.get("rules_version"),
                       kind="attachment", answer_mode="auto")
        held.append({"id": att["id"], "category": att.get("category"),
                     "lane": LANE_ATTACHMENT, "not_before": not_before})
    return held, explored, unstamped, batched


def _auto_capture_fold_locked(vault, now: _dt.datetime) -> dict[str, Any]:
    """Route eligible pending candidates into their undo holds."""
    taxonomy = ingest_taxonomy(vault)
    text_held, text_explored, text_unstamped, text_batched = _route_text_candidates(
        vault, now, taxonomy)
    attachment_held, attachment_explored, attachment_unstamped, attachment_batched = (
        _route_attachment_candidates(vault, now, taxonomy))
    held = text_held + attachment_held
    explored = text_explored + attachment_explored
    unstamped = text_unstamped + attachment_unstamped
    batched = text_batched + attachment_batched
    stats = _bump_route_stats(vault, now=now, unstamped=unstamped,
                              auto=len(held), batched=batched)
    return {"held": held, "exploration_samples": explored,
            # B8: name the DEGRADATION out loud, per run and cumulatively.
            "batched": batched, "unstamped_batched": unstamped,
            "unstamped_batched_total": int(stats.get("unstamped_batched", 0)),
            "pattern_autocapture": PATTERN_AUTOCAPTURE_STATUS}

__all__ = ['auto_capture_fold', '_auto_capture_fold_locked']
