"""Decide and EXECUTE one candidate: never-category, duplicate, quarantine, drop.

Moved verbatim out of `cos_ingest_bridge` (file-size ratchet split); every
comment travels with its code and no behaviour changes. The facade re-imports
every name, so `tools.cos_ingest_bridge` keeps exporting them.
Covered by the `tests/test_cos_ingest_bridge_*.py` slices.

`provenance` and `cos` are imported as MODULES on purpose: the suite patches
`bridge.provenance.email_classification` and `bridge.cos.propose`, which are
attributes of the shared `brain` module objects — a `from brain.provenance
import email_classification` here would bind the function and go deaf to that
patch (the split's one real hazard).
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from brain import cos, provenance
from brain.notes import sha256_text

from tools.cos_ingest_bridge_content import (
    CONTENT_ATTACHMENTS, CONTENT_BOTH, CONTENT_TEXT, _anomaly_doc,
    _anomaly_nid, _attachment_names, _proposal_content, content_choice_for)
from tools.cos_ingest_bridge_store import (
    _SETTLEMENT_CLAIM_KEYS, _bridge_ident, _claim_settlement, _conv_key,
    _row_shape, _write_manifest_lines)


def _corpus_text(row: dict, crow: dict | None, report: dict,
                 run_id: str) -> tuple[str | None, dict | None]:
    """(text, defect). All three failures quarantine the candidate rather
    than drop header-only content — preserving nothing under a real-looking
    drop is the worse outcome."""
    if crow is None:
        why = ("corpus-row-missing" if not report.get("corpus_error")
               else f"corpus-unreadable ({report['corpus_error']})")
        return None, {"reason": why, "detail": f"run {run_id} has no corpus row "
                      "for this conversation_id — nothing to build a candidate "
                      "from, and never drop header-only content"}
    text = str(crow.get("text") or "")
    if not text.strip():
        return None, {"reason": "corpus-text-empty", "detail": "the corpus row "
                      "exists but carries no body text — a drop built from it "
                      "would preserve nothing"}
    if str(crow.get("text_sha256") or "") != sha256_text(text):
        return None, {"reason": "corpus-digest-mismatch", "detail": "the corpus "
                      "row's text_sha256 does not hash to its own text — the "
                      "corpus is damaged; refusing unverifiable content"}
    return text, None


def _quarantine_candidate(vault, row: dict, outcome: dict, *, reason: str,
                          detail: str, run_id: str, key: str,
                          now: _dt.datetime, dry_run: bool) -> None:
    """Park ONE candidate: outcome + add-only ledger stamp + evidence doc in
    the engine's claim-quarantine store (reused, never a second quarantine).
    NOT silent skipping: the reason rides the report, the bridge ledger, the
    candidate row and the engine's defect log.

    The evidence is written EVERY time, on purpose. Skipping the write when
    `<nid>.md` already existed lost the second cause: a conversation that
    quarantined for one reason and then failed for a DIFFERENT one kept the
    stale doc and never reached `cos._quarantine_claim`, so the new defect was
    logged nowhere. The engine primitive is already idempotent per (id, code),
    so the same reason twice still writes one defect row — the guard bought
    nothing and cost the second reason."""
    outcome.update({"outcome": "quarantined", "reason": reason,
                    "detail": detail})
    if dry_run:
        return
    # THE HOST-PRIVATE SETTLEMENT RECORD, before the mount-side claim
    # (attempt 13): E16 exempts a stamp-less row only when this record backs
    # the row's `bridge_quarantined` field — the field alone is a VM-writable
    # ledger claim. Record first: a crash between the two leaves a record
    # with no claim (inert) rather than a claim no record backs (a night
    # failing E16 until the re-run re-settles).
    cos.record_bridge_settlement(vault, run_id=run_id, conversation_key=key,
                                 kind="quarantined", detail=reason, now=now)
    _claim_settlement(row, "bridge_quarantined", reason)
    nid = _anomaly_nid(key)
    text = _anomaly_doc(nid, run_id=run_id, row=row, reason=reason,
                        detail=detail, now=now)
    cos._quarantine_claim(vault, nid=nid, text=text,
                          sha=sha256_text(text), code=f"bridge-{reason}",
                          reason=detail, run_id=run_id, now=now,
                          source="cos-ingest-bridge")


def _clear_anomaly(vault, key: str) -> bool:
    """A settled conversation (dropped or delivered) resolves its standing
    anomaly doc — otherwise the quarantine store alerts forever on a fixed
    cause. Only the bridge's OWN evidence docs, never a real parked claim."""
    nid = _anomaly_nid(key)
    qdir = cos.claim_quarantine_dir(vault)
    cleared = False
    for suffix in (".md", ".json"):
        p = qdir / f"{nid}{suffix}"
        if p.is_file():
            p.unlink(missing_ok=True)
            cleared = True
    return cleared


