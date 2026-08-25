"""DLV-05 — the three shelf counters (corpus invariants 9, 10 and 11).

A corpus-invariant fix ships with its number in the same change (AGENTS.md
§4). The deliverables shelf is a mechanism that must keep working for years
with nobody watching it, so it gets three counts in ``INVARIANT_METRICS``,
trended in ``health-history.jsonl`` and rendered in ``brain health-report``:

1. ``unshelved_deliverables`` — marked, non-retired deliverables the shelf
   ledger does not claim a copy of.
2. ``stale_shelf_entries`` — ledger rows whose note has left the census
   (retired, unmarked, or deleted) and whose shelf copy is therefore no
   longer backed by anything in the vault.
3. ``unanchored_deliverable_payloads`` — payloads the DROP LANE ingested
   that never got their anchor note.

**One enumeration, never a second definition.** 1 and 2 are computed from a
SINGLE ``deliverables_sync.census()`` call and the SAME host-private ledger
``deliverables_sync.sync()`` writes through — the census is the one public
enumeration (its own docstring says why ``bases-query`` cannot answer this
question at all), and the ledger is the one record that decides what the
shelf owns. Both metrics read that one pair; neither re-derives it.

**Why the third one exists, and why it keys on the DROP LANE.** Metrics 1
and 2 measure the MECHANISM: they compare the marked set against the shelf.
If marking simply stops — the producing surface breaks, nobody stamps
anything — then zero deliverables exist, both metrics report 0 of 0, the
ratchet is satisfied and health reads green. The precise failure the counts
were added to prevent is the one state they are structurally unable to see.
Metric 3 covers it from outside the marked set: the drop lane
(``inbox/_deliverables/``) has exactly ONE correct outcome per file — an
archived original, a signed raw note, and a brain-zone anchor carrying
``deliverable: true`` — so a lane payload without its anchor is
unambiguously a failure, and one nothing else counts.

It keys on the lane's own ``provenance.produced_by: inbox-deliverables``
stamp, over ``raw/``. An earlier design counted every note carrying ANY
``provenance.produced_by`` value but no ``deliverable: true``: ``promote``,
``save-conversation`` and ``kb-curator`` stamp that field on ordinary
internal knowledge notes as a matter of routine, so normal use would have
ratcheted the metric upward forever and taught the owner to ignore shelf
alerts. Its stated limit is the flip side of that precision, and belongs in
the runbook: it sees only what a PRODUCING SURFACE created. A hand-authored
note nobody ever marks is outside all three metrics, and the answer to that
is the producer, not a wider heuristic here.

**Floor semantics, which matter more here than anywhere else in WAT-01.**
Floors ratchet to best-ever. Recording the first ``unshelved_deliverables``
value on a vault BEFORE its backfill therefore pins a high floor that the
backfill immediately ratchets away — the metric would detect nothing during
the exact window it exists for. The first live value is recorded AFTER the
approved apply, and s07 verifies that. Each metric also reports a
``population`` so ``invariant_floors``' shrink guard applies: a run that
read zero deliverables because the vault could not be read never earns a
floor no healthy vault can match again.

**Measurement point.** ``folds/daily.py`` runs ``corpus_invariants_fold``
AFTER ``deliverables_shelf_fold`` for these three: measured before the sync,
``unshelved_deliverables`` would count every deliverable marked since the
last nightly and fire on every ordinary addition.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .invariant_shared import SAMPLE_CAP

#: Report order, appended to ``invariants.INVARIANT_METRICS``.
DELIVERABLE_METRICS = (
    "unshelved_deliverables",
    "stale_shelf_entries",
    "unanchored_deliverable_payloads",
)


def computers(vault: Path, *, cap: int = SAMPLE_CAP) -> dict[str, Callable[[], Any]]:
    """The three metric thunks, sharing ONE census + ledger read.

    Shaped like ``invariants.corpus_invariants``' own memoized
    ``_duplicates``: the shared read happens at most once per fold run, and
    only if a metric actually asks for it."""
    memo: dict[str, dict[str, Any]] = {}

    def state() -> dict[str, Any]:
        if "shelf" not in memo:
            memo["shelf"] = shelf_state(vault)
        return memo["shelf"]

    return {
        "unshelved_deliverables": lambda: unshelved_deliverables(state(), cap=cap),
        "stale_shelf_entries": lambda: stale_shelf_entries(state(), cap=cap),
        "unanchored_deliverable_payloads":
            lambda: unanchored_deliverable_payloads(vault, cap=cap),
    }


def shelf_state(vault: Path) -> dict[str, Any]:
    """``{"entries": [...], "ledger": {...}}`` — the census and the ledger,
    read ONCE for both metrics that need them.

    A shelf that cannot be RESOLVED (any of the five DLV-03 refusals) yields
    ``{"error": ...}``: both metrics then report ``value: None`` rather than
    zero. A refusal already has its own banner-class ``notify_key``, and a
    fabricated 0 here would ratchet a floor on a vault whose shelf never ran.
    """
    from . import config, deliverables_ledger as L, deliverables_shelf as shelf_mod
    from . import deliverables_sync as sync

    root = config.vault_root(vault)
    resolved = shelf_mod.resolve(root)
    if not resolved.ok:
        # The refusal's OWN notify_key, never a key invented here: a second
        # spelling of a refusal would be a finding no registry row declares.
        assert resolved.refusal is not None
        return {"error": f"shelf unresolved ({resolved.refusal.notify_key})"}
    assert resolved.path is not None
    taken = sync.census(root)
    return {"entries": taken["entries"], "ledger": L.load_ledger(root, resolved.path)}


def unshelved_deliverables(state: dict[str, Any], *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """Marked, non-retired deliverables with no ledger row claiming a copy.

    Includes the ones whose payload could not be RESOLVED (a ``source:``
    anchor naming a missing archived original): those are genuinely not on
    the shelf, and reporting them as shelved because the fold had a reason
    would be the comfortable lie. Held moves (the blast-radius cap) show up
    here too, for one run, which is what a held move IS."""
    if state.get("error"):
        return {"value": None, "error": state["error"]}
    entries = state["entries"]
    shelved = {str(row.get("note_id") or "") for row in state["ledger"].values()}
    missing = [e["note_id"] for e in entries if str(e["note_id"]) not in shelved]
    return {
        "value": len(missing),
        # The floor guard's basis: a run that read fewer deliverables than the
        # floor was earned against never lowers it (invariant_floors).
        "population": len(entries),
        "deliverables": len(entries),
        "shelved": len(shelved),
        "sample": sorted(missing)[:cap],
    }


def stale_shelf_entries(state: dict[str, Any], *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """Ledger rows whose note is no longer a live deliverable.

    The shelf never deletes, so a copy outliving its note is not data loss —
    it is a shelf that stopped telling the truth about what the vault holds,
    which is the same defect class as an unshelved deliverable seen from the
    other side."""
    if state.get("error"):
        return {"value": None, "error": state["error"]}
    live = {str(e["note_id"]) for e in state["entries"]}
    ledger = state["ledger"]
    stale = [rel for rel, row in ledger.items()
             if str(row.get("note_id") or "") not in live]
    return {
        "value": len(stale),
        "population": len(ledger),
        "ledger_entries": len(ledger),
        "sample": sorted(stale)[:cap],
    }


def unanchored_deliverable_payloads(
    vault: Path, *, cap: int = SAMPLE_CAP,
) -> dict[str, Any]:
    """Drop-lane payloads whose anchor note is missing.

    One glob over ``raw/*.md`` and one ``is_file()`` per lane payload — the
    same shape and cost class as ``ingest_guard``'s own raw-zone walk, never
    a second walk of the whole vault. The anchor test is the drop lane's OWN
    predicate (``deliverables.anchor_rel`` + ``is_file``), the one its
    recovery pass uses to decide it is finished, so this metric and that
    recovery can never disagree about what "anchored" means.
    """
    from . import frontmatter as fm
    from .ingest import deliverables as DLV

    root = Path(vault)
    payloads = 0
    unanchored: list[str] = []
    for path in sorted((root / "raw").glob("*.md")):
        if not DLV._lane_wrote(fm, path):
            continue
        payloads += 1
        if not (root / DLV.anchor_rel(path.stem)).is_file():
            unanchored.append(f"raw/{path.name}")
    return {
        "value": len(unanchored),
        "population": payloads,
        "payloads": payloads,
        "sample": unanchored[:cap],
    }
