"""The merge modes of `cos_batch_chunk` — concatenate per-chunk answers into one verdict or category file (batch-2 drain).

Moved verbatim out of `cos_batch_chunk` with the zero-usable-chunks and
row-level-drop doctrine attached; `main` still calls them through the
parent module's namespace, so the invocation contract is unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path


def _chunk_index(chunk_dir: Path) -> int:
    return int(chunk_dir.name.split("-")[-1])


def _chunk_answers(chunks_dir: Path, prefix: str,
                   name: str) -> tuple[list[list], list[int], int]:
    """(arrays, skipped indices, chunks found) for one merge mode.

    Shared by both merges: a chunk whose answer file is missing, unreadable or
    not a JSON array is SKIPPED and its index recorded, never silently dropped.
    """
    chunk_dirs = sorted((p for p in chunks_dir.glob(f"{prefix}-*") if p.is_dir()),
                        key=_chunk_index)
    arrays: list[list] = []
    skipped: list[int] = []
    for cd in chunk_dirs:
        try:
            rows = json.loads((cd / name).read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError(f"{name} is not a JSON array")
        except (OSError, ValueError, json.JSONDecodeError):
            skipped.append(_chunk_index(cd))
            continue
        arrays.append(rows)
    return arrays, skipped, len(chunk_dirs)


def do_merge(chunks_dir: Path, out: Path) -> tuple[dict, int]:
    # A dropped chunk's rows go unjudged; the H4 coverage floor is the backstop.
    arrays, skipped, expected = _chunk_answers(chunks_dir, "chunk", "verdicts.json")
    merged = [r for rows in arrays for r in rows]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    summary = {"chunks_merged": len(arrays),
               "chunks_expected": expected,
               "rows": len(merged), "skipped": skipped}
    # ZERO usable chunks is the leg producing nothing — exit nonzero so the caller
    # dies 9 READ-ONLY. Keyed on chunks_merged, not rows: a chunk that legitimately
    # judged an empty group returns `[]`, which IS a merged chunk (rc 0, a quiet
    # night), while zero parseable chunk files is the leg that produced nothing.
    rc = 0 if arrays else 1
    return summary, rc


def _enumerated_ids(enumeration: Path) -> set[str]:
    """The conversation ids THIS run enumerated, from the driver's own file."""
    data = json.loads(enumeration.read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{enumeration.name} carries no `rows` array")
    return {str(r.get("conversation_id")) for r in rows if isinstance(r, dict)}


def do_merge_category(chunks_dir: Path, out: Path,
                      enumeration: Path | None = None) -> tuple[dict, int]:
    """Concatenate the per-chunk category answers, dropping what cannot be real.

    TWO ROW-LEVEL DROPS, AND NEITHER IS SILENT (run 133):

    * A row naming a conversation THIS RUN DID NOT ENUMERATE is dropped. Run 133
      invented `22aa30e88a5902de` — a short fake among real long EWS ids — and
      `load_categories` refused all 261 rows because of it. The count and a
      sample id are reported so a leg that hallucinates is visible rather than
      quietly trimmed.
    * An EXACT re-emission of the same `(conversation_id, category)` pair is
      collapsed. Chunking makes the run-132 shape more likely, not less: the
      multi-turn reassembly re-emits a boundary object, and now there are N
      boundaries instead of one. `load_categories` already collapses this, but
      only WITHIN one file — doing it here keeps the merged file honest.

    A CONFLICTING duplicate (one id, two different categories) is deliberately
    left in place: that is genuine ambiguity about a real thread, and
    `load_categories` refusing the file is the correct outcome. This function
    never decides which of two answers wins.

    Rows that are not objects, or that carry no usable `conversation_id`, pass
    through UNTOUCHED so the validator still refuses them. Dropping a malformed
    row here would make a broken answer look like a complete one, which is the
    exact class of defect the round-4 `load_categories` review closed.
    """
    in_scope = _enumerated_ids(enumeration) if enumeration is not None else None
    arrays, skipped, expected = _chunk_answers(chunks_dir, "catchunk",
                                               "categories.json")
    merged: list = []
    dropped_not_enumerated = 0
    dropped_sample: str | None = None
    dedup_reemissions = 0
    seen: dict[str, str] = {}
    for rows in arrays:
        for row in rows:
            cid = row.get("conversation_id") if isinstance(row, dict) else None
            cid = cid.strip() if isinstance(cid, str) else ""
            if in_scope is not None and cid and cid not in in_scope:
                dropped_not_enumerated += 1
                if dropped_sample is None:
                    dropped_sample = cid
                continue
            if cid and isinstance(row, dict) and "category" in row:
                # EXACT value equality, not the validator's normalisation: `null`
                # and `""` are different answers and must stay two rows, so the
                # validator can refuse the blank one.
                value = json.dumps(row["category"], sort_keys=True)
                if cid in seen:
                    if seen[cid] == value:
                        dedup_reemissions += 1
                        continue
                else:
                    seen[cid] = value
            merged.append(row)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    summary = {"chunks_merged": len(arrays), "chunks_expected": expected,
               "rows": len(merged), "skipped": skipped,
               "dropped_not_enumerated": dropped_not_enumerated,
               "dropped_sample": dropped_sample,
               "dedup_reemissions": dedup_reemissions}
    # Same rule as the judgment merge: zero usable chunks is the leg producing
    # nothing. For categories that is the SURVIVABLE path — no `--categories`, an
    # ungated draw, `category_gate` reading `not-run` — never a die.
    rc = 0 if arrays else 1
    return summary, rc
