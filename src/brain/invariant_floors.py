"""WAT-01 floor guards — a defect COUNT improves when the corpus SHRINKS.

The ratchet in ``invariants.update_floors`` records the best (lowest) value
ever seen and alerts on anything above it. That rule is right, and it has one
blind spot: every WAT-01 metric counts DEFECTS IN A CORPUS, so removing
documents improves the score. A run taken while documents are missing can
therefore record a floor no healthy vault can ever match again.

That is not hypothetical. The reference vault recorded
``unlinked_sources = 0`` across 19 consecutive runs on 2026-08-19, while ~120
documents from the wrongful 433-file hand retirement were still out of the
vault. They were reinstated on 2026-08-20 and the metric returned to its true
value — and every run since has reported a REGRESSION against a floor that
only existed because the corpus was damaged. An alarm that can never be
satisfied is an alarm nobody reads, which is the failure this whole fold
exists to prevent.

So a metric may not set a NEW floor on a run whose population fell below the
population its current floor was recorded at. The guard only ever declines to
LOWER a floor; it never raises one, never suppresses a regression, and never
touches the reported value. A metric with no ``population`` in its record
(pair and family counts) is ratcheted exactly as before.

Same shape as ``invariants.rebase_unreachable_floor``, which already re-seeds
``unreachable_gold`` when the golden set's label count changes the
denominator: the basis a floor was earned against is stored beside it.
"""
from __future__ import annotations

from typing import Any

#: Suffix for the population a metric's floor was recorded against. Stored in
#: the same ``floors`` mapping (an int, like ``unreachable_gold_labels``), so
#: it survives round-tripping through ``maintain-state.json``.
POPULATION_SUFFIX = "__population"


def metric_populations(metrics: dict[str, Any] | None) -> dict[str, int]:
    """Per-metric corpus size, for the metrics that report one.

    Only four do (``cross_tier_candidates``, ``cross_tier_duplicates``,
    ``unlinked_sources``, ``unsigned_notes``) — and those are exactly the
    count-over-a-corpus metrics the guard applies to."""
    out: dict[str, int] = {}
    for name, record in (metrics or {}).items():
        if isinstance(record, dict) and isinstance(record.get("population"), int):
            out[name] = record["population"]
    return out


def update_floors_guarded(
    prev_floors: dict[str, Any] | None,
    values: dict[str, int],
    metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """``update_floors``, minus floors earned on a corpus that had shrunk.

    A metric's first observation still seeds its own floor. A value that is
    not an improvement ratchets exactly as before. Only the combination
    "better than the floor" AND "measured over fewer documents than the floor
    was" is declined — and it is declined for that run only, so the next run
    at full population records it normally."""
    previous = dict(prev_floors or {})
    floors = {k: int(v) for k, v in previous.items() if isinstance(v, int)}
    populations = metric_populations(metrics)
    out: dict[str, Any] = dict(previous)
    for name, value in values.items():
        value = int(value)
        population = populations.get(name)
        basis_key = name + POPULATION_SUFFIX
        basis = previous.get(basis_key)
        recorded = floors.get(name)
        shrank = (isinstance(population, int) and isinstance(basis, int)
                  and population < basis)
        if recorded is None:
            out[name] = value
        elif value < recorded and shrank:
            continue          # the corpus lost documents — this floor is not earned
        else:
            out[name] = min(recorded, value)
        if isinstance(population, int) and out.get(name) == value:
            out[basis_key] = population
    return out


def declined_floors(
    prev_floors: dict[str, Any] | None,
    values: dict[str, int],
    metrics: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The metrics :func:`update_floors_guarded` refused to ratchet this run.

    Reported so a declined floor is visible rather than silent — a guard whose
    only evidence is a number that did not change is a guard nobody can audit.
    """
    guarded = update_floors_guarded(prev_floors, values, metrics)
    populations = metric_populations(metrics)
    out: list[dict[str, Any]] = []
    for name, value in values.items():
        recorded = (prev_floors or {}).get(name)
        if not isinstance(recorded, int) or guarded.get(name) != recorded:
            continue
        if int(value) >= recorded:
            continue
        out.append({
            "metric": name, "value": int(value), "floor": recorded,
            "population": populations.get(name),
            "floor_population": (prev_floors or {}).get(name + POPULATION_SUFFIX),
            "reason": "corpus smaller than when the floor was recorded",
        })
    return out
