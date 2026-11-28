"""The decoder ring: overlay keyword files parsed into term -> tier.

Split out of ``overlay.py`` at the 2026-09-04 size ratchet. Everything here
answers one question — which terms does this vault protect, and at what
classification — and it is the input to both ingest and the SEC-07 outbound
guard. ``overlay.py`` re-exports every public name, so ``brain.overlay.<name>``
stays the call site every existing caller and test uses.
"""
from __future__ import annotations

import json
import os
import re
import stat
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


#: What one ring DIRECTORY may make the guard read (F41). ``overlay/`` is on the
#: shared mount, so the untrusted VM can plant anything in it, and the outbound
#: guard reads the whole ring on every tool call inside a 5-second hook budget
#: that FAILS OPEN. Plain constants, not settings. Over any of them the
#: directory is reported in ``errors`` (so the guard refuses ``ring-unreadable``)
#: and the rest of it is not read. Chosen from measurement (2026-11-22, load
#: 24-31): the real rings are ~6 KB hand-written and under 1 KB generated, and
#: the generated one is itself cut at ``overlay_generated.MAX_RING_ROWS`` = 5000
#: rows, so each cap sits above a saturated legitimate ring (a test pins that).
#: The byte cap bounds the read and the parse (1 MiB of junk lines parses in
#: ~0.23 s); the TERM cap bounds the match, which costs one regex per term and is
#: not bounded by bytes (a valid row can be 25 bytes: 1 MiB of them is 40,000 terms).
MAX_RING_DIR_BYTES = 1 << 20    # all files of a directory; the cap for any ONE file too
MAX_RING_FILES = 64
MAX_RING_TERMS = 6_000          # the generator's own cap is MAX_RING_ROWS = 5,000


class _RingTooBig(OSError):
    """A ring file went past what is left of its directory's byte budget."""


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
    found: list[Path] = []
    with os.scandir(kdir) as entries:
        for e in entries:
            if e.name.endswith(".md"):
                found.append(Path(e.path))
                if len(found) > MAX_RING_FILES:     # a flood: stop listing it
                    break
    return sorted(found, key=lambda f: f.name)


