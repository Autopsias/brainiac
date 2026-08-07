#!/usr/bin/env python3
"""S06 / PT-03 — calibrate the RET-01 zone-authority prior on a HELD-OUT split.

s05's Gate 0 measured ``$BRAIN_ZONE_WEIGHTS={"brain": W, "raw": 1.0}`` lifting
``monolingual_pt`` off an exact 0.000 and the whole 66-query set from 0.424 to
0.530 — but it swept W over the SAME 66 queries it scored, so
the winning W is fitted to its own test set.  The owner accepted the option
*including* its calibration condition, so this script answers the question
that condition asks: **does a weight chosen on one half of the golden set
still help on the other half?**

The split is NOT invented here.  ``eval/golden_set.json`` already carries a
pre-registered, stratum-balanced ``held_out`` flag (33 train / 33 held-out,
each stratum split evenly).  We:

  1. sweep W and record, for EVERY gold document of every query, the rank it
     reached (one search pass per weight — ranks are deterministic, so every
     split below is computed offline from this one capture);
  2. select W* = argmax mean <target metric> on the TRAIN half only, ties to
     the smaller weight (conservative — less distortion of the ranking);
  3. read the HELD-OUT half ONCE, comparing W* against the shipped W=1.0 with
     ``eval.stats.paired_permutation_test(fold_context="held-out")`` — the
     one primary significance regime for this eval (H19) — plus a descriptive
     bootstrap CI, the minimum detectable effect at this n, and the achieved
     power.  MDE is the honest answer to "can 66 queries support a split at
     all": if the observed effect sits below what n=33 could resolve, the
     right conclusion is "not calibratable here", not a shipped constant.

Three integrity rules the first cut of this script did not have, each of which
silently corrupted the numbers it produced (fixed 2026-08-04 after review):

  * **Qrels resolve or the capture FAILS.**  A qrel that cannot be mapped to
    an indexed note used to be dropped and its query scored as a permanent
    miss at every weight — three ``temporal`` queries scored 0.0 everywhere
    for a naming mismatch, not a retrieval failure.  Resolution now goes
    canonical path -> ``--qrel-overrides`` -> ``--name-to-id-map`` -> slug,
    the same chain ``eval/s02_established_path_map.py`` uses, and any
    unresolved POSITIVE qrel aborts the run (``--allow-unmappable-qrels``
    to proceed anyway; the count is always recorded).
  * **recall@10 is recall, not hit@10.**  30 of the 66 golden queries have
    more than one gold document.  Keeping only the best rank makes an
    aggregate "gain" able to hide the loss of the other relevant documents,
    so every gold document's rank is captured.  The minimum rank feeds MRR
    and hit@10 ONLY.
  * **The held-out split is claimed ONCE.**  Only a FRESH BARE capture may
    claim ``held-out``: a ``--from-ranks`` replay and any rerank arm are
    refused outright (not merely defaulted), so a stored capture cannot mint
    a second "primary" result by switching ``--target`` — including the
    pre-fix artifacts that carry no fold-context field to key on.  Only the
    pre-declared target metric is labelled primary; the others are secondary
    descriptives.

Read-only against the index.  Emits ranks, metric values and the canonical
qrel paths already present in ``eval/golden_set.json`` — never note bodies.

Two phases, so re-analysis never needs a re-search:

    python3 eval/zone_prior_calibration.py --vault V --index-db D --out O.json
    python3 eval/zone_prior_calibration.py --from-ranks O.json --out O2.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

from stats import (  # noqa: E402
    achieved_power,
    bootstrap_ci,
    minimum_detectable_effect,
    paired_permutation_test,
)

STRATA = ["monolingual_pt", "monolingual_es", "cross_lingual_en_pt",
          "cross_lingual_en_es", "lexical_identifier", "multi_hop", "temporal"]

SCHEMA = "s06-zone-prior-calibration.v2"


# --------------------------------------------------------------------------
# metrics — every one reads the FULL per-gold rank vector
# --------------------------------------------------------------------------

def _ranks(value) -> list[int | None]:
    """Normalise a stored rank cell to a per-gold-document list.

    v1 captures (and the synthetic test fixtures) store one scalar ``rank |
    None`` per query; v2 stores one entry per gold document.  Accept both so a
    v1 artifact stays re-analysable — its recall@10 then legitimately equals
    its hit@10, because that is all the data it holds.
    """
    if isinstance(value, list):
        return list(value)
    return [value]


def _best(value) -> int | None:
    found = [r for r in _ranks(value) if r]
    return min(found) if found else None


def mrr10(value) -> float:
    r = _best(value)
    return 1.0 / r if r and r <= 10 else 0.0


def hit10(value) -> float:
    r = _best(value)
    return 1.0 if r and r <= 10 else 0.0


def recall10(value) -> float:
    """Relevant documents retrieved in the top 10 / relevant documents known."""
    rs = _ranks(value)
    return sum(1 for r in rs if r and r <= 10) / len(rs) if rs else 0.0


def hit1(value) -> float:
    return 1.0 if _best(value) == 1 else 0.0


METRICS = {"mrr@10": mrr10, "recall@10": recall10, "hit@10": hit10, "hit@1": hit1}


def _score(cells: list) -> dict:
    n = len(cells) or 1
    return {"n": len(cells),
            **{name: round(sum(f(c) for c in cells) / n, 4)
               for name, f in METRICS.items()}}


# --------------------------------------------------------------------------
# qrel resolution — the canonical path map, failing closed
# --------------------------------------------------------------------------

def _load_name_map(path: str | None) -> dict[str, str]:
    """The migration's slug -> note-id resolver (``{"map": {...}}`` or bare)."""
    if not path:
        return {}
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    inner = doc.get("map") if isinstance(doc, dict) and "map" in doc else doc
    if not isinstance(inner, dict):
        raise SystemExit(f"--name-to-id-map is not a JSON object: {path}")
    return {str(k): str(v) for k, v in inner.items() if not str(k).startswith("_")}


