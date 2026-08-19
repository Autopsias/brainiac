"""COS version-link operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._criteria import evidence_lineage_key
from ._guards import _safe_meta_id
from ._io import _append_jsonl, _read_jsonl, _write_atomic
from ._layout import _env_days, _parse_ts, _ts, _utcnow, proposals_dir

def version_links_dir(vault=None) -> Path:
    return proposals_dir(vault) / "version-links"

def _version_link_pending(vault=None) -> Path:
    return version_links_dir(vault) / "pending"

def _version_link_expired(vault=None) -> Path:
    return version_links_dir(vault) / "expired"

def _version_ledger_path(vault=None) -> Path:
    return version_links_dir(vault) / "ledger.jsonl"

def _version_runs_path(vault=None) -> Path:
    return version_links_dir(vault) / "runs.jsonl"

def version_link_runs(vault) -> list[dict[str, Any]]:
    return _read_jsonl(_version_runs_path(vault))

def version_link_digest(meta: dict[str, Any]) -> str:
    """The content identity of ONE version-link proposal — what the batch's
    signed digest actually binds. Covers both notes' hashes AND the signals the
    owner was shown, so a rewritten proposal can never ride an old approval."""
    return sha256_text(json.dumps(
        {k: meta.get(k) for k in
         ("old_id", "new_id", "old_sha256", "new_sha256", "signals")},
        sort_keys=True, separators=(",", ":"), default=str))

def version_link_metas(vault) -> list[dict[str, Any]]:
    """Version-link proposals awaiting an owner answer."""
    d = _version_link_pending(vault)
    out: list[dict[str, Any]] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nid = _safe_meta_id(m)
        if nid and isinstance(m, dict) and m.get("kind") == KIND_SUPERSEDE:
            out.append({**m, "id": nid})
    return out

def version_link_ledger(vault) -> list[dict[str, Any]]:
    return _read_jsonl(_version_ledger_path(vault))

def decided_pair_keys(vault) -> set[str]:
    """Every pair this vault has already ruled on, in EITHER direction —
    proposed-and-waiting, rejected, applied, declined-ambiguous, gone stale or
    expired unanswered. A pair in here is never generated again: re-asking a
    question the owner already answered (or pointedly did not) is nagging."""
    return {str(e.get("pair_key")) for e in version_link_ledger(vault)
            if e.get("pair_key")}

def _record_version_link(vault, pair_key: str, state: str, **extra: Any) -> dict[str, Any]:
    rec = {"pair_key": pair_key, "state": state, "ts": extra.pop("ts", None) or _ts()}
    rec.update(provenance.scrub(extra))
    _append_jsonl(_version_ledger_path(vault), rec, vault=vault)
    return rec

def _pair_tier(*classifications: str) -> str:
    """The MOST RESTRICTIVE of the pair's tiers — the evidence key a
    version-link verdict counts against. Evidence gathered on Internal material
    must never authorize the same move on Restricted material (LRN-01's
    exact-tier keying), so the pair takes its higher side."""
    from ..classification import RANK

    best = ""
    for c in classifications:
        c = str(c or "").strip()
        if c in RANK and (not best or RANK[c] > RANK[best]):
            best = c
    return best or "unknown"

def version_link_fold(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """VER-01: deduce version links over recently committed sources and stage
    each as a propose-only candidate. HOST-broker only (the caller — the COS
    broker fold — already required host); writes under the writer lock."""
    core._require_host("generate version-link proposals")
    now = now or _utcnow()
    with vault_writer_lock(core.vault, verb="cos-version-links"):
        return _version_link_fold_locked(core, now)

def _version_link_fold_locked(core, now: _dt.datetime) -> dict[str, Any]:
    from .. import versionlink as vl

    vault = core.vault
    cutoff = (now - _dt.timedelta(days=vl.window_days())).date().isoformat()
    res = vl.generate(core, cutoff=cutoff, exclude=decided_pair_keys(vault))
    report: dict[str, Any] = {
        "proposed": [], "declined": [], "by_class": {},
        "pairs_examined": res["pairs_examined"], "truncated": res["truncated"],
        "min_similarity": vl.min_similarity(),
    }
    for amb in res["ambiguous"]:
        # Declined, logged, never proposed — mirrors auto_version_chains'
        # skipped_ambiguous: an engine that cannot derive the order says so.
        _record_version_link(vault, amb["pair_key"], "declined", ts=_ts(now),
                             old_id=amb["old_id"], new_id=amb["new_id"],
                             reason=amb["reason"], signals=amb["signals"])
        report["declined"].append({"old_id": amb["old_id"], "new_id": amb["new_id"],
                                   "reason": amb["reason"]})

    pdir = _version_link_pending(vault)
    ttl_days = _env_days(PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS)
    for cand in res["candidates"]:
        meta: dict[str, Any] = {
            "kind": KIND_SUPERSEDE,
            "lane": LANE_TEXT,
            "category": vl.CATEGORY,
            "rules_version": vl.RULES_VERSION,
            "tier": _pair_tier(cand["old_classification"], cand["new_classification"]),
            "old_id": cand["old_id"], "new_id": cand["new_id"],
            "old_title": cand["old_title"], "new_title": cand["new_title"],
            "old_sha256": cand["old_sha256"], "new_sha256": cand["new_sha256"],
            "pair_key": cand["pair_key"],
            # Item 7: the signals are owner-facing output — scrubbed like every
            # other serialization surface (a subject can carry a secret).
            "signals": provenance.scrub(cand["signals"]),
            "created": _ts(now),
            "ttl_expires": _ts(now + _dt.timedelta(days=ttl_days)),
        }
        # B5, same policy as the ingestion lane: ONE host-verified conversation
        # is ONE evidence unit. A thread that carries five successive versions
        # is five owner questions but a single counted verdict — otherwise a
        # chatty counterparty alone could walk this class toward graduation.
        lineage = evidence_lineage_key(
            category=vl.CATEGORY, lane=LANE_TEXT, rules_version=vl.RULES_VERSION,
            conversation_id=meta["signals"].get("conversation"), verified=True)
        if lineage:
            meta["evidence_lineage"] = lineage
        meta["sha256"] = version_link_digest(meta)
        meta["id"] = "vlink-" + meta["sha256"][:12]
        pdir.mkdir(parents=True, exist_ok=True)
        public("_write_atomic")(pdir / f"{meta['id']}.json",
                      json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"))
        _record_version_link(vault, cand["pair_key"], "proposed", ts=_ts(now),
                             id=meta["id"], old_id=cand["old_id"],
                             new_id=cand["new_id"], signals=meta["signals"])
        report["proposed"].append(meta["id"])
        klass = str(meta["signals"].get("family_class") or "unknown")
        report["by_class"][klass] = report["by_class"].get(klass, 0) + 1

    # CUR-01: the coverage metric + the engagement line, EVERY run — including
    # the runs that proposed nothing, which is exactly when a silently-dead
    # fold looks identical to a healthy one.
    pending = version_link_metas(vault)
    report["coverage"] = {
        **vl.coverage(core.index.conn),
        # Kept SEPARATE from `linked` on purpose (see versionlink.coverage):
        # a note sitting in an unanswered proposal is not a covered note.
        "family_members_unresolved": len(
            {m[k] for m in pending for k in ("old_id", "new_id") if m.get(k)}),
        "proposals_awaiting_owner": len(pending),
    }
    _append_jsonl(_version_runs_path(vault), {
        "event": VERSION_LINK_RUN_EVENT, "ts": _ts(now),
        "proposed": len(report["proposed"]), "declined": len(report["declined"]),
        "by_class": report["by_class"],
        "pairs_examined": report["pairs_examined"],
        "truncated": report["truncated"], **report["coverage"]}, vault=vault)
    return report

def _expire_version_links(vault, now: _dt.datetime) -> list[str]:
    """TTL-expire unanswered version-link proposals (caller holds the lock).

    NOT a verdict — no outcome is recorded, exactly like an expired ingestion
    proposal. The pair stays in the ledger as decided, so an ignored supersede
    question is asked once and then dropped rather than re-offered every night
    (and never permanently occupying one of the four supersede slots)."""
    expired: list[str] = []
    for m in version_link_metas(vault):
        exp = _parse_ts(m.get("ttl_expires", ""))
        if not (exp and exp <= now):
            continue
        dest = _version_link_expired(vault)
        dest.mkdir(parents=True, exist_ok=True)
        src = _version_link_pending(vault) / f"{m['id']}.json"
        if src.exists():
            shutil.move(str(src), dest / src.name)
        _record_version_link(vault, str(m.get("pair_key") or m["id"]), "expired",
                             ts=_ts(now), id=m["id"])
        expired.append(m["id"])
    return expired

def _version_link_stale(core, meta: dict[str, Any]) -> str | None:
    """Why this pair can no longer be applied, or ``None``."""
    for side, want in (("old", meta["old_sha256"]), ("new", meta["new_sha256"])):
        nid = meta[f"{side}_id"]
        row = core.index.get(nid)
        if not row:
            return f"{side} note {nid!r} is no longer in the vault"
        path = Path(row["path"])
        if not path.is_absolute():
            path = Path(core.vault) / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return f"{side} note {nid!r} is unreadable"
        # Retirement first: it is the specific thing an operator needs told,
        # and it also moves the content hash (frontmatter lives in the file).
        fm_meta, _ = frontmatter.parse_text(text)
        retired = (str(fm_meta.get("superseded_by") or "").strip()
                   or str(fm_meta.get("is_latest_version", "")
                          ).strip().lower() == "false")
        if retired:
            return f"{side} note {nid!r} has already been superseded"
        if want and sha256_text(text) != want:
            return f"{side} note {nid!r} changed since the proposal was made"
    return None

__all__ = ['version_links_dir', '_version_link_pending', '_version_link_expired', '_version_ledger_path', '_version_runs_path', 'version_link_runs', 'version_link_digest', 'version_link_metas', 'version_link_ledger', 'decided_pair_keys', '_record_version_link', '_pair_tier', 'version_link_fold', '_version_link_fold_locked', '_expire_version_links', '_version_link_stale']
