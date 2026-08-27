"""Audit maintenance methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    Path,
)


class _CoreAuditMixin:
    """Audit maintenance methods for BrainCore."""

    def verify_audit(
        self, *, check_content: bool = False, public_key_pem: bytes | None = None
    ) -> dict[str, Any]:
        # HOST-broker only on the default path: verify() derives the public key
        # from the resolved signing key, and the VM leg must never resolve a
        # key. With an EXPLICIT public key (VULN-3388, external verification)
        # no signing key is resolved at all — a third party verifies a
        # submitted log holding only the exported public half.
        self._require_host("verify the audit chain (resolves the signing key)")
        from .. import audit as _audit

        res = self.audit.verify(public_key_pem_bytes=public_key_pem)
        # INT-02: the content pass runs on the DEFAULT surface too. A
        # signature-only "ok" reads as a content all-clear while notes signed
        # weeks ago sit changed on disk — so the plain command always reports
        # the count, and `--check-content` only adds the per-note detail.
        summary = _audit.drift_summary(self.vault, self.audit)
        res["content_drift_count"] = summary["total"]
        res["content_drift_unexplained"] = summary["unexplained"]
        if check_content:
            res["content_drift"] = summary["records"]
        if summary["unexplained"] and res["status"] == "ok":
            # signatures fine, but a signed note's bytes changed on disk and
            # nothing has triaged it
            res["status"] = "content_drift"
        return res

    def audit_pubkey(self) -> dict[str, Any]:
        """Export the audit chain's PUBLIC key (VULN-3388, external pentest
        2026-08): the verifier half an external party holds to validate a
        submitted log — `verify-audit --pubkey` — without ever touching the
        private key. HOST-broker only (deriving it resolves the signing key).
        """
        import hashlib as _hashlib

        self._require_host("export the audit public key")
        from .. import audit as _audit

        pem = _audit.public_key_pem()
        return {
            "public_key_pem": pem.decode("utf-8"),
            "sha256": _hashlib.sha256(pem).hexdigest(),
        }

    def _sync_guard_facts(self) -> dict[str, Any]:
        """The VULN-3387 downgrade guard's inputs: the audit chain's last
        signed hash per path, and the owner's drift dispositions. Both are
        host-side facts the index layer cannot reach on its own. If either
        cannot be read the guard is returned inert (pre-guard behavior) —
        reconciliation must not stop over an unreadable chain, and the drift
        surfaces (verify-audit/doctor) still report independently."""
        audit = getattr(self, "audit", None)
        if audit is None:
            return {}
        try:
            signed_hashes = audit.latest_signed_hashes()
        except Exception:  # noqa: BLE001 — unreadable chain => guard inert
            return {}
        from ..audit_drift import load_drift_dispositions

        return {"signed_hashes": signed_hashes,
                "dispositions": load_drift_dispositions(self.vault)}

    def set_vm_egress_tier(self, tier: str | None) -> dict[str, Any]:
        """Set (or with ``None``, remove) the HOST-SIGNED VM egress ceiling
        (VULN-3386). This is the one sanctioned way to raise what a role=vm
        session may read: it signs tier+vault_id with the audit key, so the
        session — which can write everything on the mount, including this
        file — cannot forge or alter it. HOST-broker only; fails closed when
        no signing key resolves."""
        from ..vm_ceiling import remove_signed, resolved_ceiling, write_signed

        self._require_host("set the VM egress ceiling (signs it)")
        if tier is None:
            removed = remove_signed(self.vault)
            enforced, provenance = resolved_ceiling(self.vault)
            return {"removed": removed, "enforced": enforced,
                    "provenance": provenance}
        payload = write_signed(self.vault, tier)
        enforced, provenance = resolved_ceiling(self.vault)
        return {"tier": payload["tier"], "enforced": enforced,
                "provenance": provenance}
    def anchor_chain(self, anchor_dir: str | Path) -> dict[str, Any]:
        """Publish the signed chain head to an OFF-HOST append-only store."""
        self._require_host("anchor the audit chain off-host")
        from .. import anchor as _anchor

        return _anchor.anchor(self.audit.log_path, Path(anchor_dir))
    def verify_anchor(self, anchor_dir: str | Path) -> dict[str, Any]:
        """Verify the live chain against the off-host anchor (detect rewrite)."""
        self._require_host("verify the off-host anchor")
        from .. import anchor as _anchor

        return _anchor.verify_against_anchor(self.audit.log_path, Path(anchor_dir))
    def backup(self, dest_dir: str | Path, *, encrypt: bool = True) -> dict[str, Any]:
        """Create an encrypted off-device backup of the Markdown truth."""
        self._require_host("create an off-device backup")
        from .. import backup as _backup

        return _backup.create_backup(self.vault, Path(dest_dir), encrypt=encrypt).to_dict()
    def restore(self, archive: str | Path, dest_dir: str | Path) -> dict[str, Any]:
        """Restore (and decrypt) a backup archive into ``dest_dir``."""
        self._require_host("restore a backup")
        from .. import backup as _backup

        return _backup.restore_backup(Path(archive), Path(dest_dir))