def _load_overrides(path: str | None) -> dict[str, str]:
    """Owner-private ``{canonical qrel path: note id}``, same shape as
    ``eval/s02_established_path_map.py --legacy-id-overrides``."""
    if not path:
        return {}
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    inner = doc.get("overrides") if isinstance(doc, dict) and "overrides" in doc else doc
    if not isinstance(inner, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in inner.items()):
        raise SystemExit(f"--qrel-overrides must map string paths to string note ids: {path}")
    return inner


def resolve_gold(qrels: list[dict], notes: dict[str, tuple[str, str]],
                 name_map: dict[str, str], overrides: dict[str, str],
                 slugify) -> tuple[list[str], list[str]]:
    """Return (vault-relative gold paths, unresolved canonical qrel paths).

    Resolution order matches ``eval/s02_established_path_map.build_map``:
    an explicit override, then the migration name map, then the slug itself.
    """
    gold: list[str] = []
    unresolved: list[str] = []
    for r in qrels:
        canonical = r["path"]
        slug = slugify(Path(canonical).stem)
        note_id = overrides.get(canonical) or name_map.get(slug, slug)
        entry = notes.get(str(note_id))
        if entry:
            gold.append(entry[0])
        else:
            unresolved.append(canonical)
    return sorted(set(gold)), unresolved


def refuse_unmappable(unmappable: dict[str, list[str]], allow: bool) -> None:
    """Abort unless every positive qrel resolved (``--allow-unmappable-qrels``).

    An unresolved qrel is scored as a miss at EVERY weight, so it is a
    fabricated retrieval failure that shifts means, MDE and power alike, and it
    silently unbalances the pre-registered strata. Fail closed.
    """
    if not unmappable or allow:
        return
    lines = "\n".join(f"  {qid}: {', '.join(paths)}"
                      for qid, paths in sorted(unmappable.items()))
    raise SystemExit(
        f"{sum(len(v) for v in unmappable.values())} positive qrel(s) do not "
        f"resolve to an indexed note:\n{lines}\n"
        "An unresolved qrel scores as a permanent miss at every weight, which "
        "is a fabricated retrieval failure. Supply --qrel-overrides / "
        "--name-to-id-map, or pass --allow-unmappable-qrels to proceed with "
        "the count recorded in the artifact."
    )


