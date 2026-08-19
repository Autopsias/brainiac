"""COS hold-release operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._approval import approved_verify_key, stage_approved
from ._attachment_acceptance import _accept_attachment
from ._attachment_anchors import attachment_hold_authz, clear_attachment_hold_authz
from ._attachment_store import _discard_attachment, _write_attachment_meta, attachment_metas
from ._guards import _safe_meta_id
from ._hold_store import verified_hold
from ._hold_undo import _undone_before, _write_released_marker
from ._io import _write_atomic
from ._layout import _env_days, _parse_ts, _ts, _utcnow, hold_dir, proposals_dir
from ._learning_ledger import log_defect
from ._routing import route_decision
from ._taxonomy import ingest_taxonomy

def _still_eligible_at_release(vault, evidence: dict[str, Any], *,
                               now: _dt.datetime,
                               taxonomy: dict[str, Any]) -> tuple[bool, str]:
    """Does this hold's category STILL qualify for the auto lane, right now?

    B2 — release used to check only ``not_before`` and the undo log, so a hold
    placed while a category was graduated proceeded to signing even after the
    category had since been demoted: by a claim-time security defect, an owner
    undo, a `never` flip, removal from the overlay, or a rolling accept-rate
    drop. The whole point of demotion is that it applies NOW, and a hold is
    precisely the population that has not yet committed. Re-run the same
    both-keys policy against the item's stored HOST-BOUND evidence and the
    CURRENT taxonomy/statistics; exploration sampling is skipped (this item
    already won its lane, we are only re-testing whether the lane is open).
    """
    if not evidence.get("category"):
        # Not a graduation-placed hold at all: `auto_capture_fold` can only
        # hold a candidate whose category is OUT of `_UNPATTERNED`, so a hold
        # with no bound category came from the host-broker's own
        # `brain cos-hold add`. There is no graduation to revoke.
        return True, "operator-placed hold (no auto-lane graduation to revoke)"
    decision = route_decision(vault, evidence, now=now, taxonomy=taxonomy)
    if decision["decision"] == "auto" or decision.get("exploration"):
        return True, "eligible"
    return False, str(decision.get("reason") or "no longer eligible")

def _return_hold_to_owner(vault, nid: str, evidence: dict[str, Any],
                          md: Path | None, *, reason: str,
                          now: _dt.datetime) -> None:
    """Put a no-longer-eligible hold back in front of the OWNER (B2).

    Never released, never silently dropped: the candidate rejoins the ordinary
    pending queue and the next ``enqueue_batch`` puts it in the owner's
    question, where a demoted category belongs."""
    pending = proposals_dir(vault) / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    meta = dict(evidence or {})
    meta.update({
        "id": nid, "state": "pending",
        "returned_from_hold": _ts(now), "returned_reason": reason,
        "ttl_expires": _ts(now + _dt.timedelta(
            days=_env_days(PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS))),
    })
    if md is not None and md.exists():
        text = md.read_text(encoding="utf-8")
        # Re-hash: the batch digest and the accept-time CAS both check the
        # sha against the file that will actually be promoted.
        meta["sha256"] = sha256_text(text)
        shutil.move(str(md), pending / f"{nid}.md")
    public("_write_atomic")(pending / f"{nid}.json",
                  (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8"))

def _quarantine_hold(vault, marker: Path, payload: Path | None, reason: str,
                     now: _dt.datetime, *, kind: str = "hold-record-unverified",
                     name: str | None = None) -> None:
    """Park an unreleasable hold OUT of the release path, marker AND payload.

    Renaming only the marker orphaned ``<id>.md`` in ``hold/``: invisible to
    ``hold_list``, outside every GC window, and impossible to find later. The
    destination is derived from a real directory entry, so no claimed id can
    steer it, and a failed rename is logged rather than raised — one poisoned
    marker must not wedge every future release."""
    stem = (name or marker.name).split(".")[0] or "hold"
    if payload is None:
        # An unsafe/unknown claimed id still has a payload — the sibling named
        # after the real directory entry. Quarantining the marker alone leaves
        # it orphaned in hold/: invisible to hold_list, outside every GC window.
        payload = marker.parent / f"{stem}.md"
    for src, suffix in ((marker, ".refused.json"),
                        (payload, ".refused.md")):
        if src is None or not (src.exists() or src.is_symlink()):
            continue
        try:
            os.replace(src, marker.parent / f"{stem}{suffix}")
        except OSError as exc:
            log_defect(vault, kind,
                       f"{stem}: could not quarantine {src.name} ({exc})",
                       ts=_ts(now))
    log_defect(vault, kind,
               f"{stem}: {reason} — NOT released (quarantined in hold/)",
               ts=_ts(now))

def hold_release_due(vault, now: _dt.datetime | None = None) -> list[str]:
    """Move every DUE held item into the approved queue (the signed drain path).

    Claim-by-rename per item (same atomic marker claim as ``hold_cancel``), so
    a concurrent cancel and release can never both act on one item — and a
    DURABLE undo recorded before the deadline wins even when the release won
    the rename. Runs under the writer lock (B4) so the eligibility re-check
    below cannot race a concurrent demotion or consume."""
    with vault_writer_lock(vault, verb="cos-hold-release"):
        return _hold_release_due_locked(vault, now or _utcnow())

def _release_held_attachments(vault, now: _dt.datetime, taxonomy: dict[str, Any],
                              returned: list[str]) -> list[str]:
    """Release due attachment holds."""
    released: list[str] = []
    for meta in attachment_metas(vault, state="held"):
        # INT-04 (round 2): `not_before` AND the content hash come from the
        # SIGNED hold authorization, never from the plain fields beside them on
        # the mount — otherwise the VM shortens its own undo window and restates
        # the hash to match whatever it substituted. No authorization, no
        # release; the item stays held and the defect names why.
        try:
            authz = attachment_hold_authz(vault, meta["id"])
        except ApprovedKeyUnavailable as exc:
            log_defect(vault, "attachment-release-refused",
                       f"{meta['id']}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        if authz is None:
            log_defect(vault, "attachment-release-unauthorized",
                       f"{meta['id']}: no verified host hold authorization "
                       f"(missing, tampered, or placed by a pre-INT-04 engine)"
                       f" — NOT released", ts=_ts(now))
            continue
        nb = _parse_ts(str(authz["not_before"]))
        if nb is None or nb > now:
            continue
        if _undone_before(vault, meta["id"], nb):
            _discard_attachment(vault, meta)
            continue
        # The CATEGORY the demotion re-check runs against is the SIGNED one:
        # deleting it from the sidecar made an auto-lane hold look
        # operator-placed, which is the one shape `_still_eligible_at_release`
        # waves through without consulting the current taxonomy.
        ok, why = _still_eligible_at_release(
            vault, {**meta, "category": authz.get("category") or ""},
            now=now, taxonomy=taxonomy)
        if not ok:
            back = {k: v for k, v in meta.items() if k != "not_before"}
            back["state"] = "pending"
            back["returned_from_hold"] = _ts(now)
            back["returned_reason"] = why
            _write_attachment_meta(vault, back)
            clear_attachment_hold_authz(vault, meta["id"])
            returned.append(meta["id"])
            continue
        _write_released_marker(vault, meta["id"], meta, now)
        try:
            _accept_attachment(vault, meta, expected_sha=str(authz["sha256"]),
                               expected_name=str(authz.get("filename") or ""),
                               now=now)
        except Exception as exc:  # noqa: BLE001 — one bad item never wedges the loop
            # INT-04: no anchor, no release. The item stays in quarantine and
            # the next hourly run retries it — a released-but-unanchored
            # attachment is exactly what this lane must not produce.
            log_defect(vault, "attachment-release-refused",
                       f"{meta['id']}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        released.append(meta["id"])
    return released


def _release_due_markers(vault, hdir: Path, now: _dt.datetime, taxonomy: dict[str, Any],
                         returned: list[str]) -> list[str]:
    """Release due text-hold markers."""
    if not hdir.is_dir():
        return []
    try:
        pubkey = public("approved_verify_key")(vault)
    except ApprovedKeyUnavailable as exc:
        # No key => nothing could be signed anyway. Leave every hold parked
        # rather than claim markers this run cannot honour.
        log_defect(vault, "hold-release-skipped",
                   f"no host key, holds left parked ({exc})", ts=_ts(now))
        return []
    released: list[str] = []
    for marker in sorted(hdir.glob("*.hold.json")):
        # ONE poisoned file must never wedge every future release. Everything
        # from here to the end of the iteration is attacker-influenced, so a
        # malformed record is quarantined and the loop CONTINUES — it used to
        # raise (non-dict JSON -> AttributeError from `.get`, a Unicode
        # filename with no id -> ValueError from `safe_slug`) straight out of
        # the function, permanently blocking every legitimate due hold.
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # C-1 trust boundary: this id BECOMES A PATH, and the marker is
        # attacker-writable. Quarantine under the DIRECTORY ENTRY's own name
        # (one path component by construction) rather than the claimed id.
        nid = _safe_meta_id(m)
        if nid is None:
            _quarantine_hold(vault, marker, None,
                             "hold record is malformed or carries an unsafe id "
                             "(not a bare slug)", now, name=marker.name)
            continue
        try:
            released_here = _release_one_hold(
                vault, hdir, marker, m, nid, pubkey=pubkey, now=now,
                taxonomy=taxonomy, returned=returned)
        except Exception as exc:  # noqa: BLE001 — never wedge the whole run
            _quarantine_hold(vault, marker, None,
                             f"unhandled error while releasing "
                             f"({type(exc).__name__}: {exc})", now,
                             name=marker.name)
            continue
        if released_here:
            released.append(nid)
    return released


def _hold_release_due_locked(vault, now: _dt.datetime) -> list[str]:
    """Release all due holds through their authorized paths."""
    returned: list[str] = []
    taxonomy = ingest_taxonomy(vault)
    released = _release_held_attachments(vault, now, taxonomy, returned)
    released.extend(_release_due_markers(
        vault, hold_dir(vault), now, taxonomy, returned))
    if returned:
        log_defect(vault, "hold-returned-to-owner",
                   f"{len(returned)} due hold(s) no longer eligible at release: "
                   f"{', '.join(returned)}", ts=_ts(now))
    return released

def _release_one_hold(vault, hdir: Path, marker: Path, m: dict[str, Any],
                      nid: str, *, pubkey, now: _dt.datetime, taxonomy,
                      returned: list[str]) -> bool:
    """Release exactly ONE due hold. Returns True if it reached the queue.

    Split out of the loop so a failure on one marker is contained by the
    caller's ``except`` instead of aborting every remaining hold."""
    # The AUTHORIZATION decides, and only the signed body is read for it —
    # `not_before` beside the signature is decoration an attacker can edit.
    authz = verified_hold(vault, m, nid=nid, pubkey=pubkey)
    if authz is None:
        # Two causes, one safe answer, but NOT one wording. An unsigned marker
        # is what every hold parked by a pre-INT-01 engine looks like — an
        # upgrade, not an attack — and reading it out as tampering would be both
        # wrong and alarming. Either way the item is quarantined rather than
        # released or auto-promoted: routing it back into the owner queue here
        # would bypass claim validation (secret scrub, tier, claims ledger, run
        # attribution) that every other candidate passes. Recovery is an
        # operator action: re-propose the quarantined payload.
        legacy = not isinstance(m.get("body"), str)
        _quarantine_hold(
            vault, marker, hdir / f"{nid}.md",
            ("parked by a pre-INT-01 engine, so it carries no host "
             "authorization to verify (UPGRADE, not tampering); re-propose the "
             "quarantined payload if it is still wanted"
             if legacy else "no valid host authorization for this hold"),
            now,
            kind="hold-record-legacy-unsigned" if legacy
            else "hold-record-unverified")
        return False
    nb = _parse_ts(str(authz.get("not_before", "")))
    if nb is None or nb > now:
        return False
    claimed = hdir / f"{nid}.releasing.json"
    try:
        os.rename(marker, claimed)
    except OSError:
        return False              # a concurrent cancel/release won the claim
    md = hdir / f"{nid}.md"
    if _undone_before(vault, nid, nb):
        # The owner's undo is DURABLE and predates the deadline: it wins the
        # race by design, even though this release claimed the rename.
        md.unlink(missing_ok=True)
        claimed.unlink(missing_ok=True)
        return False
    ok, why = _still_eligible_at_release(
        vault, m.get("evidence") or {}, now=now, taxonomy=taxonomy)
    if not ok:
        _return_hold_to_owner(vault, nid, m.get("evidence") or {}, md,
                              reason=why, now=now)
        claimed.unlink(missing_ok=True)
        returned.append(nid)
        return False
    out = False
    if md.exists():
        # INT-01: a released hold takes the same anchored route as an
        # owner-accepted candidate — and the anchor binds the bytes the host
        # AUTHORIZED at hold time, not whatever is on the mount now.
        text = md.read_text(encoding="utf-8")
        if sha256_text(text) != authz["sha256"]:
            _quarantine_hold(vault, claimed, md,
                             "held bytes changed since the host authorized them",
                             now, kind="hold-payload-drift")
            return False
        try:
            stage_approved(vault, nid, text, sha256_hex=authz["sha256"],
                           batch_id=f"hold:{nid}", kind="hold", now=now)
        except Exception as exc:  # noqa: BLE001 — retry next run, never sign
            os.replace(claimed, marker)       # un-claim so the next run retries
            log_defect(vault, "hold-release-failed",
                       f"{nid}: approved queue unavailable "
                       f"({type(exc).__name__}: {exc})", ts=_ts(now))
            return False
        md.unlink(missing_ok=True)
        _write_released_marker(vault, nid, m.get("evidence") or {}, now)
        out = True
    claimed.unlink(missing_ok=True)
    return out

__all__ = ['_still_eligible_at_release', '_return_hold_to_owner', '_quarantine_hold', 'hold_release_due', '_hold_release_due_locked', '_release_one_hold']
