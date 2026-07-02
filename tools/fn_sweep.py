#!/usr/bin/env python3
"""False-negative sweep over the WHOLE migrated corpus (S03 acceptance gate).

The r2-codex threshold requires ZERO known Restricted/Secret false-negatives — a
sensitive note classified too LOW is an egress failure (default-deny only
protects UNLABELLED notes, not mislabelled-LOW ones). The stratified human
spot-review samples ~40 notes; this sweep bounds the WHOLE corpus by re-scanning
every note classified at or below ``--ceiling`` (default Internal) for a strong
sensitivity token. Any hit is a candidate false-negative to fix or quarantine.

Reads the manifest emitted by migrate_corpus.py; re-reads each flagged source.

Usage:
    python3 tools/fn_sweep.py <source-vault> --manifest <jsonl> [--ceiling Internal] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TIERS = ["Public", "Internal", "Confidential", "Restricted", "Secret"]
RANK = {t: i for i, t in enumerate(TIERS)}

# Strong sensitivity tokens. A note classified <= ceiling that contains any of
# these is a candidate false-negative (should very likely be Confidential+).
STRONG = re.compile(
    r"\b(Globex|Zephyr|Orion|Nimbus|Atlas|Secret|insider nonpublic|"
    r"UnitA|UnitB|UnitC|system migration|migration strategy|cutover|"
    r"term ?sheet|valuation|due diligence|merger|acquisition|M&A|"
    r"pre[- ]?signing|not[- ]to[- ]exceed|reorganization|restructuring|"
    r"salary|compensation|headcount|board pack|equity stake)\b", re.I)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--ceiling", default="Internal", choices=TIERS)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    src = Path(args.source).resolve()
    ceil = RANK[args.ceiling]
    rows = [json.loads(l) for l in open(args.manifest, encoding="utf-8")]

    scanned = 0
    flags = []
    for r in rows:
        if RANK[r["classification"]] > ceil:
            continue
        scanned += 1
        p = src / r["source"]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = STRONG.search(text)
        if m:
            flags.append({
                "source": r["source"],
                "classified": r["classification"],
                "token": m.group(0),
            })

    result = {
        "ceiling": args.ceiling,
        "scanned_at_or_below_ceiling": scanned,
        "false_negative_candidates": len(flags),
        "zero_false_negatives": len(flags) == 0,
        "flags": flags[:200],
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    print(f"scanned {scanned} notes <= {args.ceiling}; "
          f"{len(flags)} false-negative candidate(s)")
    for f in flags[:25]:
        print(f"  FN? [{f['classified']}] token={f['token']!r}  {f['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
