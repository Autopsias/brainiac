#!/usr/bin/env python3
"""Apply the owner's cross-tier-twin ruling (BAK-01) through the audited host
write path.

    .venv/bin/python tools/apply_crosstier_ruling.py                  # dry-run
    .venv/bin/python tools/apply_crosstier_ruling.py --apply
    .venv/bin/python tools/apply_crosstier_ruling.py --apply --only 2026-07-11-x

201 documents exist twice at two tiers: the `brain ingest` drop-zone copy at
`Internal` (the LOW twin) and the corpus-migration copy at Restricted /
Confidential / MNPI (the HIGH twin). A reader capped at Internal therefore
reaches the sensitive substance through the low twin.

The ruling (`_decisions/invariants-s04-crosstier-ruling.md`, 2026-08-10) is
read by this script, not paraphrased by it — the per-bucket dispositions come
out of that file's table, and a ruling that stops saying what it says today
stops this script.

TWO OPERATIONS PER PAIR, in this order and no other:

  1. RAISE — set the LOW twin's `classification` to the HIGH twin's tier.
     Frontmatter-only, through `core.write_note` (Ed25519-signed, hash-chained).
     `raw/` BODIES ARE NEVER TOUCHED: `frontmatter.set_keys` replaces one line
     of the block and returns the body byte-for-byte, the same primitive
     `core.supersede` already uses on `raw/` notes.
  2. LINK — `core.supersede(low, high)`: retire the July re-ingest in favour of
     the canonical migrated document.

Raise BEFORE link, always: supersession does not remove a note from the index
(`--latest-only` is opt-in), so a linked-but-unraised low twin still leaks.

DIRECTION (low retired, high survives) is this script's choice, because the
ruling is silent on it. The high twin is the original: correct provenance
(`origin:` points at the real source), the classification the migration set,
and a clean body — where 191 of 201 low twins carry the source file's
frontmatter pasted INTO their body, a July extraction defect. Retiring the
defective duplicate keeps the clean document as the chain head. It also matches
the seven A-daily pairs already linked this way in the vault. Fourteen other
pairs are already linked the OTHER way (high retired); this script leaves every
existing link alone and reports it — re-pointing a live chain is not something
the ruling ordered.

RESUMABLE AND IDEMPOTENT with no ledger: every decision is re-derived from
on-disk frontmatter each run, so an interrupted run is resumed by re-running.
The live index is NOT trusted for chain state — measured 2026-08-10, its
`superseded_by` column is stale for seven pairs whose files carry the key.
"""
from __future__ import annotations

import csv
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, NamedTuple

REPO = Path(__file__).resolve().parent.parent
DEFAULT_RULING = REPO / "_decisions/invariants-s04-crosstier-ruling.md"
DEFAULT_PAIRLIST = REPO / "_evidence/invariants/pairlist.tsv"

# The bucket the ruling forbids any supersession link. Named here so the
# assertion at the link site reads as a rule, not as a lookup that could
# silently start returning True.
NO_LINK_BUCKET = "C"


class Stop(Exception):
    """An unexpected condition. Nothing further is attempted."""


class Pair(NamedTuple):
    bucket: str
    sim: float
    low: str
    low_tier: str
    high: str
    high_tier: str
    cos_used: bool


# ---------------------------------------------------------------- the ruling


