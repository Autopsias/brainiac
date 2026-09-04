"""Derive the EGRESS decoder ring from the notes this vault already classifies.

**The problem this solves.** ``overlay/keywords/`` is hand-written. On every
vault nobody curates it is EMPTY, so the SEC-07 outbound guard
(:mod:`brain.egress_terms`) can only ever report "no term could have been
caught" — an honest message and a useless one. A guard whose data the owner
must type is a guard that ships inert. Owner ruling 2026-09-01: "users won't
know about it and won't do shit about it".

**Why frequency does not work.** Measured 2026-09-01 on a 4,313-note vault:
ranking capitalised phrases by how much more often they appear in protected
notes than in Internal ones produced 5,298 candidates whose highest-scoring
rows were Portuguese stopwords (``Mas``, ``Como``, ``Porque``) and transcript
boilerplate (``Speaker Summary``, ``Original File``). The corpus splits by
LANGUAGE and NOTE SHAPE, not by sensitivity, so the tier alone is not a
signal. Do not reach for this again without re-measuring.

**What is the signal.** A ``project`` note IS a codename, its own
``classification`` is the tier the owner already assigned it, and its
``aliases`` are the other names for that same programme — precisely what a
decoder ring exists to map. No word lists, no model, no language assumption.

**Why ONLY ``project``, measured 2026-09-01 on the reference vault.** The
first cut harvested ``person``/``company``/``concept`` too, and the output was
unusable in three different ways. ``concept`` (175 terms) is vocabulary, not
entities: ``API``, ``APIs``, ``Adobe``, ``Amazon Web Services``,
``asset management``, ``3-Tier``. ``company`` (25) is mostly public firms whose
mention discloses nothing — ``IBM``, ``McKinsey``, ``Deloitte``, ``BCG``,
``Accenture``. ``person`` (204) carries 41 bare first names — ``David``,
``Carlos``, ``Catarina``. ``project`` (16) was the ONLY type where every single
derived term was a real codename. An over-refusing guard is switched off within
a day, and a guard that is off protects nothing, so precision beats coverage
here. Owner ruling 2026-09-01: ``project`` only. Anything else worth guarding
goes in the hand-written ring, which always wins.

**A third discriminator was tried and REJECTED — do not re-invent it.** "A
protected term never appears in Internal notes" sounds right and is false for a
working vault: the reference vault's most important codename appears in 172 of
its Internal notes, so a leakage filter at any threshold drops that term while
correctly dropping ``API``. Note type is the discriminator that
works; neither frequency nor leakage is.

**Why this is NOT written into ``overlay/keywords/``.** That directory is read
by :func:`brain.overlay.resolve_keyword_tiers`, which feeds INGEST
classification in ``provenance.py`` — where ``tier = mapped or "MNPI"``. A
fuller ring there does not over-classify; it LOWERS the fail-closed default.
Measured on the same vault: 659 of 670 MNPI documents carry a generated term
and would have been stamped ``Restricted`` instead. That is a real change to a
vault's protective posture, and the owner ruled 2026-09-01 that the generated
ring serves EGRESS ONLY — the hand-written ring stays the sole input to
classification. Keep the two directories separate; merging them silently
re-tiers a corpus.

**A generated ring never invents a term.** Every row traces to one note, by id,
that the owner classified at ``Confidential`` or above. A brand-new vault
generates zero rows and the guard says so, which is the correct answer for a
vault that has classified nothing yet.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NamedTuple

from . import frontmatter
from . import overlay as ov

#: Sibling of ``keywords/`` — deliberately NOT inside it. See the module
#: docstring: ``keywords/`` drives ingest classification and this does not.
GENERATED_DIRNAME = ov.KEYWORDS_GENERATED_DIR
GENERATED_FILENAME = "generated.md"

#: The one note type whose title is reliably a CODENAME. Widening this is a
#: measurement, not a preference — see the docstring for what `person`,
#: `company` and `concept` actually produced on a real 4,313-note vault.
ENTITY_TYPES: tuple[str, ...] = ("project",)

#: Below this tier a term is not worth guarding: a vault is `Internal` by
#: default, so harvesting Internal entities would map most of the vocabulary
#: and the guard would be switched off within a day. Same reasoning, and the
#: same tier, as `egress_terms.DEFAULT_MIN_TIER`.
MIN_TIER = "Confidential"

#: Two-character terms match inside too much ordinary text to be useful.
MIN_TERM_CHARS = 3


class Row(NamedTuple):
    """One generated decoder-ring row, and the note it came from."""

    term: str
    tier: str
    source_id: str
    source_type: str


def _note_rows(path: Path, hand_ring: set[str]) -> list[Row]:
    """Every term ONE note contributes: its title, then each alias."""
    try:
        meta, _body = frontmatter.parse_text(path.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover - an unreadable note is not a finding here
        return []
    if not isinstance(meta, dict):
        return []
    ntype = str(meta.get("type") or "").strip()
    tier = str(meta.get("classification") or "").strip()
    if ntype not in ENTITY_TYPES or tier not in ov.TIERS:
        return []
    if ov.TIERS.index(tier) < ov.TIERS.index(MIN_TIER):
        return []
    nid = str(meta.get("id") or path.stem).strip()
    names = [meta.get("title")]
    aliases = meta.get("aliases")
    if isinstance(aliases, list):
        names.extend(aliases)
    out: list[Row] = []
    for name in names:
        if not isinstance(name, str):
            continue
        term = name.strip().strip('"').strip()
        # The hand-written ring WINS. An owner who typed a term chose its tier
        # deliberately; a derived row must never silently retier it.
        if len(term) < MIN_TERM_CHARS or term.casefold() in hand_ring:
            continue
        out.append(Row(term, tier, nid, ntype))
    return out


def derive(vault: str | os.PathLike[str]) -> list[Row]:
    """Every generated row for ``vault``, deduplicated and sorted.

    On a term claimed by two notes at different tiers the HIGHER tier wins:
    the ring answers "may this leave", and the protective answer is the safe
    one to be wrong about.
    """
    root = Path(vault)
    hand_ring = set(ov.resolve_keyword_tiers(root))
    best: dict[str, Row] = {}
    brain_dir = root / "brain"
    if not brain_dir.is_dir():
        return []
    for path in sorted(brain_dir.rglob("*.md")):
        for row in _note_rows(path, hand_ring):
            key = row.term.casefold()
            prior = best.get(key)
            if prior is None or ov.TIERS.index(row.tier) > ov.TIERS.index(prior.tier):
                best[key] = row
    return sorted(best.values(), key=lambda r: (r.term.casefold(), r.tier))


def render(rows: list[Row]) -> str:
    """The generated overlay file, byte-stable for an unchanged corpus.

    Deterministic on purpose: the fold rewrites this hourly, and a file whose
    bytes churn on every run would show up as vault noise in every diff, every
    backup and every drift check.
    """
    head = (
        "---\n"
        f"overlay_type: {GENERATED_DIRNAME}\n"
        'title: "generated decoder ring"\n'
        "generated: true\n"
        "---\n\n"
        "<!-- GENERATED by brain.overlay_generated — DO NOT HAND-EDIT.\n"
        "     Rewritten by the nightly fold from this vault's own notes.\n"
        "     Every row is one `project` note classified Confidential or\n"
        "     above, plus its aliases.\n"
        "     To change a row, change that note. To pin a term the generator\n"
        "     cannot see, add it to overlay/keywords/ instead — the\n"
        "     hand-written ring wins on any conflict.\n"
        "     EGRESS ONLY: this file is NOT read by ingest classification. -->\n\n"
    )
    if not rows:
        return head + (
            "_No entity note in this vault is classified Confidential or above, "
            "so this ring is empty. That is not evidence there is nothing to "
            "protect._\n"
        )
    body = ["| Term | Expansion | Classification |", "|---|---|---|"]
    body.extend(f"| {_cell(r.term)} | {_cell(r.source_type)} note "
                f"`{_cell(r.source_id)}` | {r.tier} |"
                for r in rows)
    return head + "\n".join(body) + "\n"


def _cell(text: str) -> str:
    """One Markdown table cell, with the separator escaped (s06 round 4).

    The writer used to interpolate a term straight into a `|`-delimited row
    while the reader split every row on `|` with no unescaping. A project
    legitimately named ``Project | Apollo`` therefore rendered as FOUR cells;
    the reader's fixed third column became the expansion text, which is not a
    tier name, and the row was dropped SILENTLY — no exception, no entry in the
    caller's ``errors`` list, nothing in ``brain doctor``. A protected project
    disappeared from the egress ring because of its own name.

    `brain.overlay._table_cells` performs the matching unescape. The two must
    change together; ``tests/test_overlay_generated_ring.py`` round-trips them.
    """
    return text.replace("\\", "\\\\").replace("|", "\\|")


def generated_path(vault: str | os.PathLike[str],
                   explicit: str | os.PathLike[str] | None = None) -> Path:
    return ov.overlay_dir(vault, explicit) / GENERATED_DIRNAME / GENERATED_FILENAME


def _publish(path: Path, text: str) -> None:
    """Write the ring so no reader can ever see HALF of it (s06 round 3, H-2).

    ``Path.write_text`` TRUNCATES before it writes. The outbound guard reads
    this file on every tool call, so a read that interleaves after the truncate
    sees a syntactically valid PREFIX — a real frontmatter block and the first
    N table rows — raises nothing, and the terms in the tail are simply absent.
    ``ring_errors`` stays empty, the ring looks smaller than it is, and a term
    the vault DOES map leaves the host. A reader-side marker would not close
    that: the reader would still have to guess. Publishing by rename does,
    because ``os.replace`` is atomic on POSIX and on Windows — a reader holds
    either the whole old file or the whole new one, never a prefix of either.

    The temp file is created in the SAME DIRECTORY on purpose: ``os.replace``
    is only atomic within one filesystem. Its name starts with a dot so the
    ``*.md`` glob in :func:`brain.overlay._ring_files` cannot pick a half-written
    temp file up as a ring file of its own.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def regenerate(vault: str | os.PathLike[str],
               explicit: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Rewrite the generated ring. Returns what changed, never raises on a no-op.

    ``changed`` is False when the rendered bytes match what is already on disk,
    so an unchanged corpus costs one read and no write.
    """
    rows = derive(vault)
    path = generated_path(vault, explicit)
    text = render(rows)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    changed = current != text
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        _publish(path, text)
    tiers: dict[str, int] = {}
    for r in rows:
        tiers[r.tier] = tiers.get(r.tier, 0) + 1
    return {
        "path": str(path),
        "terms": len(rows),
        "sources": len({r.source_id for r in rows}),
        "by_tier": tiers,
        "changed": changed,
    }
