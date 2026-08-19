"""Grounding-delivery sub-steps of the E-checks (s18 extraction).

The join-scoring and substance-sentence stages of ``cos_echecks.
_grounding_delivery`` moved verbatim out of that module, which hands its own
callables (``short_chunks``, ``_exact_int``) over so their definitions stay
single. This module never imports :mod:`brain.cos_echecks`.
"""
from __future__ import annotations

from typing import Any, Callable


def join_problems(run: dict[str, Any], j: dict[str, Any], *,
                  short_chunks: Callable,
                  exact_int: Callable) -> tuple[list[str], set[str], set[str]]:
    """Score the delivery join's own conditions; return (problems, union, with_content)."""
    problems: list[str] = []
    chunks = j.get("chunks") or []
    # CONDITION 1 — the producer's own verdict. `ok` is written by
    # `cos_batch_chunk` and was READ BY NOTHING, so a join could declare itself
    # not-ok and still pass every condition below (review 2026-08-15). It is a
    # cheap, independent second opinion on the same file and it is now required
    # to be exactly `True`.
    if j.get("ok") is not True:
        problems.append(f"the grounding join's own `ok` is {j.get('ok')!r} — "
                        "the producer does not stand behind this delivery")
    # CONDITION 2 — composition. A map that exists and never reached the prompt.
    # ONE predicate, shared with the producer and the nightly's log line.
    bad = short_chunks(j)
    if bad:
        problems.append(f"{len(bad)} chunk(s) did not carry the grounding they "
                        f"were mapped: {bad[:6]}")
    # CONDITION 3 — the union covers the frozen required set. THE DENOMINATOR IS
    # THE DECLARATION'S, not the join's own: with `--grounding` forgotten the
    # chunker never reads a map, so its `required` is 0 and `0 >= 0` would pass
    # the very failure this condition exists for. The declaration is the
    # independently produced artifact, and it is what the union is scored
    # against.
    #
    # AND THE NUMERATOR IS RECOMPUTED FROM THE CHUNK RECORDS, not read off the
    # producer's `required_covered_by_chunks`. That field is the producer's own
    # word about itself, and the review passed a join declaring two covered ids
    # with `chunks: []` — zero problems returned. `covered_ids` per chunk is what
    # the map actually keyed; the union of those is the only honest numerator,
    # and the declared count must AGREE with it.
    g = run.get("grounding") if isinstance(run.get("grounding"), dict) else {}
    required_ids = {str(x) for x in (g.get("required") or [])}
    required = len(required_ids)
    union: set[str] = set()
    with_content: set[str] = set()
    for c in chunks:
        if not isinstance(c, dict):
            continue
        union |= {str(x) for x in (c.get("covered_ids") or [])}
        with_content |= {str(x) for x in (c.get("with_text_ids") or [])}
    uncovered = sorted(required_ids - union)
    if uncovered:
        problems.append(f"the chunks delivered grounding for "
                        f"{len(required_ids) - len(uncovered)} of the "
                        f"{required} required id(s), recomputed from the chunk "
                        f"records: {uncovered[:6]}")
    covered = exact_int(j.get("required_covered_by_chunks"))
    if covered is None:
        problems.append("the grounding join records no coverage count as an "
                        f"integer ({j.get('required_covered_by_chunks')!r})")
    elif covered < required:
        problems.append(f"the chunks delivered grounding for {covered} of the "
                        f"{required} required id(s)")
    elif covered != len(union):
        problems.append(f"the grounding join DECLARES {covered} covered id(s) "
                        f"and its own chunk records carry {len(union)}")
    # THE DENOMINATOR. `required` must be a subset of the ids the rendered
    # batches carry — D13's guarantee, joined to the batches rather than assumed.
    orphans = j.get("required_not_in_batches") or []
    if orphans:
        problems.append(f"{len(orphans)} required id(s) are in no rendered "
                        f"batch: {orphans[:6]}")
    # THE PER-LEG DERIVATION. `required` is the UNSUBTRACTED union of the four
    # legs, so on a grounded night every leg's ungrounded count is zero BY
    # CONSTRUCTION. A non-zero one means the judged night and the declaration
    # disagree about the population, which is precisely what a state word hides.
    judgment = run.get("judgment") if isinstance(run.get("judgment"), dict) else {}
    legs = (((judgment or {}).get("run_facts") or {}).get("grounding")
            or {}).get("legs") or {}
    shortlegs = {leg: v.get("ungrounded") for leg, v in legs.items()
                 if isinstance(v, dict) and v.get("ungrounded")}
    if shortlegs:
        problems.append(f"the judged night reports ungrounded rows on a "
                        f"GROUNDED run: {shortlegs}")
    return problems, union, with_content


