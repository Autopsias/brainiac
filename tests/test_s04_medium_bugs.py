"""Regression tests for the S04 medium-severity bug fixes (M-1..M-6)."""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from brain import frontmatter
from brain import graph as G
from brain.index import BrainIndex, GrepPatternError
from brain.vectors import BruteForceBackend


# ---------------------------------------------------------------------------
# M-1 — grep --regex is refused (not silently unbounded) on the minimal build
# ---------------------------------------------------------------------------
def test_regex_grep_blocked_without_bounded_engine(tmp_path, monkeypatch):
    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend())
    idx.rebuild(tmp_path)  # empty vault is fine — we never reach the scan
    monkeypatch.setattr("brain.index._GREP_HAS_TIMEOUT", False)
    with pytest.raises(GrepPatternError, match="bounded match timeout"):
        idx.grep("(a+)+$", regex=True)


def test_plain_grep_still_works_without_bounded_engine(tmp_path, monkeypatch):
    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend())
    idx.rebuild(tmp_path)
    monkeypatch.setattr("brain.index._GREP_HAS_TIMEOUT", False)
    assert idx.grep("hello", regex=False) == []  # doesn't raise


# ---------------------------------------------------------------------------
# M-2 — off-host anchor detects a truncated audit-chain tail (folded into
# integrity()); documented via a covered-by-anchor.py.test_anchor.py suite,
# regression here proves it's wired into BrainCore.integrity().
# ---------------------------------------------------------------------------
def test_integrity_surfaces_anchor_truncation(populated_core, monkeypatch, tmp_path):
    from brain import anchor as A

    adir = tmp_path / "offhost-anchor"
    monkeypatch.setenv("BRAIN_ANCHOR_DIR", str(adir))
    populated_core.audit.append("write", "brain/a.md", "seed entry")
    populated_core.audit.append("write", "brain/b.md", "second entry")
    A.anchor(populated_core.audit.log_path, adir)  # anchor the current head

    # Truncate the live chain (drop the last entry) without re-signing.
    lines = populated_core.audit.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    populated_core.audit.log_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    res = populated_core.integrity(min_score=0.999, k=5)
    assert res["audit_issue"] is not None
    assert "anchor" in res["audit_issue"]["finding"].lower()


def test_integrity_documents_gap_when_no_anchor_configured(populated_core, monkeypatch):
    monkeypatch.delenv("BRAIN_ANCHOR_DIR", raising=False)
    res = populated_core.integrity(min_score=0.999, k=5)
    assert res["audit_issue"] is not None
    assert "no off-host anchor configured" in res["audit_issue"]["finding"]


# ---------------------------------------------------------------------------
# M-3 — drain_drafts rejects a duplicate id instead of writing a collision
# ---------------------------------------------------------------------------
def test_drain_drafts_skips_duplicate_id(populated_core, populated_vault):
    # An existing note already occupies this id in the vault.
    existing_id = "public-overview"
    inbox = populated_core.capture_inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    draft = inbox / f"{existing_id}.md"
    draft.write_text(
        f"---\nid: {existing_id}\ntitle: \"Dup\"\ntype: note\n"
        "classification: Internal\ncreated: 2026-06-27\nupdated: 2026-06-27\n"
        "---\n\nA colliding draft.\n",
        encoding="utf-8",
    )
    res = populated_core.drain_drafts()
    reasons = [s["reason"] for s in res["details"]["skipped"]]
    assert any("duplicate-id" in r for r in reasons)
    assert existing_id not in res["details"]["promoted"]


# ---------------------------------------------------------------------------
# M-4 — fallback (no-PyYAML) frontmatter parser strips a trailing comment
# ---------------------------------------------------------------------------
def test_fallback_parser_strips_inline_comment(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)  # force ImportError on `import yaml`
    block = "\nid: foo\nclassification: Internal  # note\ntitle: bar\n"
    data = frontmatter.parse(block)
    assert data["classification"] == "Internal"
    assert data["title"] == "bar"


