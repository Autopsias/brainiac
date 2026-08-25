"""REG-01 — the remediation registry: what happens to every health finding.

One table decides, for every alert key the engine can produce, which of four
things happens to it:

* ``auto``   — an owning maintain branch heals it; it only reaches the owner
  when that branch stops working;
* ``owner``  — it becomes ONE decidable question (options + a stated default)
  through the existing capped inbox/proposal-batch path;
* ``log``    — a ``hot.md`` line, never a banner;
* ``banner`` — it is the report that something STOPPED, so nothing may route
  it anywhere that could suppress it.

Nothing is allowed to exist outside the table. A key the table does not know
resolves to ``None`` and renders as :data:`UNTRIAGED` in ``brain alerts`` —
drift is loud, never a silently-dropped finding.

Three rules this module enforces at IMPORT time, because a registry that only
fails on the unlucky lookup is a registry nobody can trust:

1. **The banner class is structurally unsuppressible.** A liveness/dead-man
   key (:data:`UNSUPPRESSIBLE_KEYS`, :data:`UNSUPPRESSIBLE_PREFIXES`) routed to
   ``auto`` or ``log`` raises :class:`RegistryError` — a fold that died cannot
   report its own death, so the one class that must survive automation may not
   be handed to automation.
2. **``auto`` costs something to declare.** It needs an ``owning_branch`` whose
   cadence is declared here, an ``escalate_after`` count, and a
   ``reversibility`` of ``additive``/``raising``/``process``. There is no
   fourth, irreversible kind of automatic repair.
3. **No ambiguous resolution.** Exact keys are unique by construction and win
   over prefixes; two PREFIXES matching one key raises rather than
   first-match-wins, and :func:`validate` rejects a table where two prefixes
   could ever both match.

``escalate_after`` counts NON-CONVERGENCE as well as failure: a branch whose
remaining-target set does not shrink for that many runs escalates even when
every run exits 0. ``shadow_nights`` is how many report-only nights a branch
runs before it is allowed to write; it auto-promotes, it is not an operator
ritual.

The owning-branch CADENCE map lives here (:data:`BRANCH_CADENCE_DAYS`) rather
than in ``maintenance_escalation._CADENCE_DAYS``: the scheduler consumes the
registry, not the other way round. That consumption is real, not a claim —
``maintenance_escalation._cadence_days`` falls back to this map for any branch
its own does not name, so a weekly lane declared here is judged against seven
days and not against the daily default.

The ROW TYPE and the three rules are in ``remediation_schema`` since the
2026-08-24 size split; every name is re-exported here, so this module stays the
one public surface.

This module is PURE DATA plus lookups. No I/O, no index, no state — ``brain
alerts`` imports it at session start and must stay at the engine's import
floor. Its one engine import (``maintenance_retention``) is stdlib-only at
module scope and is what makes the quarantine reasons DERIVED rather than
copied.
"""

from __future__ import annotations

from .maintenance_retention import _QUARANTINE_REMEDY
# The ROW TYPE and the rules that judge one live in `remediation_schema`; this
# module is the TABLE. Re-exported wholesale so `remediation.Remedy`,
# `remediation.AUTO`, `remediation.RegistryError` and friends stay the one
# public surface for every caller and every test.
from .remediation_schema import (  # noqa: F401  (facade re-export)
    AmbiguousKey as AmbiguousKey, AUTO as AUTO, BANNER as BANNER,
    DISPOSITIONS as DISPOSITIONS, is_unsuppressible as is_unsuppressible,
    LOG as LOG, OWNER as OWNER, Remedy as Remedy, RegistryError as RegistryError,
    REVERSIBILITY as REVERSIBILITY, UNSUPPRESSIBLE_KEYS as UNSUPPRESSIBLE_KEYS,
    UNSUPPRESSIBLE_PREFIXES as UNSUPPRESSIBLE_PREFIXES, UNTRIAGED as UNTRIAGED,
    UNTRIAGED_PREFIX as UNTRIAGED_PREFIX,
)
from . import remediation_schema as _schema

# ---------------------------------------------------------------------------
# Owning branches and their cadences (in days). The scheduler reads THIS, so a
# branch cannot be given work without also declaring how often it is expected
# to run — which is what liveness is measured against.
# ---------------------------------------------------------------------------
BRANCH_CADENCE_DAYS = {
    "sign_repair": 1,
    "reguard": 1,
    "extract_retry": 1,
    "update_retry": 1,
    "synthesis_retry": 1,
    # BAK-04's standing linking lane, not a new maintain branch: the daily
    # `corpus_invariants` fold produces the worklist and the weekly synthesis
    # session consumes it, so its cadence is the session's (fix-05/s05).
    "link_lane": 7,
}

