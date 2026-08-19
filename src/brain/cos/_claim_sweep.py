"""COS proposal-drop claiming."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._claim_evaluation import _bind_claim
from ._criteria import evidence_lineage_key, evidence_unit_key
from ._ingress import _validate_correction_payload
from ._io import _append_jsonl
from ._layout import _ts, proposal_drop_dir, proposals_dir, verdict_drop_dir
from ._learning_ledger import _claims_path


def _claim_text_drops(vault, cap_mod, now: _dt.datetime, *, taxonomy: dict[str, Any],
                      ledger_idx: dict[str, list[dict[str, str]]], ttl_days: int,
                      seen_hashes: set[str], rejected_dir: Path) -> dict[str, Any]:
    """Claim proposal-markdown drops."""
    claimed: list[str] = []
    rejected: list[dict[str, str]] = []
    replayed: list[str] = []
    quarantined: list[dict[str, Any]] = []
    unjoined = 0
    drop_dir = proposal_drop_dir(vault)
    for drop in sorted(drop_dir.glob("*.md")) if drop_dir.is_dir() else []:
        if drop.is_symlink() or not drop.is_file():
            drop.unlink(missing_ok=True)
            rejected.append({"drop": drop.name, "reason": "not a regular file (symlink refused)"})
            continue
        try:
            text = drop.read_text(encoding="utf-8")
        except OSError as exc:
            rejected.append({"drop": drop.name, "reason": f"unreadable: {exc}"})
            continue
        try:
            text = provenance.without_host_only_text(text, keys=_STRIPPED_CLAIM_KEYS)
        except provenance.HostOnlyKeyResidue as exc:
            reason = f"host-only provenance forgery: {exc}"
            shutil.move(str(drop), rejected_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{drop.name}")
            rejected.append({"drop": drop.name, "reason": reason})
            _append_jsonl(_claims_path(vault), {"sha256": sha256_text(text), "drop": drop.name,
                                                "ts": _ts(now), "disposition": "rejected: " + reason}, vault=vault)
            continue
        sha = sha256_text(text)
        if sha in seen_hashes:
            drop.unlink(missing_ok=True)
            replayed.append(drop.name)
            _append_jsonl(_claims_path(vault), {"sha256": sha, "drop": drop.name,
                                                "ts": _ts(now), "disposition": "replay-rejected"}, vault=vault)
            continue
        outcome = _bind_claim(vault, cap_mod, text=text, sha=sha, source=drop.name, now=now,
                              taxonomy=taxonomy, ledger_idx=ledger_idx, ttl_days=ttl_days)
        if outcome["state"] == "rejected":
            shutil.move(str(drop), rejected_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{drop.name}")
            rejected.append({"drop": drop.name, "reason": outcome["reason"]})
            _append_jsonl(_claims_path(vault), {"sha256": sha, "drop": drop.name, "ts": _ts(now),
                                                "disposition": "rejected: " + outcome["reason"]}, vault=vault)
            continue
        drop.unlink(missing_ok=True)
        seen_hashes.add(sha)
        if outcome["state"] == "quarantined":
            quarantined.append(outcome["quarantine"])
            unjoined += int(outcome["quarantine"]["code"] == QUARANTINE_NO_LEDGER)
            _append_jsonl(_claims_path(vault), {"sha256": sha, "id": outcome["id"], "ts": _ts(now),
                                                "disposition": "quarantined: " + outcome["quarantine"]["code"]}, vault=vault)
            continue
        claimed.append(outcome["id"])
        _append_jsonl(_claims_path(vault), {"sha256": sha, "id": outcome["id"], "ts": _ts(now),
                                            "disposition": "claimed"}, vault=vault)
    return {"claimed": claimed, "rejected": rejected, "replayed": replayed,
            "quarantined": quarantined, "unjoined": unjoined}


def _claim_correction_drops(vault, now: _dt.datetime, rejected_dir: Path
                            ) -> tuple[list[str], list[dict[str, str]]]:
    """Claim correction-json drops."""
    claimed: list[str] = []
    rejected: list[dict[str, str]] = []
    drop_dir = verdict_drop_dir(vault)
    pending = proposals_dir(vault) / "corrections-pending"
    if not drop_dir.is_dir():
        return claimed, rejected
    pending.mkdir(parents=True, exist_ok=True)
    for drop in sorted(drop_dir.glob("correction-*.json")):
        if drop.is_symlink() or not drop.is_file():
            drop.unlink(missing_ok=True)
            rejected.append({"drop": drop.name, "reason": "not a regular file (symlink refused)"})
            continue
        try:
            payload = json.loads(drop.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        errors = _validate_correction_payload(payload)
        if errors:
            shutil.move(str(drop), rejected_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{drop.name}")
            rejected.append({"drop": drop.name, "reason": "; ".join(errors)})
            continue
        shutil.move(str(drop), pending / drop.name)
        claimed.append(drop.name)
    return claimed, rejected


__all__ = ["_claim_text_drops", "_claim_correction_drops"]
