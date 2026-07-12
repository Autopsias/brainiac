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
glue requiring ``pip install 'brainiac-cli[mcp]'``.
"""
from __future__ import annotations

import os
from typing import Any

from . import classification as cls
from . import egress
from .core import BrainCore

READ_TOOLS = ("search", "get", "recent", "bases_query", "dossier")

# Server-side egress ceiling (SEC-01 hardening). A caller-supplied ``max_tier``
# was previously honored unbounded — an MCP client could simply ASK for
# ``max_tier="MNPI"`` and receive it. That is a human-gated elevation on the
# CLI (an explicit ``--max-tier`` flag someone typed), but the MCP transport has
# no equivalent "a person is watching this" signal, so the adapter now clamps
# EVERY request to a ceiling the operator configures out-of-band. A caller may
# still request something NARROWER than the ceiling (always honored); it can
# never request higher.
EGRESS_CEILING_ENV_VAR = "BRAIN_MAX_EGRESS_TIER"
DEFAULT_EGRESS_CEILING_TIER = "MNPI"  # matches cls.DEFAULT_MAX_TIER: the MCP
# adapter is a HOST-side surface (Chat tab / Cowork MCP-on-host), so it gets
# the same full-vault default as the host CLI (owner decision, 2026-07-10).
# Operators who want a hard server-side cap set $BRAIN_MAX_EGRESS_TIER; the
# clamp mechanism below is unchanged and still narrows every request to it.


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
        out: dict[str, Any] = {"results": surfaced, "egress": report}
        # RET-09 freshness signal — same contract as the CLI (see
        # cli._freshness_block): tells the agent when the vault continues
        # past its newest hit, so "latest/current" answers don't silently
        # ground on stale-but-coherent material.
        dates = [h.get("date", "") for h in surfaced if h.get("date")]
        if dates:
            try:
                fresh = core.source_freshness(max(dates), max_tier)
            except Exception:  # noqa: BLE001 — freshness must never break search
                fresh = None
            if fresh and fresh.get("newer_count", 0) > 0:
                fresh["hint"] = (
                    f"{fresh['newer_count']} note(s)/source(s) are newer than your "
                    f"newest hit ({fresh['newest_hit_date']}; vault newest "
                    f"{fresh['vault_newest']}) — for 'latest/current' questions, "
                    f"probe past these hits (recent, bases_query latest_only=True, "
                    f"or a narrower search) before treating this as current.")
            if fresh:
                out["freshness"] = fresh
        return out
    if tool in ("get", "read"):
        note = core.get(str(args["id"]))
        surfaced, report = _filtered([note] if note else [], max_tier)
        return {"result": surfaced[0] if surfaced else None, "egress": report}
    if tool == "recent":
        surfaced, report = _filtered(core.recent(limit=int(args.get("n", 10))), max_tier)
        return {"results": surfaced, "egress": report}
    if tool == "dossier":
        res = core.dossier(str(args["query"]), k=int(args.get("k", 12)))
        decisions, drep = _filtered(res["decisions"], max_tier)
        sources, srep = _filtered(res["sources"], max_tier)
        # Merge the two egress reports by NAMED keys — a naive comprehension
        # KeyErrors on conditional keys (casing_mismatch_warnings appears
        # only when a wrong-case tier exists in that half).
        report: dict[str, Any] = {
            k2: drep[k2] + srep[k2]
            for k2 in ("total", "surfaced", "withheld",
                       "withheld_unlabelled_default_deny")
        }
        report["max_tier"] = drep["max_tier"]
        casing = sorted(set(drep.get("casing_mismatch_warnings", []))
                        | set(srep.get("casing_mismatch_warnings", [])))
        if casing:
            report["casing_mismatch_warnings"] = casing
        return {"query": res["query"], "decisions": decisions,
                "sources": sources,
                "retired_excluded": res["retired_excluded"], "egress": report}
    if tool in ("bases-query", "bases_query"):
        filters = dict(args.get("where") or {})
        items = core.bases_query(
            filters, k=int(args.get("k", 50)),
            latest_only=bool(args.get("latest_only", False)),
            as_of=args.get("as_of") or None)
        surfaced, report = _filtered(items, max_tier)
        return {"results": surfaced, "egress": report}
    raise ValueError(f"unknown / non-read tool {tool!r}; MCP adapter exposes only {READ_TOOLS}")


def serve(vault: str | None = None) -> None:  # pragma: no cover - transport glue
    """Optional stdio MCP server for the Chat tab. Requires the `mcp` package."""
    from mcp.server.fastmcp import FastMCP

    core = BrainCore(vault=vault)
    server = FastMCP("brain")

    @server.tool()
    def search(query: str, k: int = 10, max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """Hybrid (BM25+dense) retrieval over the vault, egress-filtered.

        Every hit carries `date` (its valid time), `is_latest_version`, and
        `type` — the AUTHORITY signal: a `type: decision` hit IS the
        recorded decision layer; a `type: source` hit (memos, decks,
        drafts) is material under consideration and NEVER overturns a
        decision on its own — if a newer source conflicts with a decision
        note, report the tension, don't promote the proposal. READ THE
        `freshness` BLOCK: when it reports sources newer than your newest
        hit, the vault continues past what you just retrieved — for
        'latest/current' questions, probe further (recent, bases_query with
        latest_only=True, or a narrower search) before treating the result
        as the current state. If `egress.hint` reports withheld notes,
        re-call with a higher max_tier instead of concluding the vault is
        empty. Curated notes (zone brain/) are synthesis; zone raw/ holds
        the newest unprocessed sources — check both for recency-sensitive
        questions."""
        return dispatch("search", {"query": query, "k": k, "max_tier": max_tier}, core=core)

    @server.tool()
    def get(id: str, max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """Fetch one full note by id, egress-filtered. Inspect
        `superseded_by` / `previous_version` / `is_latest_version` on the
        result to walk a version chain ("previous version", "what replaced
        this")."""
        return dispatch("get", {"id": id, "max_tier": max_tier}, core=core)

    @server.tool()
    def recent(n: int = 10, max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """List the most recently created/updated notes, egress-filtered —
        the cheapest way to see what entered the vault lately (use after a
        search whose freshness block reported newer sources)."""
        return dispatch("recent", {"n": n, "max_tier": max_tier}, core=core)

    @server.tool()
    def dossier(query: str, k: int = 12, max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """THE ONE-CALL SWEEP for decision-state questions ("what have we
        decided", "latest decisions", "current state of X"). Returns the
        decision layer and the sources under consideration SEPARATED, with
        each decision carrying a `tensions` list — newer sources that
        post-date it (report the tension, never promote the proposal) —
        and retired versions already excluded. Prefer this over plain
        search for decision-state questions; fall back to search/get for
        everything else."""
        return dispatch("dossier", {"query": query, "k": k, "max_tier": max_tier}, core=core)

    @server.tool()
    def bases_query(where: dict | None = None, k: int = 50,
                    latest_only: bool = False, as_of: str = "",
                    max_tier: str = cls.DEFAULT_MAX_TIER) -> dict:
        """Structured frontmatter query (no embedding), egress-filtered.
        `where` filters exact frontmatter keys (e.g. {"type": "decision"}).
        TEMPORAL ROUTING: for "what's current/latest" use latest_only=True
        (excludes superseded notes); for "as of <date>" pass
        as_of="YYYY-MM-DD" (point-in-time view). Prefer this over semantic
        search when the question is really about time or note metadata."""
        return dispatch("bases_query", {
            "where": where or {}, "k": k, "latest_only": latest_only,
            "as_of": as_of or None, "max_tier": max_tier}, core=core)

    server.run()


if __name__ == "__main__":  # pragma: no cover
    serve()
