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
            "Restricted: the secret Atlas counterparty negotiation terms."),
        "brain/projects/secret-merger.md": _note(
            "secret-merger", "Secret Merger", "Secret",
            "Secret: insider nonpublic merger information about the arctic deal."),
        # default-deny: no classification key at all
        "brain/resources/unlabelled.md": (
            "---\nid: unlabelled\ntitle: \"Unlabelled\"\ntype: note\n"
            "created: 2026-06-27\nupdated: 2026-06-27\n---\n\n"
            "This note has NO classification and must be default-denied (Secret).\n"),
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
