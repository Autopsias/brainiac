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
        out[t] = header + json.dumps(sub, indent=1, ensure_ascii=False)
    return out


def map_text_for(src: ChunkSources, group: list[str],
                 chunk_name: str) -> tuple[dict, str] | None:
    if not src.grounded:
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
