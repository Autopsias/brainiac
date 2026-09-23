"""``supersede`` / ``unsupersede`` through the broker (SUP-01, 2026-09-18).

Cowork has no terminal that can type ``brain supersede``, so a new version
dropped from a Cowork session left the old one live in search. These two tools
are that verb, reached through the host-run broker — see
:mod:`brain.supersede_declared` for the guards and for why a pair the vault
does not itself relate is PROPOSED to the owner rather than applied.
"""
from __future__ import annotations

from typing import Any

from ..mcp_adapter import dispatch


def register(server: Any, *, core: Any) -> None:
    """Register ``supersede`` and ``unsupersede`` on ``server``."""

    @server.tool()
    def supersede(old_id: str, new_id: str, reason: str) -> dict:
        """Retire note ``old_id`` in favour of ``new_id`` (a new version).

        APPLIED only when the vault itself relates the pair: ``new_id`` (or its
        deliverable anchor) wikilinks or declares ``replaces:`` ``old_id``, or
        the two share a document name. Otherwise NOT applied — it is staged for
        the owner's nightly question and this returns
        ``{applied: false, proposed: true}``. A note written by ``capture`` is
        untrusted and is never related by its own say-so. Both ids must be
        notes you can read; ``reason`` is required. Returns both sides'
        ``is_latest_version`` / ``superseded_by`` / ``previous_version``."""
        return dispatch("supersede", {"old_id": old_id, "new_id": new_id,
                                      "reason": reason}, core=core)

    @server.tool()
    def unsupersede(old_id: str, new_id: str, reason: str) -> dict:
        """Undo ONE supersession link (``old_id`` was retired under
        ``new_id``), both sides, through the same audited path. Same
        visibility and ``reason`` guards as ``supersede``."""
        return dispatch("unsupersede", {"old_id": old_id, "new_id": new_id,
                                        "reason": reason}, core=core)