#: The quarantine reasons whose fix is a MECHANICAL operator/host action rather
#: than a judgement about the file — the ones `extract_retry` may retry.
#:
#: EXPLICIT allow-list, not "every reason with a remedy" (s01-review finding,
#: 2026-08-21). The first cut read this straight off
#: `maintenance_retention._QUARANTINE_REMEDY` — "has an operator remedy
#: string" was the whole test for "safe to auto-retry" — and that lets a
#: FUTURE maintainer flip a security refusal into a daily auto-retry by doing
#: exactly the helpful thing that file's docstring invites: adding a remedy
#: line for `symlink_rejected` or `zip_bomb_suspected`. A remedy is necessary
#: (validated below) but never SUFFICIENT; only a reason named HERE is
#: mechanical. Every other quarantine reason — including one that gains a
#: remedy tomorrow — stays owner-class via the `quarantine:` prefix.
ALLOWED_MECHANICAL_QUARANTINE_REASONS: tuple[str, ...] = (
    "pdf_no_text_layer",
    "empty_or_low_text_density",
    "pdf_encrypted",
)


def validate_mechanical_allow_list(
    allowed: tuple[str, ...] = ALLOWED_MECHANICAL_QUARANTINE_REASONS,
    remedy_table: dict[str, str] | None = None,
) -> None:
    """Every allow-listed reason must carry a remedy — necessary, never
    sufficient (a remedy alone does NOT confer auto; see the constant's own
    docstring). Callable directly (not just at import) so a test can probe it
    against a fabricated pair without mutating the shipped module state."""
    remedy_table = _QUARANTINE_REMEDY if remedy_table is None else remedy_table
    for reason in allowed:
        if reason not in remedy_table:
            raise RegistryError(
                f"ALLOWED_MECHANICAL_QUARANTINE_REASONS names {reason!r}, which "
                "has no entry in maintenance_retention._QUARANTINE_REMEDY — a "
                "mechanical reason must carry the operator remedy text "
                "extract_retry quotes back to the owner on exhaustion")


validate_mechanical_allow_list()
MECHANICAL_QUARANTINE_REASONS = ALLOWED_MECHANICAL_QUARANTINE_REASONS

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
}

