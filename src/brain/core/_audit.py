"""Audit maintenance methods for BrainCore."""
from __future__ import annotations

from ._shared import (
    Any,
    Path,
)


class _CoreAuditMixin:
    """Audit maintenance methods for BrainCore."""

    def verify_audit(self, *, check_content: bool = False) -> dict[str, Any]:
        # HOST-broker only: verify() derives the public key via the resolved
        # signing key — the VM leg must never resolve a key.
        self._require_host("verify the audit chain (resolves the signing key)")
        from .. import audit as _audit

        res = self.audit.verify()
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
