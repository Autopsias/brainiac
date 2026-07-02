"""SEC-01 — the deny-by-default egress gate fires on EVERY content-returning
subcommand (r2-codex): hybrid_search/search, grep, bases-query, graph-expand,
get/read, recent — plus the reranker output path. A later content path must not
bypass a gate a sibling path already enforces.

Each test drives the real ``brain.cli.main`` (the integration surface) over a
linked vault, captures the JSON, and asserts the sensitive tiers are withheld at
the default cap and surfaced only under an explicit ``--max-tier`` elevation.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from brain import cli, egress


def _note(nid, title, classification, body, links=()):
    cls_line = f"classification: {classification}\n" if classification else ""
    linktext = " ".join(f"[[{l}]]" for l in links)
    return (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n{cls_line}"
        f"created: 2026-06-27\nupdated: 2026-06-27\n---\n\n{body} {linktext}\n"
    )


@pytest.fixture
def linked_vault(tmp_path, monkeypatch):
    """A vault spanning all tiers, with a hub note wikilinking the sensitive ones
    (so graph-expand surfaces them as candidates that must be withheld)."""
    vault = tmp_path / "vault"
    (vault / "brain" / "resources").mkdir(parents=True)
    (vault / "brain" / "projects").mkdir(parents=True)
    files = {
        "brain/resources/hub.md": _note(
            "hub", "Hub", "Internal",
            "Atlas deal hub linking everything",
            links=["public-fact", "internal-fact", "confi-pricing", "restricted-deal", "top-secret"]),
        "brain/resources/public-fact.md": _note(
            "public-fact", "Public Fact", "Public", "Atlas public deal fact"),
        "brain/resources/internal-fact.md": _note(
            "internal-fact", "Internal Fact", "Internal", "Atlas internal deal fact"),
        "brain/resources/confi-pricing.md": _note(
            "confi-pricing", "Confidential Pricing", "Confidential",
            "Atlas confidential deal pricing"),
        "brain/projects/restricted-deal.md": _note(
            "restricted-deal", "Restricted Deal", "Restricted",
            "Atlas restricted deal counterparty secret"),
        "brain/projects/top-secret.md": _note(
            "top-secret", "Top Secret", "Secret",
            "Atlas secret deal insider nonpublic"),
        # unlabelled => default-deny (Secret)
        "brain/resources/unlabelled.md": _note(
            "unlabelled", "Unlabelled", "", "Atlas deal unlabelled note"),
    }
    for rel, text in files.items():
        (vault / rel).write_text(text, encoding="utf-8")

    # Isolate the index under tmp and point brute-force backend (offline).
    monkeypatch.setenv("BRAIN_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setenv("BRAIN_VAULT", str(vault))
    # build the index
    _run(["rebuild"])
    return vault


def _run(argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def _json(argv) -> tuple[int, dict]:
    code, out = _run(argv)
    return code, json.loads(out)


SENSITIVE_TOKENS = ("counterparty secret", "insider nonpublic")


# ---- the enumeration is exhaustive (guard against an un-tested new path) ----
def test_subcommand_enumeration_is_complete():
    # Every content-returning subcommand this test file exercises must be in the
    # canonical registry, and vice-versa (minus aliases handled together).
    exercised = {"search", "hybrid-search", "grep", "bases-query",
                 "graph-expand", "get", "read", "recent"}
    assert set(egress.CONTENT_RETURNING_SUBCOMMANDS) == exercised


@pytest.mark.parametrize("cmd", ["search", "hybrid-search"])
def test_search_aliases_gate(linked_vault, cmd):
    code, res = _json([cmd, "Atlas deal", "-k", "20", "--json"])
    assert code == 0
    ids = {r["id"] for r in res["results"]}
    assert "restricted-deal" not in ids and "top-secret" not in ids
    assert "unlabelled" not in ids
    assert res["egress"]["withheld"] >= 3
    # elevation surfaces them (the explicit human gate)
    _, hi = _json([cmd, "Atlas deal", "-k", "20", "--max-tier", "Secret", "--json"])
    hi_ids = {r["id"] for r in hi["results"]}
    assert {"restricted-deal", "top-secret", "unlabelled"} <= hi_ids


def test_search_rerank_path_still_gates(linked_vault):
    # the reranker re-orders the SAME hits; the gate must still fire after it
    code, res = _json(["search", "Atlas deal", "-k", "20", "--rerank", "--json"])
    assert code == 0
    ids = {r["id"] for r in res["results"]}
    assert not ({"restricted-deal", "top-secret", "unlabelled"} & ids)
    assert res["egress"]["withheld"] >= 3


def test_grep_gates(linked_vault):
    code, res = _json(["grep", "Atlas", "-k", "50", "--json"])
    assert code == 0
    ids = {r["id"] for r in res["results"]}
    assert not ({"restricted-deal", "top-secret", "unlabelled"} & ids)
    assert res["egress"]["withheld"] >= 3


def test_bases_query_gates(linked_vault):
    code, res = _json(["bases-query", "--where", "type=note", "-k", "50", "--json"])
    assert code == 0
    ids = {r["id"] for r in res["results"]}
    assert not ({"restricted-deal", "top-secret", "unlabelled"} & ids)
    # even a direct filter ON the restricted tier is withheld at the default cap
    _, direct = _json(["bases-query", "--where", "classification=Restricted", "--json"])
    assert direct["results"] == []
    assert direct["egress"]["withheld"] >= 1


def test_graph_expand_gates(linked_vault):
    # discovery candidates from the hub include the sensitive notes -> withheld
    code, res = _json(["graph-expand", "hub", "--depth", "2", "-k", "20", "--json"])
    assert code == 0
    ids = {r["id"] for r in res["results"]}
    assert "public-fact" in ids or "internal-fact" in ids  # low tiers reachable
    assert not ({"restricted-deal", "top-secret", "unlabelled"} & ids)
    assert res["egress"]["withheld"] >= 1


@pytest.mark.parametrize("cmd", ["get", "read"])
def test_get_read_withhold_sensitive(linked_vault, cmd):
    code, res = _json([cmd, "restricted-deal", "--json"])
    assert code == 2  # withheld_by_egress_filter exit code
    assert res["error"] == "withheld_by_egress_filter"
    # elevation surfaces it
    code2, res2 = _json([cmd, "restricted-deal", "--max-tier", "Restricted", "--json"])
    assert code2 == 0 and res2["id"] == "restricted-deal"


def test_recent_gates(linked_vault):
    code, res = _json(["recent", "-n", "50", "--json"])
    assert code == 0
    ids = {r["id"] for r in res["results"]}
    assert not ({"restricted-deal", "top-secret", "unlabelled"} & ids)
    assert res["egress"]["withheld"] >= 3


def test_no_sensitive_token_leaks_via_any_default_path(linked_vault):
    # belt-and-braces: the literal secret strings never appear in default output
    for argv in (["search", "Atlas deal", "-k", "20", "--json"],
                 ["grep", "Atlas", "-k", "50", "--json"],
                 ["bases-query", "--where", "type=note", "--json"],
                 ["graph-expand", "hub", "--json"],
                 ["recent", "-n", "50", "--json"]):
        _, out = _run(argv)
        for tok in SENSITIVE_TOKENS:
            assert tok not in out, f"{tok!r} leaked via {argv[0]}"
