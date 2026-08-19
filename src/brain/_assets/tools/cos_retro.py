#!/usr/bin/env python3
"""COS retro miner — repeated corrections become one answerable question."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.modules.setdefault("cos_retro", sys.modules[__name__])

LEDGER_GLOBS = (
    "*cos_archive_ledger*.jsonl",
    "*cos_chip_ledger*.jsonl",
    "*cos_drafts_ledger*.jsonl",
    "*cos_draft_ledger*.jsonl",
    "*cos_hold_reconciliation*.jsonl",
)
VERDICT_GLOBS = ("*cos_verdicts*.jsonl",)
SENDER_HELD_RUNS = 3
REASON_OVERTURNED = 2
CHIP_REAPPLIED = 2
MAX_PROPOSALS = 5
SOURCE_PREFIX = "cos-retro"
DECLINE_MARK = "decline"
STATE_NAME = "cos-retro-state.json"
KNOWN_KEYS = frozenset({
    "sender", "thread_id", "conversation_id", "items", "groups", "reason",
    "held_reason", "action", "operation", "event", "run_id", "run", "verdict",
    "bucket", "state_after", "state_before", "disposition",
})

from cos_retro_scanners import (  # noqa: E402
    Scan,
    _collect_chip_events,
    _collect_holds,
    _iter_rows,
    _key_of,
    _managed_categories,
    _norm,
    _run_of,
    _thread_of,
    _ts_of,
    find_patterns,
    scan_vault,
)


def _short(text: str, n: int = 90) -> str:
    text = _norm(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def build_proposal(pattern: dict, today: str) -> dict:
    kind, who, n = pattern["type"], pattern["identity"], pattern["occurrences"]
    key = _key_of(SOURCE_PREFIX, kind, who)
    if kind == "sender-held-repeat":
        question = (f"COS held mail from “{_short(who, 60)}” in {n} separate runs. "
                    f"Standing rule for this sender?")
        options = [
            "always archive — add an auto-archive sender rule in overlay/cos/",
            "always hold — keep holding, this sender needs my eyes",
            "re-level — chip it P2 instead of holding",
            f"{DECLINE_MARK} — leave the current behaviour alone",
        ]
        default = f"{DECLINE_MARK} — leave the current behaviour alone"
        context = (f"pattern={kind} occurrences={n} runs="
                   f"{','.join(pattern['evidence'].get('runs', [])[:5])} "
                   f"top_reason={_short(pattern['evidence'].get('top_reason', ''))}")
    elif kind == "hold-reason-overturned":
        question = (f"The hold reason “{_short(who, 60)}” was overturned {n} times "
                    f"as a wrong hold. Change the screen?")
        options = [
            "relax it — stop holding on this signal alone",
            "keep it — the overturns were one-offs",
            "kernel proposal — emit a doctrine prompt so this is fixed with tests",
            f"{DECLINE_MARK} — leave the screen alone",
        ]
        default = "kernel proposal — emit a doctrine prompt so this is fixed with tests"
        context = (f"pattern={kind} occurrences={n} verdicts="
                   f"{','.join(pattern['evidence'].get('verdict_ids', [])[:5])}")
    else:
        question = (f"A chip was cleared and re-applied {n} times on conversation "
                    f"{_short(who, 40)}. Which state is right?")
        options = [
            "stay cleared — stop re-chipping this thread",
            "stay chipped — stop auto-clearing this thread",
            f"{DECLINE_MARK} — leave the lifecycle rules alone",
        ]
        default = f"{DECLINE_MARK} — leave the lifecycle rules alone"
        context = (f"pattern={kind} occurrences={n} "
                   f"chip_events={pattern['evidence'].get('events', 0)}")
    return {
        "key": f"{SOURCE_PREFIX}:{key}",
        "created": today,
        "source": f"{SOURCE_PREFIX}:{kind}",
        "question": question,
        "options": options,
        "default": default,
        "context": f"{context} · miner=tools/cos_retro.py (evidence only — "
                   f"no rule is applied until you answer)",
        "status": "open",
        "answer": None,
        "answered": None,
    }


def build_aggregate(patterns: list[dict], today: str) -> dict:
    kinds = sorted({p["type"] for p in patterns})
    key = _key_of(SOURCE_PREFIX, "aggregate", *kinds, str(len(patterns)))
    listing = "; ".join(
        f"{p['type']}:{_short(p['identity'], 40)}×{p['occurrences']}"
        for p in patterns[:12]
    )
    return {
        "key": f"{SOURCE_PREFIX}:{key}",
        "created": today,
        "source": f"{SOURCE_PREFIX}:aggregate",
        "question": (f"{len(patterns)} further recurring COS corrections were mined "
                     f"beyond this week's 4 individual questions. How should they "
                     f"be handled?"),
        "options": [
            "walk me through them next session — surface each in dialogue",
            "open a doctrine session — fix them together in the kernel with tests",
            f"{DECLINE_MARK} — not now, re-raise only if they get worse",
        ],
        "default": "walk me through them next session — surface each in dialogue",
        "context": f"patterns={listing} · miner=tools/cos_retro.py",
        "status": "open",
        "answer": None,
        "answered": None,
    }


def read_inbox(inbox: Path) -> tuple[set[str], dict[str, str]]:
    """Return unanswered keys and answered key-to-answer mappings."""
    open_keys: set[str] = set()
    answered: dict[str, str] = {}
    if not inbox.is_file():
        return open_keys, answered
    try:
        text = inbox.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return open_keys, answered
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key.startswith(SOURCE_PREFIX + ":"):
            continue
        if row.get("status") == "answered" or row.get("answer"):
            answered[key] = str(row.get("answer") or "")
        else:
            open_keys.add(key)
    return open_keys, answered


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("declined", {})
    state.setdefault("seen", {})
    return state


def select_proposals(
    patterns: list[dict],
    open_keys: set[str],
    answered: dict[str, str],
    state: dict,
    today: str,
    max_proposals: int = MAX_PROPOSALS,
) -> tuple[list[dict], int, int]:
    """Apply proposal idempotency, the cap, and overflow aggregation."""
    declined = state["declined"]
    seen = state["seen"]
    for key, answer in answered.items():
        if key not in declined and DECLINE_MARK in str(answer).casefold():
            declined[key] = {
                "occurrences_at_decline": int(seen.get(key, 0)),
                "declined_on": today,
            }

    eligible: list[tuple[dict, dict]] = []
    suppressed = 0
    for pattern in patterns:
        proposal = build_proposal(pattern, today)
        key = proposal["key"]
        if key in open_keys:
            suppressed += 1
            continue
        entry = declined.get(key)
        if entry is not None:
            floor = 2 * int(entry.get("occurrences_at_decline") or 0)
            if pattern["occurrences"] < max(floor, 1):
                suppressed += 1
                continue
            declined.pop(key, None)
        elif key in answered:
            suppressed += 1
            continue
        eligible.append((pattern, proposal))

    if len(eligible) <= max_proposals:
        chosen = [proposal for _pattern, proposal in eligible]
        overflow: list[dict] = []
    else:
        chosen = [proposal for _pattern, proposal in eligible[: max_proposals - 1]]
        overflow = [pattern for pattern, _proposal in eligible[max_proposals - 1:]]
        chosen.append(build_aggregate(overflow, today))

    for pattern, proposal in eligible:
        seen[proposal["key"]] = pattern["occurrences"]
    return chosen, suppressed, len(overflow)


def mine(
    vault: Path,
    today: str,
    dry_run: bool = False,
    max_proposals: int = MAX_PROPOSALS,
) -> dict:
    """Scan the vault and optionally append new owner questions."""
    cos_ops = vault / "cos-ops"
    memory = vault / ".brain" / "memory"
    inbox = memory / "inbox.jsonl"
    state_path = memory / STATE_NAME

    scan = scan_vault(cos_ops)
    readable = scan.lines_read - scan.lines_malformed
    patterns = find_patterns(scan) if readable > 0 else []
    open_keys, answered = read_inbox(inbox)
    state = load_state(state_path)
    proposals, suppressed, overflow = select_proposals(
        patterns, open_keys, answered, state, today, max_proposals,
    )

    if readable <= 0:
        status = "no-data"
    elif not patterns:
        status = "no-patterns"
    elif not proposals:
        status = "no-new-proposals"
    else:
        status = "proposals"

    written, summary_error, state_is_valid = 0, None, True
    if proposals and not dry_run:
        try:
            memory.mkdir(parents=True, exist_ok=True)
            with inbox.open("a", encoding="utf-8") as handle:
                for proposal in proposals:
                    handle.write(json.dumps(proposal, ensure_ascii=False) + "\n")
            written = len(proposals)
        except OSError as exc:
            status, summary_error, state_is_valid = "write-failed", str(exc), False

    if state_is_valid and not dry_run:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    return {
        "status": status,
        "vault": str(vault),
        "date": today,
        "files_matched": scan.files_matched,
        "files_unreadable": scan.files_unreadable,
        "lines_read": scan.lines_read,
        "lines_malformed": scan.lines_malformed,
        "rows_unknown_schema": scan.rows_unknown_schema,
        "patterns_found": len(patterns),
        "patterns_suppressed": suppressed,
        "proposals_written": written,
        "proposals": proposals,
        "aggregated": overflow,
        "error": summary_error,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true", help="mine and report, write nothing")
    ap.add_argument("--max-proposals", type=int, default=MAX_PROPOSALS)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args(argv)
    try:
        summary = mine(args.vault, args.date, args.dry_run, args.max_proposals)
    except Exception as exc:  # noqa: BLE001 — a miner NEVER takes the fold down
        summary = {"status": "miner-error", "error": repr(exc), "vault": str(args.vault)}
    loggable = {key: value for key, value in summary.items()
                if key not in ("proposals", "aggregated")}
    print(json.dumps(loggable, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
