"""OPTIONAL, DELETABLE ~50-line MCP adapter for the pure Claude Desktop CHAT tab.

This is the ONE surface that cannot run a shell command — so it gets a thin MCP
bridge. Every OTHER harness (Codex, Claude Code, Gemini CLI, the Desktop Code
tab, the Cowork VM) calls the ``brain`` CLI directly; MCP is NEVER the
foundation. Delete this file and nothing else breaks.

The bridge wraps the SAME ``BrainCore`` + the SAME deny-by-default
``ClassificationFilter`` the CLI applies at stdout — there is no second egress
path to keep in sync. It exposes ONLY the read verbs (``search`` / ``get`` /
``recent``); it never exposes write/draft/host-broker commands. ``dispatch`` is
the pure, importable, testable core; ``serve`` is the (optional) stdio transport
glue requiring ``pip install 'profile-a-brain[mcp]'``.
"""
from __future__ import annotations

import os
from typing import Any

from . import classification as cls
from . import egress
from .core import BrainCore

READ_TOOLS = ("search", "get", "recent")

# Server-side egress ceiling (SEC-01 hardening). A caller-supplied ``max_tier``
# was previously honored unbounded — an MCP client could simply ASK for
# ``max_tier="Secret"`` and receive it. That is a human-gated elevation on the
# CLI (an explicit ``--max-tier`` flag someone typed), but the MCP transport has
# no equivalent "a person is watching this" signal, so the adapter now clamps
# EVERY request to a ceiling the operator configures out-of-band. A caller may
# still request something NARROWER than the ceiling (always honored); it can
# never request higher.
EGRESS_CEILING_ENV_VAR = "BRAIN_MAX_EGRESS_TIER"
DEFAULT_EGRESS_CEILING_TIER = "Internal"  # matches cls.DEFAULT_MAX_TIER


def _egress_ceiling_tier() -> str:
    """The operator-configured hard ceiling for MCP egress. Unset or an
    unrecognised value falls back to the conservative default — fail-closed,
    never fail-open on a typo'd env var."""
    raw = os.environ.get(EGRESS_CEILING_ENV_VAR, DEFAULT_EGRESS_CEILING_TIER).strip()
    return raw if raw in cls.RANK else DEFAULT_EGRESS_CEILING_TIER


def _clamp_max_tier(requested_tier: str) -> str:
    """Clamp a caller-supplied ``max_tier`` to ``min(requested_rank, ceiling_rank)``.

    An unrecognised ``requested_tier`` is passed through UNCHANGED so the
    existing ``ClassificationFilter`` validation (``egress.apply_gate`` ->
    ``cls.ClassificationFilter.__post_init__``) still raises its normal, clear
    "unknown max_tier" error — this clamp only ever narrows a VALID request, it
    never manufactures or swallows a validation error.
    """
    requested = requested_tier.strip()
    if requested not in cls.RANK:
        return requested_tier
    ceiling_rank = cls.RANK[_egress_ceiling_tier()]
    clamped_rank = min(cls.RANK[requested], ceiling_rank)
    return cls.TIERS[clamped_rank]


def _filtered(items: list[dict], max_tier: str) -> tuple[list[dict], dict]:
    # Same single egress chokepoint as the CLI (SEC-01) — no second egress path.
    return egress.apply_gate(items, max_tier)


def dispatch(tool: str, args: dict[str, Any], *, core: BrainCore | None = None,
             vault: str | None = None) -> dict[str, Any]:
    """Run one read tool through the SAME egress gate as the CLI. Pure + testable.

    ``max_tier`` is clamped server-side to the ``BRAIN_MAX_EGRESS_TIER`` ceiling
    (default "Internal") BEFORE it reaches the classification filter — a caller
    cannot self-elevate past the configured ceiling by simply asking.
    """
    core = core or BrainCore(vault=vault)  # host or vm; reads only either way
    max_tier = _clamp_max_tier(str(args.get("max_tier", cls.DEFAULT_MAX_TIER)))
    if tool in ("search", "hybrid-search"):
        hits = [h.to_dict() for h in core.hybrid_search(str(args["query"]), k=int(args.get("k", 10)))]
        surfaced, report = _filtered(hits, max_tier)
        return {"results": surfaced, "egress": report}
    if tool in ("get", "read"):
        note = core.get(str(args["id"]))
        surfaced, report = _filtered([note] if note else [], max_tier)
        return {"result": surfaced[0] if surfaced else None, "egress": report}
    if tool == "recent":
        surfaced, report = _filtered(core.recent(limit=int(args.get("n", 10))), max_tier)
        return {"results": surfaced, "egress": report}
    raise ValueError(f"unknown / non-read tool {tool!r}; MCP adapter exposes only {READ_TOOLS}")


def serve(vault: str | None = None) -> None:  # pragma: no cover - transport glue
    """Optional stdio MCP server for the Chat tab. Requires the `mcp` package."""
    from mcp.server.fastmcp import FastMCP

    core = BrainCore(vault=vault)
    server = FastMCP("brain")

    @server.tool()
    def search(query: str, k: int = 10, max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """Hybrid (BM25+dense) retrieval, deny-by-default egress-filtered."""
        return dispatch("search", {"query": query, "k": k, "max_tier": max_tier}, core=core)

    @server.tool()
    def get(id: str, max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """Fetch one note by id, egress-filtered."""
        return dispatch("get", {"id": id, "max_tier": max_tier}, core=core)

    @server.tool()
    def recent(n: int = 10, max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """List recently updated notes, egress-filtered."""
        return dispatch("recent", {"n": n, "max_tier": max_tier}, core=core)

    server.run()


if __name__ == "__main__":  # pragma: no cover
    serve()
