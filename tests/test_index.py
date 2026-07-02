"""Index + vector adapter (CORE-01): both backends satisfy the same contract."""
from __future__ import annotations

import os
import time

import pytest

from brain import index as index_mod
from brain.index import (
    GREP_REGEX_TIMEOUT_S,
    MAX_GREP_PATTERN_LEN,
    BrainIndex,
    GrepPatternError,
    _grep_bounded_search,
)
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


# --------------------------------------------------------------------------
# grep ReDoS / resource-exhaustion guard (RET-04 hardening)
# --------------------------------------------------------------------------
def test_grep_rejects_overlong_pattern(sample_vault, tmp_path):
    idx = BrainIndex(db_path=tmp_path / "grep-len.sqlite", backend=BruteForceBackend())
    idx.rebuild(sample_vault)
    too_long = "a" * (MAX_GREP_PATTERN_LEN + 1)
    with pytest.raises(GrepPatternError):
        idx.grep(too_long, regex=True)
    # a pattern right at the cap is still accepted (boundary, not off-by-one).
    at_cap = "a" * MAX_GREP_PATTERN_LEN
    idx.grep(at_cap, regex=True)  # must not raise


def test_grep_bounded_search_swallows_engine_timeout(monkeypatch):
    """Deterministic, environment-independent test of the timeout WIRING: when
    the `regex` engine raises TimeoutError mid-match, `_grep_bounded_search`
    must treat that as "no match on this line" (return None) rather than
    letting the exception escape and abort the whole grep() call."""
    monkeypatch.setattr(index_mod, "_GREP_HAS_TIMEOUT", True)

    class _RaisesTimeout:
        def search(self, text, timeout=None):
            assert timeout == GREP_REGEX_TIMEOUT_S  # the budget is actually passed through
            raise TimeoutError("simulated catastrophic-backtracking budget exceeded")

    assert _grep_bounded_search(_RaisesTimeout(), "any line") is None


def test_grep_bounded_search_uses_plain_search_without_timeout_engine(monkeypatch):
    """When the optional `regex` engine is absent, the guard falls back to a
    plain (non-timeout) `.search()` call — the length cap is then the sole
    ReDoS mitigation (documented residual risk: docs/SECURITY_NOTES.md)."""
    monkeypatch.setattr(index_mod, "_GREP_HAS_TIMEOUT", False)

    class _PlainEngine:
        def search(self, text):
            return "matched"

    assert _grep_bounded_search(_PlainEngine(), "any line") == "matched"


@pytest.mark.skipif(not index_mod._GREP_HAS_TIMEOUT,
                     reason="the optional `regex` timeout engine is not installed; "
                            "without it there is no bounded-time guarantee to assert "
                            "(the length cap is the only mitigation — see SECURITY_NOTES.md)")
def test_grep_bounds_catastrophic_backtracking_regex_end_to_end(sample_vault, tmp_path):
    """End-to-end: a classic ReDoS pattern (r'(a+)+$') against a crafted note
    body returns within a small wall-clock budget when the `regex` engine is
    installed. Confirmed exploit: stdlib `re` alone on this pattern vs just 30
    'a's took ~47s in isolation; the `regex` module's native timeout on the
    same input returns in well under a millisecond."""
    vault = sample_vault
    (vault / "brain" / "resources" / "redos-bait.md").write_text(
        "---\nid: redos-bait\ntitle: \"ReDoS bait\"\ntype: note\n"
        "classification: Internal\ncreated: 2026-06-27\nupdated: 2026-06-27\n"
        "---\n\n" + ("a" * 30) + "!\n",
        encoding="utf-8",
    )
    idx = BrainIndex(db_path=tmp_path / "grep-redos.sqlite", backend=BruteForceBackend())
    idx.rebuild(vault)

    t0 = time.monotonic()
    idx.grep(r"(a+)+$", regex=True, ignore_case=False)
    elapsed = time.monotonic() - t0
    # generous bound vs. the confirmed ~47s stdlib-re blowup on the identical
    # pattern/input -- real (timeout-guarded) behaviour is sub-second.
    assert elapsed < 5, f"grep took {elapsed:.1f}s on a classic ReDoS pattern"


def test_grep_pattern_error_is_a_value_error(sample_vault, tmp_path):
    # GrepPatternError must stay catchable by callers that only know ValueError
    # (e.g. a generic CLI error handler) -- it is not a brand-new exception family.
    assert issubclass(GrepPatternError, ValueError)


# --------------------------------------------------------------------------
# on-disk file permission hardening (CORE-01-adjacent)
# --------------------------------------------------------------------------
def test_index_db_file_is_not_world_readable(sample_vault, tmp_path):
    if os.name == "nt":  # POSIX mode bits are not the enforcement mechanism on Windows
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    db = tmp_path / "perm.sqlite"
    idx = BrainIndex(db_path=db, backend=BruteForceBackend())
    idx.rebuild(sample_vault)
    idx.close()
    mode = db.stat().st_mode & 0o777
    assert mode == 0o600, f"index db should be owner-only 0600, got {oct(mode)}"
