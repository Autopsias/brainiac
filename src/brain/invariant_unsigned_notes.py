"""Audit-chain coverage for indexed notes.

The corpus-invariant registry remains in ``brain.invariants``; this vertical
slice owns the audit-chain read and the indexed-note population walk. Missing
or unreadable chains are unavailable measurements, never counts of all notes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config


def _unavailable(chain_path: Path | None, error: str) -> dict[str, Any]:
    result: dict[str, Any] = {"value": None, "available": False, "error": error}
    if chain_path is not None:
        result["chain"] = str(chain_path)
    return result


def _resolve_chain_path(
    vault: Path, chain_path: Path | None,
) -> tuple[Path | None, dict[str, Any] | None]:
    if chain_path is not None:
        return Path(chain_path), None
    try:
        return Path(config.index_dir(vault)) / "audit_chain.jsonl", None
    except Exception as exc:  # noqa: BLE001 — an unresolvable index dir is a gap
        return None, _unavailable(None, f"{type(exc).__name__}: {exc}")


def _parse_audit_record(raw: str) -> tuple[str, Any] | None:
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("path"), str):
        return None
    return obj["path"], obj.get("verb")


def _read_signed_paths(
    chain_path: Path,
) -> tuple[set[str], int, dict[str, Any] | None]:
    if not chain_path.is_file():
        return set(), 0, _unavailable(
            chain_path, "no audit chain — cannot tell signed from unsigned")

    signed: set[str] = set()
    entries = 0
    try:
        with chain_path.open(encoding="utf-8") as fh:
            for raw in fh:
                record = _parse_audit_record(raw)
                if record is None:
                    continue
                path, verb = record
                entries += 1
                if verb in ("write", "ingest"):
                    signed.add(path)
                elif verb == "delete":
                    signed.discard(path)
    except OSError as exc:
        return set(), 0, _unavailable(chain_path, f"unreadable audit chain: {exc}")
    if not entries:
        return set(), 0, _unavailable(
            chain_path, "audit chain has no usable entries — cannot measure")
    return signed, entries, None


def _collect_unsigned_notes(
    conn: Any, vault: Path, signed: set[str], generated_maps: frozenset[str],
    entries: int, cap: int,
) -> dict[str, Any]:
    """Measure the indexed population after the audit chain is read."""
    unsigned: list[tuple[float, str, str]] = []
    population = 0
    by_zone: dict[str, int] = {}
    root = str(vault.resolve()).replace("\\", "/").rstrip("/") + "/"
    excluded_generated = 0
    for (path,) in conn.execute("SELECT path FROM notes"):
        raw_path = str(path or "").replace("\\", "/")
        if not raw_path:
            continue
        rel = raw_path[len(root):] if raw_path.startswith(root) else raw_path
        if rel.rsplit("/", 1)[-1] in generated_maps:
            excluded_generated += 1
            continue
        population += 1
        if rel in signed:
            continue
        zone = rel.split("/", 1)[0]
        by_zone[zone] = by_zone.get(zone, 0) + 1
        try:
            mtime = (vault / rel).stat().st_mtime
        except OSError:
            mtime = 0.0
        unsigned.append((mtime, zone, rel))

    unsigned.sort(reverse=True)
    return {
        "value": len(unsigned),
        "available": True,
        "population": population,
        "by_zone": by_zone,
        "chain_entries": entries,
        "excluded_generated_maps": excluded_generated,
        "basis": ("INDEXED notes (INT-03 scope, so raw/originals/ and every "
                  "other excluded tree is already out), minus the regenerated "
                  "maps, with no write/ingest entry in the audit chain"),
        "sample": [rel for _, _, rel in unsigned[:cap]],
    }


def unsigned_notes(conn: Any, vault: Path, *, cap: int = 10,
                   chain_path: Path | None = None) -> dict[str, Any]:
    """Return indexed notes with no surviving audit-chain entry."""
    from . import invariants

    resolved, error = _resolve_chain_path(vault, chain_path)
    if error is not None:
        return error
    assert resolved is not None
    signed, entries, error = _read_signed_paths(resolved)
    if error is not None:
        return error
    return _collect_unsigned_notes(
        conn, Path(vault), signed, invariants.GENERATED_MAP_BASENAMES, entries, cap)
