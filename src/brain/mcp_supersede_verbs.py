"""The broker's ``supersede`` / ``unsupersede`` (SUP-01, 2026-09-18).

Thin on purpose: every guard lives in :func:`brain.supersede_declared.guarded`,
which ends in the SAME ``core.supersede`` the CLI runs. ``supersede`` stays a
host-only verb — the broker IS the host. Neither name is in
``mcp_verbs.VM_WRITE_ALIASES``, so ``mcp_adapter.dispatch`` refuses a
``role=vm`` core before this module is reached, and ``core._require_host``
would refuse it again.

Not in ``BODYLESS_TOOLS``: the visibility guard goes through
``egress.apply_gate``, so every call leaves an ordinary SEC-06 row.
"""
from __future__ import annotations

from typing import Any


def dispatch_supersede(
    tool: str, args: dict[str, Any], *, core: Any, max_tier: str,
) -> dict[str, Any]:
    from . import supersede_declared

    return supersede_declared.guarded(
        core, str(args.get("old_id") or ""), str(args.get("new_id") or ""),
        reason=str(args.get("reason") or ""), max_tier=max_tier,
        declared_by=f"mcp:{tool}", undo=tool == "unsupersede",
    )
