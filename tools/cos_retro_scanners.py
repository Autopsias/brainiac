"""Classify patterns found in COS ledgers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from cos_retro import (
    CHIP_REAPPLIED,
    KNOWN_KEYS,
    LEDGER_GLOBS,
    REASON_OVERTURNED,
    SENDER_HELD_RUNS,
    VERDICT_GLOBS,
)

_HOLD_RE = re.compile(r"\b(?:hold|holds|held|holding)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _norm(text: object) -> str:
    return _WS_RE.sub(" ", str(text)).strip()


def _key_of(*parts: str) -> str:
    raw = "\x1f".join(_norm(part).casefold() for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _managed_categories(state: object) -> list[str] | None:
    """Read either supported managed-chip state shape."""
    if isinstance(state, list):
        return [str(item) for item in state]
    if isinstance(state, dict):
        for key in ("managed_categories", "categories"):
            value = state.get(key)
            if isinstance(value, list):
                return [str(item) for item in value]
    return None


def _thread_of(row: dict[str, object]) -> str | None:
    for key in ("thread_id", "conversation_id", "convid"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _run_of(row: dict[str, object], stem: str) -> str:
    for key in ("run_id", "run"):
        value = row.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return stem


def _ts_of(row: dict[str, object]) -> str:
    for key in ("action_ts", "ts", "verification_ts", "created", "captured"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class Scan:
    """Everything the miner learned from disk, plus skipped-input counts."""

    def __init__(self) -> None:
        self.files_matched = 0
        self.files_unreadable = 0
        self.lines_read = 0
        self.lines_malformed = 0
        self.rows_unknown_schema = 0
        self.holds: list[tuple[str, str, str]] = []
        self.chip_events: list[tuple[str, str, str]] = []
        self.verdicts: list[dict[str, object]] = []


def _iter_rows(path: Path, scan: Scan) -> Iterator[dict[str, object]]:
    """Yield valid JSON object rows while accounting for every skip."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        scan.files_unreadable += 1
        return
    scan.files_matched += 1
    with handle:
        while True:
            try:
                line = handle.readline()
            except (OSError, UnicodeError):
                scan.files_unreadable += 1
                return
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            scan.lines_read += 1
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                scan.lines_malformed += 1
                continue
            if not isinstance(row, dict):
                scan.lines_malformed += 1
                continue
            yield row


def _collect_holds(
    row: dict[str, object],
    stem: str,
    is_reconciliation: bool,
    out: list[tuple[str, str, str]],
) -> None:
    run = _run_of(row, stem)
    reason = row.get("held_reason") or row.get("reason") or ""
    action = str(row.get("action") or row.get("operation") or "")

    if is_reconciliation and isinstance(row.get("sender"), str):
        out.append((row["sender"], _norm(reason), run))
        return
    if "hold" in action.casefold():
        items = row.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("sender"), str):
                    out.append((item["sender"], _norm(reason), run))
        elif isinstance(row.get("sender"), str):
            out.append((row["sender"], _norm(reason), run))
        return
    disposition = str(row.get("disposition") or "")
    held = bool(row.get("held_reason")) or "held" in disposition.casefold() \
        or bool(reason and _HOLD_RE.search(str(reason)))
    if held and isinstance(row.get("sender"), str):
        out.append((row["sender"], _norm(reason), run))


def _classify_chip_transition(
    before: list[str] | None,
    after: list[str] | None,
    operation: str,
) -> str | None:
    if before is not None and before == after:
        return None
    cleared = "clear" in operation
    if after == [] and (before or cleared):
        return "clear"
    if after is None and cleared and before:
        return "clear"
    if after:
        return "apply"
    return None


def _append_chip_event(
    row: dict[str, object],
    timestamp: str,
    operation: str,
    out: list[tuple[str, str, str]],
) -> None:
    thread = _thread_of(row)
    before = _managed_categories(row.get("state_before"))
    after = _managed_categories(row.get("state_after"))
    if thread is not None and (before is not None or after is not None):
        kind = _classify_chip_transition(before, after, operation)
        if kind:
            out.append((thread, timestamp, kind))


def _collect_chip_events(
    row: dict[str, object], out: list[tuple[str, str, str]]
) -> None:
    """Collect chip clears and applications from row and item shapes."""
    timestamp = _ts_of(row)
    operation = str(row.get("operation") or row.get("action") or "").casefold()
    _append_chip_event(row, timestamp, operation, out)
    items = row.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            _append_chip_event(item, _ts_of(item) or timestamp, operation, out)


def _scan_ledger_file(path: Path, scan: Scan) -> None:
    """Read one hold or chip ledger into the shared scan."""
    stem = path.stem
    is_reconciliation = "hold_reconciliation" in path.name
    is_chip = "chip_ledger" in path.name
    for row in _iter_rows(path, scan):
        if not (KNOWN_KEYS & row.keys()):
            scan.rows_unknown_schema += 1
            continue
        _collect_holds(row, stem, is_reconciliation, scan.holds)
        if is_chip:
            _collect_chip_events(row, scan.chip_events)


