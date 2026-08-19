"""Plan cross-tier ruling actions."""
from __future__ import annotations

import argparse
from typing import Any
from pathlib import Path

from tools import apply_crosstier_ruling as _source
from tools.apply_crosstier_ruling import (
    DEFAULT_PAIRLIST,
    DEFAULT_RULING,
    Pair,
    Plan,
    Stop,
)

__doc__ = _source.__doc__

def _load_pair_states(
    vault: Path, pair: Pair, paths: dict[str, Path]
) -> tuple[Any, Any]:
    for nid in (pair.low, pair.high):
        if nid not in paths:
            raise Stop(f"{nid!r} is not in the index — the pairlist is stale")
    return _source.read_state(vault, paths[pair.low]), _source.read_state(vault, paths[pair.high])


def _validate_pair_state(pair: Pair, lo: Any, hi: Any) -> None:
    from brain.classification import RANK

    if hi.tier != pair.high_tier:
        raise Stop(
            f"{pair.high}: on-disk tier {hi.tier!r} != ruling's {pair.high_tier!r}")
    if lo.tier not in (pair.low_tier, hi.tier):
        raise Stop(
            f"{pair.low}: on-disk tier {lo.tier!r} is neither the ruling's "
            f"{pair.low_tier!r} nor the already-raised {hi.tier!r}")
    if RANK.get(hi.tier, -1) <= RANK.get(pair.low_tier, 99):
        # The ruling is "all 201 resolve UPWARD". A pair that would move a
        # label DOWN is not this ruling's business.
        raise Stop(f"{pair.low} -> {hi.tier!r} would not be a RAISE "
                   f"(from {pair.low_tier!r}) — refusing")

def _existing_link(lo: Any, hi: Any, pair: Pair) -> str:
    if lo.superseded_by == pair.high:
        return "low retired"
    if hi.superseded_by == pair.low:
        return "HIGH retired"
    return ""


def _plan_link(
    pair: Pair,
    ruling: dict[str, dict[str, bool]],
    lo: Any,
    hi: Any,
    raise_to: str | None,
) -> Plan:
    existing = _existing_link(lo, hi, pair)

    if not ruling[pair.bucket]["link"]:
        note = f"bucket {pair.bucket}: prohibited by the ruling"
        if existing:
            note += (f" — BUT ALREADY LINKED ({existing}), pre-dating this run. "
                     "The ruling forbids CREATING one and did not order breaking "
                     "one; reported, not touched")
        return Plan(pair, raise_to, False, note)
    if existing:
        return Plan(pair, raise_to, False, f"already linked ({existing}) — left alone")
    blockers = []
    if lo.retired:
        blockers.append(f"low already retired (superseded_by={lo.superseded_by or '-'})")
    if hi.retired:
        blockers.append(f"high is retired (superseded_by={hi.superseded_by or '-'})")
    if hi.previous_version and hi.previous_version != pair.low:
        blockers.append(
            f"high.previous_version={hi.previous_version!r} would be overwritten")
    if blockers:
        return Plan(pair, raise_to, False, "REFUSED: " + "; ".join(blockers))
    return Plan(pair, raise_to, True, "")


def plan_pair(vault: Path, pair: Pair, paths: dict[str, Path],
              ruling: dict[str, dict[str, bool]]) -> Plan:
    if pair.bucket not in ruling:
        raise Stop(f"pair {pair.low!r} is bucket {pair.bucket!r}, "
                   "which the ruling does not disposition")
    lo, hi = _load_pair_states(vault, pair, paths)
    _validate_pair_state(pair, lo, hi)
    raise_to = None if lo.tier == hi.tier else hi.tier
    return _plan_link(pair, ruling, lo, hi, raise_to)


def _parse_args(argv: list[str] | None) -> Any:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", default=None, help="default: $BRAIN_VAULT")
    ap.add_argument("--ruling", type=Path, default=DEFAULT_RULING)
    ap.add_argument("--pairlist", type=Path, default=DEFAULT_PAIRLIST)
    ap.add_argument("--apply", action="store_true",
                    help="write. Default is a dry run.")
    ap.add_argument("--only", default=None,
                    help="one pair, by its LOW id (the single-pair probe)")
    ap.add_argument("--bucket", default=None, help="restrict to one bucket")
    ap.add_argument("--limit", type=int, default=0)
    return ap.parse_args(argv)