def _never_category(vault, row: dict, outcome: dict, *, category: str,
                    taxonomy: dict, run_id: str, key: str,
                    now: _dt.datetime, dry_run: bool) -> bool:
    """NEVER-CATEGORY: zero drops (owner rule 2026-08-17 #2), reason on the
    row. True = the candidate is settled. The settlement is host-recorded
    BEFORE the row claim (attempt 13) — E16's exemption reads the record,
    never the claim."""
    _cat, disposition = cos.resolve_category(
        vault, category, lane=cos.LANE_TEXT, taxonomy=taxonomy)
    if disposition != cos.DISPOSITION_NEVER:
        return False
    outcome.update({"outcome": "never", "reason": f"never-ingest category "
                    f"{category!r} (overlay cos/ingest.md) — zero drops by "
                    "owner rule 2026-08-17 #2"})
    if not dry_run:
        cos.record_bridge_settlement(
            vault, run_id=run_id, conversation_key=key, kind="never-category",
            detail=f"never-ingest category {category!r}", now=now)
        _claim_settlement(row, "bridge_refused", "never-category")
    return True


def _same_run_duplicate(vault, row: dict, outcome: dict, prior: dict, *,
                        run_id: str, key: str, now: _dt.datetime,
                        dry_run: bool) -> None:
    """SAME-RUN DUPLICATE ROW (owner ruling): two candidate rows for one
    conversation inside ONE run is rule-1 idempotency — one conversation,
    one settlement, and the second row mirrors the first's verdict. Only
    SAME-WORK rows reach here (attempt 15): `_decide_candidate`'s shape gate
    sends a diverging duplicate to quarantine instead of this mirror.

    BOTH branches settle the row as a DUPLICATE — record + ledger claim
    (attempt 16, finding 1). The quarantined-prior branch used to set
    neither: E16 then saw a stamp-less row claiming nothing, `score_row`
    FAILed it on the missing proposal_id, and the duplicate of a quarantined
    conversation took the night to RUN_INVALID — the quarantine breaking the
    night it exists to save, returned through the duplicate path."""
    if prior.get("quarantined"):
        outcome.update({"outcome": "quarantined",
                        "reason": prior.get("reason") or "quarantined",
                        "detail": "duplicate row of a conversation this pass "
                        "already quarantined — counted once",
                        "duplicate_of": prior["ident"]})
    else:
        outcome.update({"outcome": "already-dropped", "ident": prior["ident"],
                        "prior_drop": prior.get("prior_drop", "this-run"),
                        "reason": "a duplicate candidate row for this "
                        "conversation — this run already settled it earlier "
                        "in this pass; one conversation, one drop "
                        "(idempotent skip)"})
        if prior.get("sha256"):
            outcome["proposal_sha256"] = prior["sha256"]
    if not dry_run:
        # honest ledger record of the skip, add-only key; never the drop
        # stamps — those belong to the ONE row the drop was made from.
        # Host settlement record first (attempt 13): E16's exemption for
        # this claim reads the record, never the claim.
        cos.record_bridge_settlement(
            vault, run_id=run_id, conversation_key=key, kind="duplicate",
            detail=f"same-run duplicate row of {prior['ident']}", now=now)
        _claim_settlement(row, "bridge_duplicate_of", prior["ident"])


def _candidate_content(run_id: str, row: dict, *, choice: str,
                       crow: dict | None, report: dict
                       ) -> tuple[str | None, list[str], dict | None]:
    """(text, attachment names, defect). A defect quarantines the candidate:
    corpus failures for text-carrying choices, and a file-carrying choice
    whose row names no attachment filename (the bridge refuses to guess a
    file the manifest would claim)."""
    text: str | None = None
    if choice in (CONTENT_TEXT, CONTENT_BOTH):
        text, defect = _corpus_text(row, crow, report, run_id)
        if defect:
            return None, [], defect
    wants_files = choice in (CONTENT_ATTACHMENTS, CONTENT_BOTH)
    attachments = _attachment_names(row) if wants_files else []
    if wants_files and not attachments:
        return None, [], {
            "reason": "attachment-names-missing",
            "detail": f"content_choice {choice!r} but the ledger row names no "
            "attachment filename — the bridge refuses to guess a file the "
            "manifest would claim"}
    return text, attachments, None


