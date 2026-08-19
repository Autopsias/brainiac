#!/usr/bin/env python3
"""Split the COS batches into per-chunk copies, and merge the answers back.

WHY THIS EXISTS (STREAM-01 follow-on, run 132). The category leg's stream
reassembly recovered all 258 conversations, but the JUDGMENT model, handed 258
rich verdict rows in one call, DELIBERATED (44k thinking tokens) and answered
with prose and a "pick A/B/C" question instead of the array — 24 verdicts out of
258. The H4 coverage floor caught it and made the night READ-ONLY. The model
handles small batches fine (run 130 one-shot 232, and it itself proposed doing
258 "as a second pass"), so the fix is to split the judgment batch into chunks of
~50 conversations, judge each in its own model call, and concatenate the
verdicts. `cos_judge.py --judge` (H3 dedup, H4 coverage floor) then runs
unchanged over the merged verdicts.

AND THEN THE CATEGORY LEG BECAME THE WEAK ONE (run 133). Chunking fixed the
judgment leg outright — 261 of 261 across 6 chunks — while the CATEGORY leg was
still one call over all ~261 rows, and on that run it HALLUCINATED a
conversation_id (`22aa30e88a5902de`, a short fake beside the real long EWS ids).
`load_categories` refuses a file that stamps a thread this run did not enumerate
— correctly, it is not an answer to this run's batch — so the whole 261-row
answer was thrown away and `category_gate` read `not-run` on a night whose model
had actually done the work. So the category leg is chunked too, and its merge
takes the run's own `enumeration.json`: a row naming a thread the run never
enumerated is DROPPED (and reported) instead of poisoning the file. That is the
one place the two merges differ, and it is deliberate — a hallucinated id is a
row-level defect, and one bad row must not cost the other 260.

Four modes, all trusted host code — no model, no network:

    python3 tools/cos_batch_chunk.py --split --batches-dir <dir> \
        --out-dir <dir> --size N \
        [--grounding <grounding.json> --instruction <part1.txt> \
         --closing <part7.txt> --join-out <grounding-join.json>]
    python3 tools/cos_batch_chunk.py --merge --chunks-dir <dir> --out <verdicts.json>
    python3 tools/cos_batch_chunk.py --split-category --batch <batch-category.md> \
        --out-dir <dir> --size N
    python3 tools/cos_batch_chunk.py --merge-category --chunks-dir <dir> \
        --out <categories.json> [--enumeration <enumeration.json>]

A batch file is `<prose/rules>` + a line containing `BATCH` + a JSON array of row
objects, every row carrying a `conversation_id` (see `cos_judge.py`
`batch_prompts`). Split reads the conversation_id ORDER from `batch-triage.md`
(the full-population file), partitions it into consecutive groups of <=N, and
writes one `chunk-<k>/batch-<type>.md` per group per type — the source file's
header verbatim, then only that group's rows. `--split-category` does the same
over the ONE `batch-category.md` file, into `catchunk-<k>/batch-category.md`.
Merge concatenates every `chunk-*/verdicts.json` (or `catchunk-*/categories.json`)
that parses; a missing or unparseable one is SKIPPED (its rows go unanswered; the
downstream H4 coverage floor, or the category validator, catches the shortfall)
but REPORTED, never silent.

Fails closed: split dies nonzero if `batch-triage.md` is missing/unparseable or a
source file lacks a BATCH line; merge with ZERO usable chunks writes `[]` and
exits nonzero, which is the caller's existing die-9 READ-ONLY path (for
categories: the survivable "no `--categories`, ungated draw, gate `not-run`"
path). `--merge-category` with an unreadable `--enumeration` is ALSO nonzero and
writes nothing: without the enumeration it cannot tell a hallucinated id from a
real one, and guessing is the failure this mode exists to end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_batch_chunk_compose import ChunkSources  # noqa: E402
from cos_batch_chunk_merge import (  # noqa: E402,F401  merge modes, batch-2 drain
    _chunk_answers, _chunk_index, _enumerated_ids, do_merge,
    do_merge_category)
from cos_batch_chunk_write import fit, write_chunks, write_join  # noqa: E402

# The four judgment batches. batch-category.md is the PRE-DRAW category leg and
# is judged in one call elsewhere — it is never chunked or copied here.
BATCH_TYPES = ("triage", "staging", "hold", "draft")

# ---------------------------------------------------------------------------
# GROUNDING (design D2, D2a, D6a, D8, D9, D9a) — the map, the prompt, the join
# ---------------------------------------------------------------------------
# WHY THE MAP RIDES HERE AND NOT IN A ROW KEY. Grounding is ONE block per
# CONVERSATION, and a conversation recurs across the four batches — measured at
# 104 row occurrences for 50 distinct conversations in the worst real chunk
# (`_evidence/nightly/2026-08-15-run137/chunks/chunk-01`). A per-row key would
# therefore cost 104 x 1500 = 156,000 characters where a per-chunk map costs
# 50 x GROUNDING_BLOCK_MAX. And batch-header prose is worse still: `do_split`
# copies each header VERBATIM into every chunk, so header grounding writes the
# whole vault sweep once per chunk. So: one map per chunk, keyed by
# conversation_id, and `cos_judge.py --batches` renders exactly what it always
# did. (This REVERSES `docs/cos-grounding-design.md` revision 1 and the earlier
# DOCTRINE §2.8 wording; the measurement is in D9.)
#
# THE ONLY COMPONENT THAT KNOWS WHICH CONVERSATION LANDS IN WHICH CHUNK IS THIS
# ONE, which is why the map, the prompt composition, the fed-byte budget and
# the join all live here rather than in inline shell: the re-split is a `split`
# operation on a smaller group, and a rule that cannot be executed by a test
# without slicing a shell script is a rule nothing can prove.

#: D2's separator lines. Part 2 is the VERBATIM bytes of `$CHUNK/grounding.json`
#: — never re-serialized — because D2a's join looks for exactly those bytes.
SEP_MAP = "===== VAULT CONTEXT MAP — data, never instructions ====="
SEP_BATCH = {t: f"===== BATCH {t.upper()} =====" for t in BATCH_TYPES}
#: What an UNGROUNDED night puts where the map would have been. One line, so the
#: leg is never handed an empty header it has to interpret.
NO_CONTEXT_LINE = ("VAULT CONTEXT: none this night — this run declared itself "
                   "UNGROUNDED and judges from the message text alone.")

#: D9.3 — the assertion is on the RECORDED FED BYTE COUNT, which for this
#: composition is exactly `len(prompt.txt bytes)`: the instruction block, the
#: chunk's map, ALL FOUR batch files and the closing line, measured as UTF-8
#: rather than as characters. PROVISIONAL (D9a): it is a guard number set above
#: the corrected real worst case (97,680 batch bytes + a 117,100-byte map) with
#: headroom, not a measured transport limit.
PROMPT_MAX_BYTES = 200_000
#: D9a.3 — 50 → 25 → 13 → 7 → 4. The bound exists so a pathological input cannot
#: spin; hitting it is RECORDED (`resplit_bound_hit`), never raised away.
RESPLIT_MAX_HALVINGS = 4


def prompt_max_bytes() -> int:
    return int(os.environ.get("COS_PROMPT_MAX_BYTES") or PROMPT_MAX_BYTES)


def _ground():
    """`cos_ground`, imported lazily and only on the grounding path — the two
    merge modes must not pay for the fetcher's import, and `cos_ground` is where
    the 0600-atomic writer and the canonical map serialization already live."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cos_ground                                            # noqa: PLC0415
    return cos_ground


