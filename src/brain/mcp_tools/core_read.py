"""The six read tools the broker has always served, moved onto the seam.

Bodies are unchanged from ``mcp_adapter.serve()`` before closed-stacks S02:
same names, same signatures, same defaults, same docstrings (the docstring IS
the tool description an MCP client shows the model, so editing one is a
behaviour change, not a comment change).
"""
from __future__ import annotations

from typing import Any

from .. import classification as cls
from ..mcp_adapter import dispatch


def register(server: Any, *, core: Any) -> None:
    """Register the core read verbs on ``server``."""

    @server.tool()
    def vault_languages(max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER) -> dict:
        """Return the derived language census for this vault, counted over the notes max_tier admits."""
        return dispatch(
            "vault_languages", {"max_tier": max_tier}, core=core,
        )

    @server.tool()
    def search(
        query: str,
        variants: list[str] | None = None,
        k: int = 10,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
        include_retired: bool = False,
    ) -> dict:
        """Search the vault with optional multilingual query variants.

        Versions retired by a supersede chain are hidden unless
        ``include_retired`` — ask for them only for a 'previous version'
        question."""
        return dispatch(
            "search",
            {"query": query, "variants": variants or [], "k": k,
             "max_tier": max_tier, "include_retired": include_retired},
            core=core,
        )

    @server.tool()
    def get(id: str, max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER) -> dict:
        """Fetch one full note by id."""
        return dispatch("get", {"id": id, "max_tier": max_tier}, core=core)

    @server.tool()
    def recent(
        n: int = 10,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
        include_retired: bool = False,
    ) -> dict:
        """List recently created or updated notes. Versions a supersede chain
        retired are hidden — superseding a note updates it, so they would
        otherwise sort straight to the top. Ask for ``include_retired`` only
        for a 'previous version' question."""
        return dispatch(
            "recent",
            {"n": n, "max_tier": max_tier, "include_retired": include_retired},
            core=core,
        )

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
