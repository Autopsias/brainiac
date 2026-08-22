"""Cross-tier corpus metric."""
from __future__ import annotations

from typing import Any

def cross_tier_duplicates(
    conn: Any, *, cap: int = 10, detail: bool = False,
) -> dict[str, Any]:
    """Documents that exist twice at DIFFERENT classifications, found by
    content (metric 5, and its undecided half metric 6).

    Returns three numbers that are never collapsed into one:
    ``value`` (decided conflicts), ``candidates`` (undecided), and
    ``coverage`` (the fraction of the exposure population this detector can
    fingerprint at all, with ``population`` as its stated denominator)."""
    return _cross_tier_duplicates_impl(conn, cap=cap, detail=detail)


#: One scanned document: ``(id, normalized tier, RAW classification, sketch)``.
#:
#: The raw value rides alongside the normalized one because the two answer
#: different questions and only one of them was being carried. `tier_of`
#: (``classification.normalize``) maps a MISSING or mis-cased label to the
#: default-deny tier, which is right for deciding rank and for deciding
#: whether two documents sit at different tiers — and destroys the only
#: evidence that a note asserts nothing at all. This lane's tier rule
#: (`remediation_answers.unraisable`) exists precisely to refuse a pair whose
#: member carries no classification, and it was UNREACHABLE from here for
#: three review rounds: by the time it saw a pair, an unlabelled note was the
#: string "MNPI" and the guard could not fire. Nothing about the metric's
#: arithmetic reads this field — it is carried so the consumer can tell "this
#: note asserts MNPI" apart from "this note asserts nothing".
_Doc = tuple[str, str, str, frozenset[int]]


def _load_cross_tier_docs(
    conn: Any, floor: int,
) -> tuple[list[_Doc], dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, classification, zone, path, is_latest_version, body FROM notes"
    ).fetchall()
    excluded_by_reason: dict[str, int] = {}
    retained_superseded = 0
    population = 0
    too_short = 0
    subfloor = 0
    docs: list[_Doc] = []
    for nid, cls, zone, path, ilv, body in rows:
        reason = _ct_exclusion(str(path or ""), str(zone or ""), ilv)
        if reason:
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
            if reason in CROSS_TIER_SKIP_REASONS:
                continue
            retained_superseded += 1
        population += 1
        tokens = _ct_tokens(body or "")
        short = len(tokens) < CROSS_TIER_MIN_TOKENS
        below = _floor_bytes(body or "") < floor
        too_short += short
        subfloor += below
        if short or below:
            continue
        docs.append((str(nid), tier_of(cls), str(cls or ""), _ct_sketch(tokens)))
    return docs, {
        "excluded_by_reason": excluded_by_reason,
        "retained_superseded": retained_superseded,
        "population": population,
        "too_short": too_short,
        "subfloor": subfloor,
    }


def _find_cross_tier_survivors(docs: list[_Doc]) -> list[tuple[int, int]]:
    """Screened pairs, decided on the NORMALIZED tier — unchanged, and it must
    stay that way: two documents sit at different tiers when their EFFECTIVE
    tiers differ, so an unlabelled note is compared as MNPI here exactly as at
    the egress gate. The raw value is carried past this, never used in it."""
    survivors: list[tuple[int, int]] = []
    for index, (_, tier, _raw, sketch) in enumerate(docs):
        for other_index in range(index + 1, len(docs)):
            _, other_tier, _other_raw, other_sketch = docs[other_index]
            if other_tier == tier:
                continue
            if len(sketch & other_sketch) >= _invariants.screen_gate(
                    len(sketch), len(other_sketch)):
                survivors.append((index, other_index))
    return survivors


def _load_cross_tier_tokens(
    conn: Any, docs: list[_Doc], survivors: list[tuple[int, int]],
) -> dict[str, list[str]]:
    wanted = sorted({docs[index][0] for index, _ in survivors}
                    | {docs[index][0] for _, index in survivors})
    tokens_by_id: dict[str, list[str]] = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        query = ",".join("?" * len(chunk))
        for nid, body in conn.execute(
                f"SELECT id, body FROM notes WHERE id IN ({query})", chunk):
            tokens_by_id[str(nid)] = _ct_tokens(body or "")
    return tokens_by_id


