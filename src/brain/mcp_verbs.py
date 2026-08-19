"""Implement MCP read-verb bodies."""
from __future__ import annotations

import os
import time
from typing import Any

from . import classification as cls
from . import egress
from . import querylog
from .rerank import RERANK_TOP_DEFAULT, rerank_enabled


EGRESS_CEILING_ENV_VAR = "BRAIN_MAX_EGRESS_TIER"
# The ceiling for the HOST MCP transport is the full vault (owner ruling
# 2026-08-17). `brain-mcp` runs on the host, as the owner, over a
# single-owner vault — the same trust context the CLI already resolves in
# full since 2026-07-10. Borrowing the VM leg's conservative tier here was
# measured starvation: Confidential/Restricted notes are where a curated
# vault keeps its substance, so Desktop and Cowork's MCP-on-host path saw
# only scraps. An operator narrows it back with $BRAIN_MAX_EGRESS_TIER; the
# clamp below still binds whatever that resolves to.
DEFAULT_EGRESS_CEILING_TIER = cls.HOST_MCP_DEFAULT_MAX_TIER
VM_READ_ALIASES = frozenset(
    {"search", "hybrid-search", "get", "read", "recent", "bases-query",
     "bases_query", "dossier", "vault_languages", "vault-languages"}
)


def _egress_ceiling_tier() -> str:
    """The operator-configured hard ceiling for MCP egress.

    UNSET means the shipped default: the full vault, same as the host CLI
    (owner ruling 2026-08-17). A SET-BUT-UNRECOGNISED value is different and
    stays fail-CLOSED at the conservative tier: the only reason to set this
    var is to NARROW the gate, so a typo must never silently hand back more
    than the operator asked for — which is exactly what falling back to the
    permissive default would do."""
    raw = os.environ.get(EGRESS_CEILING_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_EGRESS_CEILING_TIER
    return raw if raw in cls.RANK else cls.VM_DEFAULT_MAX_TIER


def _clamp_max_tier(requested_tier: str) -> str:
    """Clamp a valid request to the configured MCP ceiling."""
    requested = requested_tier.strip()
    if requested not in cls.RANK:
        return requested_tier
    ceiling_rank = cls.RANK[_egress_ceiling_tier()]
    clamped_rank = min(cls.RANK[requested], ceiling_rank)
    return cls.TIERS[clamped_rank]


def _filtered(
    items: list[dict[str, Any]], max_tier: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the shared egress gate to MCP-visible rows."""
    return egress.apply_gate(items, max_tier)


def _variant_queries(args: dict[str, Any]) -> list[str]:
    """Return the original query plus unique, ordered query variants."""
    raw = args.get("variants") or []
    if isinstance(raw, str):
        raw = [raw]
    queries: list[str] = []
    seen: set[str] = set()
    for query in [args.get("query", "")] + list(raw):
        text = str(query or "").strip()
        key = " ".join(text.lower().split())
        if text and key not in seen:
            seen.add(key)
            queries.append(text)
    return queries


def _capture_rerank_metadata(
    core: Any, trace: Any | None, *, requested: bool, rerank_top: int,
) -> dict[str, Any]:
    """Record the ranking mode used by the MCP search body."""
    applied = bool(getattr(trace, "rerank_applied", False))
    model = None
    if applied:
        cache = getattr(getattr(core, "index", None), "_reranker_cache", None)
        if isinstance(cache, tuple) and len(cache) == 2:
            model = getattr(cache[1], "model_id", None) or cache[0]
    return {
        "requested": requested,
        "applied": applied,
        "model": str(model) if model else None,
        "top_n": rerank_top if applied else 0,
    }


def dispatch_search(
    tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the MCP search body, including gated capture projection."""
    role = getattr(core, "role", "host")
    capture_enabled = querylog.capture_requested(role)
    started = time.perf_counter() if capture_enabled else None
    trace = None
    use_rerank = rerank_enabled()
    rerank_top = RERANK_TOP_DEFAULT
    variants = _variant_queries(args)
    if len(variants) > 1:
        hits = [
            hit.to_dict()
            for hit in core.search_multi(
                variants,
                k=int(args.get("k", 10)),
                rerank=use_rerank,
                rerank_top=rerank_top,
            )
        ]
    elif capture_enabled:
        trace_hits, trace = core.hybrid_search_with_trace(
            str(args["query"]),
            k=int(args.get("k", 10)),
            rerank=use_rerank,
            rerank_top=rerank_top,
        )
        hits = [hit.to_dict() for hit in trace_hits]
    else:
        hits = [
            hit.to_dict()
            for hit in core.hybrid_search(
                str(args["query"]),
                k=int(args.get("k", 10)),
                rerank=use_rerank,
                rerank_top=rerank_top,
            )
        ]
    surfaced, report = _filtered(hits, max_tier)
    redacted_ids = core.annotate_create_safety(
        str(args["query"]), surfaced, max_tier,
    )
    output: dict[str, Any] = {"results": surfaced, "egress": report}
    if len(variants) > 1:
        output["variants"] = {"issued": variants, "fanout": True}
    dates = [hit.get("date", "") for hit in surfaced if hit.get("date")]
    if dates:
        try:
            freshness = core.source_freshness(max(dates), max_tier)
        except Exception:  # noqa: BLE001 — freshness must never break search
            freshness = None
        if freshness and freshness.get("newer_count", 0) > 0:
            freshness["hint"] = (
                f"{freshness['newer_count']} note(s)/source(s) are newer than your "
                f"newest hit ({freshness['newest_hit_date']}; vault newest "
                f"{freshness['vault_newest']}) — for 'latest/current' questions, "
                f"probe past these hits (recent, bases_query latest_only=True, "
                f"or a narrower search) before treating this as current."
            )
        if freshness:
            output["freshness"] = freshness
    if capture_enabled and started is not None:
        capture_top, capture_digest = querylog.projection_from_gated(
            surfaced, trace=trace, redacted_ids=redacted_ids,
        )
        querylog.capture_post_egress(
            vault=core.vault,
            role=role,
            index=core.index,
            query=str(args["query"]),
            mode=tool,
            k=int(args.get("k", 10)),
            rrf_k=60,
            exact_leg_enabled=bool(getattr(trace, "exact_leg_enabled", False)),
            rerank=_capture_rerank_metadata(
                core, trace, requested=use_rerank, rerank_top=rerank_top,
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            top=capture_top,
            candidate_digest=capture_digest,
            max_tier=max_tier,
        )
    return output


def dispatch_note(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the MCP get/read body."""
    note = core.get(str(args["id"]))
    surfaced, report = _filtered([note] if note else [], max_tier)
    return {"result": surfaced[0] if surfaced else None, "egress": report}


def dispatch_recent(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the MCP recent body."""
    surfaced, report = _filtered(
        core.recent(limit=int(args.get("n", 10))), max_tier,
    )
    return {"results": surfaced, "egress": report}


def dispatch_dossier(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the MCP decision-state dossier body."""
    role = getattr(core, "role", "host")
    capture_enabled = querylog.capture_requested(role)
    started = time.perf_counter() if capture_enabled else None
    result = core.dossier(str(args["query"]), k=int(args.get("k", 12)))
    decisions, decision_report = _filtered(result["decisions"], max_tier)
    sources, source_report = _filtered(result["sources"], max_tier)
    core.annotate_create_safety(str(args["query"]), decisions + sources, max_tier)
    report: dict[str, Any] = {
        key: decision_report[key] + source_report[key]
        for key in (
            "total", "surfaced", "withheld", "withheld_unlabelled_default_deny",
        )
    }
    report["max_tier"] = decision_report["max_tier"]
    casing = sorted(
        set(decision_report.get("casing_mismatch_warnings", []))
        | set(source_report.get("casing_mismatch_warnings", []))
    )
    if casing:
        report["casing_mismatch_warnings"] = casing
    output: dict[str, Any] = {
        "query": result["query"],
        "decisions": decisions,
        "sources": sources,
        "retired_excluded": result["retired_excluded"],
        "egress": report,
    }
    if capture_enabled and started is not None:
        capture_top, capture_digest = querylog.projection_from_gated(
            decisions + sources,
        )
        querylog.capture_post_egress(
            vault=core.vault,
            role=role,
            index=core.index,
            query=str(args["query"]),
            mode="dossier",
            k=int(args.get("k", 12)),
            rrf_k=60,
            exact_leg_enabled=os.environ.get(
                "BRAIN_EXACT_LEG_ENABLED", "1",
            ).strip().lower() not in {"0", "false", "no", "off"},
            rerank={"requested": False, "applied": False, "model": None, "top_n": 0},
            latency_ms=(time.perf_counter() - started) * 1000,
            top=capture_top,
            candidate_digest=capture_digest,
            max_tier=max_tier,
        )
    return output


def dispatch_vault_languages(
    _tool: str,
    _args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the aggregate language-census body."""  # noqa: ARG001
    return {"languages": core.index.language_census()}


def dispatch_bases_query(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the structured frontmatter query body."""
    filters = dict(args.get("where") or {})
    items = core.bases_query(
        filters,
        k=int(args.get("k", 50)),
        latest_only=bool(args.get("latest_only", False)),
        as_of=args.get("as_of") or None,
    )
    surfaced, report = _filtered(items, max_tier)
    return {"results": surfaced, "egress": report}
