"""Ledger-bound discard of the drafts one named COS run superseded.

This is deliberately not a fourth ordinary mutation verb.  The browser half's
generic allowlist continues to reject ``DeleteItem``; this module reaches one
separate page operation that can delete only an exact, signed draft item after
re-reading it in Drafts.  Every eligible item comes from THIS LANE's own
write-ahead ledgers — whichever run wrote it, because a thread keeps one draft
and last week's is the loser (owner ruling 2026-08-28) — and every delete gets
a distinct write-ahead row. The named run selects the THREADS, never the
losers; that scoping lives in the manifest and is enforced here by consuming
its exact entry list.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cos_mutate_discard_closure as closure
import cos_mutate_discard_manifest as manifest_stages


DISCARD_VERB = "discard-draft"
DISCARD_VERIFICATION = "verified-draft-discarded"
FORWARD_VERIFICATION = "verified-draft-saved"
DISCARD_MANIFEST_SCHEMA = manifest_stages.DISCARD_MANIFEST_SCHEMA
#: The lane's no-op, one module over (the 500-LOC bound).
DISCARD_REFUSAL_LIMIT = closure.DISCARD_REFUSAL_LIMIT
DISCARD_CLOSED_STATE = closure.DISCARD_CLOSED_STATE
DISCARD_CLOSED_VERIFICATION = closure.DISCARD_CLOSED_VERIFICATION
standing_refusal = closure.standing_refusal
close_backlog = closure.close_backlog

DISCARD_RECEIPTS = {
    "is_draft": True,
    "signature_present": True,
    "send_attempted": False,
    "item_id_matches": True,
    "conversation_matches": True,
    "signature_matches": True,
    "signature_unambiguous": True,
    "sent_evidence_absent": True,
    "request_not_send": True,
    "absent_from_drafts": True,
    "drafts_enumeration_complete": True,
}

def build_discard_manifest(vault: Path, source_run: str, *,
                           lane) -> dict[str, Any]:
    return manifest_stages.build(
        vault, source_run, lane=lane, forward_candidate=_forward_candidate,
        manual_projector=_manual)


def _manual(row: dict[str, Any], reason: str, *, lane) -> dict[str, str]:
    """A non-sensitive pointer back to a row a human must resolve."""
    return {
        "conversation_id": lane.short(str(row.get("conversation_id") or "")),
        "draft_item_id": lane.short(str(row.get("new_item_id") or "")),
        "reason": reason,
    }


def _forward_candidate(row: dict[str, Any], run_id: str, *, lane
                       ) -> tuple[dict[str, Any] | None, str | None]:
    """Project one forward ledger row onto the closed discard admission set."""
    cid = row.get("conversation_id")
    item_id = row.get("new_item_id")
    receipts = row.get("receipts")
    if row.get("run") != run_id:
        return None, "the row is not bound to this named run"
    if not isinstance(cid, str) or not cid:
        return None, "the row records no conversation id"
    # ponytail: the exact-equality admission guards are a table, not a branch
    # each, so adding one is a row rather than another rung of complexity.
    # Order is load-bearing: the FIRST mismatch names the refusal reason.
    # `dispatched` stays OUT of this table on purpose — it is an identity
    # check (`is not True`), and 1 == True, so a table equality would admit a
    # row whose proof-of-dispatch is the integer 1.
    for field, expected_value, refusal in (
        ("idempotency_key", f"{cid}|draft",
         "the row is not the named run's draft-save key"),
        ("state", "reconciled",
         "the row is not a reconciled, verified draft save"),
        ("verification", FORWARD_VERIFICATION,
         "the row is not a reconciled, verified draft save"),
        ("primitive", lane.PRIMITIVE["draft"],
         "the row is not the audited draft-save primitive"),
        ("mutation_lane", lane.MUTATION_LANE,
         "the row is not bound to this mutation lane"),
    ):
        if row.get(field) != expected_value:
            return None, refusal
    if row.get("dispatched") is not True:
        return None, "the row does not prove this run dispatched the draft save"
    if str(row.get("destination_folder") or "").casefold() \
            != lane.DRAFT_FOLDER.casefold():
        return None, "the row does not record Drafts as its destination"
    if not isinstance(item_id, str) or not item_id:
        return None, "the row records no saved draft item id"
    if not isinstance(receipts, dict) \
            or receipts.get("is_draft") is not True \
            or receipts.get("signature_present") is not True \
            or receipts.get("send_attempted") is not False:
        return None, "the row lacks the exact draft/signature/no-send receipts"

    expected = lane.draft_signature(run_id, cid)
    recorded = row.get("signature")
    # Runs predating S11 did not serialize the literal signature even though
    # their verified receipt proves the page re-read it.  It is safe to rebuild
    # only the deterministic value the same engine always generated from this
    # row's run + conversation; no mailbox value or free input is guessed.
    if recorded not in (None, "") and recorded != expected:
        return None, "the recorded signature contradicts this run's identity"
    if not expected or not expected.startswith(f"[cos:{run_id}:") \
            or not expected.endswith("]"):
        return None, "the run-specific signature is missing or malformed"
    return ({"row": row, "conversation_id": cid, "item_id": item_id,
             "signature": expected, "owner_run": run_id,
             "signature_source": ("ledger" if recorded else
                                  "run-derived-from-verified-receipt")}, None)


def discard_candidates(ledger, run_id: str, *, lane
                       ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Every distinct verified draft-save row, plus all undecidable rows."""
    rows = [r for r in ledger.rows() if r.get("verb") == "draft"]
    outcome_keys = {r.get("idempotency_key") for r in rows
                    if r.get("state") != "intent"}
    eligible: list[dict[str, Any]] = []
    manual: list[dict[str, str]] = []
    for row in rows:
        if row.get("state") == "intent" \
                and row.get("idempotency_key") in outcome_keys:
            continue
        candidate, reason = _forward_candidate(row, run_id, lane=lane)
        if candidate is not None:
            eligible.append(candidate)
        else:
            manual.append(_manual(row, str(reason), lane=lane))

    # One item id must name one identity. A corrupted/tampered ledger that binds
    # it to two conversations or signatures is manual in both directions.
    identities: dict[str, set[tuple[str, str]]] = {}
    for c in eligible:
        identities.setdefault(c["item_id"], set()).add(
            (c["conversation_id"], c["signature"]))
    conflicted = {item for item, ids in identities.items() if len(ids) != 1}
    if conflicted:
        kept = []
        for c in eligible:
            if c["item_id"] in conflicted:
                manual.append(_manual(c["row"],
                                      "the draft item id has conflicting ledger identities",
                                      lane=lane))
            else:
                kept.append(c)
        eligible = kept

    # Repeated transitions for one saved item do not authorize repeated
    # deletes. Keep the last verified row for that exact item.
    by_item: dict[str, dict[str, Any]] = {}
    for c in eligible:
        by_item[c["item_id"]] = c
    unique_manual = {(m["conversation_id"], m["draft_item_id"], m["reason"]): m
                     for m in manual}
    return list(by_item.values()), list(unique_manual.values())