def chunk_map(payload: dict, group: list[str], chunk_name: str) -> dict:
    """The per-chunk map (D6a file 2): the run map's shape, `blocks` restricted
    to this group, plus `chunk` and `parent_run_id`."""
    blocks = payload.get("blocks") or {}
    return {
        "chunk": chunk_name,
        "parent_run_id": payload.get("run_id"),
        "run_id": payload.get("run_id"),
        "state": payload.get("state"),
        "reason": payload.get("reason", ""),
        "blocks": {cid: blocks[cid] for cid in group if cid in blocks},
    }


def compose_prompt(instruction: str, map_text: str | None,
                   bodies: dict[str, str], closing: str) -> str:
    """`$CHUNK/prompt.txt`, in D2's fixed order and with nothing else in it.

    1 instruction · 2 the chunk's map (verbatim) · 3-6 the four batches in
    `BATCH_TYPES` order · 7 the closing instruction. `map_text` is `None` on an
    UNGROUNDED night, and then part 2 is one honest line rather than an empty
    header — never a silently short prompt.
    """
    parts = [instruction.rstrip("\n"), ""]
    if map_text is None:
        parts += [NO_CONTEXT_LINE, ""]
    else:
        # VERBATIM. `map_text` is `cos_ground.map_text(...)`'s own output and is
        # copied in unchanged — not re-indented, not re-encoded.
        parts += [SEP_MAP, map_text.rstrip("\n"), ""]
    for t in BATCH_TYPES:
        parts += [SEP_BATCH[t], bodies[t].rstrip("\n"), ""]
    parts += [closing.strip(), ""]
    return "\n".join(parts)


