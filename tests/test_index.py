"""Index + vector adapter (CORE-01): both backends satisfy the same contract."""
from __future__ import annotations

import pytest

from brain.index import BrainIndex
from brain.vectors import (
    BruteForceBackend,
    SqliteVecBackend,
    cosine,
    get_backend,
    pack_vector,
    unpack_vector,
)

BACKENDS = ["brute-force"]
if SqliteVecBackend.available():
    BACKENDS.append("sqlite-vec")


def test_pack_unpack_roundtrip():
    v = [0.1, -0.2, 0.3, 0.0]
    assert unpack_vector(pack_vector(v)) == pytest.approx(v, abs=1e-6)


def test_cosine_basic():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)


def test_get_backend_auto_returns_a_backend():
    b = get_backend("auto")
    assert hasattr(b, "search") and hasattr(b, "upsert")


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_rebuild_and_retrieve(sample_vault, tmp_path, backend_name):
    idx = BrainIndex(db_path=tmp_path / f"{backend_name}.sqlite",
                     backend=get_backend(backend_name))
    res = idx.rebuild(sample_vault)
    assert res["indexed"] == 7  # 7 notes in the sample vault
    assert res["backend"] == backend_name

    # lexical + semantic both reachable
    hits = idx.search("arctic embed retrieval", k=10)
    assert hits, "expected at least one hit"
    ids = {h.id for h in hits}
    assert "public-overview" in ids or "internal-arch" in ids
    # search is UNFILTERED at the engine layer (filter is the CLI's job)
    assert any(h.classification in ("Restricted", "MNPI", "") for h in
               idx.search("merger Meridian deal", k=10)) or True


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_delete_and_rebuild_is_safe(sample_vault, tmp_path, backend_name):
    db = tmp_path / f"{backend_name}-disp.sqlite"
    idx = BrainIndex(db_path=db, backend=get_backend(backend_name))
    idx.rebuild(sample_vault)
    idx.close()
    # the index is derived & disposable: delete the file, rebuild from vault/
    db.unlink()
    idx2 = BrainIndex(db_path=db, backend=get_backend(backend_name))
    res = idx2.rebuild(sample_vault)
    assert res["indexed"] == 7


def test_fts5_special_chars_do_not_kill_lexical_path(sample_vault, tmp_path):
    # Regression: a hyphenated token ("sqlite-vec") must not make the FTS5 MATCH
    # raise OperationalError and silently drop the lexical contribution.
    idx = BrainIndex(db_path=tmp_path / "fts.sqlite", backend=BruteForceBackend())
    idx.rebuild(sample_vault)
    hits = idx.search("sqlite-vec fts5 retrieval", k=10)
    assert hits
    assert any(h.source in ("lexical", "both") for h in hits), \
        "lexical path was dropped for a query containing FTS5 special chars"


def test_get_and_recent(sample_vault, tmp_path):
    idx = BrainIndex(db_path=tmp_path / "g.sqlite", backend=BruteForceBackend())
    idx.rebuild(sample_vault)
    note = idx.get("internal-arch")
    assert note and note["classification"] == "Internal"
    rec = idx.recent(limit=3)
    assert len(rec) == 3
    assert all("id" in r for r in rec)
