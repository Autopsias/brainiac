"""The ``invariant:*`` rows of the remediation table (REG-01).

Split out of :mod:`brain.remediation` on 2026-10-28 when that module reached
the 500-line bound; ``remediation._EXACT`` still merges all three groups, so
the table, its validation and every lookup are unchanged.
"""

from __future__ import annotations

from .remediation_schema import AUTO, BANNER, OWNER, Remedy

_AUTO_INVARIANTS = {
    "invariant:unsigned_notes": Remedy(
        AUTO, "sign_repair", 3, "additive", shadow_nights=3,
        note="signs ONLY notes with host-side provenance of an interrupted "
             "host write; an unsigned note without it is a TAMPER exception"),
    "invariant:unguarded_ingests": Remedy(
        AUTO, "reguard", 3, "raising", shadow_nights=3,
        note="ENF-04 re-run; only ever raises, undecided fails closed high"),
    "invariant:subfloor_families": Remedy(
        AUTO, "extract_retry", 3, "additive",
        note="heals only when re-extraction clears the ENF-01 floor, never by "
             "merging stubs"),
    "invariant:unlinked_sources": Remedy(
        AUTO, "link_lane", 2, "additive",
        note="fix-05: keyed to BAK-04 lane health, not the raw count; "
             "escalate_after is 2 MISSED WEEKS at the link_lane cadence"),
}

_OWNER_INVARIANTS = {
    "invariant:cross_tier_duplicates": Remedy(
        OWNER, note="CUR-01-style proposal batch; re-tiering cascades"),
    "invariant:cross_tier_candidates": Remedy(
        OWNER, note="the UNDECIDED band — a guess here is the failure mode"),
    "invariant:cross_tier_twins": Remedy(
        OWNER, note="proposal batch, same store as the other two"),
    "invariant:unreachable_gold": Remedy(
        OWNER, note="usually a retirement mistake; reinstating is an owner act"),
}

#: DLV-05's three shelf counters. BANNER, and deliberately neither of the two
#: dispositions its neighbours in this block carry.
#:
#: NOT ``auto``: an auto row is a promise about the REMEDIATION apparatus — an
#: owning branch that writes a `_remediation.branches.<name>` row every night,
#: which `branch_liveness`, `coverage` and `thrashing_targets` then read to
#: decide whether to suppress the finding. The nightly fold that actually
#: converges these three (`deliverables_shelf_fold`, and the drop lane's
#: recovery pass at the top of every drain) is an ordinary maintain fold, not a
#: remediation branch, and writes no such row — so declaring `auto` here would
#: claim an escalation apparatus that does not exist and park a permanently
#: empty branch on the exceptions page.
#:
#: NOT ``owner``: an owner row must be ONE decidable question with options and
#: a stated default. A count that stayed above its floor is not that — and each
#: CAUSE already has its own named, answerable surface (`shelf:move-cap`,
#: `shelf:refused`, `shelf:diverged`, `shelf:sole-copy`, the five
#: `deliverables:*` resolver refusals). Asking a second time here would be the
#: same finding twice.
#:
#: So: the same "something stopped" class as those nine keys, and structurally
#: visible for as long as it is true.
_SHELF_INVARIANTS = {
    "invariant:unshelved_deliverables": Remedy(
        BANNER,
        note="marked deliverables the shelf ledger claims no copy of. The "
             "nightly shelf fold copies every censused deliverable, so a count "
             "above the floor means that fold declined or could not finish — "
             "and the reason it declined carries its own key"),
    "invariant:stale_shelf_entries": Remedy(
        BANNER,
        note="shelf ledger rows whose note has left the census. Nothing is "
             "lost (the shelf never deletes; displaced copies go to "
             "_previous/), but the shelf has stopped telling the truth about "
             "what the vault holds"),
    "invariant:unanchored_deliverable_payloads": Remedy(
        BANNER,
        note="drop-lane payloads with no anchor note. The lane's own recovery "
             "pass retries the anchor at the top of every drain, so a count "
             "that stays up means that write keeps failing — the one metric "
             "that can see the shelf mechanism having stopped entirely, since "
             "the other two read 0 of 0 when nothing is being marked at all"),
    "invariant:supersede_declared_failed": Remedy(
        BANNER,
        note="live notes whose `replaces:` names a note that is NOT retired "
             "under them (SUP-01) — the old version is still live in search. "
             "`brain health-report` samples the pairs; `brain supersede <old> "
             "<new>` on the host clears each"),
    "supersede_declared_failed": Remedy(
        BANNER,
        note="this run's owner-declared supersession(s) that could not be "
             "applied; the corpus count above keeps reporting them"),
}
