"""The split mode's output stage: byte-budget placement, chunk files, the
grounding join.

``cos_batch_chunk.do_split`` keeps its name, signature and module; what lives
here is everything a split WRITES — the D9a bounded-halving placement that
decides the chunk groups, the per-chunk files under ``chunk-NN/``, and the
D2a join record with its delivery verdict. The parent callables the stage
needs (``split_batch``, ``join_chunk``, ``short_chunks``, the lazy ``_ground``
fetcher) arrive as parameters, so this module never imports
``cos_batch_chunk`` and a monkeypatched parent attribute keeps working.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from cos_batch_chunk_compose import ChunkSources, bodies_for, compose, written_map


def fed_bytes(src: ChunkSources, group: list[str]) -> int:
    """THE quantity D9 asserts on: the exact UTF-8 size of everything the leg
    is fed in one call — instruction + map + ALL FOUR batch files + closing.
    Bytes, not characters: a multi-byte body measures what it costs."""
    return len(compose(src, group).encode("utf-8"))


def fit(src: ChunkSources, group: list[str], depth: int, prompt_max: int,
        resplit_max_halvings: int) -> list[tuple[list[str], int, bool, bool]]:
    """(group, bytes, oversize, resplit_bound_hit) after D9a's bounded halving.

    NEVER TRUNCATED AND NEVER DROPPED, at either terminal: a truncated prompt
    is a short batch wearing a full one's row count, and a row the model
    never sees is a coverage hole. A single oversized row means one document
    blew the body budget; a MULTI-ROW group still over at the bound means the
    arithmetic is wrong — the ceiling and the per-row budget disagree — and
    it is the ceiling that must be revisited, not the input. So the two are
    distinguished by `resplit_bound_hit` rather than merged.
    """
    n = fed_bytes(src, group)
    if n <= prompt_max or len(group) <= 1 or depth >= resplit_max_halvings:
        over = n > prompt_max
        return [(group, n, over, over and len(group) > 1
                 and depth >= resplit_max_halvings)]
    mid = (len(group) + 1) // 2          # 50 → 25 → 13 → 7 → 4
    return (fit(src, group[:mid], depth + 1, prompt_max, resplit_max_halvings)
            + fit(src, group[mid:], depth + 1, prompt_max, resplit_max_halvings))


def write_chunks(src: ChunkSources, placed: list[tuple[list[str], int, bool, bool]],
                 out_dir: Path, split_batch: Callable[[str], tuple[str, list[dict]]],
                 join_chunk: Callable[..., dict]) -> tuple[list[int], list[dict],
                                                           list[dict], set[str]]:
    """One directory per placed group; returns (per_chunk, chunk_records,
    join_records, covered_by_chunks)."""
    per_chunk, chunk_records, join_records = [], [], []
    covered_by_chunks: set[str] = set()
    for k, (group, nbytes, oversize, bound_hit) in enumerate(placed):
        # ponytail: :02d sorts lexically for <=99 chunks (4950 rows at size 50);
        # widen the pad if a run ever exceeds that. Numbering is re-derived from
        # the FINAL group list, so a re-split leaves `chunk-NN` contiguous.
        chunk_name = f"chunk-{k:02d}"
        chunk_dir = out_dir / chunk_name
        chunk_dir.mkdir(parents=True, exist_ok=True)
        bodies = bodies_for(src, group)
        for t in src.batch_types:
            (chunk_dir / f"batch-{t}.md").write_text(bodies[t], encoding="utf-8")
        mt = written_map(src, chunk_dir, group, chunk_name)
        if mt is not None:
            covered_by_chunks |= set(mt[0]["blocks"])
        per_chunk.append(len(group))
        rec = {"chunk": chunk_name, "rows": len(group)}
        if src.instruction is not None:
            prompt_text = src.compose_prompt(src.instruction,
                                             mt[1] if mt else None, bodies,
                                             src.closing)
            # 0600, THROUGH THE SAME WRITER THE MAPS USE. `prompt.txt` is the
            # single most concentrated artifact this pipeline produces — the
            # whole chunk's vault context plus every mail body it will judge —
            # and its own mode is the second belt behind the run directory's
            # 0700 (`cos_nightly.sh`). A default `write_text` here would be
            # umask-derived 0644.
            src.ground().write_text_0600(chunk_dir / "prompt.txt", prompt_text)
            nbytes = len(prompt_text.encode("utf-8"))
            src.ground().write_text_0600(chunk_dir / "prompt.bytes", f"{nbytes}\n")
            rec.update(prompt_bytes=nbytes, oversize=oversize,
                       resplit_bound_hit=bound_hit)
            batch_ids: set[str] = set()
            for t in src.batch_types:
                _h, rows_t = split_batch(bodies[t])
                batch_ids |= {r.get("conversation_id") for r in rows_t
                              if isinstance(r, dict)}
            join_records.append(join_chunk(chunk_name, prompt_text,
                                           mt[0] if mt else None,
                                           mt[1] if mt else None,
                                           batch_ids, src.required))
        chunk_records.append(rec)
    return per_chunk, chunk_records, join_records, covered_by_chunks


def write_join(src: ChunkSources, join_out: Path, join_records: list[dict],
               covered_by_chunks: set[str], short_chunks: Callable[[dict], list[str]]
               ) -> tuple[bool, int]:
    """The D2a join record, written 0600; returns (ok, required_covered)."""
    # THE DENOMINATOR JOIN, and it is between two INDEPENDENTLY produced
    # artifacts: `required` comes from the fetcher's frozen map, and
    # `batch_ids` comes from the batch files `cos_judge.py --batches`
    # rendered. D13's guarantee is that they are the same set; a fetcher
    # that under-required is exactly what scores a clean `grounded` when
    # nothing joins its own word to anything else.
    all_batch_ids: set[str] = set()
    for t in src.batch_types:
        all_batch_ids |= {r.get("conversation_id") for r in src.parsed[t][1]
                          if isinstance(r, dict)}
    join: dict[str, Any] = {"run_id": (src.payload or {}).get("run_id"),
                            "chunks": join_records,
                            "required": len(src.required),
                            "required_covered_by_chunks": len(covered_by_chunks),
                            "required_not_in_batches": sorted(src.required - all_batch_ids),
                            "batch_ids_not_required": sorted(all_batch_ids - src.required)}
    # ONE predicate, called by the producer, `cos_echecks` and the shell.
    bad = short_chunks(join)
    # `ok` is a claim about DELIVERY, so an ungrounded night — which claims
    # no delivery — is never `ok` here and E10 never reads this file for one.
    join["ok"] = (src.grounded and not bad
                  and not join["required_not_in_batches"]
                  and join["required_covered_by_chunks"] >= join["required"])
    src.ground().write_text_0600(join_out, json.dumps(join, indent=2) + "\n")
    return join["ok"], join["required_covered_by_chunks"]
