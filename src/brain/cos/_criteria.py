"""COS learning-criteria operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._io import _read_jsonl
from ._layout import _parse_ts, _utcnow
from ._learning_config import _category_config, _pattern_config
from ._learning_ledger import _last_demotion, _outcomes_path, defects
from ._taxonomy import ingest_taxonomy

def _wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (center - margin) / denom)

def pattern_stats(vault, pattern: str, bundle_version: str) -> dict[str, Any]:
    """Owner-decision volume/accept-rate + claim-time defect count for
    ``pattern``, scoped to THIS ``bundle_version`` only."""
    # `defect_count`, not `defects`: this module imports a `defects` FUNCTION
    # from `_learning_ledger`, and a local of that name hid it for the rest of
    # the scope. The reported key stays "defects".
    n = accepted = defect_count = 0
    for e in _read_jsonl(_outcomes_path(vault)):
        if e.get("pattern") != pattern or e.get("bundle_version") != bundle_version:
            continue
        outcome = e.get("outcome")
        if outcome == "accepted":
            n += 1
            accepted += 1
        elif outcome == "rejected":
            n += 1
        elif outcome == "claim-rejected-security":
            defect_count += 1
    return {"n": n, "accepted": accepted, "defects": defect_count,
            "lower_bound": _wilson_lower_bound(accepted, n)}

def auto_capture_eligible(vault, pattern: str | None,
                          bundle_version: str | None) -> tuple[bool, dict[str, Any]]:
    """The ING-04 gate. Returns ``(eligible, stats)`` — ``stats`` always
    carries enough to explain the decision (never a bare bool)."""
    if pattern in _UNPATTERNED or bundle_version in _UNPATTERNED:
        return False, {"reason": "no pattern/bundle_version on candidate"}
    cfg = _pattern_config(vault, pattern)
    stats = pattern_stats(vault, pattern, bundle_version)
    stats["config"] = cfg
    if stats["n"] < cfg["min_volume"]:
        return False, {**stats, "reason": "below-min-volume"}
    if stats["defects"] > 0:
        return False, {**stats, "reason": "defects-present"}
    if stats["lower_bound"] < cfg["min_lower_bound"]:
        return False, {**stats, "reason": "lower-bound-below-threshold"}
    return True, {**stats, "reason": "eligible"}

def canonical_evidence_text(body: str) -> str:
    """Normalize one candidate body down to the material it actually carries."""
    kept: list[str] = []
    for raw in str(body or "").splitlines():
        line = raw.strip()
        while line.startswith(">"):
            line = line[1:].lstrip()
        if not line or _FORWARD_WRAPPER_RE.match(line):
            continue
        kept.append(line)
    return " ".join(" ".join(kept).split()).casefold()

def evidence_unit_key(*, category: str, lane: str, rules_version: str,
                      body: str = "") -> str:
    """The stable EVIDENCE UNIT one verdict is allowed to count for
    (HARDENED:codex-4, hardened again by B5): a CANONICAL content fingerprint
    + category + lane + ruleset version.

    The source conversation id used to be hashed in here too — but it is a VM
    claim, so repeated forwards under fresh ids counted as independent
    verdicts and inflated the Wilson sample. Unverified conversation identity
    must never INCREASE evidence cardinality, so it is gone from this key; the
    only conversation identity that dedups is the host-verified lineage below,
    which can only ever merge units.
    """
    return sha256_text("|".join([
        sha256_text(canonical_evidence_text(body)),
        str(category or ""), str(lane or ""), str(rules_version or ""),
    ]))

def evidence_lineage_key(*, category: str, lane: str, rules_version: str,
                         conversation_id: Any = None,
                         verified: bool = False) -> str | None:
    """The HOST-VERIFIED conversation lineage a verdict belongs to, or ``None``.

    ``verified`` is the host's own assertion (``provenance.verified``, S04 —
    parsed from an archived original, never assertable by a VM). Without it
    there is no lineage: a claimed conversation id buys the producer nothing.
    """
    if not verified or not str(conversation_id or "").strip():
        return None
    return sha256_text("|".join([
        "lineage", str(conversation_id).strip(),
        str(category or ""), str(lane or ""), str(rules_version or ""),
    ]))

def category_stats(vault, *, category: str, lane: str, tier: str,
                   rules_version: str, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Owner-decision volume/accept-rate + defect count for ONE evidence key,
    over the ROLLING window (default: last 90 days, at most the last 50
    verdicts). Windowing is what makes calibration drift inside a stable
    ruleset show up as an accept-rate DROP that un-graduates the category —
    an all-time count would dilute a recent collapse into ancient successes."""
    cfg = _category_config(vault, category)
    now = now or _utcnow()
    cutoff = now - _dt.timedelta(days=int(cfg["window_days"]))
    demoted_at = _last_demotion(vault, category)
    bulk_cap = int(cfg["bulk_accept_max_batch"])
    verdicts: list[str] = []
    defect_count = 0
    excluded_bulk = 0
    for e in _read_jsonl(_outcomes_path(vault)):
        if (e.get("category") != category or e.get("lane") != lane
                or e.get("tier") != tier or e.get("rules_version") != rules_version):
            continue
        stamp = str(e.get("ts", ""))
        parsed = _parse_ts(stamp)
        if parsed is None or parsed < cutoff:
            continue
        if demoted_at and stamp <= demoted_at:
            continue                      # evidence reset by a demotion
        if e.get("counted") is False:
            continue                      # duplicate evidence unit
        outcome = e.get("outcome")
        if outcome in ("claim-rejected-security", "undo"):
            defect_count += 1
        elif outcome in ("accepted", "rejected"):
            if (outcome == "accepted" and e.get("answer_mode") == "accept-all"
                    and int(e.get("batch_size") or 0) > bulk_cap):
                # Approval fatigue is MEASURED, not hypothetical: owners bulk
                # approve ~90%+ of HITL prompts, so an `accept all` over a
                # large batch is not per-candidate agreement. Excluded from the
                # numerator AND the denominator (counting it as a rejection
                # would be just as wrong). Rejects always count.
                excluded_bulk += 1
                continue
            verdicts.append(outcome)
    verdicts = verdicts[-int(cfg["window_verdicts"]):]
    n = len(verdicts)
    accepted = verdicts.count("accepted")
    return {"n": n, "accepted": accepted, "defects": defect_count,
            "lower_bound": _wilson_lower_bound(accepted, n),
            "excluded_bulk_accepts": excluded_bulk, "demoted_at": demoted_at,
            "config": cfg}

