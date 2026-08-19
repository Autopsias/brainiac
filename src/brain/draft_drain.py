"""Promote host-visible capture drafts through the audited write path."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import autolink, cos, frontmatter, provenance
from .audit import KeyUnavailable
from .notes import note_from_text, safe_slug


@dataclass
class DraftCandidate:
    """Validated in-memory draft ready for security transforms and commit."""

    draft: Path
    approved: bool
    pubkey: Any
    approved_sha: str | None
    content: str
    note: Any
    note_id: str
    rel_path: str
    subtree: str
    source_name: str


@dataclass
class DraftDrainRun:
    """Mutable result state for one host drain-on-invoke pass."""

    core: Any
    promoted: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    any_dir: bool = False

    def skip(self, draft: Path, reason: str, source_name: str) -> None:
        """Record a fail-closed refusal with its originating queue."""
        self.skipped.append(
            {"draft": draft.name, "source": source_name, "reason": reason}
        )


def _read_and_hash_candidate(
    run: DraftDrainRun,
    draft: Path,
    *,
    approved: bool,
    pubkey: Any,
    source_name: str,
) -> tuple[str, str | None] | None:
    """Read once and verify the approved payload's signed SHA anchor."""
    if approved:
        try:
            return cos.read_approved(run.core.vault, draft, pubkey=pubkey)
        except cos.ApprovedRefused as exc:
            cos.refuse_approved(run.core.vault, draft, str(exc))
            run.skip(draft, f"approved-queue refusal (NOT signed): {exc}", source_name)
            return None
        except cos.ApprovedKeyUnavailable as exc:
            run.skip(draft, f"no-signing-key (fail-closed): {exc}", source_name)
            return None
    try:
        return draft.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        run.skip(draft, f"unreadable: {type(exc).__name__}", source_name)
        return None


def _validate_candidate(
    run: DraftDrainRun,
    draft: Path,
    *,
    approved: bool,
    pubkey: Any,
    source_name: str,
    content: str,
    approved_sha: str | None,
) -> DraftCandidate | None:
    """Validate identity, owner-gate state, destination, and uniqueness."""
    note = note_from_text(content, draft, run.core.vault)
    if note is None:
        run.skip(draft, "no-frontmatter", source_name)
        return None
    try:
        note_id = safe_slug(note.id)
    except ValueError as exc:
        run.skip(draft, f"unsafe-id (fail-closed): {exc}", source_name)
        return None
    if approved and note_id != draft.stem:
        cos.refuse_approved(
            run.core.vault,
            draft,
            f"frontmatter id {note_id!r} != approved id {draft.stem!r}",
        )
        run.skip(
            draft,
            f"approved-queue refusal (NOT signed): id mismatch {note_id!r} != {draft.stem!r}",
            source_name,
        )
        return None
    if not _owner_gate_allows(run, draft, note_id, approved, source_name):
        return None
    rel_path, subtree = _promotion_path(note, note_id)
    destination = run.core.vault / rel_path
    try:
        already_indexed = run.core.index.get(note_id) is not None
    except Exception:
        already_indexed = False
    if already_indexed or destination.exists():
        run.skip(draft, f"duplicate-id: {note_id!r} already exists", source_name)
        return None
    return DraftCandidate(
        draft=draft,
        approved=approved,
        pubkey=pubkey,
        approved_sha=approved_sha,
        content=content,
        note=note,
        note_id=note_id,
        rel_path=rel_path,
        subtree=subtree,
        source_name=source_name,
    )


def _owner_gate_allows(
    run: DraftDrainRun,
    draft: Path,
    note_id: str,
    approved: bool,
    source_name: str,
) -> bool:
    """Quarantine a proposal-bypass attempt and fail closed on gate errors."""
    try:
        if not approved and note_id in cos.undecided_proposal_ids(run.core.vault):
            destination = cos.quarantine_gate_bypass(run.core.vault, draft)
            run.skip(
                draft,
                f"gate-bypass: {note_id!r} is awaiting the owner's accept/reject — "
                f"quarantined to {destination.parent.name}/{destination.name}",
                source_name,
            )
            return False
    except Exception as exc:
        run.skip(
            draft,
            "gate-check-failed (fail-closed, left in place): "
            f"{type(exc).__name__}: {exc}",
            source_name,
        )
        return False
    return True


def _promotion_path(note: Any, note_id: str) -> tuple[str, str]:
    """Choose the canonical raw or brain-resources destination."""
    if note.type == "source" or note.zone == "raw":
        return f"raw/{note_id}.md", "raw"
    return f"brain/resources/{note_id}.md", "brain/resources"


