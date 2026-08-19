"""Sub-steps of the run validator's CANDIDATE-STAMPS check (s18 extraction).

One function per stage of ``check_candidate_stamps`` (E16): the
applicability/denial decision and the per-row stamp scoring. The check itself
stays in :mod:`brain.cos_runverify` with an unchanged signature; this module
never imports it. The lane module's own callables and vocabularies
(``_drop_stamped``, ``_SHA256_RE``, ``_PLACEHOLDER_CATEGORIES``) arrive as
arguments, so their definitions stay single.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from . import cos
from .cos_runverify_checks import (DEGRADED, FAIL, INCONCLUSIVE, PASS, _row)


def settlement_claim(row: dict[str, Any]) -> str | None:
    """The bridge settlement this row CLAIMS, or ``None``.

    A CLAIM only: these fields sit on candidate rows in the ingestion ledger
    under ``cos.run_ops_dir`` — the directory the engine's own docstring calls
    VM-writable — so nothing here is evidence of anything."""
    if str(row.get("bridge_quarantined") or "").strip():
        return "quarantined"
    if str(row.get("bridge_refused") or "") == "never-category":
        return "never-category"
    if str(row.get("bridge_duplicate_of") or "").strip():
        return "duplicate"
    return None


def bridge_settled(row: dict[str, Any], *, drop_stamped: Callable,
                   settlements: dict[str, dict[str, int]]) -> str | None:
    """The bridge's non-drop settlement of this row, or ``None`` to score it.

    A candidate the INGESTION BRIDGE settled WITHOUT a drop is a legitimate
    stamp-less shape (s03 change 2): a quarantined row (``bridge_quarantined``
    — parked with evidence in the claim-quarantine store), a never-category
    row (``bridge_refused: never-category`` — zero drops by owner rule), or a
    duplicate of a conversation another row/run already settled
    (``bridge_duplicate_of``). Each of those has NO proposal to attribute, so
    demanding an id from it turned one quarantined thread into RUN_INVALID
    and made the night's genuinely good drops unclaimable — the quarantine
    design breaking the night it exists to save.

    THE EXEMPTION IS KEYED ON THE HOST'S OWN RECORD (s03 attempt 13, the
    CONSENSUS finding of both reviewers). The row's settlement fields live in
    the run's ingestion ledger under ``cos.run_ops_dir`` — VM-writable by the
    engine's own account — so on their own they are a claim, and honouring a
    claim here handed the untrusted leg the exemption: write "I was
    quarantined" on a row and E16 stops demanding evidence a proposal was
    ever made, the night scores claimable, the mail is archived, and nothing
    reached the owner. The bridge now records every non-drop settlement
    host-privately (``cos.record_bridge_settlement``, the same off-mount
    store as its delivery receipts), and ``settlements`` — loaded from that
    record via ``cos.bridge_settlements`` — is what exempts: a row claiming a
    settlement kind the host did not record for its conversation is NOT
    exempt and stays fully scored. Three guards, all required: any drop stamp
    on the row disables the exemption (a producer cannot hide a duplicate id
    or a digest mismatch behind a settlement marker); the claimed KIND must
    match the recorded kind (a host record is evidence of what it says, not a
    wildcard); and the record is COUNT-BOUND, not a reusable bearer token
    (s03 attempt 14, the round-6 finding): each exempted row SPENDS one
    recorded settlement event — ``settlements`` is mutated in place — so one
    genuine quarantine can never exempt a second, forged row naming the same
    conversation."""
    if drop_stamped(row):
        return None
    kind = settlement_claim(row)
    if kind is None:
        return None
    key = cos.bridge_conversation_key(row.get("conversation_id"))
    kinds = settlements.get(key)
    if not kinds or kinds.get(kind, 0) < 1:
        return None
    kinds[kind] -= 1
    return kind


def applicability_row(candidates: list[dict[str, Any]], vault, run_id: str, *,
                      drop_stamped: Callable) -> dict[str, Any] | None:
    """Decide whether E16's stamp clause applies; ``None`` means score it.

    APPLICABILITY IS DERIVED, AND A DENIAL BESIDE EVIDENCE IS A FAIL (review
    2026-08-13, round 2, K2). The flag alone used to decide, and nothing in
    production could set it True — so this control short-circuited on a value
    that was a constant, before inspecting one proposal id or digest. Now the
    HOST's own record answers first (``cos.run_proposal_drops``: the pending
    metas and quarantined claims it wrote when it took delivery, neither of
    which the run can forge), and the row's own stamps answer beside it.

    Three ways this can go, and only one of them is "does not apply":
      flag false + host silent + no stamps -> inapplicable, and SAID so
      flag false + (host receipt or stamps) -> CONTRADICTION, hard FAIL
      anything else                         -> score the stamps
    """
    denied = all(r.get("proposals_dropped") is False for r in candidates)
    host_drops = 0
    host_why = ""
    try:
        record = cos.run_proposal_drop_record(vault, run_id)
        host_drops = record["drops"]
        # A DAMAGED RECEIPT IS NOT A ZERO (review 2026-08-13, round 5, H6). The
        # loaders used to skip a receipt they could not parse, so an unreadable
        # or half-written one arrived here as "the host recorded no drops" —
        # the exact branch below that makes this control inapplicable. K2 then
        # failed OPEN on corruption: damage the receipt, and a candidate row
        # denying a drop it made is waved through as "does not apply".
        if record["malformed"]:
            host_why = (
                f"{record['malformed']} of the host's drop receipt(s) are "
                "unreadable or incomplete. An unreadable receipt carries no "
                "run id, so it can be attributed to no run — including this "
                "one — and reading it as an absence is how this control "
                "used to pass on damaged evidence")
    except Exception as exc:                                      # noqa: BLE001
        # The host record could not be read, so applicability is UNKNOWN — and
        # an unknown applicability may not be reported as "does not apply".
        host_why = f"the host's drop record could not be read: {str(exc)[:120]}"
    stamped = [r for r in candidates if drop_stamped(r)]
    if denied and (host_drops or stamped or host_why):
        if host_why:
            return _row("candidate_stamps", INCONCLUSIVE,
                        f"{len(candidates)} candidate row(s) say this run "
                        f"dropped no proposal, and {host_why}. Whether this "
                        "control applies is unknown, and an unknown "
                        "applicability is not an inapplicability",
                        reexecuted=False)
        return _row("candidate_stamps", FAIL,
                    f"{len(candidates)} candidate row(s) claim "
                    "`proposals_dropped: false` while the drop they deny is on "
                    "record: "
                    + "; ".join(filter(None, [
                        f"the host took delivery of {host_drops} candidate(s) "
                        f"for this run" if host_drops else "",
                        f"{len(stamped)} row(s) carry drop stamps "
                        f"({', '.join(sorted(set(k for r in stamped for k in drop_stamped(r))))})"
                        if stamped else ""]))
                    + ". The flag short-circuits this whole control, so a "
                      "producer that denies a drop it made would hide every "
                      "duplicate id and digest mismatch behind 'does not "
                      "apply' — that is the failure this refuses",
                    reexecuted=True)
    if denied:
        return _row("candidate_stamps", DEGRADED,
                    f"{len(candidates)} candidate row(s) staged; this run "
                    "dropped no proposal and the HOST's own record agrees (0 "
                    "pending metas, 0 quarantined claims for this run). "
                    "`proposal_id` and `content_sha256` are read off the drop "
                    "the host takes delivery of, so there is nothing here to "
                    "attribute and this control does NOT apply. It is not a "
                    "pass: minting an id from the run and a digest of the "
                    "evidence span would name a proposal that does not exist, "
                    "and the claim-time join would quarantine every one of them",
                    reexecuted=True)
    return None


def score_row(candidates: list[dict[str, Any]], *,
              sha256_re: "re.Pattern[str]",
              placeholder_categories: set[str]) -> dict[str, Any]:
    """Score every candidate row's id, digest and category."""
    problems: list[str] = []
    seen: dict[str, int] = {}
    no_digest = 0
    for r in candidates:
        pid = str(r.get("proposal_id") or r.get("id") or "").strip()
        if not pid:
            problems.append("a candidate row carries no proposal_id — the host "
                            "cannot attribute the drop it names")
            continue
        seen[pid] = seen.get(pid, 0) + 1
        digest = str(r.get("content_sha256") or r.get("sha256") or "").strip().lower()
        if not digest:
            no_digest += 1
        elif not sha256_re.match(digest):
            problems.append(f"{pid}: content_sha256 {digest[:16]!r} is not a "
                            "sha256 — a hand-computed hash proves nothing about "
                            "the staged bytes")
        cat = str(r.get("category") or "").strip().lower()
        if cat in placeholder_categories:
            problems.append(f"{pid}: category {cat!r} is an invented placeholder "
                            "— the value is the owner's real taxonomy id, or the "
                            "key is absent")
    problems.extend(f"{pid}: {n} candidate rows claim the same proposal id"
                    for pid, n in sorted(seen.items()) if n > 1)
    if problems:
        return _row("candidate_stamps", FAIL,
                    f"{len(candidates)} candidate row(s): " + "; ".join(problems[:8])
                    + (" …" if len(problems) > 8 else ""),
                    reexecuted=True)
    if no_digest:
        return _row("candidate_stamps", DEGRADED,
                    f"{no_digest} of {len(candidates)} candidate row(s) carry no "
                    "content digest, so the host cannot join them to the bytes "
                    "that were dropped — those candidates quarantine at claim "
                    "time rather than bind",
                    reexecuted=True)
    return _row("candidate_stamps", PASS,
                f"{len(candidates)} candidate row(s), each with a unique "
                "proposal id, a sha256 content digest and a non-placeholder "
                "category",
                reexecuted=True)
