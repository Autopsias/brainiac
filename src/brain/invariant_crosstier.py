"""ENF-03 content-decided cross-tier duplicate detection primitives."""
from __future__ import annotations

import re
from typing import Any

from .invariant_shared import SAMPLE_CAP

def cross_tier_twins(conn: Any, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """``<ingest-date>-<slug>`` / ``<slug>`` id pairs at DIFFERENT
    classifications (metric 2). Reports the whole twin population alongside
    the cross-tier subset — a twin count alone says nothing about exposure."""
    rows = conn.execute("SELECT id, classification FROM notes").fetchall()
    cls = {str(r[0]): str(r[1] or "") for r in rows}
    pairs = 0
    cross: list[tuple[str, str]] = []
    for nid in sorted(cls):
        if not _DATE_PREFIX.match(nid):
            continue
        stem = _DATE_PREFIX.sub("", nid, count=1)
        if stem not in cls:
            continue
        pairs += 1
        if cls[nid] != cls[stem]:
            cross.append((nid, stem))
    return {
        "value": len(cross),
        "pairs": pairs,
        "sample": [f"{a} ({cls[a] or 'unlabelled'}) / {b} ({cls[b] or 'unlabelled'})"
                    for a, b in cross[:cap]],
        # EXC-01: the cross-tier pairs themselves, in the shape the
        # content detector returns them, so one owner question can be staged
        # per pair. `pairs` above is the whole TWIN population (a count) and
        # was already taken; this is the CROSS-tier subset.
        "cross_pairs": [{"a": a, "a_tier": cls[a], "b": b, "b_tier": cls[b]}
                        for a, b in cross],
    }


# ---------------------------------------------------------------------------
# ENF-03 — the cross-tier near-duplicate detector. CONTENT, never filenames.
#
# `cross_tier_twins` above compares ONE filename shape (`<date>-slug` vs
# `slug`). Every other way one document appears twice — a second embedded
# date, a rename, an OCR-vs-text-layer re-extraction, a `-v1`/`-v2` pair, a
# mangled accent — is invisible to it, so it reads 0 while real cross-tier
# duplicates sit in the corpus. ENF-02 tried to close that on FILENAMES and
# was withdrawn whole: its 138 "detections" were 138 filename matches and 0
# content matches. So this detector never looks at an id.
#
# TWO measures, because one of them cannot honestly decide on its own, and a
# detector that guesses is the failure mode this plan exists to prevent:
#
#   SAME DOCUMENT (decided)  5-word-shingle Jaccard >= 0.60. Word ORDER is
#       load-bearing: two documents share 5-word runs only when one is a copy,
#       a re-extraction or a light edit of the other. This is s03's pre-stated
#       definition (2026-08-10), unchanged, so the >= 0.90 coverage bar is
#       judged on the basis it was written for.
#   SHARES THE SUBSTANCE (undecided)  word-SET Jaccard >= 0.60 while the
#       shingle measure says less. Same vocabulary, different word order: a
#       reformatted copy, a later revision of the same deck — or two genuinely
#       different documents about one subject. The detector will NOT guess.
#       These are REPORTED as unclassified candidates, never merged, never
#       silently counted as clean. (Measured on the reference vault: the
#       shingle population is a strict SUBSET of the word-set population, so
#       one screen serves both.)
#
# The ENF-01 body-size floor applies before either: two notes are never judged
# the same document on a body too short to carry evidence of anything.
# ---------------------------------------------------------------------------
CROSS_TIER_SHINGLE = 5
CROSS_TIER_MIN_TOKENS = 40
CROSS_TIER_SAME_DOC = 0.60      # 5-word-shingle Jaccard: the same document
CROSS_TIER_CANDIDATE = 0.60     # word-set Jaccard: shares the substance
CROSS_TIER_SKETCH = 192         # bottom-k sketch width, screening only
# Screen gate as a fraction of the sketch. A pair at word-set Jaccard 0.60
# lands near 0.43*k shared sketch entries (resemblance j/(2-j)); 0.25*k is
# ~5 sigma below that, and the screen's real-world recall is not assumed from
# this arithmetic — it is MEASURED against an exhaustive all-pairs scan by
# `tools/crosstier_coverage.py`, which is where the reported coverage number
# comes from.
CROSS_TIER_SCREEN = 0.25
# `link_coverage_exclusion` reasons this metric SKIPS. Deliberately NOT
# `superseded`: retiring a note does not remove it from the index
# (`--latest-only` is opt-in), so a superseded low twin is still fully
# readable at its own classification and is still the exposure. Excluding it
# here would blind the detector to exactly the population s04 just linked.
# Superseded notes are therefore KEPT and reported as `retained_superseded`.
CROSS_TIER_SKIP_REASONS = ("quarantined", "non_knowledge_zone", "generated_map")

_CT_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
_CT_WORD = re.compile(r"\w+", re.U)


def _ct_tokens(body: str) -> list[str]:
    """The normalized token stream both measures are computed over: a leading
    frontmatter block stripped (191 of the July re-ingests carry the source
    file's frontmatter pasted INTO the body — an extraction artifact, not
    content), NFC, casefold, then ``\\w+`` words.

    ``\\w+`` and not ``str.split()``, and that choice is measured rather than
    stylistic: splitting on whitespace leaves punctuation glued to the token,
    so a re-extraction that moves one comma breaks the 5-word runs around it.
    Measured on the reference deployment (ENF-03, 2026-08-12), a live pair of
    one document held at Internal and at MNPI scores 0.484 whitespace-split
    and **0.788** here — invisible against a 0.60 threshold under the one, a
    decided conflict under the other. Punctuation drift is precisely what a
    second extraction pass produces, so the tokenizer must not be sensitive
    to it."""
    import unicodedata
    text = _CT_FRONTMATTER.sub("", body or "", count=1)
    return _CT_WORD.findall(unicodedata.normalize("NFC", text).casefold())


def _ct_shingles(tokens: list[str], k: int = CROSS_TIER_SHINGLE) -> set[str]:
    return {" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


def _jaccard(a: set[Any], b: set[Any]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if inter else 0.0


def _ct_sketch(tokens: list[str], k: int = CROSS_TIER_SKETCH) -> frozenset[int]:
    """Bottom-k sketch of the word set — the SCREEN only, never a verdict.

    ``zlib.crc32`` and not a cryptographic digest because this runs over every
    note every night and its collisions cost nothing: a survivor is re-checked
    EXACTLY on the real token sets before it is called anything. Holding the
    full shingle sets for the whole corpus instead costs ~760 MB (measured on
    the reference vault); the sketch costs ~4 MB."""
    import zlib
    hashes = sorted({zlib.crc32(t.encode("utf-8")) for t in set(tokens)})
    return frozenset(hashes[:k])


def screen_gate(a: int, b: int) -> int:
    """Shared sketch entries a pair must reach to survive the screen, for
    sketches of size ``a`` and ``b``. THE one definition — the ingest guard
    (``ingest.tierguard``) imports this one rather than carrying its own.

    It SCALES with the smaller sketch, and that is a fix, not a flourish. A
    bottom-k sketch of a set smaller than k IS the whole set, so a document
    above the ENF-01 body floor whose vocabulary is under
    ``CROSS_TIER_SKETCH * CROSS_TIER_SCREEN`` (48 distinct words) — a form, a
    rate card, a repetitive template — can never reach 48 shared entries, and
    a verbatim cross-tier copy of it scoring 5-word-shingle Jaccard 1.000 was
    discarded by the screen before anything measured it. Identical to the old
    fixed ``CROSS_TIER_SKETCH * CROSS_TIER_SCREEN`` (48) wherever both sketches
    are full width, so nothing above the vocabulary threshold changes.

    Loosening the screen can only ADD survivors, never change a verdict: every
    survivor is re-verified exactly on the real token sets afterwards."""
    return max(1, int(CROSS_TIER_SCREEN * min(a, b)))


def _ct_exclusion(path: str, zone: str, ilv: Any) -> str | None:
    return link_coverage_exclusion(path=path, zone=zone, is_latest_version=ilv)


def cross_tier_candidates_entry(dup: dict[str, Any]) -> dict[str, Any]:
    """Metric 6 — the UNDECIDED half of metric 5, ratcheted on its own so a
    growing pile of pairs the detector cannot decide alerts exactly like a
    growing pile it can. ``cross_tier_twins`` had no undecided bucket at all,
    which is why its "0 unclassified" was structurally incapable of being
    anything else (s12 acceptance review, criterion 3)."""
    if dup.get("error"):
        return {"value": None, "error": dup["error"]}
    return {
        "value": dup.get("candidates"),
        "population": dup.get("population"),
        "comparable": dup.get("comparable"),
        "coverage": dup.get("coverage"),
        "sample": dup.get("candidate_sample") or [],
        # EXC-01: the undecided half's own pair population (see the sibling
        # note in `invariants_metrics._cross_tier_duplicates_impl`).
        "pairs": dup.get("candidate_pairs") or [],
    }


# ---------------------------------------------------------------------------
# ENF-04 — the ingest-time cross-tier guard's own numbers (metric 7).
#
# The guard (`brain.ingest.tierguard`) stamps EVERY note the ingest pipeline
# writes with `classification_guard: clear|raised|subfloor|unavailable`, so its
# outcome is recorded in the note's own signed frontmatter rather than in a
# side ledger that can be lost or forged. This reads them back.
#
# ONE of the four statuses ratchets, and the choice is deliberate. `raised` and
# `subfloor` are MONOTONE over an append-only zone (`raw/` is immutable), so a
# min-ever floor would alert on every single firing of a working guard — a
# false-alarm generator, not a watchdog. `unavailable` is different: it means
# the guard was supposed to run and COULDN'T (no index, a read error, or
# `$BRAIN_INGEST_TIER_GUARD_DISABLED`). It should be 0 and stay 0, so the
# ratchet is exactly right there and fires precisely when the guard dies.
#
# The invariant the guard SERVES — one document, one classification — already
# ratchets, as `cross_tier_duplicates`/`cross_tier_candidates`. Holding those
# at their floor is the guard's job; this metric only proves the guard is alive
# and shows what it did (AGENTS.md §4 rule 6).
# ---------------------------------------------------------------------------

# Parent-namespace binds, deferred past this module's own defs.
from .invariants import _DATE_PREFIX as _DATE_PREFIX  # noqa: E402
from .invariant_coverage import link_coverage_exclusion as link_coverage_exclusion  # noqa: E402
