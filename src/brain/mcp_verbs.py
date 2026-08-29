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
#: The verbs the untrusted VM leg may dispatch through the broker. It grows
#: ONLY with verbs the s01 survey classifies `add_read` — a verb `cli.VM_ALLOWED`
#: already trusts that leg with over the CLI (`vault_languages` predates this
#: rule and is MCP-only: no such verb exists in `cli.py`, so it is not in
#: `cli.VM_ALLOWED`, and the survey lists it under `mcp_registered_tools`
#: rather than scoring it as a CLI verb).
#: The 28 `host_only_never` verbs
#: (`write`, `rebuild`, `maintain`, `ingest`, `verify-audit`, `sync`, `snapshot`,
#: `anchor`, …) must never be admitted here to close a "gap": that inverts the
#: trust split the closed-stacks plan exists to build. See
#: docs/operations/cowork-skill-verb-survey.json.
#:
#: This is the READ set specifically. A future write-adjacent verb (s04's
#: capture is a quasi-write) gets its own set and its own gate — see
#: :data:`brain.mcp_adapter.WRITE_TOOLS` — never a quiet admission here.
#:
#: ``alerts``/``exceptions``/``inbox`` (S03, DESK-03) are reads too, but of
#: HOUSEKEEPING status, never of note bodies — they carry no ``max_tier`` and
#: are already CLI ``VM_ALLOWED`` (``alerts``, ``exceptions``) or deliberately
#: NOT a CLI verb at all (``inbox`` is MCP-only, an alias of ``exceptions`` —
#: see :func:`dispatch_exceptions`).
VM_READ_ALIASES = frozenset(
    {"search", "hybrid-search", "hybrid_search", "get", "read", "recent",
     "bases-query", "bases_query", "dossier", "vault_languages",
     "vault-languages", "grep", "graph-expand", "graph_expand", "diagnose",
     "alerts", "exceptions", "inbox"}
)