def join_chunk(chunk_name: str, prompt_text: str, cmap: dict | None,
               map_text: str | None, batch_ids: set[str],
               required: set[str]) -> dict:
    """D2a: join the MAP to the COMPOSED PROMPT, on the block TEXT.

    The id join of the design's revision 3 is DELETED, not kept beside this one:
    every grounded id is ALREADY in `prompt.txt` as a batch row key (D13's union
    guarantee), so an id assertion passed with part 2 entirely absent — it could
    not fail for the thing it was named for.

    Two assertions do the work instead: the map's WHOLE BYTES occur as a literal
    substring (`map_bytes_found`), and each `ok` block's
    `json.dumps(text, ensure_ascii=False)` needle — the JSON string literal
    INCLUDING its surrounding quotes, which is exactly what `map_text` wrote —
    occurs too. Digests, never text: this artifact is not on D14's sink
    allowlist, so it records WHAT was checked without making a fourth copy of
    MNPI prose.
    """
    blocks = (cmap or {}).get("blocks") or {}
    expected = sorted(batch_ids & required)
    with_text = [cid for cid, b in blocks.items()
                 if isinstance(b, dict) and b.get("status") == "ok"]
    found, missing_text, digests = [], [], {}
    for cid in with_text:
        needle = json.dumps(blocks[cid]["text"], ensure_ascii=False)
        digests[cid] = hashlib.sha256(needle.encode("utf-8")).hexdigest()
        (found if needle in prompt_text else missing_text).append(cid)
    return {
        "chunk": chunk_name,
        "expected": len(expected),
        "blocks": len(blocks),
        # THE ABSENT-MAP CASE, NAMED (design D2a). `map_bytes_found` is
        # UNDEFINED when there is no map — there are no bytes to look for, so
        # the assertion has no subject. It is `null`, `blocks` is 0, and the run
        # FAILs E10 on the UNION condition (the denominator), never on this one.
        # Nobody should later "fix" the delivery condition to fire on a null.
        "map_bytes_found": (map_text in prompt_text) if map_text is not None
                           else None,
        "with_text": len(with_text),
        "no_text": len(blocks) - len(with_text),
        "text_found_in_prompt": len(found),
        "text_digests": digests,
        "missing_text": sorted(missing_text),
        "missing": sorted(set(expected) - set(blocks)),
        "unexpected": sorted(set(blocks) - set(expected)),
        # THE IDS, so a consumer can RECOMPUTE what this file only declares.
        # E10 used to read `required_covered_by_chunks` — a number this same
        # producer wrote about itself — and a join declaring two covered ids
        # with `chunks: []` returned no problems at all (review 2026-08-15).
        # Ids, never text: `covered_ids` is the block key set and `with_text_ids`
        # the subset carrying vault prose, both of which the declaration
        # (`_cos_grounding_<run>.json`) already publishes.
        "covered_ids": sorted(blocks),
        "with_text_ids": sorted(with_text),
    }


def short_chunks(join: dict) -> list[str]:
    """`brain.cos_echecks.short_chunks` — the ONE delivery predicate, imported.

    Not re-implemented here, and not defined here either: the gate that reads
    this file runs from an INSTALLED engine with no `tools/` tree, so the single
    copy has to live where both ends can reach it. `tools/` already depends on
    `brain`; nothing goes the other way. See the docstring there for the null
    (`map_bytes_found: null`, absent map) decision, which is now made once.
    """
    from brain import cos_echecks                                # noqa: PLC0415
    return cos_echecks.short_chunks(join)


