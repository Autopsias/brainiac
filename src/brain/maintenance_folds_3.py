"""Nightly navigation regeneration fold."""
from __future__ import annotations

from pathlib import Path
from typing import Any

def refresh_navigation(vault: Path) -> dict[str, Any]:
    """NAV-01: regenerate the human navigation surfaces nightly —
    ``brain/backlinks.md`` + one ``catalog.md`` per PARA zone. Byte-compatible
    with ``tools/validate.py --backlinks --catalogs`` (same formats, both
    deterministic, no wall-clock timestamps), so either producer yields a
    no-op diff over an unchanged vault."""
    return _refresh_navigation_impl(vault)


def _load_navigation_notes(vault: Path) -> list[dict[str, Any]]:
    from . import frontmatter as fm

    notes: list[dict[str, Any]] = []
    brain_dir = vault / "brain"
    raw_dir = vault / "raw"
    for base, zone in ((raw_dir, "raw"), (brain_dir, "brain")):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.name in ("backlinks.md", "catalog.md"):
                continue
            try:
                meta, body = fm.parse_text(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — a broken note never kills navigation
                continue
            notes.append({"meta": meta, "body": body, "path": path, "zone": zone})
    return notes


def _write_navigation_backlinks(
    brain_dir: Path, notes: list[dict[str, Any]],
) -> int:
    ids = {note["meta"].get("id") for note in notes if note["meta"].get("id")}
    backlinks: dict[str, set[str]] = {}
    for note in notes:
        source = note["meta"].get("id")
        for match in _WIKILINK_RE.finditer(note["body"]):
            target = match.group(1).strip()
            if target in ids:
                backlinks.setdefault(target, set()).add(source)
    title_by_id = {
        note["meta"].get("id"): note["meta"].get("title", note["meta"].get("id"))
        for note in notes
    }
    lines = [
        "---", "id: backlinks", "title: \"Backlinks (generated)\"",
        "type: index", "classification: Internal", "---", "",
        "# Backlinks (generated — do not hand-edit)", "",
    ]
    for target in sorted(backlinks):
        lines.append(f"## [[{target}]]")
        for source in sorted(link for link in backlinks[target] if link):
            lines.append(f"- [[{source}|{title_by_id.get(source, source)}]]")
        lines.append("")
    brain_dir.mkdir(parents=True, exist_ok=True)
    (brain_dir / "backlinks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(backlinks)


def _write_navigation_catalogs(
    brain_dir: Path, notes: list[dict[str, Any]],
) -> dict[str, int]:
    by_zone: dict[str, list[dict[str, Any]]] = {zone: [] for zone in _PARA_ZONES}
    for note in notes:
        if note["zone"] != "brain":
            continue
        relative = note["path"].relative_to(brain_dir).parts
        if len(relative) > 1 and relative[0] in by_zone:
            by_zone[relative[0]].append(note)
    for zone in _PARA_ZONES:
        zone_dir = brain_dir / zone
        zone_dir.mkdir(parents=True, exist_ok=True)
        catalog = [
            "---", f"id: catalog-{zone}",
            f"title: \"{zone.capitalize()} catalog (generated)\"",
            "type: index", "classification: Internal", "---", "",
            f"# {zone.capitalize()} catalog (generated — do not hand-edit)", "",
            "| id | title | type | updated | classification |",
            "|---|---|---|---|---|",
        ]
        for note in sorted(by_zone[zone], key=lambda item: item["meta"].get("id") or ""):
            meta = note["meta"]
            catalog.append(
                f"| [[{meta.get('id', '')}]] | {meta.get('title', '')} | "
                f"{meta.get('type', '')} | {meta.get('updated', '')} | {meta.get('classification', '')} |"
            )
        catalog.append("")
        (zone_dir / "catalog.md").write_text("\n".join(catalog) + "\n", encoding="utf-8")
    return {zone: len(by_zone[zone]) for zone in _PARA_ZONES}


def _refresh_navigation_impl(vault: Path) -> dict[str, Any]:
    brain_dir = vault / "brain"
    notes = _load_navigation_notes(vault)
    backlink_targets = _write_navigation_backlinks(brain_dir, notes)
    catalog_counts = _write_navigation_catalogs(brain_dir, notes)
    return {"backlink_targets": backlink_targets, "catalog_counts": catalog_counts}


from . import maintenance as _maintenance  # noqa: E402

_PARA_ZONES = _maintenance._PARA_ZONES
_WIKILINK_RE = _maintenance._WIKILINK_RE
