"""BrainCore public facade with stable ``brain.core`` imports."""
from __future__ import annotations

import os
from pathlib import Path

from ._shared import (
    Any, AuditChain, BrainIndex, CosFoldsMixin, DailyFoldsMixin, DraftDrainMixin,
    GoldenFoldsMixin, GoldenOpsMixin, GraphFoldsMixin, GraphOpsMixin, Hit,
    IntakeFoldsMixin, InvariantFoldsMixin, KeyUnavailable, MaintenanceOrchestratorMixin,
    MULTI_GUARD_STRONG_RANK, MULTI_MAX_VARIANTS, MULTI_RRF_K,
    OrganizationFoldsMixin, PreflightFoldsMixin, PublishFoldsMixin,
    ReportingFoldsMixin, RetentionFoldsMixin, RetrievalOpsMixin, RoleError,
    StatusOpsMixin, SupersedeJournalUnreadable, SupersedePreconditionFailed,
    UpdateOpsMixin, WatchdogFoldsMixin, WeeklyFoldsMixin, WriterLockBusy,
    classification, config,
    frontmatter, safe_slug, sha256_text, vault_writer_lock,
    _audit_status_summary, _contained_in, _stamp_draft_frontmatter,
)
from ._durability import (
    SupersedeNotDurable, _fsync_dir_strict, _mkdir_durable,
    _require_durable_replace, _write_atomic_durable, _write_note_durable,
)

from ._retrieval import _CoreRetrievalMixin
from ._capture import _CoreCaptureMixin
from ._supersession_journal import _SupersessionJournalMixin
from ._supersession import _SupersessionTransactionMixin
from ._audit import _CoreAuditMixin
from ._briefing import _CoreBriefingMixin
from ._health import _CoreHealthMixin
from ._maintenance_state import _MaintenanceStateMixin
from ._cos_facade import _CosFacadeMixin
from ._maintenance import _MaintenanceMixin

class BrainCore(
    _CoreRetrievalMixin,
    _CoreCaptureMixin,
    _SupersessionJournalMixin,
    _SupersessionTransactionMixin,
    _CoreAuditMixin,
    _CoreBriefingMixin,
    _CoreHealthMixin,
    _MaintenanceStateMixin,
    _CosFacadeMixin,
    _MaintenanceMixin,
    DraftDrainMixin,
    CosFoldsMixin,
    DailyFoldsMixin,
    GoldenOpsMixin,
    GraphOpsMixin,
    GraphFoldsMixin,
    GoldenFoldsMixin,
    IntakeFoldsMixin,
    InvariantFoldsMixin,
    MaintenanceOrchestratorMixin,
    OrganizationFoldsMixin,
    PreflightFoldsMixin,
    PublishFoldsMixin,
    ReportingFoldsMixin,
    RetentionFoldsMixin,
    RetrievalOpsMixin,
    StatusOpsMixin,
    UpdateOpsMixin,
    WatchdogFoldsMixin,
    WeeklyFoldsMixin,
):
    """Host/VM engine facade assembled from single-responsibility mixins."""

    _SUPERSEDE_JOURNAL_V = 1
    SUPERSESSION_KEYS_OLD = ("superseded_by", "superseded_date", "is_latest_version")
    SUPERSESSION_KEYS_NEW = ("previous_version",)

    def __init__(
        self,
        vault: str | Path | None = None,
        index: BrainIndex | None = None,
        audit_log: str | Path | None = None,
        *,
        role: str | None = None,
    ) -> None:
        self.role = config.role(role)
        self.vault = config.vault_root(vault)
        if index is not None:
            self.index = index
        elif self.role == config.ROLE_VM:
            # VM leg reads ONLY the published read-only snapshot — never the
            # authoritative writable index, never WAL.
            self.index = BrainIndex(db_path=config.snapshot_db_path(self.vault),
                                    read_only=True)
        else:
            # Field bug 3: before opening the index/audit dir, migrate a legacy
            # absolute-path-keyed dir onto the move-stable vault-id key so a
            # vault move never re-embeds or forks the audit chain. Best-effort.
            config.migrate_index_location(self.vault)
            self.index = BrainIndex(db_path=config.index_path(self.vault))
        if self.role == config.ROLE_VM:
            # No signing surface AT ALL on the VM: the audit chain (and thus
            # resolve_signing_key) is simply not constructed here.
            self.audit = None
        else:
            log = Path(audit_log) if audit_log else config.default_audit_log(self.vault)
            self.audit = AuditChain(log)

    def _require_host(self, op: str) -> None:
        if self.role != config.ROLE_HOST:
            raise RoleError(
                f"role={self.role!r} may not {op}; this is a host-broker privilege "
                "(the VM leg is read + draft only). Run on the host."
            )

    def _write_note_durable(self, target: Path, content: str) -> None:
        """Call the public durability seam for supersession note writes."""
        _write_note_durable(target, content)

    def _require_durable_replace(self, what: str) -> None:
        """Call the public durability seam before a supersession transaction."""
        _require_durable_replace(what)

    def _mkdir_durable(self, directory: Path) -> None:
        """Create a durable journal directory through the facade seam."""
        _mkdir_durable(directory, fsync_dir=_fsync_dir_strict)

__all__ = [
    "BrainCore", "RoleError", "SupersedeJournalUnreadable",
    "SupersedeNotDurable", "SupersedePreconditionFailed", "WriterLockBusy",
    "MULTI_GUARD_STRONG_RANK", "MULTI_MAX_VARIANTS", "MULTI_RRF_K",
    "_contained_in", "_fsync_dir_strict", "_mkdir_durable",
    "_require_durable_replace", "_stamp_draft_frontmatter",
    "_write_atomic_durable", "_write_note_durable", "vault_writer_lock",
]
