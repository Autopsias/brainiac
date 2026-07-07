"""TMP-02/TMP-03 — supersession engine: indexed bitemporal columns, bases-query
--latest-only/--as-of, and the audited `brain supersede` verb (ADR-0003 Ruling
2/8). Offline + deterministic: HashEmbedder + BruteForceBackend, env-injected
audit key (see conftest.audit_key_env)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import cli
from brain.core import BrainCore, RoleError
from brain.embed import HashEmbedder
from brain.index import BrainIndex
from brain.vectors import BruteForceBackend


def _note(nid, title, classification, body, extra="", *, updated="2026-01-01"):
    cls_line = f"classification: {classification}\n" if classification else ""
    return (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n{cls_line}"
        f"created: 2026-01-01\nupdated: {updated}\n{extra}---\n\n{body}\n"
    )


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "brain" / "resources").mkdir(parents=True)
    (v / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n\nMap.\n", encoding="utf-8")
    (v / "brain" / "resources" / "old.md").write_text(
        _note("old", "Old Choice", "Internal", "arctic embed choice body.",
              "document_date: 2026-02-01\n"), encoding="utf-8")
    (v / "brain" / "resources" / "new.md").write_text(
        _note("new", "New Choice", "Internal", "e5-small choice body."),
        encoding="utf-8")
    return v


@pytest.fixture
def core(tmp_path, audit_key_env, monkeypatch):
    vault = _vault(tmp_path)
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path / "runtime"))
    idx = BrainIndex(db_path=tmp_path / "index.sqlite",
                     backend=BruteForceBackend(), embedder=HashEmbedder())
    idx.rebuild(vault)
    return BrainCore(vault=vault, index=idx, audit_log=tmp_path / "audit.jsonl", role="host")


# --------------------------------------------------------------------------
# indexed columns + temporal queries
# --------------------------------------------------------------------------
def test_bitemporal_columns_indexed_and_default_current(core):
    rows = {r["id"]: r for r in core.bases_query({}, k=50)}
    assert rows["old"]["is_latest_version"] == ""  # unset -> current by default


def test_latest_only_excludes_superseded_note(core):
    core.supersede("old", "new")
    latest_ids = {r["id"] for r in core.bases_query({}, k=50, latest_only=True)}
    assert "old" not in latest_ids
    assert "new" in latest_ids
    all_ids = {r["id"] for r in core.bases_query({}, k=50)}
    assert "old" in all_ids  # not latest_only: still retrievable


def test_as_of_includes_the_pre_supersession_note(core):
    core.supersede("old", "new", reason="switched embedder")
    # old's document_date is 2026-02-01, and it is superseded only "today" (the
    # test run date, well after 2026-02-15) -- as-of that date it was current.
    as_of_ids = {r["id"] for r in core.bases_query({}, k=50, as_of="2026-02-15")}
    assert "old" in as_of_ids


def test_as_of_before_document_date_excludes_note(core):
    # old's document_date is 2026-02-01 -- as-of a date before that, it did not
    # exist yet (fallback chain: effective_date, else document_date, else created).
    as_of_ids = {r["id"] for r in core.bases_query({}, k=50, as_of="2026-01-15")}
    assert "old" not in as_of_ids


def test_as_of_excludes_note_after_it_was_superseded(core):
    core.supersede("old", "new")
    import datetime as _dt
    future = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    as_of_ids = {r["id"] for r in core.bases_query({}, k=50, as_of=future)}
    assert "old" not in as_of_ids  # superseded by then
    assert "new" in as_of_ids


# --------------------------------------------------------------------------
# supersede: happy path + is_latest_version surfaced on search/get
# --------------------------------------------------------------------------
def test_supersede_writes_both_sides_and_reindexes(core):
    res = core.supersede("old", "new", reason="switched embedder")
    assert res["old_id"] == "old" and res["new_id"] == "new"
    old_text = (core.vault / "brain/resources/old.md").read_text()
    new_text = (core.vault / "brain/resources/new.md").read_text()
    assert "superseded_by: new" in old_text
    assert "is_latest_version: false" in old_text
    assert "previous_version: old" in new_text
    assert "is_latest_version: true" in new_text
    got_old = core.get("old")
    got_new = core.get("new")
    assert got_old["is_latest_version"] == "false"
    assert got_new["is_latest_version"] == "true"


def test_supersede_self_refused(core):
    with pytest.raises(ValueError):
        core.supersede("old", "old")


def test_supersede_refuses_reserving_already_superseded_old(core):
    core.supersede("old", "new")
    (core.vault / "brain/resources/third.md").write_text(
        _note("third", "Third", "Internal", "third body."), encoding="utf-8")
    core.sync(drain=False)
    with pytest.raises(ValueError, match="already superseded"):
        core.supersede("old", "third")


def test_supersede_refuses_second_latest_target(core):
    # new is already itself retired (is_latest_version: false) -- refuse making
    # it a "latest" a second time.
    (core.vault / "brain/resources/new.md").write_text(
        _note("new", "New Choice", "Internal", "e5-small choice body.",
              "superseded_by: someone-else\nis_latest_version: false\n"),
        encoding="utf-8")
    core.sync(drain=False)
    with pytest.raises(ValueError, match="second latest"):
        core.supersede("old", "new")


def test_supersede_refuses_missing_successor_classification(core):
    # [HARDENED:grill] classification is NEVER inherited implicitly.
    (core.vault / "brain/resources/new.md").write_text(
        _note("new", "New Choice", None, "e5-small choice body."), encoding="utf-8")
    core.sync(drain=False)
    with pytest.raises(ValueError, match="classification"):
        core.supersede("old", "new")
    # side-effect-free: old must be untouched.
    old_text = (core.vault / "brain/resources/old.md").read_text()
    assert "superseded_by" not in old_text


# --------------------------------------------------------------------------
# atomicity: failure injection between the two signed writes
# --------------------------------------------------------------------------
def test_supersede_atomic_across_failure_between_writes(core):
    orig_write = core.write_note
    calls = []

    def flaky(rel_path, content, reason="", *, subtree=None):
        calls.append(rel_path)
        if len(calls) == 2:
            raise RuntimeError("simulated crash between the two writes")
        return orig_write(rel_path, content, reason=reason, subtree=subtree)

    core.write_note = flaky
    with pytest.raises(RuntimeError):
        core.supersede("old", "new")
    # old WAS written+signed (half-chain on disk); new was not.
    old_text = (core.vault / "brain/resources/old.md").read_text()
    assert "superseded_by: new" in old_text
    new_text = (core.vault / "brain/resources/new.md").read_text()
    assert "previous_version" not in new_text
    journal = core._supersede_journal_path()
    assert journal.exists()

    # Next invocation rolls the half-chain back BEFORE doing anything else, then
    # (since the precondition is restored) the retry succeeds cleanly.
    core.write_note = orig_write
    res = core.supersede("old", "new")
    assert res["old_id"] == "old"
    assert not journal.exists()
    old_text = (core.vault / "brain/resources/old.md").read_text()
    assert "superseded_by: new" in old_text
    new_text = (core.vault / "brain/resources/new.md").read_text()
    assert "previous_version: old" in new_text


# --------------------------------------------------------------------------
# VM refusal: BEFORE any signing-key resolution / WAL / index mutation
# --------------------------------------------------------------------------
def test_supersede_vm_refused_before_any_side_effect(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path / "runtime"))
    idx = BrainIndex(db_path=tmp_path / "index.sqlite",
                     backend=BruteForceBackend(), embedder=HashEmbedder())
    idx.rebuild(vault)

    calls = {"resolve": 0}
    import brain.audit as audit_mod
    monkeypatch.setattr(audit_mod, "resolve_signing_key",
                        lambda *a, **k: calls.__setitem__("resolve", calls["resolve"] + 1))

    vm_core = BrainCore(vault=vault, index=idx, role="vm")
    assert vm_core.audit is None  # VM never even constructs a signing surface
    before = (vault / "brain/resources/old.md").read_text()
    with pytest.raises(RoleError):
        vm_core.supersede("old", "new")
    assert calls["resolve"] == 0
    assert (vault / "brain/resources/old.md").read_text() == before  # no side effect
    assert not (vault / ".brain" / "supersede-pending.json").exists()


def test_cli_supersede_refused_on_vm_role(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    monkeypatch.setenv("BRAIN_VAULT", str(vault))
    monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
    rc = cli.main(["--role", "vm", "supersede", "old", "new", "--json"])
    assert rc == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "role_forbidden"


# --------------------------------------------------------------------------
# TMP-03: CLI help advertises the new temporal surface
# --------------------------------------------------------------------------
def test_cli_help_advertises_temporal_flags(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for token in ("supersede", "--latest-only", "--as-of", "temporal-intent"):
        assert token in out, f"--help missing temporal token: {token!r}"
