"""Per-chunk text construction for the COS split mode: batch bodies, grounding
map, composed prompt.

``cos_batch_chunk.do_split`` keeps its name, signature and module (the shell,
the tests and the doctrine text name it there); what lives here is the text a
single chunk is made of. Everything the construction needs from the parent
module (``BATCH_TYPES``, ``chunk_map``, ``compose_prompt``, the lazy
``_ground`` fetcher) rides on :class:`ChunkSources`, built once per split —
this module never imports ``cos_batch_chunk``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, NamedTuple


class ChunkSources(NamedTuple):
    """What one split loaded, plus the parent callables the composition uses."""

    parsed: dict[str, tuple[str, list[dict]]]
    payload: dict | None
    grounded: bool
    required: set[str]
    instruction: str | None
    closing: str
    ground: Callable[[], Any]
    chunk_map: Callable[[dict, list[str], str], dict]
    compose_prompt: Callable[..., str]
    batch_types: tuple[str, ...]
    #: DRAFT-01: composes the draft leg's own prompt. Defaulted so a caller
    #: that predates the second leg still constructs.
    compose_draft: Callable[..., str] | None = None


#: THE ROW COUNT IN A CHUNK'S HEADER IS THE CHUNK'S, NOT THE NIGHT'S (2026-09-04).
#: `bodies_for` slices the rows and kept the source file's header verbatim, so
#: every chunk of run 257 opened with the WHOLE night's count while carrying its
#: own slice: chunk-04 said "You are judging 117 conversations" over 17 rows, and
#: every chunk's draft batch said "10 CANDIDATE rows" over 0, 5, 2, 0 and 3. The
#: prompts then demand "ANSWER EVERY ROW", so the number the model is told and
#: the number it is given disagree on every chunk of every night since the
#: chunker shipped.
#:
#: The patterns are DERIVED FROM THE TEMPLATES' OWN `{n}` LINE rather than
#: restated here. A second spelling of a prompt line is a rename away from
#: silently matching nothing, and a renumber that matches nothing looks exactly
#: like a renumber that was not needed.
_COUNT_RES: dict[str, re.Pattern[str]] | None = None


def _count_res() -> dict[str, re.Pattern[str]]:
    global _COUNT_RES
    if _COUNT_RES is None:
        from cos_judge_prompts import (  # noqa: PLC0415
            DRAFT_PROMPT, HOLD_PROMPT, STAGING_PROMPT, TRIAGE_PROMPT)
        built: dict[str, re.Pattern[str]] = {}
        for leg, tpl in (("triage", TRIAGE_PROMPT), ("staging", STAGING_PROMPT),
                         ("hold", HOLD_PROMPT), ("draft", DRAFT_PROMPT)):
            line = next((ln for ln in tpl.splitlines() if "{n}" in ln), None)
            if line is None:
                continue
            built[leg] = re.compile("".join(
                r"(\d+)" if part == "{n}" else re.escape(part)
                for part in re.split(r"(\{n\})", line)))
        _COUNT_RES = built
    return _COUNT_RES


def renumber_header(leg: str, header: str, n: int) -> str:
    """The header with its row count set to `n`, or unchanged when the line the
    template renders is not there. Never raises: a chunk whose header cannot be
    renumbered is still a chunk, and killing the split over a prompt-wording
    change would trade a wrong number for no night at all."""
    rx = _count_res().get(leg)
    m = rx.search(header) if rx is not None else None
    if m is None:
        return header
    return header[:m.start(1)] + str(n) + header[m.end(1):]


def bodies_for(src: ChunkSources, group: list[str]) -> dict[str, str]:
    gset = set(group)
    out = {}
    for t in src.batch_types:
        header, rows = src.parsed[t]
        # Rows this file holds for this group, in the FILE's own order. A type
        # with none still gets a file with `[]`, so every batch is present.
        # Staging text/offset rows are per-row self-contained, so slicing by
        # id keeps each span valid — no renumber.
        sub = [r for r in rows if r.get("conversation_id") in gset]
        out[t] = (renumber_header(t, header, len(sub))
                  + json.dumps(sub, indent=1, ensure_ascii=False))
    return out


def map_text_for(src: ChunkSources, group: list[str],
                 chunk_name: str) -> tuple[dict, str] | None:
    """The map for this chunk, or None when there is NOTHING to ship.

    A PAYLOAD THAT EXISTS IS SHIPPED, GROUNDED OR NOT (2026-09-03). This
    returned None on every ungrounded night, so one failed lookup out of 120
    discarded the other 118 blocks — context already fetched, already written
    to disk. Measured over runs 248/249/250: 157 threads judged blind while
    their vault context sat there.

    D2's objection was that a half-delivered map behind the word "ungrounded"
    is unauditable. It is not: `cos_ground_write.map_text` renders every block
    with its OWN status — `ok` with text, `no-vault-content`, or
    `lookup-failed` with a reason — and `chunk_map` carries the run's `state`
    and `reason` at the top plus `not_attempted` for ids with no block at all.
    Nothing is silently missing, so the ambiguity never arises.

    None still means what it always meant at the prompt boundary — no payload
    on disk, so `NO_CONTEXT_LINE` covers the chunk.
    """
    if not isinstance(src.payload, dict):
        return None
    cmap = src.chunk_map(src.payload, group, chunk_name)
    return cmap, src.ground().map_text(cmap)


def written_map(src: ChunkSources, chunk_dir: Path, group: list[str],
                chunk_name: str) -> tuple[dict, str] | None:
    """Write the chunk's map, then READ IT BACK — and compose part 2 from
    the bytes that are actually on disk.

    THE PROBE THAT FOUND THIS. Composing from the in-memory string made the
    writer and the composer the same expression, so D2a's join compared a
    value against itself: a serializer that wrote something else to disk
    would still have joined clean. Reading back makes "the map arrived byte
    for byte" a claim about the FILE the design names, which is the only
    version of that claim worth making."""
    mt = map_text_for(src, group, chunk_name)
    if mt is None:
        return None
    # 0600 + atomic, through the SAME writer the run map uses.
    path = src.ground().write_text_0600(chunk_dir / "grounding.json", mt[1])
    return mt[0], path.read_text(encoding="utf-8")


def compose(src: ChunkSources, group: list[str],
            chunk_name: str = "chunk-XX") -> str:
    # THE MEASUREMENT USES A PLACEHOLDER NAME, and the bound is stated rather
    # than hidden: `chunk-XX` is the same width as every real `chunk-NN` up to
    # 99 chunks, so the re-split DECISION is exact there and understates by one
    # byte per extra digit beyond that. The RECORDED `prompt_bytes` is always
    # re-measured on the written text, so the artifact is exact either way.
    mt = map_text_for(src, group, chunk_name)
    return src.compose_prompt(src.instruction or "", mt[1] if mt else None,
                              bodies_for(src, group), src.closing)
