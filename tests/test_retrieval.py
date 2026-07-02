"""S04 retrieval pipeline: RRF fusion (RET-01), skippable reranker (RET-02),
wikilink-BFS + PPR discovery graph (RET-03), agentic tool surface (RET-04)."""
from __future__ import annotations

import json

import pytest

from brain import cli
from brain.core import BrainCore
from brain.embed import get_embedder
from brain.graph import (
    PROVENANCE,
    build_graph,
    parse_wikilinks,
    personalized_pagerank,
    wikilink_bfs,
)
from brain.index import BrainIndex
from brain.rerank import (
    RERANK_TOP_MAX,
    RERANK_TOP_MIN,
    NoopReranker,
    clamp_rerank_top,
    get_reranker,
)
from brain.vectors import get_backend


def _idx(vault, tmp_path, name="ret"):
    """Backend-independent index: brute-force + offline HashEmbedder (fast)."""
    idx = BrainIndex(
        db_path=tmp_path / f"{name}.sqlite",
        backend=get_backend("brute-force"),
        embedder=get_embedder("hash"),
    )
    idx.rebuild(vault)
    return idx


# --------------------------------------------------------------------------
# Linked fixture (the shared sample_vault has no wikilinks)
# --------------------------------------------------------------------------
def _ln(nid, title, classification, body):
    return (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n"
        f"classification: {classification}\ncreated: 2026-06-27\n"
        f"updated: 2026-06-27\n---\n\n{body}\n"
    )


@pytest.fixture
def linked_vault(tmp_path):
    vault = tmp_path / "lvault"
    (vault / "brain" / "resources").mkdir(parents=True)
    notes = {
        "brain/resources/alpha.md": _ln(
            "alpha", "Alpha", "Internal",
            "Alpha hub about retrieval. See [[beta]] and [[gamma]] and [[secret-deal]]."),
        "brain/resources/beta.md": _ln(
            "beta", "Beta", "Internal", "Beta node links onward to [[delta]]."),
        "brain/resources/gamma.md": _ln(
            "gamma", "Gamma", "Internal", "Gamma node also links to [[delta|the delta]]."),
        "brain/resources/delta.md": _ln(
            "delta", "Delta", "Internal", "Delta leaf about arctic embed and fusion."),
        "brain/resources/secret-deal.md": _ln(
            "secret-deal", "Secret Deal", "Restricted",
            "Restricted negotiation terms; must be withheld at default tier."),
        "brain/resources/island.md": _ln(
            "island", "Island", "Internal", "Disconnected note with no [[links here broken."),
    }
    for rel, text in notes.items():
        (vault / rel).write_text(text, encoding="utf-8")
    return vault


# ===================== RET-01 — RRF fusion =================================
def test_hybrid_search_returns_sourced_hits(sample_vault, tmp_path):
    idx = _idx(sample_vault, tmp_path)
    hits = idx.hybrid_search("arctic embed retrieval", k=10)
    assert hits
    assert all(h.source in ("lexical", "semantic", "both") for h in hits)
    ids = {h.id for h in hits}
    assert "public-overview" in ids or "internal-arch" in ids


def test_rrf_scores_are_fusion_scale_and_ordered(sample_vault, tmp_path):
    idx = _idx(sample_vault, tmp_path)
    hits = idx.hybrid_search("arctic embed retrieval", k=10, rrf_k=60)
    # RRF score is bounded by 2/(rrf_k+1) (appears rank 1 in both lists).
    assert all(h.score <= 2.0 / 61.0 + 1e-9 for h in hits)
    # strictly non-increasing
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_rrf_k_changes_fusion_weighting(sample_vault, tmp_path):
    idx = _idx(sample_vault, tmp_path)
    small = idx.hybrid_search("arctic embed retrieval", k=10, rrf_k=1)
    big = idx.hybrid_search("arctic embed retrieval", k=10, rrf_k=600)
    # A smaller k sharpens top-rank dominance → higher top score than a large k.
    assert small[0].score > big[0].score


def test_rrf_fuses_lexical_only_query(sample_vault, tmp_path):
    # exact hyphenated token present only in internal-arch
    idx = _idx(sample_vault, tmp_path)
    hits = idx.hybrid_search("sqlite-vec fts5", k=10)
    assert any(h.source in ("lexical", "both") for h in hits)


def test_search_delegates_to_hybrid(sample_vault, tmp_path):
    idx = _idx(sample_vault, tmp_path)
    a = [h.id for h in idx.search("arctic embed", k=5)]
    b = [h.id for h in idx.hybrid_search("arctic embed", k=5)]
    assert a == b


# ===================== RET-05 — multi-query fan-out =======================
def test_search_multi_single_query_equals_hybrid(sample_vault, tmp_path):
    """A one-element variant list degrades exactly to hybrid_search."""
    from brain.core import BrainCore

    idx = _idx(sample_vault, tmp_path)
    core = BrainCore(vault=sample_vault, index=idx)
    a = [h.id for h in core.search_multi(["arctic embed"], k=5)]
    b = [h.id for h in core.hybrid_search("arctic embed", k=5)]
    assert a == b


