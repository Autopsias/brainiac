"""Execute multi-variant retrieval fan-out."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, replace
from typing import Any

from . import frontmatter
from .index import Hit, rerank_gate_enabled


# OUTER pooling controls for RET-05.  They remain re-exported by brain.core
# because the CLI help and eval harness import them from that facade.
MULTI_RRF_K = 60
MULTI_MAX_VARIANTS = 4
MULTI_GUARD_STRONG_RANK = 3


@dataclass
class FanoutPlan:
    """Resolved guards and trace state for one RET-05 fan-out."""

    variants: list[str]
    fanout_k: int
    per_query_k: int
    guard_enabled: bool
    rerank_fused: bool
    trace: dict[str, Any]


def _env_switch(name: str, default: bool) -> bool:
    """Read a boolean kill switch with the exact-leg environment contract."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive integer, warning and falling back on invalid input."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        print(
            f"brain: ignoring {name}={raw!r} (want a positive integer); using {default}",
            file=sys.stderr,
        )
        return default
    return value


def _prepare_fanout(
    core: Any,
    queries: list[str],
    *,
    k: int,
    rrf_k: int,
    per_query_k: int | None,
    rerank_fused: bool | None,
    rerank_gate: bool | None,
    fanout_k: int | None,
    max_variants: int | None,
    guard: bool | None,
) -> FanoutPlan:
    """Normalize variants and resolve every RET-05 guard before searching."""
    asked = [query for query in (queries or []) if query and query.strip()]
    variants: list[str] = []
    dropped_duplicate: list[str] = []
    seen: set[str] = set()
    for query in asked:
        key = frontmatter.normalize_identity(query) or query.strip()
        if key in seen:
            dropped_duplicate.append(query)
            continue
        seen.add(key)
        variants.append(query)

    disabled = not _env_switch("BRAIN_VARIANTS_ENABLED", True)
    dropped_disabled = variants[1:] if disabled else []
    if disabled:
        variants = variants[:1]
    cap = (
        max_variants
        if max_variants is not None
        else _env_positive_int("BRAIN_MULTI_MAX_VARIANTS", MULTI_MAX_VARIANTS)
    )
    dropped_over_cap = variants[cap:]
    variants = variants[:cap]
    outer_k = (
        fanout_k
        if fanout_k is not None
        else _env_positive_int("BRAIN_MULTI_RRF_K", MULTI_RRF_K)
    )
    guard_enabled = (
        guard if guard is not None else _env_switch("BRAIN_MULTI_GUARD", False)
    )
    rerank_fused_auto = rerank_fused is None
    if rerank_fused_auto:
        rerank_fused = len(variants) > 1 and not _env_switch(
            "BRAIN_RERANK_FUSED_DISABLED", False
        )
    query_k = per_query_k or max(k, 20)
    trace: dict[str, Any] = {
        "variants": list(variants),
        "variant_count": len(variants),
        "dropped": {
            "duplicate": dropped_duplicate,
            "over_cap": dropped_over_cap,
            "kill_switch": dropped_disabled,
            "max_variants": cap,
        },
        "fanout_k": outer_k,
        "inner_rrf_k": rrf_k,
        "exact_leg_enabled": core.index._exact_leg_enabled(rrf_k),
        "per_query_k": query_k,
        "guard": {
            "enabled": guard_enabled,
            "strong_rank": MULTI_GUARD_STRONG_RANK,
            "demoted": [],
        },
        "rerank_fused": bool(rerank_fused),
        "rerank_fused_source": "auto" if rerank_fused_auto else "caller",
        "rerank_gate": {
            "enabled": rerank_gate_enabled(rerank_gate),
            "skipped": False,
            "reason": "rerank_fused_off",
        },
        "pin": {"id": None, "applied": False, "source": "original_query"},
        "per_variant": [],
        "contributions": {},
    }
    return FanoutPlan(
        variants=variants,
        fanout_k=outer_k,
        per_query_k=query_k,
        guard_enabled=guard_enabled,
        rerank_fused=bool(rerank_fused),
        trace=trace,
    )