#: The WRITE-adjacent verbs the VM leg may dispatch. ``capture`` (S04,
#: DESK-04) is the first member — a quasi-write (AGENTS.md §5), so it belongs
#: HERE, never quietly inside :data:`VM_READ_ALIASES`. The role gate in
#: ``mcp_adapter.dispatch`` BRANCHES on this set before it consults
#: :data:`VM_READ_ALIASES` — membership here takes the write branch, defined
#: in :mod:`brain.mcp_capture_verbs` (host-side staging, the untrusted-author
#: sanitisation already shared with ``brain write --untrusted-author``, and a
#: distinct write-shaped read-log ``cmd`` prefix, ``mcp:capture``). So
#: admitting a write-adjacent verb is one explicit line in one obvious place
#: and every assertion about what is a READ stays true for everything else.
VM_WRITE_ALIASES: frozenset[str] = frozenset({"capture"})


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
    include_retired = bool(args.get("include_retired", False))
    if len(variants) > 1:
        hits = [
            hit.to_dict()
            for hit in core.search_multi(
                variants,
                k=int(args.get("k", 10)),
                rerank=use_rerank,
                rerank_top=rerank_top,
                include_retired=include_retired,
            )
        ]
    elif capture_enabled:
        trace_hits, trace = core.hybrid_search_with_trace(
            str(args["query"]),
            k=int(args.get("k", 10)),
            rerank=use_rerank,
            rerank_top=rerank_top,
            include_retired=include_retired,
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
                include_retired=include_retired,
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
    """Run the MCP get/read body.

    RECORDED RESIDUAL, not closed here: the ``egress`` counters differ between a
    real above-ceiling id and one that does not exist, so this is an existence
    oracle on a caller-chosen target. Deferred with its parity reason in
    ``docs/operations/cowork-skill-verb-survey.json``
    (``s02_update.adversarial_review_2026_08_28.recorded_not_changed[0]``);
    pinned by ``tests/test_mcp_tools_retrieval.py::test_read_withholds_an_above_ceiling_note``.
    """
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
        core.recent(
            limit=int(args.get("n", 10)),
            include_retired=bool(args.get("include_retired", False)),
        ),
        max_tier,
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
    egress.gate_dossier_tensions(decisions, sources)
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


def dispatch_grep(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the MCP grep body — the same shape `brain grep --json` returns.

    ``max_tier`` is passed INTO the scan, not only applied after it. grep is the
    only read verb whose pattern the caller chooses, so filtering the matches
    afterwards leaves an oracle: a caller can binary-search an above-ceiling
    body from how many rows disappeared and how the survivors reordered, without
    ever receiving the body. Dropping those notes before matching makes an
    above-ceiling note indistinguishable from one that is not there — so the
    ``egress`` block on this surface reports ``withheld: 0`` by construction,
    and that zero is the refusal shape, not a claim that nothing was hidden.
    :func:`_filtered` still runs behind it: the pre-drop is the property, the
    gate is the chokepoint, and dropping either would be a regression.
    """
    items = core.grep(
        str(args["pattern"]),
        k=int(args.get("k", 20)),
        regex=bool(args.get("regex", False)),
        max_tier=max_tier,
    )
    surfaced, report = _filtered(items, max_tier)
    return {"pattern": str(args["pattern"]), "results": surfaced, "egress": report}


def dispatch_graph_expand(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the MCP graph-expand body — DISCOVERY-ONLY (RET-03).

    Mirrors ``cli_cmds/navigation._run_graph_expand``, with ONE deliberate
    difference: the ceiling goes INTO the expansion (``max_tier=``), so
    above-ceiling notes never enter the graph, never resolve as a seed and
    never traverse. The CLI still gates afterwards.

    That difference is not cosmetic. Gating only the ``results`` list left an
    existence oracle, and it was open: seeding a guessed MNPI id came back as
    ``resolved_seeds: ['<that id>']`` while a nonsense id came back under
    ``unresolved_seeds``, so an Internal-capped caller could confirm a note it
    may not read by guessing its id (found by adversarial review 2026-08-28,
    reproduced before the fix). The seed echo is the same shape either way now.
    ``_filtered`` still runs below, and on this surface it reports ``withheld:
    0`` by construction — the notes were gone before ranking, exactly like
    ``grep``.
    """
    seeds = args.get("seeds") or []
    if isinstance(seeds, str):
        seeds = [seeds]
    result = core.graph_expand(
        [str(s) for s in seeds],
        depth=int(args.get("depth", 2)),
        k=int(args.get("k", 10)),
        use_ppr=bool(args.get("use_ppr", True)),
        use_inferred=bool(args.get("use_inferred", False)),
        max_tier=max_tier,
    )
    surfaced, report = _filtered(result.get("results", []), max_tier)
    result["results"] = surfaced
    result["egress"] = report
    return result


def dispatch_diagnose(
    _tool: str,
    args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the MCP diagnose body — the ADR-0008 target miss tracer.

    Mirrors ``cli_cmds/retrieval._run_diagnose`` including its withheld
    response: when the target is above the cap the payload carries the
    sentinel and the aggregate gate count, never the query or the trace.

    STATED LIMIT, because "withheld" reads stronger than it is: that aggregate
    count is query-dependent, so it is a NARROW existence channel that survives
    the refusal — a caller can watch ``egress.withheld`` move with a pattern it
    chose. It is left as-is deliberately. The counter is the engine-wide
    elevation contract (``egress.hint``, AGENTS.md "a starved result means
    elevate, not give up"), shared with the CLI and with ``search``; removing
    it here alone would break the CLI-shape parity DESK-02 exists to keep and
    would decide an engine-wide egress question inside one verb. Recorded for
    S06's whole-surface pass.

    NOT a property of ranked retrieval. ``grep`` and ``graph-expand`` are the
    two verbs WITHOUT this channel, because they drop above-ceiling notes
    pre-match. Every other content verb has it, single-id ``get``/``read`` and
    ``bases_query`` included; the survey record
    (``docs/operations/cowork-skill-verb-survey.json``,
    ``s02_update.adversarial_review_2026_08_28.recorded_not_changed[0]``) names
    them and carries the deferral.
    """
    query = str(args["query"])
    target = str(args["target"])
    trace_hits, trace = core.hybrid_search_with_trace(
        query,
        k=int(args.get("k", 10)),
        rerank=bool(args.get("rerank", False)),
        rerank_top=int(args.get("rerank_top", RERANK_TOP_DEFAULT)),
        rrf_k=int(args.get("rrf_k", 60)),
    )
    hits = [hit.to_dict() for hit in trace_hits]
    surfaced, report = _filtered(hits, max_tier)
    core.annotate_create_safety(query, surfaced, max_tier)
    final_ranks = {hit["id"]: rank for rank, hit in enumerate(surfaced, start=1)}
    diagnosis = core.diagnose_target(
        query, target, max_tier=max_tier, trace=trace,
        final_rank=final_ranks.get(target),
    )
    payload = {**diagnosis, "egress": report}
    if diagnosis.get("verdict") != "withheld":
        payload = {"query": query, **payload}
    return payload


# Housekeeping verbs (closed-stacks S03, DESK-03: ``alerts``, ``exceptions``,
# ``inbox``) live in :mod:`brain.mcp_housekeeping_verbs`, a sibling leaf
# module split out when adding them here crossed the file-size ratchet — see
# that module's docstring.
