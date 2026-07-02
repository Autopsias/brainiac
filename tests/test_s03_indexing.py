"""S03 — Arctic embedder adapter (IDX-01), chunking + contextual prefix (IDX-02),
incremental sync + delete-propagation (IDX-03), snapshot publisher, drain-on-invoke.

These run offline with the deterministic HashEmbedder; the real fastembed/ONNX
path + cross-lingual behaviour are proven by tools/probe_xlingual.py (evidence)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import chunk as ch
from brain.core import BrainCore
from brain.embed import (
    ARCTIC_MODEL_ID,
    MRL_DIM,
    QUERY_PREFIX,
    ArcticEmbedder,
    HashEmbedder,
    get_embedder,
    mrl_truncate,
)
from brain.index import BrainIndex
from brain.snapshot import publish_snapshot, read_manifest, snapshot_status
from brain.vectors import BruteForceBackend


# ---- IDX-01: embedder adapter, model_id, MRL --------------------------------
def test_hash_embedder_has_model_id_and_accepts_is_query():
    e = HashEmbedder()
    assert e.model_id == "hash-v1"
    # is_query is accepted (protocol parity) and ignored deterministically
    assert e.embed("x", is_query=True) == e.embed("x", is_query=False)


def test_mrl_truncate_dims_and_unit_norm():
    v = [float(i) for i in range(768)]
    t = mrl_truncate(v, MRL_DIM)
    assert len(t) == MRL_DIM
    assert sum(x * x for x in t) == pytest.approx(1.0, abs=1e-6)


def test_query_prefix_is_canonical_and_untranslated():
    assert QUERY_PREFIX == "query: "


def test_arctic_embedder_metadata_without_loading_model():
    # Constructing does not download/load; model_id + dim are readable.
    e = ArcticEmbedder()
    assert e.model_id == ARCTIC_MODEL_ID
    assert e.dim == MRL_DIM


def test_get_embedder_hash_forces_fallback():
    assert get_embedder("hash").model_id == "hash-v1"


# ---- IDX-02: chunking + in-language contextual prefix -----------------------
def test_chunk_splits_multi_section_note_into_multiple_chunks():
    body = (
        "# Title\n\nIntro paragraph about the engine.\n\n"
        "## Section A\n\n" + ("alpha " * 200) + "\n\n"
        "## Section B\n\n" + ("bravo " * 200) + "\n"
    )
    chunks = ch.chunk_text(body)
    assert len(chunks) >= 2
    headings = {c.heading for c in chunks}
    assert "Section A" in headings and "Section B" in headings


def test_detect_language_en_pt_es():
    assert ch.detect_language("the company is growing its offshore wind power") == "en"
    assert ch.detect_language("a empresa está a aumentar a produção de energia") == "pt"
    assert ch.detect_language("la empresa amplía su generación con aerogeneradores") == "es"


def test_contextual_prefix_is_in_language():
    assert ch.contextual_prefix("T", "resources", "Sec", "pt").startswith("Contexto")
    assert ch.contextual_prefix("T", "resources", "Sec", "es").startswith("Contexto")
    assert ch.contextual_prefix("T", "resources", "Sec", "en").startswith("Context")
    # Portuguese section label, not English
    assert "Secção" in ch.contextual_prefix("T", "z", "Sec", "pt")


def test_embed_input_prepends_contextual_prefix_before_text():
    c = ch.Chunk(0, "Estratégia", "energia eólica no mar", "pt")
    ei = c.embed_input("Nota", "resources")
    assert ei.startswith("Contexto")
    assert ei.endswith("energia eólica no mar")
    # canonical task prefix is NOT here (the embedder adds query:/passage:)
    assert "query:" not in ei and "passage:" not in ei


# ---- IDX-03: incremental sync + delete-propagation --------------------------
def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: I\ntype: index\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nMap.\n", encoding="utf-8")
    (v / "brain" / "resources" / "a.md").write_text(
        "---\nid: a\ntitle: A\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\n# A\n\nAlpha content.\n",
        encoding="utf-8")
    return v


def test_sync_add_update_unchanged_delete(tmp_path):
    v = _vault(tmp_path)
    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=BruteForceBackend(),
                     embedder=HashEmbedder())
    idx.rebuild(v)
    # add a note, modify a.md, delete index.md
    (v / "brain" / "resources" / "b.md").write_text(
        "---\nid: b\ntitle: B\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nBravo.\n", encoding="utf-8")
    (v / "brain" / "resources" / "a.md").write_text(
        "---\nid: a\ntitle: A\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-28\n---\n\n# A\n\nAlpha CHANGED.\n",
        encoding="utf-8")
    (v / "brain" / "index.md").unlink()
    res = idx.sync(v)
    assert res["mode"] == "incremental"
    assert res["added"] == 1 and res["updated"] == 1 and res["deleted"] == 1
    assert idx.get("b") is not None
    assert idx.get("index") is None  # delete propagated
    assert "CHANGED" in idx.get("a")["body"]


def test_sync_unchanged_note_is_not_reindexed(tmp_path):
    v = _vault(tmp_path)
    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=BruteForceBackend(),
                     embedder=HashEmbedder())
    idx.rebuild(v)
    res = idx.sync(v)
    assert res["added"] == 0 and res["updated"] == 0 and res["deleted"] == 0
    assert res["unchanged"] == 2


def test_model_change_forces_clean_rebuild(tmp_path):
    v = _vault(tmp_path)
    db = tmp_path / "i.sqlite"
    BrainIndex(db_path=db, backend=BruteForceBackend(),
               embedder=HashEmbedder(dim=384)).rebuild(v)
    # different embed_dim => model mismatch => sync must rebuild, not upsert
    idx2 = BrainIndex(db_path=db, backend=BruteForceBackend(),
                      embedder=HashEmbedder(dim=256))
    res = idx2.sync(v)
    assert res["mode"].startswith("rebuild")
    assert idx2.get_meta("embed_dim") == "256"


# ---- snapshot publisher (atomic + generation manifest) ----------------------
def test_publish_snapshot_atomic_and_generation_increments(tmp_path):
    v = _vault(tmp_path)
    db = tmp_path / "i.sqlite"
    idx = BrainIndex(db_path=db, backend=BruteForceBackend(), embedder=HashEmbedder())
    idx.rebuild(v)
    idx.close()
    dest = tmp_path / "snap"
    m1 = publish_snapshot(db, dest)
    assert m1.generation == 1
    assert (dest / "index.snapshot.sqlite").is_file()
    assert m1.notes == 2
    m2 = publish_snapshot(db, dest)
    assert m2.generation == 2
    man = read_manifest(dest)
    assert man.generation == 2
    st = snapshot_status(dest)
    assert st["snapshot"] == "present" and st["generation"] == 2
    # absent snapshot reports cleanly
    assert snapshot_status(tmp_path / "nope")["snapshot"] == "absent"


# ---- drain-on-invoke (host capture drain) ----------------------------------
def test_drain_on_invoke_promotes_draft_when_signed(tmp_path, audit_key_env):
    v = _vault(tmp_path)
    drafts = v / ".brain" / "drafts"
    drafts.mkdir(parents=True)
    (drafts / "d1.md").write_text(
        "---\nid: d1\ntitle: Draft One\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\nstatus: draft\n---\n\nDrafted.\n",
        encoding="utf-8")
    core = BrainCore(vault=v, index=BrainIndex(
        db_path=tmp_path / "i.sqlite", backend=BruteForceBackend(),
        embedder=HashEmbedder()), audit_log=tmp_path / "audit.jsonl")
    res = core.sync(drain=True)
    assert res["drain"]["promoted"] == 1
    assert not (drafts / "d1.md").exists()        # draft consumed
    assert (v / "brain" / "resources" / "d1.md").is_file()  # promoted
    assert core.get("d1") is not None             # now indexed


def test_drain_fails_closed_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_AUDIT_KEY_PEM", raising=False)
    v = _vault(tmp_path)
    drafts = v / ".brain" / "drafts"
    drafts.mkdir(parents=True)
    (drafts / "d1.md").write_text(
        "---\nid: d1\ntitle: D\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nx.\n", encoding="utf-8")
    core = BrainCore(vault=v, index=BrainIndex(
        db_path=tmp_path / "i.sqlite", backend=BruteForceBackend(),
        embedder=HashEmbedder()), audit_log=tmp_path / "audit.jsonl")
    res = core.drain_drafts()
    assert res["promoted"] == 0 and res["skipped"] == 1
    assert (drafts / "d1.md").exists()  # left in place — never promoted unsigned
