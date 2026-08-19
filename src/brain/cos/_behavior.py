"""COS behaviour-grading operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._corrections import known_ledger_keys, list_corrections, shadow_ledger_entries
from ._io import _read_jsonl
from ._layout import verdict_drop_dir

def grade_behaviour(bucket: str, observed: str, *,
                    auto_archived: bool = False) -> str:
    """One verdict row + one observed behaviour -> a grade.

    The error that matters for auto-archive is exactly one: "would have
    archived mail the owner needed". So a NOISE verdict the owner then replied
    to or flagged is `contradicted` — the hard, gate-relevant failure. A noise
    row he merely opened is `read_anyway`: a weak signal (people open
    newsletters), reported but never gated on. Noise he left untouched or
    archived himself is `consistent`. An act/read row he archived without
    engaging is `overcalled` — over-caution, harmless, informational.
    """
    b, o = str(bucket).lower(), str(observed).lower()
    # Aged-read lane (owner policy 2026-07-17): priority-list mail may be
    # auto-archived when read + no-action + >7d old. Those rows are bucket
    # `read`, not `noise` — so the drift contradiction must key on the ACTION
    # (we auto-archived it), not the bucket: the owner replying to or flagging
    # ANY row we auto-archived is the gate error, whichever lane moved it.
    if auto_archived and o in ("owner_replied", "owner_flagged"):
        return "contradicted"
    if b == "noise":
        if o in ("owner_replied", "owner_flagged"):
            return "contradicted"
        if o == "owner_read":
            return "read_anyway"
        if o in ("untouched", "owner_archived"):
            return "consistent"
    elif b in ("act", "read") and o == "owner_archived":
        return "overcalled"
    return "neutral"

def behaviour_entries(vault) -> list[dict[str, Any]]:
    """Raw behaviour observations from the VM drop (``behaviour-*.jsonl``),
    deduped by (round, msg_key) — last write wins, same idempotency shape as
    the shadow ledger. Rows are VM-authored and untrusted: consumed as data."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("behaviour-*.jsonl")) if vdir.is_dir() else []
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                by_key[(r, k)] = e
    return list(by_key.values())

def behaviour_report(vault) -> dict[str, Any]:
    """Aggregate observed-behaviour evidence: per-bucket grade counts, the
    noise-safety numbers an auto-archive re-arm decision needs, and the
    owner's own archive patterns (top senders he archives himself — evidence
    for FUTURE noise-signals, never an actuator by itself)."""
    entries = behaviour_entries(vault)
    # Exclusion (2026-07-18 field report): legacy `content-rejoin:` keys — and,
    # when a shadow ledger exists, any row whose msg_key joins no verdict —
    # never enter the rates. No ledger at all ⇒ only the legacy scheme is
    # excludable (can't prove a join miss against nothing).
    ledger = known_ledger_keys(vault)
    verdict_keys = {k for _, k in ledger} if ledger else None
    excluded = 0
    joined: list[dict[str, Any]] = []
    for e in entries:
        k = str(e.get("msg_key", ""))
        if k.startswith(LEGACY_REJOIN_PREFIX) or (
                verdict_keys is not None and k not in verdict_keys):
            excluded += 1
            continue
        joined.append(e)
    entries = joined
    per_bucket: dict[str, dict[str, int]] = {}
    contradicted_rows: list[dict[str, Any]] = []
    owner_archive_patterns: dict[str, int] = {}
    rounds: set[int] = set()
    for e in entries:
        b = str(e.get("bucket", "?")).lower()
        o = str(e.get("observed", "?")).lower()
        g = grade_behaviour(b, o, auto_archived=bool(e.get("auto_archived")))
        per_bucket.setdefault(b, {})[g] = per_bucket.setdefault(b, {}).get(g, 0) + 1
        rounds.add(int(e["round"]))
        if g == "contradicted":
            contradicted_rows.append(
                {k: e.get(k) for k in ("round", "msg_key", "sender", "subject",
                                        "observed")})
        if o == "owner_archived":
            key = str(e.get("sender") or e.get("sender_domain") or "unknown").lower()
            owner_archive_patterns[key] = owner_archive_patterns.get(key, 0) + 1
    noise = per_bucket.get("noise", {})
    noise_observed = sum(noise.values())
    contradicted = noise.get("contradicted", 0)
    return {
        "observations": len(entries),
        "excluded_unjoined": excluded,
        "rounds_observed": len(rounds),
        "per_bucket": per_bucket,
        "noise_observed": noise_observed,
        "noise_contradicted": contradicted,
        "noise_consistency": (round((noise_observed - contradicted) / noise_observed, 4)
                              if noise_observed else None),
        "contradicted_rows": contradicted_rows[:20],
        "owner_archive_patterns": dict(sorted(owner_archive_patterns.items(),
                                              key=lambda kv: -kv[1])[:20]),
    }

def calibration_report(vault) -> dict[str, Any]:
    """Shadow-mode trust-gate report: calibration = reduce(verdicts,
    correction_events). A verdict is bucket-correct when no correction exists
    for its (round, msg_key) OR the correction only changed the tier.
    Rounds completed = distinct rounds present in the shadow ledger."""
    verdicts = shadow_ledger_entries(vault)
    corr = {(c["round"], c["msg_key"]): c for c in list_corrections(vault)}
    rounds: dict[int, dict[str, int]] = {}
    buckets: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        r = int(v["round"])
        key = (r, v["msg_key"])
        b = str(v.get("bucket", "?")).lower()
        rr = rounds.setdefault(r, {"total": 0, "corrected": 0})
        bb = buckets.setdefault(b, {"predicted": 0, "bucket_correct": 0})
        rr["total"] += 1
        bb["predicted"] += 1
        c = corr.get(key)
        if c is not None:
            rr["corrected"] += 1
        if c is None or str(c["corrected_bucket"]).lower() == b:
            bb["bucket_correct"] += 1
    for s in buckets.values():
        s["precision"] = (round(s["bucket_correct"] / s["predicted"], 4)
                          if s["predicted"] else None)
    total = len(verdicts)
    bucket_correct = sum(s["bucket_correct"] for s in buckets.values())
    return {
        "rounds_completed": len(rounds),
        "rounds": {str(k): v for k, v in sorted(rounds.items())},
        "verdicts": total,
        "corrections": len(corr),
        "overall_bucket_precision": (round(bucket_correct / total, 4)
                                     if total else None),
        "per_bucket": buckets,
        # revealed preference alongside stated preference: the corrections
        # count above stays authoritative where it exists, but 0 corrections
        # no longer means 0 evidence.
        "behaviour": behaviour_report(vault),
    }

__all__ = ['grade_behaviour', 'behaviour_entries', 'behaviour_report', 'calibration_report']
