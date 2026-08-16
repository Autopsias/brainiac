"""Wikilink target resolution — the ONE lookup order, and its last-resort key.

`raw/` sources carry no `title:` (AGENTS.md §2 gives the zone none), so a link
written with the document's own human name had no resolver key at all and
counted as dangling. The transcript lane wrote hundreds that way: 501 dangling
targets on the reference vault, of which 314 resolve to a real note id once the
separators are regularised.

This is separator normalization, NOT a fuzzy match. Every one of the 20 sampled
targets that resolved did so on IDENTITY — the normalized link text IS the note
id — with no prefix-stripping and no scoring step.

Two rules bound it, and the second is narrower than it first looks:

1. Exact id/stem/title keys ALWAYS win — the normalized key is ``setdefault``
   beneath all of them, so it can only ever fill a gap.
2. Among ids that own a normalized key ONLY by normalization, ambiguity
   refuses: if two collapse to the same key, neither claims it. A resolver that
   picked one would invent an edge between documents whose only relationship is
   punctuation, and a wrong edge is worse here than a missing one — bulk edges
   have measured harm on this vault (see ``graph.BULK_LINK_KEY``).

What rule 2 does NOT cover, deliberately: if one colliding spelling is itself a
real id (``foo-bar`` beside ``foo--bar``), that id owns the key by IDENTITY
under rule 1 and a link normalizing onto it resolves there. That is the
transcript case this exists for, not a guess.
"""
from __future__ import annotations

import re

_NORMALIZE_TARGET = re.compile(r"[^a-z0-9]+")


def normalize_link_target(target: str) -> str:
    """Collapse a target to the shape a kebab-slug id already has."""
    return _NORMALIZE_TARGET.sub("-", (target or "").lower().strip()).strip("-")


def _add_normalized_keys(resolver: dict[str, str], ids: list[str]) -> None:
    """Add the last-resort key for every id that owns one unambiguously."""
    owners: dict[str, set[str]] = {}
    for nid in ids:
        key = normalize_link_target(nid)
        if key:
            owners.setdefault(key, set()).add(nid)
    for key, claimants in owners.items():
        if len(claimants) == 1:
            resolver.setdefault(key, next(iter(claimants)))


def _build_resolver(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    """Map every alias (id, path stem, lowercased title) -> canonical note id,
    plus the normalized last-resort key this module documents above."""
    resolver: dict[str, str] = {}
    for nid, title, path in rows:
        resolver[nid] = nid
        resolver[nid.lower()] = nid
        stem = path.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[:-3]
        resolver.setdefault(stem, nid)
        resolver.setdefault(stem.lower(), nid)
        if title:
            resolver.setdefault(title.lower(), nid)
    _add_normalized_keys(resolver, [r[0] for r in rows])
    return resolver


def resolve_target(resolver: dict[str, str], target: str) -> str | None:
    """id/stem/title first, normalized key last. ONE lookup order, so a caller
    can never accidentally ship a stricter or looser one than its siblings."""
    return (resolver.get(target)
            or resolver.get((target or "").lower())
            or resolver.get(normalize_link_target(target)))
