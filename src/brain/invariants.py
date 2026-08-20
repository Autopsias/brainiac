"""WAT-01 — the corpus-invariants watchdog: seven cheap counts, trended,
alerting, plus its own dead-man's switch.

Corpus properties drifted for months each because nobody had a NUMBER for
them. This module computes those counts and nothing else; the nightly
``maintain`` umbrella runs it as a daily fold (see ``BrainCore.maintain``,
the ``corpus_invariants`` block), persists them into ``maintain-state.json``
+ ``health-history.jsonl``, and ``brain health-report`` renders them under
"Corpus invariants".

The seven:

1. ``unlinked_sources``  — ``raw/``-zone notes with ZERO inbound reference
   (a wikilink from any note body, a ``source:``/``replaces:`` frontmatter
   link from a ``brain/`` note, or an indexed supersession link). Excludes
   the vault's own re-ingested operational output (ENF-06), which is not
   knowledge anyone should be writing a note about.
2. ``cross_tier_twins``  — ``<ingest-date>-<slug>`` / ``<slug>`` id pairs
   whose two notes carry DIFFERENT ``classification`` (an Internal-capped
   reader reaching Restricted substance through the twin).
3. ``cross_tier_duplicates`` — the same exposure found by CONTENT instead of
   by filename (ENF-03): documents that exist twice at two tiers however
   their ids are spelled. Metric 2 sees ONE filename shape and reads 0 while
   these sit in the corpus; metric 3 is blind to a name twin whose two copies
   are genuinely different documents. Neither contains the other, so both
   ship.
4. ``cross_tier_candidates`` — the UNDECIDED half of metric 3: pairs sharing
   a document's vocabulary but not its word order. Reported, never guessed
   at, never silently counted as clean.
5. ``unguarded_ingests`` — ENF-04: sources the ingest pipeline admitted while
   the cross-tier guard was UNAVAILABLE (no index, a read error, or the env
   kill switch). Should be 0 and stay 0, so it ratchets. The guard's raises,
   its refusals below the ENF-01 floor, and the sources predating it are
   reported beside it — per leg for raises, because an aggregate cannot tell
   a clean corpus from a dead leg.
6. ``subfloor_families`` — supersession families whose members share one
   normalized body hash (a DDP-01 auto-link) and where some member's body is
   below the ``$BRAIN_FAMILY_MIN_BODY`` floor (auto-superseding on empty-OCR
   stubs).
7. ``unreachable_gold``  — READ, never re-run, from the newest
   ``eval/s06_reachability.py`` artifact: gold documents no ranking leg
   reaches at all.
8. ``unsigned_notes`` — indexed notes without an audit-chain entry; its
   implementation lives in ``invariant_unsigned_notes``.

All eight are no-model and read-only. 1-4 and 6 read the index (plus the
``brain/`` zone's frontmatter, ~hundreds of small files); 5 reads ``raw/``
frontmatter (~1.7s on the reference vault); 7 reads one JSON artifact; 8
reads the audit chain once and stats each note (no note bodies).

**Thresholds are ABSOLUTE and RATCHETING, never percent** (hardening G3). A
percent-regression check against a trailing baseline dies at zero — the exact
state this plan's backfills drive three of these metrics toward — and an
absolute threshold pinned at today's large baseline never fires again once
the backfill lands. So each metric carries a FLOOR: the best (lowest) value
ever recorded, persisted in ``maintain-state.json``. A regression is
``value > floor + tolerance`` (tolerance default 0, env-tunable per metric).
That one rule works identically at 2,132 and at 0: as a backfill ratchets the
floor down to zero, the very next new violation fires.

**The dead-man's switch** (owner ruling 2026-08-10, "we need an alarm on the
alarm too"): the fold's own last-successful-run date is part of the health
record, and a row older than ``$BRAIN_INVARIANTS_MAX_AGE_DAYS`` (default 3)
— or missing entirely on a vault whose other branches DO run — is itself a
finding. It is checked from lanes that do not depend on the fold firing:
``brain doctor``/``brain health-report`` (a STALE row -> DEGRADED),
``maintenance.degradation_findings`` (-> the per-day notify marker the
SessionStart alerts hook reads), and the weekly synthesis watchdog prompt.

Because the fold writes a real ``maintain-state.json`` row, ES-01's own
branch-liveness gate applies to it too (``_CADENCE_DAYS["corpus_invariants"]``
= 1, loud past 2 cadences). So a stale row is caught TWICE, at 2 days by
ES-01 and at ``$BRAIN_INVARIANTS_MAX_AGE_DAYS`` by the explicit check — the
explicit one is the tunable contract and the one that also covers a row that
is MISSING ENTIRELY, which ES-01 structurally cannot see (it iterates the
rows that are present).
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from .invariant_shared import SAMPLE_CAP
from .invariant_reachability import unreachable_gold
from .invariant_unsigned_notes import unsigned_notes

# The metric names, in report order. Every consumer (state, health
# record, report, alerting) iterates THIS tuple rather than re-listing them.
INVARIANT_METRICS = (
    "unlinked_sources",
    "cross_tier_twins",
    "cross_tier_duplicates",
    "cross_tier_candidates",
    "unguarded_ingests",
    "subfloor_families",
    "unreachable_gold",
    "unsigned_notes",
)

# The state key this fold owns inside maintain-state.json. It is a plain
# (non-`_`-prefixed) key on purpose: `maintenance.maintain_escalation` and
# `doctor.check_vm_maintain_heartbeat` iterate those and therefore give the
# fold ES-01 liveness escalation for free, on top of the explicit
# max-age check below.
STATE_KEY = "corpus_invariants"

MAX_AGE_DAYS_ENV = "BRAIN_INVARIANTS_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 3
REACHABILITY_GLOB_ENV = "BRAIN_INVARIANTS_REACHABILITY_GLOB"

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-(?=.)")


def ingest_guard(vault: Path, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """ENF-04 — the ingest guard's verdicts, read back off ``raw/``.

    ``value`` is the count of sources admitted while the guard was UNAVAILABLE
    (the only non-monotone, should-be-zero number here). Every other status is
    reported beside it, per leg for raises, so "0 raised" can never quietly
    mean "the guard never looked"."""
    from . import frontmatter as fm
    from .ingest.tierguard import (
        _NO_CORPUS_ERROR, GUARD_KEY, GUARD_LEG_KEY, GUARD_REASON_KEY,
        GUARD_STATUSES, NO_CORPUS,
    )

    by_status = {s: 0 for s in GUARD_STATUSES}
    by_leg: dict[str, int] = {}
    unstamped = 0
    unknown = 0
    sample: list[str] = []
    raw = Path(vault) / "raw"
    total = 0
    for path in sorted(raw.glob("*.md")):
        total += 1
        try:
            meta, _body = fm.parse_text(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        status = str(meta.get(GUARD_KEY) or "").strip()
        if not status:
            # Ingested before ENF-04 shipped. Reported with its own number so
            # the denominator is honest — never folded into `clear`, which
            # would claim a check that never happened.
            unstamped += 1
            continue
        if status not in by_status:
            unknown += 1
            continue
        # Sources ingested before the 2026-08-17 split carry `unavailable`
        # even when the guard RAN against an empty corpus — its own reason
        # line says so, and `raw/` is immutable so the stamp cannot be
        # corrected in place. Read the recorded evidence rather than leaving a
        # permanent false alarm on every vault whose first document predates
        # the split.
        if status == "unavailable" and _NO_CORPUS_ERROR in str(
                meta.get(GUARD_REASON_KEY) or ""):
            status = NO_CORPUS
        by_status[status] += 1
        if status == "raised":
            leg = str(meta.get(GUARD_LEG_KEY) or "unrecorded")
            by_leg[leg] = by_leg.get(leg, 0) + 1
            if len(sample) < cap:
                sample.append(f"{path.stem} -> {meta.get('classification')} ({leg})")
    return {
        "value": by_status["unavailable"],
        "sources": total,
        "raised": by_status["raised"],
        "raised_by_leg": by_leg,
        "clear": by_status["clear"],
        "subfloor": by_status["subfloor"],
        # Reported beside `value`, never inside it: the guard looked and the
        # corpus held nothing comparable, so there was no higher-tier twin to
        # leak from. Never hidden — a leg that stops working must still show.
        "no_corpus": by_status[NO_CORPUS],
        "unstamped": unstamped,
        "unknown_status": unknown,
        "sample": sample,
    }


def subfloor_families(conn: Any, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """Supersession families auto-linked on a body BELOW the
    ``$BRAIN_FAMILY_MIN_BODY`` floor (metric 3).

    A family here is a connected component over the indexed supersession
    links whose members all share ONE normalized body hash — the DDP-01
    auto-dedup shape. It counts as sub-floor when any member's body is
    shorter than the floor, which is how a 122-byte ``[no text detected]``
    OCR stub silently retires a different document."""
    from .index import _family_min_body
    from .maintenance import _floor_bytes, body_sha256

    floor = _family_min_body()
    rows = conn.execute(
        "SELECT id, body, superseded_by, previous_version FROM notes").fetchall()
    # UTF-8 BYTES — the unit the floor is declared in and the unit the `B`
    # label below claims. `len(str)` counts Unicode scalars, and chars <= bytes,
    # so this metric flagged a strict SUPERSET of what the writer refuses: a
    # 1,100-byte / 900-character Portuguese family was legitimately merged by
    # `auto_dedup_tier1` and simultaneously counted here as a violation. On an
    # absolute-threshold invariant (any value > 0 is a regression) that is a
    # permanent, unclearable false alarm on a multilingual corpus. Same helper
    # as the writer and the ranker (ENF-01, adversarial review round 2).
    #
    # NORMALIZED bytes (round 3), the same string ``body_sha256`` above hashes
    # and the same one ``auto_dedup_tier1`` now measures. Measuring raw bytes
    # here let a whitespace-padded 18-byte stub read as 1,118 bytes, so the
    # metric agreed with the writer that a sub-floor family was fine.
    info = {str(r[0]): (body_sha256(r[1] or ""), _floor_bytes(r[1] or "")) for r in rows}

    parent: dict[str, str] = {nid: nid for nid in info}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    linked: set[str] = set()
    for nid, _body, sup_by, prev in rows:
        for other in (sup_by, prev):
            o = str(other or "")
            if not o or o not in info or o == nid:
                continue
            linked.add(str(nid))
            linked.add(o)
            ra, rb = find(str(nid)), find(o)
            if ra != rb:
                parent[ra] = rb

    families: dict[str, list[str]] = {}
    for nid in linked:
        families.setdefault(find(nid), []).append(nid)

    hits: list[str] = []
    for root, members in sorted(families.items()):
        hashes = {info[m][0] for m in members}
        if len(hashes) != 1:
            continue  # a real revision family, not a byte-identical auto-link
        if min(info[m][1] for m in members) >= floor:
            continue
        hits.append(f"{min(info[m][1] for m in members)}B: " + ", ".join(sorted(members)))
    return {
        "value": len(hits),
        "families": len(families),
        "floor": floor,
        "sample": hits[:cap],
    }


def corpus_invariants(conn: Any, vault: Path, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """All four metrics in one read-only pass. Never raises on one metric —
    a metric that blows up records ``{"value": None, "error": …}`` so the
    other three still trend."""
    out: dict[str, Any] = {}
    # Metrics 5 and 6 are two halves of ONE scan (decided / undecided), so the
    # scan runs once and both entries read its result.
    memo: dict[str, dict[str, Any]] = {}

    def _duplicates() -> dict[str, Any]:
        if "dup" not in memo:
            try:
                memo["dup"] = cross_tier_duplicates(conn, cap=cap)
            except Exception as exc:  # noqa: BLE001 — surfaced on BOTH halves
                memo["dup"] = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        return memo["dup"]

    computers = {
        "unlinked_sources": lambda: unlinked_sources(conn, vault, cap=cap),
        "cross_tier_twins": lambda: cross_tier_twins(conn, cap=cap),
        "cross_tier_duplicates": _duplicates,
        "cross_tier_candidates": lambda: cross_tier_candidates_entry(_duplicates()),
        "unguarded_ingests": lambda: ingest_guard(vault, cap=cap),
        "subfloor_families": lambda: subfloor_families(conn, cap=cap),
        "unreachable_gold": lambda: unreachable_gold(vault),
        "unsigned_notes": lambda: unsigned_notes(conn, vault, cap=cap),
    }
    for name in INVARIANT_METRICS:
        try:
            out[name] = computers[name]()
        except Exception as exc:  # noqa: BLE001 — one broken metric never sinks four
            out[name] = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
    return out


# ---------------------------------------------------------------------------
# The ratchet (G3): absolute thresholds anchored on the best value ever
# recorded, so the SAME rule alerts from a 2,132 baseline and from a zero one.
# ---------------------------------------------------------------------------
def tolerance_env(metric: str) -> str:
    return f"BRAIN_INVARIANTS_{metric.upper()}_TOLERANCE"


def metric_tolerance(metric: str) -> int:
    try:
        return max(0, int(os.environ.get(tolerance_env(metric), "").strip() or 0))
    except ValueError:
        return 0


def metric_values(metrics: dict[str, Any]) -> dict[str, int]:
    """``{metric: value}`` for the metrics that produced a real number this
    run. A ``None`` (errored, or no artifact yet) is OMITTED — it must not
    ratchet a floor down or read as a regression."""
    values: dict[str, int] = {}
    for name in INVARIANT_METRICS:
        entry = metrics.get(name)
        if isinstance(entry, dict) and isinstance(entry.get("value"), int):
            values[name] = int(entry["value"])
    return values


def rebase_unreachable_floor(
    prev_floors: dict[str, Any] | None, metrics: dict[str, Any],
) -> dict[str, Any]:
    """Drop the ``unreachable_gold`` floor when the artifact's LABEL COUNT
    changed since the floor was recorded (basis key
    ``unreachable_gold_labels``, an int, so it survives ``update_floors``).

    The floor is min-ever over an absolute count, so a golden-set expansion
    (181 -> 375 labels on 2026-08-18) reads as a permanent nightly
    regression against a floor measured on a different denominator — a
    standing false alarm, the exact fatigue the ratchet exists to prevent.
    A re-seeded metric behaves like a first observation: it never alerts on
    the re-seed run, then ratchets on the new basis. Accepts any
    ``prev_floors`` shape (the caller passes the raw state value)."""
    if not isinstance(prev_floors, dict):
        prev_floors = {}
    m = metrics.get("unreachable_gold") if isinstance(metrics, dict) else None
    labels = m.get("labels") if isinstance(m, dict) else None
    if not isinstance(labels, int):
        return dict(prev_floors or {})
    out = dict(prev_floors or {})
    if out.get("unreachable_gold_labels") != labels:
        out.pop("unreachable_gold", None)
        out["unreachable_gold_labels"] = labels
    return out


def update_floors(prev_floors: dict[str, Any] | None, values: dict[str, int]) -> dict[str, int]:
    """The new floor per metric: the lowest value ever recorded. A metric's
    first observation seeds its own floor, so the first run never alerts."""
    floors = {k: int(v) for k, v in (prev_floors or {}).items() if isinstance(v, int)}
    for name, value in values.items():
        floors[name] = min(floors[name], value) if name in floors else value
    return floors


def invariant_regressions(
    prev_floors: dict[str, Any] | None, values: dict[str, int],
) -> list[dict[str, Any]]:
    """Metrics that came in ABOVE their recorded floor plus tolerance. Empty
    on the very first run of each metric (no floor yet -> nothing to regress
    against)."""
    floors = {k: int(v) for k, v in (prev_floors or {}).items() if isinstance(v, int)}
    out: list[dict[str, Any]] = []
    for name in INVARIANT_METRICS:
        if name not in values or name not in floors:
            continue
        tol = metric_tolerance(name)
        value, floor = values[name], floors[name]
        if value > floor + tol:
            out.append({
                "metric": name, "value": value, "floor": floor, "tolerance": tol,
                "summary": (f"corpus invariant '{name}' regressed: {value} "
                            f"(best recorded {floor}"
                            + (f", tolerance {tol}" if tol else "") + ")"),
            })
    return out


def render_invariants_hot_entry(
    regressions: list[dict[str, Any]], metrics: dict[str, Any], today: datetime.date,
) -> str:
    """ONE hot.md LOG line per regressing run (never a queue item, never
    owner-input-needed — same PUSH posture as every other fold)."""
    lines = [f"## {today.isoformat()} — Corpus invariants: regression"]
    for r in regressions:
        lines.append(f"- **{r['metric']}:** {r['value']} (best recorded {r['floor']}"
                      + (f", tolerance {r['tolerance']}" if r["tolerance"] else "") + ")")
        entry = metrics.get(r["metric"]) if isinstance(metrics.get(r["metric"]), dict) else {}
        for s in (entry.get("sample") or [])[:3]:
            lines.append(f"  - `{s}`")
    lines.append(
        "- **Next:** see the 'Corpus invariants' section of `brain health-report` "
        "for the trend. A corpus-invariant fix never ships without its metric in "
        "the same change (AGENTS.md §4) — so this is a real corpus change, not a "
        "reporting artifact.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The dead-man's switch (owner ruling 2026-08-10). Pure, no I/O — every lane
# (doctor, health-report, degradation notifications, the weekly synthesis
# watchdog) calls these against an already-loaded maintain-state.
# ---------------------------------------------------------------------------
def max_age_days() -> int:
    try:
        return max(1, int(os.environ.get(MAX_AGE_DAYS_ENV, "").strip()
                          or DEFAULT_MAX_AGE_DAYS))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def invariants_age_days(
    state: dict[str, Any], today: datetime.date | None = None,
) -> int | None:
    """Days since the fold last COMPLETED, or ``None`` when it never has."""
    d = today or datetime.date.today()
    entry = state.get(STATE_KEY) if isinstance(state.get(STATE_KEY), dict) else {}
    last_run = entry.get("last_run")
    if not last_run:
        return None
    try:
        return (d - datetime.date.fromisoformat(str(last_run))).days
    except ValueError:
        return None


def liveness_finding(
    state: dict[str, Any] | None, today: datetime.date | None = None,
) -> tuple[str, str] | None:
    """``(dedup_key, text)`` when the invariants row is stale or missing —
    the alarm ON the alarm. ``None`` when it is fresh.

    A fold that has died cannot report its own death, so this keys on the
    EXPECTED state row rather than on iterating the rows that happen to be
    present: a row missing entirely, on a vault where other branches DO run,
    is the engine-restage failure mode this exists to catch. A vault where
    NOTHING has ever run (fresh install) is silent."""
    d = today or datetime.date.today()
    state = state or {}
    branches = [k for k, v in state.items()
                if not str(k).startswith("_") and isinstance(v, dict)]
    if not branches:
        return None
    limit = max_age_days()
    if STATE_KEY not in branches:
        return ("invariants-liveness",
                "corpus-invariants watchdog has NEVER run on this vault while "
                f"{len(branches)} other maintain branch(es) have — the fold is "
                "missing from this engine build (restage?)")
    age = invariants_age_days(state, d)
    if age is None:
        return ("invariants-liveness",
                "corpus-invariants watchdog has no successful run recorded "
                "(present but never completed)")
    if age > limit:
        return ("invariants-liveness",
                f"corpus-invariants watchdog last succeeded {age}d ago "
                f"(> {limit}d) — the {len(INVARIANT_METRICS)} corpus counts "
                "are unwatched")
    return None


def state_regressions(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The regressions the last fold run recorded — read back out of
    maintain-state so the notification/report lanes never recompute them."""
    entry = (state or {}).get(STATE_KEY)
    if not isinstance(entry, dict):
        return []
    regs = entry.get("regressions")
    return [r for r in regs if isinstance(r, dict)] if isinstance(regs, list) else []



