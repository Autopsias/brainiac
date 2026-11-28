"""SEC-07 PreToolUse egress guard — cross-platform pure-stdlib Python engine.

Replaces the 1,025-line Bash hook with a fast, tested, cross-platform Python
module invoked via isolated Python (``python -I -m brain.egress_guard``) on
both POSIX (through the thin ``scripts/brainiac-egress-guard.sh`` launcher) and
Windows (registered directly in ``settings.json``).

Security invariants preserved:
- Zero eager heavy imports (pure stdlib only, cold-start p95 < 200 ms).
- The environment is NOT an input: ``BRAIN_BIN``, ``BRAINIAC_EGRESS_GUARD``,
  ``BRAIN_OVERLAY_DIR``, ``BRAIN_EGRESS_TERM_MIN_TIER``, and ``BRAIN_VAULT``
  are ignored and scrubbed before invoking the pinned ``brain`` engine.
- Sidecar pins (``.engine`` and ``.registry``) fail closed when present and
  malformed/broken; absent pins degrade to documented allow-when-unconfigured.
- ``Read``, ``Grep``, and ``Glob`` are RECORD-ONLY: they update the per-session
  vault union and exit 0 before any destination check or engine call.
- Shell syntax is selected by tool name (``Bash`` vs ``PowerShell``), NEVER
  ``sys.platform``; malformed quoting, ``$'...'``, ``$(...)``, backticks,
  ``<(...)``, ``eval``, and PowerShell obfuscation FAIL CLOSED.
- ``@file`` / ``< file`` payload attachments are inspected under a strict byte
  cap (256 KiB) and <2 s wall-clock deadline that fails closed before Claude
  Code's 5 s fail-open hook timeout.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from ._egress_guard_paths import (
    check_control_files,
    extract_path_candidates,
    is_abs_path,
    is_contained_write,
    load_registry_entries,
    path_pair,
    resolve_attachment_path,
    resolve_vault_for,
    resolve_vaults_for_many,
    update_session_union,
)
from ._egress_guard_shell import analyze_shell

#: Record-only inbound tools: tracked for cross-vault provenance, never refused.
RECORD_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})

#: Environment variables scrubbed before invoking the pinned `brain` engine.
SCRUBBED_ENV_VARS = frozenset(
    {
        "BRAIN_BIN",
        "BRAINIAC_EGRESS_GUARD",
        "BRAIN_OVERLAY_DIR",
        "BRAIN_EGRESS_TERM_MIN_TIER",
        "BRAIN_VAULT",
        "PYTHONPATH",
        "PYTHONHOME",
    }
)

#: Wall-clock budget for attachment inspection (<2s, failing closed before 5s).
ATTACHMENT_DEADLINE_S = 1.8

#: Overall hook wall-clock budget (<5s Claude Code hook timeout) and policy cap.
HOOK_BUDGET_S = 4.0
POLICY_CHECK_TIMEOUT_S = 3.0

_RECOVERY_HINT = (
    "  If this is the engine broken rather than a real refusal, the recovery "
    "is EXTERNAL and deliberately so: quit Claude Code, remove this hook's entry "
    "from the PreToolUse block of ~/.claude/settings.json (or from the harness repo "
    "that owns it), and start a new session. No environment variable turns this off "
    "— committed project settings can define those."
)


def _fail(msg: str) -> int:
    out = msg.rstrip("\n") + "\n"
    try:
        sys.stderr.write(out)
    except UnicodeEncodeError:
        buf = getattr(sys.stderr, "buffer", None)
        if buf is not None:
            sys.stderr.flush()
            buf.write(out.encode("utf-8", errors="replace"))
            buf.flush()
        else:
            enc = getattr(sys.stderr, "encoding", None) or "ascii"
            sys.stderr.write(
                out.encode(enc, errors="replace").decode(enc, errors="replace")
            )
    return 2


def _parse_args(argv: list[str]) -> tuple[str, str, str, str | None]:
    """Parse ``--script-path``, ``--hooks-dir``, and ``--inherited-path``."""
    script_path = ""
    hooks_dir = ""
    inherited_path = os.environ.get("PATH", "")
    seen: set[str] = set()
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag in ("--script-path", "--hooks-dir", "--inherited-path"):
            if flag in seen:
                return (
                    "",
                    "",
                    "",
                    f"brainiac-egress-guard: duplicate {flag} argument — REFUSING, fail-closed.",
                )
            if i + 1 >= len(argv):
                return (
                    "",
                    "",
                    "",
                    f"brainiac-egress-guard: missing value for {flag} — REFUSING, fail-closed.",
                )
            seen.add(flag)
            if flag == "--script-path":
                script_path = argv[i + 1]
            elif flag == "--hooks-dir":
                hooks_dir = argv[i + 1]
            else:
                inherited_path = argv[i + 1]
            i += 2
        else:
            return (
                "",
                "",
                "",
                f"brainiac-egress-guard: unexpected argument {flag!r} — REFUSING, fail-closed.",
            )
    if not hooks_dir:
        if script_path:
            hooks_dir = os.path.dirname(script_path)
        else:
            hooks_dir = os.path.join(os.path.expanduser("~"), ".claude", "hooks")
    return (script_path, hooks_dir, inherited_path, None)


def _pin_path(script_path: str, hooks_dir: str, suffix: str) -> str:
    if script_path:
        stem = script_path[:-3] if script_path.endswith(".sh") else script_path
        return stem + suffix
    return os.path.join(hooks_dir, "brainiac-egress-guard" + suffix)


def _is_executable(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def _resolve_engine(
    script_path: str,
    hooks_dir: str,
    inherited_path: str,
) -> tuple[str, str | None]:
    """Resolve the ``brain`` engine path; return ``(engine_path, error_msg)``."""
    pin = _pin_path(script_path, hooks_dir, ".engine")
    if os.path.isfile(pin):
        try:
            with open(pin, encoding="utf-8", errors="replace") as fh:
                named = fh.readline().strip()
        except OSError:
            named = ""
        if not is_abs_path(named) or not _is_executable(named):
            return (
                "",
                f"brainiac-egress-guard: the PINNED engine named in {pin} is missing "
                "or not executable — REFUSING, fail-closed.\n"
                "  This host has the SEC-07 guard installed, so an absent engine is a "
                "broken install, not an absence of authority. Re-run `brain install-hook`, "
                "or remove this hook entry from the PreToolUse block of "
                "~/.claude/settings.json OUTSIDE a session.",
            )
        return (named, None)

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".brainiac", "venv", "Scripts", "brain.exe"),
        os.path.join(home, ".brainiac", "venv", "bin", "brain"),
        os.path.join(home, ".local", "bin", "brain"),
        "/usr/local/bin/brain",
        "/opt/homebrew/bin/brain",
        "/usr/bin/brain",
    ]
    for cand in candidates:
        if _is_executable(cand):
            return (cand, None)

    import shutil

    found = shutil.which("brain", path=inherited_path) or ""
    if found and is_abs_path(found) and _is_executable(found):
        return (found, None)
    return ("", None)


def _resolve_registry_pin(script_path: str, hooks_dir: str) -> tuple[str, str | None]:
    """Read ``.registry`` pin if present; refuse if non-absolute."""
    pin = _pin_path(script_path, hooks_dir, ".registry")
    if not os.path.isfile(pin):
        return ("", None)
    try:
        with open(pin, encoding="utf-8", errors="replace") as fh:
            named = fh.readline().strip()
    except OSError:
        named = ""
    if not is_abs_path(named):
        return (
            "",
            f"brainiac-egress-guard: {pin} does not name an absolute path "
            "— REFUSING, fail-closed.\n"
            "  Re-run `brain install-hook`, or remove this hook entry from the "
            "PreToolUse block of ~/.claude/settings.json OUTSIDE a session.",
        )
    return (named, None)


def _has_linebreak(val: Any) -> bool:
    return isinstance(val, str) and ("\n" in val or "\r" in val)


def _extract_tool_text(tool: str, ti: dict[str, Any], raw_extra: list[str]) -> str:
    """Extract only the outbound text for ``tool`` (never ``file_path``)."""
    parts: list[Any]
    if tool in ("Bash", "PowerShell"):
        parts = [ti.get("command")]
    elif tool == "Write":
        parts = [ti.get("content")]
    elif tool == "NotebookEdit":
        parts = [ti.get("new_source")]
    elif tool == "Edit":
        parts = [ti.get("old_string"), ti.get("new_string")]
    elif tool == "MultiEdit":
        raw_edits = ti.get("edits")
        edits = raw_edits if isinstance(raw_edits, list) else []
        parts = [
            val
            for e in edits
            if isinstance(e, dict)
            for val in (e.get("old_string"), e.get("new_string"))
        ]
    elif tool in ("WebSearch", "WebFetch"):
        raw_allowed = ti.get("allowed_domains")
        allowed = raw_allowed if isinstance(raw_allowed, list) else []
        raw_blocked = ti.get("blocked_domains")
        blocked = raw_blocked if isinstance(raw_blocked, list) else []
        parts = [ti.get("query"), ti.get("url"), ti.get("prompt")]
        parts.extend(str(d) for d in allowed)
        parts.extend(str(d) for d in blocked)
    else:
        parts = [json.dumps(ti, ensure_ascii=False)] if ti else []
    parts.extend(raw_extra)
    return " ".join(p for p in parts if isinstance(p, str))


def _parse_payload(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse stdin JSON and enforce framing-field line-break invariants."""
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return (
            None,
            "brainiac-egress-guard: the field extraction produced no usable frame "
            "— REFUSING, fail-closed.\n"
            "  The hook payload was not a valid JSON object, so the guard has no "
            "tool name, no vault and no decoder ring to decide with.",
        )

    raw_ti = data.get("tool_input")
    ti = raw_ti if isinstance(raw_ti, dict) else {}
    raw_extra = (
        []
        if isinstance(raw_ti, dict)
        else ([str(raw_ti)] if raw_ti is not None else [])
    )

    raw_path = ti.get("path")
    checked_path = (
        raw_path if isinstance(raw_path, str) and is_abs_path(raw_path) else ""
    )
    framing_checks = (
        ("tool_name", data.get("tool_name") or ""),
        ("cwd", data.get("cwd") or ""),
        ("session_id", data.get("session_id") or ""),
        ("tool_input.file_path", ti.get("file_path") or ""),
        ("tool_input.notebook_path", ti.get("notebook_path") or ""),
        ("tool_input.path", checked_path),
    )
    for field_name, val in framing_checks:
        if _has_linebreak(val):
            return (
                None,
                f"brainiac-egress-guard: `{field_name}` contains a line break — "
                "REFUSING, fail-closed.\n"
                "  A path, tool name, cwd or session id may not contain one: the guard "
                "would have to decide about a repaired value that is not what the tool "
                f"receives. Re-issue the call with the line break removed from `{field_name}`.",
            )

    tool = str(data.get("tool_name") or "")
    target = str(ti.get("file_path") or ti.get("notebook_path") or "")
    hook_cwd = str(data.get("cwd") or "")
    named_paths = [
        v if is_abs_path(v) else resolve_attachment_path(v, hook_cwd)
        for v in (ti.get("file_path"), ti.get("notebook_path"), ti.get("path"))
        if isinstance(v, str) and v and (is_abs_path(v) or hook_cwd)
    ]
    extracted = _extract_tool_text(tool, ti, raw_extra)
    return (
        {
            "tool": tool,
            "target": target,
            "cwd": hook_cwd,
            "session_id": str(data.get("session_id") or ""),
            "named_paths": named_paths,
            "text": extracted if extracted else raw,
        },
        None,
    )


