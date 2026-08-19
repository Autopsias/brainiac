"""COS routing operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._criteria import auto_capture_eligible, category_eligible
from ._io import _write_atomic
from ._layout import _ts, proposals_dir

def _route_stats_path(vault=None) -> Path:
    return proposals_dir(vault) / "route-stats.json"

def route_stats(vault=None) -> dict[str, Any]:
    try:
        out = json.loads(_route_stats_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}

def _bump_route_stats(vault, *, now: _dt.datetime, unstamped: int = 0,
                      auto: int = 0, batched: int = 0,
                      **extra: int) -> dict[str, Any]:
    """Add to the cumulative routing counters (`brain status`'s `route_stats`).

    STA-01 adds `unjoined_claims` + `quarantined_claims` through ``**extra`` —
    same shape, same surfacing, so a candidate the host could not attribute is
    as loud as one that arrived unstamped."""
    cur = route_stats(vault)
    for key, add in (("unstamped_batched", unstamped), ("auto_captured", auto),
                     ("batched", batched), *extra.items()):
        cur[key] = int(cur.get(key, 0)) + int(add)
    if unstamped:
        cur["last_unstamped"] = _ts(now)
    if extra.get("quarantined_claims"):
        cur["last_quarantine"] = _ts(now)
    p = _route_stats_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    public("_write_atomic")(p, (json.dumps(cur, sort_keys=True) + "\n").encode("utf-8"))
    return cur

def _stamp_missing(bound: dict[str, Any]) -> bool:
    """Did this candidate arrive with no category / no ruleset version at all?"""
    return (bound.get("category") in _UNPATTERNED
            or bound.get("rules_version") in _UNPATTERNED)

def _is_exploration_sample(ident: str, k: int) -> bool:
    """1-in-K deterministic exploration sampling, keyed on the candidate id.

    Deterministic (not random) so the cadence is testable and a re-run of the
    same fold makes the same choice. ``k<=0`` disables exploration entirely;
    ``k==1`` explores everything (i.e. graduation is inert)."""
    if k <= 0:
        return False
    if k == 1:
        return True
    digest = hashlib.sha256(f"cos-explore:{ident}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % k == 0

def route_decision(vault, bound: dict[str, Any], *,
                   now: _dt.datetime | None = None,
                   taxonomy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply the both-keys policy to ONE host-bound candidate meta.

    ``bound`` is the HOST-recorded sidecar (``pending/<id>.json`` or an
    attachment meta) — never the VM-authored frontmatter."""
    category = bound.get("category") or CATEGORY_UNCLASSIFIED
    lane = bound.get("lane") or LANE_TEXT
    pattern_ok, pattern_stats_ = auto_capture_eligible(
        vault, bound.get("pattern"), bound.get("bundle_version"))
    category_ok, category_stats_ = category_eligible(
        vault, category=category, lane=lane, tier=bound.get("tier"),
        rules_version=bound.get("rules_version"), now=now, taxonomy=taxonomy)
    out = {"lane": lane, "category": category,
           "pattern": {"eligible": pattern_ok, **pattern_stats_},
           "category_gate": {"eligible": category_ok, **category_stats_}}
    if not pattern_ok or not category_ok:
        out["decision"] = "batch"
        out["reason"] = ("pattern: " + str(pattern_stats_.get("reason"))
                         + " | category: " + str(category_stats_.get("reason")))
        return out
    k = int(category_stats_.get("config", {}).get(
        "exploration_k", DEFAULT_AUTOCAP_EXPLORATION_K))
    # Keyed on the HOST-BOUND content sha, not the producer-chosen id (medium
    # finding 6): same determinism, but a producer can no longer choose ids
    # that miss the exploration bucket and so never be sampled back through
    # the owner batch — which would leave the post-graduation accept-rate
    # window seeing only material the loop already agreed with.
    if _is_exploration_sample(str(bound.get("sha256") or bound.get("id") or ""), k):
        out["decision"] = "batch"
        out["reason"] = f"exploration-sample (1-in-{k})"
        out["exploration"] = True
        return out
    out["decision"] = "auto"
    out["reason"] = "both keys eligible"
    return out

__all__ = ['_route_stats_path', 'route_stats', '_bump_route_stats', '_stamp_missing', '_is_exploration_sample', 'route_decision']
