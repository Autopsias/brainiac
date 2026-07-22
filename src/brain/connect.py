"""``brain connect`` (SUI-02) — universal per-client wirer.

``brain connect --client claude-code|claude-desktop|codex|gemini`` writes
that client's wiring itself instead of the human hand-copying four different
JSON/Markdown snippets from docs. HOST-ONLY (refused on ``role=vm`` at the
same CLI trust gate as ``supersede``/``ingest`` — see ``cli.py``
``VM_ALLOWED``): this module never touches ``BrainCore`` at all, so there is
nothing role-gated inside it beyond that refusal.

Every client transform is a pure ``plan_*`` function (old text -> new text ->
unified diff -> ``already_connected`` flag) so it is unit-testable against
fixture files with zero writes to the real home directory. ``apply_*``
performs the actual write + first-mutation backup. The interactive
diff-then-confirm loop and ``--yes``/non-interactive exit-nonzero contract
live in ``cli.py`` (same shape as the existing ``init --import-from`` dry-run
gate), not here.

Two writers for the Desktop MCP stanza would be a bug (the hardening addendum
for this session is explicit: verified there is currently NO Desktop-config
writer anywhere in this codebase — ``mcp-config`` only prints). This module
is that ONE writer; ``cli.py``'s ``mcp-config`` stays print-only and unchanged.
"""
from __future__ import annotations

import difflib
import json
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

CLIENTS = ("claude-code", "claude-desktop", "codex", "gemini")

MARKETPLACE_NAME = "brainiac"
KERNEL_PLUGIN = "brainiac-kernel"
DEFAULT_MARKETPLACE_SOURCE = "Autopsias/brainiac"  # README's documented add-source

# Marked block used for the two Markdown-append clients (claude-code's
# CLAUDE.md, codex's AGENTS.md) so a re-run detects "already connected" and
# --remove can strip exactly this block without touching anything else a
# human wrote in the file.
BLOCK_START = "<!-- brainiac:connect:begin -->"
BLOCK_END = "<!-- brainiac:connect:end -->"

BRAIN_USAGE_PARAGRAPH = (
    "## Brain usage\n\n"
    "Retrieval, capture, and indexing in this project are owned by the "
    "**`brain` CLI** — call it from your native shell, never via MCP. Read "
    "tools: `brain search \"<q>\" --json`, `brain get <id> --json`, "
    "`brain recent --json`; capture with `brain draft-capture`. Every read "
    "applies a classification egress filter before stdout. Run "
    "`brain --help` for the always-current contract."
)


def _marked_block(body: str) -> str:
    return f"{BLOCK_START}\n{body}\n{BLOCK_END}"


_BLOCK_RE = re.compile(
    re.escape(BLOCK_START) + r"\n.*?\n" + re.escape(BLOCK_END) + r"\n?",
    re.DOTALL,
)


# --------------------------------------------------------------------------
# Plan result — the diff-first contract every client transform returns
# --------------------------------------------------------------------------

@dataclass
class ConnectPlan:
    client: str
    target_path: Path
    action: str  # "noop" | "create" | "append" | "merge" | "remove"
    old_text: Optional[str]
    new_text: Optional[str]
    already_connected: bool
    diff: str
    kind: str = "file"  # "file" | "subprocess" (claude-code plugin install)
    extra: dict = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return self.action not in ("noop",)


def _diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{path} (before)", tofile=f"{path} (after)"))


def _backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d", time.gmtime())
    return path.with_name(path.name + f".bak-{stamp}")


def backup_original(path: Path) -> Optional[Path]:
    """Back up ``path`` alongside itself on FIRST mutation only — a backup
    that already exists (today's stamp) is left untouched so the original
    pre-connect content is never clobbered by a second run's backup."""
    if not path.exists():
        return None
    bak = _backup_path(path)
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Markdown marked-block clients: claude-code's project CLAUDE.md, codex's
# project AGENTS.md
# --------------------------------------------------------------------------

def plan_marked_block(target_path: Path, body: str = BRAIN_USAGE_PARAGRAPH) -> ConnectPlan:
    old_text = target_path.read_text(encoding="utf-8") if target_path.exists() else None
    block = _marked_block(body)
    if old_text is not None and BLOCK_START in old_text:
        return ConnectPlan(
            client="", target_path=target_path, action="noop",
            old_text=old_text, new_text=old_text, already_connected=True, diff="")
    base = old_text or ""
    sep = "" if not base or base.endswith("\n\n") else ("\n" if base.endswith("\n") else "\n\n")
    new_text = base + sep + block + "\n"
    return ConnectPlan(
        client="", target_path=target_path,
        action="append" if old_text is not None else "create",
        old_text=old_text, new_text=new_text, already_connected=False,
        diff=_diff(old_text or "", new_text, target_path))


