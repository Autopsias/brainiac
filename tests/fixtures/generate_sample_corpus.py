#!/usr/bin/env python3
"""Materialise tests/fixtures/sample_corpus/ — the CUT-05 POPULATED fixture
corpus deterministically.

Run once (``python3 tests/fixtures/generate_sample_corpus.py``) whenever the
corpus needs regenerating; the output .md files are committed so tests read a
static, reviewable fixture rather than regenerating it on every run. This is
the SYNTHETIC fixture (a representative sample, not the live vault) — the
live-vault index is built later in C-s10 per docs/cutover/brain-cli-gaps.md
and sessions/s03.context.md.

Coverage, by design (so `brain check/health/curate/integrity/promote-scan/
maintain` exercise REAL findings, not an empty index):
  * all five classification tiers + 2 unclassified/invalid-tier notes
    (curate's unclassified_notes lint has something to find)
  * an INTENTIONAL near-duplicate pair (integrity's near-dup scan has a real
    hit to surface, gated through egress like any other pair)
  * raw/ sources not yet promoted to a typed brain/ note (promote-scan)
  * a wikilink hub + spokes (graph-expand)
  * updated dates spread over the past 3 weeks (recent/digest)
  * brain/resources, brain/projects, brain/areas, raw/ zones
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent / "sample_corpus"


def _note(rel: str, *, nid: str, title: str, classification: str | None,
          body: str, created: str, updated: str, links: tuple[str, ...] = (),
          extra_frontmatter: str = "") -> None:
    cls_line = f"classification: {classification}\n" if classification is not None else ""
    linktext = (" " + " ".join(f"[[{l}]]" for l in links)) if links else ""
    text = (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n{cls_line}"
        f"created: {created}\nupdated: {updated}\n{extra_frontmatter}---\n\n"
        f"{body}{linktext}\n"
    )
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _source(rel: str, *, nid: str, captured: str, body: str) -> None:
    text = (
        f"---\nid: {nid}\ntype: source\nclassification: Internal\n"
        f"captured: {captured}\norigin: verbal\nimmutable: true\n"
        f"sha256: deadbeef{nid}\n---\n\n{body}\n"
    )
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build() -> None:
    # -- brain/resources: curated reference notes across tiers ------------
    _note(
        "brain/resources/hub.md", nid="hub", title="Sample Corpus Hub",
        classification="Internal",
        body="Hub note for the CUT-05 fixture corpus. Links every spoke note "
             "so graph-expand has a real wikilink neighbourhood to walk.",
        created="2026-06-08", updated="2026-06-08",
        links=("public-overview", "internal-architecture", "confidential-pricing",
               "restricted-deal", "mnpi-merger"),
    )
    _note(
        "brain/resources/public-overview.md", nid="public-overview",
        title="Public Overview", classification="Public",
        body="A public note about the second-brain engine: markdown truth, "
             "a derived sqlite-vec/FTS5 index, deny-by-default egress.",
        created="2026-06-09", updated="2026-06-09",
    )
    _note(
        "brain/resources/internal-architecture.md", nid="internal-architecture",
        title="Internal Architecture", classification="Internal",
        body="Internal note: the brain engine fuses FTS5 lexical search with a "
             "dense vector backend (sqlite-vec, brute-force fallback) via RRF.",
        created="2026-06-10", updated="2026-06-12",
    )
    _note(
        "brain/resources/confidential-pricing.md", nid="confidential-pricing",
        title="Confidential Pricing", classification="Confidential",
        body="Confidential pricing model details for the sample fixture deal.",
        created="2026-06-11", updated="2026-06-13",
    )
    _note(
        "brain/resources/retrieval-notes.md", nid="retrieval-notes",
        title="Retrieval Notes", classification="Public",
        body="RRF(60) fuses BM25 + dense rankings by reciprocal rank, not raw "
             "score, so the two retrievers never need score reconciliation.",
        created="2026-06-14", updated="2026-06-18",
    )

    # -- brain/projects: tiered project notes, incl. the near-dup pair ----
    _note(
        "brain/projects/restricted-deal.md", nid="restricted-deal",
        title="Restricted Deal", classification="Restricted",
        body="Restricted: the secret Meridian counterparty negotiation terms "
             "for the sample fixture deal.",
        created="2026-06-12", updated="2026-06-15",
    )
    _note(
        "brain/projects/mnpi-merger.md", nid="mnpi-merger",
        title="MNPI Merger", classification="MNPI",
        body="MNPI: material non-public merger information about the sample "
             "fixture acquisition.",
        created="2026-06-13", updated="2026-06-16",
    )
    # INTENTIONAL near-duplicate pair (integrity's near-dup scan target).
    _note(
        "brain/projects/quarterly-review-draft.md", nid="quarterly-review-draft",
        title="Quarterly Review Draft", classification="Internal",
        body="Quarterly review summary: revenue grew steadily, the fixture "
             "deal pipeline stayed healthy, and the team closed three "
             "workstreams ahead of schedule this quarter.",
        created="2026-06-19", updated="2026-06-19",
    )
    _note(
        # Single-word edit from quarterly-review-draft ("healthy" -> "strong")
        # — a realistic near-dup (an edited draft of the same note), and
        # empirically >=0.95 cosine even under the crude HashEmbedder fallback
        # used by offline tests (see generate_sample_corpus.py module docstring).
        "brain/projects/quarterly-review-draft-v2.md", nid="quarterly-review-draft-v2",
        title="Quarterly Review Draft v2", classification="Internal",
        body="Quarterly review summary: revenue grew steadily, the fixture "
             "deal pipeline stayed strong, and the team closed three "
             "workstreams ahead of schedule this quarter.",
        created="2026-06-20", updated="2026-06-20",
    )

    # -- brain/areas: areas-of-responsibility notes, incl. unclassified ----
    _note(
        "brain/areas/area-overview.md", nid="area-overview",
        title="Area Overview", classification="Public",
        body="Area-of-responsibility overview note for the fixture corpus.",
        created="2026-06-15", updated="2026-06-21",
    )
    # Unclassified (no classification key at all) -> curate's lint target.
    _note(
        "brain/areas/unclassified-note.md", nid="unclassified-note",
        title="Unclassified Note", classification=None,
        body="This note carries NO classification key and must be default-"
             "denied (MNPI) and flagged by curate's unclassified-notes lint.",
        created="2026-06-22", updated="2026-06-22",
    )
    # Invalid/unrecognised classification value -> also default-denied.
    _note(
        "brain/areas/bad-tier-note.md", nid="bad-tier-note",
        title="Bad Tier Note", classification="internal",  # wrong case
        body="This note has a WRONG-CASE classification value ('internal' not "
             "'Internal') — default-denied, and a casing-mismatch diagnostic.",
        created="2026-06-23", updated="2026-06-23",
    )

    # -- raw/: sources not yet promoted -> promote-scan candidates --------
    _source(
        "raw/2026-06-24-fixture-call-notes.md", nid="fixture-call-notes",
        captured="2026-06-24",
        body="Verbatim capture: a call about the fixture deal pipeline. "
             "Not yet promoted into a typed brain/ note.",
    )
    _source(
        "raw/2026-06-25-fixture-research-clip.md", nid="fixture-research-clip",
        captured="2026-06-25",
        body="Verbatim capture: a research clipping about retrieval evals. "
             "Not yet promoted into a typed brain/ note.",
    )

    # -- a few more recent notes so digest/recent have spread --------------
    _note(
        "brain/resources/recent-update-1.md", nid="recent-update-1",
        title="Recent Update 1", classification="Internal",
        body="A recently-updated fixture note, for brain digest --days 7 "
             "coverage.",
        created="2026-06-26", updated="2026-06-27",
    )
    _note(
        "brain/resources/recent-update-2.md", nid="recent-update-2",
        title="Recent Update 2", classification="Public",
        body="Another recently-updated fixture note, for brain recent -n "
             "coverage and the maintain umbrella's digest branch.",
        created="2026-06-27", updated="2026-06-28",
    )


if __name__ == "__main__":
    build()
    n = sum(1 for _ in ROOT.rglob("*.md"))
    print(f"wrote {n} fixture notes under {ROOT}")