def prior_fold_context(cap: dict) -> str | None:
    """The fold context an already-attached analysis claimed, if any.

    Checks EVERY metric, not just ``primary_metric``: a v1 artifact has no
    ``primary_metric`` key, and keying on it alone left exactly the v1 captures
    this guard exists to protect replayable into a second held-out primary.
    """
    prior = cap.get("analysis") or {}
    contexts = {((read or {}).get("permutation") or {}).get("fold_context")
                for read in (prior.get("held_out_read") or {}).values()}
    return "held-out" if "held-out" in contexts else next(iter(contexts - {None}), None)


def refuse_held_out(cap: dict, from_ranks: str | None, rerank: bool) -> None:
    """Refuse to CLAIM the held-out split unless this is a fresh bare capture.

    The first cut of this guard keyed on ``prior_fold_context(cap) ==
    "held-out"`` — a property only the artifact it had already fixed carried.
    The retained pre-fix RERANK capture declares ``"non-held-out"`` on every
    metric, so it sailed straight through and re-read the locked split as a
    second ``recall@10`` primary (p=0.0276). The rule is now positional, not
    field-derived, so an artifact that predates a field cannot slip past it:

      * a REPLAY (``--from-ranks``) re-reads a split some earlier run already
        claimed — there is no such thing as a first read of a stored capture;
      * a RERANK arm is a confirmation of the bare primary read, never a
        primary of its own (see the module docstring's third integrity rule);
      * anything already carrying an analysis has been read.
    """
    if from_ranks:
        raise SystemExit(
            f"{from_ranks} is a stored capture: a replay may not claim "
            "'held-out'. The held-out split is read ONCE, by the capture that "
            "produced it; re-analysing it under another target would mint a "
            "second primary result from the same data. Re-run with "
            "--fold-context non-held-out."
        )
    if rerank or (cap.get("arm") or {}).get("rerank"):
        raise SystemExit(
            "a rerank arm may not claim 'held-out': it CONFIRMS the bare "
            "primary read on the same split rather than being a primary read "
            "of its own. Re-run with --fold-context non-held-out."
        )
    prior = prior_fold_context(cap)
    if prior is not None:
        raise SystemExit(
            f"this capture already carries a {prior!r} analysis. The held-out "
            "split is read ONCE. Re-run with --fold-context non-held-out."
        )


# --------------------------------------------------------------------------
# phase 1 — capture per-query, per-gold-document ranks at each weight
# --------------------------------------------------------------------------

