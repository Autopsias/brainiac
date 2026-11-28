"""Promote host-visible capture drafts through the audited write path."""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _optional, autolink, draft_update, frontmatter, provenance
from .audit import KeyUnavailable
from .notes import note_from_text, safe_slug

# ADR 0013: COS owns the approved queue and the proposal gate. Without it the
# approved queue is never a drain source and no proposal awaits the owner.
if _optional.cos_available():
    from . import cos



class UntrustedAuthorRefusal(ValueError):
    """Note text authored by an untrusted leg failed a host-broker control."""


def sanitize_untrusted_note(content: str, *, path: Path, vault: Path) -> str:
    """Apply the host-broker's untrusted-author controls to note text.

    THE ONE implementation, called from both places that need it: this module's
    draft drain, and `brain write --untrusted-author` (which the weekly
    synthesis sign-drain uses). Two copies of this rule would be two rules; the
    2026-08-16 finding below is exactly what happens when one path has it and
    another does not.

    What it enforces, and why each one:

    * **A safe id, fail-closed.** `safe_slug` rejects an id that could escape
      its subtree. An untrusted author picks the id.
    * **No forged host-only provenance.** A model that read attacker-controlled
      vault content can be steered into writing `provenance.verified` — or any
      other key the HOST derives for itself — into frontmatter. Signing those
      bytes turns a claim into an assertion the host made. `without_host_only_text`
      strips the lines and raises when the key still resolves.
    * **The trust marker.** What an untrusted leg wrote is recorded as
      untrusted, even though the host is what signs it. Signing attests that
      these are the bytes; it does not attest that they are trustworthy.

    *Why this exists (2026-08-16, Codex cloud security round):* the weekly
    synthesis sign-drain called `brain write` directly on every brain/ note the
    confined session had written. That closed a real, measured hole — 24
    unsigned notes up to MNPI, sitting indexed and retrievable — but it opened
    another, because the drain skipped every control above. The session denies
    Bash, runs BRAIN_ROLE=vm and holds no key precisely because it reads
    untrusted content; signing its output unvalidated hands back the authority
    that confinement removed.

    Raises `UntrustedAuthorRefusal` rather than returning a flag: a note that
    fails a control must not reach the signing path at all.
    """
    note = note_from_text(content, path, vault)
    if note is None:
        raise UntrustedAuthorRefusal("no frontmatter — cannot identify the note")
    try:
        safe_slug(note.id)
    except ValueError as exc:
        raise UntrustedAuthorRefusal(f"unsafe id (fail-closed): {exc}") from exc
    if frontmatter.split(content) is None:
        return content
    try:
        content = provenance.without_host_only_text(content)
    except provenance.HostOnlyKeyResidue as exc:
        raise UntrustedAuthorRefusal(f"host-only provenance forgery: {exc}") from exc
    split = frontmatter.split(content)
    if split is not None and frontmatter.uses_alias(split[0]):
        raise UntrustedAuthorRefusal("frontmatter uses YAML anchors/aliases (fail-closed)")
    stamped = frontmatter.set_keys(content, {"provenance.trust": "untrusted"})
    # Fail closed: whatever the parser keeps (YAML keeps the LAST duplicate)
    # must read untrusted, or the host would sign a forged trust marker.
    trust = frontmatter.parse_text(stamped)[0].get("provenance.trust")
    if trust != "untrusted":
        raise UntrustedAuthorRefusal(
            f"provenance.trust reads {trust!r} after stamping (fail-closed)"
        )
    return stamped


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
    is_update: bool = False


@dataclass
class DraftDrainRun:
    """Mutable result state for one host drain-on-invoke pass."""

    core: Any
    promoted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    any_dir: bool = False

    def skip(self, draft: Path, reason: str, source_name: str) -> None:
        """Record a fail-closed refusal with its originating queue."""
        self.skipped.append(
            {"draft": draft.name, "source": source_name, "reason": reason}
        )


# 64 MiB = 10.4x the largest untrusted-lane note measured 2026-11-14 (6,431,396 B).
MAX_DRAFT_BYTES = 64 * 1024 * 1024


class _UnsafeDraft(Exception):
    """A capture-inbox entry that is a symlink or not a regular file."""


