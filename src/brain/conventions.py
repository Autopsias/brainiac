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
  - One file (``cli.py``) dispatches many subcommands from a single giant
    function (``_main``); for that file only, the site id is sharpened by
    scanning backward for the nearest ``if cmd == "<name>":`` guard so
    ``brain write`` gets its own registry entry instead of collapsing every
    CLI verb into one ``cli._main`` bucket.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# -- the registry -------------------------------------------------------------

NOTE_CREATION_POLICIES: dict[str, dict[str, str]] = {
    "core.capture": {
        "path": "core.BrainCore.capture (ad-hoc captures AND the daily-note "
                "fold, which calls capture() with note_type='daily')",
        "kind": "chained",
        "reason": "daily notes chain to yesterday's note (LNK-02); ad-hoc "
                  "captures are the caller's responsibility, counted by the "
                  "daily kl_orphans watch (LNK-03b)",
    },
    "core.drain_drafts": {
        "path": "core.BrainCore.drain_drafts (drain-on-invoke promotion of "
                "capture-inbox/ drafts — VM draft_capture, cos-propose, "
                "auto-capture holds all land here)",
        "kind": "autolinked",
        "reason": "sweep-promotion runs autolink.apply_autolinks on the "
                  "draft body before signing (LNK-01)",
    },
    "ingest.pipeline._process_claimed": {
        "path": "ingest.pipeline._process_claimed (inbox document promotion "
                "-> raw/)",
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
    "cli.write": {
        "path": "cli._main, `brain write` dispatch -> core.write_note",
        "kind": "exempt",
        "reason": "host-broker direct write of an operator-authored body — "
                  "links are the caller's own responsibility per AGENTS.md §3",
    },
    "core._supersede_locked": {
        "path": "core.BrainCore._supersede_locked (via supersede())",
        "kind": "exempt",
        "reason": "rewrites frontmatter of two EXISTING notes (both sides of "
                  "a version chain) — no new note body is created",
    },
    "core._recover_pending_supersede": {
        "path": "core.BrainCore._recover_pending_supersede",
        "kind": "exempt",
        "reason": "crash-recovery rewrite restoring a note's pre-transaction "
                  "content — not note creation",
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
    sites: list[CreationSite] = []
    for file in sorted(src_root.rglob("*.py")):
        if "__pycache__" in file.parts:
            continue
        if file.name == "conventions.py":
            # this module's own docstring quotes call-shaped example text
            # (``<expr>.write_note(``) — not a real site, exclude by name
            # rather than trying to out-clever prose detection.
            continue
        lines = file.read_text(encoding="utf-8").splitlines()
        modname = _module_name(src_root, file)
        # indent stack of (def_line_indent, function_name). Popped on ANY
        # NEW logical line (not just `def`s) that dedents to <= a stack
        # entry's indent — otherwise a nested helper (e.g. `_append` inside
        # `_process_claimed`) would wrongly stay "current" for the rest of
        # the outer function once its own body ends. ``paren_depth`` tracks
        # unclosed ``([{`` so a multi-line def signature or call's closing
        # line (indent-aligned back to the opening statement) is treated as
        # a CONTINUATION of the same logical line, never as a dedent —
        # otherwise closing a multi-arg ``def foo(\n  ...\n):`` at the def's
        # own indent would immediately pop the very scope it just opened.
        # Blank/comment-only lines are skipped for indent purposes; a naive
        # char count also can't see brackets inside string literals — a
        # documented limitation (module docstring).
        stack: list[tuple[int, str]] = []
        paren_depth = 0
        for i, line in enumerate(lines, start=1):
            is_continuation = paren_depth > 0
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                indent = len(line) - len(line.lstrip())
                if not is_continuation:
                    m = _DEF_RE.match(line)
                    if m:
                        while stack and indent <= stack[-1][0]:
                            stack.pop()
                        stack.append((indent, m.group("name")))
                    else:
                        while stack and indent <= stack[-1][0]:
                            stack.pop()
            paren_depth += sum(line.count(ch) for ch in "([{")
            paren_depth -= sum(line.count(ch) for ch in ")]}")
            paren_depth = max(paren_depth, 0)
            if not is_continuation and _DEF_RE.match(line):
                continue  # the def line itself is never a call site
            if not _CALL_RE.search(line):
                continue
            enclosing = stack[-1][1] if stack else "<module>"
            if enclosing == "write_note":
                continue  # inside the definition body itself — not a
                          # distinct external creation site.
            site_id = f"{modname}.{enclosing}"
            if modname == "cli":
                cmd_name = None
                for prev in reversed(lines[:i - 1]):
                    cm = _CMD_RE.search(prev)
                    if cm:
                        cmd_name = cm.group(1)
                        break
                    if _DEF_RE.match(prev):
                        break  # walked out of the enclosing function
                if cmd_name:
                    site_id = f"cli.{cmd_name}"
            sites.append(CreationSite(
                file=str(file.relative_to(src_root)), function=enclosing,
                line=i, site_id=site_id,
            ))
    return sites


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