def _discard_key(candidate: dict[str, Any], *, lane) -> str:
    return (f"{candidate['conversation_id']}|{DISCARD_VERB}|"
            f"{lane.short(candidate['item_id'])[:16]}")


def _discard_intent(candidate: dict[str, Any], run_id: str, account: str,
                    *, lane) -> dict[str, Any]:
    forward = candidate["row"]
    return {
        "idempotency_key": _discard_key(candidate, lane=lane),
        "conversation_id": candidate["conversation_id"],
        "conversation_id_digest": lane.short(candidate["conversation_id"]),
        "verb": DISCARD_VERB,
        "state": "intent",
        "reason": f"discard of this run's verified signed draft ({run_id})",
        "account": account,
        "message_id": forward.get("message_id"),
        "key_scheme": "draft-item-id",
        "thread_id": candidate["conversation_id"],
        "mutation_lane": lane.MUTATION_LANE,
        "original_folder": lane.DRAFT_FOLDER,
        "destination_folder": lane.DRAFT_DISCARD_DESTINATION,
        "action_ts": lane._ts(),
        "primitive": lane.PRIMITIVE[DISCARD_VERB],
        "connector_result": None,
        "verification": None,
        "chip": None,
        "mode": None,
        "before_image": None,
        "item_id_at_resolve": candidate["item_id"],
        "changekey_refetched_at": None,
        "run": run_id,
        "new_item_id": candidate["item_id"],
        "dispatched": None,
        "receipts": None,
        "observed_after": None,
        "signature": candidate["signature"],
    }