def _sanitize_candidate(run: DraftDrainRun, candidate: DraftCandidate) -> bool:
    """Strip forged host assertions and add ordinary-draft autolinks."""
    content = candidate.content
    if frontmatter.split(content) is not None:
        try:
            content = provenance.without_host_only_text(content)
        except provenance.HostOnlyKeyResidue as exc:
            run.skip(
                candidate.draft,
                f"host-only provenance forgery: {exc}",
                candidate.source_name,
            )
            return False
        content = frontmatter.set_keys(content, {"provenance.trust": "untrusted"})
    split = None if candidate.approved else frontmatter.split(content)
    if split is not None:
        frontmatter_block, body = split
        linked_body, _added = autolink.apply_autolinks(
            body,
            title=candidate.note.title,
            origin=str(candidate.note.meta.get("origin", "")),
            vault=run.core.vault,
        )
        if linked_body != body:
            content = f"---{frontmatter_block}---{linked_body}"
    candidate.content = content
    return True


def _approval_anchor_still_binds(run: DraftDrainRun, candidate: DraftCandidate) -> bool:
    """Close the approved-queue TOCTOU window immediately before signing."""
    if not candidate.approved:
        return True
    if cos.anchor_still_binds(
        run.core.vault,
        candidate.note_id,
        candidate.approved_sha or "",
        pubkey=candidate.pubkey,
    ):
        return True
    cos.refuse_approved(
        run.core.vault,
        candidate.draft,
        "the signed approval anchor no longer binds these bytes (changed during the drain)",
    )
    run.skip(
        candidate.draft,
        "approved-queue refusal (NOT signed): anchor changed during the drain",
        candidate.source_name,
    )
    return False


def _sign_wal_index_candidate(run: DraftDrainRun, candidate: DraftCandidate) -> bool:
    """Delegate signing, WAL append, and index upsert to BrainCore.write_note."""
    try:
        run.core.write_note(
            candidate.rel_path,
            candidate.content,
            reason=f"drain-on-invoke promote {candidate.draft.name}",
            subtree=candidate.subtree,
        )
    except KeyUnavailable:
        run.skip(candidate.draft, "no-signing-key (fail-closed)", candidate.source_name)
        return False
    except ValueError as exc:
        run.skip(
            candidate.draft,
            f"unsafe-path (fail-closed): {exc}",
            candidate.source_name,
        )
        return False
    return True


def _remove_committed_draft(run: DraftDrainRun, candidate: DraftCandidate) -> None:
    """Remove only a successfully committed draft or approved queue entry."""
    if candidate.approved:
        if not cos.clear_approved(run.core.vault, candidate.note_id):
            run.skip(
                candidate.draft,
                f"signed as {candidate.rel_path}, but the queue copy could NOT be removed — "
                "delete it by hand",
                candidate.source_name,
            )
    else:
        candidate.draft.unlink()
    run.promoted.append(candidate.rel_path)


def _drain_candidate(
    run: DraftDrainRun,
    draft: Path,
    *,
    approved: bool,
    pubkey: Any,
    source_name: str,
) -> None:
    """Run one draft through validation, transforms, commit, and removal."""
    read = _read_and_hash_candidate(
        run,
        draft,
        approved=approved,
        pubkey=pubkey,
        source_name=source_name,
    )
    if read is None:
        return
    content, approved_sha = read
    candidate = _validate_candidate(
        run,
        draft,
        approved=approved,
        pubkey=pubkey,
        source_name=source_name,
        content=content,
        approved_sha=approved_sha,
    )
    if candidate is None or not _sanitize_candidate(run, candidate):
        return
    if not _approval_anchor_still_binds(run, candidate):
        return
    if not _sign_wal_index_candidate(run, candidate):
        return
    _remove_committed_draft(run, candidate)


class DraftDrainMixin:
    """Provide BrainCore's host-only drain-on-invoke operation."""

    def drain_drafts(self) -> dict[str, Any]:
        """Promote pending drafts through the audited host-broker write path.

        Drafts remain in place on validation or signing failure. Approved bytes
        are read once, anchor-verified twice, and never reopened before signing;
        see AGENTS.md §6's VM-draft → host-commit protocol.
        """
        self._require_host("drain capture drafts (sign + index)")
        sources, initial_skips = self._drain_sources()
        run = DraftDrainRun(core=self, skipped=initial_skips)
        for directory, approved, pubkey in sources:
            if not directory.is_dir():
                continue
            run.any_dir = True
            source_name = "approved-queue" if approved else "capture-inbox"
            for draft in sorted(directory.glob("*.md")):
                _drain_candidate(
                    run,
                    draft,
                    approved=approved,
                    pubkey=pubkey,
                    source_name=source_name,
                )
        result: dict[str, Any] = {
            "promoted": len(run.promoted),
            "skipped": len(run.skipped),
            "details": {"promoted": run.promoted, "skipped": run.skipped},
        }
        if not run.any_dir:
            result["reason"] = "no-drafts-dir"
        return result
