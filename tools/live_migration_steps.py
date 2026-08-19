"""Write a migrated corpus workspace."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from tools import apply_live_migration as _source
from tools.apply_live_migration import (
    WIKILINK_RE,
    ZONE_TYPE,
    _dedupe_slug,
    _find_date,
    _iso_now,
    _yaml_scalar,
    mc,
)

__doc__ = _source.__doc__


def _collect_entries(src: Path, limit: int) -> tuple[list[dict], dict[str, str]]:
    zone_tag_of = {
        zone: re.sub(r"[^a-z0-9]+", "", zone.split(" ", 1)[1].lower())[:4] or "zn"
        for zone in mc.ZONE_RULES
    }
    slug_used: Counter[str] = Counter()
    entries: list[dict] = []
    files = sorted(mc.iter_md(src))
    if limit:
        files = files[:limit]
    title_index: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(src).as_posix()
        zone = relative.split("/", 1)[0]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        frontmatter, body = mc.split_frontmatter(text)
        has_frontmatter = frontmatter is not None
        meta: dict = {}
        if has_frontmatter:
            try:
                import yaml  # type: ignore

                loaded = yaml.safe_load(frontmatter)
                if isinstance(loaded, dict):
                    meta = loaded
            except Exception:
                meta = {}
        tier, confidence, rationale = mc.classify(
            zone, text, body if body is not None else text, has_frontmatter
        )
        raw_or_brain, bucket = mc.ZONE_RULES.get(
            zone, ("raw", "resources", "MNPI", "low")
        )[:2]
        raw_stem = path.stem
        note_id = _dedupe_slug(
            mc.slugify(raw_stem), slug_used, zone_tag_of.get(zone, "zn")
        )
        target_rel = (
            f"raw/{note_id}.md" if raw_or_brain == "raw"
            else f"brain/{bucket}/{note_id}.md"
        )
        entries.append({
            "source_rel": relative,
            "zone": zone,
            "classification": tier,
            "confidence": confidence,
            "rationale": rationale,
            "rawbrain": raw_or_brain,
            "bucket": bucket,
            "id": note_id,
            "target_rel": target_rel,
            "title": str(meta.get("title") or raw_stem),
            "date": _find_date(meta),
            "meta": meta,
            "body": body if body is not None else text,
            "path": path,
        })
        variants = {
            raw_stem,
            mc._title_of(raw_stem) if hasattr(mc, "_title_of") else raw_stem,
            re.sub(r"^\d\d[. ]", "", raw_stem).strip(),
        }
        for variant in variants:
            key = variant.strip().casefold()
            if key:
                title_index.setdefault(key, note_id)
    return entries, title_index


def _rewrite_link(
    match: re.Match[str], title_index: dict[str, str], unresolved_links: Counter[str]
) -> tuple[str, bool]:
    target, anchor, alias = match.group(1), match.group(2), match.group(3)
    segment = re.sub(r"^\d\d[. ]", "", target.split("/")[-1]).strip()
    key = segment.casefold()
    note_id = title_index.get(key)
    if note_id:
        return f"[[{note_id}{anchor}{alias}]]", True
    unresolved_links[key] += 1
    return match.group(0), False


def _write_entries(
    entries: list[dict], title_index: dict[str, str], dest: Path
) -> tuple[Counter[str], Counter[str], Counter[str], int, dict[str, str], int]:
    unresolved_links: Counter[str] = Counter()
    path_map: dict[str, str] = {}
    tier_counts: Counter[str] = Counter()
    zone_counts: Counter[str] = Counter()
    missing_classification = 0
    resolved_links = 0
    for entry in entries:
        tier = entry["classification"] or ""
        if not tier:
            missing_classification += 1
        tier_counts[tier] += 1
        zone_counts[entry["zone"]] += 1

        def rewrite(match: re.Match[str]) -> str:
            nonlocal resolved_links
            rewritten, resolved = _rewrite_link(match, title_index, unresolved_links)
            resolved_links += int(resolved)
            return rewritten

        new_body = WIKILINK_RE.sub(rewrite, entry["body"])
        frontmatter_lines = [
            "---",
            f"id: {entry['id']}",
            f"title: {_yaml_scalar(entry['title'])}",
            f"type: {ZONE_TYPE.get(entry['zone'], 'note')}",
            f"classification: {entry['classification']}",
            f"classification_confidence: {entry['confidence']}",
            f"zone: {entry['bucket']}",
            f"source_zone: {_yaml_scalar(entry['zone'])}",
            f"source_path: {_yaml_scalar(entry['source_rel'])}",
        ]
        if entry["date"]:
            frontmatter_lines.extend([
                f"created: {entry['date']}",
                f"updated: {entry['date']}",
            ])
        frontmatter_lines.extend([f"migrated: {_iso_now()}", "---"])
        output_path = dest / entry["target_rel"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            "\n".join(frontmatter_lines) + "\n" + new_body, encoding="utf-8"
        )
        path_map[entry["target_rel"]] = entry["source_rel"]
    return (
        unresolved_links, tier_counts, zone_counts, missing_classification,
        path_map, resolved_links,
    )


def _write_reports(
    src: Path,
    dest: Path,
    entries: list[dict],
    unresolved_links: Counter[str],
    tier_counts: Counter[str],
    zone_counts: Counter[str],
    missing_classification: int,
    resolved_links: int,
    path_map: dict[str, str],
    report_path: Path,
    path_map_path: Path,
) -> None:
    path_map_path.parent.mkdir(parents=True, exist_ok=True)
    path_map_path.write_text(
        json.dumps(path_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="source vault root (read-only)")
    ap.add_argument("--dest", required=True, help="workspace root to WRITE the migrated corpus into")
    ap.add_argument("--report", required=True)
    ap.add_argument("--path-map", required=True, help="target_relpath -> source_relpath JSON")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    src, dest = Path(args.source).resolve(), Path(args.dest).resolve()
    if not src.is_dir():
        print(f"error: {src} is not a directory")
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    entries, title_index = _collect_entries(src, args.limit)
    unresolved, tiers, zones, missing, path_map, resolved = _write_entries(
        entries, title_index, dest
    )
    _write_reports(
        src, dest, entries, unresolved, tiers, zones, missing, resolved,
        path_map, Path(args.report), Path(args.path_map),
    )
    print(f"wrote {len(entries)} notes -> {dest}")
    print(f"tiers: {dict(tiers)}")
    print(f"missing_classification (should be 0): {missing}")
    print(f"wikilinks resolved={resolved} unresolved_unique={len(unresolved)}")
    print(f"report -> {args.report}")
    print(f"path-map -> {args.path_map}")
    return 0
