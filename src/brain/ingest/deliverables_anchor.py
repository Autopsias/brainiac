"""Where a drop-lane deliverable's anchor note lives (split out of
``deliverables.py`` for the file-size ratchet; re-exported there)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

_ANCHOR_SUBTREE = "brain/resources"


def anchor_id(slug: str) -> str:
    return f"{slug}-deliverable"


def anchor_rel(slug: str) -> str:
    return f"{_ANCHOR_SUBTREE}/{anchor_id(slug)}.md"


def existing_anchor_rel(vault: Any, slug: str) -> str | None:
    """Where the anchor for ``slug`` sits now, or ``None``. auto-PARA
    (maintenance_para.py) moves a superseded anchor to ``brain/archive/``,
    and that anchor still exists: re-writing it at ``anchor_rel`` would mint a
    duplicate id, and counting it missing is a false regression."""
    for rel in (anchor_rel(slug), f"brain/archive/{anchor_id(slug)}.md"):
        if (Path(vault) / rel).is_file():
            return rel
    return None