def _verified_page_result(out: dict[str, Any], *, lane) -> bool:
    """Require every positive proof before the host records reconciliation."""
    receipts = out.get("receipts")
    if not isinstance(receipts, dict):
        return False
    if any(receipts.get(key) is not expected
           for key, expected in DISCARD_RECEIPTS.items()):
        return False
    return (str(receipts.get("source_folder") or "").casefold()
            == lane.DRAFT_FOLDER.casefold()
            and out.get("state") == "reconciled"
            and out.get("dispatched") is True
            and out.get("verification") == DISCARD_VERIFICATION)


def _every_run_ledger(vault: Path, *, lane):
    """Every run's ledger, oldest first — one (run_id, ledger) pair each.

    The manifest is built across ALL of them (owner ruling 2026-08-28: a thread
    keeps one draft, whichever run wrote the losers), so anything that has to
    RECOGNIZE what the manifest named must read the same surface. Reading only
    the current run's ledger here is what made run197 refuse all 23 of its own
    selected losers.
    """
    from brain import cos                                      # noqa: PLC0415
    prefix, suffix = "_cos_undo_ledger_", ".jsonl"
    for path in sorted(cos.run_ops_dir(vault).glob(f"{prefix}*{suffix}")):
        run_id = path.name[len(prefix):-len(suffix)]
        yield run_id, lane.UndoLedger(vault, run_id)


def _manifest_candidates(vault: Path, run_id: str, manifest_path: Path, *,
                         lane):
    manifest = manifest_stages.load_verified(
        manifest_path, vault, lane=lane, source_run=run_id,
        forward_candidate=_forward_candidate, manual_projector=_manual)
    ledger = lane.UndoLedger(vault, run_id)
    # `identity_key` binds the OWNING run, and the manifest keys each loser by
    # the run that wrote it. Rebuild the same way or a loser from an earlier
    # night can never match, whatever the manifest selected. Only this run's
    # refusals are reported as manual, matching the manifest's own scoping.
    by_identity: dict[str, dict[str, Any]] = {}
    manual: list[dict[str, str]] = []
    for owner_run, owner_ledger in _every_run_ledger(vault, lane=lane):
        available, owner_manual = discard_candidates(
            owner_ledger, owner_run, lane=lane)
        if owner_run == run_id:
            manual = owner_manual
        for candidate in available:
            by_identity[manifest_stages.identity_key(
                owner_run, candidate["conversation_id"],
                candidate["item_id"], candidate["signature"])] = candidate
    missing = [entry.get("identity_key") for entry in manifest["entries"]
               if entry.get("identity_key") not in by_identity]
    if missing:
        raise lane.MutationStop(
            "draft-discard manifest names an item this lane's eligible ledger "
            "set does not contain")
    candidates = [by_identity[entry["identity_key"]]
                  for entry in manifest["entries"]]
    return manifest, ledger, candidates, manual


def _prior_discards(vault: Path, *, lane) -> dict[str, list[dict[str, Any]]]:
    """Every discard row this lane has ever written, keyed by identity.

    `_discard_key` names a conversation and an item, never a run, so a discard
    recorded on an EARLIER night answers for the same item tonight. Reading one
    ledger would re-attempt an item already in Deleted Items. The whole HISTORY
    per key, not the last row: a standing refusal is a property of the sequence
    and the last row alone cannot show it.
    """
    prior: dict[str, list[dict[str, Any]]] = {}
    for _owner_run, owner_ledger in _every_run_ledger(vault, lane=lane):
        for row in owner_ledger.rows():
            if row.get("verb") == DISCARD_VERB:
                prior.setdefault(str(row.get("idempotency_key") or ""),
                                 []).append(row)
    return prior