def _read_ring_file(f: Path, limit: int) -> bytes:
    """One ring file's bytes: a REGULAR file, at most ``limit`` of them.

    A VM-planted FIFO would block ``open`` for ever, a symlink can point at one
    (or at ``/dev/zero``), and a multi-gigabyte file would be read whole — each
    one holds the PreToolUse guard past its fail-open budget, so the call
    proceeds unchecked. So: open ``O_NONBLOCK | O_NOFOLLOW`` (a FIFO opens at
    once, a symlink is refused), judge the OPEN descriptor so there is no
    check-then-open race, and read ``limit + 1`` bytes so a file that grows
    after the check is caught as well. Every refusal is an ``OSError`` for
    :func:`_tiers_from_dir` to record. Windows has no ``O_NOFOLLOW``; there the
    ``S_ISREG`` test on the opened target is what remains.
    """
    flags = os.O_RDONLY
    for extra in ("O_NONBLOCK", "O_NOFOLLOW", "O_BINARY"):    # absent on some platforms
        flags |= getattr(os, extra, 0)
    with os.fdopen(os.open(f, flags), "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError("not a regular file (FIFO, directory or device)")
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise _RingTooBig(
            f"over the ring size cap ({limit} of {MAX_RING_DIR_BYTES} bytes left)")
    return raw


def _ring_bodies(kdir: Path, files: list[Path], errors: list[str]):
    """Yield ``(file, body)`` for each ring file that read, within the caps.

    Every refusal lands in ``errors``; a file past the directory's byte budget
    ends the directory (the rest is not read).
    """
    if len(files) > MAX_RING_FILES:
        errors.append(
            f"{kdir}: more than {MAX_RING_FILES} ring files; the rest were not read")
    left = MAX_RING_DIR_BYTES
    for f in files[:MAX_RING_FILES]:
        try:
            raw = _read_ring_file(f, left)
            left -= len(raw)
            # decode as ``read_text`` did, universal newlines included
            _meta, body = frontmatter.parse_text(
                raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))
        except Exception as exc:
            errors.append(f"{f}: {type(exc).__name__}: {exc}")
            if isinstance(exc, _RingTooBig):
                return
            continue
        yield f, body


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
    if errors is None:
        errors = []
    try:
        files = _ring_files(kdir)
    except OSError as exc:
        errors.append(f"{kdir}: {type(exc).__name__}: {exc}")
        return out
    for f, body in _ring_bodies(kdir, files, errors):
        for line in _strip_noise(body):
            cells = _table_cells(line)
            if len(cells) < 3:
                continue
            term, tier = cells[0], cells[2]
            if tier not in TIERS or not term or _PLACEHOLDER_RE.match(term):
                continue
            out[term.casefold()] = tier
            if len(out) > MAX_RING_TERMS:
                errors.append(
                    f"{f}: more than {MAX_RING_TERMS} ring terms; the rest were not read")
                return out
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

    ``errors`` collects unreadable ring files — see :func:`_tiers_from_dir` —
    and the one entry :func:`_pin_egress_ring` adds when the ring was weakened.
    """
    base = overlay_dir(vault, explicit)
    unread: list[str] = []
    merged = _tiers_from_dir(base / KEYWORDS_GENERATED_DIR, unread)
    merged.update(_tiers_from_dir(base / "keywords", unread))
    if errors is not None:
        errors.extend(unread)
    # A ring that could not be read already refuses, naming the file; the pin
    # judges only a ring that was read. shortcut: an explicit --overlay-dir is
    # not pinned.
    if explicit is None and not unread:
        _pin_egress_ring(vault, merged, errors)
    return merged


def _pin_egress_ring(
    vault: str | os.PathLike[str] | None,
    merged: dict[str, str],
    errors: list[str] | None,
) -> None:
    """Keep the outbound guard's ring from WEAKENING behind the owner's back (F17).

    ``overlay/keywords*/`` sits in the vault, which a Cowork VM can write, so
    deleting the directory or re-tiering a row to Public used to let a
    classified term leave with no sign anything had changed. The first call
    pins the ring host-private (``config.egress_ring_pin_path``, trust on first
    use; shortcut: a ring poisoned BEFORE the first pin is pinned as is).
    Later calls put back every pinned term the live ring dropped or lowered and
    add ONE ``errors`` entry, which makes ``egress_terms.check`` refuse on
    every tool: a ring that shrank is degraded, not absent. Additions and
    raises re-pin; the pin is never rewritten while a weakening stands. To
    accept a deliberate removal or lowering the owner deletes the pin file the
    entry names; the next call re-pins the ring as it stands.

    Tighten-only and never raises: a pin store that cannot be proven off the
    mount or written, or a pin file that cannot be parsed, leaves the ring
    exactly as read (shortcut: no pin, so no protection, until the store or
    the file is fixed).
    """
    from . import config
    from .atomic_temp import write_json_atomic

    try:
        pin = config.egress_ring_pin_path(vault)
    except (config.HostPathUnsafe, config.VaultNotFoundError, OSError):
        return
    try:
        raw = json.loads(pin.read_text(encoding="utf-8"))
        pinned = {t: r for t, r in raw["terms"].items() if r in TIERS}
    except FileNotFoundError:
        pinned = {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return
    weak = {t: r for t, r in pinned.items()
            if t not in merged or TIERS.index(merged[t]) < TIERS.index(r)}
    if weak:
        merged.update(weak)
        if errors is not None:
            errors.append(
                f"{pin}: {len(weak)} decoder-ring term(s) were removed or "
                "lowered since the ring was pinned, and keep their pinned tier. "
                f"If you changed the ring on purpose, delete {pin}; the next "
                "call pins the ring as it stands. If not, restore the overlay keyword files.")
        return
    if merged and merged != pinned:
        try:
            pin.parent.mkdir(parents=True, exist_ok=True)
            config.secure_file_permissions(pin.parent, 0o700)
            write_json_atomic(pin, {"schema": 1, "terms": merged})
            config.secure_file_permissions(pin, 0o600)
        except OSError:  # shortcut: an unwritable pin store is no pin, see above
            pass


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


def top_tier_term(
    text: str,
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> tuple[str | None, list[str]]:
    """A ring term mapped to the TOP tier that ``text`` contains, plus every
    ring file that could not be read.

    The admission rule for sources that do NOT come from email (owner ruling
    2026-09-11). The email lane starts at MNPI and uses the whole ring to
    LOWER; a drop-zone or transcript source starts low, so only a top-tier row
    may move it, and only up. Applying every row was measured first on the
    reference vault: 648 of 1,019 Internal sources would have entered at
    Restricted and left the Cowork VM's Internal cap, almost all on one common
    Restricted term.

    ``errors`` is returned, never swallowed: "the ring could not be read" and
    "no term matched" are opposite answers, and each caller decides what the
    first one costs.
    """
    errors: list[str] = []
    tiers = resolve_keyword_tiers(vault, explicit, errors)
    top = {term: tier for term, tier in tiers.items() if tier == TIERS[-1]}
    if not top:
        return None, errors
    return match_keyword_tier(text, tiers=top)[1], errors
