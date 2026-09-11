"""Two batching decisions the nightly used to leave implicit — owner ruling
2026-09-09 ("1 and 2 please"), measured on run 281 (118 threads, 68 minutes).

THE DRAFT LEG GETS ITS OWN SPLIT (`do_split_draft`). Until tonight draft rows
rode the JUDGMENT chunks: five draft calls carried 7 / 3 / 8 / 3 / 9 rows over a
27 KB fixed prompt each, and — the real cost — every candidate COULD land in
one chunk, so the per-message output ceiling (35 drafts, 6200 chars each under
54,400 tokens) became the per-NIGHT cap. 27 threads the judge had marked `act`
were never offered because the cap filled with higher-tier mail first. Split
on their own at <=35 per message, the same 105-row pool is 3 fuller calls
instead of 5 thin ones, and the night cap disappears. This is the seam the
category leg already uses (`do_split_category`); the draft prompt takes no
grounding map, which is what makes the split this small.

THE JUDGMENT ROUNDS ARE FILLED (`round_size`). Five chunks under `CHUNK_PARALLEL=3`
run as rounds of 3 + 2, one slot idle for the whole second round; judgment was 32
of the night's 68 minutes with calls of 18-32 minutes each. Sizing the groups so
the chunk count is a multiple of the parallelism costs nothing and removes the
idle slot. The arithmetic assumes the byte-fit's measured landing size (25 rows
at ~184 KB against the 200 KB ceiling) rather than the authored 50, because 50
is what the halving turns INTO 25.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path


def _write_0600(path: Path, text: str) -> int:
    """Owner-only mode at CREATE time, never a chmod after a 0644 write."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return len(text.encode("utf-8"))


def do_split_draft(batch: Path, out_dir: Path, size: int, *,
                   instruction: Path | None, closing: Path | None,
                   split_batch, renumber_header, compose_draft_prompt) -> dict:
    """`batch-draft.md` -> `dchunk-NN/{batch-draft.md,prompt-draft.txt}`.

    Rows are taken in FILE order — `cos_judge_batches.batch_membership` already
    sorted them tier-first — in consecutive groups of <=`size`. A chunk with no
    rows is never written, so a night with nothing to draft fires no call.
    """
    if size < 1:
        raise ValueError(f"--size must be >= 1, got {size}")
    if not batch.exists():
        raise FileNotFoundError(f"{batch} is missing")
    header, rows = split_batch(batch.read_text(encoding="utf-8"))
    instruction_text = instruction.read_text(encoding="utf-8") if instruction else ""
    closing_text = closing.read_text(encoding="utf-8") if closing else ""
    groups = [rows[i : i + size] for i in range(0, len(rows), size)]
    per_chunk, max_bytes = [], 0
    for k, group in enumerate(groups):
        chunk_dir = out_dir / f"dchunk-{k:02d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        body = (renumber_header("draft", header, len(group))
                + json.dumps(group, indent=1, ensure_ascii=False))
        _write_0600(chunk_dir / "batch-draft.md", body)
        n = _write_0600(chunk_dir / "prompt-draft.txt",
                        compose_draft_prompt(instruction_text, body, closing_text))
        max_bytes = max(max_bytes, n)
        per_chunk.append(len(group))
    return {"draft_chunks": len(groups), "size": size, "rows_total": len(rows),
            "per_chunk": per_chunk, "prompt_bytes_max": max_bytes}


def round_size(rows: int, parallel: int, *, rows_per_call: int = 25,
              max_size: int = 50) -> int:
    """The judgment group size that makes the chunk count a multiple of
    `parallel`, so no round runs with an idle slot.

    118 rows at parallel 3: ceil(118/25) = 5 chunks -> round up to 6 -> 20 rows
    each (run 281 ran 25/25/25/28/18 as 3 + 2). 60 rows: 3 -> 3 -> 20. 250
    rows: 10 -> 12 -> 21. A tiny night (10 rows) becomes 3 chunks of 4, which
    is three short calls in one round rather than one — cheap either way.
    """
    if rows <= 0 or parallel <= 0:
        return max_size
    est = max(1, math.ceil(rows / max(1, rows_per_call)))
    target = parallel * math.ceil(est / parallel)
    return max(1, min(max_size, math.ceil(rows / target)))


def run_split_draft(args, split_batch, compose_draft_prompt) -> int:
    """The `--split-draft` CLI branch, kept here so `cos_batch_chunk.py` stays
    inside its 500-LOC bound. Same exit contract as the other splits."""
    if not (args.batch and args.out_dir):
        print("--split-draft needs --batch and --out-dir", file=sys.stderr)
        return 2
    from cos_batch_chunk_compose import renumber_header              # noqa: PLC0415
    try:
        summary = do_split_draft(
            args.batch, args.out_dir, args.size, instruction=args.instruction,
            closing=args.closing, split_batch=split_batch,
            renumber_header=renumber_header,
            compose_draft_prompt=compose_draft_prompt)
    except (FileNotFoundError, ValueError, json.JSONDecodeError, KeyError) as e:
        print(f"draft split failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0
