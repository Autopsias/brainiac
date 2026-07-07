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


# ---- S11: the implicit auto->hash fallback must never be SILENT --------------
def _force_no_real_embedder(monkeypatch):
    """Make every real embedder report unavailable + clear the model env, so
    get_embedder('auto') is driven onto the implicit HashEmbedder path."""
    import brain.embed as em

    monkeypatch.delenv("BRAIN_EMBED_MODEL", raising=False)
    monkeypatch.setattr(em.OnnxEmbedder, "available", staticmethod(lambda: False))
    monkeypatch.setattr(em.ArcticEmbedder, "available", staticmethod(lambda: False))
    monkeypatch.setattr(em.CatalogEmbedder, "available", staticmethod(lambda: False))
    monkeypatch.setattr(em.QwenEmbedder, "available", staticmethod(lambda: False))


def test_auto_hash_fallback_warns_loudly(monkeypatch, capsys):
    _force_no_real_embedder(monkeypatch)
    monkeypatch.delenv("BRAIN_REQUIRE_REAL_EMBEDDER", raising=False)
    e = get_embedder("auto")
    assert e.model_id == "hash-v1"
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "HashEmbedder" in err  # the loud, actionable message reached stderr


def test_auto_hash_fallback_fails_closed_when_required(monkeypatch):
    from brain.embed import EmbedderUnavailable

    _force_no_real_embedder(monkeypatch)
    monkeypatch.setenv("BRAIN_REQUIRE_REAL_EMBEDDER", "1")
    with pytest.raises(EmbedderUnavailable):
        get_embedder("auto")


def test_explicit_hash_does_not_warn(monkeypatch, capsys):
    # prefer='hash' is an explicit, intentional selection (tests/CI) — silent.
    monkeypatch.delenv("BRAIN_REQUIRE_REAL_EMBEDDER", raising=False)
    e = get_embedder("hash")
    assert e.model_id == "hash-v1"
    assert "WARNING" not in capsys.readouterr().err


def test_status_surfaces_live_embedder(tmp_path, audit_key_env):
    # brain status must report the LIVE embedder in use, flagged distinctly from
    # the index's recorded metadata, so a silent semantic downgrade is visible.
    idx = BrainIndex(db_path=tmp_path / "idx.sqlite",
                     backend=BruteForceBackend(), embedder=HashEmbedder())
    idx.rebuild(_repo_vault_or_tmp(tmp_path))
    core = BrainCore(vault=tmp_path, index=idx,
                     audit_log=tmp_path / "audit.jsonl", role="host")
    st = core.status()
    assert st["live_embedder"]["model_id"] == "hash-v1"
    assert st["live_embedder"]["is_hash_fallback"] is True


def _repo_vault_or_tmp(tmp_path: Path) -> Path:
    """A minimal vault dir with one note so rebuild has something to index."""
    v = tmp_path / "vault"
    (v / "brain").mkdir(parents=True)
    (v / "brain" / "n.md").write_text(
        "---\ntitle: N\nclassification: Internal\n---\nhello brain\n", encoding="utf-8")
    return v


def _local_e5_small_dir() -> str | None:
    """Locate the offline e5-small ONNX snapshot in the repo's model cache, if
    present. Returns a dir containing onnx/model.onnx + tokenizer.json, or None."""
    import os
    from glob import glob

    env = os.environ.get("BRAIN_MODEL_CACHE")
    cands: list[str] = []
    if env:
        cands.append(env)
    repo = Path(__file__).resolve().parent.parent
    cands += glob(str(repo / ".fastembed_cache"
                  / "models--Xenova--multilingual-e5-small" / "snapshots" / "*"))
    for d in cands:
        if d and Path(d, "tokenizer.json").exists() and Path(d, "onnx", "model.onnx").exists():
            return d
    return None


def test_onnx_embedder_truncates_over_512_tokens_without_crash():
    """Regression (S10 LV-01): e5-small is a BERT encoder capped at 512 position
    embeddings. Before the fix, a chunk that tokenised to >512 tokens crashed the
    whole rebuild with an ONNX broadcast FAIL ('512 by 620') because the tokenizer
    padded but never truncated. The embedder now enable_truncation(512), so an
    over-long input is clamped and embeds cleanly. Skips offline (no bundled model)."""
    from brain.embed import OnnxEmbedder

    local = _local_e5_small_dir()
    if not (OnnxEmbedder.available() and local):
        pytest.skip("e5-small ONNX model not available offline")
    e = OnnxEmbedder(local_dir=local)
    # ~2000 whitespace tokens -> far above the 512 cap; this is the exact input
    # class that previously raised onnxruntime FAIL on the position-embedding Add.
    over_long = "palavra " * 2000
    v = e.embed(over_long, is_query=False)
    assert len(v) == e.dim
    # L2-normalised output (proves a real forward pass completed, not a stub)
    assert abs(sum(x * x for x in v) - 1.0) < 1e-3


