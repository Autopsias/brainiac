"""REG-01 — the SCHEMA half of the remediation registry: what a row may say,
and the rules that judge one.

Split out of ``remediation.py`` at the 500-line file ratchet (DLV-05), along
the seam the module docstring already drew: this file holds the ROW TYPE, the
four dispositions, the structurally-unsuppressible banner class, and the three
import-time rules — none of which name a single alert key. ``remediation.py``
keeps the TABLE (the rows themselves, their owning branches and cadences) and
stays the one public surface: every name here is re-exported there, so
``remediation.Remedy`` / ``remediation.AUTO`` / ``remediation.resolve`` are
unchanged for every caller.

The dependency runs one way — rules never import rows — so there is no partial
module to observe mid-import, which is the failure a table-and-rules cycle
would have introduced.

Like its parent this module is PURE DATA plus lookups: no I/O, no index, no
state. ``brain alerts`` imports it at session start and must stay at the
engine's import floor.
"""

from __future__ import annotations

from dataclasses import dataclass

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


UNSUPPRESSIBLE_KEYS = frozenset({
    "blocked",
    "invariants-liveness",
    "synthesis-watchdog",
    "maintain:stale",
    "maintain:no-feed",
    "maintain:unparseable-feed",
    "cos:sheet-heartbeat",
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


def validate(
    exact: dict[str, Remedy],
    prefixes: tuple[tuple[str, Remedy], ...],
    cadences: dict[str, int],
) -> None:
    """Raise :class:`RegistryError` if the table breaks any structural rule.

    Called at import on the shipped table, and by tests on hand-built ones —
    which is why the bad-table failure is a VALIDATION failure, not a lookup
    that happens to go wrong later.

    ``cadences`` is passed IN rather than imported: the branch map lives with
    the table, and a rules module that reached back for it would be the import
    cycle this split exists to avoid."""
    for pattern, remedy in [*exact.items(), *prefixes]:
        _validate_row(pattern, remedy, cadences)

    for i, (a, _) in enumerate(prefixes):
        for b, _ in prefixes[i + 1:]:
            if a.startswith(b) or b.startswith(a):
                raise RegistryError(
                    f"prefixes {a!r} and {b!r} overlap — a key could match "
                    "both, and the registry never resolves ambiguity by order")


def _validate_row(pattern: str, remedy: Remedy, cadences: dict[str, int]) -> None:
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
        if remedy.owning_branch not in cadences:
            raise RegistryError(
                f"{pattern!r}: branch {remedy.owning_branch!r} has no declared "
                "cadence declared beside the table")
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
    exact: dict[str, Remedy],
    prefixes: tuple[tuple[str, Remedy], ...],
) -> Remedy | None:
    """The declared remedy for ``key``, or ``None`` when nothing declares it.

    Exact declarations are strictly more specific and win outright. Otherwise
    the prefix patterns are scanned and a key matching TWO of them raises
    :class:`AmbiguousKey` — first-match-wins would silently pick a disposition
    nobody chose."""
    if key in exact:
        return exact[key]
    matched = [(p, r) for p, r in prefixes if key.startswith(p)]
    if len(matched) > 1:
        raise AmbiguousKey(
            f"{key!r} matches {len(matched)} patterns "
            f"({', '.join(sorted(p for p, _ in matched))}) — the registry "
            "refuses to resolve by order")
    return matched[0][1] if matched else None