def load_ruling(path: Path) -> dict[str, dict[str, bool]]:
    """Parse the per-bucket disposition table out of the owner's ruling.

    Returns ``{bucket: {"raise": bool, "link": bool}}``. Raises :class:`Stop`
    if the file is absent, undecided, or does not carry the table — s04 must
    never infer a disposition from anywhere else.
    """
    if not path.exists():
        raise Stop(f"ruling not found: {path}")
    text = path.read_text(encoding="utf-8")
    if not re.search(r"^\*\*Status:\*\*\s*DECIDED", text, re.M):
        raise Stop(f"ruling is not marked DECIDED: {path}")

    table: dict[str, dict[str, bool]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 4 or cells[0] in ("bucket", "---"):
            continue
        bucket = cells[0]
        if not re.fullmatch(r"[A-Z](-[a-z]+)?", bucket):
            continue
        disposition = cells[2].lower()
        if "raise" not in disposition:
            raise Stop(f"ruling bucket {bucket!r}: no RAISE in {cells[2]!r}")
        # "RAISE + supersede-link" vs "RAISE only — do NOT supersede-link".
        link = "supersede-link" in disposition and "not supersede-link" not in disposition
        table[bucket] = {"raise": True, "link": link}

    if not table:
        raise Stop(f"ruling carries no per-bucket table: {path}")
    if NO_LINK_BUCKET not in table:
        raise Stop(f"ruling has no bucket {NO_LINK_BUCKET!r} row")
    if table[NO_LINK_BUCKET]["link"]:
        raise Stop(
            f"ruling now permits a supersede-link for bucket {NO_LINK_BUCKET!r}. "
            "It prohibited one on 2026-08-10 because those pairs are divergent "
            "extractions where neither copy supersedes the other. This script "
            "will not link them; re-read the ruling with a human."
        )
    return table


def load_pairs(path: Path) -> list[Pair]:
    if not path.exists():
        raise Stop(f"pairlist not found: {path}")
    out: list[Pair] = []
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    for row in rows[1:]:
        bucket, sim, low, high, _bytes, cos = row
        lid, ltier = low.split(" [")[0], low.split("[")[1].rstrip("]")
        hid, htier = high.split(" [")[0], high.split("[")[1].rstrip("]")
        out.append(Pair(bucket, float(sim), lid, ltier, hid, htier, bool(cos.strip())))
    return out


# ------------------------------------------------------------- vault reading


def vault_guard(vault: Path) -> int:
    """Assert we are pointed at the reference vault, not an empty checkout."""
    db = vault / ".brain/snapshot/index.snapshot.sqlite"
    if not db.exists():
        raise Stop(f"no snapshot at {db} — wrong vault?")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n = conn.execute(
            "SELECT count(*) FROM notes WHERE path LIKE '%/raw/%'").fetchone()[0]
    finally:
        conn.close()
    if n <= 1000:
        raise Stop(f"WRONG VAULT: {n} raw sources at {vault}")
    return n


def note_paths(core: Any, ids: set[str]) -> dict[str, Path]:
    q = ",".join("?" * len(ids))
    rows = core.index.conn.execute(
        f"SELECT id, path FROM notes WHERE id IN ({q})", list(ids)).fetchall()
    return {str(r[0]): Path(str(r[1])) for r in rows}


class NoteState(NamedTuple):
    path: Path
    text: str
    tier: str
    superseded_by: str
    previous_version: str
    retired: bool


def read_state(vault: Path, path: Path) -> NoteState:
    from brain import frontmatter

    p = path if path.is_absolute() else vault / path
    text = p.read_text(encoding="utf-8")
    meta, _ = frontmatter.parse_text(text)
    if not meta:
        raise Stop(f"unparseable frontmatter: {p}")
    ilv = str(meta.get("is_latest_version", "")).strip().lower()
    sup = str(meta.get("superseded_by") or "").strip()
    return NoteState(
        path=p, text=text,
        tier=str(meta.get("classification") or "").strip(),
        superseded_by=sup,
        previous_version=str(meta.get("previous_version") or "").strip(),
        retired=bool(sup) or ilv == "false",
    )


# ------------------------------------------------------------------ planning


class Plan(NamedTuple):
    pair: Pair
    raise_to: str | None      # None => already at the target tier
    link: bool                # supersede(low, high)
    link_note: str            # why not, when link is False




# ----------------------------------------------------------------- execution


def do_raise(core: Any, vault: Path, pair: Pair, paths: dict[str, Path],
             tier: str) -> None:
    from brain import frontmatter

    lo = read_state(vault, paths[pair.low])
    after = frontmatter.set_keys(lo.text, {"classification": tier})
    _, body_before = frontmatter.parse_text(lo.text)
    _, body_after = frontmatter.parse_text(after)
    # raw/ is immutable: prove the body is byte-identical before signing it.
    if body_before != body_after:
        raise Stop(f"{pair.low}: raise would change the body — refusing")
    rel = lo.path.relative_to(vault).as_posix()
    core.write_note(
        rel, after,
        reason=(f"BAK-01 cross-tier ruling 2026-08-10: raise {pair.low} "
                f"{lo.tier} -> {tier} to match twin {pair.high} "
                f"(bucket {pair.bucket}, frontmatter only)"),
    )


def do_link(core: Any, pair: Pair) -> None:
    # The ruling's bucket-C prohibition, as an assertion at the site that would
    # violate it — not as a comment, and not only as a planning-time filter.
    assert pair.bucket != NO_LINK_BUCKET, (
        f"bucket {NO_LINK_BUCKET} pair {pair.low!r} reached the link site: the "
        "ruling prohibits a supersession link on divergent extractions")
    core.supersede(
        pair.low, pair.high,
        reason=(f"BAK-01 cross-tier ruling 2026-08-10: {pair.low} is a July "
                f"re-ingest duplicate of {pair.high} (bucket {pair.bucket}, "
                f"similarity {pair.sim:.4f}) — retiring the duplicate"))


# ---------------------------------------------------------------------- main




sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("tools.apply_crosstier_ruling", sys.modules[__name__])
from tools.crosstier_pairs import main, plan_pair  # noqa: E402,F401

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as exc:
        print(f"\nSTOPPED: {exc}", file=sys.stderr)
        sys.exit(2)