def _filter_pairs(pairs: list[Pair], args: Any) -> list[Pair]:
    if args.only:
        pairs = [pair for pair in pairs if pair.low == args.only]
        if not pairs:
            raise Stop(f"--only {args.only!r} matches no pair")
    if args.bucket:
        pairs = [pair for pair in pairs if pair.bucket == args.bucket]
    if args.limit:
        pairs = pairs[:args.limit]
    return pairs


def _apply_raises(
    plans: list[Plan], core: Any, vault: Path, apply: bool,
    vault_writer_lock: Any, paths: dict[str, Path]
) -> None:
    if not apply:
        return
    with vault_writer_lock(vault, verb="crosstier-raise"):
        for plan in plans:
            if plan.raise_to:
                _source.do_raise(core, vault, plan.pair, paths, plan.raise_to)
        core.sync(drain=False)


def _render_plans(
    plans: list[Plan], core: Any, vault: Path, apply: bool, vault_writer_lock: Any,
    paths: dict[str, Path],
) -> tuple[int, int]:
    n_raised = n_linked = 0
    retired_this_run: set[str] = set()
    _apply_raises(plans, core, vault, apply, vault_writer_lock, paths)
    for index, plan in enumerate(plans, 1):
        pair = plan.pair
        if plan.raise_to:
            result = f"raise {pair.low_tier}->{plan.raise_to}"
            if apply:
                n_raised += 1
        else:
            result = f"raise SKIP (already {pair.high_tier})"
        if plan.link:
            link_result = _render_link(pair, core, apply, retired_this_run)
            if apply and link_result.startswith("link ") and "retired=>" in link_result:
                n_linked += 1
        else:
            link_result = f"link NO ({plan.link_note})"
        cos = "COS" if pair.cos_used else "   "
        print(f"[{index:3}/{len(plans)}] {pair.bucket:8} {cos} {pair.low}")
        print(f"            {result:34} | {link_result}")
    return n_raised, n_linked


def _render_link(
    pair: Pair,
    core: Any,
    apply: bool,
    retired_this_run: set[str],
) -> str:
    if not apply:
        return f"link {pair.low} =retired=> {pair.high}"
    if pair.low in retired_this_run:
        return f"link NO (REFUSED: {pair.low} was retired earlier in this run)"
    try:
        _source.do_link(core, pair)
    except ValueError as exc:
        return f"link NO (REFUSED by engine: {exc})"
    retired_this_run.add(pair.low)
    return f"link {pair.low} =retired=> {pair.high}"


def main(argv: list[str] | None = None) -> int:
    import os

    from brain import config
    from brain.core import BrainCore, vault_writer_lock

    args = _parse_args(argv)
    vault = Path(os.path.expanduser(
        args.vault or os.environ.get("BRAIN_VAULT", ""))).resolve()
    if not vault.is_dir():
        raise Stop(f"no vault at {vault} — set $BRAIN_VAULT")

    n_raw = _source.vault_guard(vault)
    ruling = _source.load_ruling(args.ruling)
    pairs = _source.load_pairs(args.pairlist)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"# BAK-01 cross-tier ruling — {mode}")
    print(f"# vault    : {vault}  ({n_raw} raw sources)")
    print(f"# index    : {config.index_dir(vault)}")
    print(f"# ruling   : {args.ruling}")
    for b in sorted(ruling):
        print(f"#   bucket {b:8} raise={ruling[b]['raise']} link={ruling[b]['link']}")
    print(f"# pairs    : {len(pairs)}")
    print()

    pairs = _filter_pairs(pairs, args)

    core = BrainCore(vault, role="host")
    paths = _source.note_paths(core, {p.low for p in pairs} | {p.high for p in pairs})

    plans = [plan_pair(vault, pair, paths, ruling) for pair in pairs]

    n_raise = sum(1 for pl in plans if pl.raise_to)
    n_link = sum(1 for pl in plans if pl.link)

    n_raised, n_linked = _render_plans(
        plans, core, vault, args.apply, vault_writer_lock, paths
    )

    print()
    # On --apply these are what ACTUALLY happened, not what was planned: a run
    # that stops early must never print the plan's numbers as if it finished.
    print(f"# would raise : {n_raise}" if not args.apply else f"# raised : {n_raised}")
    print(f"# would link  : {n_link}" if not args.apply else f"# linked : {n_linked}")
    for why in ("prohibited", "already linked", "REFUSED"):
        n = sum(1 for pl in plans if not pl.link and why in pl.link_note)
        print(f"# link {why:15}: {n}")
    if not args.apply:
        print("\n# nothing was written. Re-run with --apply.")
    return 0