def _scan_verdict_file(path: Path, scan: Scan) -> None:
    """Read one verdict ledger into the shared scan."""
    for row in _iter_rows(path, scan):
        if not isinstance(row.get("verdict"), str):
            scan.rows_unknown_schema += 1
            continue
        scan.verdicts.append(row)


def _scan_files(
    cos_ops: Path,
    patterns: tuple[str, ...],
    seen: set[Path],
    scan: Scan,
    reader: Callable[[Path, Scan], None],
) -> None:
    """Visit each matching file once and dispatch it to its reader."""
    for pattern in patterns:
        for path in sorted(cos_ops.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            reader(path, scan)


def scan_vault(cos_ops: Path) -> Scan:
    """Read all supported COS ledgers into one defensive scan."""
    scan = Scan()
    if not cos_ops.is_dir():
        return scan
    seen: set[Path] = set()
    _scan_files(cos_ops, LEDGER_GLOBS, seen, scan, _scan_ledger_file)
    _scan_files(cos_ops, VERDICT_GLOBS, seen, scan, _scan_verdict_file)
    return scan


def _sender_patterns(scan: Scan) -> list[dict[str, object]]:
    """Find senders held repeatedly across runs."""
    by_sender: dict[str, dict[str, object]] = {}
    for sender, reason, run in scan.holds:
        entry = by_sender.setdefault(
            _norm(sender).casefold(),
            {"display": _norm(sender), "runs": set(), "reasons": {}},
        )
        runs = entry["runs"]
        reasons = entry["reasons"]
        assert isinstance(runs, set)
        assert isinstance(reasons, dict)
        runs.add(run)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    found: list[dict[str, object]] = []
    for entry in by_sender.values():
        runs = entry["runs"]
        reasons = entry["reasons"]
        assert isinstance(runs, set)
        assert isinstance(reasons, dict)
        if len(runs) >= SENDER_HELD_RUNS:
            top = sorted(reasons.items(), key=lambda item: -item[1])[:1]
            found.append({
                "type": "sender-held-repeat",
                "identity": entry["display"],
                "occurrences": len(runs),
                "evidence": {"runs": sorted(runs)[:8],
                             "top_reason": top[0][0] if top else ""},
            })
    return found


def _reason_patterns(scan: Scan) -> list[dict[str, object]]:
    """Find hold reasons that are repeatedly overturned."""
    by_reason: dict[str, dict[str, object]] = {}
    for verdict in scan.verdicts:
        if str(verdict.get("verdict")) != "wrong-hold":
            continue
        reason = _norm(verdict.get("held_reason") or "")
        if not reason:
            continue
        entry = by_reason.setdefault(
            reason.casefold(), {"display": reason, "n": 0, "ids": []}
        )
        entry["n"] = int(entry["n"]) + 1
        ids = entry["ids"]
        assert isinstance(ids, list)
        ids.append(str(verdict.get("id") or ""))
    found: list[dict[str, object]] = []
    for entry in by_reason.values():
        if int(entry["n"]) >= REASON_OVERTURNED:
            ids = entry["ids"]
            assert isinstance(ids, list)
            found.append({
                "type": "hold-reason-overturned",
                "identity": entry["display"],
                "occurrences": entry["n"],
                "evidence": {"verdict_ids": ids[:8]},
            })
    return found


def _chip_patterns(scan: Scan) -> list[dict[str, object]]:
    """Find chip categories re-applied after being cleared."""
    by_thread: dict[str, list[tuple[str, str]]] = {}
    for thread, timestamp, kind in scan.chip_events:
        by_thread.setdefault(thread, []).append((timestamp, kind))
    found: list[dict[str, object]] = []
    for thread, events in by_thread.items():
        events.sort(key=lambda event: event[0])
        reapplied, cleared = 0, False
        for _timestamp, kind in events:
            if kind == "clear":
                cleared = True
            elif kind == "apply" and cleared:
                reapplied += 1
                cleared = False
        if reapplied >= CHIP_REAPPLIED:
            found.append({
                "type": "chip-reapplied-after-clear",
                "identity": thread,
                "occurrences": reapplied,
                "evidence": {"events": len(events)},
            })
    return found


def find_patterns(scan: Scan) -> list[dict[str, object]]:
    """Find repeated holds, overturned reasons, and re-applied chips."""
    found = _sender_patterns(scan) + _reason_patterns(scan) + _chip_patterns(scan)
    found.sort(key=lambda pattern: (
        -int(pattern["occurrences"]),
        str(pattern["type"]),
        str(pattern["identity"]),
    ))
    return found


__all__ = [
    "Scan",
    "_collect_chip_events",
    "_collect_holds",
    "_iter_rows",
    "_key_of",
    "_managed_categories",
    "_norm",
    "_run_of",
    "_thread_of",
    "_ts_of",
    "find_patterns",
    "scan_vault",
]
