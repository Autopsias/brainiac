"""Evaluate golden-probe cases."""
from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from typing import Any, Optional


_SOURCE: dict[str, Any] | None = None


def configure(source: dict[str, Any]) -> None:
    """Bind the facade's CLI helpers and probe policy constants."""
    global _SOURCE
    _SOURCE = source


class _SourceProxy:
    """Resolve probe helpers from the facade after configuration."""

    def __getattr__(self, name: str) -> Any:
        if _SOURCE is None:
            raise RuntimeError("golden-probe facade has not configured its case seam")
        return _SOURCE[name]


source = _SourceProxy()
Call = Callable[[list[Any]], tuple[Any, ...]]


def _claims_of(probe: dict[str, Any]) -> list[str]:
    """Return validated lowercase claim fragments."""
    raw = probe.get("claim_any")
    if raw is None:
        return []
    if not isinstance(raw, list) or any(
        not isinstance(claim, str) or not claim.strip() for claim in raw
    ):
        raise source.ProbeInvalid("claim_any must be a list of non-empty strings")
    return [claim.strip().lower() for claim in raw]


def _resolve_probe_anchor(
    call: Call,
    anchor: str | None,
    claims: list[str],
    max_tier: Optional[str],
) -> str | None:
    """Resolve a live anchor, falling back only for a missing retired id."""
    if not anchor:
        return None
    try:
        head_id, _, _ = source._chain_head(call, anchor, max_tier)
        return head_id
    except source.ProbeInvalid as exc:
        if not claims or getattr(exc, "kind", "config") != "missing_anchor":
            raise
        return None


def _find_decision_claim(
    call: Call,
    decisions: list[dict[str, Any]],
    claims: list[str],
    max_tier: Optional[str],
) -> str | None:
    """Find a claim in decision snippets or fetched decision bodies."""
    for decision in decisions:
        haystack = (
            str(decision.get("title", ""))
            + " "
            + str(decision.get("snippet", ""))
        ).lower()
        if any(claim in haystack for claim in claims):
            return f"decision layer carries the claim (in {decision.get('id')})"
    body_fetch_transient = False
    for decision in decisions:
        try:
            status, note = source._get_note(call, decision.get("id", ""), max_tier)
        except source.ProbeTransient:
            body_fetch_transient = True
            continue
        if status == "ok" and any(
            claim in str(note.get("body", "")).lower() for claim in claims
        ):
            return f"decision layer carries the claim (in {decision.get('id')})"
    if body_fetch_transient:
        raise source.ProbeTransient(
            "a candidate decision's body fetch was transient; the claim match "
            "is undetermined — retry rather than assert a regression"
        )
    return None


def _probe_decision_state(
    call: Call,
    probe: dict[str, Any],
    *,
    k: int,
    max_tier: Optional[str],
) -> str:
    """Check that a decision anchor or claim reaches the decision layer."""
    claims = _claims_of(probe)
    anchor = probe.get("anchor_id")
    if not claims and not anchor:
        raise source.ProbeInvalid("decision_state probe needs anchor_id and/or claim_any")
    head_id = _resolve_probe_anchor(call, anchor, claims, max_tier)

    rc, result = source._cli_json(
        call,
        ["dossier", probe["query"], "--json", "-k", str(k)]
        + source._tier_args(max_tier),
    )
    if rc != 0:
        raise source.ProbeTransient(f"dossier returned rc={rc}")
    decisions = result.get("decisions") or []
    withheld = (result.get("egress") or {}).get("withheld", 0)
    starvation = f"; {withheld} note(s) withheld by egress — starvation?" if withheld else ""
    if head_id is not None:
        if any(decision.get("id") == head_id for decision in decisions):
            return f"decision layer surfaced anchor {head_id}"
        raise source.ProbeFail(
            f"anchor {head_id} resolved but is NOT in the top-{k} decision "
            f"layer ({len(decisions)} hit(s)) — crowded out?{starvation}"
        )

    claim_match = _find_decision_claim(call, decisions, claims, max_tier)
    if claim_match is not None:
        return claim_match
    raise source.ProbeFail(
        f"decision layer ({len(decisions)} hit(s)) lacks the expected "
        f"decision (claims={claims}){starvation}"
    )