def split_batch(text: str) -> tuple[str, list[dict]]:
    """(header, rows) for a batch file — header up to AND INCLUDING the BATCH line.

    The JSON array follows the first line containing `BATCH` (uppercase, which in
    a rendered batch file appears only on that marker line). Raises if there is no
    BATCH line or the trailing text is not a JSON array of objects.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if "BATCH" in line:
            header = "".join(lines[: i + 1])
            body = "".join(lines[i + 1 :])
            rows = json.loads(body)
            if not isinstance(rows, list):
                raise ValueError("batch body is not a JSON array")
            return header, rows
    raise ValueError("no line containing BATCH")


def do_split(batches_dir: Path, out_dir: Path, size: int, *,
             grounding: Path | None = None, instruction: Path | None = None,
             closing: Path | None = None, join_out: Path | None = None,
             prompt_max: int | None = None) -> dict:
    if size < 1:
        raise ValueError(f"--size must be >= 1, got {size}")
    prompt_max = prompt_max if prompt_max is not None else prompt_max_bytes()

    # The full population and its order come from triage — every conversation is
    # in it; staging/hold/draft are subsets. A missing/unparseable triage is
    # fatal: with no population there are no groups to draw.
    triage_path = batches_dir / "batch-triage.md"
    if not triage_path.exists():
        raise FileNotFoundError(f"{triage_path} is missing")
    _, triage_rows = split_batch(triage_path.read_text(encoding="utf-8"))
    order = [r["conversation_id"] for r in triage_rows]

    # Consecutive groups of <=N, order preserved.
    groups = [order[i : i + size] for i in range(0, len(order), size)] or [[]]

    # Parse each source file ONCE (header kept verbatim, rows kept in file order).
    parsed: dict[str, tuple[str, list[dict]]] = {}
    for t in BATCH_TYPES:
        path = batches_dir / f"batch-{t}.md"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing")
        parsed[t] = split_batch(path.read_text(encoding="utf-8"))

    # THE GROUNDING MAP RIDES ONLY A GROUNDED NIGHT. An `ungrounded` run makes no
    # claim to have delivered context, so it ships none and says so in one line
    # (D2): a half-delivered map behind the word "ungrounded" is the same
    # unauditable state the two null statuses exist to prevent.
    # A GROUNDING FAILURE IS A LABEL, NEVER A DEAD NIGHT (D5). The fetch is
    # best-effort and the nightly passes `--grounding` unconditionally, so an
    # absent or unreadable map must compose an UNGROUNDED prompt — not raise,
    # which would `die 7` the split and turn "judgment was not grounded
    # tonight" into "the night did not judge".
    payload = None
    if grounding is not None:
        try:
            payload = json.loads(grounding.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
    grounded = isinstance(payload, dict) and payload.get("state") == "grounded"
    required = set((payload or {}).get("required") or []) \
        if isinstance(payload, dict) else set()
    instruction_text = (instruction.read_text(encoding="utf-8")
                        if instruction else None)
    closing_text = closing.read_text(encoding="utf-8") if closing else ""

    src = ChunkSources(parsed=parsed, payload=payload, grounded=grounded,
                       required=required, instruction=instruction_text,
                       closing=closing_text, ground=_ground,
                       chunk_map=chunk_map, compose_prompt=compose_prompt,
                       batch_types=BATCH_TYPES)

    if instruction_text is None:
        placed = [(g, 0, False, False) for g in groups]
    else:
        placed = [rec for g in groups
                  for rec in fit(src, g, 0, prompt_max, RESPLIT_MAX_HALVINGS)]

    per_chunk, chunk_records, join_records, covered_by_chunks = write_chunks(
        src, placed, out_dir, split_batch, join_chunk)

    summary = {"chunks": len(placed), "size": size,
               "rows_total": len(order), "per_chunk": per_chunk,
               "grounded": grounded,
               "oversize_chunks": sum(1 for r in chunk_records
                                      if r.get("oversize")),
               "resplit_bound_hits": sum(1 for r in chunk_records
                                         if r.get("resplit_bound_hit")),
               "prompt_bytes_max": max((r.get("prompt_bytes", 0)
                                        for r in chunk_records), default=0)}

    if instruction_text is not None and join_out is not None:
        join_ok, covered = write_join(src, join_out, join_records,
                                      covered_by_chunks, short_chunks)
        summary["join_ok"] = join_ok
        summary["required_covered_by_chunks"] = covered

    return summary


def do_split_category(batch: Path, out_dir: Path, size: int) -> dict:
    """The SAME split over the one `batch-category.md` file (run 133).

    There is no separate population file here — the category batch IS the whole
    enumeration, one row per conversation — so the rows are partitioned in file
    order and each group is written back under its own source header.
    """
    if size < 1:
        raise ValueError(f"--size must be >= 1, got {size}")
    if not batch.exists():
        raise FileNotFoundError(f"{batch} is missing")
    header, rows = split_batch(batch.read_text(encoding="utf-8"))
    groups = [rows[i : i + size] for i in range(0, len(rows), size)] or [[]]
    per_chunk = []
    for k, group in enumerate(groups):
        chunk_dir = out_dir / f"catchunk-{k:02d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps(group, indent=1, ensure_ascii=False)
        (chunk_dir / "batch-category.md").write_text(header + body, encoding="utf-8")
        per_chunk.append(len(group))
    return {"chunks": len(groups), "size": size,
            "rows_total": len(rows), "per_chunk": per_chunk}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", action="store_true")
    p.add_argument("--merge", action="store_true")
    p.add_argument("--split-category", action="store_true")
    p.add_argument("--merge-category", action="store_true")
    p.add_argument("--batches-dir", type=Path)
    p.add_argument("--batch", type=Path)
    p.add_argument("--out-dir", type=Path)
    p.add_argument("--chunks-dir", type=Path)
    p.add_argument("--enumeration", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--size", type=int)
    p.add_argument("--grounding", type=Path,
                   help="the run's `grounding.json`; absent, the chunker writes "
                        "no per-chunk map and composes an ungrounded prompt")
    p.add_argument("--instruction", type=Path,
                   help="part 1 of `prompt.txt`; absent, no prompt is composed "
                        "and no join is written")
    p.add_argument("--closing", type=Path, help="part 7 of `prompt.txt`")
    p.add_argument("--join-out", type=Path,
                   help="where D2a's join record is written (digests, no text)")
    p.add_argument("--prompt-max-bytes", type=int, default=None)
    args = p.parse_args(argv)

    modes = [args.split, args.merge, args.split_category, args.merge_category]
    if sum(1 for m in modes if m) != 1:
        print("choose exactly one of --split / --merge / --split-category / "
              "--merge-category", file=sys.stderr)
        return 2
    if args.size is None:
        env = ("COS_CATEGORY_CHUNK_SIZE" if args.split_category
               else "COS_JUDGE_CHUNK_SIZE")
        args.size = int(os.environ.get(env) or 50)

    if args.split or args.split_category:
        need = "--batch" if args.split_category else "--batches-dir"
        source = args.batch if args.split_category else args.batches_dir
        if not (source and args.out_dir):
            print(f"this split needs {need} and --out-dir", file=sys.stderr)
            return 2
        try:
            summary = (do_split_category(source, args.out_dir, args.size)
                       if args.split_category
                       else do_split(source, args.out_dir, args.size,
                                     grounding=args.grounding,
                                     instruction=args.instruction,
                                     closing=args.closing,
                                     join_out=args.join_out,
                                     prompt_max=args.prompt_max_bytes))
        except (FileNotFoundError, ValueError, json.JSONDecodeError, KeyError) as e:
            print(f"split failed: {e}", file=sys.stderr)
            return 1
        print(json.dumps(summary))
        return 0

    if not (args.chunks_dir and args.out):
        print("this merge needs --chunks-dir and --out", file=sys.stderr)
        return 2
    if args.merge_category:
        # An unreadable enumeration is fatal BEFORE anything is written: without
        # it a hallucinated id cannot be told from a real one, and the whole
        # point of this mode is not to guess.
        try:
            summary, rc = do_merge_category(args.chunks_dir, args.out,
                                            args.enumeration)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"category merge failed: {e}", file=sys.stderr)
            return 1
    else:
        summary, rc = do_merge(args.chunks_dir, args.out)
    print(json.dumps(summary))
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
