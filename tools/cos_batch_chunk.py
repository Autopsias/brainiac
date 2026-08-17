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

    def bodies_for(group: list[str]) -> dict[str, str]:
        gset = set(group)
        out = {}
        for t in BATCH_TYPES:
            header, rows = parsed[t]
            # Rows this file holds for this group, in the FILE's own order. A type
            # with none still gets a file with `[]`, so every batch is present.
            # Staging text/offset rows are per-row self-contained, so slicing by
            # id keeps each span valid — no renumber.
            sub = [r for r in rows if r.get("conversation_id") in gset]
            out[t] = header + json.dumps(sub, indent=1, ensure_ascii=False)
        return out

    def map_text_for(group: list[str], chunk_name: str) -> tuple[dict, str] | None:
        if not grounded:
            return None
        cmap = chunk_map(payload, group, chunk_name)
        return cmap, _ground().map_text(cmap)

    def written_map(chunk_dir: Path, group: list[str],
                    chunk_name: str) -> tuple[dict, str] | None:
        """Write the chunk's map, then READ IT BACK — and compose part 2 from
        the bytes that are actually on disk.

        THE PROBE THAT FOUND THIS. Composing from the in-memory string made the
        writer and the composer the same expression, so D2a's join compared a
        value against itself: a serializer that wrote something else to disk
        would still have joined clean. Reading back makes "the map arrived byte
        for byte" a claim about the FILE the design names, which is the only
        version of that claim worth making."""
        mt = map_text_for(group, chunk_name)
        if mt is None:
            return None
        # 0600 + atomic, through the SAME writer the run map uses.
        path = _ground().write_text_0600(chunk_dir / "grounding.json", mt[1])
        return mt[0], path.read_text(encoding="utf-8")

    # THE MEASUREMENT USES A PLACEHOLDER NAME, and the bound is stated rather
    # than hidden: `chunk-XX` is the same width as every real `chunk-NN` up to
    # 99 chunks, so the re-split DECISION is exact there and understates by one
    # byte per extra digit beyond that. The RECORDED `prompt_bytes` is always
    # re-measured on the written text, so the artifact is exact either way.
    def compose(group: list[str], chunk_name: str = "chunk-XX") -> str:
        mt = map_text_for(group, chunk_name)
        return compose_prompt(instruction_text or "", mt[1] if mt else None,
                              bodies_for(group), closing_text)

    def fed_bytes(group: list[str]) -> int:
        """THE quantity D9 asserts on: the exact UTF-8 size of everything the leg
        is fed in one call — instruction + map + ALL FOUR batch files + closing.
        Bytes, not characters: a multi-byte body measures what it costs."""
        return len(compose(group).encode("utf-8"))

    def fit(group: list[str], depth: int) -> list[tuple[list[str], int, bool, bool]]:
        """(group, bytes, oversize, resplit_bound_hit) after D9a's bounded halving.

        NEVER TRUNCATED AND NEVER DROPPED, at either terminal: a truncated prompt
        is a short batch wearing a full one's row count, and a row the model
        never sees is a coverage hole. A single oversized row means one document
        blew the body budget; a MULTI-ROW group still over at the bound means the
        arithmetic is wrong — the ceiling and the per-row budget disagree — and
        it is the ceiling that must be revisited, not the input. So the two are
        distinguished by `resplit_bound_hit` rather than merged.
        """
        n = fed_bytes(group)
        if n <= prompt_max or len(group) <= 1 or depth >= RESPLIT_MAX_HALVINGS:
            over = n > prompt_max
            return [(group, n, over, over and len(group) > 1
                     and depth >= RESPLIT_MAX_HALVINGS)]
        mid = (len(group) + 1) // 2          # 50 → 25 → 13 → 7 → 4
        return fit(group[:mid], depth + 1) + fit(group[mid:], depth + 1)

    if instruction_text is None:
        placed = [(g, 0, False, False) for g in groups]
    else:
        placed = [rec for g in groups for rec in fit(g, 0)]

    per_chunk, chunk_records, join_records = [], [], []
    covered_by_chunks: set[str] = set()
    for k, (group, nbytes, oversize, bound_hit) in enumerate(placed):
        # ponytail: :02d sorts lexically for <=99 chunks (4950 rows at size 50);
        # widen the pad if a run ever exceeds that. Numbering is re-derived from
        # the FINAL group list, so a re-split leaves `chunk-NN` contiguous.
        chunk_name = f"chunk-{k:02d}"
        chunk_dir = out_dir / chunk_name
        chunk_dir.mkdir(parents=True, exist_ok=True)
        bodies = bodies_for(group)
        for t in BATCH_TYPES:
            (chunk_dir / f"batch-{t}.md").write_text(bodies[t], encoding="utf-8")
        mt = written_map(chunk_dir, group, chunk_name)
        if mt is not None:
            covered_by_chunks |= set(mt[0]["blocks"])
        per_chunk.append(len(group))
        rec = {"chunk": chunk_name, "rows": len(group)}
        if instruction_text is not None:
            prompt_text = compose_prompt(instruction_text,
                                         mt[1] if mt else None, bodies,
                                         closing_text)
            # 0600, THROUGH THE SAME WRITER THE MAPS USE. `prompt.txt` is the
            # single most concentrated artifact this pipeline produces — the
            # whole chunk's vault context plus every mail body it will judge —
            # and its own mode is the second belt behind the run directory's
            # 0700 (`cos_nightly.sh`). A default `write_text` here would be
            # umask-derived 0644.
            _ground().write_text_0600(chunk_dir / "prompt.txt", prompt_text)
            nbytes = len(prompt_text.encode("utf-8"))
            _ground().write_text_0600(chunk_dir / "prompt.bytes", f"{nbytes}\n")
            rec.update(prompt_bytes=nbytes, oversize=oversize,
                       resplit_bound_hit=bound_hit)
            batch_ids: set[str] = set()
            for t in BATCH_TYPES:
                _h, rows_t = split_batch(bodies[t])
                batch_ids |= {r.get("conversation_id") for r in rows_t
                              if isinstance(r, dict)}
            join_records.append(join_chunk(chunk_name, prompt_text,
                                           mt[0] if mt else None,
                                           mt[1] if mt else None,
                                           batch_ids, required))
        chunk_records.append(rec)

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
        # THE DENOMINATOR JOIN, and it is between two INDEPENDENTLY produced
        # artifacts: `required` comes from the fetcher's frozen map, and
        # `batch_ids` comes from the batch files `cos_judge.py --batches`
        # rendered. D13's guarantee is that they are the same set; a fetcher
        # that under-required is exactly what scores a clean `grounded` when
        # nothing joins its own word to anything else.
        all_batch_ids: set[str] = set()
        for t in BATCH_TYPES:
            all_batch_ids |= {r.get("conversation_id") for r in parsed[t][1]
                              if isinstance(r, dict)}
        join = {"run_id": (payload or {}).get("run_id"),
                "chunks": join_records,
                "required": len(required),
                "required_covered_by_chunks": len(covered_by_chunks),
                "required_not_in_batches": sorted(required - all_batch_ids),
                "batch_ids_not_required": sorted(all_batch_ids - required)}
        # ONE predicate, called by the producer, `cos_echecks` and the shell.
        bad = short_chunks(join)
        # `ok` is a claim about DELIVERY, so an ungrounded night — which claims
        # no delivery — is never `ok` here and E10 never reads this file for one.
        join["ok"] = (grounded and not bad
                      and not join["required_not_in_batches"]
                      and join["required_covered_by_chunks"] >= join["required"])
        _ground().write_text_0600(join_out, json.dumps(join, indent=2) + "\n")
        summary["join_ok"] = join["ok"]
        summary["required_covered_by_chunks"] = join["required_covered_by_chunks"]

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
