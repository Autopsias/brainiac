"""LNK-03a — the "born-linked" convention registry + a mechanical scanner
that finds every note-creating call site in ``src/brain/`` and checks it
against the registry.

Root cause (operator-approved, 2026-07-20): LNK-01 (autolink.py) and LNK-02
(daily-note chaining) each fixed ONE discovered orphan source — but nothing
stopped a THIRD note-creating path from shipping unlinked and quietly
re-accruing orphans. This module is the structural fix: every call to the
one signed-write choke point, ``BrainCore.write_note`` (AGENTS.md §5 — the
host-broker privilege every note-creating path ultimately funnels through,
directly or via a capture-inbox/proposal-drop staging area that a HOST drain
later promotes through the same call), must be declared in
``NOTE_CREATION_POLICIES`` with a stated linking policy. An undeclared call
site fails ``tests/test_note_creation_conventions.py`` — CI, not discipline,
enforces the declaration.

Policy kinds:
  - ``autolinked``: the path runs ``autolink.apply_autolinks`` (or an
    equivalent evidence-gated linker) before signing (LNK-01).
  - ``chained``: the path deterministically links to a prior note of the
    same family (LNK-02's daily-note -> yesterday's-note chain).
  - ``counted``: no automatic link is attempted; the daily ``kl_orphans``
    watch (LNK-03b) is the safety net that surfaces growth.
  - ``exempt``: no linking obligation applies — either AGENTS.md §3 only
    binds the ``brain/`` zone (``raw/`` sources, and any pure frontmatter
    rewrite of an EXISTING note such as ``supersede``), or the caller is a
    host operator writing their own body directly (``brain write``).

Discovery limits (read before trusting a clean run):
  - Regex + indentation-stack scan, NOT a full AST. It finds physical lines
    matching an attribute-call shape (``<expr>.write_note(``, e.g.
    ``self.write_note(``/``core.write_note(``) and is *not* fooled by the
    ``def write_note(`` definition line itself or by prose that merely
    mentions ``write_note()`` with no leading dot (comments/docstrings), but
    it WILL miss: a call reached only through re-assignment/aliasing
    (``wn = core.write_note; wn(...)``), a call built via
    ``getattr(core, "write_note")(...)``, or one hidden inside a string
    that is later ``eval``'d. None of those patterns exist in this codebase
    today (grepped and confirmed at authoring time) — if one is introduced,
    this scanner will silently miss it and the registry check would false-pass.
  - Enclosing-function resolution assumes standard 4-space, tab-free
    indentation (true of this codebase; ``ruff format`` enforces it) and
    walks a simple indent stack rather than parsing scopes properly.
  - Legacy ``cli.py`` dispatch sites are sharpened by scanning backward for
    the nearest ``if cmd == "<name>":`` guard. New command handlers live in
    named ``cli_cmds`` functions, so their ordinary module/function identity
    is already precise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .conventions_scan import discover_creation_sites as discover_creation_sites_impl

# -- the registry -------------------------------------------------------------

NOTE_CREATION_POLICIES: dict[str, dict[str, str]] = {
    "core._briefing.capture": {
        "path": "core/_briefing.py BrainCore.capture (ad-hoc captures AND the "
                "daily-note fold, which calls capture() with note_type='daily')",
        "kind": "chained",
        "reason": "daily notes chain to yesterday's note (LNK-02); ad-hoc "
                  "captures are the caller's responsibility, counted by the "
                  "daily kl_orphans watch (LNK-03b)",
    },
    "draft_drain._sign_wal_index_candidate": {
        "path": "draft_drain.DraftDrainMixin._sign_wal_index_candidate "
                "(drain-on-invoke promotion of capture-inbox/ drafts — VM "
                "draft_capture, cos-propose, auto-capture holds all land here)",
        "kind": "autolinked",
        "reason": "sweep-promotion runs autolink.apply_autolinks on the "
                  "draft body before signing (LNK-01)",
    },
    "ingest.pipeline_stages.signed_note_write_stage": {
        "path": "ingest.pipeline_stages.signed_note_write_stage (inbox "
                "document promotion -> raw/)",
        "kind": "autolinked",
        "reason": "attendee/origin autolinking applied before write_note "
                  "(LNK-01); raw/ itself carries no linking obligation "
                  "(AGENTS.md §3 binds brain/ only) — this is opportunistic",
    },
    "ingest.transcript.ingest_transcript": {
        "path": "ingest.transcript.ingest_transcript (transcript promotion "
                "-> raw/)",
        "kind": "autolinked",
        "reason": "attendee/origin autolinking applied before write_note "
                  "(LNK-01); raw/ itself carries no linking obligation "
                  "(AGENTS.md §3 binds brain/ only) — this is opportunistic",
    },
    "cli_cmds.ingest_storage._run_write": {
        "path": "cli_cmds.ingest_storage._run_write, `brain write` dispatch "
                "-> core.write_note",
        "kind": "exempt",
        "reason": "host-broker direct write of an operator-authored body — "
                  "links are the caller's own responsibility per AGENTS.md §3",
    },
    "remediation_folds.audited_write": {
        "path": "remediation_folds.audited_write (the ONE write surface both "
                "FIX-01/FIX-02 repair branches use)",
        "kind": "exempt",
        "reason": "it REFUSES a target that does not already exist and refuses "
                  "any body change, so it can only ever re-sign or re-stamp an "
                  "EXISTING note — no new note body is created and no unlinked "
                  "note can appear",
    },
    "core._supersession._supersede_locked": {
        "path": "core/_supersession.py BrainCore._supersede_locked "
                "(via supersede())",
        "kind": "exempt",
        "reason": "rewrites frontmatter of two EXISTING notes (both sides of "
                  "a version chain) — no new note body is created",
    },
    "core._supersession._unsupersede_locked": {
        "path": "core/_supersession.py BrainCore._unsupersede_locked "
                "(via unsupersede())",
        "kind": "exempt",
        "reason": "DROPS the supersession keys from two EXISTING notes "
                  "(undoing a wrong DDP-01 auto-link, ENF-01) — no new note "
                  "body is created, the exact inverse of _supersede_locked",
    },
    "core._supersession_journal._recover_pending_supersede": {
        "path": "core/_supersession_journal.py "
                "BrainCore._recover_pending_supersede",
        "kind": "exempt",
        "reason": "crash-recovery rewrite restoring a note's pre-transaction "
                  "content — not note creation",
    },
    "cos._hold_undo._retire_signed_note": {
        "path": "cos/_hold_undo.py _retire_signed_note (undo of an "
                "ALREADY-SIGNED auto-captured note, HARDENED:codex-9)",
        "kind": "exempt",
        "reason": "stamps retired/retired_date/retired_reason on an EXISTING "
                  "note through the audited write path — a retirement, not a "
                  "new note body. Deliberately NOT the supersession keys: an "
                  "undo has no successor, and is_latest_version/"
                  "superseded_date without superseded_by is a shape "
                  "tools/validate.py rejects (AGENTS.md §2)",
    },
}

_VALID_KINDS = {"autolinked", "chained", "exempt", "counted"}


# -- discovery ----------------------------------------------------------------

_DEF_RE = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>\w+)\s*\(")
_CALL_RE = re.compile(r"\.write_note\s*\(")
_CMD_RE = re.compile(r'if\s+cmd\s*==\s*"([\w-]+)"\s*:')


@dataclass(frozen=True)
class CreationSite:
    file: str          # path relative to the scanned root
    function: str      # enclosing function name, or "<module>"
    line: int
    site_id: str       # module-qualified id, the NOTE_CREATION_POLICIES key


def _module_name(root: Path, file: Path) -> str:
    rel = file.relative_to(root).with_suffix("")
    return ".".join(rel.parts)


def discover_creation_sites(src_root: Path) -> list[CreationSite]:
    """Scan every ``*.py`` under ``src_root`` for calls to the ``write_note``
    choke point and return one :class:`CreationSite` per call, with its
    would-be ``NOTE_CREATION_POLICIES`` key already computed. See the module
    docstring for what this scan does and does not catch."""
    return discover_creation_sites_impl(
        src_root,
        module_name=_module_name,
        definition_pattern=_DEF_RE,
        call_pattern=_CALL_RE,
        command_pattern=_CMD_RE,
        site_factory=CreationSite,
    )


def unmapped_sites(sites: list[CreationSite],
                    policies: dict[str, dict[str, str]] | None = None,
                    ) -> list[CreationSite]:
    """Discovered sites whose ``site_id`` has no registry entry (or whose
    entry has a ``kind`` outside the recognised vocabulary)."""
    policies = NOTE_CREATION_POLICIES if policies is None else policies
    out = []
    for s in sites:
        entry = policies.get(s.site_id)
        if entry is None or entry.get("kind") not in _VALID_KINDS:
            out.append(s)
    return out