_EXACT: dict[str, Remedy] = {
    **_AUTO_INVARIANTS,
    **_OWNER_INVARIANTS,
    **_SHELF_INVARIANTS,
    # Engine auto-update.
    "update:available": Remedy(AUTO, "update_retry", 3, "process"),
    "update:failed": Remedy(AUTO, "update_retry", 3, "process"),
    "update:applied": Remedy(LOG, note="good news is not an alert"),
    "update:attestation-held": Remedy(
        BANNER,
        note="A-03: the newer version is not attested to this project's own\n"
             "CI publisher, so the UNATTENDED upgrade refused it. No branch can\n"
             "remedy this — installing anyway is the thing being refused — and it\n"
             "is not an owner QUESTION either: the owner answers it by running\n"
             "`brain update` himself, which runs the whole chain end to end"),
    "staging:stale": Remedy(
        AUTO, "update_retry", 3, "process",
        note="a stale Cowork staging is healed by the same `brain update` "
             "run update_retry already invokes"),
    # Weekly synthesis.
    "synthesis:failing": Remedy(
        AUTO, "synthesis_retry", 1, "process",
        note="retried at most once per night — it costs real money"),
    "synthesis:stale": Remedy(AUTO, "synthesis_retry", 1, "process"),
    # Mechanical quarantine: an operator action on THIS host, retryable.
    **{f"quarantine:{r}": Remedy(AUTO, "extract_retry", 3, "process")
       for r in MECHANICAL_QUARANTINE_REASONS},
    # An unsigned note whose bytes NO host record covers (FIX-01). The
    # sign_repair branch refuses it on purpose: a signature is the one thing
    # automation must never grant on trust, and an unsigned file on the shared
    # mount is as likely to be a drop as an interrupted write.
    "tamper:unsigned-note": Remedy(
        OWNER, keeps_blocking=True,
        note="sign_repair signs only bytes the audit chain already covers; "
             "anything else is an owner ruling, never a branch. "
             "keeps_blocking because of what the OPTIONS actually do: two of "
             "the three end the condition by removing it (a file admitted "
             "through `brain write` is signed, a deleted one is gone, and "
             "either way it leaves the target set and there is no finding "
             "left to re-surface). The third is the DEFAULT, it says in so "
             "many words that the finding stays open, and it is the only "
             "branch where the condition survives the answer — the file is "
             "still unsigned, still counted by invariant:unsigned_notes, and "
             "still skipped by sign_repair every hour, so the branch can "
             "never converge. Dropping the finding there would be the vault "
             "quietly ceasing to report a file it will never repair"),
    # A write that did not land where it was signed for (FIX-01/FIX-02). The
    # repair batch STOPS on it: an ordinary refusal is per-target, this one
    # says the vault is being written concurrently outside the audited path.
    "tamper:redirected-write": Remedy(
        OWNER, keeps_blocking=True,
        note="the repair batch stopped; find the concurrent writer before the "
             "lane is re-enabled. keeps_blocking because the redirect RECURS: "
             "the same path stalls the batch every hour until the cause is "
             "fixed, so the answer settles the question and not the condition"),
    # DLV-03 — the deliverables shelf path resolver. All five are fail-closed
    # refusals (never a write) that mean the shelf is simply not working
    # until a config change clears them — not an owner-answerable QUESTION
    # with options (there is exactly one fix, named in the refusal text
    # itself), so BANNER: the same "something stopped" class as
    # maintain:stale/blocked, structurally unsuppressible rather than routed
    # through the owner-question apparatus.
    "deliverables:home_dir_parent": Remedy(
        BANNER,
        note="the resolved shelf parent is the user's home dir; set "
             "$BRAIN_DELIVERABLES_DIR to a non-home path"),
    "deliverables:git_root_parent": Remedy(
        BANNER,
        note="the resolved shelf parent is a git working tree root with no "
             "explicit $BRAIN_DELIVERABLES_DIR — an untracked copy of note "
             "content there is one `git add -A` from a confidentiality leak"),
    "deliverables:shadow_conflict": Remedy(
        BANNER,
        note="the resolved target equals, lies inside, or contains an "
             "existing non-empty directory the shelf binding registry does "
             "not name; the refusal text carries the one recovery move"),
    "deliverables:no_vault_id": Remedy(
        BANNER,
        note="no stable vault id could be established (read-only vault?); "
             "the shelf must never resolve to a directory named 'None'"),
    "deliverables:bound_elsewhere": Remedy(
        BANNER,
        note="the resolved target is already bound to a different vault in "
             "the host-private registry; two vaults must never interleave "
             "writes into one shelf"),
    # DLV-10 — the shelf's own guards. Three are the resolver-refusal class:
    # the fold declined to act, and the one fix is named in the finding itself.
    "shelf:refused": Remedy(
        BANNER, note="the census read zero deliverables while the ledger "
        "still claimed files; a failed read looks exactly like a vault that "
        "emptied, so the run moved nothing"),
    "shelf:move-cap": Remedy(
        BANNER, note="one run planned more moves than $BRAIN_SHELF_MAX_MOVES "
        "allows and moved nothing; a run that large usually means the vault "
        "was read wrong"),
    "shelf:diverged": Remedy(
        BANNER, note="a shelf file holds bytes the fold did not write, so it "
        "is neither overwritten nor moved aside — and stops being updated"),
    # OWNER: keep the last surviving copy of a deleted note's payload, or
    # remove it. `keeps_blocking` False — once ruled on, that file is settled.
    "shelf:sole-copy": Remedy(
        OWNER, note="a retired copy is past the _previous/ window and no "
        "byte-identical content exists in the vault or on the live shelf, so "
        "it may be the last copy"),
    # Log-only.
    "engine-feedback": Remedy(LOG, note="a backlog count, never a banner"),
    # SPD-01: a remediation cost regression (the FIRST night any model-backed
    # branch spends anything, an absolute floor crossed, or a 5x
    # week-over-week jump). NOT a cap — there is none, by owner ruling — this
    # is purely "tell the owner what the automation is spending", so it is an
    # EXACT entry rather than falling under the generic `trend:` prefix
    # (BANNER, no branch owns a regression, no owner can answer it): a cost
    # line is answerable ("do nothing, it's expected" / "investigate"), which
    # every other `trend:` key is not.
    "trend:remediation_cost": Remedy(
        OWNER, note="no cap is enforced on remediation spend; this is a "
                    "trend/floor alert only, never a throttle"),
    # The banner class.
    "blocked": Remedy(BANNER),
    "invariants-liveness": Remedy(BANNER, note="WAT-01 dead-man's switch"),
    "synthesis-watchdog": Remedy(BANNER),
    "maintain:stale": Remedy(BANNER),
    "maintain:no-feed": Remedy(BANNER),
    "maintain:unparseable-feed": Remedy(
        BANNER, note="a forged/broken feed fails closed to stale"),
    "exceptions": Remedy(
        BANNER, note="EXC-03: the unified 'N exception(s) — open <page>' "
                     "banner, read the SAME way on host and VM from the "
                     "signed exceptions.json summary"),
    "exceptions:unreachable": Remedy(
        BANNER, note="EXC-03: the VM could not VERIFY the signed summary "
                     "(signature/vault_id/schema/freshness/page-hash) — "
                     "unreachable, never a fabricated zero"),
    "exceptions:no-summary": Remedy(
        BANNER, note="EXC-03: this vault ran `maintain` before the "
                     "exceptions page existed; unreported until the next run"),
    "exceptions:stale": Remedy(
        BANNER, note="EXC-03: the exceptions summary predates the same "
                     "staleness window `maintain:stale` uses"),
    "exceptions:unasked": Remedy(
        BANNER, note="EXC-01: cross-tier exceptions staged but not in the "
                     "owner queue. The cap is a PUSH RATE, so the store keeps "
                     "every pair — but nothing renders that store until the "
                     "exceptions page exists, and a staged pair reached no "
                     "surface at all. One aggregate line, not one per pair"),
    "exceptions:unlabelled": Remedy(
        BANNER, note="EXC-01: a note in a duplicate pair carries no "
                     "recognised classification. NOT owner-class — there is "
                     "nothing to decide: a default-denied tier is the absence "
                     "of a label, never a tier to raise toward, so the pair "
                     "is not an answerable cross-tier question. ONE aggregate "
                     "line carrying the COUNT and the host-private path the "
                     "ids are listed at — never the ids themselves, since "
                     "this feed is read as a plain file on the VM; labelling "
                     "them converges it"),
    "degradation": Remedy(BANNER, note="the roll-up of the current feed"),
    "degradation:unrecognised-key": Remedy(
        BANNER, note="a key outside the recognised alphabet was withheld"),
    UNTRIAGED: Remedy(BANNER, note="a key this table does not know"),
}

