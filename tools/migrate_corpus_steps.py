"""Inventory corpus migration steps."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tools import migrate_corpus as _source
from tools.migrate_corpus import (
    ZONE_RULES,
    classify,
    iter_md,
    slugify,
    split_frontmatter,
)

__doc__ = _source.__doc__


def _inventory(
    src: Path, limit: int
) -> tuple[list[dict], Counter[str], Counter[str], Counter[str], int, int]:
    rows: list[dict] = []
    tier_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    zone_counts: Counter[str] = Counter()
    quarantined = 0
    unlabelled = 0
    for index, path in enumerate(iter_md(src)):
        if limit and index >= limit:
            break
        relative = path.relative_to(src).as_posix()
        zone = relative.split("/", 1)[0]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        frontmatter, body = split_frontmatter(text)
        has_frontmatter = frontmatter is not None
        tier, confidence, rationale = classify(zone, text, body, has_frontmatter)
        rule = ZONE_RULES.get(zone, ("raw", "resources", "MNPI", "low"))
        raw_or_brain, bucket = rule[0], rule[1]
        target = (f"vault/raw/{slugify(path.stem)}.md" if raw_or_brain == "raw"
                  else f"vault/brain/{bucket}/{slugify(path.stem)}.md")
        is_quarantine = confidence == "low" and tier == "MNPI"
        quarantined += int(is_quarantine)
        unlabelled += int(not tier)
        rows.append({
            "source": relative,
            "zone": zone,
            "raw_or_brain": raw_or_brain,
            "para_bucket": bucket,
            "target": target,
            "classification": tier,
            "confidence": confidence,
            "quarantined": is_quarantine,
            "has_frontmatter": has_frontmatter,
            "rationale": rationale,
        })
        tier_counts[tier] += 1
        confidence_counts[confidence] += 1
        zone_counts[zone] += 1
    return rows, tier_counts, confidence_counts, zone_counts, quarantined, unlabelled


def _write_reports(
    src: Path,
    rows: list[dict],
    tier_counts: Counter[str],
    confidence_counts: Counter[str],
    zone_counts: Counter[str],
    quarantined: int,
    unlabelled: int,
    manifest_path: Path,
    coverage_path: Path,
) -> tuple[int, float]:
    total = len(rows)
    labelled_or_excluded = sum(1 for row in rows if row["classification"])
    coverage_pct = round(100.0 * labelled_or_excluded / total, 2) if total else 0.0
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    coverage = {
        "source_vault": str(src),
        "total_notes": total,
        "labelled_or_excluded": labelled_or_excluded,
        "unlabelled": unlabelled,
        "coverage_pct": coverage_pct,
        "quarantined": quarantined,
        "tier_distribution": dict(tier_counts),
        "confidence_distribution": dict(confidence_counts),
        "zone_distribution": dict(zone_counts),
        "restricted_or_mnpi": tier_counts["Restricted"] + tier_counts["MNPI"],
        "thresholds": {
            "coverage_pct_required": 100.0,
            "coverage_pct_met": coverage_pct >= 100.0,
            "unlabelled_must_be_zero": unlabelled == 0,
        },
        "note": "DRY RUN. Rule-based first pass only; LLM second pass + human "
                "review of every Restricted/MNPI assignment are downstream and "
                "NOT auto-applied above Internal. Mislabel rate is measured by "
                "the stratified human spot-review (classification-spot-review.md).",
    }
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.write_text(
        json.dumps(coverage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return total, coverage_pct


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="source vault root (read-only)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--coverage", required=True)
    ap.add_argument("--limit", type=int, default=0, help="cap files (0 = all)")
    args = ap.parse_args()

    src = Path(args.source).resolve()
    if not src.is_dir():
        print(f"error: {src} is not a directory")
        return 2
    rows, tier_counts, confidence_counts, zone_counts, quarantined, unlabelled = _inventory(
        src, args.limit
    )
    manifest_path, coverage_path = Path(args.manifest), Path(args.coverage)
    total, coverage_pct = _write_reports(
        src, rows, tier_counts, confidence_counts, zone_counts,
        quarantined, unlabelled, manifest_path, coverage_path,
    )
    print(f"inventoried {total} notes from {src}")
    print(f"coverage: {coverage_pct}% labelled-or-excluded; "
          f"{unlabelled} unlabelled; {quarantined} quarantined")
    print(f"tiers: {dict(tier_counts)}")
    print(f"manifest -> {manifest_path}")
    print(f"coverage -> {coverage_path}")
    return 0