def _collect_variant_hits(
    core: Any,
    plan: FanoutPlan,
    *,
    rerank: bool,
    rerank_top: int,
    rrf_k: int,
    rerank_gate: bool | None,
) -> tuple[dict[str, list[Any]], Hit | None]:
    """Run each prepared variant and accumulate outer-RRF contributions."""
    fused: dict[str, list[Any]] = {}
    original_top: Hit | None = None
    for variant_index, query in enumerate(plan.variants):
        started = time.perf_counter()
        hits = core.hybrid_search(
            query,
            k=plan.per_query_k,
            rerank=rerank,
            rerank_top=rerank_top,
            rrf_k=rrf_k,
            rerank_gate=rerank_gate,
        )
        plan.trace["per_variant"].append(
            {
                "index": variant_index,
                "query": query,
                "returned": len(hits),
                "order": [hit.id for hit in hits],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )
        if variant_index == 0 and hits:
            original_top = hits[0]
        for rank, hit in enumerate(hits, start=1):
            contribution = 1.0 / (plan.fanout_k + rank)
            plan.trace["contributions"].setdefault(hit.id, []).append(
                {"variant": variant_index, "rank": rank, "contribution": contribution}
            )
            current = fused.get(hit.id)
            if current is None:
                fused[hit.id] = [contribution, hit, rank, variant_index == 0]
            else:
                current[0] += contribution
                current[2] = min(current[2], rank)
                current[3] = current[3] or variant_index == 0
    return fused, original_top


def _rank_variant_hits(plan: FanoutPlan, fused: dict[str, list[Any]]) -> list[Hit]:
    """Apply the correlated-vote band and stamp the outer-fusion order."""
    entries = list(fused.values())
    if plan.guard_enabled:
        before = [entry[1].id for entry in sorted(entries, key=lambda item: -item[0])]
        ranked = sorted(
            entries,
            key=lambda item: (item[2] > MULTI_GUARD_STRONG_RANK, -item[0]),
        )
        after = [entry[1].id for entry in ranked]
        plan.trace["guard"]["demoted"] = [
            old for old, new in zip(before, after) if old != new
        ]
    else:
        ranked = sorted(entries, key=lambda item: -item[0])
    return [
        replace(
            hit,
            score=score,
            create_safety=(
                "probable"
                if not from_original and hit.create_safety == "exists"
                else hit.create_safety
            ),
        )
        for score, hit, _best_rank, from_original in ranked
    ]


def _pin_original_identity(
    plan: FanoutPlan, fused_hits: list[Hit], original_top: Hit | None
) -> list[Hit]:
    """Preserve the original query's unique-identity rank-one guarantee."""
    pin_id = (
        original_top.id
        if original_top is not None
        and original_top.create_safety == "exists"
        and original_top.evidence in {"alias_hit", "exact_title_match"}
        else None
    )
    plan.trace["pin"]["id"] = pin_id
    if pin_id is None or not fused_hits or fused_hits[0].id == pin_id:
        return fused_hits
    for position, hit in enumerate(fused_hits):
        if hit.id == pin_id:
            top_score = fused_hits[0].score + 1.0 / (plan.fanout_k + 1)
            fused_hits.insert(0, replace(fused_hits.pop(position), score=top_score))
            plan.trace["pin"]["applied"] = True
            break
    return fused_hits


def _rerank_pooled_hits(
    core: Any, plan: FanoutPlan, hits: list[Hit], fused_pool: int
) -> list[Hit]:
    """Rerank the pooled candidates unless the pooled identity pin decides it."""
    if not plan.rerank_fused or not hits:
        return hits
    pooled_pin = hits[0].create_safety == "exists" and hits[0].evidence in {
        "alias_hit",
        "exact_title_match",
    }
    gate_enabled = plan.trace["rerank_gate"]["enabled"]
    skip = pooled_pin and gate_enabled
    plan.trace["rerank_gate"] = {
        "enabled": gate_enabled,
        "skipped": skip,
        "reason": (
            "pooled_unique_identity_pin"
            if skip
            else "gate_disabled"
            if not gate_enabled
            else "no_pooled_unique_identity_pin"
        ),
    }
    if skip:
        return hits
    reranked = core.index._apply_rerank(plan.variants[0], hits, None, fused_pool)
    count = len(reranked)
    return [
        replace(hit, score=float(count - index)) for index, hit in enumerate(reranked)
    ]


class RetrievalOpsMixin:
    """Provide BrainCore's RET-05 multi-query retrieval operation."""

    def search_multi(
        self,
        queries: list[str],
        k: int = 10,
        *,
        rerank: bool = False,
        rerank_top: int = 15,
        rrf_k: int = 60,
        per_query_k: int | None = None,
        rerank_fused: bool | None = None,
        fused_pool: int = 20,
        rerank_gate: bool | None = None,
        fanout_k: int | None = None,
        max_variants: int | None = None,
        guard: bool | None = None,
        return_trace: bool = False,
    ) -> list[Hit] | tuple[list[Hit], dict[str, Any]]:
        """Fuse guarded query variants through RET-05's outer RRF layer.

        The caller supplies ordered reformulations. The original query alone may
        retain ADR-0008's exact-identity pin; later variants are recall aids and
        cannot claim ``create_safety=exists``. See
        ``docs/operations/s10-agentic-retrieval-analysis.md`` and ENG-02.
        """
        plan = _prepare_fanout(
            self,
            queries,
            k=k,
            rrf_k=rrf_k,
            per_query_k=per_query_k,
            rerank_fused=rerank_fused,
            rerank_gate=rerank_gate,
            fanout_k=fanout_k,
            max_variants=max_variants,
            guard=guard,
        )

        def done(hits: list[Hit]) -> list[Hit] | tuple[list[Hit], dict[str, Any]]:
            return (hits, plan.trace) if return_trace else hits

        if not plan.variants:
            return done([])
        if len(plan.variants) == 1:
            return done(
                self.hybrid_search(
                    plan.variants[0],
                    k=k,
                    rerank=rerank,
                    rerank_top=rerank_top,
                    rrf_k=rrf_k,
                    rerank_gate=rerank_gate,
                )
            )
        fused, original_top = _collect_variant_hits(
            self,
            plan,
            rerank=rerank,
            rerank_top=rerank_top,
            rrf_k=rrf_k,
            rerank_gate=rerank_gate,
        )
        hits = _rank_variant_hits(plan, fused)
        hits = _pin_original_identity(plan, hits, original_top)
        hits = _rerank_pooled_hits(self, plan, hits, fused_pool)
        final = hits[:k]
        kept_ids = {hit.id for hit in final}
        plan.trace["contributions"] = {
            note_id: contribution
            for note_id, contribution in plan.trace["contributions"].items()
            if note_id in kept_ids
        }
        return done(final)
