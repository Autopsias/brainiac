#!/usr/bin/env python3
"""Durable state and run-ledger records for a chained COS sign-in session.

The mailbox enumeration is the backlog census.  ``select`` keeps that full
census as evidence, then projects the oldest conversations that have not yet
been judged in this sign-in into the bounded model population.  ``record``
only adds a conversation to the judged set after the run ledger says
``judgment_pending: false``; an absent or unreadable ledger never means that a
thread was judged.

State is deliberately per invocation/sign-in, persists between child runs,
and lives under the engine's proven host-private base because it controls the
model population.  The append-only ``_cos_batch_ledger.jsonl`` is the S06
producer that S07 will read for ``door_check`` and ``batch_stop``.  It is operational output
under ``cos-ops`` and is never a sheet or indexed knowledge.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from brain import config  # noqa: E402
from brain.cos.feedback_sheet import (  # noqa: E402
    BATCH_LEDGER_SCHEMA as LEDGER_SCHEMA,
    DOOR_VERDICTS as SHEET_DOOR_VERDICTS,
    STOP_REASONS as SHEET_STOP_REASONS,
)

SCHEMA = "cos-batch-session/1"
DOOR_VERDICTS = set(SHEET_DOOR_VERDICTS)
STOP_REASONS = set(SHEET_STOP_REASONS)
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> list[dict[str, Any]]:
    """Read a JSON enumeration or JSONL ledger, refusing malformed input."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        obj = json.loads(text)
        rows = obj.get("rows") if isinstance(obj, dict) else obj
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path} does not contain a rows list")
    seen: set[str] = set()
    for row in rows:
        cid = row.get("conversation_id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"{path} contains a row without conversation_id")
        if cid in seen:
            raise ValueError(f"{path} repeats conversation_id {cid!r}")
        seen.add(cid)
    return rows


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def session_root(vault: Path, session_id: str) -> Path:
    if not _SAFE_ID.fullmatch(session_id):
        raise ValueError("session id must be 1-128 safe characters")
    base = config.proven_off_mount(
        config.host_private_base() / "cos-batch-sessions",
        vault,
        what="COS chained-session control state",
    )
    return base / config.vault_slug8(vault) / session_id


def _ensure_private_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # The state contains conversation ids and controls which threads are sent
    # to the model.  Neither it nor its directory names belong on the VM mount
    # or under a process-default 0755 directory.
    for directory in (root.parent.parent, root.parent, root):
        config.secure_file_permissions(directory, 0o700)


def init_state(vault: Path, session_id: str, batches: int, thread_cap: int) -> Path:
    if batches < 1 or thread_cap < 1:
        raise ValueError("batches and thread cap must both be positive")
    root = session_root(vault, session_id)
    _ensure_private_root(root)
    state_path = root / "state.json"
    if state_path.exists():
        state = _read_json(state_path)
        if (state.get("batches_requested"), state.get("thread_cap")) != (
            batches,
            thread_cap,
        ):
            raise ValueError("an existing session has different batch controls")
        return state_path
    state = {
        "schema": SCHEMA,
        "session_id": session_id,
        "started_at": _now(),
        "batches_requested": batches,
        "thread_cap": thread_cap,
        "judged": {},
        "run_ids": [],
        "door_checks": [],
    }
    _atomic_json(state_path, state)
    return state_path


def _state(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} state")
    if not isinstance(value.get("judged"), dict):
        raise ValueError(f"{path} has no judged mapping")
    return value


def _received_key(row: dict[str, Any]) -> tuple[str, str]:
    # ISO timestamps sort lexically. Missing timestamps sort first because the
    # conservative choice is to surface an age-unknown thread, not starve it.
    return (str(row.get("received") or ""), str(row["conversation_id"]))


def select(
    state_path: Path,
    enumeration_path: Path,
    selected_path: Path,
    excluded_path: Path,
    run_id: str,
) -> dict[str, Any]:
    state = _state(state_path)
    source = _read_json(enumeration_path)
    rows = _rows(enumeration_path)  # raises on unreadable/missing input
    already = set(state["judged"])
    eligible = sorted(
        (row for row in rows if row["conversation_id"] not in already),
        key=_received_key,
    )
    chosen = eligible[: int(state["thread_cap"])]
    chosen_ids = {row["conversation_id"] for row in chosen}
    excluded = [row["conversation_id"] for row in rows if row["conversation_id"] not in chosen_ids]
    selected = dict(source) if isinstance(source, dict) else {}
    selected["rows"] = chosen
    selected["batch_thread_cap"] = int(state["thread_cap"])
    selected["full_population"] = len(rows)
    selected["already_judged_this_sign_in"] = len(already & {r["conversation_id"] for r in rows})
    selected["selected_run_id"] = run_id
    _atomic_json(selected_path, selected)
    _atomic_json(excluded_path, excluded)
    if run_id not in state["run_ids"]:
        state["run_ids"].append(run_id)
    state["last_enumeration"] = str(enumeration_path)
    state["last_selection"] = str(selected_path)
    state["last_selection_run_id"] = run_id
    state["last_selected_count"] = len(chosen)
    _atomic_json(state_path, state)
    return {
        "run_id": run_id,
        "thread_cap": int(state["thread_cap"]),
        "full_population": len(rows),
        "already_judged": len(already & {r["conversation_id"] for r in rows}),
        "selected": len(chosen),
        "remaining_before": len(eligible),
        "excluded": len(excluded),
    }


def record(
    state_path: Path,
    run_id: str,
    selected_path: Path | None,
    ledger_path: Path | None,
) -> dict[str, Any]:
    """Record only ledger-proven judgments; missing input is never success."""
    state = _state(state_path)
    if run_id and run_id != "not-started" and run_id not in state["run_ids"]:
        state["run_ids"].append(run_id)
        _atomic_json(state_path, state)
    prior_selected = (int(state.get("last_selected_count") or 0)
                      if state.get("last_selection_run_id") == run_id else 0)
    if not selected_path or not selected_path.exists():
        return {"run_id": run_id, "selected": prior_selected, "judged_added": 0,
                "unreconciled": prior_selected, "recorded": False,
                "why": "selection-not-produced"}
    try:
        selected_ids = {row["conversation_id"] for row in _rows(selected_path)}
    except (OSError, ValueError) as exc:
        return {"run_id": run_id, "selected": prior_selected, "judged_added": 0,
                "unreconciled": prior_selected, "recorded": False,
                "why": f"selection-unreadable: {type(exc).__name__}"}
    if not ledger_path or not ledger_path.exists():
        return {"run_id": run_id, "selected": len(selected_ids), "judged_added": 0,
                "unreconciled": len(selected_ids), "recorded": False,
                "why": "judgment-ledger-not-produced"}
    try:
        ledger_rows = _rows(ledger_path)
    except (OSError, ValueError) as exc:
        return {"run_id": run_id, "selected": len(selected_ids),
                "judged_added": 0, "unreconciled": len(selected_ids),
                "recorded": False,
                "why": f"judgment-ledger-unreadable: {type(exc).__name__}"}
    if selected_ids and not ledger_rows:
        return {"run_id": run_id, "selected": len(selected_ids),
                "judged_added": 0, "unreconciled": len(selected_ids),
                "recorded": False, "why": "judgment-ledger-unreadable: empty"}
    judged_ids = {
        row["conversation_id"]
        for row in ledger_rows
        if row["conversation_id"] in selected_ids
        and row.get("judgment_pending") is False
    }
    added = 0
    for cid in sorted(judged_ids):
        if cid not in state["judged"]:
            state["judged"][cid] = run_id
            added += 1
    _atomic_json(state_path, state)
    return {
        "run_id": run_id,
        "selected": len(selected_ids),
        "judged_added": added,
        "judged_total": len(state["judged"]),
        "unreconciled": len(selected_ids - judged_ids),
        "recorded": True,
    }


def remaining(state_path: Path, enumeration_path: Path | None) -> dict[str, Any]:
    state = _state(state_path)
    if not enumeration_path or not enumeration_path.exists():
        return {"remaining": None, "population": None, "why": "enumeration-not-produced"}
    ids = {row["conversation_id"] for row in _rows(enumeration_path)}
    judged = set(state["judged"])
    return {"remaining": len(ids - judged), "population": len(ids),
            "judged_in_population": len(ids & judged)}


def record_door(state_path: Path, path: Path, command_rc: int = 0) -> dict[str, Any]:
    state = _state(state_path)
    value = _read_json(path)
    door = value.get("door_check") if isinstance(value, dict) else None
    if not isinstance(door, dict):
        raise ValueError("door result has no door_check mapping")
    verdict = door.get("verdict")
    if verdict not in DOOR_VERDICTS:
        raise ValueError(f"door verdict must be one of {sorted(DOOR_VERDICTS)}")
    if verdict == "open" and command_rc != 0:
        door = dict(door, verdict="closed",
                    failure=f"door check command exited {command_rc}")
        verdict = "closed"
    if not str(door.get("lane") or "").strip() or not str(door.get("toolset") or "").strip():
        raise ValueError("door check must name both lane and toolset")
    if verdict == "open":
        remaining_s = door.get("remaining_validity_seconds")
        required_s = door.get("required_validity_seconds")
        numeric = lambda value: (type(value) in (int, float)  # noqa: E731
                                 and math.isfinite(float(value)))
        if not numeric(remaining_s) or not numeric(required_s):
            raise ValueError("an open door needs measured remaining and required validity")
        if required_s <= 0 or remaining_s < required_s:
            raise ValueError("door claimed open without enough remaining validity")
    door = dict(door, checked_at=door.get("checked_at") or _now())
    state["door_checks"].append(door)
    state["last_door_check"] = door
    _atomic_json(state_path, state)
    return door


def finish(
    state_path: Path,
    vault: Path,
    reason: str,
    status: str,
    unreconciled: int,
    stale_before: int,
    stale_after: int,
) -> dict[str, Any]:
    if reason not in STOP_REASONS:
        raise ValueError(f"stop reason must be one of {sorted(STOP_REASONS)}")
    state = _state(state_path)
    door = state.get("last_door_check") or {
        "verdict": "closed",
        "lane": "not-reached",
        "toolset": "not-reached",
    }
    row = {
        "schema": LEDGER_SCHEMA,
        "session_id": state["session_id"],
        "started_at": state["started_at"],
        "finished_at": _now(),
        "status": status,
        "run_ids": list(state["run_ids"]),
        "batches_requested": state["batches_requested"],
        "batches_run": len(state["run_ids"]),
        "thread_cap": state["thread_cap"],
        "judged_conversations": len(state["judged"]),
        "door_check": door,
        "batch_stop": {"reason": reason, "unreconciled_threads": max(0, unreconciled)},
        "stale_open_recovery": {
            "before": max(0, stale_before),
            "after": max(0, stale_after),
            "reconciled": max(0, stale_before - stale_after),
        },
    }
    ledger = vault / "cos-ops" / "_cos_batch_ledger.jsonl"
    _append_jsonl(ledger, row)
    state["finished"] = row
    _atomic_json(state_path, state)
    return {"ledger": str(ledger), **row}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("init")
    q.add_argument("--vault", type=Path, required=True)
    q.add_argument("--session-id", required=True)
    q.add_argument("--batches", type=int, required=True)
    q.add_argument("--thread-cap", type=int, required=True)
    q = sub.add_parser("select")
    q.add_argument("--state", type=Path, required=True)
    q.add_argument("--enumeration", type=Path, required=True)
    q.add_argument("--selected", type=Path, required=True)
    q.add_argument("--excluded", type=Path, required=True)
    q.add_argument("--run-id", required=True)
    q = sub.add_parser("record")
    q.add_argument("--state", type=Path, required=True)
    q.add_argument("--run-id", required=True)
    q.add_argument("--selected", type=Path)
    q.add_argument("--ledger", type=Path)
    q = sub.add_parser("remaining")
    q.add_argument("--state", type=Path, required=True)
    q.add_argument("--enumeration", type=Path)
    q = sub.add_parser("door")
    q.add_argument("--state", type=Path, required=True)
    q.add_argument("--input", type=Path, required=True)
    q.add_argument("--command-rc", type=int, default=0)
    q = sub.add_parser("finish")
    q.add_argument("--state", type=Path, required=True)
    q.add_argument("--vault", type=Path, required=True)
    q.add_argument("--reason", required=True)
    q.add_argument("--status", required=True)
    q.add_argument("--unreconciled", type=int, default=0)
    q.add_argument("--stale-before", type=int, default=0)
    q.add_argument("--stale-after", type=int, default=0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        out: Any = {"state": str(init_state(args.vault, args.session_id, args.batches, args.thread_cap))}
    elif args.command == "select":
        out = select(args.state, args.enumeration, args.selected, args.excluded, args.run_id)
    elif args.command == "record":
        out = record(args.state, args.run_id, args.selected, args.ledger)
    elif args.command == "remaining":
        out = remaining(args.state, args.enumeration)
    elif args.command == "door":
        out = record_door(args.state, args.input, args.command_rc)
    else:
        out = finish(args.state, args.vault, args.reason, args.status,
                     args.unreconciled, args.stale_before, args.stale_after)
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, config.HostPathUnsafe) as exc:
        print(f"cos-batch-session refused: {exc}", file=sys.stderr)
        raise SystemExit(2)