def apply_marked_block(plan: ConnectPlan) -> None:
    if plan.old_text is not None:
        backup_original(plan.target_path)
    write_text(plan.target_path, plan.new_text or "")


def plan_remove_marked_block(target_path: Path) -> ConnectPlan:
    if not target_path.exists():
        return ConnectPlan(client="", target_path=target_path, action="noop",
                            old_text=None, new_text=None, already_connected=False, diff="")
    old_text = target_path.read_text(encoding="utf-8")
    new_text = _BLOCK_RE.sub("", old_text)
    # collapse a leftover blank-line gap left by the removed separator
    new_text = re.sub(r"\n{3,}\Z", "\n", new_text)
    if new_text == old_text:
        return ConnectPlan(client="", target_path=target_path, action="noop",
                            old_text=old_text, new_text=old_text,
                            already_connected=False, diff="")
    return ConnectPlan(client="", target_path=target_path, action="remove",
                        old_text=old_text, new_text=new_text,
                        already_connected=False,
                        diff=_diff(old_text, new_text, target_path))


def apply_remove_marked_block(plan: ConnectPlan) -> None:
    if plan.action != "remove":
        return
    backup_original(plan.target_path)
    write_text(plan.target_path, plan.new_text or "")


# --------------------------------------------------------------------------
# JSON merge clients: claude-desktop's claude_desktop_config.json,
# gemini's .gemini/settings.json. Merge, never replace — preserve unknown
# top-level keys AND unrelated mcpServers entries.
# --------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def mcp_server_entry(vault: str, name: str = "brainiac", max_tier: str = "Internal") -> dict:
    """The SAME entry shape ``brain mcp-config`` prints (cli.py) — the one
    place this shape is built; both the print-only command and this writer
    call it so they can never drift apart."""
    import sys as _sys

    brain_mcp = shutil.which("brain-mcp") or str(Path(_sys.executable).parent / "brain-mcp")
    env = {"BRAIN_VAULT": str(vault), "BRAIN_MAX_EGRESS_TIER": max_tier}
    model_dir = Path(vault) / ".brain" / "model"
    if model_dir.is_dir():
        env["BRAIN_MODEL_CACHE"] = str(model_dir)
    return {name: {"command": brain_mcp, "env": env}}


def claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        import os

        appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return appdata / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"  # linux fallback


def plan_claude_desktop(config_path: Path, vault: str, name: str = "brainiac",
                         max_tier: str = "Internal") -> ConnectPlan:
    old_data = _load_json(config_path)
    entry = mcp_server_entry(vault, name, max_tier)
    new_data = dict(old_data)
    servers = dict(new_data.get("mcpServers") or {})
    already = servers.get(name) == entry[name]
    servers[name] = entry[name]
    new_data["mcpServers"] = servers
    new_text = _dump_json(new_data)
    old_rendered = _dump_json(old_data) if config_path.exists() else ""
    if already:
        return ConnectPlan(client="claude-desktop", target_path=config_path,
                            action="noop", old_text=old_rendered, new_text=old_rendered,
                            already_connected=True, diff="")
    return ConnectPlan(
        client="claude-desktop", target_path=config_path,
        action="merge" if config_path.exists() else "create",
        old_text=old_rendered or None, new_text=new_text, already_connected=False,
        diff=_diff(old_rendered, new_text, config_path))


def apply_json_merge(plan: ConnectPlan) -> None:
    if plan.old_text:
        backup_original(plan.target_path)
    write_text(plan.target_path, plan.new_text or "")


def plan_gemini(settings_path: Path) -> ConnectPlan:
    old_data = _load_json(settings_path)
    old_rendered = _dump_json(old_data) if settings_path.exists() else ""
    if old_data.get("contextFileName") == "AGENTS.md":
        return ConnectPlan(client="gemini", target_path=settings_path, action="noop",
                            old_text=old_rendered, new_text=old_rendered,
                            already_connected=True, diff="")
    new_data = dict(old_data)
    new_data["contextFileName"] = "AGENTS.md"
    new_text = _dump_json(new_data)
    return ConnectPlan(
        client="gemini", target_path=settings_path,
        action="merge" if settings_path.exists() else "create",
        old_text=old_rendered or None, new_text=new_text, already_connected=False,
        diff=_diff(old_rendered, new_text, settings_path))


