"""hot.md rotation past its soft cap (retro signature ``hot-md-bloat``)."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# hot.md rotation (retro signature ``hot-md-bloat``): handoff.md already
# auto-rotates to archive/ at ~15 KB (docs/session-memory.md); hot.md gets the
# same treatment at its own soft cap (``retro.HOT_MD_SOFT_MAX_BYTES``, 32 KB —
# the size it reached unread in the field). Aged, resolved entries rotate to
# ``archive/hot-<date>.md`` WITH their idempotency-key comment lines (so the
# append-once guard still finds them there); recent entries and anything still
# asking for owner input stay in the live file.
# ---------------------------------------------------------------------------
HOT_MD_ROTATE_KEEP_DAYS = 7


def _joined_len(blocks: list[str]) -> int:
    """Byte length of ``blocks`` once joined the way ``kept_text`` is."""
    return len(("\n\n".join(blocks) + "\n").encode("utf-8")) if blocks else 0


def rotate_hot_md(
    text: str, today: datetime.date, *,
    max_bytes: int | None = None, keep_days: int = HOT_MD_ROTATE_KEEP_DAYS,
) -> tuple[str, str]:
    """Split hot.md into ``(kept_text, rotated_text)``.

    No-op (``rotated_text == ""``) while the file is under ``max_bytes``.
    When over, every idempotency-keyed block whose ``## <date>`` header is
    older than ``keep_days`` AND that carries no open ``**Owner input
    needed`` line moves to ``rotated_text``, key line included. Unkeyed
    preamble and recent entries are always kept.

    Aged UNRESOLVED blocks are preferred-kept, not kept unconditionally: if
    the file is still over ``max_bytes`` once the resolved ones are gone,
    they rotate too, oldest first, until it fits. Exempting them forever
    made the cap unenforceable — measured 2026-07-27, a live hot.md sat at
    60 KB (2x the cap) because 47 of 51 blocks, up to 20 days old, carried
    an ``**Owner input needed`` line that no fold ever cleared. Under the
    PUSH model (AGENTS.md §9) a real owner decision belongs in
    ``inbox.jsonl``; an unresolved marker aging out in hot.md is a log
    entry, not a queue item, and rotation ARCHIVES it (key line included)
    rather than dropping it. Recent entries are still never rotated, so a
    freshly-raised owner question always stays in the live file."""
    from .retro import _HEADER_DATE_RE, _KEY_RE, HOT_MD_SOFT_MAX_BYTES

    limit = HOT_MD_SOFT_MAX_BYTES if max_bytes is None else max_bytes
    if len(text.encode("utf-8")) <= limit:
        return text, ""
    kept: list[str] = []
    rotated: list[str] = []
    deferred: list[tuple[datetime.date, int, str]] = []  # aged+unresolved
    for part in re.split(r"\n(?=<!--\s*idempotency-key:)", text):
        block = part.strip("\n")
        if not block:
            continue
        if not _KEY_RE.search(block):
            kept.append(block)  # unkeyed preamble/tail — never rotated
            continue
        aged = False
        entry_date: datetime.date | None = None
        m = _HEADER_DATE_RE.search(block)
        if m:
            try:
                entry_date = datetime.date.fromisoformat(m.group("date"))
                aged = (today - entry_date).days > keep_days
            except ValueError:
                aged = False
        unresolved = "**Owner input needed" in block
        if aged and unresolved and entry_date is not None:
            # Preferred-kept: reconsidered below only if still over the cap.
            deferred.append((entry_date, len(kept), block))
            kept.append(block)
        else:
            (rotated if aged else kept).append(block)
    # Second pass: still over the cap, so give up the oldest aged+unresolved
    # blocks (oldest first) until it fits. They are archived, never dropped.
    if deferred and _joined_len(kept) > limit:
        for _, slot, block in sorted(deferred, key=lambda d: d[0]):
            kept[slot] = ""
            rotated.append(block)
            if _joined_len([k for k in kept if k]) <= limit:
                break
        kept = [k for k in kept if k]
    if not rotated:
        return text, ""
    kept_text = ("\n\n".join(kept) + "\n") if kept else ""
    rotated_text = "\n\n".join(rotated) + "\n"
    return kept_text, rotated_text

# Cross-section binds, deferred past this module's own defs.
