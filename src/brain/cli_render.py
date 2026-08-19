"""Render helpers for the CLI read surfaces (egress footer, variant/explain blocks)."""
from __future__ import annotations

from typing import Any

def _egress_footer(report: dict) -> str:
    """The `-- N/M surfaced; K withheld` line, plus the elevation hint when the
    gate withheld anything (RET-08). One renderer so every read surface nudges
    identically."""
    line = (
        f"-- {report['surfaced']}/{report['total']} surfaced; "
        f"{report['withheld']} withheld (max-tier={report['max_tier']})"
    )
    if report.get("hint"):
        line += f"\n-- {report['hint']}"
    return line


def _variant_block(fanout: dict, allowed_ids: set[str], *, explain: bool) -> dict:
    """Project a RET-05 fan-out trace onto the ALREADY-GATED result.

    The trace is built pre-egress, so every id in it (per-variant orders,
    per-variant contributions, the pin) is filtered to ``allowed_ids`` here — a
    withheld note must not leak through the fan-out attribution any more than
    through the ranking itself. Variant TEXTS are the caller's own input, never
    vault content, so they are echoed verbatim.
    """
    dropped = {
        key: value
        for key, value in fanout["dropped"].items()
        if key == "max_variants" or value
    }
    block = {
        "used": fanout["variants"],
        "count": fanout["variant_count"],
        "dropped": dropped,
    }
    if not explain:
        return block
    block.update(
        {
            # Both constants, side by side: the outer one is what this layer pools
            # at, the inner one is ADR-0008's pin that gates the exact leg.
            "fanout_k": fanout["fanout_k"],
            "inner_rrf_k": fanout["inner_rrf_k"],
            "exact_leg_enabled": fanout["exact_leg_enabled"],
            "per_query_k": fanout["per_query_k"],
            "guard": fanout["guard"],
            "rerank_fused": fanout["rerank_fused"],
            "rerank_fused_source": fanout["rerank_fused_source"],
            "rerank_gate": fanout["rerank_gate"],
            # A pin is only ever the ORIGINAL query's unique identity owner, and it
            # is named here only when that note also survived egress.
            "pin": (
                fanout["pin"]
                if fanout["pin"]["id"] in allowed_ids
                else {**fanout["pin"], "id": None}
            ),
            "per_variant": [
                {**entry, "order": [i for i in entry["order"] if i in allowed_ids]}
                for entry in fanout["per_variant"]
            ],
            "contributions": {
                note_id: [
                    {**c, "contribution": round(c["contribution"], 6)} for c in contribs
                ]
                for note_id, contribs in fanout["contributions"].items()
                if note_id in allowed_ids
            },
        }
    )
    return block


def _render_variant_block(block: dict) -> list[str]:
    """The text-mode fan-out footer — what ran, and what was dropped."""
    lines = [
        f"-- fan-out: {block['count']} variant(s): "
        + "; ".join(repr(v) for v in block["used"])
    ]
    for key in ("duplicate", "over_cap", "kill_switch"):
        dropped = block["dropped"].get(key)
        if dropped:
            lines.append(f"-- dropped ({key}): " + "; ".join(repr(v) for v in dropped))
    if "fanout_k" in block:
        guard = block["guard"]
        gate = block["rerank_gate"]
        lines.append(
            f"-- pooled at fanout_k={block['fanout_k']} "
            f"(inner rrf_k={block['inner_rrf_k']}, "
            f"exact_leg={'on' if block['exact_leg_enabled'] else 'off'}, "
            f"per_query_k={block['per_query_k']}); "
            f"guard={'on' if guard['enabled'] else 'off'}; "
            f"rerank_fused={'on' if block['rerank_fused'] else 'off'} "
            f"[{block.get('rerank_fused_source', 'caller')}] "
            f"(gate: {gate['reason']}); pin={block['pin']['id']}"
        )
        for note_id, contribs in block["contributions"].items():
            votes = " ".join(f"v{c['variant']}@{c['rank']}" for c in contribs)
            lines.append(f"--   {note_id}: {votes}")
    return lines