def _decide_candidate(vault, run_id: str, row: dict, outcome: dict, *,
                      taxonomy: dict, corpus: dict, report: dict,
                      manifest_path: Path, known_keys: set[str],
                      handled: dict[str, dict], consumed: set[str],
                      flush, now: _dt.datetime, dry_run: bool) -> None:
    """Decide and (unless dry-run) EXECUTE one candidate, in place on
    ``outcome`` and the ledger ``row``. Every exit sets `outcome["outcome"]`,
    so the tally and the bridge ledger agree by construction.

    NO DELIVERY VERDICT (attempt 14). The only ways a candidate does NOT
    produce a fresh drop are decided from THIS pass's own input: the
    never-category rule, a SAME-WORK duplicate row of a conversation this
    same pass already settled (``handled``, in-memory — a duplicate row that
    names DIFFERENT work quarantines as ambiguity instead of being absorbed,
    attempt 15), and a content defect. No recorded stamp, outcome, custody
    or drop file is consulted for any of it. The ONE recorded thing read —
    ``consumed``, the claims ledger's sha set, AFTER the drop is written —
    can only turn a drop the engine will delete unseen into a loud
    quarantine (`_execute_fresh_drop`); it can never make this function skip
    silently or report a delivery."""
    cid = str(row.get("conversation_id") or "")
    category = str(row.get("category") or "").strip()
    ident = _bridge_ident(run_id, cid)
    key = _conv_key(cid)

    if _never_category(vault, row, outcome, category=category,
                       taxonomy=taxonomy, run_id=run_id, key=key, now=now,
                       dry_run=dry_run):
        return

    choice = content_choice_for(category, taxonomy)
    outcome["content_choice"] = choice
    shape = _row_shape(row)

    prior = handled.get(key)
    if prior is not None:
        if prior.get("shape") == shape:
            _same_run_duplicate(vault, row, outcome, prior, run_id=run_id,
                                key=key, now=now, dry_run=dry_run)
        else:
            # DIVERGING DUPLICATE ROWS (attempt 15, Codex :686-690): two rows
            # for one conversation naming DIFFERENT work — another category,
            # another tier claim, other attachments — are not idempotency,
            # and mirroring the first verdict silently dropped the second.
            # The bridge cannot know which one the judge meant, so the
            # ambiguity is PARKED with both shapes in the evidence; the row
            # already settled stays settled.
            _quarantine_candidate(
                vault, row, outcome, reason="duplicate-rows-diverge",
                detail=("a second candidate row for this conversation names "
                        "DIFFERENT work than the row this pass settled as "
                        f"{prior.get('ident')}: (category, classification, "
                        f"attachments) {prior.get('shape')!r} vs {shape!r} — "
                        "absorbing one into the other silently drops it; "
                        "fix the judgment ledger and replay"),
                run_id=run_id, key=key, now=now, dry_run=dry_run)
        return

    crow = corpus.get(cid)
    text, attachments, defect = _candidate_content(
        run_id, row, choice=choice, crow=crow, report=report)
    if defect is not None:
        _quarantine_candidate(vault, row, outcome, reason=defect["reason"],
                              detail=defect["detail"], run_id=run_id,
                              key=key, now=now, dry_run=dry_run)
        handled[key] = {"ident": ident, "quarantined": True,
                        "reason": defect["reason"], "shape": shape}
        return

    # THE TIER, from the one engine classifier: MNPI default; the row's claim
    # and the category floor can only RAISE. The bridge cannot lower a tier.
    tier, _why = provenance.email_classification(
        vault, proposed=row.get("classification"), category=category)
    outcome["tier"] = tier

    if dry_run:
        # The rehearsal reports drop INTENT. It does not predict the sweep's
        # replay verdict: that needs the STAGED bytes' sha, which only the
        # real `cos.propose` (via `capture.enforce`) computes — a rehearsal
        # duplicating that pipeline read-only would be a second copy of the
        # path rule, the exact debt these reviews keep killing.
        outcome.update({"outcome": "dropped (dry-run)", "ident": ident,
                        "manifest_lines": list(attachments)})
        handled[key] = {"ident": ident, "sha256": None,
                        "prior_drop": "this-run", "shape": shape}
        return

    _execute_fresh_drop(
        vault, run_id, row, outcome, choice=choice, tier=tier, text=text,
        crow=crow, attachments=attachments, ident=ident, key=key,
        manifest_path=manifest_path, known_keys=known_keys,
        handled=handled, consumed=consumed, shape=shape, flush=flush, now=now)


