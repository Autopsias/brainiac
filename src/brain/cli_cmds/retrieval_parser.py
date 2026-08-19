"""Register ranked retrieval commands."""

from __future__ import annotations

import argparse

from .. import core as core_mod
from ..cli import EPILOG
from .common_parser import add_common


def _add_search(sub, name: str, help_text: str) -> None:
    sp = sub.add_parser(
        name,
        help=help_text,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=10, help="max results (default: 10)")
    sp.add_argument(
        "--rerank",
        dest="rerank",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="re-order the top results with the cross-encoder (RET-02); ON by default (window 20) — skippable, degrades to the pre-rerank order if the model is absent or a call exceeds its timeout budget. --no-rerank opts out per call; BRAIN_RERANK_DISABLED=1 is the global kill switch (mirrors BRAIN_EXACT_LEG_ENABLED) — an explicit --rerank/--no-rerank always wins over the env var",
    )
    sp.add_argument(
        "--rerank-top",
        type=int,
        default=20,
        help="rerank window, clamped to 10-50 by default (BRAIN_RERANK_MAX raises the ceiling further; default: 20 — a wider window costs strongly super-linearly, see eval/FOLLOWUPS.md #6)",
    )
    sp.add_argument(
        "--rerank-gate",
        dest="rerank_gate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="adaptive rerank gate (RK-02): skip the cross-encoder on a query whose rank 1 is already fixed by a unique exact-identity pin, where it is measurably worth nothing. ON by default; --no-rerank-gate (or BRAIN_RERANK_GATE_DISABLED=1) forces unconditional reranking back on",
    )
    sp.add_argument(
        "--rrf-k",
        type=int,
        default=60,
        help="Reciprocal Rank Fusion constant (default: 60)",
    )
    sp.add_argument(
        "--variant",
        action="append",
        default=None,
        metavar="TEXT",
        help=f"an alternative phrasing of the same question (repeatable) — each is searched separately and the result lists are rank-fused into one ranking (RET-05). Use it when the question's words are yours rather than the notes' (a translation, a synonym expansion). Identical variants are deduplicated; variants past {core_mod.MULTI_MAX_VARIANTS} ($BRAIN_MULTI_MAX_VARIANTS) are dropped from the tail and reported; BRAIN_VARIANTS_ENABLED=0 is the kill switch",
    )
    sp.add_argument(
        "--rerank-fused",
        dest="rerank_fused",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --variant: run the cross-encoder ONCE over the pooled candidates against the original query (RET-05b) instead of leaving the fused RRF order. ON by default whenever 2+ variants are supplied, and NEVER on a single query (owner ruling 2026-08-12); --no-rerank-fused opts out per call and BRAIN_RERANK_FUSED_DISABLED=1 is the global kill switch",
    )
    sp.add_argument(
        "--explain",
        action="store_true",
        help="show per-stage RRF/zone/staleness attribution for each egress-surfaced result (ADR-0008)",
    )
    add_common(sp)


def _add_diagnose(sub) -> None:
    sp = sub.add_parser(
        "diagnose",
        help="ADR-0008 target miss tracer: run production search unchanged, then report the target's gated per-stage presence/cutoff; withheld targets print only the `withheld` sentinel",
    )
    sp.add_argument("query")
    sp.add_argument("--target", required=True, help="note id to trace (egress-gated)")
    sp.add_argument(
        "-k", type=int, default=10, help="production max results (default: 10)"
    )
    sp.add_argument(
        "--rerank",
        action="store_true",
        help="diagnose the same cross-encoder rerank path search/hybrid-search run by default (this diagnostic itself stays opt-in)",
    )
    sp.add_argument(
        "--rerank-top",
        type=int,
        default=20,
        help="production rerank window, clamped to 10-50 by default (default: 20 — same as search/hybrid-search)",
    )
    sp.add_argument(
        "--rrf-k",
        type=int,
        default=60,
        help="production Reciprocal Rank Fusion constant (default: 60)",
    )
    add_common(sp)


def _add_dossier(sub) -> None:
    sp = sub.add_parser(
        "dossier",
        help="RET-10: the ONE-CALL retrieval sweep for decision-state questions — decision-layer hits + corroborating sources + TENSIONS (newer sources post-dating a recorded decision) + freshness, with retired versions already excluded. Prefer this over plain search when the question is 'what have we decided / what's the current state'",
    )
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=12, help="max live hits (default: 12)")
    add_common(sp)


def _add_grep(sub) -> None:
    sp = sub.add_parser(
        "grep", help="lexical-first exact/regex scan over notes — NO embedding (RET-04)"
    )
    sp.add_argument("pattern")
    sp.add_argument("-k", type=int, default=20, help="max results (default: 20)")
    sp.add_argument("--regex", action="store_true", help="treat pattern as a regex")
    add_common(sp)


def _add_bases_query(sub) -> None:
    sp = sub.add_parser(
        "bases-query",
        help="structured frontmatter view over indexed columns — NO embedding (RET-04)",
    )
    sp.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="exact-match filter on id/title/type/classification/zone/path (repeatable)",
    )
    sp.add_argument(
        "--latest-only",
        action="store_true",
        help="TMP-02: exclude notes retired via `brain supersede` (is_latest_version: false) — the Latest Only view",
    )
    sp.add_argument(
        "--as-of",
        default=None,
        metavar="YYYY-MM-DD",
        help="TMP-02: point-in-time view — notes valid on this date (effective_date, else document_date, else created; excludes anything superseded by then) — the As Of view",
    )
    sp.add_argument("-k", type=int, default=50, help="max results (default: 50)")
    add_common(sp)


def add_parser(sub) -> None:
    _add_search(
        sub,
        "search",
        "fused RRF(60) BM25 + dense + exact alias/title retrieval — hits carry type/date/is_latest_version/evidence/create_safety; --explain emits gated attribution",
    )
    _add_search(
        sub,
        "hybrid-search",
        "alias of `search`: fused RRF(60) BM25 + dense + exact alias/title leg (ADR-0008)",
    )
    _add_diagnose(sub)
    _add_dossier(sub)
    _add_grep(sub)
    _add_bases_query(sub)
