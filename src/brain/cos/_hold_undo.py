"""COS hold-undo operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._approval import approved_anchor_path_or_none, approved_payload_path_or_none, approved_queued
from ._approval_cleanup import clear_approved
from ._attachment_anchors import clear_attachment_anchor
from ._attachment_store import _attachment_lifecycle, _discard_attachment, _ingested_raw_id, _lifecycle_payload, attachment_metas
from ._io import _append_jsonl, _read_jsonl, _write_atomic
from ._layout import _parse_ts, _ts, _utcnow, hold_dir
from ._learning_ledger import demote_category, log_defect, record_outcome

def _undos_path(vault) -> Path:
    return hold_dir(vault) / "undos.jsonl"

def _undone_before(vault, nid: str, deadline: _dt.datetime | None) -> bool:
    if deadline is None:
        return False
    for e in _read_jsonl(_undos_path(vault)):
        if e.get("id") != nid:
            continue
        ts = _parse_ts(str(e.get("ts", "")))
        if ts and ts <= deadline:
            return True
    return False

def _write_released_marker(vault, nid: str, evidence: dict[str, Any],
                           now: _dt.datetime) -> None:
    """Keep the graduation key ALIVE past release — an undo of an already
    released (or already signed) item must still know which category to
    demote, and the hold marker is gone by then."""
    nid = safe_slug(nid)              # never let an id become a path unchecked
    hdir = hold_dir(vault)
    hdir.mkdir(parents=True, exist_ok=True)
    public("_write_atomic")(hdir / f"{nid}.released.json", (json.dumps(
        {"id": nid, "released": _ts(now), "evidence": evidence or {}},
        sort_keys=True) + "\n").encode("utf-8"))

def _hold_evidence(vault, nid: str) -> dict[str, Any]:
    hdir = hold_dir(vault)
    for name in (f"{nid}.hold.json", f"{nid}.releasing.json",
                 f"{nid}.cancelled.json", f"{nid}.released.json"):
        p = hdir / name
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and isinstance(m.get("evidence"), dict):
            return m["evidence"]
    att = attachment_metas(vault)
    for m in att:
        if m.get("id") == nid:
            return m
    return _attachment_lifecycle(vault, nid)     # B3: survives the release

def _signed_target_id(vault, nid: str) -> str:
    """The note id an undo of ``nid`` must actually retire.

    For an email-text candidate that is the id itself. For an ATTACHMENT it is
    the ``raw/`` id the ingest drain minted from the released bytes (B3)."""
    life = _attachment_lifecycle(vault, nid)
    if life:
        return _ingested_raw_id(vault, str(life.get("sha256") or "")) or nid
    return nid

def undo_state(vault, nid: str, *, core: Any = None) -> str:
    """Where in the state machine ``nid`` currently sits."""
    hdir = hold_dir(vault)
    if (hdir / f"{nid}.hold.json").exists():
        return "held"
    if (hdir / f"{nid}.releasing.json").exists():
        return "releasing"
    for m in attachment_metas(vault):
        if m.get("id") == nid:
            return "held" if m.get("state") == "held" else "quarantined"
    # INT-01: broker-accepted/released items now wait in the host-only approved
    # queue; a VM draft of the same id may ALSO sit in capture-inbox. Either
    # way the item is unsigned and awaiting the drain.
    if (approved_queued(vault, nid)
            or (config.capture_inbox_dir(vault) / f"{nid}.md").exists()):
        return "capture-pending"
    # B3: a RELEASED attachment no longer answers to its own name anywhere —
    # not in capture-inbox/ (it goes to vault/inbox/ as a plain file) and not
    # in the index (ingestion renames it). Follow its lifecycle record.
    life = _attachment_lifecycle(vault, nid)
    if life:
        # R3 recovery: a `releasing` record means the payload is at `dest` OR
        # still at `src` (a crash can land either side of the move). Reconcile
        # from whichever location actually holds it — the identity, and so the
        # ability to withdraw, survives the window either way.
        if _lifecycle_payload(life, vault) is not None:
            return "inbox-pending"      # unsigned, still awaiting the drain
        if _ingested_raw_id(vault, str(life.get("sha256") or "")):
            return "signed"
    if core is not None:
        try:
            if core.index.get(nid):
                return "signed"
        except Exception:  # noqa: BLE001 — an unreadable index is not a state
            pass
    return "absent"

def hold_undo(vault, ident: str, *, core: Any = None,
              now: _dt.datetime | None = None) -> dict[str, Any]:
    """Undo ONE auto-captured item, whatever state it has reached.

    Under the writer lock (B4): an undo races the release path over the same
    hold markers, quarantine sidecars and capture drafts, and its signed
    branch writes a note."""
    with vault_writer_lock(vault, verb="cos-undo"):
        return _hold_undo_locked(vault, ident, core=core, now=now or _utcnow())


def _undo_waiting_draft(vault, nid: str, state: str, hdir: Path, *,
                        now: _dt.datetime) -> dict[str, Any]:
    """Remove one unsigned waiting draft."""
    if state == "releasing":
        (hdir / f"{nid}.md").unlink(missing_ok=True)
    inbox_draft = config.capture_inbox_dir(vault) / f"{nid}.md"
    try:
        inbox_draft.unlink(missing_ok=True)
    except OSError:
        pass
    cleared = clear_approved(vault, nid) and not inbox_draft.exists()
    if cleared:
        return {"undone": True, "action": "draft-deleted"}
    still = [str(path) for path in (
        approved_payload_path_or_none(vault, nid), approved_anchor_path_or_none(vault, nid), inbox_draft)
        if path is not None and path.exists()]
    log_defect(vault, "undo-incomplete",
               f"{nid}: undo could NOT remove {', '.join(still)} — the item is still queued and would be signed",
               ts=_ts(now))
    return {"undone": False, "action": "undo-failed", "blocked_by": still}


def _hold_undo_locked(vault, ident: str, *, core: Any,
                      now: _dt.datetime) -> dict[str, Any]:
    nid = safe_slug(ident)
    hdir = hold_dir(vault)
    hdir.mkdir(parents=True, exist_ok=True)
    # DURABLE FIRST — before touching any state, so the timestamp exists even
    # if this process dies mid-undo and so the release path can see it.
    _append_jsonl(_undos_path(vault), {"id": nid, "ts": _ts(now)}, vault=vault)
    evidence = _hold_evidence(vault, nid)
    state = undo_state(vault, nid, core=core)
    result: dict[str, Any] = {"id": nid, "state_before": state, "undone": False,
                              "action": "none"}

    if state in ("held", "quarantined"):
        att = next((m for m in attachment_metas(vault) if m.get("id") == nid), None)
        if att is not None:
            _discard_attachment(vault, att)
            result.update(undone=True, action="discarded")
        else:
            claimed = hdir / f"{nid}.cancelled.json"
            try:
                os.rename(hdir / f"{nid}.hold.json", claimed)
            except FileNotFoundError:
                claimed = None  # a concurrent release claimed it; fall through
            (hdir / f"{nid}.md").unlink(missing_ok=True)
            if claimed is not None:
                claimed.unlink(missing_ok=True)
            result.update(undone=True, action="cancelled")
    elif state in ("releasing", "capture-pending"):
        # An UNSIGNED capture draft has joined no audit chain and no index —
        # deleting it is the correct, complete undo. Both waiting rooms: the
        # host-only approved queue and the VM-writable capture-inbox. An undo
        # that could not actually remove it must SAY SO: reporting
        # "draft-deleted" over a failed unlink leaves the next drain free to
        # sign the thing the owner just revoked.
        result.update(_undo_waiting_draft(vault, nid, state, hdir, now=now))
    elif state == "inbox-pending":
        # B3: a released ATTACHMENT sitting in vault/inbox/ awaiting the ingest
        # drain. Nothing signed it yet, so removing it is complete — but it is
        # the owner's file, so it goes to the recoverable expired/ area (B7),
        # never straight to unlink.
        life = _attachment_lifecycle(vault, nid)
        payload = _lifecycle_payload(life, vault)   # guarded, or None
        if payload is None:
            result.update(undone=False, action="withdraw-refused",
                          reason="lifecycle record names no payload inside this vault")
        else:
            _discard_attachment(vault, {"id": nid, "path": str(payload)})
            # INT-04: the acceptance is revoked, so its anchor goes with it —
            # otherwise a later, unrelated drop under the same inbox name is
            # refused against bytes nobody is waiting for any more.
            clear_attachment_anchor(vault, payload)
            result.update(undone=True, action="inbox-file-withdrawn")
    elif state == "signed":
        result.update(**_retire_signed_note(
            core, nid, now=now, target_id=_signed_target_id(vault, nid)))

    if result["undone"]:
        cat = evidence.get("category") or CATEGORY_UNCLASSIFIED
        result["demoted"] = demote_category(
            vault, cat, reason=f"owner undo of auto-committed {nid} ({state})",
            ts=_ts(now))
        record_outcome(vault, pattern=evidence.get("pattern"), ident=f"{nid}:undo",
                       outcome="undo", bundle_version=evidence.get("bundle_version"),
                       ts=_ts(now), category=cat, lane=evidence.get("lane"),
                       tier=evidence.get("tier"),
                       rules_version=evidence.get("rules_version"))
    return result

def _retire_signed_note(core: Any, nid: str, *, now: _dt.datetime,
                        target_id: str | None = None) -> dict[str, Any]:
    """Undo AFTER signing: an audited retirement, never a raw deletion.

    B6 — this used to stamp ``is_latest_version: false`` + ``superseded_date``
    with NO successor. That shape is FORBIDDEN by the substrate: AGENTS.md §2
    says `false` implies a successor exists, and `tools/validate.py` errors on
    both keys without `superseded_by` — so one owner undo left the vault
    permanently failing the documented pre-commit conventions gate, far from
    the cause. There IS no successor here (an undo retracts a claim, it does
    not replace it), so `core.supersede` is not the right path either: it
    requires a real successor note on the other side of the chain.

    The conforming shape is a plain retirement marker the validator
    recognises — ``retired``/``retired_date``/``retired_reason``, no
    supersession keys — written through the ordinary signed write path, so the
    note stays in the hash-chained brain exactly as before.
    """
    if core is None:
        return {"undone": False, "action": "needs-core",
                "detail": "already signed: retiring it is an AUDITED write — "
                          "call with a host BrainCore"}
    target = target_id or nid
    row = core.index.get(target)
    if not row:
        return {"undone": False, "action": "absent"}
    path = Path(row["path"])
    before = path.read_text(encoding="utf-8")
    after = frontmatter.set_keys(before, {
        "retired": True,
        "retired_date": now.date().isoformat(),
        "retired_reason": f"cos auto-capture undo ({_ts(now)})",
    })
    rel = path.relative_to(core.vault).as_posix()
    core.write_note(rel, after, reason=f"cos-undo audited retirement: {target}")
    return {"undone": True, "action": "retired", "retired_id": target}

def hold_cancel(vault, ident: str) -> bool:
    """Cancel a held item (the one-word revert). Thin wrapper over the full
    undo state machine — kept returning a bool for the CLI's exit code."""
    return bool(hold_undo(vault, ident)["undone"])

__all__ = ['_undos_path', '_undone_before', '_write_released_marker', '_hold_evidence', '_signed_target_id', 'undo_state', 'hold_undo', '_hold_undo_locked', '_retire_signed_note', 'hold_cancel']