def capture(args) -> dict:
    from capture_run import _fingerprint
    from pt_stratum_diagnosis import _slug, _vault_notes

    from brain.embed import get_embedder
    from brain.index import BrainIndex
    from brain.vectors import get_backend

    vault = Path(args.vault).resolve()
    notes = _vault_notes(vault)
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    name_map = _load_name_map(args.name_to_id_map)
    overrides = _load_overrides(args.qrel_overrides)

    queries = []
    unmappable: dict[str, list[str]] = {}
    for q in golden["queries"]:
        gold, unresolved = resolve_gold(q["qrels"], notes, name_map, overrides, _slug)
        if unresolved:
            unmappable[q["id"]] = unresolved
        queries.append({"id": q["id"], "stratum": q["stratum"],
                        "held_out": bool(q.get("held_out")), "text": q["text"],
                        "gold": gold, "unresolved_qrels": unresolved})

    refuse_unmappable(unmappable, args.allow_unmappable_qrels)

    ranks: dict[str, dict[str, list[int | None]]] = {}
    applied: dict[str, dict[str, bool]] = {}
    gated: dict[str, dict[str, bool]] = {}
    timing: dict[str, float] = {}
    index_stats: dict = {}
    for w in args.weights:
        key = f"{w}"
        os.environ["BRAIN_ZONE_WEIGHTS"] = json.dumps({"brain": w, "raw": 1.0})
        index = BrainIndex(db_path=Path(args.index_db).resolve(),
                           backend=get_backend("sqlite-vec"),
                           embedder=get_embedder("auto"), read_only=True)
        index_stats = index.stats()
        t0 = time.time()
        per_q: dict[str, list[int | None]] = {}
        per_q_rerank: dict[str, bool] = {}
        per_q_gate: dict[str, bool] = {}
        for q in queries:
            if not q["gold"]:
                per_q[q["id"]] = []
                continue
            # Trace mode runs the SAME ranking; it is the only way to record
            # whether reranking actually happened rather than what was asked
            # for (an absent model or a fired timeout silently falls back).
            hits, trace = index.hybrid_search_with_trace(
                q["text"], k=args.k, rerank=args.rerank, rerank_top=args.rerank_top)
            # `rerank_applied` alone cannot separate an RK-02 gate skip from an
            # absent model or a fired timeout (AGENTS.md §5), so keep both.
            per_q_rerank[q["id"]] = bool(trace.rerank_applied)
            per_q_gate[q["id"]] = bool((trace.rerank_gate or {}).get("skipped"))
            order, seen = [], set()
            for h in hits:
                rel = (str(Path(h.path).relative_to(vault))
                       if Path(h.path).is_absolute() else h.path)
                if rel not in seen:
                    seen.add(rel)
                    order.append(rel)
            per_q[q["id"]] = [(order.index(g) + 1 if g in order else None)
                              for g in q["gold"]]
        timing[key] = round(time.time() - t0, 1)
        if hasattr(index, "close"):
            index.close()
        ranks[key] = per_q
        applied[key] = per_q_rerank
        gated[key] = per_q_gate
        cells = list(per_q.values())
        print(f"brain={w:<5} overall recall@10 {_score(cells)['recall@10']:.4f} "
              f"mrr {_score(cells)['mrr@10']:.4f}  ({timing[key]}s)")
    os.environ.pop("BRAIN_ZONE_WEIGHTS", None)

    return {
        "probe": SCHEMA,
        "captured": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fingerprint": _fingerprint(index_stats, args.golden),
        "arm": {"k": args.k, "rerank": args.rerank,
                "rerank_top": args.rerank_top if args.rerank else None,
                "zone_scope": os.environ.get("BRAIN_ZONE_SCOPE", "semantic_only")},
        "qrel_resolution": {
            "name_to_id_map": args.name_to_id_map,
            "qrel_overrides": args.qrel_overrides,
            "unmappable_qrels": sum(len(v) for v in unmappable.values()),
            "unmappable_by_query": unmappable,
            "allowed_unmappable": bool(args.allow_unmappable_qrels),
        },
        "weights": list(args.weights),
        "queries": queries,
        "ranks": ranks,
        "rerank_applied": applied,
        "rerank_gate_skipped": gated,
        "seconds_per_weight": timing,
    }


# --------------------------------------------------------------------------
# phase 2 — offline split analysis
# --------------------------------------------------------------------------

def _rerank_application(cap: dict, weights: list[str], base: str,
                        selected: str) -> dict | None:
    """Whether the reranker actually ran EQUALLY across the compared arms.

    A cross-encoder timeout is sticky for the rest of a pass, so two weights
    can differ in how much of the run was reranked at all.  A delta between
    unequally-reranked arms measures the reranker, not the zone prior.
    """
    applied = cap.get("rerank_applied")
    if not applied:
        return None
    counts = {w: sum(1 for v in applied.get(w, {}).values() if v) for w in weights}
    totals = {w: len(applied.get(w, {})) for w in weights}
    # Equal COUNTS are not equal APPLICATION: {q1: True, q2: False} against
    # {q1: False, q2: True} totals the same while the two arms reranked
    # DIFFERENT paired observations, so the per-query deltas still measure the
    # reranker. Compare query-key sets and per-query values, never the totals.
    a = applied.get(base) or {}
    b = applied.get(selected) or {}
    mismatched = sorted(k for k in set(a) | set(b) if bool(a.get(k)) != bool(b.get(k)))
    equal = set(a) == set(b) and not mismatched
    return {"applied_per_weight": counts, "queries_per_weight": totals,
            "compared_weights": [base, selected], "equal_application": equal,
            "mismatched_query_count": len(mismatched),
            "mismatched_queries": mismatched[:20],
            "same_query_keys": set(a) == set(b),
            "comparison_valid": equal}