def _probe_currency(
    call: Call,
    probe: dict[str, Any],
    *,
    k: int,
    max_tier: Optional[str],
) -> str:
    """Check that an anchored supersession chain resolves to a live head."""
    head_id, head, hops = source._chain_head(call, probe["anchor_id"], max_tier)
    latest = str(head.get("is_latest_version") or "").lower()
    if latest == "false":
        raise source.ProbeFail(
            f"version-chain HEAD {head_id} is retired "
            f"(is_latest_version: false) with no successor — stale HEAD"
        )
    via = f" (followed {hops} supersession hop(s))" if hops else ""
    return f"chain HEAD {head_id} is current{via}"


def _probe_freshness(
    call: Call,
    probe: dict[str, Any],
    *,
    k: int,
    max_tier: Optional[str],
) -> str:
    """Check that a recent note is reachable through the gated surface."""
    max_age = source._coerce_num(probe["max_age_days"], "max_age_days", integer=True, minimum=0)
    rc, result = source._cli_json(
        call, ["recent", "--json", "-n", "200"] + source._tier_args(max_tier)
    )
    if rc != 0:
        raise source.ProbeTransient(f"recent returned rc={rc}")
    items = result.get("results") or []
    if not items:
        raise source.ProbeFail("`brain recent` surfaced nothing — empty index or total egress starvation")
    today_utc = _dt.datetime.now(_dt.timezone.utc).date()
    dated: list[tuple[_dt.date, dict[str, Any]]] = []
    for item in items:
        try:
            date = _dt.date.fromisoformat(str(item.get("updated", ""))[:10])
        except ValueError:
            continue
        if date <= today_utc:
            dated.append((date, item))
    if not dated:
        raise source.ProbeFail(
            "no non-future parseable `updated` date among recent notes "
            "(empty index, or all newest notes are future-dated — check the clock)"
        )
    newest_date, newest = max(dated, key=lambda item: item[0])
    age = (today_utc - newest_date).days
    if age > max_age:
        raise source.ProbeFail(
            f"newest indexed note is {age}d old (> {max_age}d) — "
            f"sweep/ingest death? (newest: {newest.get('id')})"
        )
    return f"newest note {newest.get('id')} is {age}d old (within {max_age}d)"


def _probe_tension(
    call: Call,
    probe: dict[str, Any],
    *,
    k: int,
    max_tier: Optional[str],
) -> str:
    """Check that an anchored decision reports its newer-source tension."""
    head_id, _, _ = source._chain_head(call, probe["anchor_id"], max_tier)
    rc, result = source._cli_json(
        call,
        ["dossier", probe["query"], "--json", "-k", str(k)]
        + source._tier_args(max_tier),
    )
    if rc != 0:
        raise source.ProbeTransient(f"dossier returned rc={rc}")
    match = next(
        (decision for decision in result.get("decisions") or [] if decision.get("id") == head_id),
        None,
    )
    if match is None:
        raise source.ProbeFail(
            f"decision {head_id} absent from the dossier decision layer — cannot evaluate tensions"
        )
    tensions = match.get("tensions") or []
    if not tensions:
        raise source.ProbeFail(
            f"decision {head_id} carries NO tension flag despite an expected newer source — "
            "proposal-promotion guard is blind"
        )
    wanted = source._clean_link(probe.get("expect_source_id")) if probe.get("expect_source_id") else None
    if wanted and all(source._clean_link(tension.get("id")) != wanted for tension in tensions):
        raise source.ProbeFail(
            f"tension list on {head_id} lacks expected source {wanted} "
            f"(has: {[tension.get('id') for tension in tensions]})"
        )
    return f"decision {head_id} carries {len(tensions)} tension flag(s)"


_PROBE_FNS: dict[str, Callable[..., str]] = {
    "decision_state": _probe_decision_state,
    "currency": _probe_currency,
    "freshness": _probe_freshness,
    "tension": _probe_tension,
}


