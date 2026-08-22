"""Core dependency definitions."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import classification, config, frontmatter
from ..audit import AuditChain, KeyUnavailable
from ..draft_drain import DraftDrainMixin
from ..folds import (
    CosFoldsMixin, DailyFoldsMixin, GraphFoldsMixin, GoldenFoldsMixin,
    IntakeFoldsMixin, InvariantFoldsMixin, MaintenanceOrchestratorMixin,
    OrganizationFoldsMixin, PreflightFoldsMixin, PublishFoldsMixin,
    RemediationFoldsMixin, ReportingFoldsMixin, RetentionFoldsMixin,
    WatchdogFoldsMixin, WeeklyFoldsMixin,
)
from ..graph_ops import GraphOpsMixin
from ..golden_ops import GoldenOpsMixin
from ..index import BrainIndex, Hit
from ..lock import WriterLockBusy, vault_writer_lock
from ..notes import safe_slug, sha256_text
from ..retrieval_ops import (
    MULTI_GUARD_STRONG_RANK, MULTI_MAX_VARIANTS, MULTI_RRF_K, RetrievalOpsMixin,
)
from ..status_ops import StatusOpsMixin
from ..update_ops import UpdateOpsMixin

def source_repo_root(start: Path | None = None) -> Path | None:
    """Depth-independent source-checkout root for repo-relative dev artifacts.

    Returns the nearest ancestor of ``start`` (default: THIS file) that carries
    both ``pyproject.toml`` and ``src/brain`` — never ``parents[N]`` depth
    arithmetic, which silently resolved to ``src/`` after the tier-4 split
    moved ``brain/core.py`` one directory deeper into ``brain/core/`` and the
    autoresearch/framework-sync lookups started reading nonexistent paths
    under ``src/eval`` and ``src/tools``. A candidate ancestor counts ONLY
    when the resolved ``start`` lies inside that candidate's ``src/brain``
    (resolved-path containment, ``_contained_in`` — never string prefixes):
    an INSTALLED copy, in site-packages ANYWHERE — including a ``.venv``
    nested inside a checkout — sees the checkout's markers above it without
    being source of that checkout, so it resolves to ``None``. A DIRECTORY
    ``start`` is its own first candidate, so ``source_repo_root(<repo root>)``
    returns that root. ``None`` when the engine runs from an installed
    package (site-packages), where repo-relative artifacts (``eval/runs``,
    ``tools/``) legitimately do not exist — callers must treat that as
    "not applicable", never as an error.
    """
    here = (start if start is not None else Path(__file__)).resolve()
    candidates = (here, *here.parents) if here.is_dir() else here.parents
    for parent in candidates:
        if not ((parent / "pyproject.toml").is_file()
                and (parent / "src" / "brain").is_dir()):
            continue
        # An ancestor's markers prove a checkout only when the START path is
        # source of THAT checkout — never for an installed copy that merely
        # sits below it (.venv/site-packages). The start directory itself is
        # the caller's own claim, so it needs no containment proof.
        if here == parent or _contained_in(here, parent / "src" / "brain"):
            return parent
    return None

def _contained_in(target: Path, base: Path) -> bool:
    """True iff RESOLVED ``target`` is strictly inside RESOLVED ``base``.

    Uses Path.relative_to on resolved paths — never string-prefix checks
    (sibling-directory bypass, e.g. ``vault-x`` matching ``vault``). Resolving
    also follows symlinks, so a symlink inside the vault pointing outside it
    fails containment. Path.resolve() is non-strict, so a not-yet-existing
    target (draft-capture writes NEW files) resolves fine.
    """
    target = target.resolve()
    base = base.resolve()
    if target == base:
        return False
    try:
        target.relative_to(base)
    except ValueError:
        return False
    return True

def _stamp_draft_frontmatter(content: str, note_id: str, is_source: bool) -> str:
    """Return ``content`` with draft markers ensured (idempotent, non-clobbering).

    Guarantees the staged file carries frontmatter with an ``id``, ``status:
    draft`` and ``provenance.trust: untrusted`` so (a) the host drain's
    ``load_note`` can read it and (b) any reader can see it is an uncommitted,
    untrusted draft. Existing keys are never overwritten — capture is additive.
    """
    meta, body = frontmatter.parse_text(content)
    if not content.startswith("---") or not meta:
        # No (or unparseable) frontmatter — synthesise a minimal block.
        dtype = "source" if is_source else "note"
        return (
            f"---\nid: {note_id}\ntype: {dtype}\nstatus: draft\n"
            f"provenance.trust: untrusted\n---\n\n{content.lstrip()}\n"
        )
    block, after = content.split("---", 2)[1], content.split("---", 2)[2]
    additions = []
    if "id" not in meta:
        additions.append(f"id: {note_id}")
    if "status" not in meta:
        additions.append("status: draft")
    if "provenance.trust" not in meta:
        additions.append("provenance.trust: untrusted")
    if not additions:
        return content
    new_block = block.rstrip("\n") + "\n" + "\n".join(additions) + "\n"
    return f"---{new_block}---{after}"

def _audit_status_summary(audit_res: dict[str, Any]) -> str:
    """One line naming WHAT is wrong with the chain — signature/linkage errors
    and, since INT-02, content drift. "status=content_drift (0 error(s))" alone
    reads like a chain with nothing wrong."""
    parts = [f"audit chain status={audit_res.get('status')}",
             f"({len(audit_res.get('errors', []))} chain error(s)"]
    unexplained = audit_res.get("content_drift_unexplained")
    if unexplained:
        parts.append(f", {unexplained} of {audit_res.get('content_drift_count')} "
                     f"signed note(s) changed after signing with no disposition")
    return "".join(parts) + ")"

class RoleError(RuntimeError):
    """A host-broker operation was attempted from the read+draft-only VM leg.

    The VM leg (``role=vm``) may never write notes, mutate/WAL the index, publish
    a snapshot, or resolve a signing key. These ops fail with RoleError BEFORE
    any signing-key resolution or index write is attempted (S06 hard guarantee).
    """

class SupersedePreconditionFailed(ValueError):
    """A caller's out-of-band ``expect`` preconditions no longer hold.

    Raised INSIDE ``supersede``'s writer lock, before the first signed write,
    so a proposal decided against one state of the vault can never apply
    against another (VER-02: an owner-accepted supersede proposal sits in the
    queue while the nightly folds keep running).
    """

class SupersedeJournalUnreadable(RuntimeError):
    """The crash journal for an unfinished ``supersede``/``unsupersede`` exists
    but cannot be parsed, so the pre-transaction content of a half-written
    version chain is not recoverable automatically.

    Fail closed: the journal is PRESERVED and every supersession verb refuses
    until a human repairs the two notes and removes it. Its own class because
    the nightly dedup fold swallows a per-pair `Exception` and moves on, which
    would turn a sticky, vault-wide refusal into a silent zero — the fold
    re-raises this one instead of counting it as "nothing to merge".
    """
