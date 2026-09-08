"""The decoder ring: overlay keyword files parsed into term -> tier.

Split out of ``overlay.py`` at the 2026-09-04 size ratchet. Everything here
answers one question — which terms does this vault protect, and at what
classification — and it is the input to both ingest and the SEC-07 outbound
guard. ``overlay.py`` re-exports every public name, so ``brain.overlay.<name>``
stays the call site every existing caller and test uses.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import frontmatter
from .overlay_core import (
    KEYWORDS_GENERATED_DIR,
    TIERS,
    _PLACEHOLDER_RE,
    _strip_noise,
    _TABLE_ROW_RE,
    overlay_dir,
)

#: Split a table row on its UNESCAPED separators only. A term may legitimately
#: contain a pipe — a project called ``Project | Apollo`` is an ordinary name —
#: and until s06 round 4 both halves of this round trip ignored that: the
#: generator interpolated the raw term into a ``|``-delimited row, this reader
#: split on every ``|``, the fixed third column landed on the expansion text
#: instead of the tier, and the row was dropped SILENTLY. A protected project
#: vanished from the egress ring because of its own name, with no error, no
#: entry in ``errors`` and nothing in ``brain doctor``.
#:
#: ``brain.overlay_generated._cell`` writes the escapes this undoes. The two
#: must change together. A HAND-written ring that contains a bare ``|`` inside
#: a cell still splits on it — that is the pre-existing Markdown behaviour an
#: author sees rendered, and changing it would retier hand-written rows.
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def _uncell(cell: str) -> str:
    return cell.replace("\\|", "|").replace("\\\\", "\\")


def _table_cells(line: str) -> list[str]:
    m = _TABLE_ROW_RE.match(line)
    if not m:
        return []
    return [_uncell(c).strip().strip("`").strip()
            for c in _UNESCAPED_PIPE_RE.split(m.group(1))]


def resolve_keyword_tiers(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, str]:
    """``{term (casefolded): tier}`` from the overlay's ``keywords/*.md`` tables.

    Rows are ``| Term | Expansion | Classification |``; the third column is
    OPTIONAL (a glossary without it maps nothing, exactly as before). Template
    placeholders (``<ACRONYM>``) and header/separator rows are skipped. Never
    raises — an unreadable or absent overlay maps nothing.

    ``errors``, when a caller passes a list, collects the path of every ring
    file that could NOT be read or parsed. Ingest classification ignores it and
    keeps the old lenient behaviour; the OUTBOUND guard passes one, because for
    that caller "I could not read the ring" and "the ring is empty" are
    opposite answers — see :func:`brain.egress_terms.check`.
    """
    return _tiers_from_dir(overlay_dir(vault, explicit) / "keywords", errors)


def _ring_files(kdir: Path) -> list[Path]:
    """Every ``*.md`` in one ring directory. RAISES when the ring is degraded.

    Absent and unreadable are OPPOSITE answers, and telling them apart is the
    whole job here (s06 round 3, H-1). Round 2 wrapped ``Path.glob`` in
    ``except OSError`` — but ``glob`` swallows ``PermissionError`` itself and
    returns ``[]``, so that handler NEVER executed and a ring directory at
    ``chmod 000`` still read as an EMPTY ring, which allows on every non-strict
    tool. ``Path.is_dir()`` swallows the same error, so the ``is_dir()`` guard
    ahead of it did the same thing one level up: with the overlay PARENT
    unreadable, ``keywords/`` simply "was not a directory".

    So nothing here asks a question whose failure looks like a negative answer:

    * ``os.lstat`` first — is anything there AT ALL? ``FileNotFoundError`` is
      the one honest absence (no generated ring on this vault, no overlay), and
      only that returns empty. Any OTHER error, ``PermissionError`` on the
      parent included, propagates as degraded.
    * then ``os.scandir``, which raises rather than swallowing:
      ``PermissionError`` on an unreadable directory, ``NotADirectoryError``
      when a FILE sits where the ring should be, ``FileNotFoundError`` when the
      entry ``lstat`` just saw is a DANGLING symlink.
    """
    try:
        os.lstat(kdir)
    except FileNotFoundError:
        return []                     # genuinely absent: not a degraded ring
    with os.scandir(kdir) as entries:
        return sorted((Path(e.path) for e in entries if e.name.endswith(".md")),
                      key=lambda f: f.name)


def _tiers_from_dir(kdir: Path, errors: list[str] | None = None) -> dict[str, str]:
    """``{term (casefolded): tier}`` from every ``*.md`` table under one dir.

    A DEGRADED ring is not an empty one (s06 round 2, H-1; round 3, H-1). This
    function used to swallow every read failure with ``except Exception:
    continue``, so one ``chmod 000`` on a ring file made the guard's decoder
    ring read as empty — and empty only refuses under ``--strict``, which the
    guard passes on the two web tools alone. Measured: ring readable -> the
    call was blocked, ring unreadable -> the SAME call was allowed, and nothing
    said why. The failure is still swallowed here (this function never raises,
    and ingest depends on that) but it is now RECORDED, and the guard refuses
    on it. See :func:`_ring_files` for the DIRECTORY half, which round 2 missed.
    """
    out: dict[str, str] = {}
    try:
        files = _ring_files(kdir)
    except OSError as exc:
        if errors is not None:
            errors.append(f"{kdir}: {type(exc).__name__}: {exc}")
        return out
    for f in files:
        try:
            _meta, body = frontmatter.parse_text(f.read_text(encoding="utf-8"))
        except Exception as exc:
            if errors is not None:
                errors.append(f"{f}: {type(exc).__name__}: {exc}")
            continue
        for line in _strip_noise(body):
            cells = _table_cells(line)
            if len(cells) < 3:
                continue
            term, tier = cells[0], cells[2]
            if tier not in TIERS or not term or _PLACEHOLDER_RE.match(term):
                continue
            out[term.casefold()] = tier
    return out


def resolve_egress_keyword_tiers(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, str]:
    """The ring the OUTBOUND guard judges against: generated, then hand.

    This is deliberately a DIFFERENT set from :func:`resolve_keyword_tiers`.
    That one feeds ingest classification and must keep answering exactly what
    the owner typed; this one may also carry every term derived from the
    vault's own entity notes. A hand-written row always wins on a conflict —
    an owner who typed a tier chose it.

    ``errors`` collects unreadable ring files — see :func:`_tiers_from_dir`.
    """
    base = overlay_dir(vault, explicit)
    merged = _tiers_from_dir(base / KEYWORDS_GENERATED_DIR, errors)
    merged.update(_tiers_from_dir(base / "keywords", errors))
    return merged


def match_keyword_tier(
    text: str,
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
    tiers: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Highest tier any mapped keyword found in ``text`` resolves to, plus the
    term that matched. Word-boundary matching, case-insensitive.

    ``tiers`` lets a caller pass a ring it already resolved — the egress guard
    passes the MERGED ring so it does not resolve the same files twice, and so
    that this function stays the one place the matching rule lives.
    """
    if tiers is None:
        tiers = resolve_keyword_tiers(vault, explicit)
    if not tiers:
        return None, None
    hay = text.casefold()
    best: tuple[str, str] | None = None
    for term, tier in sorted(tiers.items()):
        pattern = re.escape(term)
        if term[:1].isalnum():
            pattern = r"\b" + pattern
        if term[-1:].isalnum():
            pattern = pattern + r"\b"
        if not re.search(pattern, hay):
            continue
        if best is None or TIERS.index(tier) > TIERS.index(best[0]):
            best = (tier, term)
    return best if best else (None, None)
