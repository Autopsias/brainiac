"""Seeded sample notes for a freshly initialized vault (ONB-02)."""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any

from . import config

# --------------------------------------------------------------------------
# ONB-02: seed a brand-new (empty) vault with generic sample notes
# --------------------------------------------------------------------------
# Fully generic content -- zero proper nouns (the release contamination scan
# is a hard gate). Plain filesystem writes, same posture as scaffold_overlay
# above: installer scaffolding, not captured content, so it never needs to go
# through the audited write_note path (a hand-authored note added directly to
# vault/brain/ is always valid -- Markdown+YAML is the substrate's single
# source of truth, the index is a derived cache). Host-only: the VM leg never
# writes directly into vault/brain/ even out-of-band (AGENTS.md §6 write
# split) -- run_full_init below skips this on client == "cowork".
_GENERATED_BRAIN_FILENAMES = {"backlinks.md", "catalog.md"}


def _existing_brain_note_count(vault: str | os.PathLike[str] | None) -> int:
    """Notes under ``vault/brain/`` excluding the top-level index.md and any
    generated file (backlinks.md, catalog.md) -- the "is this vault actually
    empty" check ``seed_sample_notes`` gates on."""
    brain_dir = config.vault_root(vault, allow_missing=True) / "brain"
    if not brain_dir.is_dir():
        return 0
    count = 0
    for p in brain_dir.rglob("*.md"):
        if p.name in _GENERATED_BRAIN_FILENAMES:
            continue
        if p.name == "index.md" and p.parent == brain_dir:
            continue
        count += 1
    return count


def _sample_notes(today: str) -> dict[str, str]:
    """``id -> full Markdown content`` for the 3 seeded sample notes: a
    welcome note (shape), a ``concept`` note (type + Counter-Arguments
    section), and their wikilinked partner (the "linked pair")."""
    return {
        "welcome-to-your-second-brain": f"""---
id: welcome-to-your-second-brain
title: "Welcome to your second brain"
type: note
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Welcome to your second brain

This is a sample note showing the note shape every file under `vault/brain/`
follows: YAML frontmatter (an `id`, a `title`, a `type`, a `classification`,
and `created`/`updated` dates) up top, then a Markdown body below the second
`---`.

Notes stay flat inside their PARA folder (`projects/`, `areas/`,
`resources/`, `archive/`) -- no nesting, no numbering. Structure comes from
**wikilinks**, not folders or tags: see [[example-linked-note]] for a small
worked pair, and [[example-concept]] for the `concept` note type.

Delete these three sample notes whenever you like -- they exist only to show
the shape before you write your own.
""",
        "example-concept": f"""---
id: example-concept
title: "Example concept note"
type: concept
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Example concept note

## Definition

A `concept` note captures one idea worth naming and reusing across other
notes -- a definition, a mental model, a recurring pattern.

## Context & Application

Link to a concept from wherever the idea applies, instead of re-explaining it
each time. See [[welcome-to-your-second-brain]] for the note-shape overview
this sample set demonstrates.

## Counter-Arguments

Reasons this concept might be wrong, incomplete, or context-dependent -- a
concept note without this section is warn-flagged by the validator as a
quality nudge. This sample note's own counter-argument: it isn't a real
concept, just a placeholder.

## Related Concepts

[[example-linked-note]]

## Sources
""",
        "example-linked-note": f"""---
id: example-linked-note
title: "Example linked note"
type: note
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Example linked note

This note and [[welcome-to-your-second-brain]] link to each other -- a small
worked example of the wikilink-first structure this vault uses instead of
folders or tags. It also links to [[example-concept]] to show a `note`
pointing at a `concept`.
""",
    }


def _sample_index(today: str) -> str:
    """A minimal top-level ``brain/index.md`` -- ``tools/validate.py`` hard-
    requires this file to exist (``vault/brain/index.md missing`` is an
    error, not a warning), and nothing else in the install path creates it
    for a genuinely brand-new vault, so seeding is the one place that can
    satisfy that gate. Create-if-absent only (see ``seed_sample_notes``) --
    never overwrites an owner's own index.md."""
    return f"""---
id: index
title: "Index"
type: index
classification: Internal
created: {today}
updated: {today}
tags: []
---

# Index

Map of this vault. Start here, then follow the wikilinks.

## Sample notes

- [[welcome-to-your-second-brain]] -- the note shape (frontmatter, PARA folders, wikilinks)
- [[example-concept]] -- the `concept` note type
- [[example-linked-note]] -- a small wikilinked pair
"""


def seed_sample_notes(vault: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Write the 3 sample notes into ``vault/brain/resources/`` -- ONLY when
    the vault carries no real notes yet (idempotent: a second run against a
    now-populated vault is always a no-op, never a clobber). Also writes a
    minimal top-level ``brain/index.md`` create-if-absent (never overwrites
    an existing one) so the freshly seeded vault passes
    ``tools/validate.py``'s hard ``index.md missing`` gate."""
    v = config.vault_root(vault, allow_missing=True)
    existing = _existing_brain_note_count(v)
    if existing > 0:
        return {"performed": False,
                "reason": f"vault/brain/ already has {existing} note(s)",
                "created": []}
    today = _dt.date.today().isoformat()
    brain_dir = v / "brain"
    dest_dir = brain_dir / "resources"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # tools/validate.py hard-requires vault/raw/ to exist too (`vault/raw/
    # missing` is an error) -- an empty dir is a no-op to create and nothing
    # else in the install path creates it for a genuinely brand-new vault.
    (v / "raw").mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    index_path = brain_dir / "index.md"
    if not index_path.exists():
        index_path.write_text(_sample_index(today), encoding="utf-8")
        created.append("index.md")

    for note_id, content in _sample_notes(today).items():
        path = dest_dir / f"{note_id}.md"
        if path.exists():  # defensive: the emptiness gate above already
            continue        # implies these shouldn't exist yet
        path.write_text(content, encoding="utf-8")
        created.append(f"resources/{note_id}.md")
    return {"performed": True, "created": created}
