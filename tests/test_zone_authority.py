"""PT-02 (s05) — retrieval-time zone-authority restoration.

`docs/eval-bench/pt-diagnosis.md` (s04, pt-01) found the RET-01b anti-burial
zone-authority prior was DEAD on the live migrated index: every note's
indexed ``zone`` column is flattened to ``brain``/``raw`` (People pages and
Companies both land in ``brain/areas/``; meeting transcripts land in
``raw/``), so ``_zone_weight`` — keyed on Johnny-Decimal zone names like
``"10 People"`` — never matched anything.

``BrainIndex._resolve_zone`` (s05) re-arms it RETRIEVAL-TIME-ONLY (H23/H11 —
no re-index, no schema change): the migration tool
(``tools/apply_live_migration.py``) already writes the original JD zone into
each migrated note's frontmatter as ``source_zone:``; this reads that field
straight off the note's file at query time and falls back to the flattened
column when absent (brain-native notes created after migration).

These tests cover: the resolver contract in isolation (frontmatter present /
absent / disabled via kill switch / cached), AND an end-to-end
``hybrid_search`` regression guard reproducing the exact diagnosed failure
mode — a curated note buried under transcript-zone volume — showing the
fix recovers it and the pre-fix column-only mode does not.
"""
from __future__ import annotations

import pytest

from brain.embed import get_embedder
from brain.index import BrainIndex
from brain.vectors import get_backend


def _fm(nid, *, zone_col, source_zone=None, body="hello world"):
    lines = [f"id: {nid}", f'title: "{nid}"', "type: note", "classification: Internal",
              "created: 2026-07-02", "updated: 2026-07-02"]
    if source_zone is not None:
        lines.append(f"source_zone: {source_zone}")
        lines.append(f"source_path: {source_zone}/{nid}.md")
    text = "---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n"
    return text


@pytest.fixture
def zone_vault(tmp_path):
    vault = tmp_path / "zvault"
    (vault / "brain" / "areas").mkdir(parents=True)
    (vault / "raw").mkdir(parents=True)
    (vault / "brain" / "areas" / "curated-person.md").write_text(
        _fm("curated-person", zone_col="brain", source_zone="10 People",
            body="Jordan Rivera business partnering contact."),
        encoding="utf-8",
    )
    (vault / "brain" / "areas" / "no-source-zone.md").write_text(
        _fm("no-source-zone", zone_col="brain", source_zone=None,
            body="A brain-native note with no source_zone (never migrated)."),
        encoding="utf-8",
    )
    for i in range(3):
        (vault / "raw" / f"transcript-{i}.md").write_text(
            _fm(f"transcript-{i}", zone_col="raw", source_zone="40 Meetings",
                body=f"Meeting transcript {i} mentioning many topics."),
            encoding="utf-8",
        )
    return vault


def _idx(vault, tmp_path, name="zone"):
    idx = BrainIndex(
        db_path=tmp_path / f"{name}.sqlite",
        backend=get_backend("brute-force"),
        embedder=get_embedder("hash"),
    )
    idx.rebuild(vault)
    return idx


# ===================== _resolve_zone unit contract ==========================

def test_resolve_zone_prefers_frontmatter_source_zone(zone_vault, tmp_path):
    idx = _idx(zone_vault, tmp_path)
    path = str(zone_vault / "brain" / "areas" / "curated-person.md")
    assert idx._resolve_zone("brain", path) == "10 People"


def test_resolve_zone_falls_back_when_no_source_zone(zone_vault, tmp_path):
    idx = _idx(zone_vault, tmp_path)
    path = str(zone_vault / "brain" / "areas" / "no-source-zone.md")
    assert idx._resolve_zone("brain", path) == "brain"


def test_resolve_zone_falls_back_on_missing_file(zone_vault, tmp_path):
    idx = _idx(zone_vault, tmp_path)
    assert idx._resolve_zone("raw", str(tmp_path / "does-not-exist.md")) == "raw"


