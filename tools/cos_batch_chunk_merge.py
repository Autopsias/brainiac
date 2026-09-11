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


def graft_drafts(chunks_dir: Path, merged: list, prefix: str = "chunk") -> dict:
    """DRAFT-01: copy each `draft` the SECOND leg wrote onto its triage verdict.

    The draft job runs in its own model call (`prompt-draft.txt` →
    `verdicts-draft.json`) because sharing one call with the triage job silences
    it — measured 2026-08-27, 0 drafts across 30 slots while the triage half
    answered in full. Everything downstream still reads ONE verdict per
    conversation, so the two answers are joined here, on conversation_id.

    A draft NEVER overwrites one the triage leg already produced, and a draft
    whose conversation has no triage verdict is DROPPED, not invented as a bare
    row: a conversation the judgment leg never judged has no bucket, no tier and
    no evidence span, and a row carrying only a draft would walk into the judge
    wearing a verdict it never received.

    `needs_owner` RIDES THE SAME JOIN (owner ruling 2026-08-28). The leg's
    answer for a row it did not draft is the WORD saying why, and until now
    this function threw that row away on `not row.get("draft")` — so the one
    thing the ruling asks for could never reach the sheet even once the model
    returned it. `orphaned` now counts a row that carried EITHER, because both
    are answers about a conversation this run did not judge.
    """
    by_id = {}
    for row in merged:
        if isinstance(row, dict) and row.get("conversation_id") is not None:
            by_id.setdefault(str(row["conversation_id"]), row)
    grafted, orphaned, chunks, flagged = 0, 0, 0, 0
    for cd in sorted(chunks_dir.glob(f"{prefix}-*")):
        try:
            rows = json.loads((cd / "verdicts-draft.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        chunks += 1
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            has_draft, has_flag = bool(row.get("draft")), bool(row.get("needs_owner"))
            if not (has_draft or has_flag):
                continue
            target = by_id.get(str(row.get("conversation_id")))
            if target is None:
                orphaned += 1
                continue
            if has_draft and not target.get("draft"):
                target["draft"] = row["draft"]
                grafted += 1
            if has_flag and not target.get("needs_owner"):
                target["needs_owner"] = row["needs_owner"]
                flagged += 1
    return {"draft_chunks": chunks, "drafts_grafted": grafted,
            "drafts_orphaned": orphaned, "needs_owner_flagged": flagged}


def sum_usage(*dirs: tuple[Path, str]) -> dict:
    """Add up the per-call receipts `cos_model_answer` writes beside each
    answer (`<answer>.usage.json`). Numbers only; a missing or unreadable
    receipt is counted as a call with no usage, never as an error — the night
    reporting what it spent must not be able to stop the night."""
    tot = {"calls": 0, "with_usage": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
           "cost_usd": 0.0, "duration_ms": 0}
    models: set[str] = set()
    for d, prefix in dirs:
        if not (d and d.is_dir()):
            continue
        for cd in sorted(d.glob(f"{prefix}-*")):
            tot["calls"] += 1
            try:
                u = json.loads(next(cd.glob("*.usage.json")).read_text(encoding="utf-8"))
            except (StopIteration, OSError, ValueError):
                continue
            if not isinstance(u, dict):
                continue
            tot["with_usage"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                      "cache_creation_input_tokens", "duration_ms"):
                if isinstance(u.get(k), int):
                    tot[k] += u[k]
            if isinstance(u.get("cost_usd"), (int, float)):
                tot["cost_usd"] = round(tot["cost_usd"] + float(u["cost_usd"]), 4)
            for m in u.get("models") or []:
                if isinstance(m, str):
                    models.add(m)
    tot["models"] = sorted(models)
    return tot


def do_merge(chunks_dir: Path, out: Path,
             draft_dir: Path | None = None) -> tuple[dict, int]:
    # A dropped chunk's rows go unjudged; the H4 coverage floor is the backstop.
    arrays, skipped, expected = _chunk_answers(chunks_dir, "chunk", "verdicts.json")
    merged = [r for rows in arrays for r in rows]
    # DRAFTS COME FROM THEIR OWN SPLIT when the nightly made one (2026-09-09);
    # an older evidence dir with drafts riding the judgment chunks still merges.
    if draft_dir is not None and draft_dir.is_dir():
        drafts = graft_drafts(draft_dir, merged, "dchunk")
    else:
        drafts = graft_drafts(chunks_dir, merged)
    usage = sum_usage((chunks_dir, "chunk"), (draft_dir, "dchunk"))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    summary = {"chunks_merged": len(arrays),
               "chunks_expected": expected,
               "rows": len(merged), "skipped": skipped, **drafts,
               "usage": usage}
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