def _execute_fresh_drop(vault, run_id: str, row: dict, outcome: dict, *,
                        choice: str, tier: str, text: str | None,
                        crow: dict | None, attachments: list[str], ident: str,
                        key: str, manifest_path: Path, known_keys: set[str],
                        handled: dict[str, dict], consumed: set[str],
                        shape: tuple, flush, now: _dt.datetime) -> None:
    """Write the drop for one candidate, in this order: flush pending stamps,
    MANIFEST LINE, drop, REPLAY OBSERVATION, stamps."""
    # PERSIST PENDING STAMPS BEFORE THIS DROP HITS DISK: with the ledger
    # rewrite batched (O(N), not once per candidate), this is what keeps the
    # crash window at today's size — at any instant at most ONE drop on disk
    # lacks its persisted stamp, and an unstamped drop is recovered by
    # RE-DROPPING onto the same ident filename, never by adopting its bytes.
    flush()
    # ATTACHMENT INTENT BEFORE THE DROP PUBLISHES (attempt 10, finding 2).
    # The manifest line is the durable record the ingest sweep consumes, so
    # it lands FIRST: a crash after `cos.propose` used to leave a published
    # drop with no durable attachment intent — the retry adopted the drop as
    # already-delivered, detected no missing line (the row's
    # attachment_manifest was never persisted), exited clean, and the
    # attachment silently never entered the sweep. Written before the drop,
    # every crash ordering retries to a complete state: line-without-drop
    # re-drops (the line dedups), drop-consumed-without-stamp skips with the
    # line already durable.
    _write_manifest_lines(vault, run_id, row, crow, names=attachments,
                          tier=tier, path=manifest_path, outcome=outcome,
                          known_keys=known_keys, now=now)
    # ONE atomic write at the deterministic ident: a leftover there — crash
    # residue, a prior pass's still-in-flight drop, or a forgery — is simply
    # overwritten by the bridge's own bytes. No attempt counter, no
    # replay-avoidance loop (attempt 14): the content is deterministic per
    # run, and whether these exact bytes were already claimed, rejected or
    # expired is the ENGINE's replay guard's call at sweep time, not ours.
    res = cos.propose(vault, _proposal_content(
        run_id=run_id, row=row, choice=choice, tier=tier, text=text,
        corpus_row=crow, attachments=attachments, now=now), ident=ident)
    # THE REPLAY OBSERVATION (attempt 15). The guard's call is still the
    # engine's — but the bridge reads what that call WILL BE, from the same
    # authority the sweep reads (`_claim_text_drops`: any claims-ledger entry
    # with this sha, whatever its disposition), and refuses to exit clean
    # over a write the engine deletes unseen. Deterministic bytes made that
    # deletion PERMANENT: no later pass ever stages different bytes, so the
    # candidate was silently gone forever. It quarantines instead — reason,
    # ident and digest in the evidence, counted toward the threshold. ONE
    # carve-out, verified on bytes: the engine's own claim quarantine parked
    # these exact bytes as HELD custody (`claim-quarantine/<ident>.md`, the
    # crash-between-drop-and-stamp recovery). Those bytes are waiting for
    # the very stamps this pass writes — the hourly release delivers them —
    # so quarantining here would strand a healthy heal. A planted held copy
    # buys an attacker nothing suppressive: it routes the SAME bytes to the
    # owner through the release path instead.
    if res["sha256"] in consumed:
        held = cos.claim_quarantine_dir(vault) / f"{res['id']}.md"
        try:
            held_ok = held.is_file() and sha256_text(
                held.read_text(encoding="utf-8")) == res["sha256"]
        except OSError:
            held_ok = False
        if not held_ok:
            _quarantine_candidate(
                vault, row, outcome, reason="replay-rejected",
                detail=(f"the claims ledger already holds these exact bytes "
                        f"(drop {res['id']}, sha256 {res['sha256']}) and no "
                        "held claim-quarantine copy awaits release: the "
                        "engine's claim sweep will replay-reject and delete "
                        "this drop unseen, so this pass's write cannot reach "
                        "the owner — parked loudly instead of lost silently"),
                run_id=run_id, key=key, now=now, dry_run=False)
            handled[key] = {"ident": ident, "quarantined": True,
                            "reason": "replay-rejected", "shape": shape}
            return
    # THE STAMPS E16 READS AND THE CLAIM JOIN NEEDS. Without
    # `proposals_dropped: true` the verifier sees the flag deny a drop the host
    # took delivery of (hard FAIL); without id+digest the claim cannot
    # attribute it to this run.
    row.update({"proposal_id": res["id"], "content_sha256": res["sha256"],
                "proposals_dropped": True, "content_choice": choice})
    for k in _SETTLEMENT_CLAIM_KEYS:
        row.pop(k, None)
    outcome.update({"outcome": "dropped", "ident": res["id"],
                    "proposal_sha256": res["sha256"]})
    if _clear_anomaly(vault, key):
        outcome["anomaly_cleared"] = True
    handled[key] = {"ident": res["id"], "sha256": res["sha256"],
                    "prior_drop": "this-run", "shape": shape}
