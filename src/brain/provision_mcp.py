"""Wire 5 — this vault's own Claude Desktop MCP server entry.

Its own module because the Desktop config is a THIRD-party file with a
third party still writing to it: every guard below exists because the
write landing is not the same thing as the entry being there afterwards.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .pathkey import file_lock, same_path


def mcp_server_name(workspace: Path) -> str:
    """A per-vault MCP server name derived from the workspace folder.

    NOT the bare default `brainiac`: that name is a single global slot, so a
    second vault registering under it would silently repoint Claude Desktop
    away from the first. One vault, one server, one name.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-")
    return f"brainiac-{slug}" if slug else "brainiac"


def _entry_matches(got: Optional[dict], want: dict) -> bool:
    """Is the entry on disk the one we planned? Whole-entry, not just the name:
    another writer can replace the slot between the merge and the re-read with
    the same name but a different vault, command or tier, and a name-only check
    would call that success. ``BRAIN_VAULT`` compares as a PATH."""
    if not isinstance(got, dict) or got.get("command") != want.get("command"):
        return False
    genv, wenv = dict(got.get("env") or {}), dict(want.get("env") or {})
    if not same_path(genv.pop("BRAIN_VAULT", ""), wenv.pop("BRAIN_VAULT", "")):
        return False
    return genv == wenv and set(got) == set(want)


def register_mcp(vault: Path, workspace: Path, *,
                 max_tier: str = "Internal",
                 config_path: Path | None = None) -> dict[str, Any]:
    """Register this vault as its own Claude Desktop MCP server.

    Without this a newly provisioned vault is unreachable from the Desktop
    Chat tab and from Cowork's MCP-on-host retrieval path — the vault
    installs, stages and indexes correctly and still cannot be queried, which
    is exactly what a new vault looked like before 2026-08-17.

    Idempotent and additive: `plan_claude_desktop` MERGES into the existing
    `mcpServers` map and reports `noop` when the entry already matches, so
    re-running never disturbs another vault's server or any unrelated one.

    Three guards, each closing a way this could point Desktop at the wrong
    thing (2026-08-30). COLLISION: ``mcp_server_name`` slugs ``Client A`` and
    ``client-a`` identically and ``plan_claude_desktop`` assigns the slot
    unconditionally, so wiring the second workspace would silently repoint the
    first vault's server — refuse whenever the name is held by anything not
    positively bound to THIS vault, naming both sides. CONCURRENCY: the
    read-modify-write had no lock, so two provisions landing together kept
    whichever wrote last. RE-READ: Claude Desktop is reported to rewrite
    ``mcpServers`` and drop hand-added stdio entries
    (anthropics/claude-code#63549), so the write returning is not evidence the
    entry is there — read the WHOLE entry back and compare it.

    Never raises: the drain treats a failure as a reported gap;
    ``provision-local`` surfaces it as ``wire 5 FAILED``.
    """
    from . import connect as _connect

    try:
        cfg = Path(config_path) if config_path else _connect.claude_desktop_config_path()
        name = mcp_server_name(workspace)
        want = _connect.mcp_server_entry(str(vault), name, max_tier)[name]
        with file_lock(vault, cfg):
            existing = (_connect._load_json(cfg).get("mcpServers") or {}).get(name)
            # The slot is ours ONLY if what sits in it is positively bound to
            # THIS vault. Guarding on `held and not same_path(...)` meant an
            # entry with no `BRAIN_VAULT` at all — any unrelated MCP server
            # that happens to hold the derived name — read as "free to take",
            # and the merge below overwrote a working entry in the owner's real
            # Claude Desktop config. Absence of a binding is "not ours".
            if existing is not None:
                held = ((existing.get("env") or {}).get("BRAIN_VAULT")
                        if isinstance(existing, dict) else None)
                if not same_path(held or "", vault):
                    return {"status": "failed", "name": name, "config": str(cfg),
                            "error": f"MCP server name {name!r} is already taken by "
                                     f"an entry that is not this vault's: "
                                     f"existing={held or '<no BRAIN_VAULT>'} "
                                     f"requested={vault}. Rename the workspace "
                                     f"folder (the name is derived from it) or "
                                     f"edit {cfg}."}
            plan = _connect.plan_claude_desktop(cfg, str(vault), name, max_tier)
            if plan.already_connected:
                return {"status": "already-registered", "name": name,
                        "config": str(cfg)}
            _connect.apply_json_merge(plan)
            got = (_connect._load_json(cfg).get("mcpServers") or {}).get(name)
            if not _entry_matches(got, want):
                return {"status": "failed", "name": name, "config": str(cfg),
                        "error": f"merged {name} into {cfg} but the entry on "
                                 f"re-read is absent or different "
                                 f"(got={got!r}) — Claude Desktop rewrites "
                                 f"mcpServers; make the file read-only with "
                                 f"`chflags uchg` if this recurs"}
        return {"status": "registered", "name": name, "config": str(cfg),
                "max_tier": max_tier,
                "note": "restart Claude Desktop for the new server to appear"}
    except Exception as exc:  # noqa: BLE001 — never fail a provision over this
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


