#!/usr/bin/env python3
"""S10 LV-01 — APPLY the corpus migration (Phase 1/2/3 of docs/corpus-migration.md)
into a NAMED, gitignored brain workspace. The source vault is READ-ONLY (never
written to); this is the write-side counterpart of ``migrate_corpus.py``'s
Phase-0 dry-run inventory.

Reuses the SAME classify()/slugify()/iter_md() rules as migrate_corpus.py (single
source of truth for tier assignment) so the applied corpus and the dry-run
coverage report never disagree.

For every source note this writes a NEW file under ``<workspace>/raw/<slug>.md``
or ``<workspace>/brain/<bucket>/<slug>.md`` carrying:
  - a fresh frontmatter block: id, title, type, classification, zone (source JD
    zone, kept for traceability), created/updated (best-effort from the
    original frontmatter or file mtime), source_path (original relative path
    -- NOT egress-surfaced by the brain CLI, but useful for audit/debug)
  - the original body, with a best-effort wikilink rewrite: ``[[Old Title]]``
    is rewritten to ``[[new-slug-id]]`` when the target is also migrated;
    unresolvable links are left as-is (reported in the summary, not fatal --
    graph-expand degrades gracefully on a dangling link).

Also writes a ``path-map.json`` ``{target_relpath: source_relpath}`` for
``eval/capture_brain_run.py --map`` (so retrieval hits over the migrated corpus
normalise back to the ORIGINAL vault paths the golden set's qrels use).

The source vault is opened READ-ONLY throughout (only ``Path.read_text`` calls
against it); every write goes to ``--dest``.

Usage:
    python3 tools/apply_live_migration.py <source-vault> --dest _workspace/live-vault \
        --report _evidence/cutover-s10/migration-apply-report.json \
        --path-map _evidence/cutover-s10/path-map.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Import migrate_corpus.py as a module (single source of truth for the
# classify/slugify/iter_md/ZONE_RULES rules — never re-implement them here).
_spec = importlib.util.spec_from_file_location("migrate_corpus", HERE / "migrate_corpus.py")
mc = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mc)

ZONE_TYPE = {
    "10 People": "person",
    "20 Companies": "company",
    "30 Projects": "project",
    "40 Meetings": "meeting",
    "50 Sources": "source",
    "60 Concepts": "concept",
    "70 Decisions": "decision",
    "90 System": "doc",
    "00 Inbox": "inbox",
    "80 Daily": "daily",
    "99 Workspace": "workspace-note",
}

WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)((?:#[^\]\|]+)?)((?:\|[^\]]+)?)\]\]")
DATE_KEYS = ("document_date", "created", "date", "captured", "updated")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _yaml_scalar(v: str) -> str:
    """Quote a frontmatter scalar if it contains a colon-space, a leading
    special char, or other YAML-unsafe content (typed-zone-creation-discipline
    lesson: unquoted 'label: rest: more' parses as a nested mapping)."""
    if v is None:
        return '""'
    s = str(v)
    needs_quote = (
        ": " in s or s.strip() != s or s.startswith(("#", "-", "[", "{", "*", "&", "!", "|", ">", "'", '"', "%", "@", "`"))
        or s.lower() in ("true", "false", "null", "yes", "no", "~", "")
        or re.match(r"^[\d.+-]+$", s or "")
    )
    if needs_quote:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _find_date(meta: dict) -> str:
    for k in DATE_KEYS:
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            m = re.match(r"\d{4}-\d{2}-\d{2}", v.strip())
            if m:
                return m.group(0)
    return ""


def _dedupe_slug(base: str, used: Counter, zone_tag: str) -> str:
    if used[base] == 0:
        used[base] += 1
        return base
    cand = f"{base}-{zone_tag}"
    if used[cand] == 0:
        used[cand] += 1
        return cand
    n = 2
    while used[f"{cand}-{n}"] > 0:
        n += 1
    used[f"{cand}-{n}"] += 1
    return f"{cand}-{n}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="source vault root (read-only)")
    ap.add_argument("--dest", required=True, help="workspace root to WRITE the migrated corpus into")
    ap.add_argument("--report", required=True)
    ap.add_argument("--path-map", required=True, help="target_relpath -> source_relpath JSON")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    src = Path(args.source).resolve()
    dest = Path(args.dest).resolve()
    if not src.is_dir():
        print(f"error: {src} is not a directory")
        return 2
    dest.mkdir(parents=True, exist_ok=True)

    # ---- pass 1: read + classify + compute slug ids -----------------------
    zone_tag_of = {z: re.sub(r"[^a-z0-9]+", "", z.split(" ", 1)[1].lower())[:4] or "zn" for z in mc.ZONE_RULES}
    slug_used: Counter = Counter()
    entries = []  # dict per note: source_rel, zone, tier, conf, rationale, rawbrain, bucket, id, target_rel, title, date, orig_meta, body
    files = sorted(mc.iter_md(src))
    if args.limit:
        files = files[: args.limit]

    title_index: dict[str, str] = {}  # normalised old-title-key -> new id

    for p in files:
        rel = p.relative_to(src).as_posix()
        zone = rel.split("/", 1)[0]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        fm, body = mc.split_frontmatter(text)
        has_fm = fm is not None
        meta = {}
        if has_fm:
            try:
                import yaml  # type: ignore
                loaded = yaml.safe_load(fm)
                if isinstance(loaded, dict):
                    meta = loaded
            except Exception:
                meta = {}
        tier, conf, rationale = mc.classify(zone, text, body if body is not None else text, has_fm)
        rule = mc.ZONE_RULES.get(zone, ("raw", "resources", "MNPI", "low"))
        rawbrain, bucket = rule[0], rule[1]

        raw_stem = p.stem
        base_slug = mc.slugify(raw_stem)
        nid = _dedupe_slug(base_slug, slug_used, zone_tag_of.get(zone, "zn"))
        target_rel = (f"raw/{nid}.md" if rawbrain == "raw" else f"brain/{bucket}/{nid}.md")

        title = str(meta.get("title") or raw_stem)
        date = _find_date(meta)

        entries.append({
            "source_rel": rel, "zone": zone, "classification": tier, "confidence": conf,
            "rationale": rationale, "rawbrain": rawbrain, "bucket": bucket, "id": nid,
            "target_rel": target_rel, "title": title, "date": date, "meta": meta,
            "body": body if body is not None else text, "path": p,
        })

        # index title variants -> id, for wikilink resolution
        for variant in {raw_stem, mc._title_of(raw_stem) if hasattr(mc, "_title_of") else raw_stem,
                        re.sub(r"^\d\d[. ]", "", raw_stem).strip()}:
            key = variant.strip().casefold()
            if key:
                title_index.setdefault(key, nid)

    # ---- pass 2: write, rewriting wikilinks against the title_index -------
    unresolved_links = Counter()
    resolved_links = 0
    path_map: dict[str, str] = {}
    tier_counts = Counter()
    zone_counts = Counter()
    missing_classification = 0

    def _rewrite_link(m: re.Match) -> str:
        nonlocal resolved_links
        target, anchor, alias = m.group(1), m.group(2), m.group(3)
        seg = target.split("/")[-1]
        seg = re.sub(r"^\d\d[. ]", "", seg).strip()
        key = seg.casefold()
        new_id = title_index.get(key)
        if new_id:
            resolved_links += 1
            return f"[[{new_id}{anchor}{alias}]]"
        unresolved_links[key] += 1
        return m.group(0)

    for e in entries:
        tier = e["classification"] or ""
        if not tier or tier not in mc.TIERS if hasattr(mc, "TIERS") else False:
            pass  # classify() always returns one of TIERS; kept for completeness
        if not tier:
            missing_classification += 1
        tier_counts[tier] += 1
        zone_counts[e["zone"]] += 1

        new_body = WIKILINK_RE.sub(_rewrite_link, e["body"])

        fm_lines = ["---"]
        fm_lines.append(f"id: {e['id']}")
        fm_lines.append(f"title: {_yaml_scalar(e['title'])}")
        fm_lines.append(f"type: {ZONE_TYPE.get(e['zone'], 'note')}")
        fm_lines.append(f"classification: {e['classification']}")
        fm_lines.append(f"classification_confidence: {e['confidence']}")
        fm_lines.append(f"zone: {e['bucket']}")
        fm_lines.append(f"source_zone: {_yaml_scalar(e['zone'])}")
        fm_lines.append(f"source_path: {_yaml_scalar(e['source_rel'])}")
        if e["date"]:
            fm_lines.append(f"created: {e['date']}")
            fm_lines.append(f"updated: {e['date']}")
        fm_lines.append(f"migrated: {_iso_now()}")
        fm_lines.append("---")
        new_text = "\n".join(fm_lines) + "\n" + new_body

        out_path = dest / e["target_rel"]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(new_text, encoding="utf-8")
        path_map[e["target_rel"]] = e["source_rel"]

    Path(args.path_map).parent.mkdir(parents=True, exist_ok=True)
    Path(args.path_map).write_text(json.dumps(path_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "source_vault": str(src),
        "workspace": str(dest),
        "total_notes_written": len(entries),
        "zone_distribution": dict(zone_counts),
        "tier_distribution": dict(tier_counts),
        "missing_classification": missing_classification,
        "wikilinks_resolved": resolved_links,
        "wikilinks_unresolved_unique_targets": len(unresolved_links),
        "wikilinks_unresolved_total": sum(unresolved_links.values()),
        "top_unresolved_targets": unresolved_links.most_common(15),
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {len(entries)} notes -> {dest}")
    print(f"tiers: {dict(tier_counts)}")
    print(f"missing_classification (should be 0): {missing_classification}")
    print(f"wikilinks resolved={resolved_links} unresolved_unique={len(unresolved_links)}")
    print(f"report -> {args.report}")
    print(f"path-map -> {args.path_map}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