def test_resolve_zone_column_kill_switch(zone_vault, tmp_path, monkeypatch):
    idx = _idx(zone_vault, tmp_path)
    path = str(zone_vault / "brain" / "areas" / "curated-person.md")
    monkeypatch.setenv("BRAIN_ZONE_SOURCE_MODE", "column")
    assert idx._resolve_zone("brain", path) == "brain"
    monkeypatch.delenv("BRAIN_ZONE_SOURCE_MODE", raising=False)
    assert idx._resolve_zone("brain", path) == "10 People"


def test_resolve_zone_caches_and_invalidates_on_mtime_change(zone_vault, tmp_path):
    import os
    import time

    idx = _idx(zone_vault, tmp_path)
    p = zone_vault / "brain" / "areas" / "curated-person.md"
    path = str(p)
    assert idx._resolve_zone("brain", path) == "10 People"
    assert (path, os.stat(path).st_mtime_ns) in idx._source_zone_cache

    # Rewrite with a DIFFERENT source_zone and force a distinct mtime.
    p.write_text(_fm("curated-person", zone_col="brain", source_zone="60 Concepts"),
                 encoding="utf-8")
    os.utime(path, ns=(time.time_ns() + 5_000_000_000, time.time_ns() + 5_000_000_000))
    assert idx._resolve_zone("brain", path) == "60 Concepts"


def test_default_zone_weights_include_curated_boost_and_meetings_damp():
    weights = BrainIndex._DEFAULT_ZONE_WEIGHTS
    for z in ("10 People", "20 Companies", "30 Projects", "60 Concepts", "70 Decisions"):
        assert weights[z] > 1.0, f"{z} should be boosted"
    assert weights["40 Meetings"] < 1.0, "40 Meetings (flooding zone) should be damped"


# ===================== end-to-end regression guard ==========================
# Reproduces the exact diagnosed failure: a curated note ranked LAST by the
# fused dense/lexical legs (buried by transcript-zone volume) must be
# recoverable once the zone-authority prior is alive (auto mode) and must
# NOT be recoverable pre-fix (column mode) — i.e. this test would have FAILED
# before s05 and demonstrates the fix is real, not a tautology.

def test_hybrid_search_rearms_prior_via_source_zone(zone_vault, tmp_path, monkeypatch):
    idx = _idx(zone_vault, tmp_path)
    rows = {
        r[1]: r[0]
        for r in idx.conn.execute("SELECT rowid, id FROM notes").fetchall()
    }
    curated_rid = rows["curated-person"]
    transcript_rids = [rows[f"transcript-{i}"] for i in range(3)]

    # Force: nothing found lexically (in_lex empty -> semantic_only scope
    # applies the prior to every candidate); dense leg buries the curated
    # note LAST behind the three transcript notes (the diagnosed shape).
    monkeypatch.setattr(idx, "_lexical_ranked", lambda q, n: [])
    dense_order = [*transcript_rids, curated_rid]
    monkeypatch.setattr(idx, "_dense_ranked", lambda q, n: (dense_order, {}, {}))

    # Pre-fix behaviour: flattened column only -> no default weight key
    # matches "brain"/"raw" -> RRF order is untouched -> curated note stays
    # buried outside top-3.
    monkeypatch.setenv("BRAIN_ZONE_SOURCE_MODE", "column")
    idx._zone_weights = None  # clear the memoised weights cache
    pre_fix_top3 = [h.id for h in idx.hybrid_search("jordan rivera", k=3)]
    assert "curated-person" not in pre_fix_top3, (
        "pre-fix (column mode) must reproduce the burial — if this note is "
        "already recoverable, the test fixture isn't reproducing the bug"
    )

    # Post-fix: frontmatter source_zone resolution + CV-selected weights
    # (10 People x2.0, 40 Meetings x0.55) overturn the volume-driven burial.
    monkeypatch.delenv("BRAIN_ZONE_SOURCE_MODE", raising=False)
    idx._zone_weights = None
    post_fix_top3 = [h.id for h in idx.hybrid_search("jordan rivera", k=3)]
    assert "curated-person" in post_fix_top3, (
        "post-fix (auto mode) should recover the curated note from behind "
        "transcript-zone volume via the re-armed authority prior"
    )


