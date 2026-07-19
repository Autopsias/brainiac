"""Priority-chip projection (chief-of-staff kernel v4.7) — tested reference
implementation for the executable halves of the P-taxonomy contract.

The nightly chief-of-staff run is executed by a model reading SKILL.md; the
behaviours here are the parts that must never drift on a prompt rewrite, so
they live as engine code with contract tests (``tests/test_cos_chips.py``,
fake mailbox adapter + fault injection):

- ``assign_chip``       — the authoritative P0/P1/P2 assignment rules
- ``desired_categories``— category-set PRESERVATION diff (never a bare-set write)
- ``verify_write``      — whole-set server-state verification
- ``apply_chip_to_conversation`` — message-level apply + journal, per-row verified
- ``recover_from_journal``       — journal recovery after a partial/failed pass
- ``lease_state``       — the cos-ops/_mutation_lease.json semantics
- ``desired_chip_and_trigger`` — v4.7 LIF-01/02 nightly reconciliation (auto-clear +
  re-level), full-evidence desired-state diff, never rule-ordering
- ``apply_relevel_to_conversation`` — v4.7 add-before-remove re-level apply
- ``dedupe_automated_p2`` — v4.7 recurring-automated-sender P2 collapse (s02 finding)

Chips are a PROJECTION of the act queue — never a verdict. Classification
(Phase 1.5 buckets/tiers) is untouched by this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

CHIP_P0 = "P0 · Now"          # red
CHIP_P1 = "P1 · Today"        # orange
CHIP_P2 = "P2 · This week"    # blue
CHIPS = (CHIP_P0, CHIP_P1, CHIP_P2)
LEGACY_ACTION = "Action"
#: every category this projection owns and may add/remove; anything else on a
#: message belongs to the owner and must survive every write untouched.
MANAGED_CATEGORIES = frozenset(CHIPS) | {LEGACY_ACTION}

CHIP_COLORS = {CHIP_P0: "red", CHIP_P1: "orange", CHIP_P2: "blue"}


# --------------------------------------------------------------------------
# Assignment (PRJ-01 hardened rules — roster tier alone never makes P0)
# --------------------------------------------------------------------------

def assign_chip(
    *,
    bucket: str,
    roster_high: bool = False,
    direct_ask: bool = False,
    deadline_hours: float | None = None,
    blocking_others: bool = False,
) -> str | None:
    """Return the ONE chip for a conversation, or ``None`` for non-``act``.

    P0 · Now       = (roster-high AND (direct ask OR any stated deadline))
                     OR hard deadline < 48h OR blocking others.
    P1 · Today     = a direct ask on the owner.
    P2 · This week = every other act row.
    Roster tier alone NEVER makes P0.
    """
    if bucket != "act":
        return None
    has_deadline = deadline_hours is not None
    if (
        (roster_high and (direct_ask or has_deadline))
        or (has_deadline and deadline_hours < 48)
        or blocking_others
    ):
        return CHIP_P0
    if direct_ask:
        return CHIP_P1
    return CHIP_P2


# --------------------------------------------------------------------------
# Desired-state category diff (codex-r2 i — preservation, never a bare set)
# --------------------------------------------------------------------------

def desired_categories(existing: Iterable[str], chip: str | None) -> list[str]:
    """``existing − MANAGED + {chip}`` with the owner's categories preserved
    in their original order; ``chip=None`` strips every managed category."""
    if chip is not None and chip not in CHIPS:
        raise ValueError(f"not a managed P-chip: {chip!r}")
    kept = [c for c in existing if c not in MANAGED_CATEGORIES]
    return kept + ([chip] if chip else [])


def verify_write(
    pre_categories: Iterable[str], post_categories: Iterable[str], chip: str | None
) -> bool:
    """A chip write verifies ONLY when the entire post-write set is right:
    the chip present (or absent, on removal) AND the non-managed subset
    unchanged. Order of the owner's categories is not significant."""
    pre_kept = sorted(c for c in pre_categories if c not in MANAGED_CATEGORIES)
    post = list(post_categories)
    post_kept = sorted(c for c in post if c not in MANAGED_CATEGORIES)
    if pre_kept != post_kept:
        return False
    managed_present = [c for c in post if c in MANAGED_CATEGORIES]
    return managed_present == ([chip] if chip else [])


# --------------------------------------------------------------------------
# Message-level apply under the conversation abstraction + journal recovery
# --------------------------------------------------------------------------

class MailboxAdapter(Protocol):
    """The minimal mutation surface (fake in tests, Chrome/REST in life)."""

    def inbox_message_ids(self, conversation_id: str) -> list[str]: ...
    def get_categories(self, message_id: str) -> list[str]: ...
    def set_categories(self, message_id: str, categories: list[str]) -> None: ...


