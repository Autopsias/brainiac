"""RET-06 — gated graph-augmented multi-hop retrieval.

Covers the three load-bearing guarantees of ``brain.multihop`` +
``BrainIndex.hybrid_search_graph``:

  1. **Gate** — a query naming >= 2 distinct non-hub entities is multi-hop-
     shaped; a query naming 0-1 (or only a hub) is not.
  2. **Single-hop non-regression (by construction)** — when the gate does not
     fire, ``hybrid_search_graph`` returns EXACTLY ``hybrid_search`` (same ids,
     same scores). This is the property that makes enabling the layer safe.
  3. **Flat-dominant fusion** — the flat rank-1 note can never be displaced by
     a discovery-only graph candidate; a note in BOTH lists is promoted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.graph import LinkGraph
from brain.index import BrainIndex
from brain.multihop import (
    EntityLexicon,
    EntityMention,
    fuse_flat_and_graph,
    is_multihop_shaped,
    rank_graph_candidates,
)
from brain.vectors import BruteForceBackend


# ---------------------------------------------------------------------------
# Pure-function unit tests (no index)
# ---------------------------------------------------------------------------

def _m(nid, surface, is_hub=False):
    return EntityMention(nid, surface, "person", is_hub)


def test_gate_fires_on_two_distinct_non_hub_entities():
    assert is_multihop_shaped([_m("a", "northwind"), _m("b", "gamma")]) is True


def test_gate_does_not_fire_on_single_entity():
    assert is_multihop_shaped([_m("a", "northwind")]) is False


def test_gate_does_not_fire_on_hub_plus_one():
    # "contoso" (hub) + one entity is single-hop-shaped: the hub does not count.
    assert is_multihop_shaped([_m("contoso", "contoso", is_hub=True), _m("b", "northwind")]) is False


def test_gate_fires_when_two_non_hub_plus_a_hub():
    assert is_multihop_shaped(
        [_m("contoso", "contoso", is_hub=True), _m("b", "northwind"), _m("c", "gamma")]
    ) is True


def test_fusion_flat_rank1_never_displaced_by_graph_only():
    # flat=[A,B,C]; graph=[Z] (novel). The flat rank-1 note A can never be
    # overtaken by a graph-ONLY note for any graph_weight <= 1.
    for w in (0.25, 0.5, 1.0):
        assert fuse_flat_and_graph(["A", "B", "C"], ["Z"], graph_weight=w)[0][0] == "A"
    # At the default weight (0.5) a graph-ONLY novel note lands strictly AFTER
    # every flat note (it augments the tail; it does not disrupt flat's order).
    fused = fuse_flat_and_graph(["A", "B", "C"], ["Z"], graph_weight=0.5)
    assert [nid for nid, _ in fused] == ["A", "B", "C", "Z"]


def test_fusion_promotes_a_corroborated_flat_tail_doc():
    # D is deep in flat (rank 5) but graph rank 1 -> it should be promoted above
    # some flat docs. Graph weight 1.0 makes the effect visible.
    flat = ["A", "B", "C", "D_tail", "E"]
    graph = ["D_tail"]
    fused = [nid for nid, _ in fuse_flat_and_graph(flat, graph, graph_weight=1.0)]
    assert fused.index("D_tail") < flat.index("D_tail")


def test_fusion_empty_graph_preserves_flat_order():
    flat = ["A", "B", "C"]
    assert [nid for nid, _ in fuse_flat_and_graph(flat, [], graph_weight=0.5)] == flat


def test_rank_graph_candidates_excludes_seeds_and_handles_no_seed():
    g = LinkGraph()
    for n in ("p", "q", "r"):
        g.nodes.add(n)
        g.out.setdefault(n, set())
        g.inn.setdefault(n, set())
    g.out["p"].add("q"); g.inn["q"].add("p")
    g.out["q"].add("r"); g.inn["r"].add("q")
    cands = rank_graph_candidates(g, ["p"], depth=2, limit=10)
    assert "p" not in cands and set(cands) <= {"q", "r"}
    assert rank_graph_candidates(g, ["unknown"], depth=2) == []


# ---------------------------------------------------------------------------
# Integration tests (real built index)
# ---------------------------------------------------------------------------

def _entity_note(nid, title, etype, body):
    return (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: {etype}\n"
        f"classification: Internal\ncreated: 2026-06-27\nupdated: 2026-06-27\n---\n\n{body}\n"
    )


@pytest.fixture
def entity_vault(tmp_path: Path) -> Path:
    """A tiny vault with entity-typed notes + wikilinks so the gate + graph
    have real material: two people, two companies, and source notes linking
    them."""
    vault = tmp_path / "vault"
    (vault / "brain").mkdir(parents=True)
    notes = {
        "brain/northwind.md": _entity_note(
            "northwind", "Northwind", "company",
            "Northwind runs a SAP estate. Linked to [[Contoso]] via the JV."),
        "brain/contoso.md": _entity_note(
            "contoso", "Contoso", "company",
            "Contoso runs S/4HANA. See [[Northwind]] and [[Alexandre Silva]]."),
        "brain/alexandre.md": _entity_note(
            "alexandre-silva", "Alexandre Silva", "person",
            "Alexandre Silva leads IT. Works with [[Nuno Costa]] and [[Contoso]]."),
        "brain/nuno.md": _entity_note(
            "nuno-costa", "Nuno Costa", "person",
            "Nuno Costa is offered a role. Links to [[Northwind]] and [[Alexandre Silva]]."),
        "brain/sap-jv.md": _entity_note(
            "sap-jv", "SAP JV decision", "decision",
            "The JV keeps Contoso on S/4HANA while Northwind stays on its own SAP estate. "
            "See [[Northwind]] and [[Contoso]] and [[Alexandre Silva]]."),
        "brain/unrelated.md": _entity_note(
            "unrelated", "Weather note", "note",
            "A note about the weather and coffee, unrelated to any company."),
    }
    for rel, text in notes.items():
        (vault / rel).write_text(text, encoding="utf-8")
    return vault


@pytest.fixture
def entity_index(entity_vault, tmp_path):
    idx = BrainIndex(db_path=tmp_path / "ent.sqlite", backend=BruteForceBackend())
    idx.rebuild(entity_vault)
    return idx


def test_entity_lexicon_builds_from_entity_types(entity_index):
    lex = EntityLexicon.build(entity_index.conn)
    # "weather note" is type:note -> NOT in the lexicon; entities ARE.
    m_multi = lex.mentions("How does Northwind connect to Alexandre Silva in the JV?")
    surfaces = {x.surface for x in m_multi}
    assert "northwind" in surfaces and "alexandre silva" in surfaces
    assert lex.mentions("a note about the weather") == []


def test_single_hop_passthrough_is_byte_identical(entity_index):
    # A query naming <2 entities must return EXACTLY hybrid_search — same ids,
    # same scores. This is the single-hop non-regression guarantee.
    q = "Tell me about Northwind and its SAP estate."  # names only Northwind (+ no 2nd entity)
    flat = entity_index.hybrid_search(q, k=10)
    graph, trace = entity_index.hybrid_search_graph(q, k=10, return_trace=True)
    assert trace["fired"] is False
    assert [h.id for h in flat] == [h.id for h in graph]
    assert [round(h.score, 9) for h in flat] == [round(h.score, 9) for h in graph]


def test_multi_hop_query_fires_and_returns_ranking(entity_index):
    q = "How does Northwind connect to Nuno Costa and Alexandre Silva?"
    graph, trace = entity_index.hybrid_search_graph(q, k=10, return_trace=True)
    assert trace["fired"] is True
    assert len(trace["non_hub_entities"]) >= 2
    assert graph, "expected a non-empty ranking"
    # scores are strictly descending (fused rank encoded), so a {path: score}
    # round-trip (the eval harness) preserves the fused order.
    scores = [h.score for h in graph]
    assert scores == sorted(scores, reverse=True)


def test_multi_hop_top_hit_is_authoritative_flat_hit(entity_index):
    # Flat-dominant fusion MAY promote a graph-corroborated FLAT hit, but the #1
    # result must always be a note flat actually retrieved (source != "graph"),
    # never a discovery-only graph candidate.
    q = "How does Northwind connect to Nuno Costa and Alexandre Silva?"
    flat_ids = {h.id for h in entity_index.hybrid_search(q, k=30)}
    graph = entity_index.hybrid_search_graph(q, k=10)
    assert graph[0].source != "graph"
    assert graph[0].id in flat_ids