def _classify_cross_tier_matches(
    docs: list[_Doc],
    survivors: list[tuple[int, int]],
    tokens_by_id: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    words: dict[str, set[str]] = {}
    shingles: dict[str, set[str]] = {}
    conflicts: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for first_index, second_index in survivors:
        # AS RECORDED, not normalized: `a_tier`/`b_tier` are what the owner-
        # question lane consumes, and it has to be able to see an ABSENCE.
        first_id, _first_norm, first_tier, _ = docs[first_index]
        second_id, _second_norm, second_tier, _ = docs[second_index]
        first_tokens = tokens_by_id.get(first_id)
        second_tokens = tokens_by_id.get(second_id)
        if first_tokens is None or second_tokens is None:
            continue
        words.setdefault(first_id, set(first_tokens))
        words.setdefault(second_id, set(second_tokens))
        word_jaccard = _jaccard(words[first_id], words[second_id])
        if word_jaccard < CROSS_TIER_CANDIDATE:
            continue
        shingles.setdefault(first_id, _ct_shingles(first_tokens))
        shingles.setdefault(second_id, _ct_shingles(second_tokens))
        shingle_jaccard = _jaccard(shingles[first_id], shingles[second_id])
        record = {
            "a": first_id, "a_tier": first_tier,
            "b": second_id, "b_tier": second_tier,
            "shingle_jaccard": round(shingle_jaccard, 4),
            "word_jaccard": round(word_jaccard, 4),
        }
        (conflicts if shingle_jaccard >= CROSS_TIER_SAME_DOC else unclassified).append(record)
    conflicts.sort(key=lambda record: -record["shingle_jaccard"])
    unclassified.sort(key=lambda record: -record["word_jaccard"])
    return conflicts, unclassified


def _format_cross_tier_sample(record: dict[str, Any], key: str) -> str:
    """Display only — through the SAME renderer the owner-question lane uses.

    An absent label printed as ``unlabelled`` was already right; a MIS-CASED
    one was not. ``classification: internal`` printed as ``internal`` beside a
    genuine ``MNPI`` twin, so a human reading the row saw two different words
    and no sign that one of them is default-denied — an MNPI-vs-Internal
    exposure reading as ordinary. ``remediation_exceptions._shown`` already
    says both halves ("internal (unrecognised — treated as MNPI)"), and a
    third renderer here is how the two would eventually disagree
    (s06 review, 2026-08-22).

    Imported lazily: this module is on ``invariants``' import path and the
    exceptions lane is not, so a module-level import would tie the metric to
    the whole owner-question stack for one display string."""
    from .remediation_exceptions import _shown

    return (f"{record[key]:.3f} {_shown(record['a_tier'])} {record['a']} / "
            f"{_shown(record['b_tier'])} {record['b']}")


def _cross_tier_duplicates_impl(
    conn: Any, *, cap: int, detail: bool,
) -> dict[str, Any]:
    floor = _family_min_body()
    docs, stats = _load_cross_tier_docs(conn, floor)
    survivors = _find_cross_tier_survivors(docs)
    tokens_by_id = _load_cross_tier_tokens(conn, docs, survivors)
    conflicts, unclassified = _classify_cross_tier_matches(docs, survivors, tokens_by_id)
    population = stats["population"]
    out: dict[str, Any] = {
        "value": len(conflicts),
        "candidates": len(unclassified),
        "population": population,
        "comparable": len(docs),
        "coverage": round(len(docs) / population, 4) if population else None,
        "coverage_basis": (
            "comparable / population; population = indexed notes minus "
            + "/".join(CROSS_TIER_SKIP_REASONS)
            + " (superseded notes are RETAINED, they still leak); comparable = "
            f"those with a body >= {floor}B and >= {CROSS_TIER_MIN_TOKENS} tokens"),
        "too_short": stats["too_short"],
        "subfloor": stats["subfloor"],
        "floor": floor,
        "excluded_by_reason": stats["excluded_by_reason"],
        "retained_superseded": stats["retained_superseded"],
        "screened": len(survivors),
        "sample": [_format_cross_tier_sample(record, "shingle_jaccard")
                   for record in conflicts[:cap]],
        "candidate_sample": [_format_cross_tier_sample(record, "word_jaccard")
                             for record in unclassified[:cap]],
        # EXC-01: the FULL pair populations behind the two counts, in the same
        # `{a, a_tier, b, b_tier, ...}` shape `cross_tier_twins` returns, so
        # the owner-question layer stages one proposal per pair instead of
        # re-deriving pairs from the capped, human-readable `sample` strings.
        # Same posture as `subfloor_families["members"]`: unbounded here for
        # the in-process consumer, truncated at the PERSISTENCE boundary by
        # `invariants.persistable_metrics`.
        "pairs": conflicts,
        "candidate_pairs": unclassified,
    }
    if detail:
        out["conflicts"] = conflicts
        out["unclassified"] = unclassified
    return out


from . import classification as _classification  # noqa: E402
from . import index as _index  # noqa: E402
from . import invariants as _invariants  # noqa: E402
from . import maintenance as _maintenance  # noqa: E402

tier_of = _classification.normalize
_family_min_body = _index._family_min_body
CROSS_TIER_CANDIDATE = _invariants.CROSS_TIER_CANDIDATE
CROSS_TIER_MIN_TOKENS = _invariants.CROSS_TIER_MIN_TOKENS
CROSS_TIER_SAME_DOC = _invariants.CROSS_TIER_SAME_DOC
CROSS_TIER_SKIP_REASONS = _invariants.CROSS_TIER_SKIP_REASONS
_ct_exclusion = _invariants._ct_exclusion
_ct_shingles = _invariants._ct_shingles
_ct_sketch = _invariants._ct_sketch
_ct_tokens = _invariants._ct_tokens
_jaccard = _invariants._jaccard
_floor_bytes = _maintenance._floor_bytes