# The link-coverage exclusions/metric/lane live in invariant_coverage.py and
# the ENF-03 cross-tier primitives in invariant_crosstier.py since the
# 2026-08-16 size ratchet; re-exported so every `brain.invariants.<name>`
# caller (including ingest/pipeline.py and ingest/tierguard.py imports) works
# unchanged.
from .invariant_coverage import (  # noqa: E402,F401  (facade re-export)
    EMBEDDED_FRONTMATTER_HEAD as EMBEDDED_FRONTMATTER_HEAD,
    EXCLUSION_REASONS as EXCLUSION_REASONS,
    GENERATED_MAP_BASENAMES as GENERATED_MAP_BASENAMES,
    NON_KNOWLEDGE_ZONES as NON_KNOWLEDGE_ZONES,
    OPERATIONAL_SOURCE_TYPES as OPERATIONAL_SOURCE_TYPES,
    QUARANTINE_PATH_SEGMENTS as QUARANTINE_PATH_SEGMENTS,
    _frontmatter_links as _frontmatter_links,
    _resolve as _resolve,
    _unlinked_rows as _unlinked_rows,
    embedded_source_type as embedded_source_type,
    link_coverage_exclusion as link_coverage_exclusion,
    link_lane_budget as link_lane_budget,
    link_lane_candidates as link_lane_candidates,
    lane_consumption as lane_consumption,
    _untrusted_draft as _untrusted_draft,
    unlinked_sources as unlinked_sources,
    write_link_lane as write_link_lane,
    DEFAULT_LINK_LANE_BUDGET as DEFAULT_LINK_LANE_BUDGET,
    LINK_LANE_BUDGET_ENV as LINK_LANE_BUDGET_ENV,
    LINK_LANE_RELPATH as LINK_LANE_RELPATH,
    LINK_LANE_SELECTION as LINK_LANE_SELECTION,
    _TIER_ORDER as _TIER_ORDER,
)
from .invariant_crosstier import (  # noqa: E402,F401  (facade re-export)
    CROSS_TIER_CANDIDATE as CROSS_TIER_CANDIDATE,
    CROSS_TIER_MIN_TOKENS as CROSS_TIER_MIN_TOKENS,
    CROSS_TIER_SAME_DOC as CROSS_TIER_SAME_DOC,
    CROSS_TIER_SCREEN as CROSS_TIER_SCREEN,
    CROSS_TIER_SHINGLE as CROSS_TIER_SHINGLE,
    CROSS_TIER_SKETCH as CROSS_TIER_SKETCH,
    CROSS_TIER_SKIP_REASONS as CROSS_TIER_SKIP_REASONS,
    _CT_FRONTMATTER as _CT_FRONTMATTER,
    _CT_WORD as _CT_WORD,
    _ct_exclusion as _ct_exclusion,
    _ct_shingles as _ct_shingles,
    _ct_sketch as _ct_sketch,
    _ct_tokens as _ct_tokens,
    _jaccard as _jaccard,
    cross_tier_candidates_entry as cross_tier_candidates_entry,
    cross_tier_twins as cross_tier_twins,
    screen_gate as screen_gate,
)
from .invariants_metrics import cross_tier_duplicates as cross_tier_duplicates  # noqa: E402

