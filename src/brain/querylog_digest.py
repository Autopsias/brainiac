"""Vault-fingerprint and candidate-digest helpers for the query log."""
from __future__ import annotations

from typing import Any, Iterable

def _normalise_fingerprint(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    return raw[7:] if raw.startswith("sha256:") else raw


def live_index_fingerprint(index: Any) -> str | None:
    """Read the live SQLite content fingerprint, never a VM snapshot marker."""
    try:
        raw = index.get_meta("vault_fingerprint")
    except Exception:
        return None
    norm = _normalise_fingerprint(raw)
    return f"sha256:{norm}" if norm else None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _safe_top(top: Iterable[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(top, start=1):
        if len(out) >= max(0, limit):
            break
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        score = _safe_float(item.get("pre_rerank_score"))
        final_rank = item.get("final_rank")
        if not isinstance(final_rank, int) or final_rank < 1:
            final_rank = rank
        out.append({"id": ident, "pre_rerank_score": score, "final_rank": final_rank})
    return out


def _safe_digest(digest: Any) -> dict[str, Any]:
    """Keep the bounded S03 shape without trusting a frontend object blindly."""
    if not isinstance(digest, dict):
        return empty_digest([])
    per_leg_limit = digest.get("per_leg_limit", 20)
    try:
        per_leg_limit = max(1, min(20, int(per_leg_limit)))
    except (TypeError, ValueError):
        per_leg_limit = 20

    def project(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for rank, item in enumerate(items[:per_leg_limit], start=1):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            item_rank = item.get("rank")
            out.append({"id": item["id"], "rank": item_rank if isinstance(item_rank, int) else rank})
        return out

    legs = digest.get("legs") if isinstance(digest.get("legs"), dict) else {}
    return {
        "version": 1,
        "per_leg_limit": per_leg_limit,
        "truncated": bool(digest.get("truncated", False)),
        "legs": {
            "lexical": project(legs.get("lexical")),
            "dense": project(legs.get("dense")),
            "exact": project(legs.get("exact")),
        },
        "pre_rerank": project(digest.get("pre_rerank")),
        "final": project(digest.get("final")),
    }


def _rerank_mode(rerank: dict[str, Any]) -> str:
    """Return the small, stable mode label used for traffic segmentation."""
    if bool(rerank.get("applied", False)):
        return "applied"
    if bool(rerank.get("requested", False)):
        return "requested_not_applied"
    return "disabled"


def empty_digest(ids: Iterable[str], *, per_leg_limit: int = 20) -> dict[str, Any]:
    """S03-compatible bounded digest for a dossier's composed response."""
    visible = [ident for ident in ids if isinstance(ident, str) and ident]
    limit = max(1, min(20, int(per_leg_limit)))
    final = [{"id": ident, "rank": rank} for rank, ident in enumerate(visible[:limit], start=1)]
    return {
        "version": 1,
        "per_leg_limit": limit,
        "truncated": len(visible) > limit,
        "legs": {"lexical": [], "dense": [], "exact": []},
        "pre_rerank": list(final),
        "final": final,
    }


def projection_from_gated(
    surfaced: list[dict[str, Any]],
    *,
    trace: Any | None = None,
    redacted_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the sole capture projection from already-egress-approved rows.

    Frontends hand this function only their gated result list.  When an S03
    trace is available, its score is read through the same identity-redaction
    switch used for user-facing ``--explain``; otherwise the already-surfaced
    score is the only permitted fallback.  This makes the CLI and MCP adapter
    share one post-egress serialization seam.
    """
    redacted = redacted_ids or set()
    # A trace needs set membership; a composed response (dossier) needs the
    # surfaced order. Keep both representations so its final-list digest is
    # deterministic and agrees with the returned ranking instead of inheriting
    # Python set iteration order.
    visible_ids = [item["id"] for item in surfaced if isinstance(item.get("id"), str)]
    ids = set(visible_ids)
    digest = trace.compact_digest(ids) if trace is not None else empty_digest(visible_ids)
    top: list[dict[str, Any]] = []
    for rank, item in enumerate(surfaced, start=1):
        ident = item.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        score = item.get("score")
        if trace is not None:
            try:
                explain = trace.explain_for_id(
                    ident, rank, redact_identity=ident in redacted,
                )
            except Exception:
                explain = None
            if isinstance(explain, dict):
                score = explain.get("pre_rerank_score")
        top.append({"id": ident, "pre_rerank_score": score, "final_rank": rank})
    return _safe_top(top, limit=len(surfaced)), _safe_digest(digest)
