"""ENF-04 — the ingest-time cross-tier guard: admit at the high-water mark.

``pipeline._meta`` stamps every drop-zone ingest ``classification: Internal``.
``_existing_note_classification`` already inherits the tier when the SAME id is
re-ingested, but the same DOCUMENT under a DIFFERENT id — a second date prefix,
a rename, a re-extraction — enters at ``Internal`` while its twin sits at
Restricted or MNPI. An Internal-capped reader (the Cowork VM's default) then
reaches high-tier substance through the low-tier copy. That produced 201
cross-tier pairs (fixed 2026-08-10) and 31 more the name-based metric could not
see (2026-08-12, ``_decisions/invariants-s13-crosstier-extension.md``).

**This guard never reasons about a filename.** ENF-02 did, and was withdrawn
whole when its "138 detections" turned out to be 138 filename matches and 0
content matches. Every decision here is made on BODY CONTENT, through the
ENF-03 detector's own primitives (``invariants._ct_tokens`` /``_ct_sketch``
/``_ct_shingles``/``_jaccard``/``screen_gate`` and its thresholds) — imported,
never re-implemented, so there is exactly ONE notion of document identity in
this engine.

Three legs, and each is counted separately in ``LegCounts`` because an
aggregate cannot tell a clean corpus from a dead leg:

1. ``screen``      bottom-k sketch intersection >= ``invariants.screen_gate``.
                   Cheap, approximate, recall measured (not asserted) by
                   ``tools/crosstier_coverage.py``. Contributes CANDIDATES.
2. ``shared_substance``  word-set Jaccard >= 0.60 on the exact token sets.
3. ``same_document``     5-word-shingle Jaccard >= 0.60 — word ORDER shared.

**Both 2 and 3 raise.** The ENF-03 *detector* deliberately refuses to decide the
undecided band (word-set without word-order) because it reports a corpus state.
A *guard* chooses an admission tier, and there the cost is asymmetric: a
wrongly-raised new source is over-classified and a human can lower it through
the audited path; a wrongly-admitted one is a leak that nothing notices. So the
undecided band fails CLOSED, to the higher tier, and says which leg decided it
in the note's own frontmatter.

**Directional, always.** The guard only ever RAISES, and only ever to a tier a
twin already carries. It cannot lower anything — so the email/attachment lane
(``provenance.email_classification``, MNPI by default) is untouched: nothing
outranks MNPI, and ``verdict()`` returns before building anything.

**The ENF-01 body-size floor applies first.** Below
``$BRAIN_FAMILY_MIN_BODY`` (default 1024 UTF-8 normalized bytes) a failed OCR
stub is byte-identical to every other failed OCR stub, so the guard REFUSES TO
JUDGE rather than guess — and says so on the note (``classification_guard:
subfloor``) instead of leaving a silent gap.

Cost, measured on the reference deployment (2,729 indexed notes, 2,427
comparable): a one-time corpus table of **5.0 s / 2.5 MB** built LAZILY on the
first document that actually needs it (an empty drop zone — the common hourly
case — pays nothing), then **~12 ms of screen + up to ~0.3 s of exact
verification per ingested document**. Against a naive per-document pairwise
scan, which is what this exists instead of.

ponytail: the corpus table is rebuilt once per ingest RUN and held in memory.
Persisting sketches in the index would amortize the 5 s across runs at the cost
of a schema column and an invalidation rule — worth it only if ingest runs stop
being dominated by PDF/OCR extraction, which today costs seconds to minutes per
document.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..classification import TIERS, normalize as tier_of, rank as tier_rank
from ..invariants import (
    CROSS_TIER_CANDIDATE,
    CROSS_TIER_MIN_TOKENS,
    CROSS_TIER_SAME_DOC,
    CROSS_TIER_SKIP_REASONS,
    _ct_shingles,
    _ct_sketch,
    _ct_tokens,
    _jaccard,
    link_coverage_exclusion,
    screen_gate,
)

DISABLED_ENV = "BRAIN_INGEST_TIER_GUARD_DISABLED"
#: Raise only on the DECIDED leg (5-word-shingle), leaving the undecided band
#: to enter at its declared tier. The shipped default is fail-closed (both legs
#: raise) per the brief's constraint 4, and that is the right default: measured
#: on the reference corpus, the undecided band is ~45 % genuinely one document
#: (divergent extractions and successive editions) against ~11 % that are a
#: different document quoting the higher-tier one. But 11 % is not nothing —
#: those enter over-classified — so a vault whose undecided band is dominated by
#: quoting documents can narrow the guard to the decided leg without turning it
#: off. Which leg decided a raise is on the note (`classification_guard_leg`),
#: so the alternative remedy — find them and lower them through the audited
#: path — stays available either way.
DECIDED_ONLY_ENV = "BRAIN_INGEST_TIER_GUARD_DECIDED_ONLY"

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
GUARD_STATUSES = (CLEAR, RAISED, SUBFLOOR, UNAVAILABLE)


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
    unavailable: int = 0       # no corpus to compare against (or disabled)
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
            "top_tier": self.top_tier,
            "legs": {"screen": self.screened,
                     "shared_substance": self.shared_substance,
                     "same_document": self.same_document},
            "raised_by_leg": dict(self.raised_by_leg),
        }


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0", "false", "False")


def _floor() -> int:
    from ..index import _family_min_body

    return _family_min_body()


def _floor_bytes(body: str) -> int:
    from ..maintenance import _floor_bytes as fb

    return fb(body)


class CrossTierGuard:
    """One per ingest run. Build is lazy; the caller threads the same instance
    through every candidate (and every nested zip member / eml attachment) so
    the corpus table is paid for once."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._docs: list[tuple[str, int, frozenset[int]]] | None = None
        #: Notes admitted THIS RUN before the corpus table was built. The
        #: top-tier bypass returns without building it, so an MNPI attachment
        #: processed first would otherwise be forgotten by the time an ordinary
        #: copy of the same document arrives later in the run — and that copy
        #: would enter at `Internal`, which is the exact leak this class exists
        #: to close. Flushed into the table the moment it is built.
        self._pending: list[tuple[str, int, list[str]]] = []
        self._tokens: dict[str, list[str]] = {}
        self._words: dict[str, set[str]] = {}
        self._shingles: dict[str, set[str]] = {}
        self._error: str = ""
        self.counts = LegCounts()

    # -- corpus table -----------------------------------------------------
    def _table(self) -> list[tuple[str, int, frozenset[int]]] | None:
        """``[(id, tier_rank, sketch)]`` for every comparable indexed note, or
        ``None`` when there is no corpus to compare against."""
        if self._docs is not None:
            return self._docs
        if self._error:
            return None
        if _env_on(DISABLED_ENV):
            self._error = f"disabled by ${DISABLED_ENV}"
            return None
        if self._conn is None:
            self._error = "no index connection"
            return None
        floor = _floor()
        try:
            rows = self._conn.execute(
                "SELECT id, classification, zone, path, is_latest_version, body "
                "FROM notes"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 — a missing/!built index is not a crash
            self._error = f"{type(exc).__name__}: {exc}"
            return None
        docs: list[tuple[str, int, frozenset[int]]] = []
        for nid, cls, zone, path, ilv, body in rows:
            # The ONE shared exclusion definition (G12). `superseded` is NOT
            # skipped, for the same reason ENF-03 keeps it: retiring a note
            # does not remove it from the index, so a retired high twin still
            # holds the tier this document must be raised to.
            if link_coverage_exclusion(
                    path=str(path or ""), zone=str(zone or ""),
                    is_latest_version=ilv) in CROSS_TIER_SKIP_REASONS:
                continue
            tokens = _ct_tokens(body or "")
            if len(tokens) < CROSS_TIER_MIN_TOKENS or _floor_bytes(body or "") < floor:
                continue
            docs.append((str(nid), tier_rank(cls), _ct_sketch(tokens)))
        for nid, rk, toks in self._pending:
            docs.append((nid, rk, _ct_sketch(toks)))
            self._tokens[nid] = toks
        self._pending.clear()
        if not docs:
            self._error = "index holds no comparable notes"
            return None
        self._docs = docs
        return docs

    def _tokens_for(self, note_id: str) -> list[str] | None:
        if note_id in self._tokens:
            return self._tokens[note_id]
        try:
            row = self._conn.execute(
                "SELECT body FROM notes WHERE id = ?", (note_id,)).fetchone()
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        toks = _ct_tokens(row[0] or "")
        self._tokens[note_id] = toks
        return toks

    # -- the verdict ------------------------------------------------------
    def verdict(self, body: str, tier: str) -> Verdict:
        """The tier ``body`` may be admitted at, given the corpus.

        Never below ``tier``. ``status`` is one of ``clear`` (judged, no
        higher-tier twin), ``raised``, ``subfloor`` (ENF-01: too short to
        judge) or ``unavailable`` (no corpus / disabled / index error)."""
        base = tier_of(tier)
        if tier_rank(base) >= tier_rank(TIERS[-1]):
            # Already at the top tier — nothing can outrank it, so there is
            # nothing to check and nothing that could lower it. This is the
            # email/attachment lane's whole path (MNPI by default): it never
            # builds the corpus table and never pays a millisecond.
            self.counts.top_tier += 1
            return Verdict(tier=base, status=CLEAR)

        floor = _floor()
        tokens = _ct_tokens(body or "")
        if len(tokens) < CROSS_TIER_MIN_TOKENS or _floor_bytes(body or "") < floor:
            self.counts.subfloor += 1
            return Verdict(
                tier=base, status=SUBFLOOR,
                reason=(f"body below the ENF-01 duplicate-identity floor "
                        f"({_floor_bytes(body or '')}B < {floor}B, "
                        f"{len(tokens)} tokens < {CROSS_TIER_MIN_TOKENS}) — "
                        "too short to judge, admitted at the declared tier"))

        docs = self._table()
        if docs is None:
            self.counts.unavailable += 1
            return Verdict(
                tier=base, status=UNAVAILABLE,
                reason=(f"no cross-tier check was made: {self._error} — "
                        "admitted at the declared tier"))

        self.counts.judged += 1
        base_rank = tier_rank(base)
        sketch = _ct_sketch(tokens)

        # -- leg 1: the screen. Only a STRICTLY HIGHER tier can change the
        # outcome, so the rank test comes first and most of the scan never
        # touches a set operation. `invariants.screen_gate` scales the gate with
        # the smaller sketch — see it for why a fixed 48 discarded every
        # low-vocabulary document. ENF-05 closed the same hole in the detector
        # this imports from, so there is one gate, measured once.
        survivors = [(nid, rk) for nid, rk, sk in docs
                     if rk > base_rank
                     and len(sketch & sk) >= screen_gate(len(sketch), len(sk))]
        self.counts.screened += len(survivors)
        if not survivors:
            self.counts.clear += 1
            return Verdict(tier=base, status=CLEAR)

        # -- legs 2 and 3: exact verification on the real token sets, for the
        # screen's survivors only.
        words = set(tokens)
        shingles = _ct_shingles(tokens)
        decided_only = _env_on(DECIDED_ONLY_ENV)
        best: tuple[int, str, str, float] | None = None  # rank, leg, id, score
        for nid, rk in survivors:
            other = self._tokens_for(nid)
            if other is None:
                continue
            if nid not in self._words:
                self._words[nid] = set(other)
            word_j = _jaccard(words, self._words[nid])
            if word_j < CROSS_TIER_CANDIDATE:
                continue
            self.counts.shared_substance += 1
            if nid not in self._shingles:
                self._shingles[nid] = _ct_shingles(other)
            shingle_j = _jaccard(shingles, self._shingles[nid])
            if shingle_j >= CROSS_TIER_SAME_DOC:
                self.counts.same_document += 1
                leg, score = "same_document", shingle_j
            elif decided_only:
                continue
            else:
                leg, score = "shared_substance", word_j
            # High-water mark: the most sensitive twin wins. Between two twins
            # at the SAME tier prefer the decided leg, then the higher score —
            # the reason line should name the strongest evidence, not the
            # first row scanned.
            key = (rk, leg == "same_document", score)
            if best is None or key > (best[0], best[1] == "same_document", best[3]):
                best = (rk, leg, nid, score)

        if best is None:
            self.counts.clear += 1
            return Verdict(tier=base, status=CLEAR)

        rk, leg, nid, score = best
        raised_to = TIERS[rk]
        self.counts.raised += 1
        self.counts.raised_by_leg[leg] = self.counts.raised_by_leg.get(leg, 0) + 1
        measure = ("5-word-shingle Jaccard" if leg == "same_document"
                   else "word-set Jaccard")
        return Verdict(
            tier=raised_to, status=RAISED, leg=leg, twin=nid, similarity=score,
            reason=(f"raised {base} -> {raised_to}: {leg.replace('_', ' ')} as "
                    f"{nid} ({raised_to}), {measure} {score:.3f} "
                    f">= {CROSS_TIER_SAME_DOC if leg == 'same_document' else CROSS_TIER_CANDIDATE}"))

    # -- in-run admission -------------------------------------------------
    def admit(self, note_id: str, body: str, tier: str) -> None:
        """Record a note this run just wrote, so a SECOND copy arriving later
        in the SAME run sees it. ``core.write_note`` signs and writes the file
        but does not index — the index sync runs after the whole drain — so
        without this two copies dropped together both enter at ``Internal``."""
        tokens = _ct_tokens(body or "")
        if len(tokens) < CROSS_TIER_MIN_TOKENS or _floor_bytes(body or "") < _floor():
            return
        if self._docs is None:
            # Not built yet (an MNPI-first run, or a disabled/broken index).
            # Held rather than dropped — see `_pending`.
            self._pending.append((note_id, tier_rank(tier), tokens))
            return
        self._docs.append((note_id, tier_rank(tier), _ct_sketch(tokens)))
        self._tokens[note_id] = tokens


def guard_for(core: Any) -> CrossTierGuard:
    """Build the run's guard from a HOST ``BrainCore``. Never raises: an index
    that cannot be opened yields a guard that reports ``unavailable`` on every
    document, which is counted and stamped, never silent."""
    try:
        conn = core.index.conn
    except Exception:  # noqa: BLE001 — a broken index must not abort a drain
        conn = None
    return CrossTierGuard(conn)