# ===================== near-dup transcript suppression ======================
# The lever is OFF by default (s05 CV found zero incremental value on top of
# the zone fix — see docs/eval-bench/pt-fix.md), but the machinery ships and
# must be correct when enabled via BRAIN_DEDUP_THRESHOLD.

def test_dedup_off_by_default_is_a_noop():
    assert BrainIndex._DEFAULT_DEDUP_THRESHOLD is None


def test_dedup_disabled_returns_order_unchanged(zone_vault, tmp_path, monkeypatch):
    idx = _idx(zone_vault, tmp_path)
    monkeypatch.delenv("BRAIN_DEDUP_THRESHOLD", raising=False)
    ordered = [1, 2, 3, 4]
    out = idx._suppress_near_dups(ordered, {}, {}, {}, set())
    assert out == ordered


class _StubBackend:
    """Minimal get_vectors stub so the suppression logic can be unit-tested
    without building a real index (the vectors ARE the test input)."""

    def __init__(self, vecs):
        self._vecs = vecs

    def get_vectors(self, conn, rowids):
        return {r: self._vecs[r] for r in rowids if r in self._vecs}


def _stub_idx(tmp_path, vecs):
    idx = BrainIndex(
        db_path=tmp_path / "stub.sqlite",
        backend=get_backend("brute-force"),
        embedder=get_embedder("hash"),
    )
    idx.backend = _StubBackend(vecs)
    return idx


def test_dedup_defers_near_dup_transcript_keeps_representative(tmp_path, monkeypatch):
    # rowids 1,2 are near-identical transcript chunks; 3 is distinct. 2 must be
    # deferred (kept AFTER the distinct note), 1 (the representative) survives.
    idx = _stub_idx(tmp_path, {10: [1.0, 0.0], 20: [0.999, 0.001], 30: [0.0, 1.0]})
    ordered = [1, 2, 3]
    bcr = {1: 10, 2: 20, 3: 30}
    zmap = {1: "40 Meetings", 2: "40 Meetings", 3: "40 Meetings"}
    monkeypatch.setenv("BRAIN_DEDUP_THRESHOLD", "0.97")
    out = idx._suppress_near_dups(ordered, bcr, zmap, zmap, set())
    assert out == [1, 3, 2], "near-dup #2 should be demoted to the tail, #1 kept"


def test_dedup_never_suppresses_curated_zone(tmp_path, monkeypatch):
    # #2 is a near-dup of #1 but lives in a CURATED zone (10 People) — under the
    # default scope=transcript it must NOT be suppressed.
    idx = _stub_idx(tmp_path, {10: [1.0, 0.0], 20: [0.999, 0.001]})
    ordered = [1, 2]
    bcr = {1: 10, 2: 20}
    zmap = {1: "40 Meetings", 2: "10 People"}
    monkeypatch.setenv("BRAIN_DEDUP_THRESHOLD", "0.97")
    out = idx._suppress_near_dups(ordered, bcr, zmap, zmap, set())
    assert out == [1, 2], "a curated-zone near-dup must never be suppressed (scope=transcript)"


def test_dedup_scope_all_suppresses_any_zone(tmp_path, monkeypatch):
    # scope=all: a curated near-dup (#2) IS eligible for suppression. Use a
    # 3-item case so a deferral is observable as a reorder.
    idx = _stub_idx(tmp_path, {10: [1.0, 0.0], 20: [0.999, 0.001], 30: [0.0, 1.0]})
    monkeypatch.setenv("BRAIN_DEDUP_THRESHOLD", "0.97")
    monkeypatch.setenv("BRAIN_DEDUP_SCOPE", "all")
    out = idx._suppress_near_dups(
        [1, 2, 3], {1: 10, 2: 20, 3: 30},
        {1: "40 Meetings", 2: "10 People", 3: "20 Companies"},
        {1: "raw", 2: "brain", 3: "brain"}, set())
    assert out == [1, 3, 2], "scope=all should defer the curated near-dup too"


