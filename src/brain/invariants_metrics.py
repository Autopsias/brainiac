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


def _load_cross_tier_docs(
    conn: Any, floor: int,
) -> tuple[list[tuple[str, str, frozenset[int]]], dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, classification, zone, path, is_latest_version, body FROM notes"
    ).fetchall()
    excluded_by_reason: dict[str, int] = {}
    retained_superseded = 0
    population = 0
    too_short = 0
    subfloor = 0
    docs: list[tuple[str, str, frozenset[int]]] = []
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
        docs.append((str(nid), tier_of(cls), _ct_sketch(tokens)))
    return docs, {
        "excluded_by_reason": excluded_by_reason,
        "retained_superseded": retained_superseded,
        "population": population,
        "too_short": too_short,
        "subfloor": subfloor,
    }


def _find_cross_tier_survivors(
    docs: list[tuple[str, str, frozenset[int]]],
) -> list[tuple[int, int]]:
    survivors: list[tuple[int, int]] = []
    for index, (_, tier, sketch) in enumerate(docs):
        for other_index in range(index + 1, len(docs)):
            _, other_tier, other_sketch = docs[other_index]
            if other_tier == tier:
                continue
            if len(sketch & other_sketch) >= _invariants.screen_gate(
                    len(sketch), len(other_sketch)):
                survivors.append((index, other_index))
    return survivors


def _load_cross_tier_tokens(
    conn: Any, docs: list[tuple[str, str, frozenset[int]]],
    survivors: list[tuple[int, int]],
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
    docs: list[tuple[str, str, frozenset[int]]],
    survivors: list[tuple[int, int]],
    tokens_by_id: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    words: dict[str, set[str]] = {}
    shingles: dict[str, set[str]] = {}
    conflicts: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for first_index, second_index in survivors:
        first_id, first_tier, _ = docs[first_index]
        second_id, second_tier, _ = docs[second_index]
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
    return (f"{record[key]:.3f} {record['a_tier']} {record['a']} / "
            f"{record['b_tier']} {record['b']}")


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