def _validate_probe(probe: Any) -> str:
    """Validate one probe and return its registered class."""
    if not isinstance(probe, dict):
        raise source.ProbeInvalid("probe entry is not an object")
    probe_class = probe.get("class")
    if probe_class not in source.PROBE_CLASSES:
        raise source.ProbeInvalid(f"unknown probe class: {probe_class!r}")
    missing = [
        key for key in source._REQUIRED_KEYS[probe_class]
        if key not in probe or probe[key] is None
    ]
    if missing:
        raise source.ProbeInvalid(f"{probe_class} probe missing required key(s): {missing}")
    return probe_class


def _evaluate_probe(
    probe: Any,
    call: Call,
    *,
    k: int,
    max_tier: Optional[str],
) -> tuple[dict[str, Any], str, float]:
    """Run one probe and contain its deterministic/transient outcome."""
    probe_id = probe.get("id", "?") if isinstance(probe, dict) else "?"
    weight = 1.0
    try:
        probe_class = _validate_probe(probe)
        weight = source._coerce_num(
            probe.get("weight", 1.0), "weight", minimum=0.0, exclusive_min=True
        )
        reason = _PROBE_FNS[probe_class](call, probe, k=k, max_tier=max_tier)
        status = "pass"
    except source.ProbeFail as exc:
        status, reason = "fail", str(exc)
    except source.ProbeInvalid as exc:
        status, reason = "invalid", str(exc)
    except source.ProbeTransient as exc:
        status, reason = "transient", str(exc)
    except Exception as exc:  # noqa: BLE001
        status, reason = "invalid", f"unexpected: {type(exc).__name__}: {exc}"
    return {
        "id": probe_id,
        "class": probe.get("class", "?") if isinstance(probe, dict) else "?",
        "status": status,
        "weight": weight,
        "reason": reason,
    }, status, weight


def run_probes(
    spec: dict[str, Any],
    call: Call,
    *,
    threshold: Optional[float] = None,
    k: int = 12,
    max_tier: Optional[str] = None,
) -> dict[str, Any]:
    """Execute every probe and return its scored disposition document."""
    probes = spec.get("probes")
    if not isinstance(probes, list) or not probes:
        return {
            "error": "invalid_probes_file",
            "detail": "probes file has no `probes` list",
            "disposition": "action_required",
            "score": None,
            "probes": [],
            "exit_code": source.EXIT_ACTION_REQUIRED,
        }

    def config_error(detail: str) -> dict[str, Any]:
        return {
            "error": "invalid_config",
            "detail": detail,
            "disposition": "action_required",
            "score": None,
            "probes": [],
            "exit_code": source.EXIT_ACTION_REQUIRED,
        }

    if max_tier is not None and max_tier not in source.VALID_TIERS:
        return config_error(
            f"max_tier {max_tier!r} is not one of {list(source.VALID_TIERS)}"
        )
    try:
        threshold = source._coerce_num(
            spec.get("threshold", 1.0) if threshold is None else threshold,
            "threshold",
            minimum=0.0,
            exclusive_min=True,
            maximum=1.0,
        )
        k = source._coerce_num(k, "k", integer=True, minimum=1)
    except source.ProbeInvalid as exc:
        return config_error(str(exc))

    results: list[dict[str, Any]] = []
    weight_total = 0.0
    weight_passed = 0.0
    counts = {"pass": 0, "fail": 0, "invalid": 0, "transient": 0}
    for probe in probes:
        result, status, weight = _evaluate_probe(
            probe, call, k=k, max_tier=max_tier,
        )
        results.append(result)
        counts[status] += 1
        if status in ("pass", "fail"):
            weight_total += weight
            if status == "pass":
                weight_passed += weight

    score = round(weight_passed / weight_total, 4) if weight_total > 0 else None
    if score is not None and score < threshold - 1e-9:
        disposition, exit_code = "regression", source.EXIT_REGRESSION
    elif counts["invalid"]:
        disposition, exit_code = "action_required", source.EXIT_ACTION_REQUIRED
    elif counts["transient"]:
        disposition, exit_code = "transient", source.EXIT_TRANSIENT
    elif score is None:
        disposition, exit_code = "action_required", source.EXIT_ACTION_REQUIRED
    else:
        disposition, exit_code = "ok", source.EXIT_OK

    return {
        "score": score,
        "threshold": threshold,
        "disposition": disposition,
        "exit_code": exit_code,
        "counts": counts,
        "probes": results,
        "captured": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "baseline": spec.get("baseline"),
    }
