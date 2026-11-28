"""F21 — the host-private pin under ENF-04's overlay-keyword leg.

The leg raises a source to the top tier when its body carries a top-tier term
of the owner's decoder ring (``<vault>/overlay/keywords/*.md``, owner ruling
2026-09-11). That ring sits on the vault tree a Cowork VM can write, and
``resolve_keyword_tiers`` reads a deleted directory, an emptied table and a row
retiered to a lower tier as an honest "no ring". A mount writer could therefore
switch the leg off one term at a time while the note was stamped
``classification_guard: clear``.

Authority comes from a host-private pin instead: the top-tier terms the live
ring has ever held, per vault, under ``host_private_base()`` and PROVEN off
every VM-visible root. It is a ratchet — it only ever gains terms, whatever a
partial or truncated ring read returns — and a body is matched against the live
ring UNIONED with the pin, so deleting, emptying or retiering the ring cannot
lower protection.

* A body that carries a top-tier term, live or pinned, is raised to the top
  tier REGARDLESS of any ring or pin error; the errors go into the reason. A
  pinned term the live ring has lost says so, and what the owner does about it.
* A body that carries none is admitted exactly as before: the ring shrinking
  raises no alert and moves no tier, a ring file that could not be read is
  ``unavailable`` (the transcript route fails closed) as it always was, and a
  pin that could not be read or written changes NO admission for such a body.
  The guard then has the live ring alone, which is the behaviour before F21.

To accept an INTENTIONAL retier or removal the owner deletes the pin file the
reason names; the next ingest learns the live ring afresh. F17's egress-ring
pin is a SEPARATE store (egress-ring-pin, keyed by vault), so to retire a term
on purpose the owner deletes BOTH pin files. Residuals: a ring already damaged
before its first pin is pinned damaged; a mount writer who ADDS a junk top-tier
row gets it pinned (over-protection, never a leak; it shows as a raise naming
that term, and clears when the pin is deleted); a term added and removed
between two ingests is never pinned.

The pin is BOUNDED in terms (``PIN_MAX_TERMS``) and in total characters
(``PIN_MAX_CHARS``): a store that would pass either cap is skipped whole, no
pinned term is ever dropped, and the reason says the pin is full. Trade-off:
a flooded pin stops learning new terms until the owner deletes it, a smaller
harm than a pin a mount writer can grow without bound (cost per document,
persisting after the writer stops). The live ring still matches.
The read-modify-write is not atomic across concurrent writers: the file stays
valid, but a term another writer pinned in the same instant can be lost (the
live ring still matches it).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from .. import config, config_hostpaths as hp
from ..atomic_temp import write_text_atomic
from ..classification import TIERS
from ..overlay_keywords import match_keyword_tier, resolve_keyword_tiers
from .tierguard_verdict import RAISED, UNAVAILABLE, LegCounts, Verdict

_TOP = TIERS[-1]
PIN_MAX_TERMS = 6000     # constants, not knobs; the real ring is a few hundred short terms
PIN_MAX_CHARS = 200_000  # total characters: a count cap alone lets a few thousand LONG terms in


class RingCheck(NamedTuple):
    """``top_tier_term``'s answer, read through the pin: the term and the errors
    are independent, so a caller never loses one to the other."""

    term: str | None          # a top-tier term (live or pinned) the text carries
    errors: list[str]         # ring files that could not be read
    pin_errors: list[str]     # the pin could not be read, proved off the mount or written
    lapsed: bool = False      # `term` is held by the pin alone


def pin_path(vault: Any) -> Path:
    """The pin file, OFF THE MOUNT; falls back to the app-data base exactly as
    ``ingest.deliverables.journal_path`` does. Keyed by vault identity."""
    try:
        d = hp.proven_off_mount(hp.host_private_base() / "ring-pin", vault,
                                what="overlay ring pin store")
    except hp.HostPathUnsafe:
        d = hp.proven_off_mount(config._app_data_base() / "ring-pin", vault,
                                what="overlay ring pin store (app-data fallback)")
    return d / f"overlay-top-tier-ring-{config.vault_slug8(vault)}.json"


def _load(pin: Path) -> set[str]:
    try:
        data = json.loads(pin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()                  # trust on first use
    terms = data.get("terms") if isinstance(data, dict) else None
    if not isinstance(terms, list) or not all(isinstance(t, str) and t for t in terms):
        raise ValueError("not an overlay ring pin")
    return set(terms)


def _store(pin: Path, terms: set[str]) -> None:
    pin.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(pin.parent, 0o700)
    write_text_atomic(pin, json.dumps({"version": 1, "terms": sorted(terms)}))
    config.secure_file_permissions(pin, 0o600)


def _match(text: str, terms: set[str]) -> str | None:
    return match_keyword_tier(text, tiers={t: _TOP for t in terms})[1]


def pinned_top_tier_term(
    text: str, vault: Any, explicit: Any = None,
) -> RingCheck:
    """``overlay_keywords.top_tier_term`` through the pin (see module doc)."""
    errors: list[str] = []
    pin_errors: list[str] = []
    live = {t for t, tier in resolve_keyword_tiers(vault, explicit, errors).items()
            if tier == _TOP}
    pinned: set[str] = set()
    try:
        pin = pin_path(vault)
        pinned = _load(pin)
        if live - pinned:             # the pin only ever gains terms
            merged = pinned | live
            if len(merged) > PIN_MAX_TERMS or sum(map(len, merged)) > PIN_MAX_CHARS:
                # skip the store whole, drop nothing
                pin_errors.append("host overlay ring pin is full, new terms are not pinned")
            else:
                _store(pin, merged)
    except (OSError, ValueError, config.VaultNotFoundError, hp.HostPathUnsafe) as exc:
        # The class name only: an OSError message carries the pin's absolute
        # path, and the reason built from this is VM-readable frontmatter.
        pin_errors.append(f"host overlay ring pin unusable ({type(exc).__name__})")
    live_term = _match(text, live)
    lapsed_term = None if live_term else _match(text, pinned - live)
    return RingCheck(live_term or lapsed_term, errors, pin_errors, lapsed_term is not None)


def ring_why(ring: RingCheck, plain: str) -> str:
    """The reason clause of a raise. ``plain`` is the caller's own wording of a
    live-term raise; no absolute path goes in (VM-readable frontmatter)."""
    why = plain if not ring.lapsed else (
        f"pinned overlay keyword {ring.term!r} is no longer a top-tier row of the "
        "live ring (ring deleted, emptied or retiered) and still raises; if you "
        "changed the ring on purpose, delete the pin file "
        "overlay-top-tier-ring-<vault slug8>.json in the ring-pin folder under the "
        "host app-data/index directory, and the next ingest pins the ring as it "
        "stands; if the change is not yours, restore the ring from backup")
    if ring.errors or ring.pin_errors:
        why += f" (also not fully readable: {', '.join(ring.errors + ring.pin_errors)})"
    return why


def keyword_leg(
    vault: Any, counts: LegCounts, body: str, base: str,
) -> Verdict | None:
    """The ``overlay_keyword`` leg of ``CrossTierGuard``. A matched term raises
    whatever the errors; ``unavailable`` only when nothing matched and a ring
    file could not be read, exactly as before the pin existed."""
    from .tierguard import KEYWORD_LEG

    ring = pinned_top_tier_term(body, vault)
    if ring.term is not None:
        counts.raised += 1
        counts.raised_by_leg[KEYWORD_LEG] = counts.raised_by_leg.get(KEYWORD_LEG, 0) + 1
        return Verdict(
            tier=_TOP, status=RAISED, leg=KEYWORD_LEG,
            reason=f"raised {base} -> {_TOP}: " + ring_why(
                ring, f"overlay keyword {ring.term!r} maps to {_TOP}"))
    if ring.errors:
        counts.unavailable += 1
        return Verdict(
            tier=base, status=UNAVAILABLE,
            reason=("no keyword check was made: the overlay keyword ring "
                    f"could not be read ({', '.join(ring.errors)}) — admitted "
                    "at the declared tier"))
    return None
