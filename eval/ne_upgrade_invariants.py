#!/usr/bin/env python3
"""The ADR-0008 exact-behaviour invariants for the S02 named-entity gate.

Split out of `eval/ne_upgrade_gate.py` on 2026-09-05: one 159-line function
plus the four fixture helpers only it uses. The gate re-exports `_invariants`,
so `ne_upgrade_gate._invariants()` still resolves.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from brain import egress  # noqa: E402
from brain.embed import get_embedder  # noqa: E402
from brain.index import BrainIndex, _ExactLeg  # noqa: E402
from brain.vectors import get_backend  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "named_entity_vault"


@contextmanager
def _env(**updates: str | None) -> Iterator[None]:
    old = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _index(vault: Path, directory: Path, name: str) -> BrainIndex:
    idx = BrainIndex(
        db_path=directory / f"{name}.sqlite",
        backend=get_backend("brute-force"),
        embedder=get_embedder("hash"),
    )
    idx.rebuild(vault)
    return idx


def _note(note_id: str, title: str, body: str, *, classification: str = "Internal",
          aliases: str = "") -> str:
    return (
        f"---\nid: {note_id}\ntitle: \"{title}\"\ntype: note\n"
        f"classification: {classification}\ncreated: 2026-07-28\nupdated: 2026-07-28\n"
        f"{aliases}---\n\n{body}\n"
    )


def _empty_exact() -> _ExactLeg:
    return _ExactLeg([], {}, set(), set(), set(), set(), set(), None, [])


class _AdversarialReranker:
    """A reranker that actively tries to pull the RETIRED record to the top."""

    model_id = "s02-adversarial"

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        return [100.0 if "retired" in passage.lower() else float(pos)
                for pos, passage in enumerate(passages)]


def _alias_hit(idx) -> bool:
    """An NFD-composed alias resolves to its canonical note as an alias hit."""
    alias = idx.hybrid_search("Cafe\u0301 Aurora", k=8)
    return (alias[0].id == "fabrikam-aurora-cafe"
            and alias[0].evidence == "alias_hit"
            and alias[0].create_safety == "exists")


def _collision_order(idx) -> bool:
    """On a colliding name the LIVE record outranks the retired one, and
    neither is claimed as an exact identity."""
    collision = idx.hybrid_search("Orbit", k=24)
    collision_ids = [hit.id for hit in collision]
    return (
        collision_ids.index("contoso-orbit-current")
        < collision_ids.index("contoso-orbit-retired")
        and all(hit.create_safety == "probable" for hit in collision
                if hit.id.startswith("contoso-orbit-"))
    )


def _adversarial_rerank(idx) -> bool:
    """A hostile reranker moves neither the unique pin nor the collision slots."""
    unique_reranked = idx.hybrid_search(
        "Hall of Lanterns", k=12, rerank=True,
        reranker=_AdversarialReranker(), rerank_top=20,
    )
    collision_reranked = idx.hybrid_search(
        "Orbit", k=24, rerank=True,
        reranker=_AdversarialReranker(), rerank_top=20,
    )
    reranked_ids = [hit.id for hit in collision_reranked]
    return (
        unique_reranked[0].id == "contoso-lantern-program"
        and reranked_ids.index("contoso-orbit-current")
        < reranked_ids.index("contoso-orbit-retired")
    )


def _kill_switch_equivalence(idx) -> bool:
    """The switch must give the same legacy IDs, order, scores, source, and
    snippets as a manually empty third leg."""
    with _env(BRAIN_EXACT_LEG_ENABLED="0"):
        switched_off = idx.hybrid_search("Northwind Relay", k=16)
    original = idx._exact_leg
    try:
        idx._exact_leg = lambda query, rrf_k: _empty_exact()  # type: ignore[method-assign]
        manual_legacy = idx.hybrid_search("Northwind Relay", k=16)
    finally:
        idx._exact_leg = original  # type: ignore[method-assign]

    def legacy_fields(hits):
        return [(h.id, h.score, h.source, h.snippet) for h in hits]

    return legacy_fields(switched_off) == legacy_fields(manual_legacy)


def _partial_phrase_bound(root: Path) -> bool:
    """Construct a rank-1 organic anchor plus a partial-title-only candidate.
    The 0.25 exact tier must not leap the 2-leg anchor."""
    phrase_vault = root / "phrase-vault"
    (phrase_vault / "brain" / "resources").mkdir(parents=True)
    (phrase_vault / "raw").mkdir()
    (phrase_vault / "brain" / "index.md").write_text(
        _note("index", "Index", "fixture"), encoding="utf-8"
    )
    (phrase_vault / "brain" / "resources" / "anchor.md").write_text(
        _note("anchor", "Organic Anchor", "Northwind Relay organic anchor."), encoding="utf-8"
    )
    (phrase_vault / "brain" / "resources" / "partial.md").write_text(
        _note("partial", "Northwind Relay Gateway", "different title target"), encoding="utf-8"
    )
    phrase_idx = _index(phrase_vault, root, "phrase")
    anchor_rowid = phrase_idx._rowid_of("anchor")
    anchor_chunk = phrase_idx.conn.execute(
        "SELECT rowid FROM chunks WHERE note_rowid=?", (anchor_rowid,)
    ).fetchone()[0]
    phrase_idx._lexical_ranked = lambda query, n: [anchor_rowid]  # type: ignore[method-assign]
    phrase_idx._dense_ranked = lambda query, n: (  # type: ignore[method-assign]
        [anchor_rowid], {anchor_rowid: "anchor"}, {anchor_rowid: anchor_chunk},
        {anchor_rowid: 0.9},
    )
    phrase_hits = phrase_idx.hybrid_search("Northwind Relay", k=5)
    return (
        phrase_hits[0].id == "anchor"
        and next(hit for hit in phrase_hits if hit.id == "partial").evidence
        == "title_phrase_match"
    )


def _egress_after_alias_hop(root: Path) -> bool:
    """An exact alias is injected before the egress gate but a Restricted owner
    must still be withheld; visible companions become unknown."""
    egress_vault = root / "egress-vault"
    (egress_vault / "brain" / "resources").mkdir(parents=True)
    (egress_vault / "raw").mkdir()
    (egress_vault / "brain" / "index.md").write_text(
        _note("index", "Index", "Internal helper note."), encoding="utf-8"
    )
    (egress_vault / "brain" / "resources" / "restricted.md").write_text(
        _note("restricted", "Restricted Canonical", "Private record.",
              classification="Restricted", aliases="aliases: ['Hidden Alias']\n"),
        encoding="utf-8",
    )
    egress_idx = _index(egress_vault, root, "egress")
    raw_hits = [hit.to_dict() for hit in egress_idx.hybrid_search("Hidden Alias", k=8)]
    surfaced, report = egress.apply_gate(raw_hits, "Internal")
    egress_idx.annotate_create_safety("Hidden Alias", surfaced, "Internal")
    return (
        "restricted" not in {hit["id"] for hit in surfaced}
        and report["withheld"] >= 1
        and all(hit["create_safety"] == "unknown" for hit in surfaced)
    )


def _near_duplicate_exemption(root: Path) -> bool:
    """Full identities are exempt even after score ordering reaches the
    near-duplicate pass."""
    dedup_idx = BrainIndex(
        db_path=root / "dedup.sqlite", backend=get_backend("brute-force"),
        embedder=get_embedder("hash"),
    )
    with _env(BRAIN_DEDUP_THRESHOLD="0.5", BRAIN_DEDUP_SCOPE="all"):
        dedup_idx.backend.get_vectors = lambda conn, ids: {  # type: ignore[method-assign]
            1: [1.0, 0.0], 2: [1.0, 0.0]
        }
        return dedup_idx._suppress_near_dups(
            [10, 20], {10: 1, 20: 2}, {10: "brain", 20: "brain"},
            {10: "brain", 20: "brain"}, set(), {20},
        ) == [10, 20]


def _invariants() -> dict[str, Any]:
    """Exercise the exact behavior independently of aggregate ranking metrics."""
    with tempfile.TemporaryDirectory(prefix="brain-s02-ne-") as temp:
        root = Path(temp)
        with _env(
            BRAIN_EXACT_LEG_ENABLED="1", BRAIN_RECENCY_WEIGHT="0",
            BRAIN_ZONE_WEIGHTS=None, BRAIN_ZONE_SCOPE="semantic_only",
            BRAIN_DEDUP_THRESHOLD=None, BRAIN_DEDUP_SCOPE=None,
        ):
            idx = _index(FIXTURE, root, "fixture")
            rerank_off_checks = {
                "alias_nfc_nfd": _alias_hit(idx),
                "collision_live_before_retired_and_probable": _collision_order(idx),
            }
            adversarial_checks = {
                "unique_pin_and_collision_slots": _adversarial_rerank(idx),
            }
            kill_switch = {
                "legacy_result_equivalence": _kill_switch_equivalence(idx),
            }
            rerank_off_checks["partial_phrase_bound"] = _partial_phrase_bound(root)
            rerank_off_checks["egress_after_alias_hop"] = _egress_after_alias_hop(root)
            rerank_off_checks["full_exact_near_duplicate_exemption"] = (
                _near_duplicate_exemption(root))

    return {
        "rerank_off": {
            "checks": rerank_off_checks,
            "pass": all(rerank_off_checks.values()),
        },
        "adversarial_reranker_on": {
            "checks": adversarial_checks,
            "pass": all(adversarial_checks.values()),
        },
        "kill_switch_baseline_equivalence": {
            "checks": kill_switch,
            "pass": all(kill_switch.values()),
        },
        "pass": all(rerank_off_checks.values()) and all(adversarial_checks.values())
        and all(kill_switch.values()),
    }
