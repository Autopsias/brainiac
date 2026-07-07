#!/usr/bin/env python3
"""Materialise tests/fixtures/synthetic_vault/ — a larger (~65-note), densely
linked synthetic vault fixture (HARDENED:claude, session s08).

The CUT-05 ``sample_corpus/`` fixture (16 notes, one hub + a handful of
spokes) is deliberately small and can't exercise rank/staleness/centrality
logic at any real scale — the revisit-sample scorer (AUT-02), s10's graphify
build, and s12's catalog/staleness work all need more than a handful of nodes
with real link density and a real age spread. This fixture is bigger and
INTENTIONALLY messier:

  * ~65 notes across brain/{areas,projects,resources,archive} + raw/
  * dense wikilinks (seeded RNG — deterministic; regenerate with this script,
    never hand-edit the .md files)
  * `created`/`updated` spread across ~2 years, so age-based ranking has real
    variance
  * every classification tier represented, plus a couple of unclassified/
    invalid-tier notes (curate coverage)
  * several links from still-ACTIVE notes into brain/archive/ (stale-link
    "archived" detection) and a handful of links to ids that never exist at
    all (stale-link "vanished" detection) — both deterministic, not
    RNG-dependent, so the fixture never regenerates itself into a false-green

Deterministic: same seed -> byte-identical output every run. Committed like
sample_corpus/ — tests read a static fixture, never regenerate it at
collection time. Reusable via the ``synthetic_vault`` / ``synthetic_index`` /
``synthetic_core`` pytest fixtures in tests/conftest.py.
"""
from __future__ import annotations

import datetime
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "synthetic_vault"
SEED = 20260705
ANCHOR = datetime.date(2026, 7, 5)  # the date the fixture's ages are relative to
TIERS = ["Public", "Internal", "Internal", "Confidential", "Restricted", "MNPI"]
ZONES = ["areas", "projects", "resources"]

N_ACTIVE = 55
N_ARCHIVE = 8


def _note(rel: str, *, nid: str, title: str, classification: str | None,
          body: str, created: str, updated: str, links: tuple[str, ...] = ()) -> None:
    cls_line = f"classification: {classification}\n" if classification is not None else ""
    linktext = (" " + " ".join(f"[[{link}]]" for link in links)) if links else ""
    text = (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: note\n{cls_line}"
        f"created: {created}\nupdated: {updated}\n---\n\n{body}{linktext}\n"
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


def _date_back(days: int) -> str:
    return (ANCHOR - datetime.timedelta(days=days)).isoformat()


def build() -> None:
    rng = random.Random(SEED)

    active_ids = [f"topic-{i:03d}" for i in range(N_ACTIVE)]
    archive_ids = [f"legacy-{i:03d}" for i in range(N_ARCHIVE)]

    # -- active notes: dense links, mixed ages/tiers -----------------------
    for i, nid in enumerate(active_ids):
        zone = ZONES[i % len(ZONES)]
        age_days = rng.randint(1, 730)  # last touched up to ~2 years back
        created_age = age_days + rng.randint(0, 60)  # created before updated
        tier = TIERS[i % len(TIERS)]
        candidates = [x for x in active_ids + archive_ids if x != nid]
        n_links = rng.randint(2, 5)
        links = list(rng.sample(candidates, k=min(n_links, len(candidates))))
        if i % 11 == 0:
            # deterministic (not RNG-dependent) vanished-target link, so the
            # "vanished" stale-link case is guaranteed present every run.
            links.append(f"deleted-idea-{i:03d}")
        _note(
            f"brain/{zone}/{nid}.md", nid=nid, title=f"Topic {i:03d}",
            classification=tier,
            body=f"Synthetic fixture note {i:03d} in {zone}/. Densely linked "
                 f"for revisit-sample / graphify-scale testing.",
            created=_date_back(created_age), updated=_date_back(age_days),
            links=tuple(links),
        )

    # -- archive notes: old, still linked-from active (stale-link target) --
    for i, nid in enumerate(archive_ids):
        _note(
            f"brain/archive/{nid}.md", nid=nid, title=f"Legacy {i:03d}",
            classification="Internal",
            body=f"Archived fixture note {i:03d} — superseded content, "
                 f"still linked from some active notes (stale-link fixture).",
            created=_date_back(900 + i * 10), updated=_date_back(800 + i * 10),
        )

    # -- a couple of unclassified/invalid-tier notes (curate coverage) -----
    _note(
        "brain/areas/unclassified-synthetic.md", nid="unclassified-synthetic",
        title="Unclassified Synthetic", classification=None,
        body="No classification key at all — default-denied.",
        created=_date_back(30), updated=_date_back(30),
    )
    _note(
        "brain/areas/bad-tier-synthetic.md", nid="bad-tier-synthetic",
        title="Bad Tier Synthetic", classification="internal",  # wrong case
        body="Wrong-case classification value — default-denied.",
        created=_date_back(20), updated=_date_back(20),
    )

    # -- raw/ sources: not yet promoted (promote-scan coverage) ------------
    for i in range(4):
        _source(
            f"raw/synthetic-source-{i}.md", nid=f"synthetic-source-{i}",
            captured=_date_back(200 + i * 30),
            body=f"Synthetic captured source {i}, not yet promoted.",
        )


if __name__ == "__main__":
    build()
    n = sum(1 for _ in ROOT.rglob("*.md"))
    print(f"wrote {n} synthetic fixture notes under {ROOT}")
