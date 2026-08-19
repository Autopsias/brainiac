"""COS proposal-claim evaluation."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._claims_state import _quarantined
from ._criteria import evidence_lineage_key, evidence_unit_key
from ._layout import _ts, proposals_dir
from ._learning_ledger import demote_category, log_defect, record_outcome
from ._ledger_join import join_ledger_category
from ._run_migration import run_manifest
from ._runs import run_validity
from ._taxonomy import resolve_category


def _candidate_context(vault, cap_mod, *, text: str, sha: str, source: str,
                       taxonomy: dict[str, Any],
                       ledger_idx: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    """Derive claim inputs from sanitized candidate text."""
    reasons = list(cap_mod.validate(text))
    secrets = secret_findings(text)
    if secrets:
        reasons.append("secret-scrub: " + ", ".join(secrets))
    meta, body = frontmatter.parse_text(text)
    meta = provenance.without_host_only(meta, keys=_STRIPPED_CLAIM_KEYS)
    try:
        candidate_id = safe_slug(meta.get("id") or Path(source).stem)
    except ValueError as exc:
        candidate_id = None
        reasons.append(f"unsafe id: {exc}")
    join = (join_ledger_category(ledger_idx, candidate_id, sha) if candidate_id else
            {"status": QUARANTINE_NO_LEDGER,
             "reason": "candidate carries no usable id to join a ledger row by"})
    run_id = join.get("run_id")
    manifest = run_manifest(vault, run_id) if join["status"] == "joined" else None
    category, disposition = resolve_category(
        vault, join.get("category"), lane=LANE_TEXT, taxonomy=taxonomy)
    tier, _ = provenance.email_classification(
        vault, proposed=meta.get("classification"), category=category)
    return {
        "reasons": reasons, "secrets": secrets, "meta": meta, "body": body,
        "id": candidate_id, "join": join, "run_id": run_id, "manifest": manifest,
        "category": category, "disposition": disposition, "tier": tier,
        "bundle_version": (manifest or {}).get("bundle_version"),
        "rules_version": (manifest or {}).get("extraction_rules_version"),
    }


def _content_rejection(vault, context: dict[str, Any], *, source: str,
                       now: _dt.datetime) -> dict[str, Any] | None:
    """Record candidate-content refusals."""
    reasons = context["reasons"]
    if context["disposition"] == DISPOSITION_NEVER:
        reasons.append(f"category {context['category']!r} is a never-ingest category "
                       "(overlay cos/ingest.md)")
        log_defect(vault, "never-category-proposed",
                   f"{source}: category={context['category']}", ts=_ts(now))
    if context["secrets"]:
        record_outcome(
            vault, pattern=context["meta"].get("pattern"),
            ident=context["id"] or Path(source).stem, outcome="claim-rejected-security",
            bundle_version=context["bundle_version"], ts=_ts(now),
            category=context["category"], lane=LANE_TEXT, tier=context["tier"],
            rules_version=context["rules_version"])
        demote_category(vault, context["category"],
                        reason=f"claim-rejected-security: {source}", ts=_ts(now))
    pending = proposals_dir(vault) / "pending"
    if context["id"] and not reasons and (pending / f"{context['id']}.md").exists():
        reasons.append(f"duplicate pending id: {context['id']!r}")
    if reasons:
        return {"state": "rejected", "id": context["id"], "reason": "; ".join(reasons)}
    return None


def _run_gate(vault, context: dict[str, Any], *, text: str, sha: str,
              source: str, now: _dt.datetime) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Prove run authority before a claim is admitted."""
    candidate_id = context["id"]
    join = context["join"]
    run_id = context["run_id"]
    if join["status"] != "joined":
        code = (QUARANTINE_NO_LEDGER if join["status"] == QUARANTINE_NO_LEDGER
                else f"ledger-{join['status']}")
        return _quarantined(vault, nid=candidate_id, text=text, sha=sha, code=code,
                            reason=join["reason"], run_id=run_id, now=now, source=source), None
    manifest = context["manifest"]
    if manifest is None:
        return _quarantined(
            vault, nid=candidate_id, text=text, sha=sha, code=QUARANTINE_NO_MANIFEST,
            reason=(f"run {run_id} has no host run manifest — the host never recorded which "
                    "bundle produced it, so its version stamps cannot be derived "
                    "(`brain cos-run-begin` writes one at run launch)"),
            run_id=run_id, now=now, source=source), None
    verdict = run_validity(vault, run_id)
    if verdict["verdict"] not in CLAIMABLE_VERDICTS:
        return _quarantined(
            vault, nid=candidate_id, text=text, sha=sha,
            code=f"run-{verdict['verdict'].lower()}",
            reason=(f"run {run_id} is {verdict['verdict']}: "
                    f"{verdict.get('reason') or 'no reason recorded'}"),
            run_id=run_id, now=now, source=source), None
    return None, verdict


def _write_claim(vault, context: dict[str, Any], verdict: dict[str, Any], *,
                 text: str, sha: str, now: _dt.datetime,
                 ttl_days: int) -> dict[str, Any]:
    """Persist one verified pending-claim record."""
    candidate_id = context["id"]
    pending = proposals_dir(vault) / "pending"
    public("_write_atomic")(pending / f"{candidate_id}.md", text.encode("utf-8"))
    public("_write_atomic")(pending / f"{candidate_id}.json", json.dumps({
        "id": candidate_id, "sha256": sha, "claimed": _ts(now),
        "ttl_expires": _ts(now + _dt.timedelta(days=ttl_days)), "state": "pending",
        "category": context["category"], "disposition": context["disposition"],
        "lane": LANE_TEXT, "tier": context["tier"],
        "rules_version": context["rules_version"], "pattern": context["meta"].get("pattern"),
        "bundle_version": context["bundle_version"], "kind": context["meta"].get("kind"),
        "run_id": context["run_id"],
        "skill_sha256": context["manifest"].get("skill_sha256"),
        "run_verdict": verdict["verdict"],
        "evidence_unit": evidence_unit_key(
            category=context["category"], lane=LANE_TEXT,
            rules_version=context["rules_version"], body=context["body"]),
        "evidence_lineage": evidence_lineage_key(
            category=context["category"], lane=LANE_TEXT,
            rules_version=context["rules_version"],
            conversation_id=context["meta"].get("provenance.conversation_id"),
            verified=bool(context["meta"].get(provenance.VERIFIED_KEY))),
    }, sort_keys=True).encode("utf-8") + b"\n")
    return {"state": "claimed", "id": candidate_id,
            "reason": context["join"]["reason"], "run_id": context["run_id"],
            "category": context["category"]}


def _bind_claim(vault, cap_mod, *, text: str, sha: str, source: str,
                now: _dt.datetime, taxonomy: dict[str, Any],
                ledger_idx: dict[str, list[dict[str, str]]],
                ttl_days: int) -> dict[str, Any]:
    """Validate and bind one sanitized proposal candidate."""
    context = _candidate_context(
        vault, cap_mod, text=text, sha=sha, source=source, taxonomy=taxonomy,
        ledger_idx=ledger_idx)
    if (rejected := _content_rejection(vault, context, source=source, now=now)) is not None:
        return rejected
    quarantined, verdict = _run_gate(
        vault, context, text=text, sha=sha, source=source, now=now)
    if quarantined is not None:
        return quarantined
    return _write_claim(vault, context, verdict, text=text, sha=sha, now=now, ttl_days=ttl_days)


__all__ = ["_bind_claim"]
