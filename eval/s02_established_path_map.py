#!/usr/bin/env python3
"""Build the canonical-path map for S02's established retrieval corpus.

The established golden set is intentionally owner-private.  Its qrels retain
the pre-migration canonical paths, while the migrated Brainiac vault uses
stable note IDs and ``brain/`` / ``raw/`` paths.  This tool makes that mapping
explicit and fails closed if any qrel document cannot be resolved to exactly
one indexed-zone note.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _slugify(name: str) -> str:
    """Match the corpus migration's deterministic filename resolver."""
    name = re.sub(r"\.md$", "", name)
    normalized = unicodedata.normalize("NFKD", name)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"['’`]", "", normalized)
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    return re.sub(r"-+", "-", normalized).strip("-")


def _frontmatter_id(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---") or text.count("---") < 2:
        return None
    frontmatter = text.split("---", 2)[1]
    match = re.search(r"^id:\s*([^\n]+)", frontmatter, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def _indexable_notes(vault: Path) -> dict[str, list[str]]:
    """Return every canonical ID in zones that ``BrainIndex.rebuild`` scans."""
    by_id: dict[str, list[str]] = {}
    for zone in ("brain", "raw"):
        for path in sorted((vault / zone).rglob("*.md")):
            relative = path.relative_to(vault).as_posix()
            if relative.startswith("raw/originals/"):
                continue
            note_id = _frontmatter_id(path)
            if note_id:
                by_id.setdefault(note_id, []).append(relative)
    return by_id


def build_map(
    vault: Path,
    qrels: dict[str, dict[str, int]],
    name_map: dict[str, Any],
    legacy_id_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve every canonical qrel document to one current vault path."""
    legacy_id_overrides = legacy_id_overrides or {}
    notes = _indexable_notes(vault)
    canonical_paths = sorted({key.split("#", 1)[0] for docs in qrels.values() for key in docs})
    mapping: dict[str, str] = {}
    records: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    ambiguous: list[dict[str, Any]] = []

    for canonical in canonical_paths:
        slug = _slugify(Path(canonical).stem)
        note_id = legacy_id_overrides.get(canonical, name_map.get(slug, slug))
        paths = notes.get(str(note_id), [])
        if len(paths) == 1:
            relative = paths[0]
            if relative in mapping:
                ambiguous.append({
                    "canonical_path": canonical,
                    "note_id": str(note_id),
                    "paths": [relative],
                    "reason": "one current path would map to more than one canonical qrel path",
                })
                continue
            mapping[relative] = canonical
            records.append({
                "canonical_path": canonical,
                "brain_path": relative,
                "note_id": str(note_id),
                "resolution": "explicit_legacy_override" if canonical in legacy_id_overrides else "migration_name_map",
            })
        elif not paths:
            missing.append({"canonical_path": canonical, "note_id": str(note_id)})
        else:
            ambiguous.append({
                "canonical_path": canonical,
                "note_id": str(note_id),
                "paths": paths,
                "reason": "note id resolves to multiple current paths",
            })

    return {
        "schema_version": "s02-established-path-map.v1",
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vault": str(vault.resolve()),
        "mapping": dict(sorted(mapping.items())),
        "records": records,
        "coverage": {
            "qrel_document_count": len(canonical_paths),
            "mapped_document_count": len(records),
            "missing": missing,
            "ambiguous": ambiguous,
            "complete": len(records) == len(canonical_paths) and not missing and not ambiguous,
        },
        "legacy_overrides": sorted(
            canonical for canonical in canonical_paths if canonical in legacy_id_overrides
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, help="Migrated owner vault to evaluate")
    parser.add_argument("--qrels", required=True, help="Established canonical qrels JSON")
    parser.add_argument("--name-to-id-map", required=True, help="Migration resolver JSON")
    parser.add_argument(
        "--legacy-id-overrides",
        help="Optional owner-private JSON mapping of legacy canonical paths to migrated note IDs",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    vault = Path(args.vault).resolve()
    qrels_path = Path(args.qrels)
    name_map_path = Path(args.name_to_id_map)
    qrels = json.loads(qrels_path.read_text(encoding="utf-8"))
    name_map = json.loads(name_map_path.read_text(encoding="utf-8"))
    overrides_path = Path(args.legacy_id_overrides) if args.legacy_id_overrides else None
    overrides = (
        json.loads(overrides_path.read_text(encoding="utf-8"))
        if overrides_path
        else {}
    )
    if not isinstance(overrides, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in overrides.items()
    ):
        parser.error("--legacy-id-overrides must map string paths to string note IDs")
    result = build_map(vault, qrels, name_map, overrides)
    result["inputs"] = {
        "qrels_sha256": hashlib.sha256(qrels_path.read_bytes()).hexdigest(),
        "name_to_id_map_sha256": hashlib.sha256(name_map_path.read_bytes()).hexdigest(),
    }
    if overrides_path:
        result["inputs"]["legacy_id_overrides_sha256"] = hashlib.sha256(
            overrides_path.read_bytes()
        ).hexdigest()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage = result["coverage"]
    print(
        f"mapped {coverage['mapped_document_count']}/{coverage['qrel_document_count']} "
        f"qrel documents -> {out}"
    )
    return 0 if coverage["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
