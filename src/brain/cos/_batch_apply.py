"""COS batch-decision application."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._approval import approved_staged, stage_approved
from ._attachment_acceptance import _accept_attachment
from ._attachment_store import _discard_attachment, attachment_metas
from ._claims_state import _bound_meta
from ._guards import _safe_meta_id
from ._layout import _ts, host_dir, proposals_dir
from ._learning_ledger import _record_verdict, log_defect
from ._spine import _spine_ingest_commitment
from ._version_apply import _apply_version_link
from ._version_links import version_link_metas


def _safe_batch_ids(batch: dict[str, Any], report: dict[str, Any]) -> list[str]:
    """Validate batch candidate identities."""
    batch_id = batch.get("batch_id", "")
    ids: list[str] = []
    for candidate in batch.get("candidates", []):
        candidate_id = _safe_meta_id(candidate)
        if candidate_id is None:
            report["invalid"].append(
                {"batch_id": batch_id, "id": str(candidate.get("id"))[:60],
                 "reason": "candidate id is not a bare slug — not applied"})
            continue
        ids.append(candidate_id)
    return ids


def _attachment_decision(
    vault, attachment: dict[str, Any], candidate_id: str, *, accepted: bool,
    expected_sha: str | None, expected_name: str | None, batch_id: str,
    answer_mode: str, batch_size: int, now: _dt.datetime, stamp: str,
    report: dict[str, Any],
) -> None:
    """Apply one attachment verdict."""
    if not accepted:
        _record_verdict(vault, attachment, outcome="rejected", answer_mode=answer_mode,
                        batch_size=batch_size, ts=stamp)
        _discard_attachment(vault, attachment)
        report["rejected"].append(candidate_id)
        report.setdefault("attachments_rejected", []).append(candidate_id)
        return
    try:
        dest = _accept_attachment(vault, attachment, expected_sha=expected_sha,
                                  expected_name=expected_name, batch_id=batch_id, now=now)
    except (ApprovedKeyUnavailable, config.HostPathUnsafe) as exc:
        report.setdefault("systemic_error", []).append(
            {"batch_id": batch_id, "id": candidate_id,
             "reason": f"attachment NOT released — host-wide failure "
                       f"({type(exc).__name__}: {exc})"})
        log_defect(vault, "attachment-release-refused",
                   f"{candidate_id}: {type(exc).__name__}: {exc}", ts=stamp)
        return
    except Exception as exc:  # noqa: BLE001 — fail closed, keep the file
        report["invalid"].append(
            {"batch_id": batch_id, "id": candidate_id,
             "reason": f"attachment NOT released ({type(exc).__name__}: {exc})"})
        return
    _record_verdict(vault, attachment, outcome="accepted", answer_mode=answer_mode,
                    batch_size=batch_size, ts=stamp)
    report["accepted"].append(candidate_id)
    report.setdefault("attachments_accepted", []).append({"id": candidate_id, "dest": dest})


def _accepted_text_decision(
    vault, candidate_id: str, *, expected_sha: str | None, batch_id: str,
    answer_mode: str, batch_size: int, now: _dt.datetime, stamp: str,
    pending: Path, report: dict[str, Any],
) -> None:
    """Promote one accepted text candidate."""
    source = pending / f"{candidate_id}.md"
    meta_path = pending / f"{candidate_id}.json"
    if approved_staged(vault, candidate_id):
        report["accepted"].append(candidate_id)
        source.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        return
    body = source.read_text(encoding="utf-8") if source.exists() else ""
    if not body or sha256_text(body) != expected_sha:
        report["invalid"].append(
            {"batch_id": batch_id, "id": candidate_id,
             "reason": "pending file missing or content drifted since batch digest — not promoted"})
        return
    bound = _bound_meta(vault, candidate_id, body=body)
    meta, _ = frontmatter.parse_text(body)
    _record_verdict(vault, bound, outcome="accepted", answer_mode=answer_mode,
                    batch_size=batch_size, ts=stamp)
    sign_as_note = True
    if meta.get("kind") == "commitment":
        try:
            sign_as_note = _spine_ingest_commitment(vault, meta, source_ref=candidate_id, now=now)
        except Exception as exc:  # noqa: BLE001 — never block acceptance
            report.setdefault("spine_errors", []).append(
                {"id": candidate_id, "reason": f"{type(exc).__name__}: {exc}"})
    if sign_as_note:
        try:
            stage_approved(vault, candidate_id, body, sha256_hex=expected_sha or "",
                           batch_id=batch_id, now=now)
        except Exception as exc:  # noqa: BLE001 — fail closed, keep the file
            report["invalid"].append(
                {"batch_id": batch_id, "id": candidate_id,
                 "reason": f"approved queue unavailable, NOT promoted "
                           f"({type(exc).__name__}: {exc}) — the candidate stays pending for the next batch"})
            return
        source.unlink(missing_ok=True)
        report["accepted"].append(candidate_id)
    else:
        evidence_dir = host_dir(vault) / "spine-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), evidence_dir / f"{candidate_id}.md")
        report.setdefault("accepted_spine_only", []).append(candidate_id)
    meta_path.unlink(missing_ok=True)


def _rejected_text_decision(
    vault, candidate_id: str, *, answer_mode: str, batch_size: int,
    now: _dt.datetime, stamp: str, pending: Path, rejected: Path,
    report: dict[str, Any],
) -> None:
    """Archive one rejected text candidate."""
    source = pending / f"{candidate_id}.md"
    meta_path = pending / f"{candidate_id}.json"
    if source.exists():
        body = source.read_text(encoding="utf-8")
        bound = _bound_meta(vault, candidate_id, body=body)
        _record_verdict(vault, bound, outcome="rejected", answer_mode=answer_mode,
                        batch_size=batch_size, ts=stamp)
        rejected.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), rejected / f"{now.strftime('%Y%m%dT%H%M%S')}-{candidate_id}.md")
    meta_path.unlink(missing_ok=True)
    report["rejected"].append(candidate_id)


def _apply_batch_decision(core, batch: dict[str, Any], accepted_ids: list[str],
                          answer_mode: str, now: _dt.datetime,
                          report: dict[str, Any]) -> None:
    """Apply one verified batch decision."""
    vault = core.vault
    batch_id = batch.get("batch_id", "")
    candidate_ids = _safe_batch_ids(batch, report)
    sha_by_id = {candidate["id"]: candidate["sha256"] for candidate in batch.get("candidates", [])}
    name_by_id = {candidate["id"]: candidate.get("name") for candidate in batch.get("candidates", [])}
    attachments = {meta["id"]: meta for meta in attachment_metas(vault)}
    version_links = {meta["id"]: meta for meta in version_link_metas(vault)}
    pending = proposals_dir(vault) / "pending"
    rejected = proposals_dir(vault) / "rejected"
    stamp = _ts(now)
    for candidate_id in candidate_ids:
        if (version_link := version_links.get(candidate_id)) is not None:
            _apply_version_link(core, version_link, accepted=candidate_id in accepted_ids,
                                expected_sha=sha_by_id.get(candidate_id), answer_mode=answer_mode,
                                batch_size=len(candidate_ids), batch_id=batch_id, now=now, report=report)
        elif (attachment := attachments.get(candidate_id)) is not None:
            _attachment_decision(
                vault, attachment, candidate_id, accepted=candidate_id in accepted_ids,
                expected_sha=sha_by_id.get(candidate_id), expected_name=name_by_id.get(candidate_id),
                batch_id=batch_id, answer_mode=answer_mode, batch_size=len(candidate_ids),
                now=now, stamp=stamp, report=report)
        elif candidate_id in accepted_ids:
            _accepted_text_decision(
                vault, candidate_id, expected_sha=sha_by_id.get(candidate_id), batch_id=batch_id,
                answer_mode=answer_mode, batch_size=len(candidate_ids), now=now, stamp=stamp,
                pending=pending, report=report)
        else:
            _rejected_text_decision(
                vault, candidate_id, answer_mode=answer_mode, batch_size=len(candidate_ids), now=now,
                stamp=stamp, pending=pending, rejected=rejected, report=report)


__all__ = ["_apply_batch_decision"]
