"""Exact, duplicate-safe selection manifest for COS draft discard.

The manifest is a durable projection of the existing undo ledgers.  It never
authorizes a row by itself: the discard pass rebuilds the same projection and
requires the stable selection digest to match before opening the browser.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable


DISCARD_MANIFEST_SCHEMA = "cos_draft_discard_manifest/v1"


def identity_key(run_id: str, conversation_id: str, item_id: str,
                 signature: str) -> str:
    material = "\0".join((run_id, conversation_id, item_id, signature))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_digest(row: dict[str, Any]) -> str:
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry(candidate: dict[str, Any], run_id: str, *, lane
           ) -> dict[str, Any]:
    row = candidate["row"]
    item_identity = identity_key(
        run_id, candidate["conversation_id"], candidate["item_id"],
        candidate["signature"])
    row_identity = _row_digest(row)
    return {
        "identity_key": item_identity,
        # G_R2 forbids persisting a stale EWS ChangeKey. This binds the stable
        # saved-item identity to the exact append-only save transition instead;
        # the page still re-fetches the live ChangeKey immediately pre-delete.
        "item_change_identity": hashlib.sha256(
            f"{item_identity}\0{row_identity}".encode("utf-8")).hexdigest(),
        "source_run": run_id,
        "conversation_id": candidate["conversation_id"],
        "conversation_id_digest": lane.short(candidate["conversation_id"]),
        "draft_item_id": candidate["item_id"],
        "draft_item_id_digest": lane.short(candidate["item_id"]),
        "signature": candidate["signature"],
        "signature_source": candidate["signature_source"],
        "action_ts": str(row.get("action_ts") or ""),
        "ledger_ts": str(row.get("ts") or ""),
        "source_row_digest": row_identity,
    }


def _protected_entry(row: dict[str, Any], run_id: str, *, lane
                     ) -> dict[str, Any] | None:
    """Represent a physical manual item for survivor selection, not deletion."""
    cid = row.get("conversation_id")
    item_id = row.get("new_item_id")
    if not isinstance(cid, str) or not cid \
            or not isinstance(item_id, str) or not item_id:
        return None
    signature = str(row.get("signature") or lane.draft_signature(run_id, cid))
    return _entry({"row": row, "conversation_id": cid,
                   "item_id": item_id, "signature": signature,
                   "signature_source": "manual-protected"}, run_id, lane=lane)


def _occurrences(vault: Path, *, lane, forward_candidate: Callable,
                 manual_projector: Callable
                 ) -> tuple[dict[str, list[dict[str, Any]]],
                            list[dict[str, Any]]]:
    from brain import cos                                      # noqa: PLC0415

    by_item: dict[str, list[dict[str, Any]]] = {}
    manual: list[dict[str, Any]] = []
    prefix, suffix = "_cos_undo_ledger_", ".jsonl"
    for path in sorted(cos.run_ops_dir(vault).glob(f"{prefix}*{suffix}")):
        run_id = path.name[len(prefix):-len(suffix)]
        rows = [row for row in lane.UndoLedger(vault, run_id).rows()
                if row.get("verb") == "draft"]
        outcome_keys = {row.get("idempotency_key") for row in rows
                        if row.get("state") != "intent"}
        for row in rows:
            if row.get("state") == "intent" \
                    and row.get("idempotency_key") in outcome_keys:
                continue
            candidate, reason = forward_candidate(row, run_id, lane=lane)
            if candidate is not None:
                entry = _entry(candidate, run_id, lane=lane)
                entry["_eligible"] = True
            else:
                manual.append(dict(
                    manual_projector(row, str(reason), lane=lane),
                    source_run=run_id))
                entry = _protected_entry(row, run_id, lane=lane)
                if entry is None:
                    continue
                entry["_eligible"] = False
                entry["manual_reason"] = str(reason)
            by_item.setdefault(entry["draft_item_id"], []).append(entry)
    return by_item, manual


def _newest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda row: (
        row["action_ts"], row["ledger_ts"], row["source_run"],
        row["identity_key"]))


def selection_digest(doc: dict[str, Any]) -> str:
    stable = {key: doc.get(key) for key in (
        "schema", "source_run", "selection_policy", "entries", "survivors",
        "manual", "mixed", "counts")}
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build(vault: Path, source_run: str, *, lane,
          forward_candidate: Callable,
          manual_projector: Callable) -> dict[str, Any]:
    """Build the exact duplicate set on the threads ``source_run`` touched.

    ``source_run`` SELECTS THE THREADS; it no longer filters the losers
    (owner ruling 2026-08-28: "Each thread should only have one draft based on
    latest information"). Until then a loser was admitted only when the SAME
    run wrote it, so a run that superseded last week's draft selected nothing
    and the thread kept both. On the threads this run touched, every eligible
    item older than the survivor is now selected, whichever run wrote it.

    Nothing else widened. An item still enters only through
    ``forward_candidate`` — this lane's own reconciled, verified, signed save —
    so a draft the owner wrote is not selectable at any age. Manual and mixed
    claims remain visible evidence and are never treated as the survivor that
    makes a delete safe: the thread keeps its newest independently verified
    eligible item too.
    """
    lane.assert_vault(vault)
    if not isinstance(source_run, str) or not source_run.strip():
        raise lane.MutationStop(
            "draft-discard manifest requires one named source run")
    by_item, manual = _occurrences(
        vault, lane=lane, forward_candidate=forward_candidate,
        manual_projector=manual_projector)
    target_threads = {
        row["conversation_id"]
        for claims in by_item.values() for row in claims
        if row["source_run"] == source_run
    }
    manual = [row for row in manual if row["source_run"] == source_run]
    distinct: list[dict[str, Any]] = []
    mixed: list[dict[str, Any]] = []
    for item_id, claims in by_item.items():
        if not any(row["conversation_id"] in target_threads for row in claims):
            continue
        identities = {(row["conversation_id"], row["signature"])
                      for row in claims}
        eligibility = {row["_eligible"] for row in claims}
        item = _newest(claims)
        if len(identities) != 1 or len(eligibility) != 1:
            mixed.append({
                "draft_item_id": lane.short(item_id),
                "source_runs": sorted({row["source_run"] for row in claims}),
                "reason": "one item has mixed or conflicting ledger claims",
            })
            continue
        if item["_eligible"]:
            distinct.append(item)

    by_thread: dict[str, list[dict[str, Any]]] = {}
    for entry in distinct:
        by_thread.setdefault(entry["conversation_id"], []).append(entry)
    selected: list[dict[str, Any]] = []
    survivors: list[dict[str, Any]] = []
    for conversation_id in sorted(target_threads):
        rows = by_thread.get(conversation_id, [])
        if not rows:
            continue
        ordered = sorted(rows, key=lambda row: (
            row["action_ts"], row["ledger_ts"], row["source_run"],
            row["identity_key"]))
        survivor = dict(ordered[-1])
        survivor.pop("_eligible")
        survivor["disposition"] = "survivor-newest-eligible"
        survivors.append(survivor)
        for row in ordered[:-1]:
            selected.append({
                key: value for key, value in dict(
                    row,
                    survivor_identity_key=survivor["identity_key"]).items()
                if not key.startswith("_")})

    selected.sort(key=lambda row: (row["conversation_id"], row["action_ts"],
                                   row["identity_key"]))
    survivors.sort(key=lambda row: (row["conversation_id"],
                                    row["identity_key"]))
    doc = {
        "schema": DISCARD_MANIFEST_SCHEMA,
        "source_run": source_run,
        "built_at": lane._ts(),
        "selection_policy": "newest-eligible-item-survives-per-thread",
        "entries": selected,
        "survivors": survivors,
        "manual": manual,
        "mixed": mixed,
        "counts": {
            "ledger_items": len(distinct),
            "threads": len(target_threads),
            "selected": len(selected),
            "survivors": len(survivors),
            "manual": len(manual),
            "mixed": len(mixed),
        },
    }
    doc["manifest_digest"] = selection_digest(doc)
    return doc


def load_verified(path: Path, vault: Path, *, lane,
                  source_run: str,
                  forward_candidate: Callable,
                  manual_projector: Callable) -> dict[str, Any]:
    """Read one manifest and prove it is still the exact current selection."""
    from brain import cos                                      # noqa: PLC0415

    try:
        raw = cos._read_nofollow(path, max_bytes=5 * 1024 * 1024)
        doc = json.loads(raw.decode("utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        raise lane.MutationStop(
            f"draft-discard manifest {path} is unreadable: {type(exc).__name__}") \
            from None
    if not isinstance(doc, dict) or doc.get("schema") != DISCARD_MANIFEST_SCHEMA:
        raise lane.MutationStop(
            f"draft-discard manifest must be {DISCARD_MANIFEST_SCHEMA}")
    if doc.get("source_run") != source_run:
        raise lane.MutationStop(
            "draft-discard manifest is not bound to this named source run")
    recorded = doc.get("manifest_digest")
    if not isinstance(recorded, str) or recorded != selection_digest(doc):
        raise lane.MutationStop(
            "draft-discard manifest digest does not bind its exact item set")
    current = build(vault, source_run, lane=lane,
                    forward_candidate=forward_candidate,
                    manual_projector=manual_projector)
    if recorded != current["manifest_digest"]:
        raise lane.MutationStop(
            "draft ledgers changed after the manifest was built; rebuild it "
            "instead of applying a stale or partial selection")
    return doc