def _read_text_nofollow(path: Path, max_bytes: int | None = None) -> str:
    """Read a draft without following a symlink (copy of cos/_io's guard, ADR 0013)."""
    cap = MAX_DRAFT_BYTES if max_bytes is None else max_bytes
    if path.is_symlink():
        raise _UnsafeDraft(f"{path.name} is a symlink")
    # O_NONBLOCK: a planted FIFO must not block the open before the S_ISREG check.
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _UnsafeDraft(f"{path.name} is not a regular file")
        chunks, total = [], 0
        while b := os.read(fd, 1 << 16):
            chunks.append(b)
            total += len(b)
            if total > cap:  # cap on the open fd, never on stat() (INT-04)
                raise _UnsafeDraft(f"{path.name} exceeds the {cap}-byte cap")
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8")


def _refuse_approved_via_hooks_or_cos(
    run: DraftDrainRun, draft: Path, reason: str,
) -> None:
    """Record an approved-queue refusal via hook provider or legacy COS, never crashing."""
    from . import host_hooks as _host_hooks

    vault = getattr(getattr(run, "core", None), "vault", None)
    if vault is None:
        return
    hooks = _host_hooks.load_core_hooks(getattr(run, "core", None))
    refusers = _host_hooks.providers_with(hooks, "refuse_approved_draft")
    if refusers:
        for provider in refusers:
            try:
                _host_hooks.call_hook_method(
                    provider, "refuse_approved_draft", vault, draft, reason,
                )
            except Exception as exc:  # noqa: BLE001 — reporting error must not drop skip
                logging.getLogger(__name__).warning(
                    "draft_drain: refuse_approved_draft failed on %s: %s: %s",
                    getattr(provider, "name", "hook"), type(exc).__name__, exc,
                )
        return
    if _optional.cos_available():
        try:
            cos.refuse_approved(vault, draft, reason)
        except Exception as exc:  # noqa: BLE001 — reporting error must not drop skip
            logging.getLogger(__name__).warning(
                "draft_drain: cos.refuse_approved failed: %s: %s",
                type(exc).__name__, exc,
            )