def apply_chip_to_conversation(
    mailbox: MailboxAdapter, conversation_id: str, chip: str | None
) -> list[dict[str, Any]]:
    """Apply (or, with ``chip=None``, remove) the conversation's chip on EVERY
    message currently in Inbox. Per message: compute the preserved desired
    set, write it, RE-READ SERVER STATE, and journal the row with its
    verification result. A failure on one message never aborts the rest.

    Journal rows: ``{conversation_id, message_id, chip, verification}`` with
    verification ∈ ``response-confirmed`` (server re-read verified) ·
    ``verified-failed`` (write landed wrong / silently dropped) ·
    ``error:<msg>`` (the mutation call raised).
    """
    journal: list[dict[str, Any]] = []
    for mid in mailbox.inbox_message_ids(conversation_id):
        row = {"conversation_id": conversation_id, "message_id": mid, "chip": chip}
        try:
            pre = list(mailbox.get_categories(mid))
            mailbox.set_categories(mid, desired_categories(pre, chip))
            post = list(mailbox.get_categories(mid))  # server re-read, never trust the write
            row["verification"] = (
                "response-confirmed" if verify_write(pre, post, chip) else "verified-failed"
            )
        except Exception as exc:  # fault-injection boundary: journal, continue
            row["verification"] = f"error:{exc}"
        journal.append(row)
    return journal