def test_dedup_never_suppresses_lexical_hits(tmp_path, monkeypatch):
    idx = _stub_idx(tmp_path, {10: [1.0, 0.0], 20: [0.999, 0.001], 30: [0.0, 1.0]})
    ordered = [1, 2, 3]
    bcr = {1: 10, 2: 20, 3: 30}
    zmap = {1: "40 Meetings", 2: "40 Meetings", 3: "40 Meetings"}
    monkeypatch.setenv("BRAIN_DEDUP_THRESHOLD", "0.97")
    # #2 is in the lexical set -> exact-match, never suppressed even as a near-dup.
    out = idx._suppress_near_dups(ordered, bcr, zmap, zmap, in_lex={2})
    assert out == [1, 2, 3]


def test_dedup_below_threshold_keeps_all(tmp_path, monkeypatch):
    # cosine ~0.71 (orthogonal-ish) is below 0.97 -> nothing suppressed.
    idx = _stub_idx(tmp_path, {10: [1.0, 0.0], 20: [0.7, 0.7], 30: [0.0, 1.0]})
    ordered = [1, 2, 3]
    bcr = {1: 10, 2: 20, 3: 30}
    zmap = {1: "40 Meetings", 2: "40 Meetings", 3: "40 Meetings"}
    monkeypatch.setenv("BRAIN_DEDUP_THRESHOLD", "0.97")
    out = idx._suppress_near_dups(ordered, bcr, zmap, zmap, set())
    assert out == [1, 2, 3]


def test_get_vectors_roundtrip(zone_vault, tmp_path):
    """The backend accessor the suppression pass relies on returns the stored
    chunk vectors (real brute-force index)."""
    idx = _idx(zone_vault, tmp_path)
    chunk_rowids = [r[0] for r in idx.conn.execute("SELECT rowid FROM chunks LIMIT 3")]
    vecs = idx.backend.get_vectors(idx.conn, chunk_rowids)
    assert set(vecs) == set(chunk_rowids)
    assert all(len(v) == idx.embedder.dim for v in vecs.values())
    assert idx.backend.get_vectors(idx.conn, []) == {}


def test_hybrid_search_lexical_hits_are_never_deweighted(zone_vault, tmp_path, monkeypatch):
    """scope=semantic_only (the default) must leave an exact lexical match's
    score untouched even if its zone would otherwise be damped."""
    # Disable the orthogonal recency prior (RET-07) so this asserts the ZONE
    # prior's scope in isolation — recency legitimately damps a stale exact-match
    # by date, which is a separate concern with its own test.
    monkeypatch.setenv("BRAIN_RECENCY_WEIGHT", "0")
    idx = _idx(zone_vault, tmp_path)
    rows = {
        r[1]: r[0]
        for r in idx.conn.execute("SELECT rowid, id FROM notes").fetchall()
    }
    t0 = rows["transcript-0"]
    monkeypatch.setattr(idx, "_lexical_ranked", lambda q, n: [t0])
    monkeypatch.setattr(idx, "_dense_ranked", lambda q, n: ([t0], {}, {}))
    idx._zone_weights = None
    hits = idx.hybrid_search("meeting transcript", k=5)
    assert hits and hits[0].id == "transcript-0"
    lex_rrf = 1.0 / (60 + 1) + 1.0 / (60 + 1)  # both legs rank 1
    assert hits[0].score == pytest.approx(lex_rrf), (
        "an exact-match ('both') hit must not be damped by the 40 Meetings prior"
    )
