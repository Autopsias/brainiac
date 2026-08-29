"""The retrieval verbs a Cowork session lost when the vault left the mount.

The s01 survey (``docs/operations/cowork-skill-verb-survey.json``) is the
definition of done for which verbs belong here: the ones it scores ``add_read``
— already trusted to the untrusted VM leg over the CLI — and that RETURN VAULT
CONTENT. That is ``grep``, ``graph-expand`` and ``read``, plus the two
``add_read`` retrieval verbs no bundle happens to call yet, ``hybrid-search``
and ``diagnose``.

**The improvement worth naming.** A Cowork session greps the vault with raw
shell ``grep`` today: not filtered, not recorded, and reading files the desk
never sees. Through the broker the same search is both — and ``grep``
specifically is the verb where that matters most, because the caller chooses
the pattern (see :func:`brain.mcp_verbs.dispatch_grep`).

Each body is a thin call into ``mcp_adapter.dispatch``, which routes to the
same ``mcp_verbs`` handler the CLI uses and applies ``egress.apply_gate``. The
returned shape matches the verb's ``--json`` CLI output so a skill's parsing
does not change.
"""
from __future__ import annotations

from typing import Any

from .. import classification as cls
from ..mcp_adapter import dispatch


def register(server: Any, *, core: Any) -> None:
    """Register the filtered retrieval verbs on ``server``."""

    @server.tool()
    def grep(
        pattern: str,
        k: int = 20,
        regex: bool = False,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
    ) -> dict:
        """Lexical-first exact/regex scan over note bodies — no embedding.

        The cheap first probe before escalating to `search`. Notes above your
        tier ceiling are dropped BEFORE matching, so hit counts and ordering
        say nothing about them; `egress.withheld` is 0 on this surface by
        construction."""
        return dispatch(
            "grep",
            {"pattern": pattern, "k": k, "regex": regex, "max_tier": max_tier},
            core=core,
        )

    @server.tool()
    def graph_expand(
        seeds: list[str],
        depth: int = 2,
        k: int = 10,
        use_ppr: bool = True,
        use_inferred: bool = False,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
    ) -> dict:
        """Wikilink-BFS + Personalized PageRank from seed note id(s).

        DISCOVERY-ONLY: the derived graph is never authoritative. Use it to
        nominate candidate ids, then confirm each one with `get`. Notes above
        your tier ceiling are dropped BEFORE seeds resolve and before
        traversal, so an above-ceiling seed is indistinguishable from one that
        does not exist."""
        return dispatch(
            "graph_expand",
            {"seeds": seeds, "depth": depth, "k": k, "use_ppr": use_ppr,
             "use_inferred": use_inferred, "max_tier": max_tier},
            core=core,
        )

    @server.tool()
    def read(id: str, max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER) -> dict:
        """Read one full note by id. Alias of `get`, kept because the shipped
        Cowork skills invoke `brain read`."""
        return dispatch("read", {"id": id, "max_tier": max_tier}, core=core)

    @server.tool()
    def hybrid_search(
        query: str,
        variants: list[str] | None = None,
        k: int = 10,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
        include_retired: bool = False,
    ) -> dict:
        """Fused BM25 + dense + exact-identity ranking. Alias of `search`,
        kept because the CLI and the skills both name it."""
        return dispatch(
            "hybrid_search",
            {"query": query, "variants": variants or [], "k": k,
             "max_tier": max_tier, "include_retired": include_retired},
            core=core,
        )

    @server.tool()
    def diagnose(
        query: str,
        target: str,
        k: int = 10,
        rerank: bool = False,
        rerank_top: int = 20,
        rrf_k: int = 60,
        max_tier: str = cls.HOST_MCP_DEFAULT_MAX_TIER,
    ) -> dict:
        """Trace why one note did or did not surface for a query (ADR-0008).

        Runs the production ranking unchanged, then reports the target's
        per-stage presence and cutoffs. A target above your tier ceiling
        returns the `withheld` sentinel — no query, no trace, but still the
        aggregate egress counts (see `dispatch_diagnose` for why)."""
        return dispatch(
            "diagnose",
            {"query": query, "target": target, "k": k, "rerank": rerank,
             "rerank_top": rerank_top, "rrf_k": rrf_k, "max_tier": max_tier},
            core=core,
        )
