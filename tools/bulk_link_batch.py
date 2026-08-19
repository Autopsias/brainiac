"""Validate bulk link batches."""
from __future__ import annotations

from pathlib import Path

from tools.bulk_link_check import (
    MAX_PAIR_SIMILARITY,
    MIN_BODY_CHARS,
    MIN_NOVEL_TOKEN_RATIO,
    MIN_PROPOSITIONS,
    TIERS,
    _FIGURE,
    _LINK,
    _function_words,
    _sentences,
    _shingles,
    _tokens,
    _unwrap,
)


def _load_batch_notes(vault: Path, batch: str) -> list[dict]:
    from brain import frontmatter as fm

    notes: list[dict] = []
    for path in sorted((vault / "brain").rglob("*.md")):
        meta, body = fm.parse_text(path.read_text(encoding="utf-8"))
        if not meta or str(meta.get("bulk_link_batch") or "").strip() != batch:
            continue
        notes.append({"path": str(path.relative_to(vault)), "meta": meta, "body": body})
    return notes


def _load_tiers(vault: Path) -> dict[str, str]:
    from brain import frontmatter as fm

    tier_of: dict[str, str] = {}
    for path in sorted(vault.rglob("*.md")):
        if any(part in {".brain", "inbox", "overlay", "originals"}
               for part in path.parts):
            continue
        try:
            meta, _ = fm.parse_text(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta and meta.get("id"):
            tier_of[str(meta["id"])] = str(meta.get("classification") or "MNPI")
    return tier_of


def _load_raw_ids(vault: Path) -> set[str]:
    from brain import frontmatter as fm

    return {
        str(meta["id"])
        for meta in (
            fm.parse_text(path.read_text(encoding="utf-8"))[0] or {}
            for path in (vault / "raw").rglob("*.md")
            if "originals" not in path.parts
        )
        if meta.get("id")
    }


def _note_metrics(
    meta: dict,
    body: str,
    rel: str,
    tier_of: dict[str, str],
    raw_ids: set[str],
    stop: set[str],
) -> tuple[dict, list[str]]:
    nid = str(meta.get("id") or rel)
    title = str(meta.get("title") or "")
    title_tokens = set(_tokens(title))
    body_tokens = _tokens(body)
    frequency_ratio = (
        sum(1 for token in body_tokens if token not in title_tokens) / len(body_tokens)
        if body_tokens else 0.0
    )
    vocabulary = {token for token in body_tokens if token not in stop}
    novel_vocabulary = vocabulary - title_tokens
    novel_ratio = len(novel_vocabulary) / len(vocabulary) if vocabulary else 0.0
    propositions = [
        sentence for sentence in _sentences(body)
        if _FIGURE.search(sentence) and any(
            token not in title_tokens
            for token in _tokens(sentence)
            if _FIGURE.search(token)
        )
    ]
    cited = [
        target for line in _unwrap(body)
        for target in _LINK.findall(line)
        if target.split("/")[-1] in raw_ids
    ]
    bare = [
        line.strip() for line in _unwrap(body)
        if any(target.split("/")[-1] in raw_ids for target in _LINK.findall(line))
        and "—" not in line and " - " not in line
    ]
    unresolvable = [
        target for line in _unwrap(body)
        for target in _LINK.findall(line)
        if target.startswith("raw/")
    ]
    required_tier = max(
        (TIERS.index(tier_of.get(cited_id.split("/", 1)[-1], "MNPI"))
         for cited_id in cited),
        default=0,
    )
    current_classification = str(meta.get("classification") or "Public")
    current_tier = TIERS.index(current_classification) if current_classification in TIERS else -1
    row = {
        "id": nid,
        "path": rel,
        "body_chars": len(body),
        "propositions": len(propositions),
        "novel_token_ratio": round(novel_ratio, 4),
        "token_frequency_ratio": round(frequency_ratio, 4),
        "cited_sources": cited,
        "bare_links": bare,
        "graph_invisible_links": unresolvable,
        "classification": meta.get("classification"),
        "min_required_tier": TIERS[required_tier] if cited else None,
    }
    failures = _note_failures(
        meta, body, nid, propositions, novel_ratio, cited, bare, unresolvable,
        current_tier, required_tier,
    )
    return row, failures


def _note_failures(
    meta: dict,
    body: str,
    nid: str,
    propositions: list[str],
    novel_ratio: float,
    cited: list[str],
    bare: list[str],
    unresolvable: list[str],
    current_tier: int,
    required_tier: int,
) -> list[str]:
    failures: list[str] = []
    if str(meta.get("type") or "") != "source-derived":
        failures.append(f"{nid}: type is {meta.get('type')!r}, not source-derived")
    if not str(meta.get("source") or "").strip():
        failures.append(f"{nid}: no `source:` frontmatter anchor")
    if len(body) < MIN_BODY_CHARS:
        failures.append(f"{nid}: body {len(body)}B < {MIN_BODY_CHARS}B floor")
    if len(propositions) < MIN_PROPOSITIONS:
        failures.append(f"{nid}: {len(propositions)} figure-bearing propositions "
                        f"absent from the title < {MIN_PROPOSITIONS}")
    if novel_ratio < MIN_NOVEL_TOKEN_RATIO:
        failures.append(f"{nid}: only {novel_ratio:.2%} of the body's distinct content "
                        f"vocabulary is absent from the title "
                        f"(< {MIN_NOVEL_TOKEN_RATIO:.0%}) — reads as a title paraphrase")
    if not cited:
        failures.append(f"{nid}: cites no raw/ source in its body")
    if unresolvable:
        failures.append(f"{nid}: {len(unresolvable)} body link(s) written as "
                        f"[[raw/...]] — these resolve to NOTHING in the graph: "
                        f"{unresolvable[:2]}")
    if bare:
        failures.append(f"{nid}: {len(bare)} raw/ link(s) with no relation "
                        f"statement: {bare[:2]}")
    if cited and current_tier < required_tier:
        failures.append(f"{nid}: classified {meta.get('classification')} while citing "
                        f"{TIERS[required_tier]} sources — a cross-tier derived note")
    return failures


def _pair_result(first: dict, second: dict) -> tuple[dict, str | None]:
    first_shingles = _shingles(first["body"])
    second_shingles = _shingles(second["body"])
    union = first_shingles | second_shingles
    similarity = len(first_shingles & second_shingles) / len(union) if union else 0.0
    pair = {
        "a": first["meta"].get("id"),
        "b": second["meta"].get("id"),
        "similarity": round(similarity, 4),
    }
    failure = None
    if similarity > MAX_PAIR_SIMILARITY:
        failure = (f"{first['meta'].get('id')} ~ {second['meta'].get('id')}: "
                   f"5-gram similarity {similarity:.2%} > "
                   f"{MAX_PAIR_SIMILARITY:.0%} — template reuse")
    return pair, failure


def check_batch(vault: Path, batch: str) -> dict:
    notes = _load_batch_notes(vault, batch)
    tier_of = _load_tiers(vault)
    raw_ids = _load_raw_ids(vault)
    stop = _function_words()
    failures: list[str] = []
    rows: list[dict] = []
    for note in notes:
        row, note_failures = _note_metrics(
            note["meta"], note["body"], note["path"], tier_of, raw_ids, stop
        )
        rows.append(row)
        failures.extend(note_failures)

    pairs: list[dict] = []
    for index, first in enumerate(notes):
        for second in notes[index + 1:]:
            pair, pair_failure = _pair_result(first, second)
            pairs.append(pair)
            if pair_failure:
                failures.append(pair_failure)

    return {
        "batch": batch,
        "notes": len(notes),
        "rows": rows,
        "max_pair_similarity": max((p["similarity"] for p in pairs), default=0.0),
        "pairs": sorted(pairs, key=lambda p: -p["similarity"])[:10],
        "thresholds": {
            "MIN_BODY_CHARS": MIN_BODY_CHARS,
            "MIN_PROPOSITIONS": MIN_PROPOSITIONS,
            "MIN_NOVEL_TOKEN_RATIO": MIN_NOVEL_TOKEN_RATIO,
            "MAX_PAIR_SIMILARITY": MAX_PAIR_SIMILARITY,
        },
        "failures": failures,
        "ok": not failures,
    }
