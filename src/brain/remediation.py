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

This module is PURE DATA plus lookups. No I/O, no index, no state — ``brain
alerts`` imports it at session start and must stay at the engine's import
floor. Its one engine import (``maintenance_retention``) is stdlib-only at
module scope and is what makes the quarantine reasons DERIVED rather than
copied.
"""

from __future__ import annotations

from dataclasses import dataclass

from .maintenance_retention import _QUARANTINE_REMEDY

AUTO = "auto"
OWNER = "owner"
LOG = "log"
BANNER = "banner"
DISPOSITIONS = (AUTO, OWNER, LOG, BANNER)

#: An ``auto`` remedy must be one of these kinds of reversible. There is no
#: ``none``: an automatic repair that cannot be undone through the audited
#: path is an owner decision, not a branch.
#:  * ``additive``  — it only ADDS (a signature, a link, an extracted body);
#:  * ``raising``   — it only ever raises a classification (ENF-04's rule);
#:  * ``process``   — it re-runs an existing process (an update, a synthesis
#:    run, an extraction) and writes nothing the process would not have.
REVERSIBILITY = ("additive", "raising", "process")

#: What ``brain alerts`` renders for a key the table does not know.
UNTRIAGED = "UNTRIAGED"

#: How the ROUTING step (REG-03, ``remediation_routing``) stamps an undeclared
#: key on its way to the banner, so the findings FEED itself records that
#: nothing had decided what to do with it — not only the surface that renders
#: it. Deliberately NOT a table entry: a row here would make ``resolve``
#: answer for it, and the whole point of the wrapper is that nothing does.
#: Lowercase because ``alerts._FINDING_KEY_RE`` is the one thing standing
#: between a forged feed and attacker-authored text at session start, and
#: widening that alphabet to carry a prefix would be a poor trade.
UNTRIAGED_PREFIX = "untriaged:"


class RegistryError(ValueError):
    """The table itself is wrong — raised at import/validation time."""


class AmbiguousKey(RegistryError):
    """One key matched more than one pattern. Never first-match-wins."""


@dataclass(frozen=True)
class Remedy:
    """One row of the table."""

    disposition: str
    owning_branch: str | None = None
    escalate_after: int | None = None
    reversibility: str | None = None
    shadow_nights: int = 0
    #: OWNER-only. Whether an answer settles the QUESTION but not the
    #: CONDITION. Default False: for almost every owner question "decided" is
    #: a finished terminal state — the owner has ruled on that file and the
    #: ruling stands, so re-surfacing it would be nagging. Set True only where
    #: the condition RECURS and keeps blocking work after the ruling; such a
    #: finding is never re-asked, but it keeps its banner/alerts surface,
    #: because a lane that has quietly stopped repairing must not look
    #: identical to one with nothing to do.
    keeps_blocking: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# The banner class — declared SEPARATELY from the table so a table entry can be
# checked against it. These are the keys whose entire content is "something
# stopped": the ones automation is structurally unable to report about itself.
# ---------------------------------------------------------------------------
UNSUPPRESSIBLE_KEYS = frozenset({
    "blocked",
    "invariants-liveness",
    "synthesis-watchdog",
    "maintain:stale",
    "maintain:no-feed",
    "maintain:unparseable-feed",
    "degradation:unrecognised-key",
    "exceptions",
    "exceptions:unreachable",
    "exceptions:no-summary",
    "exceptions:stale",
    UNTRIAGED,
})
UNSUPPRESSIBLE_PREFIXES = ("branch-escalate:", UNTRIAGED_PREFIX)


def is_unsuppressible(key: str) -> bool:
    """Whether ``key`` belongs to the class that may only ever be ``banner``."""
    return key in UNSUPPRESSIBLE_KEYS or any(
        key.startswith(p) for p in UNSUPPRESSIBLE_PREFIXES)


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

_EXACT: dict[str, Remedy] = {
    **_AUTO_INVARIANTS,
    **_OWNER_INVARIANTS,
    # Engine auto-update.
    "update:available": Remedy(AUTO, "update_retry", 3, "process"),
    "update:failed": Remedy(AUTO, "update_retry", 3, "process"),
    "update:applied": Remedy(LOG, note="good news is not an alert"),
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

    Called at import on the shipped table, and by tests on hand-built ones —
    which is why the bad-table failure is a VALIDATION failure, not a lookup
    that happens to go wrong later."""
    exact = _EXACT if exact is None else exact
    prefixes = _PREFIXES if prefixes is None else prefixes

    for pattern, remedy in [*exact.items(), *prefixes]:
        _validate_row(pattern, remedy)

    for i, (a, _) in enumerate(prefixes):
        for b, _ in prefixes[i + 1:]:
            if a.startswith(b) or b.startswith(a):
                raise RegistryError(
                    f"prefixes {a!r} and {b!r} overlap — a key could match "
                    "both, and the registry never resolves ambiguity by order")