def _render_explain_hit(hit: dict) -> list[str]:
    """Readable ADR-0008 attribution for one already-gated search result."""
    explain = hit.get("explain") or {}
    lines = [
        f"[{hit.get('source', '?')}] {hit.get('id', '?')}  "
        f"final-rank={explain.get('final_rank')}  "
        f"pre-rerank={explain.get('pre_rerank_score')}"
    ]
    for name in ("lexical", "dense", "exact"):
        leg = explain.get(name)
        if leg is None:
            lines.append(f"  {name}: not available")
            continue
        details = " ".join(f"{key}={value}" for key, value in leg.items())
        lines.append(f"  {name}: {details}")
    zone = explain.get("zone", {})
    stale = explain.get("staleness", {})
    duplicate = explain.get("near_duplicate", {})
    pin = explain.get("pin", {})
    lines.append(
        "  raw_rrf={raw} zone={zone} (applied={applied}, scope={scope}) "
        "staleness={staleness}".format(
            raw=explain.get("raw_rrf_score"),
            zone=zone.get("factor"),
            applied=zone.get("applied"),
            scope=zone.get("scope"),
            staleness=stale.get("factor"),
        )
    )
    lines.append(
        "  duplicate: exempt={exempt} suppressed={suppressed}; "
        "pin: eligible={eligible} applied={applied}".format(
            exempt=duplicate.get("exempt"),
            suppressed=duplicate.get("suppressed"),
            eligible=pin.get("eligible"),
            applied=pin.get("applied"),
        )
    )
    if explain.get("rerank_score") is None:
        lines.append("  rerank: not scored (no numeric score is combined with RRF)")
    else:
        lines.append(
            f"  rerank: score={explain['rerank_score']} rank={explain['rerank_rank']} "
            "(separate cross-encoder scale)"
        )
    lines.append(f"    {hit.get('snippet', '')}")
    return lines


def _render_diagnose(diag: dict, report: dict) -> str:
    """Readable target-miss result without exposing a withheld target."""
    if diag.get("verdict") == "withheld":
        return _egress_footer(report) + "\nVERDICT: withheld by egress gate"
    trace = diag.get("trace", {})
    lines = [f"target: {diag.get('target')}"]
    for name, stage in trace.get("stages", {}).items():
        lines.append(
            f"  {name}: candidate={stage.get('candidate')} rank={stage.get('rank')} "
            f"matched={stage.get('matched')} cutoff={stage.get('cutoff')}"
        )
    if trace.get("first_missed_cutoff"):
        cutoff = trace["first_missed_cutoff"]
        lines.append(
            f"  first missed cutoff: {cutoff['stage']} (limit={cutoff['cutoff']})"
        )
    attribution = trace.get("attribution")
    if attribution:
        lines.extend(
            _render_explain_hit({"id": diag.get("target"), "explain": attribution})
        )
    verdict = diag.get("verdict", "candidate-miss")
    lines.append(_egress_footer(report))
    lines.append(f"VERDICT: {verdict}")
    return "\n".join(lines)


def _capture_rerank_metadata(core: Any, trace: Any | None, args: Any) -> dict[str, Any]:
    """Small, safe record of the rerank mode used for a captured query."""
    requested = bool(getattr(args, "rerank", False))
    applied = bool(getattr(trace, "rerank_applied", False))
    model = None
    if applied:
        cache = getattr(getattr(core, "index", None), "_reranker_cache", None)
        if isinstance(cache, tuple) and len(cache) == 2:
            model = getattr(cache[1], "model_id", None) or cache[0]
    top_n = int(getattr(args, "rerank_top", 0) or 0) if applied else 0
    return {
        "requested": requested,
        "applied": applied,
        "model": str(model) if model else None,
        "top_n": top_n,
    }

# Parent-namespace binds, deferred past this module's own defs.
from .cli import _excluded_note as _excluded_note  # noqa: E402
