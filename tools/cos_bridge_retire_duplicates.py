#!/usr/bin/env python3
"""DD-02 — rehearse the bridge-note cleanup: which copies to keep, which to retire.

The COS ingest bridge re-files an unchanged thread every night, so the reference
vault holds 1571 ``cosbridge-*`` notes for 190 conversations (2026-09-10). This
script groups those notes by (conversation, captured text) and plans ONE
supersession CHAIN per group — oldest to newest, keeper last.

CHAIN, DO NOT STAR. ``core.supersede(old, new)`` sets ``new.previous_version =
old`` on EVERY call, so pointing 30 copies at one keeper leaves the keeper naming
only the last of them — the asymmetric shape ``unsupersede``'s docstring was
written to repair, and ``tools/validate.py`` flags neither (checked 2026-09-10:
it has no reciprocity rule, no fork rule, and no supersession section at all —
its only bitemporal checks are the five in ``check_bitemporal``).

Four modes, and the split is load-bearing:

  ``<vault>``                     the DRY RUN (default). Reads the vault, writes
                                  a JSON receipt. Imports NOTHING that can write
                                  a note — no ``brain.core``, no index, no
                                  signing key. ``tests/`` asserts that in a
                                  subprocess.
  ``<vault> --apply <receipt>``   replays the receipt's ``apply`` pairs through
                                  ``core.supersede``.
  ``<vault> --undo <receipt>``    replays the receipt's ``undo`` pairs through
                                  ``core.unsupersede`` — the exact inverse,
                                  newest-to-oldest.
  ``--measure-fixture <out>``     times ``supersede`` end to end on a throwaway
                                  fixture vault sized to the target vault, so
                                  the receipt carries a MEASURED per-call cost.
  ``--roundtrip-fixture <out>``   applies the whole plan on a bridge fixture,
                                  undoes it, and diffs the frontmatter, so the
                                  receipt SHOWS the rollback rather than
                                  claiming it.

The last two live in ``tools/cos_bridge_retire_measure.py`` and are imported
only when asked for: everything they do writes notes, and the dry run may not.

Why the measurement is in here at all: every ``supersede`` takes the vault
writer lock, does two signed writes, and then runs a full incremental ``sync``
that walks the WHOLE vault. That is per PAIR, and there are 1360 of them, so the
owner's checkpoint is a decision about wall time as much as about the list.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

#: `cosbridge-<date>-run<N>-<key>` — the run is in the id, which is what orders
#: a group. `run9` precedes `run10`, which a plain string compare gets backwards.
_RUN_IN_ID = re.compile(r"^cosbridge-(\d{4}-\d{2}-\d{2})-run(\d+)-")
_CID = "provenance.conversation_id"
_SHA = "cos.source_sha256"


def _run_order(note_id: str) -> tuple[str, int] | None:
    m = _RUN_IN_ID.match(note_id)
    return (m.group(1), int(m.group(2))) if m else None


# ------------------------------------------------------------- the dry run --

def scan(vault: Path) -> tuple[list[dict], list[dict]]:
    """Group ``brain/**/cosbridge-*.md`` into (groups, unresolved).

    The grouping keys come from each note's own frontmatter. That is a GROUPING
    key, not a trust decision (STA-01: frontmatter alone is a claim, never
    evidence) — two notes claiming the same conversation and the same captured
    text are the same email filed twice, and nothing here reads a stamp as proof
    of anything.
    """
    from brain import frontmatter  # pure parse/serialise; writes nothing

    buckets: dict[tuple[str, str], list[dict]] = {}
    unresolved: list[dict] = []
    for path in sorted((vault / "brain").rglob("cosbridge-*.md")):
        rel = path.relative_to(vault).as_posix()
        meta, _ = frontmatter.parse_text(path.read_text(encoding="utf-8"))
        cid = str(meta.get(_CID) or "").strip()
        sha = str(meta.get(_SHA) or "").strip()
        order = _run_order(path.stem)
        missing = [k for k, v in ((_CID, cid), (_SHA, sha)) if not v]
        if order is None:
            missing.append("run-in-id")
        if missing:
            unresolved.append({"id": path.stem, "path": rel, "missing": missing})
            continue
        buckets.setdefault((cid, sha), []).append(
            {"id": path.stem, "path": rel, "order": order})

    groups = []
    for (cid, sha), members in sorted(buckets.items()):
        members.sort(key=lambda n: (n["order"], n["id"]))
        groups.append({
            "conversation_id": cid,
            "source_sha256": sha,
            "keeper": members[-1]["id"],
            "keeper_path": members[-1]["path"],
            "retirees": [m["id"] for m in members[:-1]],
        })
    return groups, unresolved


def chain_pairs(groups: list[dict]) -> list[list[str]]:
    """Adjacent (old, new) pairs, oldest-to-newest, keeper last — per group."""
    pairs: list[list[str]] = []
    for g in groups:
        chain = [*g["retirees"], g["keeper"]]
        pairs.extend([chain[i], chain[i + 1]] for i in range(len(chain) - 1))
    return pairs


def retire_sha(groups: list[dict]) -> str:
    """sha256 over the SORTED retiree id list.

    Deliberately over ids and not over note bytes: `--apply` rewrites those
    bytes, so a content hash would refuse every `--undo` of a successful apply.
    Ids are invariant under the chain, so this one sha guards both directions —
    it fires when the SET of notes in scope changed, which is the thing that
    makes a stale receipt dangerous.
    """
    retirees = [r for g in groups for r in g["retirees"]]
    return hashlib.sha256("\n".join(sorted(retirees)).encode("utf-8")).hexdigest()


def text_versions(groups: list[dict]) -> dict:
    """What the owner's SECOND option costs: retire older TEXT versions too.

    One group is one distinct captured text within one conversation, so the
    number of groups carrying a conversation id IS that thread's distinct-text
    count, and option 2 retires all but the newest of them.
    """
    per: Counter[str] = Counter(g["conversation_id"] for g in groups)
    dist = Counter(per.values())
    return {
        "conversations": len(per),
        "distinct_texts_per_conversation": dict(sorted(per.items())),
        "distribution": {str(k): dist[k] for k in sorted(dist)},
        "extra_retirees_option_2": sum(c - 1 for c in per.values()),
        "plain_words": (
            f"{len(per)} threads hold {sum(per.values())} distinct captured "
            f"texts between them. Retiring the older texts as well would retire "
            f"{sum(c - 1 for c in per.values())} more notes on top of this plan."),
    }


def build_receipt(vault: Path, measurement: dict | None,
                  roundtrip: dict | None = None) -> dict:
    groups, unresolved = scan(vault)
    apply_pairs = chain_pairs(groups)
    keep, retire = len(groups), len(apply_pairs)
    sample = _headline_sample(measurement)
    per_call = sample.get("median_seconds") if sample else None
    projected = round(per_call * retire, 1) if per_call else None
    return {
        "tool": "cos_bridge_retire_duplicates",
        "dry_run": True,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "vault": str(vault),
        "counts": {
            "total": keep + retire + len(unresolved),
            "keep": keep,
            "retire": retire,
            "unresolved": len(unresolved),
        },
        "retire_sha256": retire_sha(groups),
        "measured_seconds_per_supersede": per_call,
        "projected_total_seconds": projected,
        "projection_plain_words": _projection_words(per_call, projected, retire, sample),
        "measurement": measurement or {
            "note": "no --measure-json given; the cost fields are null, not estimated",
        },
        "text_versions": text_versions(groups),
        "how_to_reproduce": {
            "dry_run": "python3 tools/cos_bridge_retire_duplicates.py <vault> "
                       "--measure-json <m.json> --roundtrip-json <r.json> --out <out>",
            "measure": "python3 tools/cos_bridge_retire_duplicates.py "
                       "--measure-fixture <m.json> --fixture-notes 5638 "
                       "--fixture-note-bytes 25837 --fixture-pairs 7",
            "roundtrip": "python3 tools/cos_bridge_retire_duplicates.py "
                         "--roundtrip-fixture <r.json>",
            "apply": "python3 tools/cos_bridge_retire_duplicates.py <vault> "
                     "--apply <this file>",
            "undo": "python3 tools/cos_bridge_retire_duplicates.py <vault> "
                    "--undo <this file>",
            "tests": "pytest -q tests/test_cos_bridge_retire_duplicates.py",
        },
        "groups": groups,
        "unresolved": unresolved,
        "apply": apply_pairs,
        "undo": list(reversed(apply_pairs)),
        "undo_roundtrip": roundtrip or {
            "note": "no --roundtrip-json given; the rollback is untested here",
        },
        "undo_note": (
            "core.unsupersede drops the three retirement keys from the old note "
            "and previous_version from the new one, but LEAVES the successor's "
            "own is_latest_version: true (its docstring says so: that key may "
            "head an unrelated chain). So an apply-then-undo round trip returns "
            "every retiree byte-identical and leaves each group's keeper "
            "carrying one extra line, is_latest_version: true. That line is "
            "valid on its own under tools/validate.py's check_bitemporal — only "
            "is_latest_version: false needs a superseded_by."),
    }


def _headline_sample(measurement: dict | None) -> dict:
    """The sample closest to the real workload — the largest vault measured."""
    samples = (measurement or {}).get("samples") or []
    return max(samples, key=lambda s: s.get("notes", 0)) if samples else {}


def _projection_words(per_call, projected, retire: int, sample: dict) -> str:
    if not per_call:
        return ("Not measured. Run --measure-fixture and pass --measure-json "
                "before quoting a wall time.")
    span = (f"{projected / 3600:.1f} hours" if projected >= 5400
            else f"{projected / 60:.0f} minutes")
    load = round((sample.get("load_average_before") or [0])[0], 1)
    return (
        f"One supersede took {per_call:.2f} seconds end to end on a fixture "
        f"vault of {sample.get('notes')} notes averaging "
        f"{sample.get('note_bytes')} bytes — the shape of the target vault. "
        f"{retire} of them is about {span} of continuous writing, and every one "
        f"takes the vault writer lock, so the nightly cannot write while it "
        f"runs. Taken at a 1-minute load average of {load} on a Mac that also "
        f"runs two self-hosted CI runners, so the reading is recorded rather "
        f"than assumed quiet; every sample and its own load figure is in "
        f"`measurement.samples`, and the smaller one shows how much of this "
        f"cost is the full-vault sync each call ends with.")


# --------------------------------------------------------- apply and undo ---

def refuse_unless_ready(vault: Path, receipt: dict) -> None:
    """Every refusal that needs no writing surface at all. Raises ``SystemExit``.

    Write-free on purpose, so the suite can exercise all three without standing
    up an index, a signing key or an audit chain.

    The index check has to happen HERE rather than after the core is built:
    ``BrainCore.__init__`` CREATES an empty index when none is there, so asking
    it afterwards can never report the missing index this is meant to refuse on.
    """
    from brain import config

    if str(receipt.get("vault")) != str(vault):
        raise SystemExit(f"refused: receipt was written for {receipt.get('vault')!r}, "
                         f"not {str(vault)!r}")
    if not Path(config.index_path(vault)).exists():
        raise SystemExit(f"refused: no index at {config.index_path(vault)} — "
                         "run `brain rebuild` first")
    groups, _ = scan(vault)
    found = retire_sha(groups)
    if found != receipt.get("retire_sha256"):
        raise SystemExit(f"refused: the vault changed since the dry run "
                         f"(retire_sha256 {found} != {receipt.get('retire_sha256')})")


def audit_or_refuse(core) -> dict:
    """Refuse to write into a chain that does not verify.

    ``content_drift`` is NOT a broken chain — it means a signed note's bytes
    changed on disk with no disposition, which is a triage backlog, not a
    linkage failure. Refusing on it would block this cleanup on an unrelated
    queue; the count is printed instead.
    """
    audit = core.verify_audit()
    if audit.get("errors") or audit.get("status") not in ("ok", "content_drift"):
        raise SystemExit(f"refused: audit chain status={audit.get('status')!r} with "
                         f"{len(audit.get('errors') or [])} error(s) — repair the "
                         "chain before writing to it")
    if audit.get("content_drift_unexplained"):
        print(f"  note: {audit['content_drift_unexplained']} signed note(s) carry "
              "undispositioned content drift; the chain itself verifies")
    return audit


def _guarded_core(vault: Path, receipt: dict):
    """Refuse BEFORE constructing anything that writes, then hand back the core."""
    refuse_unless_ready(vault, receipt)
    from brain.core import BrainCore

    core = BrainCore(vault=vault, role="host")
    audit_or_refuse(core)
    return core


#: the refusal that IS this script's idempotence, per verb
_ALREADY = {"supersede": "is already superseded",
            "unsupersede": "is not superseded by"}


def replay(core, pairs: list[list[str]], verb: str, *, reason: str,
           reindex: bool = True, stats: dict | None = None) -> dict:
    """Run every pair through ``verb``, treating its own inverse-state refusal
    as "already done" and letting every OTHER refusal stop the run.

    ``stats`` is the caller's own dict, updated AS the loop runs. A refusal
    re-raises, so counters kept in locals die with the exception and the
    operator learns nothing about how far a 1360-pair mutation got; a caller
    that passes its dict in can still print the receipt on the abort path.
    """
    if stats is None:
        stats = {}
    stats.update(verb=verb, pairs=len(pairs))
    stats.setdefault("written", 0)
    stats.setdefault("already", 0)
    op = getattr(core, verb)
    for i, (old, new) in enumerate(pairs, 1):
        try:
            op(old, new, reason=reason, reindex=reindex)
            stats["written"] += 1
        except ValueError as exc:
            if _ALREADY[verb] not in str(exc):
                raise
            stats["already"] += 1
        if i % 25 == 0 or i == len(pairs):
            print(f"  {verb} {i}/{len(pairs)}  written={stats['written']} "
                  f"already={stats['already']}", flush=True)
    return stats


def _reconcile(core, banner: str) -> dict:
    """The ONE reconcile every exit from the bulk replay takes.

    The banner is best-effort: a ``BrokenPipeError`` raised by ``replay``'s own
    progress line reaches the abort path too, and an unguarded print there
    would raise again and skip the reconcile entirely.

    Recovery runs BEFORE the sync. An interrupt between a pair's two signed
    writes leaves a pending journal, and indexing or publishing that half-chain
    hands every reader an unfinished transaction — `old` retired with no
    reciprocal `previous_version` on `new`, which is exactly the one-sided
    chain `unsupersede` exists to repair. ``_recover_pending_supersede`` is the
    documented rollback for that journal, it restores both sides, and it is
    idempotent and a no-op when no journal exists.

    ``publish=True`` republishes the snapshot: a retirement of this size makes
    the Cowork VM's read-only copy materially wrong until some later publishing
    sync runs, and the VM cannot detect that itself.
    """
    with contextlib.suppress(Exception):
        print(banner, flush=True)
    core._recover_pending_supersede()
    return core.sync(drain=False, publish=True)


# ------------------------------------------------------------------- main ---

def _load_json(path: str | None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("vault", nargs="?", help="vault root (dry run / apply / undo)")
    p.add_argument("--dry-run", action="store_true",
                   help="the default; accepted so it can be written out loud")
    p.add_argument("--apply", metavar="RECEIPT")
    p.add_argument("--undo", metavar="RECEIPT")
    p.add_argument("--out", metavar="PATH", help="where the dry run writes its receipt")
    p.add_argument("--measure-json", metavar="PATH",
                   help="a --measure-fixture output to fold into the receipt")
    p.add_argument("--measure-fixture", metavar="OUT_PATH")
    p.add_argument("--roundtrip-fixture", metavar="OUT_PATH")
    p.add_argument("--roundtrip-json", metavar="PATH",
                   help="a --roundtrip-fixture output to fold into the receipt")
    p.add_argument("--fixture-notes", type=int, default=5638)
    p.add_argument("--fixture-note-bytes", type=int, default=25837)
    p.add_argument("--fixture-pairs", type=int, default=5)
    return p.parse_args(argv)


def _measure_module():
    """Import the write-capable sibling — ONLY from --measure-fixture/--undo/
    --apply/--roundtrip-fixture, never on the dry run. Run as a file, sys.path[0]
    is ``tools/``, so ``from tools import ...`` needs the repo root on the path
    (the same one-liner every other script in this directory uses).
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import cos_bridge_retire_measure as m

    return m


