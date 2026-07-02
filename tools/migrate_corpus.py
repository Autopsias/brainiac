#!/usr/bin/env python3
"""Corpus migration + bulk classification — DRY-RUN inventory & labeller (S03).

EVIDENCE-GATED deliverable (s03.context HARDENED:codex-verify-r1 / r2-codex):
the substrate is **default-deny on unlabelled**, so an un-migrated / unclassified
import is INVISIBLE to retrieval and any downstream eval Recall is meaningless.
This tool runs the Phase-0 inventory + Phase-3 *rule-based first pass* of
``docs/corpus-migration.md`` against a real source vault and emits the three
required evidence artifacts:

    --manifest  PATH   one JSONL row per source .md (source->target, tier, why)
    --coverage  PATH   coverage %, tier distribution, quarantined / unlabelled

It is a **DRY RUN**: the source vault is read-only; nothing is written into it.

Acceptance thresholds (r2-codex pass gate, checked by report_coverage):
  * 100% of notes labelled-or-explicitly-excluded (unlabelled count == 0).
  * uncertain labels are QUARANTINED -> treated most-restrictive (Secret).
  * ZERO known Restricted/Secret FALSE-NEGATIVES -> verified by the separate
    stratified human spot-review (classification-spot-review.md), since
    false-LOW is an egress failure and this rule pass can only bound it, not
    prove it.

Rule pass is deterministic & auditable; the LLM-assisted second pass and the
human review of every Restricted/Secret assignment are downstream (corpus-migration
Phase 3.2-3.4) and are NOT auto-applied above Internal.

Usage:
    python3 tools/migrate_corpus.py <source-vault> \
        --manifest _evidence/s03/migration-manifest.jsonl \
        --coverage _evidence/s03/migration-coverage.json
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

TIERS = ["Public", "Internal", "Confidential", "Restricted", "Secret"]
RANK = {t: i for i, t in enumerate(TIERS)}

# --- zone -> (raw|brain, PARA bucket, base tier, base confidence) ----------
# Maps the source Johnny-Decimal zones to the Profile A substrate. Base tier is
# the floor for the zone; content keywords can only ESCALATE (fail-closed).
ZONE_RULES = {
    "10 People":      ("brain", "areas",     "Confidential", "high"),
    "20 Companies":   ("brain", "areas",     "Confidential", "high"),
    "30 Projects":    ("brain", "projects",  "Confidential", "high"),
    "40 Meetings":    ("raw",   "areas",     "Confidential", "high"),
    "50 Sources":     ("raw",   "resources", "Internal",     "medium"),
    "60 Concepts":    ("brain", "resources", "Internal",     "high"),
    "70 Decisions":   ("brain", "resources", "Confidential", "high"),
    "90 System":      ("brain", "resources", "Internal",     "high"),
    "00 Inbox":       ("raw",   "resources", "Internal",     "low"),
    "80 Daily":       ("brain", "archive",   "Internal",     "medium"),
    "99 Workspace":   ("brain", "resources", "Internal",     "low"),
}

# Content escalation. Order matters: highest tier wins.
Secret_KW = re.compile(
    r"\b(Secret|insider nonpublic|insider information|inside information)\b", re.I)
RESTRICTED_KW = re.compile(
    r"\b(Globex|Zephyr|Orion|Nimbus|merger|acquisition|valuation|deal terms?|"
    r"counterpart(?:y|ies)|due diligence|term ?sheet|Atlas|"
    r"negotiation|equity stake|"
    # migration synonyms surfaced by the human spot-review (these were the
    # false-negative class: an Atlas note that did not literally say
    # 'Atlas' in the body). Fail-closed: escalate.
    r"UnitA|UnitB|UnitC|system migration|migration strategy|"
    r"cutover|decommission|reorganization|"
    r"M&A|pre[- ]?signing|not[- ]to[- ]exceed)\b", re.I)
CONFIDENTIAL_KW = re.compile(
    r"\b(salary|compensation|headcount|reorg|restructur|confidential|"
    r"board|pricing|budget|forecast)\b", re.I)

JD_FILENAME = re.compile(r"^\d\d[. ]")


def split_frontmatter(text: str):
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    return parts[1], parts[2]


def slugify(stem: str) -> str:
    s = JD_FILENAME.sub("", stem).strip()           # strip Johnny-Decimal prefix
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "untitled"


def classify(zone: str, scan_text: str, body: str, has_fm: bool):
    """Return (tier, confidence, rationale). Fail-closed: escalate on keywords,
    quarantine (Secret) anything genuinely undecidable.

    ``scan_text`` is the FULL note text (frontmatter + body): the S03 spot-review
    found that sensitivity often lives in frontmatter (``projects: [[Atlas]]``,
    ``topics: [Atlas]``) — scanning only the body produced false-negatives."""
    rule = ZONE_RULES.get(zone)
    if rule is None:
        # Unknown zone -> quarantine, most-restrictive, low confidence.
        return "Secret", "low", f"unknown source zone '{zone}' -> quarantine (default-deny)"
    _rawbrain, _bucket, base_tier, base_conf = rule
    tier, conf, why = base_tier, base_conf, f"zone '{zone}' base"

    # Content escalation (can only raise the tier). Scans full text (fm + body).
    if Secret_KW.search(scan_text):
        if RANK["Secret"] > RANK[tier]:
            tier, why = "Secret", why + " + Secret keyword"
        conf = "medium"
    if RESTRICTED_KW.search(scan_text):
        if RANK["Restricted"] > RANK[tier]:
            tier, why = "Restricted", why + " + restricted/deal keyword"
            conf = "medium"
    if CONFIDENTIAL_KW.search(scan_text):
        if RANK["Confidential"] > RANK[tier]:
            tier, why = "Confidential", why + " + confidential keyword"
            conf = "medium"

    # Quarantine genuinely empty/undecidable notes (no frontmatter AND tiny body
    # in a low-confidence zone) at the most-restrictive tier.
    if not has_fm and len(body.strip()) < 40 and base_conf == "low":
        return "Secret", "low", "no frontmatter + negligible body -> quarantine"

    return tier, conf, why


ZONE_DIR = re.compile(r"^\d\d ")  # Johnny-Decimal content zone, e.g. "10 People"


def iter_md(root: Path):
    """Yield Markdown that is actual VAULT CONTENT — only files inside a top-level
    Johnny-Decimal zone (``NN Name/``). Infrastructure (``.claude/``, ``.agents/``,
    ``.pytest_cache/``, ``_archive/``, ``_example_vault_scheduled_tasks_staging/``,
    root dotfiles, the build ``_plans/``) is NOT vault content and is excluded —
    matching the migration source in docs/corpus-migration.md."""
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if not rel.parts:
            continue
        top = rel.parts[0]
        if not ZONE_DIR.match(top):
            continue
        sp = p.as_posix()
        # Vendored dependency artifacts are NOT vault content.
        if any(seg in sp for seg in (
            "/.venv/", "/.venv-linux/", "/site-packages/", "/node_modules/",
            "/.git/", "/__pycache__/")):
            continue
        yield p


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

    rows = []
    tier_counts = Counter()
    conf_counts = Counter()
    zone_counts = Counter()
    quarantined = 0
    unlabelled = 0

    for i, p in enumerate(iter_md(src)):
        if args.limit and i >= args.limit:
            break
        rel = p.relative_to(src).as_posix()
        zone = rel.split("/", 1)[0]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            text = ""
        fm, body = split_frontmatter(text)
        has_fm = fm is not None
        tier, conf, why = classify(zone, text, body, has_fm)
        rule = ZONE_RULES.get(zone, ("raw", "resources", "Secret", "low"))
        rawbrain, bucket = rule[0], rule[1]
        target = (f"vault/raw/{slugify(p.stem)}.md" if rawbrain == "raw"
                  else f"vault/brain/{bucket}/{slugify(p.stem)}.md")
        is_quarantine = (conf == "low" and tier == "Secret")
        if is_quarantine:
            quarantined += 1
        # "labelled-or-explicitly-excluded": every row gets a tier OR is
        # quarantined. unlabelled = a row with no decision at all (should be 0).
        if not tier:
            unlabelled += 1

        rows.append({
            "source": rel,
            "zone": zone,
            "raw_or_brain": rawbrain,
            "para_bucket": bucket,
            "target": target,
            "classification": tier,
            "confidence": conf,
            "quarantined": is_quarantine,
            "has_frontmatter": has_fm,
            "rationale": why,
        })
        tier_counts[tier] += 1
        conf_counts[conf] += 1
        zone_counts[zone] += 1

    total = len(rows)
    labelled_or_excluded = sum(1 for r in rows if r["classification"])
    coverage_pct = round(100.0 * labelled_or_excluded / total, 2) if total else 0.0

    man_path = Path(args.manifest)
    man_path.parent.mkdir(parents=True, exist_ok=True)
    with man_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    coverage = {
        "source_vault": str(src),
        "total_notes": total,
        "labelled_or_excluded": labelled_or_excluded,
        "unlabelled": unlabelled,
        "coverage_pct": coverage_pct,
        "quarantined": quarantined,
        "tier_distribution": dict(tier_counts),
        "confidence_distribution": dict(conf_counts),
        "zone_distribution": dict(zone_counts),
        "restricted_or_secret": tier_counts["Restricted"] + tier_counts["Secret"],
        "thresholds": {
            "coverage_pct_required": 100.0,
            "coverage_pct_met": coverage_pct >= 100.0,
            "unlabelled_must_be_zero": unlabelled == 0,
        },
        "note": "DRY RUN. Rule-based first pass only; LLM second pass + human "
                "review of every Restricted/Secret assignment are downstream and "
                "NOT auto-applied above Internal. Mislabel rate is measured by "
                "the stratified human spot-review (classification-spot-review.md).",
    }
    Path(args.coverage).parent.mkdir(parents=True, exist_ok=True)
    Path(args.coverage).write_text(json.dumps(coverage, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8")

    print(f"inventoried {total} notes from {src}")
    print(f"coverage: {coverage_pct}% labelled-or-excluded; "
          f"{unlabelled} unlabelled; {quarantined} quarantined")
    print(f"tiers: {dict(tier_counts)}")
    print(f"manifest -> {man_path}")
    print(f"coverage -> {args.coverage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