def _validate_row(pattern: str, remedy: Remedy) -> None:
    if remedy.disposition not in DISPOSITIONS:
        raise RegistryError(
            f"{pattern!r}: unknown disposition {remedy.disposition!r}")
    if is_unsuppressible(pattern) and remedy.disposition != BANNER:
        raise RegistryError(
            f"{pattern!r} is a liveness/dead-man key and may only be "
            f"'{BANNER}', not '{remedy.disposition}' — a fold that died cannot "
            "report its own death, so it must never be routed to something "
            "that could suppress it")
    if remedy.disposition == AUTO:
        if not remedy.owning_branch:
            raise RegistryError(f"{pattern!r}: auto needs an owning_branch")
        if remedy.owning_branch not in BRANCH_CADENCE_DAYS:
            raise RegistryError(
                f"{pattern!r}: branch {remedy.owning_branch!r} has no declared "
                "cadence in BRANCH_CADENCE_DAYS")
        if not isinstance(remedy.escalate_after, int) or remedy.escalate_after < 1:
            raise RegistryError(f"{pattern!r}: auto needs escalate_after >= 1")
        if remedy.reversibility not in REVERSIBILITY:
            raise RegistryError(
                f"{pattern!r}: auto needs reversibility in {REVERSIBILITY} — "
                "an irreversible repair is an owner decision, not a branch")
    else:
        if (remedy.owning_branch or remedy.reversibility or remedy.shadow_nights
                or remedy.escalate_after is not None):
            raise RegistryError(
                f"{pattern!r}: only auto carries owning_branch/escalate_after/"
                "reversibility/shadow_nights")
    if remedy.keeps_blocking and remedy.disposition != OWNER:
        raise RegistryError(
            f"{pattern!r}: keeps_blocking is about what happens AFTER an owner "
            f"answers, so it only means anything on '{OWNER}' — a "
            f"'{remedy.disposition}' key is never asked in the first place")


def resolve(
    key: str,
    exact: dict[str, Remedy] | None = None,
    prefixes: tuple[tuple[str, Remedy], ...] | None = None,
) -> Remedy | None:
    """The declared remedy for ``key``, or ``None`` when nothing declares it.

    Exact declarations are strictly more specific and win outright. Otherwise
    the prefix patterns are scanned and a key matching TWO of them raises
    :class:`AmbiguousKey` — first-match-wins would silently pick a disposition
    nobody chose."""
    exact = _EXACT if exact is None else exact
    prefixes = _PREFIXES if prefixes is None else prefixes
    if key in exact:
        return exact[key]
    matched = [(p, r) for p, r in prefixes if key.startswith(p)]
    if len(matched) > 1:
        raise AmbiguousKey(
            f"{key!r} matches {len(matched)} patterns "
            f"({', '.join(sorted(p for p, _ in matched))}) — the registry "
            "refuses to resolve by order")
    return matched[0][1] if matched else None


def declared_keys() -> tuple[str, ...]:
    """Every exact key the table declares, sorted. Prefixes are not keys."""
    return tuple(sorted(_EXACT))


def declared_prefixes() -> tuple[str, ...]:
    return tuple(p for p, _ in _PREFIXES)


validate()