def _run_check_egress(
    engine: str,
    vaults: list[str],
    text: str,
    *,
    strict: bool,
    timeout: float = POLICY_CHECK_TIMEOUT_S,
) -> int:
    """Invoke ``brain check-egress`` with a scrubbed environment; return exit code."""
    if timeout <= 0:
        return _fail(
            "brainiac-egress-guard: check FAILED (timeout) — REFUSING, fail-closed.\n"
            + _RECOVERY_HINT
        )
    cmd = [engine]
    if vaults:
        cmd.extend(["--vault", vaults[0]])
    cmd.append("check-egress")
    for extra in vaults[1:]:
        cmd.extend(["--extra-vault", extra])
    if strict:
        cmd.append("--strict")

    import subprocess

    child_env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV_VARS}
    try:
        proc = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            "brainiac-egress-guard: check FAILED (timeout) — REFUSING, fail-closed.\n"
            + _RECOVERY_HINT
        )
    except OSError as exc:
        return _fail(
            f"brainiac-egress-guard: check FAILED ({type(exc).__name__}) "
            "— REFUSING, fail-closed.\n" + _RECOVERY_HINT
        )

    verdict = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return 0
    if proc.returncode == 6:
        return _fail(verdict)
    return _fail(
        f"brainiac-egress-guard: check FAILED (exit {proc.returncode}) "
        f"— REFUSING, fail-closed. {verdict.strip()}\n" + _RECOVERY_HINT
    )