def test_fallback_parser_keeps_hash_inside_quotes(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    block = "\nid: foo\ntitle: 'issue #42'\n"
    data = frontmatter.parse(block)
    assert data["title"] == "issue #42"


def test_fallback_parsed_classification_validates_against_tiers(monkeypatch):
    from brain import classification as C

    monkeypatch.setitem(sys.modules, "yaml", None)
    block = "\nclassification: Internal  # trailing comment\n"
    data = frontmatter.parse(block)
    assert C.normalize(data["classification"]) == "Internal"


# ---------------------------------------------------------------------------
# S05 T-1 — drain_drafts partway crash: one draft promoted, the loop then
# raises on the next draft. Assert the state is RECOVERABLE (no data loss, no
# duplicate, no crash-loop) on a follow-up drain.
# ---------------------------------------------------------------------------
def test_drain_partial_crash_leaves_recoverable_state(tmp_path, audit_key_env):
    from brain.core import BrainCore
    from brain.embed import HashEmbedder as _HashEmbedder
    from brain.vectors import BruteForceBackend as _BruteForceBackend

    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    core = BrainCore(
        vault=v,
        index=BrainIndex(db_path=tmp_path / "i.sqlite", backend=_BruteForceBackend(),
                         embedder=_HashEmbedder()),
        audit_log=tmp_path / "audit.jsonl",
    )
    core.rebuild()
    inbox = core.capture_inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "ok1.md").write_text(
        "---\nid: ok1\ntitle: Ok One\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nfine.\n", encoding="utf-8")
    (inbox / "ok2.md").write_text(
        "---\nid: ok2\ntitle: Ok Two\ntype: note\nclassification: Internal\n"
        "created: 2026-06-27\nupdated: 2026-06-27\n---\n\nalso fine.\n", encoding="utf-8")

    real_write_note = core.write_note
    calls = {"n": 0}

    def flaky_write_note(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash mid-drain")
        return real_write_note(*args, **kwargs)

    core.write_note = flaky_write_note
    with pytest.raises(RuntimeError):
        core.drain_drafts()
    core.write_note = real_write_note

    # one draft promoted-to-vault-but-not-yet-reindexed, the other still pending
    remaining = sorted(p.name for p in inbox.glob("*.md"))
    assert len(remaining) == 1  # the one that crashed before unlink() was never removed
    promoted_files = list((v / "brain" / "resources").glob("ok*.md"))
    assert len(promoted_files) == 1  # exactly one landed, no partial/duplicate file

    # a follow-up drain (the recovery path) finishes the job cleanly
    res = core.drain_drafts()
    assert res["promoted"] == 1
    assert list(inbox.glob("*.md")) == []
    core.sync(drain=False)
    assert core.get("ok1") is not None
    assert core.get("ok2") is not None
    for nid in ("ok1", "ok2"):
        row_count = core.index.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE id=?", (nid,)
        ).fetchone()[0]
        assert row_count == 1


# ---------------------------------------------------------------------------
# M-5 — wikilink parser doesn't drop links whose alias has nested brackets
# ---------------------------------------------------------------------------
def test_wikilink_alias_with_nested_brackets_resolves():
    targets = G.parse_wikilinks("See [[note-a|display [x] text]] for detail.")
    assert targets == ["note-a"]


def test_unterminated_wikilink_is_ignored_not_crashing():
    # No closing "]]" — must not raise, and must not spuriously match.
    assert G.parse_wikilinks("dangling [[note-a not closed") == []
    # A terminated link after an unterminated one is still picked up.
    assert G.parse_wikilinks("[[a]] then [[b unterminated") == ["a"]


def test_graph_resolver_nfc_nfd_id_mismatch_documented(tmp_path):
    """Characterization test: the wikilink resolver does exact/lowercased
    string matching with no unicode normalization. A note id stored in NFC
    form is NOT resolved by an NFD-encoded wikilink target of the same visual
    text (and vice versa) — document this as a known resolver limitation
    rather than silently regressing further (discovery-only surface, so a
    missed edge just falls into `unresolved`, never a crash)."""
    import unicodedata
    import sqlite3

    nfc_id = unicodedata.normalize("NFC", "café")  # "café" (composed)
    nfd_id = unicodedata.normalize("NFD", "café")  # "café" (decomposed)
    assert nfc_id != nfd_id  # sanity: genuinely different byte sequences

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE notes (id TEXT, title TEXT, path TEXT, body TEXT)")
    conn.execute(
        "INSERT INTO notes VALUES (?, ?, ?, ?)",
        (nfc_id, "Cafe", f"brain/{nfc_id}.md", "no links here"),
    )
    conn.execute(
        "INSERT INTO notes VALUES (?, ?, ?, ?)",
        ("hub", "Hub", "brain/hub.md", f"See [[{nfd_id}]] for detail."),
    )
    g = G.build_graph(conn)
    # Current behaviour: the NFD-form link target does not resolve to the
    # NFC-form note id — it lands in `unresolved`, never a crash or a wrong link.
    assert nfc_id not in g.out.get("hub", set())
    assert nfd_id in g.unresolved.get("hub", [])


def test_validate_wikilink_alias_with_nested_brackets_resolves():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import validate as V

    m = V.WIKILINK.search("[[note|display [x]]]")
    assert m is not None
    assert m.group(1) == "note"


# ---------------------------------------------------------------------------
# M-6 — PRAGMA busy_timeout is set so a concurrent writer waits instead of
# hard-failing with "database is locked".
# ---------------------------------------------------------------------------
def test_busy_timeout_set_on_write_connection(tmp_path):
    idx = BrainIndex(db_path=tmp_path / "idx.sqlite", backend=BruteForceBackend())
    conn = idx.conn
    (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
    assert timeout >= 5000


def test_concurrent_writers_queue_instead_of_crashing(tmp_path):
    db_path = tmp_path / "idx.sqlite"
    idx = BrainIndex(db_path=db_path, backend=BruteForceBackend())
    idx.rebuild(tmp_path)  # creates the schema

    errors: list[Exception] = []

    def hold_write_lock():
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO meta (k, v) VALUES ('probe', '1')")
        time.sleep(0.3)
        conn.commit()
        conn.close()

    t = threading.Thread(target=hold_write_lock)
    t.start()
    time.sleep(0.05)  # let the thread take the lock first
    try:
        conn2 = sqlite3.connect(str(db_path))
        conn2.execute("PRAGMA busy_timeout=5000")
        conn2.execute("INSERT INTO meta (k, v) VALUES ('probe2', '1')")
        conn2.commit()
        conn2.close()
    except sqlite3.OperationalError as exc:  # pragma: no cover - would be the bug
        errors.append(exc)
    t.join()
    assert not errors, f"concurrent writer failed instead of waiting: {errors}"