def rows_needing_recovery(journal: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Journal rows whose write is NOT server-confirmed — the re-apply set."""
    return [r for r in journal if r.get("verification") != "response-confirmed"]


def recover_from_journal(
    mailbox: MailboxAdapter, journal: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Re-drive every unconfirmed journal row, idempotently: a row whose
    server state already matches the desired set verifies as
    ``already-applied`` without a write; otherwise re-apply + re-verify.
    Returns the recovery journal (same row shape)."""
    out: list[dict[str, Any]] = []
    for r in rows_needing_recovery(journal):
        mid, chip = r["message_id"], r["chip"]
        row = {**r}
        try:
            current = list(mailbox.get_categories(mid))
            if verify_write(current, current, chip):
                row["verification"] = "already-applied"
            else:
                mailbox.set_categories(mid, desired_categories(current, chip))
                post = list(mailbox.get_categories(mid))
                row["verification"] = (
                    "response-confirmed" if verify_write(current, post, chip)
                    else "verified-failed"
                )
        except Exception as exc:
            row["verification"] = f"error:{exc}"
        out.append(row)
    return out


# --------------------------------------------------------------------------
# Lifecycle v4.7 (LIF-01/02/03) — desired-state reconciliation, never
# rule-ordering: every conversation's desired chip is recomputed from the
# FULL current evidence each night, so a reply followed by NEW actionable
# inbound still chips (a stale rule-order clear-on-reply would silently drop
# it under the "no chip = don't look" runbook rule — codex-r2 (i)).
# --------------------------------------------------------------------------

#: the ONLY trigger that clears a chip outright — every other signal below
#: (thread-closed / meeting-passed / handled-by-others) may only de-escalate.
CLEAR_TRIGGER = "owner_reply_is_latest_no_open_items"
DEESCALATE_TRIGGERS = frozenset({"thread_closed", "meeting_passed", "handled_by_others"})
_DEESCALATE_STEP = {CHIP_P0: CHIP_P1, CHIP_P1: CHIP_P2, CHIP_P2: CHIP_P2}


def desired_chip_and_trigger(
    *,
    bucket: str,
    roster_high: bool = False,
    direct_ask: bool = False,
    deadline_hours: float | None = None,
    blocking_others: bool = False,
    owner_reply_is_latest: bool = False,
    has_unsent_draft: bool = False,
    has_flag: bool = False,
    has_open_commitment: bool = False,
    has_pending_deadline: bool = False,
    thread_closed: bool = False,
    meeting_passed: bool = False,
    handled_by_others: bool = False,
) -> tuple[str | None, str]:
    """Nightly desired-state reconciliation for ONE conversation, over the
    FULL current evidence (LIF-01/02, hardened (i)/(ii)).

    Clear (chip=None) fires ONLY on the closed enum: owner's reply is the
    LATEST message (no later inbound), AND no unsent draft, no flag, no open
    spine commitment, no pending deadline — every clear ledgers this trigger
    verbatim. 'thread_closed' / 'meeting_passed' / 'handled_by_others' NEVER
    clear alone; at most they de-escalate one level (P0->P1->P2, floor P2).
    """
    if (
        owner_reply_is_latest
        and not has_unsent_draft
        and not has_flag
        and not has_open_commitment
        and not has_pending_deadline
    ):
        return None, CLEAR_TRIGGER

    chip = assign_chip(
        bucket=bucket, roster_high=roster_high, direct_ask=direct_ask,
        deadline_hours=deadline_hours, blocking_others=blocking_others,
    )
    if chip is None:
        return None, "not-act"

    if handled_by_others or thread_closed or meeting_passed:
        trigger = ("handled_by_others" if handled_by_others
                   else "thread_closed" if thread_closed else "meeting_passed")
        return _DEESCALATE_STEP[chip], trigger

    return chip, "assignment"


def reconcile_action(existing_chip: str | None, desired: str | None) -> str:
    """Diff existing vs. desired -> the ledger action bucket."""
    if existing_chip == desired:
        return "none"
    if existing_chip is None:
        return "added"
    if desired is None:
        return "cleared"
    return "re-leveled"


def ledger_entry(
    conversation_id: str, *, existing_chip: str | None, desired: str | None, trigger: str,
) -> dict[str, Any]:
    """One CHIP LEDGER row (LIF-03) — added/re-leveled/cleared, with reason."""
    return {
        "conversation_id": conversation_id,
        "action": reconcile_action(existing_chip, desired),
        "from": existing_chip,
        "to": desired,
        "trigger": trigger,
    }


def apply_relevel_to_conversation(
    mailbox: MailboxAdapter, conversation_id: str, old_chip: str, new_chip: str,
) -> list[dict[str, Any]]:
    """Re-level a conversation ADD-BEFORE-REMOVE (hardened (b)): every
    message first gets BOTH chips written (transient two-chip state is
    acceptable), then the old chip is stripped in a second pass. A zero-chip
    gap never occurs even under a mid-pass failure — the next nightly
    reconciliation heals any partial state, since desired-state diff is
    idempotent (add step re-detects "already has new_chip", remove step
    re-detects "already single-chip")."""
    journal: list[dict[str, Any]] = []
    mids = mailbox.inbox_message_ids(conversation_id)
    for mid in mids:
        row = {"conversation_id": conversation_id, "message_id": mid,
               "chip": new_chip, "step": "add-new"}
        try:
            pre = list(mailbox.get_categories(mid))
            kept = [c for c in pre if c not in MANAGED_CATEGORIES]
            both = kept + [c for c in (old_chip, new_chip) if c not in kept]
            mailbox.set_categories(mid, both)
            post = list(mailbox.get_categories(mid))
            row["verification"] = (
                "response-confirmed" if new_chip in post and old_chip in post
                else "verified-failed"
            )
        except Exception as exc:
            row["verification"] = f"error:{exc}"
        journal.append(row)
    for mid in mids:
        row = {"conversation_id": conversation_id, "message_id": mid,
               "chip": new_chip, "step": "remove-old"}
        try:
            pre = list(mailbox.get_categories(mid))
            mailbox.set_categories(mid, desired_categories(pre, new_chip))
            post = list(mailbox.get_categories(mid))
            row["verification"] = (
                "response-confirmed" if verify_write(pre, post, new_chip) else "verified-failed"
            )
        except Exception as exc:
            row["verification"] = f"error:{exc}"
        journal.append(row)
    return journal


# --------------------------------------------------------------------------
# Automated-sender P2 dedupe (s02 finding, folded into v4.7 assignment)
# --------------------------------------------------------------------------

def dedupe_automated_p2(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse recurring-automated-sender act rows to ONE P2 chip per
    sender per cycle. ``rows``: dicts carrying ``chip`` and ``automated_sender``
    (the overlay-driven flag naming a known recurring bulk sender, e.g. an
    automated notification system) and ``sender``. Only P2 rows from a
    flagged automated sender are collapsed — P0/P1 rows (a direct ask or
    deadline from that sender) are never suppressed."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("chip") == CHIP_P2 and r.get("automated_sender"):
            key = r["sender"]
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


# --------------------------------------------------------------------------
# Mutation lease (codex-r3 — one mutator at a time)
# --------------------------------------------------------------------------

def lease_state(
    lease: dict[str, Any] | None, *, now: datetime, run_id: str
) -> tuple[str, str | None]:
    """Judge ``cos-ops/_mutation_lease.json`` content for a run.

    Returns ``(state, holder)`` — state ∈:
    ``clear``   no lease / our own lease ⇒ mutations allowed;
    ``held``    unexpired foreign lease (or malformed — unreadable intent
                fails closed) ⇒ ZERO mailbox mutations this pass;
    ``expired`` stale foreign lease ⇒ ignored but REPORTED in the banner.
    """
    if lease is None:
        return "clear", None
    holder = lease.get("owner") or lease.get("run_id") or "unknown"
    try:
        if not isinstance(lease, dict) or not lease.get("run_id"):
            return "held", str(holder)
        if lease["run_id"] == run_id:
            return "clear", str(holder)
        exp = datetime.fromisoformat(str(lease["ttl_expires"]))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        return "held", str(holder)  # malformed ⇒ fail closed, reported
    if exp <= now:
        return "expired", str(holder)
    return "held", str(holder)
