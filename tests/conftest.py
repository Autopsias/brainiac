"""Shared fixtures: an in-repo sample vault and an injected Ed25519 audit key."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _note(nid, title, classification, body, *, zone="brain"):
    if zone == "raw":
        fm = (
            f"---\nid: {nid}\ntype: source\nclassification: {classification}\n"
            f"captured: 2026-06-27\norigin: verbal\nimmutable: true\n"
            f"sha256: deadbeef\n---\n\n{body}\n"
        )
    else:
        fm = (
            f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n"
            f"classification: {classification}\ncreated: 2026-06-27\n"
            f"updated: 2026-06-27\n---\n\n{body}\n"
        )
    return fm


@pytest.fixture
def sample_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "brain" / "resources").mkdir(parents=True)
    (vault / "brain" / "projects").mkdir(parents=True)
    (vault / "raw").mkdir(parents=True)

    notes = {
        "brain/index.md": _note("index", "Index", "Internal", "Map of the brain. type: index"),
        "brain/resources/public-overview.md": _note(
            "public-overview", "Public Overview", "Public",
            "A public note about arctic embed and retrieval over markdown."),
        "brain/resources/internal-arch.md": _note(
            "internal-arch", "Internal Architecture", "Internal",
            "Internal note: the brain engine uses sqlite-vec and fts5 for retrieval."),
        "brain/resources/confidential-pricing.md": _note(
            "confidential-pricing", "Confidential Pricing", "Confidential",
            "Confidential pricing model details for the arctic embed deal."),
        "brain/projects/restricted-deal.md": _note(
            "restricted-deal", "Restricted Deal", "Restricted",
            "Restricted: the secret Meridian counterparty negotiation terms."),
        "brain/projects/mnpi-merger.md": _note(
            "mnpi-merger", "MNPI Merger", "MNPI",
            "MNPI: material non-public merger information about the arctic deal."),
        # default-deny: no classification key at all
        "brain/resources/unlabelled.md": (
            "---\nid: unlabelled\ntitle: \"Unlabelled\"\ntype: note\n"
            "created: 2026-06-27\nupdated: 2026-06-27\n---\n\n"
            "This note has NO classification and must be default-denied (MNPI).\n"),
    }
    for rel, text in notes.items():
        (vault / rel).write_text(text, encoding="utf-8")
    return vault


@pytest.fixture
def audit_key_env(monkeypatch):
    """Inject a fresh Ed25519 key via env so the chain signs without a keystore."""
    from brain.audit import generate_key_pem

    priv_pem, _pub = generate_key_pem()
    monkeypatch.setenv("BRAIN_AUDIT_KEY_PEM", priv_pem.decode("utf-8"))
    return priv_pem


# -- CUT-05: the POPULATED fixture corpus (tests/fixtures/sample_corpus/) ---
# A representative ~16-note sample vault (all 5 classification tiers + 2
# unclassified/invalid-tier notes, an intentional near-dup pair, raw/ sources
# not yet promoted, a wikilink hub+spokes, dates spread over 3 weeks) built
# into a REAL sqlite-vec/FTS5-backed index — not an empty vault that would let
# a dry-run/self-eval false-green by finding nothing. See
# tests/fixtures/generate_sample_corpus.py for the corpus generator and
# docs/cutover/brain-cli-gaps.md G1 for the near-dup design rationale. This is
# the SYNTHETIC fixture; the LIVE-vault index is built later in C-s10.
SAMPLE_CORPUS_VAULT = Path(__file__).resolve().parent / "fixtures" / "sample_corpus"


@pytest.fixture
def populated_vault() -> Path:
    """Path to the committed, static CUT-05 sample corpus (read-only)."""
    assert SAMPLE_CORPUS_VAULT.is_dir(), (
        f"{SAMPLE_CORPUS_VAULT} missing — run "
        "`python3 tests/fixtures/generate_sample_corpus.py` to materialise it."
    )
    return SAMPLE_CORPUS_VAULT


@pytest.fixture
def populated_index(tmp_path, audit_key_env):
    """A REAL, built sqlite (brute-force backend, deterministic HashEmbedder)
    index over the CUT-05 sample corpus — offline, fast, reproducible. Builds
    into ``tmp_path`` (never mutates the committed fixture corpus)."""
    from brain.embed import HashEmbedder
    from brain.index import BrainIndex
    from brain.vectors import BruteForceBackend

    idx = BrainIndex(
        db_path=tmp_path / "fixture-index.sqlite",
        backend=BruteForceBackend(),
        embedder=HashEmbedder(),
    )
    idx.rebuild(SAMPLE_CORPUS_VAULT)
    return idx


@pytest.fixture
def populated_core(tmp_path, populated_index, audit_key_env, monkeypatch):
    """A host-role BrainCore wired to ``populated_index`` + an isolated audit
    chain log under ``tmp_path`` (never touches the real OS app-data dir).

    ``BRAIN_RUNTIME_DIR`` is redirected to ``tmp_path`` so anything that
    derives ``vault/.brain/...`` (capture-inbox, drafts, snapshot) writes
    OUTSIDE the committed, read-only ``sample_corpus/`` fixture tree — a
    capture-draft drain or snapshot publish must never mutate the fixture
    itself."""
    from brain.core import BrainCore

    monkeypatch.setenv("BRAIN_RUNTIME_DIR", str(tmp_path / "runtime"))
    return BrainCore(
        vault=SAMPLE_CORPUS_VAULT, index=populated_index,
        audit_log=tmp_path / "audit_chain.jsonl", role="host",
    )
