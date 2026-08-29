"""The capture-inbox collision-safe staging primitive (closed-stacks S04, DESK-04).

Split out of :mod:`brain.core._capture` for the same reason
:mod:`brain.mcp_housekeeping_verbs` was split out of :mod:`brain.mcp_verbs`: a
single function with its own docstring belongs on its own, once the file it
was living in sits at the file-size ratchet ceiling. At module scope this
imports only the stdlib plus the two small helpers it needs from
:mod:`brain.core._shared` — nothing from ``._capture`` itself, so this stays a
LEAF.
"""
from __future__ import annotations

import os
import uuid as _uuid
from pathlib import Path

from ._shared import _contained_in


def stage_draft_unique(inbox: Path, note_id: str, staged: str) -> Path:
    """Write ``staged`` under ``inbox`` at a name derived from ``note_id``,
    WITHOUT ever silently overwriting a file that is already there.

    The plain name (``{note_id}.md``) wins whenever it is free — every
    existing caller that stages one draft per id still sees exactly that
    path, unchanged. Only on a COLLISION (two captures landing in the same
    instant, an MCP client and the CLI racing on the same id, a caller
    resubmitting under a chosen id before the first is drained) does this
    fall back to a suffixed name — construction-based (``uuid4``), never a
    timestamp, so two stagers racing on the same wall-clock tick never
    derive the same fallback name from "now".

    ``os.O_EXCL`` makes "does this name exist" and "create it" one atomic
    step at the OS level, so the check is race-free across processes and
    threads, not just within one — a plain ``Path.exists()`` guard followed
    by ``write_text`` would still let two racing writers both pass the check
    and one clobber the other.
    """
    data = staged.encode("utf-8")
    names = (f"{note_id}.md", *(f"{note_id}-{_uuid.uuid4().hex[:8]}.md" for _ in range(8)))
    for name in names:
        candidate = inbox / name
        # Belt over the slug check: the resolved candidate (symlinks
        # followed) must stay inside the inbox — checked for every attempted
        # name, not just the first, though only the first is ever untrusted
        # (the suffixed names are ours).
        if not _contained_in(candidate, inbox):
            raise ValueError(f"draft target escapes capture inbox: {note_id!r}")
        try:
            fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return candidate
    raise RuntimeError(
        f"could not stage a unique capture-inbox file for {note_id!r} after "
        f"{len(names)} attempts"
    )