def _resolve_outbound_text(
    tool: str,
    text: str,
    target: str,
    hook_cwd: str,
    guard_vault: str,
) -> tuple[bool, str | None, str, tuple[str, ...]]:
    """Return ``(should_check, deny_reason, text_to_check, resolved_paths)``."""
    if tool in ("Bash", "PowerShell"):
        deadline = time.monotonic() + ATTACHMENT_DEADLINE_S
        analysis = analyze_shell(
            tool,
            text,
            cwd=hook_cwd or os.getcwd(),
            guard_vault=guard_vault,
            deadline=deadline,
        )
        if analysis.deny_reason:
            return (
                False,
                f"brainiac-egress-guard: {analysis.deny_reason}",
                "",
                analysis.resolved_paths,
            )
        if not analysis.outbound:
            return (False, None, "", analysis.resolved_paths)
        check_text = text + "\n" + analysis.extra_text if analysis.extra_text else text
        return (True, None, check_text, analysis.resolved_paths)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        if is_contained_write(target, hook_cwd, os.getcwd(), guard_vault):
            return (False, None, "", ())
    return (True, None, text, ())


def _control_file_deny(own_hit: str) -> int:
    return _fail(
        f"brainiac-egress-guard: this call names the guard's own control files "
        f"({own_hit}) — REFUSING.\n"
        "  The engine pin, the workspace registry and the session union decide "
        "what this guard trusts, so a session may not write them. Change them "
        "OUTSIDE a session, or re-run `brain install-hook`."
    )


