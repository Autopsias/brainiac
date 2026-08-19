"""COS layout setup."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._attachment_store import attachment_expired_dir, attachment_lifecycle_dir, attachment_quarantine_dir, attachments_dir, ingest_manifest_dir
from ._layout import drop_dir, evidence_dir, hold_dir, host_dir, proposal_drop_dir, proposals_dir, shared_dir, verdict_drop_dir
from ._run_migration import migrate_run_records

def ensure_layout(vault=None) -> dict[str, str]:
    """Create the three permission zones + their sub-dirs (idempotent)."""
    zones = {
        "host": host_dir(vault),
        "shared": shared_dir(vault),
        "drop": drop_dir(vault),
    }
    for name, d in zones.items():
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, _PERMS[name])
        except OSError:
            pass
    for d in (evidence_dir(vault), proposals_dir(vault), hold_dir(vault),
              proposals_dir(vault) / "pending", proposals_dir(vault) / "rejected",
              proposals_dir(vault) / "expired",
              proposals_dir(vault) / "corrections-pending",
              attachments_dir(vault), attachment_quarantine_dir(vault),
              attachment_expired_dir(vault), attachment_lifecycle_dir(vault)):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)  # nosemgrep: insecure-file-permissions -- intentionally OWNER-ONLY (host-private zone), not overly-permissive
        except OSError:
            pass
    for d in (proposal_drop_dir(vault), verdict_drop_dir(vault),
              ingest_manifest_dir(vault)):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o775)  # nosemgrep: insecure-file-permissions -- VM-writable drop zone needs group-write; owner+group only, no world access
        except OSError:
            pass
    # The run-record store is NOT one of these zones — it is off the mount
    # entirely (gap-05). Layout time is where its one-time carry-forward runs,
    # because every host write path already comes through here.
    migrate_run_records(vault)
    return {str(p): oct(_PERMS[n]) for n, p in zones.items()}

__all__ = ['ensure_layout']