def _read_and_hash_candidate(
    run: DraftDrainRun,
    draft: Path,
    *,
    approved: bool,
    pubkey: Any,
    source_name: str,
) -> tuple[str, str | None] | None:
    """Read once and verify the approved payload's signed SHA anchor without consuming."""
    if approved:
        from . import host_hooks as _host_hooks

        hooks = _host_hooks.providers_with(
            _host_hooks.load_core_hooks(getattr(run, "core", None)), "validate_and_consume_approved",
        )
        if hooks:
            for provider in hooks:
                try:
                    res = _host_hooks.call_hook_method(
                        provider, "validate_and_consume_approved",
                        run.core, draft.stem, draft, True, default=None,
                    )
                    if isinstance(res, dict) and "content" in res:
                        return str(res["content"]), res.get("sha256")
                except Exception as exc:  # noqa: BLE001 — fail closed on hook error
                    run.skip(draft, f"approved-queue refusal (NOT signed): {exc}", source_name)
                    return None
            run.skip(draft, "approved-queue refusal (NOT signed): no hook validated item", source_name)
            return None
        if not _optional.cos_available():
            run.skip(draft, "approved-queue refusal (NOT signed): no approved-queue handler", source_name)
            return None
        try:
            return cos.read_approved(run.core.vault, draft, pubkey=pubkey)
        except cos.ApprovedRefused as exc:
            run.skip(draft, f"approved-queue refusal (NOT signed): {exc}", source_name)
            _refuse_approved_via_hooks_or_cos(run, draft, str(exc))
            return None
        except cos.ApprovedKeyUnavailable as exc:
            run.skip(draft, f"no-signing-key (fail-closed): {exc}", source_name)
            return None
    try:
        return _read_text_nofollow(draft), None
    except (OSError, UnicodeDecodeError) as exc:
        run.skip(draft, f"unreadable: {type(exc).__name__}", source_name)
        return None
    except _UnsafeDraft as exc:
        logging.getLogger(__name__).warning("draft refused: %s", exc)
        run.skip(draft, f"refused: {exc}", source_name)
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
        run.skip(
            draft,
            f"approved-queue refusal (NOT signed): id mismatch {note_id!r} != {draft.stem!r}",
            source_name,
        )
        _refuse_approved_via_hooks_or_cos(
            run,
            draft,
            f"frontmatter id {note_id!r} != approved id {draft.stem!r}",
        )
        return None
    if not _owner_gate_allows(run, draft, note_id, approved, source_name):
        return None
    rel_path, subtree = _promotion_path(note, note_id)
    destination = run.core.vault / rel_path
    try:
        already_indexed = run.core.index.get(note_id) is not None
    except Exception as exc:
        logging.getLogger(__name__).warning("draft_drain: index lookup failed; treating note as not indexed: %s", exc)
        already_indexed = False
    declared_update = str(note.meta.get("updates") or "").strip()
    if already_indexed or destination.exists():
        if declared_update:
            return _validate_update(
                run,
                draft,
                approved=approved,
                pubkey=pubkey,
                source_name=source_name,
                content=content,
                approved_sha=approved_sha,
                note=note,
                note_id=note_id,
            )
        run.skip(draft, f"duplicate-id: {note_id!r} already exists", source_name)
        return None
    if declared_update:
        run.skip(
            draft,
            f"update-target-missing: {declared_update!r} has no live note — "
            "drop the `updates` key to create instead",
            source_name,
        )
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
    from . import host_hooks as _host_hooks

    hooks = _host_hooks.providers_with(
        _host_hooks.load_core_hooks(getattr(run, "core", None)), "undecided_draft_ids",
    )
    if hooks:
        try:
            if not approved:
                for provider in hooks:
                    undecided = _host_hooks.call_hook_method(
                        provider, "undecided_draft_ids", run.core.vault, default=set(),
                    ) or set()
                    if note_id in undecided:
                        destination = _host_hooks.call_hook_method(
                            provider, "quarantine_bypass_draft",
                            run.core.vault, draft, default=None,
                        )
                        where = (f"{destination.parent.name}/{destination.name}"
                                 if destination is not None else "quarantine")
                        run.skip(
                            draft,
                            f"gate-bypass: {note_id!r} is awaiting the owner's accept/reject — "
                            f"quarantined to {where}",
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
    if not _optional.cos_available():
        return True  # ADR 0013: no COS, no proposal queue, nothing awaits the owner.
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


def _validate_update(
    run: DraftDrainRun,
    draft: Path,
    *,
    approved: bool,
    pubkey: Any,
    source_name: str,
    content: str,
    approved_sha: str | None,
    note: Any,
    note_id: str,
) -> DraftCandidate | None:
    """UPD-01 guards live in :mod:`brain.draft_update`; this wires the verdict."""
    verdict = draft_update.validate_update(
        run.core, note, note_id, approved=approved,
        promotion_path=_promotion_path,
    )
    if verdict.reason:
        run.skip(draft, verdict.reason, source_name)
        return None
    return DraftCandidate(
        draft=draft,
        approved=approved,
        pubkey=pubkey,
        approved_sha=approved_sha,
        content=content,
        note=note,
        note_id=note_id,
        rel_path=verdict.rel_path,
        subtree="brain",
        source_name=source_name,
        is_update=True,
    )


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
            # The id was already checked above by `_validate_candidate`; this
            # re-checks it, which is free and keeps ONE definition of the rule.
            content = sanitize_untrusted_note(
                content, path=candidate.draft, vault=run.core.vault
            )
        except UntrustedAuthorRefusal as exc:
            run.skip(candidate.draft, str(exc), candidate.source_name)
            return False
    if candidate.is_update:
        content = frontmatter.drop_keys(content, draft_update.UPDATE_TRANSPORT_KEYS)
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


def _hook_anchor_still_binds(
    all_hooks: tuple[Any, ...], run: DraftDrainRun, candidate: DraftCandidate,
) -> bool | None:
    """Re-verify the approval anchor via hooks; returns None when no hook handles it."""
    from . import host_hooks as _host_hooks

    bind_hooks = _host_hooks.providers_with(all_hooks, "approval_anchor_still_binds")
    if bind_hooks:
        for provider in bind_hooks:
            try:
                if _host_hooks.call_hook_method(
                    provider,
                    "approval_anchor_still_binds",
                    run.core,
                    candidate.note_id,
                    candidate.approved_sha or "",
                    candidate.pubkey,
                    default=False,
                ):
                    return True
            except Exception:  # noqa: BLE001 — fail closed on anchor re-check error
                break
        return False
    validate_hooks = _host_hooks.providers_with(all_hooks, "validate_and_consume_approved")
    if not validate_hooks:
        return None
    for provider in validate_hooks:
        try:
            res = _host_hooks.call_hook_method(
                provider,
                "validate_and_consume_approved",
                run.core,
                candidate.note_id,
                candidate.draft,
                True,
                default=None,
            )
            if isinstance(res, dict) and "content" in res:
                re_sha = res.get("sha256")
                if candidate.approved_sha is None or re_sha == candidate.approved_sha:
                    return True
        except Exception:  # noqa: BLE001 — fail closed on anchor re-check error
            break
    return False


def _approval_anchor_still_binds(run: DraftDrainRun, candidate: DraftCandidate) -> bool:
    """Close the approved-queue TOCTOU window immediately before signing."""
    if not candidate.approved:
        return True
    from . import host_hooks as _host_hooks

    hook_verdict = _hook_anchor_still_binds(
        _host_hooks.load_core_hooks(getattr(run, "core", None)), run, candidate,
    )
    if hook_verdict is not None:
        if hook_verdict:
            return True
        run.skip(
            candidate.draft,
            "approved-queue refusal (NOT signed): anchor changed during the drain",
            candidate.source_name,
        )
        _refuse_approved_via_hooks_or_cos(
            run,
            candidate.draft,
            "the signed approval anchor no longer binds these bytes (changed during the drain)",
        )
        return False
    if not _optional.cos_available():
        run.skip(
            candidate.draft,
            "approved-queue refusal (NOT signed): no anchor verifier available",
            candidate.source_name,
        )
        return False
    if cos.anchor_still_binds(
        run.core.vault,
        candidate.note_id,
        candidate.approved_sha or "",
        pubkey=candidate.pubkey,
    ):
        return True
    run.skip(
        candidate.draft,
        "approved-queue refusal (NOT signed): anchor changed during the drain",
        candidate.source_name,
    )
    _refuse_approved_via_hooks_or_cos(
        run,
        candidate.draft,
        "the signed approval anchor no longer binds these bytes (changed during the drain)",
    )
    return False


def _sign_wal_index_candidate(run: DraftDrainRun, candidate: DraftCandidate) -> bool:
    """Delegate signing, WAL append, and index upsert to BrainCore.write_note."""
    try:
        verb = "update" if candidate.is_update else "promote"
        run.core.write_note(
            candidate.rel_path,
            candidate.content,
            reason=f"drain-on-invoke {verb} {candidate.draft.name}",
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


def _consume_approved_via_hooks(
    run: DraftDrainRun, candidate: DraftCandidate, hooks: tuple[Any, ...],
) -> bool:
    """Consume an approved-queue entry only after signing succeeded."""
    from . import host_hooks as _host_hooks

    for provider in hooks:
        try:
            res = _host_hooks.call_hook_method(
                provider,
                "validate_and_consume_approved",
                run.core,
                candidate.note_id,
                candidate.draft,
                False,
                default=None,
            )
            if res is True or (
                isinstance(res, dict)
                and res.get("consumed") is not False
                and res.get("ok") is not False
            ):
                candidate.draft.unlink(missing_ok=True)
                return True
        except Exception as exc:  # noqa: BLE001 — already signed; report via skip
            logging.getLogger(__name__).warning(
                "draft_drain: approved consume failed on %s: %s: %s",
                getattr(provider, "name", "hook"), type(exc).__name__, exc,
            )
            return False
    return not candidate.draft.exists()


def _remove_committed_draft(run: DraftDrainRun, candidate: DraftCandidate) -> None:
    """Remove only a successfully committed draft or approved queue entry."""
    if candidate.approved:
        from . import host_hooks as _host_hooks

        hooks = _host_hooks.providers_with(
            _host_hooks.load_core_hooks(getattr(run, "core", None)), "validate_and_consume_approved",
        )
        if hooks:
            if not _consume_approved_via_hooks(run, candidate, hooks):
                run.skip(
                    candidate.draft,
                    f"signed as {candidate.rel_path}, but the queue copy could NOT be removed — "
                    "delete it by hand",
                    candidate.source_name,
                )
        elif _optional.cos_available() and not cos.clear_approved(run.core.vault, candidate.note_id):
            run.skip(
                candidate.draft,
                f"signed as {candidate.rel_path}, but the queue copy could NOT be removed — "
                "delete it by hand",
                candidate.source_name,
            )
    else:
        candidate.draft.unlink()
    (run.updated if candidate.is_update else run.promoted).append(candidate.rel_path)


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
            "updated": len(run.updated),
            "skipped": len(run.skipped),
            "details": {"promoted": run.promoted, "updated": run.updated,
                        "skipped": run.skipped},
        }
        if not run.any_dir:
            result["reason"] = "no-drafts-dir"
        return result
