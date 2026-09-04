"""Implement the MCP housekeeping-verb bodies (closed-stacks S03, DESK-03).

Split out of :mod:`brain.mcp_verbs` (file-size ratchet: adding these two
functions there crossed the 500-line limit) rather than folded into it — the
same seam :mod:`brain.mcp_mediation` was split along in S02, when that module
crossed the same ratchet. A LEAF module, same shape as ``mcp_verbs``: at
module scope it imports only the stdlib typing helper, and lazily inside each
function the module it delegates to — nothing from ``mcp_adapter``.

``alerts``/``exceptions`` are pure calls into the SAME functions the CLI runs
(``alerts.collect`` / ``exceptions_cli.collect``), so the JSON shape is
identical by construction, not by a second hand-kept copy of it, **with one
deliberate exception: the ``vaults`` list (LOW-02).** Both underlying
functions were built for the CLI, which runs as the owner on their own
machine and legitimately gets each registered vault's absolute path back.
The broker serves that same call to whatever MCP client is on the other end
of the transport, so ``_redact_vault_paths`` below strips every absolute
path from the response before it leaves this process, replacing it with a
``name`` (the vault's parent directory name — already what
``exceptions_cli.vault_row``/``alerts.vault_alerts`` use for display) and an
``id`` (``config.vault_slug8`` — the same derived hash the per-vault app-data
dir is keyed on, so two vaults never collide and one vault's id never
changes). ``brain alerts --json``/``brain exceptions --json`` at the CLI are
UNCHANGED and keep real paths. Both already branch correctly on
``core.role``: on ``vm`` the exceptions summary is VERIFIED (signature,
pinned workspace identity, schema, freshness — ``exceptions_verify.verify``,
called from inside
``alerts.exceptions_alerts``/``exceptions_cli.vault_row``) before anything in
it is trusted, and the two host-home sources
(``~/.brainiac/update-state.json``, ``~/.brain/synthesis-state.json``) come
back under ``unreachable`` rather than being silently skipped. That
verification is NOT reimplemented here — it is the same call the CLI makes,
so there is exactly one place it could regress. No note bodies, no
``max_tier``: both are declared in ``mcp_mediation.BODYLESS_TOOLS``.
"""
from __future__ import annotations

from typing import Any


def _vault_ref(path: Any) -> dict[str, str]:
    """A vault named WITHOUT its absolute filesystem path."""
    from pathlib import Path

    from . import config as _config

    p = Path(path)
    return {"name": p.parent.name or str(p), "id": _config.vault_slug8(p)}


def _redact_vault_paths(report: dict[str, Any]) -> dict[str, Any]:
    """Replace every absolute vault path in a housekeeping report's
    ``vaults`` list with a ``name``/``id`` pair (LOW-02). ``alerts.collect``
    returns bare path strings there; ``exceptions_cli.collect`` returns dicts
    carrying both a ``vault`` (path) and a ``page`` (path) key — both
    filesystem locations on the machine the broker runs on, of no use to a
    remote MCP caller and no business being handed to one."""
    vaults = report.get("vaults")
    if not isinstance(vaults, list):
        return report
    redacted: list[Any] = []
    for entry in vaults:
        if isinstance(entry, dict) and "vault" in entry:
            row = dict(entry)
            row["id"] = _vault_ref(row.pop("vault"))["id"]
            row.pop("page", None)
            redacted.append(row)
        elif isinstance(entry, (str, type(None))) and entry:
            redacted.append(_vault_ref(entry))
        else:
            redacted.append(entry)
    out = dict(report)
    out["vaults"] = redacted
    return out


def dispatch_alerts(
    _tool: str,
    _args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the degradation-digest body — same payload as ``brain alerts --json``,
    minus absolute vault paths (LOW-02, see module docstring)."""  # noqa: ARG001
    from . import alerts as _alerts

    return _redact_vault_paths(_alerts.collect(role=core.role, vault=core.vault))


def dispatch_exceptions(
    _tool: str,
    _args: dict[str, Any],
    *,
    core: Any,
    max_tier: str,
) -> dict[str, Any]:
    """Run the exceptions-summary body — same payload as ``brain exceptions --json``.

    Registered under TWO tool names, ``exceptions`` and ``inbox`` (identical
    body, same reason ``read``/``get`` and ``hybrid_search``/``search`` are two
    names for one call). There is no separate ``inbox`` verb here on purpose:
    the real owner-decision queue (``brain inbox``, ``inbox.jsonl``) is
    HOST-ONLY doctrine (the owner ruled 2026-08-22 that "attacker-writable
    file existence is not evidence") and the ``brain-inbox`` skill that reads
    it is host-only for the same reason. What a Cowork session needs when it
    asks "what's in my inbox" is answered by the SAME signed summary
    ``exceptions`` already serves — never the raw file, and never real
    question text (the summary carries only a count and an opaque key set;
    the full question/options/context text lives only in the host-only page).

    Absolute vault paths are stripped (LOW-02, see module docstring): each
    row's ``vault``/``page`` path becomes an ``id``.
    """  # noqa: ARG001
    from . import exceptions_cli as _exc_cli

    return _redact_vault_paths(_exc_cli.collect(role=core.role, vault=core.vault))
