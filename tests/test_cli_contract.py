"""CLI contract (CORE-02): search/get/recent --json + the egress filter at stdout."""
from __future__ import annotations

import json

import pytest

from brain import cli
from brain.core import BrainCore
from brain.index import BrainIndex
from brain.vectors import get_backend


@pytest.fixture
def built(sample_vault, monkeypatch, tmp_path):
    # Force the brute-force backend so the contract test is backend-independent.
    monkeypatch.setenv("BRAIN_VAULT", str(sample_vault))
    monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
    idx = BrainIndex(db_path=tmp_path / "idx" / "index.sqlite",
                     backend=get_backend("brute-force"))
    core = BrainCore(vault=sample_vault, index=idx)
    core.rebuild()
    # Patch BrainCore so the CLI uses our pre-built in-memory-ish index.
    monkeypatch.setattr(cli, "BrainCore", lambda vault=None, role=None: core)
    return core


def run(capsys, *argv) -> tuple[int, str]:
    rc = cli.main(list(argv))
    out = capsys.readouterr().out
    return rc, out


def test_vault_is_top_level_before_subcommand(capsys):
    # F-01: --vault is a TOP-LEVEL option; it parses before the subcommand and
    # is rejected after it (matching the documented contract).
    p = cli.build_parser()
    ns = p.parse_args(["--vault", "x", "rebuild"])
    assert ns.vault == "x" and ns.cmd == "rebuild"
    with pytest.raises(SystemExit):
        p.parse_args(["rebuild", "--vault", "x"])  # unrecognised after subcommand


def test_search_json_shape_and_sourced(built, capsys):
    rc, out = run(capsys, "search", "arctic embed retrieval", "--json", "-k", "10")
    assert rc == 0
    payload = json.loads(out)
    assert "results" in payload and "egress" in payload
    for hit in payload["results"]:
        assert set(hit) >= {"id", "title", "classification", "path", "score", "source"}
        assert hit["source"] in ("lexical", "semantic", "both")
    # default max-tier Internal: no Confidential/Restricted/MNPI/unlabelled surfaced
    tiers = {h["classification"] for h in payload["results"]}
    assert tiers <= {"Public", "Internal"}


def test_search_default_deny_filters_sensitive(built, capsys):
    rc, out = run(capsys, "search", "arctic deal merger pricing Meridian", "--json", "-k", "10")
    payload = json.loads(out)
    ids = {h["id"] for h in payload["results"]}
    assert "confidential-pricing" not in ids
    assert "restricted-deal" not in ids
    assert "mnpi-merger" not in ids
    assert "unlabelled" not in ids
    assert payload["egress"]["withheld"] >= 1


def test_search_elevation_surfaces_confidential(built, capsys):
    rc, out = run(capsys, "search", "pricing", "--json", "--max-tier", "Confidential")
    payload = json.loads(out)
    ids = {h["id"] for h in payload["results"]}
    assert "confidential-pricing" in ids
    # but restricted/mnpi still withheld at Confidential cap
    assert "restricted-deal" not in ids and "mnpi-merger" not in ids


def test_get_withheld_returns_nonzero(built, capsys):
    rc, out = run(capsys, "get", "restricted-deal", "--json")
    assert rc == 2
    assert json.loads(out)["error"] == "withheld_by_egress_filter"


def test_get_surfaced_with_elevation(built, capsys):
    rc, out = run(capsys, "get", "restricted-deal", "--json", "--max-tier", "Restricted")
    assert rc == 0
    assert json.loads(out)["id"] == "restricted-deal"


def test_get_not_found(built, capsys):
    rc, out = run(capsys, "get", "does-not-exist", "--json")
    assert rc == 1
    assert json.loads(out)["error"] == "not_found"


def test_recent_json_and_filtered(built, capsys):
    rc, out = run(capsys, "recent", "--json", "-n", "20")
    assert rc == 0
    payload = json.loads(out)
    tiers = {it["classification"] for it in payload["results"]}
    assert tiers <= {"Public", "Internal"}
    assert payload["egress"]["withheld"] >= 1


def test_unlabelled_never_surfaces_even_at_restricted(built, capsys):
    rc, out = run(capsys, "recent", "--json", "-n", "20", "--max-tier", "Restricted")
    ids = {it["id"] for it in json.loads(out)["results"]}
    assert "unlabelled" not in ids  # default-deny: only an MNPI cap surfaces it


def test_rebuild_failure_emits_json_error_not_traceback(built, capsys):
    # H-4: force core.rebuild() to blow up; the CLI must emit the documented
    # {"error":..., "detail":...} JSON shape + exit 3, never a raw traceback.
    def boom():
        raise RuntimeError("disk exploded")
    built.rebuild = boom
    rc, out = run(capsys, "rebuild", "--json")
    assert rc == 3
    payload = json.loads(out)  # raises if a traceback leaked instead of JSON
    assert payload["error"] == "RuntimeError"
    assert "disk exploded" in payload["detail"]


def test_sync_failure_emits_json_error_not_traceback(built, capsys):
    def boom(drain=True, publish=False):
        raise RuntimeError("sync exploded")
    built.sync = boom
    rc, out = run(capsys, "sync", "--json")
    assert rc == 3
    payload = json.loads(out)
    assert payload["error"] == "RuntimeError"


def test_snapshot_failure_emits_json_error_not_traceback(built, capsys):
    def boom(dest):
        raise RuntimeError("snapshot exploded")
    built.publish_snapshot = boom
    rc, out = run(capsys, "snapshot", "--json")
    assert rc == 3
    payload = json.loads(out)
    assert payload["error"] == "RuntimeError"


def test_help_is_self_describing(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for token in ("search", "get", "recent", "rebuild", "project", "write",
                  "verify-audit", "--json", "--max-tier", "deny-by-default",
                  "containment"):
        assert token in out, f"--help missing self-describing token: {token!r}"