def substance_sentence(run: dict[str, Any], union: set[str],
                       with_content: set[str]) -> str:
    """Render the one sentence fragment E10 puts on its PASS line.

    THE SUBSTANCE SENTENCE. `with_content` per leg is what the judged night
    recorded; `used_block_vocab_lower_bound` is how many of those rows' free
    text carried a distinctive phrase from their block
    (`cos_judge.grounding_facts`).

    IT IS RENDERED AS THE LOWER BOUND IT IS (review 2026-08-15). The
    implementation says plainly "a LOWER BOUND on use, not a measure of it";
    this line rendered a bare `(used N)`, and a bare number at the report
    boundary reads as measured usage. THREE directions of error, all of them
    downward: a leg that used a block and paraphrased it completely scores 0;
    a leg that echoes one phrase without reasoning scores 1; and — the one the
    docstring omitted — the PROJECTION REFUSES any row sharing a five-token run
    with its block, so the most strongly grounded rows never reach this counter
    at all. The two mechanisms read the same shingle space at widths 2 and 5
    with opposite consequences, so `refused_grounding_overlap` belongs on this
    same sentence: without it, a night that refused every quoting row is
    indistinguishable from a night that ignored the vault.
    """
    judgment = run.get("judgment") if isinstance(run.get("judgment"), dict) else {}
    legs = (((judgment or {}).get("run_facts") or {}).get("grounding")
            or {}).get("legs") or {}
    per_leg = ", ".join(
        f"{leg} {v.get('with_content', 0)}/{v.get('rows', 0)}"
        f" (used at least {v.get('used_block_vocab_lower_bound', 0)})"
        for leg, v in sorted(legs.items()) if isinstance(v, dict))
    projection = (((run.get("judgment") or {}).get("run_facts") or {})
                  .get("grounding") or {}).get("projection") or {}
    refused = projection.get("refused_grounding_overlap")
    n_refused = (sum(refused.values()) if isinstance(refused, dict)
                 else (refused or 0))
    return (f"{len(with_content)} of {len(union)} delivered id(s) carried "
            f"vault content" + (f"; per leg with_content: {per_leg}"
                                if per_leg else "")
            + f"; {n_refused} row(s) refused for reproducing their block")


def grounding_clause(run: dict[str, Any], g: dict[str, Any],
                     delivery_problems: Callable) -> tuple[list[str], str]:
    """Score the grounding declaration's own state clause (E10's middle)."""
    problems: list[str] = []
    substance = ""
    if g.get("state") == "ungrounded":
        if not str(g.get("reason") or "").strip():
            problems.append("the night declares UNGROUNDED with no reason")
    elif g.get("state") == "grounded":
        missing = sorted(set(g.get("required") or []) - set(g.get("covered") or []))
        if missing:
            problems.append(f"{len(missing)} required grounding id(s) are "
                            f"uncovered: {missing[:6]}")
        extra, substance = delivery_problems(run)
        problems.extend(extra)
    else:
        problems.append(f"grounding state {g.get('state')!r} is undeclared")
    return problems, substance
