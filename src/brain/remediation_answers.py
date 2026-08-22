"""EXC-01 — what an owner's ANSWER does.

Both consumers of the host-private answers ledger live elsewhere — the per-pair
batch in :mod:`brain.remediation_exceptions`, the singleton queue in
:mod:`brain.remediation_routing` — and until s06 both read only its KEYS, so
accepting "raise the lower copy" and accepting the default "defer" retired a
cross-tier pair identically and the ACCEPT action never ran. This module is the
half that reads the VALUE, and it is separate so neither consumer owns it:

* :func:`ttl_days` — the ONE knob this lane is timed by: how long an unanswered
  question holds its queue slot, and how long a DEFERRED one stays out of the
  queue before it is asked again.
* :func:`deferred_holds` — the questions the owner said "not now" to.
* :func:`accept_pair` — the only owner answer here that MAKES A WRITE.
* :func:`unraisable` / :func:`unanswerable_pair` — THE rule for what a tier is
  allowed to be in this lane, in one place because both consumers raise
  toward one and neither may invent its own notion of "the higher side" — and
  :func:`unlabelled_finding`, the surface the rule owes whatever it refuses.

Nothing here touches the proposal store, which is why importing it from either
consumer is safe.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any, Mapping

from . import classification as _classification
from . import remediation_questions as _rq

#: The same knob the COS supersession batch honours — one TTL for every
#: propose-and-ask lane in this engine, so an owner tuning it tunes all of it.
TTL_DAYS_ENV = "BRAIN_COS_PROPOSAL_TTL_DAYS"
DEFAULT_TTL_DAYS = 14

#: A pair member carrying no recognised classification, as ONE banner finding.
#: In the ``exceptions:`` namespace (it is that lane's surface) but declared
#: HERE, beside the rule that refuses the pair — and in the registry beside
#: ``exceptions:unasked``, because an undeclared key renders UNTRIAGED in
#: ``brain alerts``.
UNLABELLED_KEY = "exceptions:unlabelled"


def ttl_days() -> int:
    try:
        return max(1, int(os.environ.get(TTL_DAYS_ENV, "").strip()
                          or DEFAULT_TTL_DAYS))
    except ValueError:
        return DEFAULT_TTL_DAYS


# ---------------------------------------------------------------------------
# THE TIER RULE — one place, because both consumers of this lane raise toward
# a tier and neither may invent its own notion of "the higher side".
# ---------------------------------------------------------------------------
def unraisable(tier: object) -> str:
    """Why ``tier`` may not be raised TOWARD, or ``""`` if it may.

    ``classification.normalize`` maps a missing or unrecognised label to the
    default-deny tier and ``classification.rank`` returns MNPI's rank for
    ``""``. That is RIGHT for reading: an unlabelled note must be withheld
    from a capped reader. It is wrong as a tier to raise toward, because a
    default-denied tier is the ABSENCE of a classification and never an
    assertion of one. On a pair of ``("", "Internal")`` the UNLABELLED note
    ranked highest, so an ACCEPT raised the correctly-labelled Internal note
    to MNPI on the strength of a MISSING label — hiding it from every
    Internal-capped reader, the Cowork VM included — while the real defect, a
    note carrying no classification, reached nobody at all.
    """
    if not _classification.is_default_denied(tier):
        return ""
    shown = str(tier or "").strip() or "a missing label"
    return (f"{shown} is not a classification but the absence of one "
            "(default-denied at the egress gate), so it is never a tier to "
            "raise toward")


def unanswerable_pair(a_tier: object, b_tier: object) -> str:
    """Why these two tiers are NOT an answerable cross-tier exposure, or ``""``.

    Decided on the NORMALIZED tiers, never on the raw strings — which stay
    exactly as recorded, because the question has to name the defect it found
    (:func:`brain.remediation_exceptions._shown`). Two members that resolve to
    ONE tier are not cross-tier at all: ``'internal'`` mis-cased against an
    unlabelled note is the default-deny tier twice, and it was staged as a
    top-rank exposure that outranked every real one in the worst-first
    rotation for a whole TTL while an ACCEPT on it was a silent no-op.
    """
    if _classification.normalize(a_tier) == _classification.normalize(b_tier):
        return "both members resolve to the same tier, so neither exposes the other"
    for tier in (a_tier, b_tier):
        why = unraisable(tier)
        if why:
            return f"a member carries no recognised classification: {why}"
    return ""


def accept_pair(core: Any, meta: Mapping[str, Any]) -> str:
    """Raise the low copy to the pair's high tier. ``""`` on success, else why not.

    The ACCEPT action the disposition table already prescribes, executed
    through the write surface that already exists:
    ``remediation_folds.audited_write`` with ``RAISE_CLASSIFICATION`` — which
    only ever RAISES, refuses a changed body, a supersession key, a symlinked
    path and a write it cannot confirm at the name it signed for. No new write
    verb, and nothing here deletes, moves or lowers.

    The current tier is read from the FILE, never from the index row: the index
    can lag a write, and the only thing the index is asked for is the path."""
    from . import frontmatter as _fm
    from . import remediation_folds as _rf

    refusal = unraisable(meta.get("high_tier"))
    if refusal:
        return f"the pair's higher side is not a tier to raise to: {refusal}"
    tier = _classification.normalize(meta.get("high_tier"))
    try:
        row = core.index.get(str(meta["low_id"])) or {}
    except Exception as exc:  # noqa: BLE001 — an unreadable index refuses, never guesses
        return f"the index could not be read ({exc})"
    rel = str(row.get("path") or "")
    if not rel:
        return f"{meta['low_id']!r} is no longer in the index"
    try:
        text = (Path(core.vault) / rel).read_text(encoding="utf-8")
        now, _body = _fm.parse_text(text)
        if _classification.is_default_denied(now.get("classification")):
            # Same rule from the other side: an unlabelled low copy outranks
            # every real tier, so `rank(...) >= rank(tier)` would call the
            # exposure closed and retire the pair on a MISSING label.
            return (f"{meta['low_id']} carries no classification of its own, "
                    "so it is not a lower copy to raise — label it first")
        if _classification.rank(now.get("classification")) >= _classification.rank(tier):
            return ""   # already at or above it: the exposure is closed
        _rf.audited_write(
            core, rel, _fm.set_keys(text, {"classification": tier}),
            reason=(f"remediation exception {meta['id']}: the owner accepted "
                    f"raising {meta['low_id']} to {tier}"),
            write_class=_rf.RAISE_CLASSIFICATION)
    except Exception as exc:  # noqa: BLE001 — a refused raise keeps the finding
        return f"the audited raise refused it ({exc})"
    return ""


def deferred_holds(
    decisions: Mapping[str, Mapping[str, Any]], today: datetime.date,
) -> set[str]:
    """Question keys the owner DEFERRED whose re-ask cooldown has not elapsed.

    "Defer" means "not now". Re-asking on the next hourly run would make the
    answer a no-op and hold a queue slot the worst-first rotation needs, so a
    deferred question comes back after the same TTL an unanswered one expires
    under — one knob, not a second one. It is never silent in the meantime:
    the pair stays in the unasked-backlog finding and a deferred singleton goes
    back to the banner.

    Matched on the literal answer rather than through ``answer_verdict``
    because this is key-agnostic — ``defer`` is the same string in every shape
    that offers it."""
    out: set[str] = set()
    for key, row in decisions.items():
        if str(row.get("answer") or "").strip() != _rq.DEFER_ANSWER:
            continue
        try:
            when = datetime.date.fromisoformat(str(row.get("answered"))[:10])
        except ValueError:
            continue    # an undated answer holds nothing back
        if today < when + datetime.timedelta(days=ttl_days()):
            out.add(str(key))
    return out


def unlabelled_ids(pairs: Mapping[str, list[Mapping[str, Any]]]) -> list[str]:
    """The note ids in this run's pair population carrying no recognised
    classification. Host-side only — see :func:`unlabelled_finding` for why
    they never reach the finding text."""
    return sorted({
        str(pair[side]) for rows in pairs.values() for pair in rows
        for side, tier_key in (("a", "a_tier"), ("b", "b_tier"))
        if pair.get(side) and unraisable(pair.get(tier_key))})


def unlabelled_finding(
    pairs: Mapping[str, list[Mapping[str, Any]]],
    id_listing: str | None = None,
) -> list[tuple[str, str]]:
    """The notes in this run's pair population carrying NO recognised
    classification, as ONE banner finding.

    Such a pair is not an answerable cross-tier question — the tier rule
    refuses it — but the missing label is a real defect of its own, and until
    this finding existed it reached nobody: the pair was staged as a top-rank
    exposure, an ACCEPT on it raised the correctly-labelled note to MNPI, and
    nothing anywhere said "this note carries no classification". It is the one
    thing the owner can actually fix, and fixing it CONVERGES: once both sides
    carry a canonical tier the pair either returns as a real cross-tier
    question or stops being a pair at all.

    IDS ARE WITHHELD (ruling, EXC-03 s08 review, 2026-08-22) — this now
    matches ``exceptions:unasked``'s posture rather than diverging from it.
    The s06 argument for naming ids ("an unlabelled note ranks MNPI at the
    egress gate, so nothing leaks") does not hold on THIS channel: this
    finding lands in ``notify-sent/current.json``, which ``brain alerts``
    reads on the Cowork VM as a PLAIN FILE READ — the one deliberate
    exception to the classification gate (AGENTS.md §9) — so a note id
    written here reaches the VM regardless of the note's own tier. The count
    and the fact are the remedy signal; the id is vault-content information
    on a channel the egress gate never touches. Same shape as the
    ``tamper:unsigned-note`` action-required line is a DIFFERENT case: that
    line names FILES on the host-private drift-disposition surface, never the
    mount-visible feed."""
    ids = unlabelled_ids(pairs)
    if not ids:
        return []
    # WHERE, never WHICH: the ids themselves stay off this channel, but a
    # finding that names no target is one the owner cannot act on, which is
    # the failure this whole surface exists to prevent. `id_listing` is a
    # HOST-PRIVATE path under the index dir — unreadable from the VM, so the
    # path discloses nothing the count does not.
    where = (f" The ids are listed in {id_listing}." if id_listing else "")
    return [(UNLABELLED_KEY, (
        f"{len(ids)} note(s) in a duplicate pair carry no recognised "
        "classification — the label is missing, or a real tier in the wrong "
        "case. Each is withheld from every capped reader as MNPI, so search "
        "leaks nothing — but the id is not named here either, since this "
        "finding is read as a plain file on the Cowork VM, bypassing the "
        f"egress gate entirely.{where} Give each note a canonical "
        f"classification ({', '.join(_classification.TIERS)}) through "
        "`brain write`."))]