def _partition_candidates(prior: dict[str, list[dict[str, Any]]],
                          candidates: list[dict[str, Any]], *,
                          lane) -> tuple[list[dict[str, Any]],
                                         list[dict[str, Any]],
                                         list[dict[str, str]],
                                         list[dict[str, Any]]]:
    pending = []
    already = []
    manual = []
    closed = []
    for candidate in candidates:
        history = prior.get(_discard_key(candidate, lane=lane)) or []
        old = history[-1] if history else None
        seen = {"conversation_id": lane.short(candidate["conversation_id"]),
                "draft_item_id": lane.short(candidate["item_id"])}
        if old is None:
            pending.append(candidate)
        elif closure.already_closed(history):
            # Already closed on an earlier night. Recording it twice would make
            # the ledger count one no-op once per run forever.
            closed.append({**seen, "cause": str(old.get("connector_result")),
                           "record": False, "candidate": candidate})
        elif old.get("state") == "reconciled" \
                and old.get("verification") == DISCARD_VERIFICATION:
            already.append(seen)
        elif old.get("state") == "unknown" and old.get("dispatched") is False:
            # The driver REFUSED before any server call (`dispatched: false`
            # means `inspectDraftCandidate` returned ineligible and never
            # reached `sendDraftDiscard`). Nothing was sent, so this is a known
            # non-attempt, not the response-lost `unknown` (dispatched None)
            # that must never replay. A later run with corrected inputs retries
            # — until the SAME refusal has stood for `DISCARD_REFUSAL_LIMIT`
            # attempts, at which point retrying is the fabrication, not the
            # closing row.
            cause = standing_refusal(history)
            if cause is None:
                pending.append(candidate)
            else:
                closed.append({**seen, "cause": cause, "record": True,
                               "candidate": candidate})
        else:
            manual.append(_manual(
                candidate["row"],
                f"a prior discard attempt is {old.get('state')}; it is never "
                "replayed blindly", lane=lane))
    return pending, already, manual, closed


def _dispatch_candidates(vault: Path, run_id: str, ledger, bridge, account: str,
                         pending: list[dict[str, Any]], *, lane
                         ) -> tuple[list[dict[str, Any]],
                                    list[dict[str, str]], str | None]:
    results: list[dict[str, Any]] = []
    manual: list[dict[str, str]] = []
    stopped = None
    for candidate in pending:
        if lane.stopped(vault, run_id):
            stopped = f"the stop file {lane.stop_file(vault, run_id)} appeared"
            break
        base = _discard_intent(candidate, run_id, account, lane=lane)
        # WRITE AHEAD of every server call in the page operation, including its
        # immediate re-read. The page cannot dispatch before this row is fsync'd.
        ledger.append(base)
        # The driver validates the signature against `run_id`, and each draft
        # carries the signature of the run that CREATED it — not this discard
        # pass's run. A run that re-drafts a thread signs the newest draft;
        # last week's loser on the same thread still holds last week's run
        # signature. Send the owner run so the identity check matches.
        request = {"conversation_id": candidate["conversation_id"],
                   "item_id": candidate["item_id"],
                   "signature": candidate["signature"],
                   "run_id": candidate["owner_run"]}
        try:
            out = bridge.call("discard-draft", {"candidate": request})["out"]
        except lane.MutationStop as exc:
            # A dropped response may mean applied. Record unknown and never
            # replay it blindly; the item moves to manual resolution.
            ledger.append(dict(
                base, state="unknown",
                connector_result="response-lost-manual-resolution",
                verification="draft discard response lost; never replay blindly",
                dispatched=(True if getattr(exc, "mutation_in_flight", False)
                            else None)))
            manual.append(_manual(
                candidate["row"],
                "the discard response was lost; server outcome is unknown",
                lane=lane))
            results.append({
                "conversation_id": lane.short(candidate["conversation_id"]),
                "draft_item_id": lane.short(candidate["item_id"]),
                "state": "unknown", "dispatched": None})
            stopped = str(exc)[:300]
            break

        verified = _verified_page_result(out, lane=lane)
        state = str(out.get("state") or "unknown") if verified else "unknown"
        if state not in lane.STATES:
            state = "unknown"
        verification = (out.get("verification") if verified
                        else "draft discard receipts incomplete; manual "
                             "resolution required")
        ledger.append(dict(
            base, state=state,
            connector_result=out.get("response_code") or out.get("outcome"),
            verification=verification, dispatched=out.get("dispatched"),
            receipts=out.get("receipts"),
            changekey_refetched_at=out.get("changekey_refetched_at")))
        results.append({
            "conversation_id": lane.short(candidate["conversation_id"]),
            "draft_item_id": lane.short(candidate["item_id"]),
            "state": state, "dispatched": out.get("dispatched"),
            "verification": verification, "receipts": out.get("receipts")})
        if not verified:
            manual.append(_manual(
                candidate["row"],
                str(out.get("outcome")
                    or "the current server state refused verification"),
                lane=lane))
    return results, manual, stopped


