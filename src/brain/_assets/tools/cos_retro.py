#!/usr/bin/env python3
"""COS retro miner — repeated corrections become one answerable question.

WHY THIS EXISTS (FL-03). A month of hand-fixing the nightly's holds proved the
run records already carry the patterns: the same sender held three nights
running, the same hold reason overturned every time the owner looked at it, a
chip cleared and silently re-applied. Nobody was reading them back. This miner
does the reading — pure python, no model call, no new scheduled task — and
turns each recurring pattern into ONE decidable owner question with enumerated
options and a stated default, appended to the vault's owner inbox.

It is invoked from the HOST side of the weekly synthesis fold
(`scripts/brain-synthesis.sh`). The model session inside that fold runs in a
fail-closed sandbox with Bash denied, so the wrapper — not the model — runs it.

ROBUSTNESS IS THE FEATURE. This runs inside the weekly fold, so it NEVER
raises on bad input: a malformed or truncated line is skipped and counted, an
unreadable file is skipped and counted, a row whose field names come from an
older kernel is counted separately as `unknown_schema`. A miner that raises
takes the whole fold down with it.

It also reports `no-data` (nothing readable at all) DISTINCTLY from
`no-patterns` (read fine, nothing met threshold). A silent zero that conflates
the two is indistinguishable from health.

    python3 tools/cos_retro.py --vault <vault-root>
    python3 tools/cos_retro.py --vault <vault-root> --dry-run   # write nothing

ponytail: stdlib only, one file, one JSON summary line on stdout.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

# --- ledger discovery -------------------------------------------------------
# VERIFIED against a real cos-ops dir (2026-07-26): TWO naming families coexist
# — the older `_cos_*` (leading underscore) and the newer sweep-era `cos_*`
# (none), plus `cos_chip_ledger-sweep.jsonl` with no separator before the
# suffix and `cos_archive_ledger_2026–07–25-drain.jsonl` with EN-DASH dates.
# Leading-`*` globs absorb all of it. The draft ledger is PLURAL
# (`_cos_drafts_ledger_*`); the singular spelling is kept for back-compat.
LEDGER_GLOBS = (
    "*cos_archive_ledger*.jsonl",
    "*cos_chip_ledger*.jsonl",
    "*cos_drafts_ledger*.jsonl",
    "*cos_draft_ledger*.jsonl",
    "*cos_hold_reconciliation*.jsonl",
)
VERDICT_GLOBS = ("*cos_verdicts*.jsonl",)

# Thresholds (FL-03). Deliberately blunt: a pattern the owner corrected three
# times is worth one question; anything rarer is noise.
SENDER_HELD_RUNS = 3
REASON_OVERTURNED = 2
CHIP_REAPPLIED = 2
MAX_PROPOSALS = 5

SOURCE_PREFIX = "cos-retro"
DECLINE_MARK = "decline"
STATE_NAME = "cos-retro-state.json"

_HOLD_RE = re.compile(r"\b(?:hold|holds|held|holding)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

# Any of these keys means "this row is a shape we recognise". A parsed row with
# none of them came from a kernel we do not know and is counted separately —
# never silently folded into the readable-but-boring pile.
KNOWN_KEYS = frozenset({
    "sender", "thread_id", "conversation_id", "items", "groups", "reason",
    "held_reason", "action", "operation", "event", "run_id", "run", "verdict",
    "bucket", "state_after", "state_before", "disposition",
})


# --- tiny helpers -----------------------------------------------------------
def _norm(text: str) -> str:
    return _WS_RE.sub(" ", str(text)).strip()


def _key_of(*parts: str) -> str:
    raw = "\x1f".join(_norm(p).casefold() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _managed_categories(state):
    """Chip state comes in TWO real shapes: a bare list of category names, and
    a dict carrying `managed_categories`. Returns None when neither."""
    if isinstance(state, list):
        return [str(x) for x in state]
    if isinstance(state, dict):
        for k in ("managed_categories", "categories"):
            v = state.get(k)
            if isinstance(v, list):
                return [str(x) for x in v]
    return None


def _thread_of(row: dict):
    for k in ("thread_id", "conversation_id", "convid"):
        v = row.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _run_of(row: dict, stem: str) -> str:
    for k in ("run_id", "run"):
        v = row.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)
    return stem  # the run identity is in the FILENAME on older ledgers


def _ts_of(row: dict) -> str:
    for k in ("action_ts", "ts", "verification_ts", "created", "captured"):
        v = row.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


# --- reading ----------------------------------------------------------------
class Scan:
    """Everything the miner learned from disk, plus how much it had to skip."""

    def __init__(self):
        self.files_matched = 0
        self.files_unreadable = 0
        self.lines_read = 0
        self.lines_malformed = 0
        self.rows_unknown_schema = 0
        self.holds = []        # (sender, reason, run)
        self.chip_events = []  # (thread, ts, "clear"|"apply")
        self.verdicts = []     # dicts


def _iter_rows(path: Path, scan: Scan):
    """Yield parsed dict rows. A truncated final line, a blob of prose, an
    unreadable file — all skipped and counted, never raised."""
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


def _collect_holds(row: dict, stem: str, is_reconciliation: bool, out: list):
    run = _run_of(row, stem)
    reason = row.get("held_reason") or row.get("reason") or ""
    action = str(row.get("action") or row.get("operation") or "")

    # 1) a hold-reconciliation file is a hold inventory: every row is a hold.
    if is_reconciliation and isinstance(row.get("sender"), str):
        out.append((row["sender"], _norm(reason), run))
        return
    # 2) an explicit hold-label chip write, senders inside `items[]`.
    if "hold" in action.casefold():
        items = row.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("sender"), str):
                    out.append((item["sender"], _norm(reason), run))
        elif isinstance(row.get("sender"), str):
            out.append((row["sender"], _norm(reason), run))
        return
    # 3) an ordinary ledger row whose disposition/reason says it was held.
    disposition = str(row.get("disposition") or "")
    held = bool(row.get("held_reason")) or "held" in disposition.casefold() \
        or bool(reason and _HOLD_RE.search(str(reason)))
    if held and isinstance(row.get("sender"), str):
        out.append((row["sender"], _norm(reason), run))


def _collect_chip_events(row: dict, out: list):
    thread = _thread_of(row)
    ts = _ts_of(row)
    op = str(row.get("operation") or row.get("action") or "").casefold()
    before = _managed_categories(row.get("state_before"))
    after = _managed_categories(row.get("state_after"))

    def classify(before_, after_):
        # An unchanged state is a FAILED write, not a lifecycle event. Real
        # ledgers carry `clear_managed_chip` rows whose state_after equals
        # state_before ("clear attempted but row held") — counting those as
        # re-applications would manufacture the very pattern we mine for.
        if before_ is not None and before_ == after_:
            return None
        cleared = "clear" in op
        if after_ == [] and (before_ or cleared):
            return "clear"
        if after_ is None and cleared and before_:
            return "clear"
        if after_:
            return "apply"
        return None

    if thread is not None and (before is not None or after is not None):
        kind = classify(before, after)
        if kind:
            out.append((thread, ts, kind))

    items = row.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            t = _thread_of(item)
            if t is None:
                continue
            kind = classify(_managed_categories(item.get("state_before")),
                            _managed_categories(item.get("state_after")))
            if kind:
                out.append((t, _ts_of(item) or ts, kind))


def scan_vault(cos_ops: Path) -> Scan:
    scan = Scan()
    if not cos_ops.is_dir():
        return scan

    seen = set()
    for pattern in LEDGER_GLOBS:
        for path in sorted(cos_ops.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            stem = path.stem
            is_recon = "hold_reconciliation" in path.name
            is_chip = "chip_ledger" in path.name
            for row in _iter_rows(path, scan):
                if not (KNOWN_KEYS & row.keys()):
                    scan.rows_unknown_schema += 1
                    continue
                _collect_holds(row, stem, is_recon, scan.holds)
                if is_chip:
                    _collect_chip_events(row, scan.chip_events)

    for pattern in VERDICT_GLOBS:
        for path in sorted(cos_ops.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            for row in _iter_rows(path, scan):
                if not isinstance(row.get("verdict"), str):
                    scan.rows_unknown_schema += 1
                    continue
                scan.verdicts.append(row)
    return scan


# --- patterns ---------------------------------------------------------------
def find_patterns(scan: Scan) -> list:
    """Each pattern is a dict: {type, identity, occurrences, evidence}."""
    found = []

    # (1) same sender held across >= N DISTINCT runs.
    by_sender = {}
    for sender, reason, run in scan.holds:
        entry = by_sender.setdefault(_norm(sender).casefold(),
                                     {"display": _norm(sender), "runs": set(),
                                      "reasons": {}})
        entry["runs"].add(run)
        if reason:
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
    for entry in by_sender.values():
        if len(entry["runs"]) >= SENDER_HELD_RUNS:
            top = sorted(entry["reasons"].items(), key=lambda kv: -kv[1])[:1]
            found.append({
                "type": "sender-held-repeat",
                "identity": entry["display"],
                "occurrences": len(entry["runs"]),
                "evidence": {"runs": sorted(entry["runs"])[:8],
                             "top_reason": top[0][0] if top else ""},
            })

    # (2) same hold reason overturned by >= N `wrong-hold` verdicts.
    by_reason = {}
    for v in scan.verdicts:
        if str(v.get("verdict")) != "wrong-hold":
            continue
        reason = _norm(v.get("held_reason") or "")
        if not reason:
            continue
        by_reason.setdefault(reason.casefold(), {"display": reason, "n": 0,
                                                 "ids": []})
        by_reason[reason.casefold()]["n"] += 1
        by_reason[reason.casefold()]["ids"].append(str(v.get("id") or ""))
    for entry in by_reason.values():
        if entry["n"] >= REASON_OVERTURNED:
            found.append({
                "type": "hold-reason-overturned",
                "identity": entry["display"],
                "occurrences": entry["n"],
                "evidence": {"verdict_ids": entry["ids"][:8]},
            })

    # (3) chip re-applied after a clear, >= N times on the same thread.
    by_thread = {}
    for thread, ts, kind in scan.chip_events:
        by_thread.setdefault(thread, []).append((ts, kind))
    for thread, events in by_thread.items():
        events.sort(key=lambda e: e[0])
        reapplied, cleared = 0, False
        for _ts, kind in events:
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

    found.sort(key=lambda p: (-p["occurrences"], p["type"], p["identity"]))
    return found


# --- proposals --------------------------------------------------------------
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


def build_aggregate(patterns: list, today: str) -> dict:
    kinds = sorted({p["type"] for p in patterns})
    key = _key_of(SOURCE_PREFIX, "aggregate", *kinds, str(len(patterns)))
    listing = "; ".join(f"{p['type']}:{_short(p['identity'], 40)}×{p['occurrences']}"
                        for p in patterns[:12])
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


# --- idempotency: proposed-unanswered / answered / declined -----------------
def read_inbox(inbox: Path):
    """Returns (open_keys, answered) — answered maps key -> answer text.
    Fail-soft: a corrupt inbox line is skipped, never raised."""
    open_keys, answered = set(), {}
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


def select_proposals(patterns, open_keys, answered, state, today,
                     max_proposals=MAX_PROPOSALS):
    """Apply all THREE idempotency states, then the cap + aggregation."""
    declined = state["declined"]
    seen = state["seen"]

    # A cos-retro question the owner answered with a decline becomes a
    # suppressed pattern — remembered with the occurrence count it carried at
    # decline time, so a settled question stops burning the weekly cap.
    for key, answer in answered.items():
        if key in declined:
            continue
        if DECLINE_MARK in str(answer).casefold():
            declined[key] = {"occurrences_at_decline": int(seen.get(key, 0)),
                             "declined_on": today}

    eligible, suppressed = [], 0
    for pattern in patterns:
        proposal = build_proposal(pattern, today)
        key = proposal["key"]
        if key in open_keys:                       # (1) proposed, unanswered
            suppressed += 1
            continue
        entry = declined.get(key)
        if entry is not None:                      # (3) declined
            floor = 2 * int(entry.get("occurrences_at_decline") or 0)
            if pattern["occurrences"] < max(floor, 1):
                suppressed += 1
                continue
            declined.pop(key, None)                # doubled — ask again
        elif key in answered:                      # (2) answered, settled
            suppressed += 1
            continue
        eligible.append((pattern, proposal))

    if len(eligible) <= max_proposals:
        chosen = [p for _pat, p in eligible]
        overflow = []
    else:
        chosen = [p for _pat, p in eligible[: max_proposals - 1]]
        overflow = [pat for pat, _p in eligible[max_proposals - 1:]]
        chosen.append(build_aggregate(overflow, today))

    for pattern, proposal in eligible:
        seen[proposal["key"]] = pattern["occurrences"]
    return chosen, suppressed, len(overflow)


# --- run --------------------------------------------------------------------
def mine(vault: Path, today: str, dry_run: bool = False,
         max_proposals: int = MAX_PROPOSALS) -> dict:
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
        patterns, open_keys, answered, state, today, max_proposals)

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
            with inbox.open("a", encoding="utf-8") as fh:
                for proposal in proposals:
                    fh.write(json.dumps(proposal, ensure_ascii=False) + "\n")
            written = len(proposals)
        except OSError as exc:  # the fold must survive a read-only vault
            # The state records what was PROPOSED; nothing was, so persisting it
            # would suppress these patterns forever on a transient write failure.
            status, summary_error, state_is_valid = "write-failed", str(exc), False

    if state_is_valid and not dry_run:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False),
                                  encoding="utf-8")
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true",
                    help="mine and report, write nothing")
    ap.add_argument("--max-proposals", type=int, default=MAX_PROPOSALS)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args(argv)

    try:
        summary = mine(args.vault, args.date, args.dry_run, args.max_proposals)
    except Exception as exc:  # noqa: BLE001 — a miner NEVER takes the fold down
        summary = {"status": "miner-error", "error": repr(exc),
                   "vault": str(args.vault)}
    # Proposal bodies came from untrusted mail content; the summary line the
    # wrapper logs carries counts only.
    loggable = {k: v for k, v in summary.items() if k not in ("proposals",
                                                              "aggregated")}
    print(json.dumps(loggable, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