#: Prefix patterns, checked only after an exact miss. No prefix here may be a
#: prefix of another (``validate`` enforces it), so at most one can ever match.
_PREFIXES: tuple[tuple[str, Remedy], ...] = (
    ("branch-escalate:", Remedy(
        BANNER, note="a branch that stopped making progress IS the finding")),
    ("quarantine:", Remedy(
        OWNER, note="a genuinely unreadable drop: one question, options + "
                    "default; the mechanical reasons are exact entries above")),
    ("quarantine-exhausted:", Remedy(
        OWNER, keeps_blocking=True,
        note="extract_retry retried this file to its bound and it still "
             "fails — an owner decision, named with the file's own remedy "
             "text, carved OUT of extract_retry's own target set so one "
             "unreadable file can never escalate the whole branch (s01-review "
             "finding: quarantine:pdf_encrypted could not converge)")),
    ("trend:", Remedy(
        BANNER, note="a latency/quality regression no branch can heal and no "
                     "owner can answer as a question — it stays visible")),
)


def validate(
    exact: dict[str, Remedy] | None = None,
    prefixes: tuple[tuple[str, Remedy], ...] | None = None,
) -> None:
    """Raise :class:`RegistryError` if the table breaks any structural rule.

    The rules themselves are ``remediation_schema.validate``; this wrapper
    supplies THIS module's table and cadence map as the defaults, so a caller
    (and every existing test) still says ``remediation.validate()``."""
    _schema.validate(_EXACT if exact is None else exact,
                     _PREFIXES if prefixes is None else prefixes,
                     BRANCH_CADENCE_DAYS)


def resolve(
    key: str,
    exact: dict[str, Remedy] | None = None,
    prefixes: tuple[tuple[str, Remedy], ...] | None = None,
) -> Remedy | None:
    """The declared remedy for ``key``, or ``None`` when nothing declares it.
    Defaults to the shipped table; see ``remediation_schema.resolve``."""
    return _schema.resolve(key,
                           _EXACT if exact is None else exact,
                           _PREFIXES if prefixes is None else prefixes)


def declared_keys() -> tuple[str, ...]:
    """Every exact key the table declares, sorted. Prefixes are not keys."""
    return tuple(sorted(_EXACT))


def declared_prefixes() -> tuple[str, ...]:
    return tuple(p for p, _ in _PREFIXES)


validate()
