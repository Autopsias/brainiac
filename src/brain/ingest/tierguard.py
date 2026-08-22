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
# The guard's vocabulary and its two records live in a sibling so this module
# stays under the size limit; re-exported so every existing
# `tierguard.UNAVAILABLE` / `.Verdict` / `.LegCounts` caller is unchanged.
from .tierguard_verdict import (  # noqa: E402
    CLEAR as CLEAR,
    GUARD_KEY as GUARD_KEY,
    GUARD_LEG_KEY as GUARD_LEG_KEY,
    GUARD_REASON_KEY as GUARD_REASON_KEY,
    GUARD_STATUSES as GUARD_STATUSES,
    LegCounts as LegCounts,
    NO_CORPUS as NO_CORPUS,
    RAISED as RAISED,
    SUBFLOOR as SUBFLOOR,
    UNAVAILABLE as UNAVAILABLE,
    Verdict as Verdict,
    _NO_CORPUS_ERROR as _NO_CORPUS_ERROR,
)



def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0", "false", "False")


def _floor() -> int:
    from ..index import _family_min_body

    return _family_min_body()


def _floor_bytes(body: str) -> int:
    from ..maintenance import _floor_bytes as fb

    return fb(body)


def _match_key(match: tuple[int, str, str, float]) -> tuple[int, bool, float]:
    """Sort matches by tier, decided evidence, then similarity."""
    rank, leg, _note_id, score = match
    return rank, leg == "same_document", score


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
        #: Why a SURVIVOR's body could not be read during the current
        #: `verdict()` call. A survivor is already a plausible higher-tier
        #: near-duplicate, so failing to read one is the guard failing to do
        #: its job on the document that matters most — not a "no match".
        #: Reset per verdict; consumed at the end of `verdict()`.
        self._unreadable: str = ""
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
            self._error = _NO_CORPUS_ERROR
            return None
        self._docs = docs
        return docs

    def _tokens_for(self, note_id: str) -> list[str] | None:
        """The comparison tokens for one SURVIVOR, or ``None`` if unreadable.

        An unreadable survivor is recorded in ``self._unreadable`` rather than
        being returned as a bare ``None``. Both look identical to the caller's
        `continue`, and without the record the document is admitted stamped
        `clear` — which asserts the guard compared it and found nothing. It
        did not compare it at all.
        """
        if note_id in self._tokens:
            return self._tokens[note_id]
        try:
            row = self._conn.execute(
                "SELECT body FROM notes WHERE id = ?", (note_id,)).fetchone()
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
            self._unreadable = (
                f"could not read the body of higher-tier candidate "
                f"{note_id!r}: {type(exc).__name__}: {exc}"
            )
            return None
        if row is None:
            self._unreadable = (
                f"higher-tier candidate {note_id!r} matched the screen but "
                "has no body row to compare against"
            )
            return None
        toks = _ct_tokens(row[0] or "")
        self._tokens[note_id] = toks
        return toks

    # -- the verdict ------------------------------------------------------
    def verdict(self, body: str, tier: str) -> Verdict:
        """Return the high-water admission tier for ``body``."""
        base = tier_of(tier)
        self._unreadable = ""
        early, tokens, docs = self._verdict_precheck(body, base)
        if early is not None:
            return early
        assert docs is not None
        survivors = self._screen_higher_tiers(tokens, tier_rank(base), docs)
        if not survivors:
            return self._clear_verdict(base)
        best = self._best_exact_match(tokens, survivors)
        if best is None:
            # A survivor the guard could not READ is not a survivor it
            # CLEARED. Stamping `clear` here asserts the comparison happened,
            # and it is that stamp `unguarded_ingests` counts — so a per-row
            # read failure on exactly the higher-tier twin that would have
            # raised this document used to admit it at the declared tier and
            # leave no trace. Fail loud instead: `unavailable` is the status
            # that ratchets at 0.
            if self._unreadable:
                return self._unreadable_verdict(base)
            return self._clear_verdict(base)
        # A raise already stands ABOVE the declared tier, so an unreadable
        # survivor cannot have caused a leak here — at worst the raise is
        # conservative (a higher unreadable twin might have raised it further).
        return self._raise_verdict(base, best)

    def _unreadable_verdict(self, base: str) -> Verdict:
        """The guard ran but could not compare a higher-tier candidate."""
        self.counts.judged -= 1
        self.counts.unavailable += 1
        return Verdict(
            tier=base, status=UNAVAILABLE,
            reason=(f"no cross-tier check was made: {self._unreadable} — "
                    "admitted at the declared tier"))

    def _verdict_precheck(
        self,
        body: str,
        base: str,
    ) -> tuple[
        Verdict | None,
        list[str],
        list[tuple[str, int, frozenset[int]]] | None,
    ]:
        """Apply the top-tier, body-floor, and corpus-availability gates."""
        if tier_rank(base) >= tier_rank(TIERS[-1]):
            self.counts.top_tier += 1
            return Verdict(tier=base, status=CLEAR), [], None
        floor = _floor()
        tokens = _ct_tokens(body or "")
        body_bytes = _floor_bytes(body or "")
        if len(tokens) < CROSS_TIER_MIN_TOKENS or body_bytes < floor:
            self.counts.subfloor += 1
            reason = (
                "body below the ENF-01 duplicate-identity floor "
                f"({body_bytes}B < {floor}B, {len(tokens)} tokens < "
                f"{CROSS_TIER_MIN_TOKENS}) — too short to judge, admitted "
                "at the declared tier"
            )
            return Verdict(tier=base, status=SUBFLOOR, reason=reason), tokens, None
        docs = self._table()
        if docs is None:
            if self._error == _NO_CORPUS_ERROR:
                self.counts.no_corpus += 1
                return Verdict(
                    tier=base, status=NO_CORPUS,
                    reason=("the corpus holds no comparable document to check "
                            "against — nothing could outrank this source, so it "
                            "is admitted at the declared tier")), tokens, None
            self.counts.unavailable += 1
            reason = (
                f"no cross-tier check was made: {self._error} — "
                "admitted at the declared tier"
            )
            return Verdict(tier=base, status=UNAVAILABLE, reason=reason), tokens, None
        self.counts.judged += 1
        return None, tokens, docs

    def _screen_higher_tiers(
        self,
        tokens: list[str],
        base_rank: int,
        docs: list[tuple[str, int, frozenset[int]]],
    ) -> list[tuple[str, int]]:
        """Return higher-tier documents surviving the shared sketch gate."""
        sketch = _ct_sketch(tokens)
        survivors = [
            (note_id, rank)
            for note_id, rank, other_sketch in docs
            if rank > base_rank
            and len(sketch & other_sketch)
            >= screen_gate(len(sketch), len(other_sketch))
        ]
        self.counts.screened += len(survivors)
        return survivors

    def _best_exact_match(
        self,
        tokens: list[str],
        survivors: list[tuple[str, int]],
    ) -> tuple[int, str, str, float] | None:
        """Choose the highest, strongest exact-leg match."""
        words = set(tokens)
        shingles = _ct_shingles(tokens)
        decided_only = _env_on(DECIDED_ONLY_ENV)
        best: tuple[int, str, str, float] | None = None
        for note_id, rank in survivors:
            measured = self._measure_exact_match(
                note_id,
                words,
                shingles,
                decided_only,
            )
            if measured is None:
                continue
            leg, score = measured
            candidate = (rank, leg, note_id, score)
            if best is None or _match_key(candidate) > _match_key(best):
                best = candidate
        return best

    def _measure_exact_match(
        self,
        note_id: str,
        words: set[str],
        shingles: set[tuple[str, ...]],
        decided_only: bool,
    ) -> tuple[str, float] | None:
        """Measure the exact shared-substance and same-document legs."""
        other = self._tokens_for(note_id)
        if other is None:
            return None
        self._words.setdefault(note_id, set(other))
        word_jaccard = _jaccard(words, self._words[note_id])
        if word_jaccard < CROSS_TIER_CANDIDATE:
            return None
        self.counts.shared_substance += 1
        self._shingles.setdefault(note_id, _ct_shingles(other))
        shingle_jaccard = _jaccard(shingles, self._shingles[note_id])
        if shingle_jaccard >= CROSS_TIER_SAME_DOC:
            self.counts.same_document += 1
            return "same_document", shingle_jaccard
        if decided_only:
            return None
        return "shared_substance", word_jaccard

    def _clear_verdict(self, base: str) -> Verdict:
        self.counts.clear += 1
        return Verdict(tier=base, status=CLEAR)

    def _raise_verdict(
        self,
        base: str,
        best: tuple[int, str, str, float],
    ) -> Verdict:
        rank, leg, note_id, score = best
        raised_to = TIERS[rank]
        self.counts.raised += 1
        self.counts.raised_by_leg[leg] = self.counts.raised_by_leg.get(leg, 0) + 1
        measure = (
            "5-word-shingle Jaccard" if leg == "same_document" else "word-set Jaccard"
        )
        threshold = (
            CROSS_TIER_SAME_DOC if leg == "same_document" else CROSS_TIER_CANDIDATE
        )
        reason = (
            f"raised {base} -> {raised_to}: {leg.replace('_', ' ')} as "
            f"{note_id} ({raised_to}), {measure} {score:.3f} >= {threshold}"
        )
        return Verdict(
            tier=raised_to,
            status=RAISED,
            leg=leg,
            twin=note_id,
            similarity=score,
            reason=reason,
        )

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
