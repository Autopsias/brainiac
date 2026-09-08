"""The draft-discard lane's own NO-OP — ZERO-04, s08.

A page that refuses BEFORE dispatch tells the truth: `state: unknown`,
`dispatched: false`, nothing left the host, and a corrected later run may retry
it. But a refusal that REPEATS with the SAME outcome is not a retry, it is a
standing refusal, and re-dispatching it every night is how ONE draft item was
re-attempted on six runs across five days (run223, run240, run248, run252,
run257, run258 — every one `draft-item-id-not-in-drafts`, `dispatched: false`)
and stayed `unknown` every time. That item was 6 of the 52 live `unknown`
discard rows; the other 46 were 23 identities that recovered on a retry.

So the lane needs a way to say "we did not do this, and we have stopped
trying", which is what `aborted-not-applied` already means. It is NEVER
`reconciled`: relabelling an unresolvable row as done is exactly the
fabrication the honest `unknown` was refusing.

Its own module because `cos_mutate_discard.py` sits on the 500-LOC bound.
"""
from __future__ import annotations

from typing import Any

#: How many IDENTICAL non-attempts before the lane closes an identity.
#:
#: Three, and the number was measured before it was chosen: the 23 identities
#: that later reconciled were each refused TWICE, with two DIFFERENT outcomes,
#: before succeeding — so a limit of 2 would have closed every one of them
#: while the lane was still making progress. ONE definition; the value is
#: pinned in `tests/test_cos_mutate_discard_closure.py`.
DISCARD_REFUSAL_LIMIT = 3

#: Already in `lane.STATES` and `lane.TERMINAL`. No new state word is minted:
#: "this lane applied nothing" is precisely what the row records.
DISCARD_CLOSED_STATE = "aborted-not-applied"

DISCARD_CLOSED_VERIFICATION = (
    "draft discard closed: the page refused before dispatch with the same "
    "outcome on every attempt")


def standing_refusal(history: list[dict[str, Any]]) -> str | None:
    """The ONE outcome this identity has been refused with, if it is standing.

    Only `dispatched is False` rows count: those are the page's own proof that
    nothing left the host, so counting them counts non-attempts and never
    guesses at a server that may have acted. A response-LOST row
    (`dispatched: None`) may have applied, and no number of those closes
    anything. `None` while the refusals differ — two different refusals are a
    lane making progress, which is what the 23 recovering identities did.
    """
    refusals = [r for r in history
                if r.get("state") == "unknown" and r.get("dispatched") is False]
    outcomes = {str(r.get("connector_result") or "") for r in refusals}
    if len(refusals) >= DISCARD_REFUSAL_LIMIT and len(outcomes) == 1:
        return outcomes.pop()
    return None


def already_closed(history: list[dict[str, Any]]) -> bool:
    """This identity was closed on an earlier night. Recording it again would
    make the ledger count one no-op once per run, forever."""
    return any(r.get("state") == DISCARD_CLOSED_STATE
               and r.get("verification") == DISCARD_CLOSED_VERIFICATION
               for r in history)


def closing_row(base: dict[str, Any], cause: str, *, ts: str) -> dict[str, Any]:
    """ONE row shape for both close paths — the manifest one and the backlog one.

    `base` is any well-formed row for this identity: the lane's own write-ahead
    intent, or the last refusal already in the ledger. Everything OBSERVATIONAL
    is cleared, because a closing row is a decision and not a reading — carrying
    the last refusal's receipts forward would read as a fresh observation of a
    call this run never made. `run` and `ts` are stamped by `append` itself.
    """
    return dict(base, state=DISCARD_CLOSED_STATE, dispatched=False,
                connector_result=cause,
                verification=DISCARD_CLOSED_VERIFICATION,
                receipts=None, observed_after=None,
                changekey_refetched_at=None, action_ts=ts,
                reason=f"standing refusal closed without dispatch: {cause}")


def record(ledger, closing: list[dict[str, Any]], intent, *, ts
           ) -> list[dict[str, str]]:
    """Write the closing rows — a plain append, no browser, nothing dispatched.

    `intent` builds this lane's ordinary write-ahead row for a candidate; the
    closure is that row with the terminal state and the cause on it, so the
    closed field set and the receipts shape are the same ones the ledger
    already enforces.
    """
    out = []
    for entry in closing:
        if entry["record"]:
            ledger.append(closing_row(intent(entry["candidate"]),
                                      entry["cause"], ts=ts))
        out.append({"conversation_id": entry["conversation_id"],
                    "draft_item_id": entry["draft_item_id"],
                    "cause": entry["cause"], "source": "manifest",
                    "closed_this_run": entry["record"]})
    return out


def close_backlog(ledger, prior: dict[str, list[dict[str, Any]]],
                  handled: set[str], *, ts: str) -> list[dict[str, str]]:
    """THE BACKLOG — standing refusals with no candidate in tonight's manifest.

    Measured on the live vault 2026-09-06, and it is the same hole one level
    up: `_partition_candidates` can only close an identity the MANIFEST named,
    and the manifest is scoped to the source run's own drafted threads. The one
    live standing-refusal identity was last in a manifest on run258 — the last
    night its thread was drafted — and absent from every manifest run259
    through run263 could build, so the closure was unreachable for it and the
    lane went on skipping it in silence.

    Same guards, no exceptions: `standing_refusal` still requires
    `DISCARD_REFUSAL_LIMIT` identical `dispatched: false` refusals, a lost
    response still closes nothing, and `already_closed` still suppresses a
    second row. This path opens no browser and dispatches nothing — it appends.
    """
    out = []
    for key, history in sorted(prior.items()):
        if key in handled or already_closed(history):
            continue
        cause = standing_refusal(history)
        if cause is None:
            continue
        base = [r for r in history if r.get("state") == "unknown"
                and r.get("dispatched") is False][-1]
        ledger.append(closing_row(base, cause, ts=ts))
        # The digests are already IN the key — `<conversation>|verb|<item16>`
        # — so this path needs no id and no hashing of its own.
        out.append({"conversation_id": base.get("conversation_id_digest"),
                    "draft_item_id": key.rpartition("|")[2],
                    "cause": cause, "source": "backlog",
                    "closed_this_run": True})
    return out