def main(argv=None) -> int:
    args = _parse_args(argv)

    if args.measure_fixture:
        m = _measure_module()

        sample = m.measure_fixture(args.fixture_notes, args.fixture_note_bytes,
                                   args.fixture_pairs)
        out = Path(args.measure_fixture)
        prior = json.loads(out.read_text()).get("samples", []) if out.exists() else []
        _write_json(out, {"samples": [*prior, sample]})
        print(f"measured {sample['median_seconds']}s per supersede -> {out}")
        return 0

    if args.roundtrip_fixture:
        m = _measure_module()

        report = m.roundtrip_fixture()
        _write_json(Path(args.roundtrip_fixture), report)
        print(report["verdict"])
        return 0

    if not args.vault:
        print("a vault path is required", file=sys.stderr)
        return 2
    vault = Path(args.vault).resolve()

    if args.apply or args.undo:
        receipt = json.loads(Path(args.apply or args.undo).read_text(encoding="utf-8"))
        core = _guarded_core(vault, receipt)
        verb = "supersede" if args.apply else "unsupersede"
        from brain.lock import vault_writer_lock

        # One sync at the END, not one per pair: the per-pair sync walks the
        # whole vault, so it dominates -- measured 26s of a 26s call here, which
        # is 10 hours over 1360 pairs. Two things make that safe, and both are
        # code rather than prose, because the maintenance window is an operator
        # habit and cannot be relied on.
        #
        # 1. ONE writer lock around the whole replay. `supersede` takes the same
        #    lock per pair and the lock is re-entrant per process, so every
        #    inner acquisition is a no-op -- but no OTHER writer or reader can
        #    now observe the half-reconciled index between two pairs.
        # 2. The sync runs even when the replay aborts. `replay` re-raises any
        #    refusal that is not its own idempotence case, so without this every
        #    pair already written stayed signed on disk and invisible to every
        #    search/get/--latest-only query, with nothing reporting it.
        #
        # `_reconcile` carries the rest: the journal recovery, the sync, and why
        # the banner is best-effort.
        pairs = receipt["apply" if args.apply else "undo"]
        stats: dict = {}
        with vault_writer_lock(vault, verb=f"cos-bridge-retire-{verb}"):
            try:
                replay(core, pairs, verb, stats=stats, reindex=False,
                       reason="DD-02: one email, one note (bridge duplicate cleanup)")
            except BaseException as exc:  # noqa: BLE001 - Ctrl-C must reconcile too
                stats["aborted"] = repr(exc)
                _reconcile(core, "  aborted -- reconciling, do NOT interrupt ...")
                with contextlib.suppress(Exception):
                    print(json.dumps(stats, indent=2), flush=True)
                raise
            stats["final_sync"] = _reconcile(core, "  final sync ...")
        print(json.dumps(stats, indent=2))
        return 0

    measurement = _load_json(args.measure_json)
    receipt = build_receipt(vault, measurement, _load_json(args.roundtrip_json))
    if args.out:
        _write_json(Path(args.out), receipt)
        print(f"{receipt['counts']} -> {args.out}")
    else:
        print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
