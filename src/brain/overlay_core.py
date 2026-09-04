"""Where the active overlay lives — the one resolution both halves share.

Split out at the 2026-09-04 size ratchet so ``overlay.py`` and
``overlay_keywords.py`` can each import it without a cycle. ``overlay.py``
re-exports :func:`overlay_dir`, so every existing ``brain.overlay.overlay_dir``
caller is unchanged.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def overlay_dir(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the active overlay directory.

    Precedence: ``explicit`` arg (``--overlay-dir``) > ``$BRAIN_OVERLAY_DIR`` >
    ``<vault>/overlay`` (the overlay travels with the user's vault, alongside
    ``raw/`` and ``brain/`` — see AGENTS.md §1).
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BRAIN_OVERLAY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    from . import config

    return config.vault_root(vault) / "overlay"


#: The classification ladder, low to high. Shared by the ingest rules and
#: the decoder ring since the 2026-09-04 split.
TIERS: tuple[str, ...] = ("Public", "Internal", "Confidential", "Restricted", "MNPI")
KEYWORDS_GENERATED_DIR = "keywords-generated"
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_PLACEHOLDER_RE = re.compile(r"^<.*>$")

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"


def _strip_noise(body: str) -> list[str]:
    """Drop fenced code blocks and HTML comments — the two places a template
    legitimately shows EXAMPLE rules that must never be read as real ones."""
    out: list[str] = []
    in_fence = False
    in_comment = False
    for line in body.splitlines():
        if in_comment:
            if _COMMENT_CLOSE in line:
                in_comment = False
            continue
        if _COMMENT_OPEN in line:
            # a whole-line or trailing comment opener; single-line comments close here
            if _COMMENT_CLOSE not in line.split(_COMMENT_OPEN, 1)[1]:
                in_comment = True
            continue
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out