def discard_drafts_pass(vault, run_id: str, tab_id: int | None, *,
                        use_cdp: bool = False, use_ego: bool = False,
                        manifest_path: Path | None = None,
                        limit: int | None = None, lane=None) -> dict[str, Any]:
    """Discard only exact signed draft items proved by this run's ledger."""
    root = lane.assert_vault(vault)
    if limit is not None:
        raise lane.MutationStop(
            "draft discard no longer accepts a prefix limit; build and consume "
            "the exact duplicate-safe manifest")
    if manifest_path is None:
        raise lane.MutationStop(
            "draft discard requires --discard-manifest; a named run alone "
            "mixes duplicate items with the survivor")
    account = str(root["BRAIN_VAULT"])
    manifest, ledger, candidates, manual = _manifest_candidates(
        vault, run_id, manifest_path, lane=lane)
    prior = _prior_discards(vault, lane=lane)
    pending, already, replay_manual, closing = _partition_candidates(
        prior, candidates, lane=lane)
    manual.extend(replay_manual)
    # BEFORE the pending check: a night with nothing left to dispatch is exactly
    # the night that must still be able to record its own no-op. And the sweep
    # is over EVERY identity the lane has ever refused, not only the ones this
    # run's manifest happens to name — an identity whose thread is never
    # drafted again leaves the manifest forever, which is how the one live
    # standing refusal survived six nights of a closure written to end it.
    ts = lane._ts()
    closed = closure.record(
        ledger, closing,
        lambda c: _discard_intent(c, run_id, account, lane=lane), ts=ts)
    closed.extend(closure.close_backlog(
        ledger, prior, {_discard_key(c, lane=lane) for c in candidates}, ts=ts))
    if not pending:
        return {"run_id": run_id, "discarded": 0, "attempted": 0,
                "eligible": len(candidates), "already_discarded": already,
                "needs_manual_resolution": manual, "closed": closed,
                "results": [],
                "stopped": None, "manifest_digest": manifest["manifest_digest"],
                "vault_root_asserted": root}

    # Reuse the live lane's exact preflight: asserted vault, literal kill
    # switch, fresh receipt-bearing E17 canary and approved shape store.
    pre = lane.apply_stages.preflight(vault, lane)
    shapes = pre["shapes"]
    if not (shapes.get("shapes") or {}).get(lane.DRAFT_DISCARD_SHAPE):
        raise lane.MutationStop(
            "no approved captured draft-discard shape is on file. Delete one "
            "disposable draft in the owner UI with capture armed, import that "
            "shape, then retry; this lane never synthesizes DeleteItem")
    bridge = lane._bridge_for(tab_id, shapes["shapes"], run_id,
                              use_cdp=use_cdp, use_ego=use_ego)
    results, dispatch_manual, stopped = _dispatch_candidates(
        vault, run_id, ledger, bridge, account, pending, lane=lane)
    manual.extend(dispatch_manual)

    return {
        "run_id": run_id,
        "discarded": sum(r.get("verification") == DISCARD_VERIFICATION
                         and r.get("state") == "reconciled" for r in results),
        "attempted": len(results), "eligible": len(candidates),
        "already_discarded": already,
        "needs_manual_resolution": manual, "closed": closed,
        "results": results, "stopped": stopped,
        "manifest_digest": manifest["manifest_digest"],
        "kill_switch": pre["kill_switch"], "e17": pre["e17"],
        "shapes_path": shapes["path"], "vault_root_asserted": root,
    }