# ---- S09/PF-01: int8 quantization opt-in is a non-destructive kill-switch ---
def test_onnx_embedder_defaults_to_fp32_quantization():
    """No arg, no env var -> fp32, model_id UNCHANGED. This is the production
    default and must never move without an explicit opt-in (PF-01 addendum)."""
    from brain.embed import E5_SMALL_MODEL_ID, OnnxEmbedder

    e = OnnxEmbedder()
    assert e.quantization == "fp32"
    assert e.model_id == E5_SMALL_MODEL_ID
    assert e._onnx_file == "onnx/model.onnx"


def test_onnx_embedder_int8_opt_in_via_constructor_arg():
    from brain.embed import OnnxEmbedder

    e = OnnxEmbedder(quantization="int8")
    assert e.quantization == "int8"
    assert e.model_id == "intfloat/multilingual-e5-small-int8"
    assert e._onnx_file == "onnx/model_int8.onnx"
    # distinct model_id -> BrainIndex's embed_model/embed_dim guard forces a
    # clean rebuild before an int8-embedded index is queried with fp32
    # vectors or vice versa (same contract as any other embedder swap).


def test_onnx_embedder_int8_opt_in_via_env_var(monkeypatch):
    from brain.embed import OnnxEmbedder

    monkeypatch.setenv("BRAIN_EMBED_QUANT", "int8")
    e = OnnxEmbedder()
    assert e.quantization == "int8"
    assert e.model_id == "intfloat/multilingual-e5-small-int8"


def test_onnx_embedder_env_var_kill_switch_back_to_fp32(monkeypatch):
    """Explicit fp32 always wins over a stray env var — the kill-switch."""
    from brain.embed import E5_SMALL_MODEL_ID, OnnxEmbedder

    monkeypatch.setenv("BRAIN_EMBED_QUANT", "int8")
    e = OnnxEmbedder(quantization="fp32")
    assert e.quantization == "fp32"
    assert e.model_id == E5_SMALL_MODEL_ID


def test_onnx_embedder_rejects_unknown_quantization():
    from brain.embed import OnnxEmbedder

    with pytest.raises(ValueError):
        OnnxEmbedder(quantization="fp16")


def test_get_embedder_onnx_int8_prefer():
    """get_embedder('onnx-int8') is the explicit selector; 'onnx'/'auto' are
    UNCHANGED (still fp32 unless $BRAIN_EMBED_QUANT=int8 is ALSO set)."""
    from brain.embed import OnnxEmbedder, get_embedder

    e = get_embedder("onnx-int8")
    assert isinstance(e, OnnxEmbedder)
    assert e.quantization == "int8"

    e2 = get_embedder("onnx")
    assert isinstance(e2, OnnxEmbedder)
    assert e2.quantization == "fp32"


def test_int8_embedder_offline_metadata_without_loading_model():
    """Constructing the int8 arm does not touch the filesystem/model — same
    laziness contract as the fp32 constructor (test_arctic_embedder_metadata_
    without_loading_model above)."""
    from brain.embed import OnnxEmbedder

    e = OnnxEmbedder(quantization="int8", local_dir="/nonexistent/path")
    assert e.model_id == "intfloat/multilingual-e5-small-int8"
    assert e.dim == 384


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


def test_sync_survives_rename_same_id(tmp_path):
    """H-2 regression: renaming a note (same frontmatter id, new path) used to
    hit 'UNIQUE constraint failed: notes.id' because the insert pass ran
    before delete-propagation. Must reconcile cleanly to one row."""
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    note_body = (
        "---\nid: a\ntitle: A\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\n# A\n\nAlpha.\n"
    )
    old_path = v / "brain" / "resources" / "a.md"
    old_path.write_text(note_body, encoding="utf-8")
    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=BruteForceBackend(),
                     embedder=HashEmbedder())
    idx.rebuild(v)

    new_path = v / "brain" / "resources" / "a-renamed.md"
    old_path.rename(new_path)
    idx.sync(v)  # must not raise

    assert idx.get("a") is not None
    assert idx.get("a")["path"] == new_path.as_posix()
    row_count = idx.conn.execute(
        "SELECT COUNT(*) FROM notes WHERE id='a'"
    ).fetchone()[0]
    assert row_count == 1