def test_search_multi_empty_and_blank(sample_vault, tmp_path):
    from brain.core import BrainCore

    core = BrainCore(vault=sample_vault, index=_idx(sample_vault, tmp_path))
    assert core.search_multi([]) == []
    assert core.search_multi(["", "   "]) == []


def test_search_multi_fuses_variants_and_orders_by_fused_score(sample_vault, tmp_path):
    """Fan-out returns a UNION of the variants' hits, ranked by fused RRF score
    (descending) — a doc surfaced by either variant can appear, and a doc found
    by BOTH is promoted."""
    from brain.core import BrainCore

    idx = _idx(sample_vault, tmp_path)
    core = BrainCore(vault=sample_vault, index=idx)
    a = {h.id for h in core.hybrid_search("arctic embed retrieval", k=8)}
    b = {h.id for h in core.hybrid_search("sqlite-vec fts5", k=8)}
    fused = core.search_multi(["arctic embed retrieval", "sqlite-vec fts5"], k=8)
    fused_ids = [h.id for h in fused]
    # union membership: every fused hit came from at least one variant
    assert set(fused_ids) <= (a | b)
    # at least one variant-unique doc is represented (genuine fan-out, not just one list)
    assert set(fused_ids) & a and set(fused_ids) & b
    # fused scores are descending (replace() stamped the fused score)
    scores = [h.score for h in fused]
    assert scores == sorted(scores, reverse=True)


def test_search_multi_rerank_fused_preserves_order_through_score(sample_vault, tmp_path):
    """RET-05b: with rerank_fused, the cross-encoder REORDERS the fused pool and
    search_multi must re-stamp strictly-descending scores, so the rerank order
    survives a {path: score} round-trip + re-sort (the eval harness). With the
    NoopReranker (offline default) the order is the fused order, but the scores
    must still be strictly descending and integer-rank-encoded."""
    from brain.core import BrainCore

    core = BrainCore(vault=sample_vault, index=_idx(sample_vault, tmp_path))
    variants = ["arctic embed retrieval", "sqlite-vec fts5"]
    hits = core.search_multi(variants, k=6, rerank_fused=True, fused_pool=20)
    scores = [h.score for h in hits]
    # strictly descending (rank-encoded) so a downstream re-sort can't undo it
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(scores)  # no ties to scramble the order
    # round-trip through {path: score} (what the eval harness does) keeps the order
    rt = sorted(((h.score, h.id) for h in hits), key=lambda t: -t[0])
    assert [i for _, i in rt] == [h.id for h in hits]


# ===================== RET-02 — skippable reranker ========================
def test_clamp_rerank_top():
    assert clamp_rerank_top(5) == RERANK_TOP_MIN == 10
    assert clamp_rerank_top(25) == RERANK_TOP_MAX == 20
    assert clamp_rerank_top(15) == 15


def test_noop_reranker_preserves_order():
    rr = NoopReranker()
    scores = rr.rerank("q", ["a", "b", "c"])
    assert scores == sorted(scores, reverse=True)  # descending => stable no-op


def test_get_reranker_default_is_noop():
    assert get_reranker("noop").model_id == "noop"


def test_rerank_off_equals_noop_on(sample_vault, tmp_path):
    idx = _idx(sample_vault, tmp_path)
    off = [h.id for h in idx.hybrid_search("arctic embed retrieval", k=10, rerank=False)]
    on = [h.id for h in idx.hybrid_search(
        "arctic embed retrieval", k=10, rerank=True, reranker=NoopReranker())]
    assert off == on  # the noop reranker must not change ordering


def test_fake_reranker_reorders_head(sample_vault, tmp_path):
    idx = _idx(sample_vault, tmp_path)

    class ReverseReranker:
        model_id = "reverse"

        def rerank(self, query, passages):
            # ascending scores => stable sort desc reverses the head
            return [float(i) for i in range(len(passages))]

    base = idx.hybrid_search("arctic embed retrieval", k=10, rerank=False)
    rer = idx.hybrid_search(
        "arctic embed retrieval", k=10, rerank=True,
        reranker=ReverseReranker(), rerank_top=10)
    # at least the top differs once a non-identity reranker is applied
    assert [h.id for h in base][:1] != [h.id for h in rer][:1] or len(base) < 2


# ===================== RET-03 — wikilink graph (discovery-only) ===========
def test_parse_wikilinks_variants():
    body = "links [[plain]] and [[target|alias]] and [[note#heading]] and [[a#h|b]]"
    assert parse_wikilinks(body) == ["plain", "target", "note", "a"]


