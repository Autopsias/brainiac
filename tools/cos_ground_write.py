"""The 0600-atomic grounding-map writer of `cos_ground` — canonical serialization plus owner-only atomic file writes (D6a, batch-2 drain).

Moved verbatim out of `cos_ground`; `map_text`, `write_text_0600` and
`write_map` are re-imported by the parent, so `cos_batch_chunk`'s
`src.ground().write_text_0600(...)` route is unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def map_text(payload: dict[str, Any]) -> str:
    """THE canonical serialization of a grounding map — one function, because
    D2a's join compares `$CHUNK/grounding.json`'s bytes against the composed
    prompt, and a composer that re-serialized the map would compare two
    encodings of the same data. `ensure_ascii=False` is load-bearing for the
    same reason: the per-block needles are
    `json.dumps(text, ensure_ascii=False)` against these very bytes."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_text_0600(path: Path, text: str) -> Path:
    """Owner-only and atomic, on TEXT. `cos_batch_chunk.py` writes the per-chunk
    map through this so both maps take exactly the same route to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = text.encode("utf-8")
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise OSError(f"write made no progress on {tmp.name}")
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)          # same directory, so this is atomic
    return path


def write_map(path: Path, payload: dict[str, Any]) -> Path:
    """Owner-only and atomic. Never a partially written map a leg could be
    handed, and never a world-readable one."""
    return write_text_0600(path, map_text(payload))


def selected_ids(ev: Path | None) -> set[str] | None:
    """The conversations THIS batch judges, from the selected enumeration.

    On a chained run `$EV/enumeration.json` is the per-batch SELECTION (the
    thread-cap slice), while the full census lives in `enumeration-full.json`.
    `required` must be scored over what this run actually judges and renders,
    not the whole read population: the chunker renders the selection alone, so
    a `required` frozen over the full census leaves every deferred thread as a
    permanent `required_not_in_batches` orphan and E10 can never pass on a
    backlog wider than one thread cap. `None` (no ev, no file, or a file
    without a selection cap) means "no per-batch selection" — the whole
    population is the batch, and the caller applies no filter, exactly as
    before this scoping existed.
    """
    if ev is None:
        return None
    try:
        data = json.loads((ev / "enumeration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # A full (unselected) enumeration carries no `batch_thread_cap`; only
    # `cos_batch_session.select` writes one. Absent it, apply no filter.
    if not isinstance(data, dict) or data.get("batch_thread_cap") is None:
        return None
    ids = {str(r.get("conversation_id")) for r in (data.get("rows") or [])
           if isinstance(r, dict) and r.get("conversation_id")}
    return ids or None
