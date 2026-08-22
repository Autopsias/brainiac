"""ENF-04 vocabulary: the guard's stamped statuses and its two records.

Split out of ``tierguard`` so the guard module itself stays under the repo's
500-LOC limit. Nothing here decides anything — it is the words the guard
speaks (`clear`/`raised`/`subfloor`/`unavailable`/`no_corpus`), the note
frontmatter it stamps, and the per-run leg tally. ``tierguard`` re-exports
every name, so `tierguard.UNAVAILABLE`, `tierguard.Verdict` and
`tierguard.LegCounts` all keep working for existing callers and tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


#: Frontmatter key stamped on EVERY note this pipeline writes. Its presence is
#: the proof the guard ran; its value names the outcome. Counted by
#: ``invariants.ingest_guard`` — so "0 raised" can never quietly mean "never
#: looked", because `subfloor`/`unavailable` are counted on the same row.
GUARD_KEY = "classification_guard"
GUARD_REASON_KEY = "classification_guard_reason"
#: Which LEG decided a raise, as a structured value rather than a substring of
#: the reason line. The nightly counts raises per leg from this, so a leg that
#: silently stops firing on real content is visible in the trend — the ENF-02
#: failure mode, which nobody could see because only the total was reported.
GUARD_LEG_KEY = "classification_guard_leg"

CLEAR = "clear"
RAISED = "raised"
SUBFLOOR = "subfloor"
UNAVAILABLE = "unavailable"
#: The guard RAN and the corpus held nothing comparable — distinct from
#: ``unavailable``, which means it could not run (no connection, read error,
#: disabled). Split 2026-08-17: a cross-tier leak needs an existing
#: higher-tier near-duplicate, so a corpus with no comparable document has
#: nothing to leak FROM — counting that as "unguarded" made every brand-new
#: vault's FIRST document trip a ratcheting invariant whose floor is 0, which
#: is a watchdog crying wolf on the safe, normal case. Same distinction as
#: doctor's "cannot see it" vs "looked, and it is not there".
NO_CORPUS = "no_corpus"
#: The sentinel `_table()` sets ONLY after the index opened and the query
#: SUCCEEDED and every row was filtered out — the two real failure paths ("no
#: index connection", an exception) return before it, so matching on it can
#: never silence a genuine guard failure.
_NO_CORPUS_ERROR = "index holds no comparable notes"
GUARD_STATUSES = (CLEAR, RAISED, SUBFLOOR, UNAVAILABLE, NO_CORPUS)


@dataclass
class Verdict:
    """What tier this document is admitted at, and why."""

    tier: str
    status: str
    reason: str = ""
    leg: str = ""
    twin: str = ""
    similarity: float | None = None

    def frontmatter(self) -> dict[str, Any]:
        """The keys to merge into the note's frontmatter. ``clear`` carries no
        reason — the status alone says the guard ran and found nothing."""
        out: dict[str, Any] = {GUARD_KEY: self.status}
        if self.leg:
            out[GUARD_LEG_KEY] = self.leg
        if self.reason:
            out[GUARD_REASON_KEY] = self.reason
        return out


@dataclass
class LegCounts:
    """Per-leg firing counts for one ingest run. Reported in the ingest report
    and in the guard's own evidence, because an aggregate "0 raised" cannot be
    told apart from a leg that never fires — which is exactly how ENF-02's 138
    filename matches / 0 content matches went unnoticed."""

    judged: int = 0            # documents the guard actually compared
    subfloor: int = 0          # refused: body below the ENF-01 floor
    unavailable: int = 0       # the guard could NOT run (no conn / read error / disabled)
    no_corpus: int = 0         # it ran; the corpus held nothing comparable
    top_tier: int = 0          # already MNPI — nothing can outrank it
    screened: int = 0          # leg 1: sketch-screen survivors, summed
    shared_substance: int = 0  # leg 2: pairs passing word-set Jaccard
    same_document: int = 0     # leg 3: pairs passing 5-word-shingle Jaccard
    raised: int = 0            # documents admitted at a RAISED tier
    clear: int = 0             # judged, no higher-tier twin
    raised_by_leg: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "judged": self.judged, "clear": self.clear, "raised": self.raised,
            "subfloor": self.subfloor, "unavailable": self.unavailable,
            "no_corpus": self.no_corpus,
            "top_tier": self.top_tier,
            "legs": {"screen": self.screened,
                     "shared_substance": self.shared_substance,
                     "same_document": self.same_document},
            "raised_by_leg": dict(self.raised_by_leg),
        }
