"""The role=vm egress ceiling, from a source the VM cannot forge (VULN-3386).

The ceiling a VM session may never exceed used to resolve from
``$BRAIN_VM_MAX_EGRESS_TIER`` — but the session's own shell can export that
variable, so the "operator gate" was unenforceable from inside the session
(external pentest 2026-08: a session raised its own cap by exporting it; the
unsigned ``vm-egress-tier`` mount file had the same hole, stated in its own
bootstrap comment). The ceiling now rides a HOST-SIGNED file on the mount —
``<runtime>/vm-egress-tier.signed``: tier + vault_id, Ed25519 under the same
signing key whose public half is pinned at install time
(``pinned-verify.json``, the identity anchor the exceptions summary already
verifies with). The VM verifies before trusting; anything else — missing
anchor, missing file, malformed, bad signature, foreign vault_id — fails
CLOSED to the shipped ``Internal`` cap. The env var no longer raises a VM
ceiling at all (it remains host-role configuration, where the operator's
shell is the trusted context).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = 1
SIGNED_FILENAME = "vm-egress-tier.signed"


def signed_path(vault: Path) -> Path:
    from . import config as _config

    return _config.brain_runtime_dir(vault) / SIGNED_FILENAME


def write_signed(vault: Path, tier: str) -> dict[str, Any]:
    """HOST-broker: sign and write the ceiling file. Fails (raises) when no
    signing key resolves — an unraisable ceiling must never be written
    unsigned."""
    import base64

    from . import audit as _audit
    from . import classification as _cls
    from . import config as _config

    if tier not in _cls.RANK:
        raise ValueError(
            f"unrecognised tier {tier!r}; expected one of {list(_cls.TIERS)}")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA,
        "vault_id": _config.vault_id(vault, create=True) or "",
        "tier": tier,
    }
    key, _source = _audit.resolve_signing_key()
    canonical = json.dumps(payload, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    payload["signature"] = base64.b64encode(key.sign(canonical)).decode("ascii")
    path = signed_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def remove_signed(vault: Path) -> bool:
    """HOST-broker: drop the signed ceiling (back to the shipped cap)."""
    path = signed_path(vault)
    if path.is_file():
        path.unlink()
        return True
    return False


def resolved_ceiling(vault: Path | None = None) -> tuple[str, str]:
    """``(tier, provenance)`` for the role=vm clamp — the ONLY ceiling source
    the VM leg trusts. Verified against the pinned anchor; every failure mode
    fails closed to the shipped cap with the reason as provenance."""
    from . import classification as _cls
    from . import config as _config
    from .exceptions_verify import PINNED_FILENAME, load_pinned

    if vault is None:
        try:
            vault = _config.vault_root(allow_missing=True)
        except Exception:  # noqa: BLE001 — no resolvable vault => no elevation
            return _cls.VM_DEFAULT_MAX_TIER, "vault-unresolvable"
    pinned = load_pinned(vault)
    if pinned is None:
        return (_cls.VM_DEFAULT_MAX_TIER,
                f"no pinned anchor ({PINNED_FILENAME}) — workspace never staged")
    try:
        raw = json.loads(signed_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _cls.VM_DEFAULT_MAX_TIER, "no readable signed ceiling"
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA:
        return _cls.VM_DEFAULT_MAX_TIER, "signed ceiling malformed"
    if str(raw.get("vault_id") or "") != str(pinned["vault_id"]):
        return _cls.VM_DEFAULT_MAX_TIER, "signed ceiling vault_id mismatch"
    tier = raw.get("tier")
    if tier not in _cls.RANK:
        return _cls.VM_DEFAULT_MAX_TIER, "signed ceiling tier unrecognised"
    # Reuse the exceptions summary's verifier — ONE signature-verification
    # implementation, not a second copy that can drift weaker.
    from .exceptions_verify import _verify_signature

    if not _verify_signature(raw, str(pinned["public_key_pem"])):
        return _cls.VM_DEFAULT_MAX_TIER, "signed ceiling signature INVALID"
    return str(tier), "signed-file"
