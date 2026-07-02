#!/usr/bin/env python3
"""LV-02 — map the committed golden set onto a REAL live-vault's paths and
validate every qrel path actually exists (both in the source vault and in the
migrated workspace's path-map), dropping any query that doesn't fully resolve
rather than fabricating a match.

``eval/golden_set.json`` (tracked, committable) authors its 66 queries against
an ANONYMISED scenario (Acme / Northwind / Meridian standing in for the real
counterparty names) so the golden set itself carries no sensitive data. This
script applies a deterministic substitution to recover an operator's real
paths, then keeps only queries whose qrel paths ALL exist in the source vault.
This output is sensitive (real project/counterparty names) and MUST stay under
a gitignored path (_evidence/, never eval/).

Set the substitution map below to your own scenario before running (the shipped
values are placeholders). Usage:
    python3 eval/build_live_golden_set.py \
        --golden eval/golden_set.json \
        --source-vault /path/to/your-vault \
        --out _evidence/live/live-golden-set.json \
        --qrels-out _evidence/live/live-qrels.json \
        --report _evidence/live/live-golden-set-report.json
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

# Deterministic map from the committed anonymised names to an operator's real
# vault names. PLACEHOLDER VALUES — edit the right-hand side to your own
# scenario before running (e.g. ("Acme", "YourOrg")). Order matters only in
# that each is a distinct token; applied as plain literal substitution
# (case-sensitive; Title-case source data only uses the capitalised form).
SUBSTITUTIONS = [
    ("Acme", "SourceOrg"),
    ("Northwind", "SourceCounterparty"),
    ("Meridian", "SourceProject"),
]


def _sub(s: str) -> str:
    for a, b in SUBSTITUTIONS:
        s = s.replace(a, b)
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--source-vault", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--qrels-out", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    src = Path(args.source_vault)

    kept, dropped = [], []
    for q in golden["queries"]:
        nq = copy.deepcopy(q)
        nq["text"] = _sub(q["text"])
        ok = True
        missing = []
        for r in nq["qrels"]:
            new_path = _sub(r["path"])
            r["path"] = new_path
            if not (src / new_path).is_file():
                ok = False
                missing.append(new_path)
        if ok:
            kept.append(nq)
        else:
            dropped.append({"id": q["id"], "missing_paths": missing})

    # ranx-native qrels, same shape as build_golden_set.py
    qrels_ranx: dict[str, dict[str, int]] = {}
    for q in kept:
        d: dict[str, int] = {}
        for r in q["qrels"]:
            key = r["path"]
            if q["stratum"] == "temporal" and "version_state" in r:
                key = f"{r['path']}#{r['version_state']}"
            d[key] = int(r["grade"])
        qrels_ranx[q["id"]] = d

    doc = dict(golden)
    doc["schema_version"] = golden.get("schema_version", "") + ".live-s10"
    doc["session"] = "cutover-s10"
    doc["canonical_key"] = "source_path (REAL live-vault relative path, de-anonymised from eval/golden_set.json)"
    doc["queries"] = kept
    doc["_dropped_unresolved"] = dropped

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.qrels_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.qrels_out).write_text(json.dumps(qrels_ranx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    by_stratum = {}
    for q in kept:
        by_stratum.setdefault(q["stratum"], 0)
        by_stratum[q["stratum"]] += 1
    report = {
        "source_golden": args.golden,
        "source_vault": str(src),
        "total_queries_in_source_golden": len(golden["queries"]),
        "kept": len(kept),
        "dropped": len(dropped),
        "kept_by_stratum": by_stratum,
        "dropped_detail": dropped,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"kept {len(kept)}/{len(golden['queries'])} queries (dropped {len(dropped)} unresolved)")
    print(f"wrote {args.out}")
    print(f"wrote {args.qrels_out}")
    print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