def test_build_graph_edges(linked_vault, tmp_path):
    idx = _idx(linked_vault, tmp_path)
    g = build_graph(idx.conn)
    assert "beta" in g.out["alpha"] and "gamma" in g.out["alpha"]
    assert "alpha" in g.inn["beta"]
    assert "delta" in g.out["beta"] and "delta" in g.out["gamma"]
    # the broken "[[links here" is unresolved, not an edge
    assert "island" in g.unresolved or g.out.get("island", set()) == set()


def test_wikilink_bfs_depth(linked_vault, tmp_path):
    idx = _idx(linked_vault, tmp_path)
    g = build_graph(idx.conn)
    d1 = {d["id"] for d in wikilink_bfs(g, ["alpha"], depth=1)}
    assert {"beta", "gamma", "secret-deal"} <= d1
    assert "delta" not in d1  # delta is 2 hops
    d2 = {d["id"] for d in wikilink_bfs(g, ["alpha"], depth=2)}
    assert "delta" in d2


def test_ppr_ranks_neighbours_above_unreachable(linked_vault, tmp_path):
    idx = _idx(linked_vault, tmp_path)
    g = build_graph(idx.conn)
    pr = personalized_pagerank(g, ["alpha"])
    assert pr["beta"] > 0 and pr["delta"] > 0
    # island is disconnected from alpha's component → ~0
    assert pr.get("island", 0.0) < pr["beta"]


def test_graph_expand_is_discovery_only(linked_vault, tmp_path):
    idx = _idx(linked_vault, tmp_path)
    res = idx.graph_expand(["alpha"], depth=2, k=10)
    assert res["authoritative"] is False
    assert res["provenance"] == PROVENANCE
    ids = {r["id"] for r in res["results"]}
    assert {"beta", "gamma", "delta"} <= ids
    assert "alpha" not in ids  # seed excluded
    assert all(r["authoritative"] is False for r in res["results"])


def test_graph_expand_unresolved_seed(linked_vault, tmp_path):
    idx = _idx(linked_vault, tmp_path)
    res = idx.graph_expand(["no-such-note"], depth=2, k=10)
    assert "no-such-note" in res["unresolved_seeds"]
    assert res["results"] == []


# ===================== RET-04 — agentic tool surface (CLI) ================
@pytest.fixture
def built(linked_vault, monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_VAULT", str(linked_vault))
    monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
    idx = BrainIndex(
        db_path=tmp_path / "idx" / "index.sqlite",
        backend=get_backend("brute-force"),
        embedder=get_embedder("hash"),
    )
    core = BrainCore(vault=linked_vault, index=idx)
    core.rebuild()
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None: core)
    return core


def _run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_grep_json_and_egress(built, capsys):
    rc, out = _run(capsys, "grep", "retrieval", "--json")
    assert rc == 0
    payload = json.loads(out)
    assert payload["results"]
    for h in payload["results"]:
        assert "match_count" in h and h["source"] == "grep"


def test_grep_withholds_restricted(built, capsys):
    # secret-deal is Restricted → must not surface at default Internal tier
    rc, out = _run(capsys, "grep", "negotiation OR Restricted OR withheld",
                   "--regex", "--json")
    ids = {h["id"] for h in json.loads(out)["results"]}
    assert "secret-deal" not in ids


def test_bases_query_filters(built, capsys):
    rc, out = _run(capsys, "bases-query", "--where", "type=note",
                   "--where", "classification=Internal", "--json")
    assert rc == 0
    payload = json.loads(out)
    assert payload["results"]
    assert all(h["classification"] == "Internal" for h in payload["results"])
    assert "secret-deal" not in {h["id"] for h in payload["results"]}


def test_graph_expand_cli_discovery_and_egress(built, capsys):
    rc, out = _run(capsys, "graph-expand", "alpha", "--depth", "2", "--json")
    assert rc == 0
    payload = json.loads(out)
    assert payload["authoritative"] is False
    ids = {h["id"] for h in payload["results"]}
    assert {"beta", "gamma", "delta"} <= ids
    # secret-deal is a 1-hop neighbour but Restricted → egress-gated out
    assert "secret-deal" not in ids
    assert payload["egress"]["withheld"] >= 1


def test_read_is_get_alias(built, capsys):
    rc, out = _run(capsys, "read", "alpha", "--json")
    assert rc == 0
    assert json.loads(out)["id"] == "alpha"


def test_search_rerank_flag_runs(built, capsys, monkeypatch):
    # Force the auto-reranker onto the offline noop path so the test never
    # downloads the cross-encoder model (and exercises the skippable contract).
    monkeypatch.setattr("brain.rerank.GteReranker.available", staticmethod(lambda: False))
    rc, out = _run(capsys, "search", "delta arctic fusion", "--rerank", "--json")
    assert rc == 0
    payload = json.loads(out)
    assert payload["rerank"] is True


def test_help_lists_agentic_tools(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for token in ("hybrid-search", "grep", "bases-query", "graph-expand", "read"):
        assert token in out, f"--help missing tool: {token!r}"