# ---- H-3: bad-encoding files are skipped, not fatal --------------------------
def test_rebuild_skips_bad_encoding_file(tmp_path):
    v = tmp_path / "vault"
    (v / "brain").mkdir(parents=True)
    (v / "brain" / "good.md").write_text(
        "---\nid: good\ntitle: Good\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nfine.\n", encoding="utf-8")
    bad_path = v / "brain" / "bad.md"
    bad_path.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=BruteForceBackend(),
                     embedder=HashEmbedder())
    with pytest.warns(UserWarning):
        res = idx.rebuild(v)  # must not raise

    assert res["indexed"] == 1
    assert idx.get("good") is not None
    ids = {r[0] for r in idx.conn.execute("SELECT id FROM notes").fetchall()}
    assert "bad" not in ids
    paths = {r[0] for r in idx.conn.execute("SELECT path FROM notes").fetchall()}
    assert bad_path.relative_to(v).as_posix() not in paths


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
    monkeypatch.delenv("BRAIN_AUDIT_KEY_CMD", raising=False)
    monkeypatch.setenv("BRAIN_AUDIT_KEYCHAIN_SERVICE", "profile-a-brain-test-absent-xyz")
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


# ---- S05 T-1: sync (not just rebuild) must also skip a bad-encoding file ----
def test_sync_skips_bad_encoding_file_after_initial_rebuild(tmp_path):
    """H-3 covers rebuild; sync is a separate code path (incremental scan) and
    must degrade the same way — one malformed file must not abort the whole
    incremental reconcile nor leave the good notes unindexed."""
    v = _vault(tmp_path)
    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=BruteForceBackend(),
                     embedder=HashEmbedder())
    idx.rebuild(v)
    bad_path = v / "brain" / "resources" / "bad.md"
    bad_path.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    with pytest.warns(UserWarning):
        res = idx.sync(v)  # must not raise
    assert res["added"] >= 0  # completed without raising
    ids = {r[0] for r in idx.conn.execute("SELECT id FROM notes").fetchall()}
    assert "bad" not in ids
    # pre-existing notes are still there, untouched by the bad file
    assert idx.get("a") is not None


# ---- S05 T-1: concurrent sync — a second BrainIndex.sync() call against a
# DB another connection currently holds the write lock on must WAIT (via
# busy_timeout) and then complete cleanly, not hard-fail with "database is
# locked". (Two independent BrainIndex instances racing to insert the SAME
# new row from an unsynced starting point is a distinct, inherent
# logical-conflict outside sync()'s contract — not what busy_timeout is for —
# so this drives the realistic shape: one connection holds the write lock
# briefly, one BrainIndex.sync() call is in flight concurrently.)
def test_concurrent_brainindex_sync_waits_out_write_lock_instead_of_crashing(tmp_path):
    import sqlite3
    import threading
    import time

    v = _vault(tmp_path)
    db_path = tmp_path / "shared.sqlite"
    idx_a = BrainIndex(db_path=db_path, backend=BruteForceBackend(), embedder=HashEmbedder())
    idx_a.rebuild(v)
    idx_a.close()

    (v / "brain" / "resources" / "c1.md").write_text(
        "---\nid: c1\ntitle: C1\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nOne.\n", encoding="utf-8")

    errors: list[Exception] = []
    results: list[dict] = []

    def hold_write_lock():
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO meta (k, v) VALUES ('probe', '1')")
        time.sleep(0.3)
        conn.commit()
        conn.close()

    def run_sync():
        try:
            idx = BrainIndex(db_path=db_path, backend=BruteForceBackend(),
                             embedder=HashEmbedder())
            results.append(idx.sync(v))
            idx.close()
        except Exception as exc:  # pragma: no cover - would be the bug
            errors.append(exc)

    t1 = threading.Thread(target=hold_write_lock)
    t1.start()
    time.sleep(0.05)  # let the lock-holder go first
    t2 = threading.Thread(target=run_sync)
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"concurrent sync failed instead of queuing: {errors}"
    assert results and results[0]["added"] == 1
    final = BrainIndex(db_path=db_path, backend=BruteForceBackend(), embedder=HashEmbedder())
    row_count = final.conn.execute(
        "SELECT COUNT(*) FROM notes WHERE id='c1'"
    ).fetchone()[0]
    assert row_count == 1, f"c1 row_count={row_count}"
    final.close()
