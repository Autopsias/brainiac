"""Optional MCP facade for the read-only Chat tab transport.

The adapter keeps the public verb table and trust gate stable while the
individual read bodies live in :mod:`brain.mcp_verbs`.
"""
from __future__ import annotations

from typing import Any

from . import classification as cls
from . import config
from .core import BrainCore
from .mcp_verbs import (
    DEFAULT_EGRESS_CEILING_TIER,
    EGRESS_CEILING_ENV_VAR,
    VM_READ_ALIASES,
    _capture_rerank_metadata,
    _clamp_max_tier,
    _egress_ceiling_tier,
    _filtered,
    _variant_queries,
    dispatch_bases_query,
    dispatch_dossier,
    dispatch_note,
    dispatch_recent,
    dispatch_search,
    dispatch_vault_languages,
)

__all__ = [
    "READ_TOOLS", "dispatch", "serve", "DEFAULT_EGRESS_CEILING_TIER",
    "EGRESS_CEILING_ENV_VAR", "_capture_rerank_metadata", "_clamp_max_tier",
    "_egress_ceiling_tier", "_filtered", "_variant_queries",
]

READ_TOOLS = ("search", "get", "recent", "bases_query", "dossier", "vault_languages")


def dispatch(
    tool: str,
    args: dict[str, Any],
    *,
    core: BrainCore | None = None,
    vault: str | None = None,
) -> dict[str, Any]:
    """Dispatch one read verb after enforcing the VM trust boundary."""
    role = getattr(core, "role", config.role()) if core is not None else config.role()
    if role == config.ROLE_VM and tool not in VM_READ_ALIASES:
        raise ValueError(
            f"role=vm may not dispatch host-only tool {tool!r}; "
            f"MCP adapter exposes only {READ_TOOLS}"
        )
    core = core or BrainCore(vault=vault)
    max_tier = _clamp_max_tier(str(args.get("max_tier", cls.DEFAULT_MAX_TIER)))
    handlers = {
        "search": dispatch_search,
        "hybrid-search": dispatch_search,
        "get": dispatch_note,
        "read": dispatch_note,
        "recent": dispatch_recent,
        "dossier": dispatch_dossier,
        "vault_languages": dispatch_vault_languages,
        "vault-languages": dispatch_vault_languages,
        "bases-query": dispatch_bases_query,
        "bases_query": dispatch_bases_query,
    }
    handler = handlers.get(tool)
    if handler is None:
        raise ValueError(
            f"unknown / non-read tool {tool!r}; MCP adapter exposes only {READ_TOOLS}"
        )
    return handler(tool, args, core=core, max_tier=max_tier)


def serve(vault: str | None = None) -> None:  # pragma: no cover - transport glue
    """Run the optional stdio MCP server for the Chat tab."""
    from mcp.server.fastmcp import FastMCP

    core = BrainCore(vault=vault)
    server = FastMCP("brain")

    @server.tool()
    def vault_languages(max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER) -> dict:  # noqa: ARG001
        """Return the derived language census for this vault."""
        return dispatch(
            "vault_languages", {"max_tier": max_tier}, core=core,
        )

    @server.tool()
    def search(
        query: str,
        variants: list[str] | None = None,
        k: int = 10,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
    ) -> dict:
        """Search the vault with optional multilingual query variants."""
        return dispatch(
            "search",
            {"query": query, "variants": variants or [], "k": k, "max_tier": max_tier},
            core=core,
        )

    @server.tool()
    def get(id: str, max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER) -> dict:
        """Fetch one full note by id."""
        return dispatch("get", {"id": id, "max_tier": max_tier}, core=core)

    @server.tool()
    def recent(n: int = 10, max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER) -> dict:
        """List recently created or updated notes."""
        return dispatch("recent", {"n": n, "max_tier": max_tier}, core=core)

    @server.tool()
    def dossier(
        query: str,
        k: int = 12,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
    ) -> dict:
        """Return the separated decision-state dossier."""
        return dispatch(
            "dossier", {"query": query, "k": k, "max_tier": max_tier}, core=core,
        )

    @server.tool()
    def bases_query(
        where: dict | None = None,
        k: int = 50,
        latest_only: bool = False,
        as_of: str = "",
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
    ) -> dict:
        """Run a structured frontmatter query."""
        return dispatch(
            "bases_query",
            {
                "where": where or {},
                "k": k,
                "latest_only": latest_only,
                "as_of": as_of or None,
                "max_tier": max_tier,
            },
            core=core,
        )

    server.run()


if __name__ == "__main__":  # pragma: no cover
    serve()
