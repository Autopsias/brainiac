"""Project a parsed model answer onto the closed schema, and bind its ids.

Split out of `cos_model_answer.py` (2026-08-28). That module sat one change
away from its 500-line bound; DRAFT-01's `--allow-empty` flag pushed it over,
and this is the seam it already had — everything above PARSES an envelope into
rows, everything here decides which of those rows may be written and with what
in them. Nothing here reads a stream event, and nothing there refuses a field.

Same contract as every other sub-step in this family: each function takes the
PARENT module's namespace as `cma`, so a test that rebinds a constant on
`cos_model_answer` still steers the projection. `cos_model_answer` re-exports
all three names with their original signatures, so no caller changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from cos_model_answer_overlap import block_shingles, overlap_hit
from cos_model_answer_schema import _strings, blank_field, project_keys


def project_row(row: dict[str, Any], block_text: str | None,
                own_row_text: str, stats: dict[str, Any],
                allowed: frozenset[str], cma) -> dict[str, Any] | None:
    """One row, projected onto the closed schema. `None` means REFUSED.

    Never truncated. Refused for a bad shape, an unenumerated id or an oversize
    row; the OVERLAP rule BLANKS its field and keeps the verdict instead, bar
    `conversation_id` (a forged join key) — why in `blank_field`.

    The per-key walk (unknown-key counting, the `draft`/`evidence_span`
    sub-objects, the declared-type and per-field ceilings) lives in
    `cos_model_answer_schema.project_keys`; the aggregate row bound stays HERE,
    read off this module's own `MAX_ROW_BYTES` at call time, so a test that
    rebinds it sees the projection honour the new ceiling.
    """
    out = project_keys(row, allowed, stats)
    if out is None:
        return None

    # THE OVERLAP TEST, on the projected row and against THIS conversation's own
    # block only (rule 3). Cross-conversation matching would fire on boilerplate
    # and says less.
    if block_text:
        uniq = block_shingles(block_text, own_row_text)
        for label, text in _strings(out):
            if not overlap_hit(text, uniq):
                continue
            hit = stats["refused_grounding_overlap" if label == "conversation_id"
                        else "blanked_grounding_overlap"]
            hit[label] = hit.get(label, 0) + 1
            if label == "conversation_id":
                return None
            blank_field(out, label)
    # THE ROW'S OWN SERIALIZED SIZE, last, on exactly the bytes that would be
    # written. Per-field ceilings bound no row: ~16 string fields at their caps
    # is still tens of kilobytes per row, and the row is what `--out` serializes.
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > cma.MAX_ROW_BYTES:
        stats["refused_oversize_row"] += 1
        return None
    return out


def project(rows: list[Any], blocks: dict[str, Any],
            own_row_text: dict[str, str], allowed: frozenset[str], cma,
            *, enumerated: set[str]) -> tuple[list[Any], dict[str, Any]]:
    """The whole answer, projected. Counts everything it drops or refuses.

    `enumerated` is the HOST's own id set, read off the rendered batch files. A
    row whose `conversation_id` is not in it is REFUSED before projection: an id
    the host did not enumerate is not an id, it is model-authored text sitting in
    a field whose declared type happens to be `str`. `judge_night` already binds
    verdicts to the enumerated set, but that is two files downstream of
    `verdicts.json` — the leak the review measured was at the WRITE, not at the
    judgment.

    IT IS MANDATORY, AND KEYWORD-ONLY (review 2026-08-15, HIGH). The round that
    added it made it `enumerated: set[str] | None = None` with enforcement under
    `if enumerated is not None`, so the CRITICAL fix was INERT on every call that
    omitted it — both nightly legs happened to pass `--batches-dir`, which made
    it a latent fail-open rather than a live leak, and "a guard that is a no-op
    by default" is the exact shape this delta exists to remove. There is no
    accepted mode with no enumeration: an empty or missing set is a REFUSAL,
    because a chunk whose batches enumerate nothing has nothing to judge, and
    projecting its answer against an empty binding would admit every
    model-authored id instead of none.
    """
    if not enumerated:
        raise ValueError(
            "the host supplied no enumerated conversation id set — a projection "
            "with nothing to bind `conversation_id` against would keep whatever "
            "id the model wrote, which is the fail-open the binding exists to "
            "close")
    stats: dict[str, Any] = {"rows_in": len(rows), "rows_out": 0,
                             "dropped_unknown_keys": {},
                             "refused_grounding_overlap": {},
                             "blanked_grounding_overlap": {},
                             "refused_oversize_field": {},
                             "refused_oversize_row": 0,
                             "refused_unenumerated_id": 0,
                             "refused_shape": 0,
                             "refused_ids": []}  # why: projection_refused_ids
    out: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("conversation_id") or "")
        if cid not in enumerated:
            stats["refused_unenumerated_id"] += 1
            continue
        entry = blocks.get(cid) or {}
        block_text = entry.get("text") if isinstance(entry, dict) else None
        projected = project_row(row, block_text, own_row_text.get(cid, ""),
                                stats, allowed, cma)
        if projected is not None:
            out.append(projected)
        else:
            stats["refused_ids"].append(cid)
    stats["rows_out"] = len(out)
    return out, stats


def own_row_texts(chunk_dir: Path) -> dict[str, str]:
    """Per conversation, the text of its OWN batch row — what step 4 subtracts.

    `subject`, `sender` and (staging/draft) `text`: the values a verdict may
    legitimately echo, and which appear on BOTH sides of the comparison.

    ITS KEY SET IS ALSO THE HOST ENUMERATION `project` binds ids against, which
    is why it globs `batch-*.md` rather than iterating `BATCH_TYPES`: the
    CATEGORY leg's chunk holds one `batch-category.md` and no judgment batch, so
    a `BATCH_TYPES` loop enumerated nothing there and the binding would have been
    silently inert on half the calls.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cos_batch_chunk as cbc                                # noqa: PLC0415
    out: dict[str, list[str]] = {}
    for path in sorted(chunk_dir.glob("batch-*.md")):
        try:
            _h, rows = cbc.split_batch(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            cid = str(r.get("conversation_id") or "")
            if not cid:
                # An id-less batch row cannot enumerate anything, and admitting
                # `""` would let a row with no `conversation_id` bind.
                continue
            bucket = out.setdefault(cid, [])
            for key in ("subject", "sender", "text"):
                v = r.get(key)
                if isinstance(v, str):
                    bucket.append(v)
    return {cid: "\n".join(parts) for cid, parts in out.items()}