def plan_restore_from_backup(target_path: Path) -> dict:
    """--remove for the two JSON-merge clients: restore the FIRST backup
    (file.bak-<oldest date found>) rather than the marked-block strip used
    for the Markdown clients — there's no marker inside JSON to strip."""
    candidates = sorted(target_path.parent.glob(target_path.name + ".bak-*"))
    if not candidates:
        return {"ok": False, "reason": "no backup found; nothing to unwire",
                "target": str(target_path)}
    bak = candidates[0]  # earliest stamp = closest to pre-connect original
    return {"ok": True, "backup": str(bak), "target": str(target_path)}


def apply_restore_from_backup(target_path: Path, backup: Path) -> None:
    shutil.copy2(backup, target_path)


# --------------------------------------------------------------------------
# claude-code: real plugin-CLI mutation (verified non-interactive in this
# session's recon — `claude plugin marketplace add` / `claude plugin install`
# both exist), THEN the CLAUDE.md marked-block append. If the `claude`
# binary/subcommand surface is missing, callers fall back to printing the
# two commands (see cli.py) instead of claiming a mutation that didn't happen.
# --------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 120)
    return subprocess.run(cmd, **kwargs)


def claude_code_plugin_commands(marketplace_source: str = DEFAULT_MARKETPLACE_SOURCE,
                                 plugin: str = KERNEL_PLUGIN) -> list[list[str]]:
    claude_bin = shutil.which("claude") or "claude"
    return [
        [claude_bin, "plugin", "marketplace", "add", marketplace_source],
        [claude_bin, "plugin", "install", f"{plugin}@{MARKETPLACE_NAME}"],
    ]


def claude_plugin_cli_available(run: Runner = _default_runner) -> bool:
    if shutil.which("claude") is None:
        return False
    try:
        out = run([shutil.which("claude"), "plugin", "--help"])
    except Exception:  # noqa: BLE001 — any probe failure means "not available"
        return False
    text = ((out.stdout or "") + (out.stderr or "")).lower()
    return "marketplace" in text and "install" in text


def is_plugin_installed(claude_home: Path, plugin: str = KERNEL_PLUGIN) -> bool:
    installed_json = claude_home / "plugins" / "installed_plugins.json"
    if not installed_json.exists():
        return False
    try:
        data = json.loads(installed_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return bool((data.get("plugins") or {}).get(f"{plugin}@{MARKETPLACE_NAME}"))


def run_claude_code_plugin_install(
    marketplace_source: str = DEFAULT_MARKETPLACE_SOURCE, plugin: str = KERNEL_PLUGIN,
    run: Runner = _default_runner, claude_home: Optional[Path] = None,
) -> dict:
    claude_home = claude_home or (Path.home() / ".claude")
    cmds = claude_code_plugin_commands(marketplace_source, plugin)
    results = []
    for cmd in cmds:
        try:
            out = run(cmd)
            results.append({"cmd": cmd, "returncode": out.returncode,
                             "stdout": out.stdout, "stderr": out.stderr})
            if out.returncode != 0:
                return {"ok": False, "steps": results,
                        "reason": f"`{' '.join(cmd)}` exited {out.returncode}"}
        except Exception as exc:  # noqa: BLE001
            results.append({"cmd": cmd, "error": f"{type(exc).__name__}: {exc}"})
            return {"ok": False, "steps": results, "reason": str(exc)}
    verified = is_plugin_installed(claude_home, plugin)
    return {"ok": verified, "steps": results, "verified_installed": verified}


def run_claude_code_plugin_uninstall(
    plugin: str = KERNEL_PLUGIN, run: Runner = _default_runner,
    claude_home: Optional[Path] = None,
) -> dict:
    claude_home = claude_home or (Path.home() / ".claude")
    claude_bin = shutil.which("claude") or "claude"
    cmd = [claude_bin, "plugin", "uninstall", f"{plugin}@{MARKETPLACE_NAME}"]
    try:
        out = run(cmd)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "cmd": cmd, "error": f"{type(exc).__name__}: {exc}"}
    still_installed = is_plugin_installed(claude_home, plugin)
    return {"ok": out.returncode == 0, "cmd": cmd, "returncode": out.returncode,
            "stdout": out.stdout, "stderr": out.stderr,
            "verified_removed": not still_installed}