def analyse(cap: dict, target: str = "mrr@10", fold_context: str = "held-out",
            allow_unequal_rerank: bool = False) -> dict:
    # `fold_context` exists so the held-out split is only ever CLAIMED once.
    # The bare-ranking arm is the single primary read; any confirmation arm
    # (e.g. the same weights re-measured through the production reranker)
    # passes "non-held-out", which makes `stats` label its p-value
    # informational instead of confirmatory. It changes no arithmetic.
    if target not in METRICS:
        raise SystemExit(f"unknown target metric {target!r}")
    queries = cap["queries"]
    ranks = cap["ranks"]
    weights = [str(w) for w in cap["weights"]]
    base = "1.0"
    if base not in ranks:
        raise SystemExit("the sweep must include the shipped weight 1.0 as the baseline")

    train = [q for q in queries if not q["held_out"]]
    heldout = [q for q in queries if q["held_out"]]

    def per_query(w: str, qs: list[dict], metric: str) -> list[float]:
        f = METRICS[metric]
        return [f(ranks[w][q["id"]]) for q in qs]

    def mean(xs) -> float:
        return sum(xs) / len(xs) if xs else float("nan")

    # --- selection: TRAIN half only, ties to the smaller weight -----------
    train_curve = {w: round(mean(per_query(w, train, target)), 4) for w in weights}
    best = max(train_curve.values())
    selected = min((w for w in weights if train_curve[w] >= best - 1e-12),
                   key=lambda w: float(w))

    rerank_application = _rerank_application(cap, weights, base, selected)
    if rerank_application and not rerank_application["comparison_valid"] \
            and not allow_unequal_rerank:
        raise SystemExit(
            "the reranker did not run on the same QUERIES in the two compared "
            f"arms ({rerank_application['applied_per_weight']} applied; "
            f"{rerank_application['mismatched_query_count']} queries differ: "
            f"{rerank_application['mismatched_queries']}); a delta between "
            "unequally-reranked arms measures the reranker, not the zone "
            "prior. Re-capture, or pass --allow-unequal-rerank to record the "
            "comparison as INVALID rather than as evidence."
        )

    unmappable = (cap.get("qrel_resolution") or {}).get("unmappable_qrels", 0)

    # --- the single held-out read ----------------------------------------
    out: dict = {
        "target_metric": target,
        "primary_metric": target,
        "split": {"source": "golden_set.json held_out flag (pre-registered, "
                            "stratum-balanced)",
                  "train_n": len(train), "held_out_n": len(heldout),
                  "held_out_scorable_n": sum(1 for q in heldout if q.get("gold")),
                  "unmappable_qrels": unmappable},
        "train_curve": train_curve,
        "selected_weight": selected,
        "rerank_application": rerank_application,
        "held_out_read": {},
        "descriptive": {},
    }

    for metric in ("mrr@10", "recall@10", "hit@10"):
        primary = metric == target
        a = per_query(selected, heldout, metric)
        b = per_query(base, heldout, metric)
        deltas = [x - y for x, y in zip(a, b)]
        sd = (math.sqrt(sum((d - mean(deltas)) ** 2 for d in deltas) / (len(deltas) - 1))
              if len(deltas) > 1 else 0.0)
        # Only the PRE-DECLARED target may claim the held-out split; every other
        # metric is a secondary descriptive on the same data.
        perm = paired_permutation_test(
            deltas, fold_context=fold_context if primary else "non-held-out")
        ci = bootstrap_ci(deltas)
        obs = mean(deltas)
        out["held_out_read"][metric] = {
            "role": "primary" if primary else "secondary_descriptive",
            "baseline_weight": base,
            "baseline_mean": round(mean(b), 4),
            "selected_mean": round(mean(a), 4),
            "mean_delta": round(obs, 4),
            "delta_sd": round(sd, 4),
            "queries_better": sum(1 for d in deltas if d > 0),
            "queries_worse": sum(1 for d in deltas if d < 0),
            "queries_unchanged": sum(1 for d in deltas if d == 0),
            "permutation": perm.as_dict(),
            "permutation_caveat": perm.caveat(),
            "bootstrap_ci": ci.as_dict(),
            "mde_80pct_power": round(minimum_detectable_effect(len(deltas), sd), 4),
            "achieved_power_at_observed_effect": round(
                achieved_power(len(deltas), sd, abs(obs)), 4),
        }

    # --- descriptive only: full curve on both halves and per stratum ------
    for w in weights:
        out["descriptive"][w] = {
            "train": _score([ranks[w][q["id"]] for q in train]),
            "held_out": _score([ranks[w][q["id"]] for q in heldout]),
            "all": _score([ranks[w][q["id"]] for q in queries]),
            "by_stratum": {
                s: _score([ranks[w][q["id"]] for q in queries if q["stratum"] == s])
                for s in STRATA
            },
        }
    held_curve = {w: out["descriptive"][w]["held_out"][target] for w in weights}
    out["held_out_curve_DESCRIPTIVE"] = held_curve
    out["argmax_agreement"] = {
        "train_argmax": selected,
        "held_out_argmax": min(
            (w for w in weights if held_curve[w] >= max(held_curve.values()) - 1e-12),
            key=lambda w: float(w)),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault")
    ap.add_argument("--index-db")
    ap.add_argument("--golden", default=str(HERE / "golden_set.json"))
    ap.add_argument("--name-to-id-map",
                    help="migration slug->note-id resolver JSON (same file "
                         "eval/s02_established_path_map.py takes)")
    ap.add_argument("--qrel-overrides",
                    help="owner-private JSON {canonical qrel path: note id} for "
                         "qrels the name map cannot resolve")
    ap.add_argument("--allow-unmappable-qrels", action="store_true",
                    help="do NOT abort on an unresolved positive qrel (it scores "
                         "as a permanent miss; the count is recorded either way)")
    ap.add_argument("--weights", nargs="*", type=float,
                    default=[1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0])
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--rerank", action="store_true",
                    help="measure through the PRODUCTION rerank path (slow)")
    ap.add_argument("--rerank-top", type=int, default=20)
    ap.add_argument("--allow-unequal-rerank", action="store_true",
                    help="record an arm whose reranker fired unequally across "
                         "weights as INVALID instead of refusing to analyse it")
    ap.add_argument("--from-ranks", help="re-analyse an earlier capture, no search")
    ap.add_argument("--target", default="mrr@10", choices=["mrr@10", "recall@10"])
    ap.add_argument("--fold-context", default=None,
                    choices=["held-out", "non-held-out"],
                    help="'held-out' = the single primary read, and available "
                         "ONLY to a fresh bare capture. A replay (--from-ranks) "
                         "or a rerank arm defaults to 'non-held-out' — which "
                         "labels the p-value informational and changes no "
                         "arithmetic — and is REFUSED if it asks for "
                         "'held-out' explicitly.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.from_ranks:
        cap = json.loads(Path(args.from_ranks).read_text(encoding="utf-8"))
    else:
        if not (args.vault and args.index_db):
            ap.error("--vault and --index-db are required unless --from-ranks is given")
        cap = capture(args)

    # A replay, and any arm measured through the reranker, is a CONFIRMATION —
    # defaulting it to "held-out" is what let a second "primary" result be
    # minted from the same data just by changing --target.
    fold_context = args.fold_context
    if fold_context is None:
        fold_context = "non-held-out" if (args.from_ranks or args.rerank) else "held-out"
    if fold_context == "held-out":
        refuse_held_out(cap, args.from_ranks, bool(args.rerank))

    cap["analysis"] = analyse(cap, args.target, fold_context, args.allow_unequal_rerank)
    a = cap["analysis"]
    res = cap.get("qrel_resolution") or {}
    if res:
        print(f"qrels: {res.get('unmappable_qrels', 0)} unmappable")
    if a.get("rerank_application"):
        print(f"rerank applied: {a['rerank_application']['applied_per_weight']}")
    print(f"\nselected on TRAIN (n={a['split']['train_n']}) by {a['target_metric']}: "
          f"weight {a['selected_weight']}  [{fold_context}]")
    for m, r in a["held_out_read"].items():
        print(f"  held-out {m} ({r['role']}): {r['baseline_mean']} -> {r['selected_mean']} "
              f"(delta {r['mean_delta']:+.4f}, p={r['permutation']['p_two_sided']:.4f}, "
              f"MDE@80% {r['mde_80pct_power']:.4f}, power {r['achieved_power_at_observed_effect']:.2f})")
    print(f"  argmax agreement: {a['argmax_agreement']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