def category_eligible(vault, *, category: str | None, lane: str | None,
                      tier: str | None, rules_version: str | None,
                      now: _dt.datetime | None = None,
                      taxonomy: dict[str, Any] | None = None,
                      ) -> tuple[bool, dict[str, Any]]:
    """Has this category GRADUATED on this exact (lane, tier, ruleset) key?

    Same statistical bar as the pattern gate — min volume, zero security
    defects, Wilson lower bound — plus the overlay checks that make a
    graduation revocable by an owner EDIT: a category removed from
    `ingest.md`, or flipped to `never`, loses graduation at the next engine
    load with no grace period. Any category may graduate, including
    high-tier ones (owner decision) — a graduated category's candidates still
    commit through the undo-windowed hold path, never an instant signature.
    """
    if category in _UNPATTERNED or rules_version in _UNPATTERNED:
        return False, {"reason": "no category/extraction_rules_version on candidate"}
    if lane not in LANES:
        return False, {"reason": f"unknown lane {lane!r}"}
    if not tier or tier == "unknown":
        return False, {"reason": "no classification ceiling bound to the candidate"}
    tax = taxonomy if taxonomy is not None else ingest_taxonomy(vault)
    if tax.get("mode") != "active":
        return False, {"reason": f"ingest taxonomy {tax.get('mode')} — nothing graduates"}
    rule = tax["rules"].get(category)
    if not isinstance(rule, dict):
        return False, {"reason": "category-not-in-overlay (removed ⇒ demoted at load)"}
    if rule.get("disposition") == DISPOSITION_NEVER:
        return False, {"reason": "category is `never` (⇒ demoted at load)"}
    stats = category_stats(vault, category=category, lane=lane, tier=tier,
                           rules_version=rules_version, now=now)
    cfg = stats["config"]
    if stats["n"] < cfg["min_volume"]:
        return False, {**stats, "reason": "below-min-volume"}
    if stats["defects"] > 0:
        return False, {**stats, "reason": "defects-present"}
    if stats["lower_bound"] < cfg["min_lower_bound"]:
        return False, {**stats, "reason": "lower-bound-below-threshold"}
    return True, {**stats, "reason": "eligible"}

__all__ = ['_wilson_lower_bound', 'pattern_stats', 'auto_capture_eligible', 'canonical_evidence_text', 'evidence_unit_key', 'evidence_lineage_key', 'category_stats', 'category_eligible']