def _apply_resolved_paths(
    resolved_paths: tuple[str, ...],
    hooks_dir: str,
    registry_path: str,
    entries: list[dict[str, Any]],
    session_id: str,
    guard_vault: str,
    named_vaults: list[str],
    vaults: list[str],
) -> tuple[list[str], str | None]:
    if not resolved_paths:
        return (vaults, None)
    resolved_pairs = [path_pair(p) for p in resolved_paths]
    if own_resolved := check_control_files(
        hooks_dir, registry_path, "", "", resolved_pairs
    ):
        return (vaults, own_resolved)
    extra_vaults = resolve_vaults_for_many(resolved_pairs, "", entries)
    if extra_vaults:
        vaults = update_session_union(
            registry_path, session_id, guard_vault, [*named_vaults, *extra_vaults]
        )
    return (vaults, None)


def run_guard(argv: list[str], payload: str) -> int:
    """Execute the full PreToolUse guard lifecycle and return process exit code."""
    hook_deadline = time.monotonic() + HOOK_BUDGET_S
    script_path, hooks_dir, inherited_path, arg_err = _parse_args(argv)
    if arg_err:
        return _fail(arg_err)
    engine, eng_err = _resolve_engine(script_path, hooks_dir, inherited_path)
    if eng_err:
        return _fail(eng_err)
    if not engine:
        return 0  # no engine here, no opinion

    registry_path, reg_err = _resolve_registry_pin(script_path, hooks_dir)
    if reg_err:
        return _fail(reg_err)

    frame, frame_err = _parse_payload(payload)
    if frame_err or frame is None:
        return _fail(
            frame_err
            or "brainiac-egress-guard: no usable frame — REFUSING, fail-closed."
        )

    tool, target, hook_cwd, text = (
        frame["tool"],
        frame["target"],
        frame["cwd"],
        frame["text"],
    )
    entries = load_registry_entries(registry_path)
    guard_vault = resolve_vault_for(hook_cwd, entries)
    pairs = extract_path_candidates(frame["named_paths"], tool, text)
    named_vaults = resolve_vaults_for_many(pairs, text, entries)
    vaults = update_session_union(
        registry_path, frame["session_id"], guard_vault, named_vaults
    )

    if tool in RECORD_ONLY_TOOLS:
        return 0

    if own_hit := check_control_files(hooks_dir, registry_path, text, target, pairs):
        return _control_file_deny(own_hit)

    should_check, deny_msg, check_text, resolved_paths = _resolve_outbound_text(
        tool, text, target, hook_cwd, guard_vault
    )
    vaults, own_resolved = _apply_resolved_paths(
        resolved_paths,
        hooks_dir,
        registry_path,
        entries,
        frame["session_id"],
        guard_vault,
        named_vaults,
        vaults,
    )
    if own_resolved:
        return _control_file_deny(own_resolved)
    if deny_msg:
        return _fail(deny_msg)
    if not should_check:
        return 0

    remaining = min(POLICY_CHECK_TIMEOUT_S, hook_deadline - time.monotonic())
    return _run_check_egress(
        engine,
        vaults,
        check_text,
        strict=(tool in ("WebSearch", "WebFetch")),
        timeout=remaining,
    )


def _read_stdin_utf8() -> str:
    """Read hook payload as UTF-8 from ``stdin.buffer``, falling back if absent."""
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        raw = buf.read()
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return bytes(raw).decode("utf-8", errors="replace")
        return str(raw)
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    payload = _read_stdin_utf8()
    return run_guard(sys.argv[1:] if argv is None else argv, payload)


if __name__ == "__main__":
    raise SystemExit(main())
