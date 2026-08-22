"""Untriaged-key resolution + staged-Cowork-workspace alerts for ``brain
alerts``.

Split out of :mod:`brain.alerts` purely to keep that file under the
file-size ratchet — no behaviour change. See that module's docstring for the
degradation-digest design rationale.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: What an undeclared key renders as when the registry cannot be loaded AT
#: ALL. This module may not ask the registry for the string in that case —
#: asking is what failed — so the constant is duplicated here and pinned equal
#: to ``remediation.UNTRIAGED`` by a test.
_UNTRIAGED_FALLBACK = "UNTRIAGED"

#: REG-03's wrapper prefix, duplicated (and pinned by a test) for the same
#: reason as the constant above.
_UNTRIAGED_PREFIX_FALLBACK = "untriaged:"


def _unwrap_untriaged(key: str) -> str:
    """``untriaged:<key>`` -> ``<key>``; anything else unchanged."""
    rem = _remediation()
    prefix = _UNTRIAGED_PREFIX_FALLBACK if rem is None else rem.UNTRIAGED_PREFIX
    return key[len(prefix):] if key.startswith(prefix) else key


def _remediation() -> Any:
    """The remediation registry module, or ``None`` when it cannot be loaded.

    The import RUNS ``remediation.validate()``, so a table edit that breaks a
    structural rule (an ``auto`` row naming a branch with no declared cadence,
    two overlapping prefixes) raises HERE — on every harness, at every session
    start, on every vault. `brain alerts` reporting NOTHING is the one outcome
    this module may never produce, so an unloadable registry degrades every key
    to UNTRIAGED — itself a banner — instead of tracebacking."""
    try:
        from . import remediation

        return remediation
    except Exception:  # noqa: BLE001 — a broken table must not silence alerts
        return None


def _untriaged_key() -> str:
    """The key the UNTRIAGED line is stamped with, registry or no registry."""
    rem = _remediation()
    return _UNTRIAGED_FALLBACK if rem is None else rem.UNTRIAGED


def _untriaged(key: str) -> bool:
    """Whether the registry declares nothing for ``key``.

    An AMBIGUOUS key (two prefixes matched — a table bug) counts as untriaged
    rather than crashing the session-start digest, and so does an UNLOADABLE
    registry: `brain alerts` reporting nothing is the one outcome this module
    may never produce."""
    rem = _remediation()
    if rem is None:
        return True
    try:
        return rem.resolve(key) is None
    except rem.RegistryError:
        return True


def _staged_versions(vault: Path) -> set[str]:
    """Every version stamp a staged Cowork workspace carries, as file reads.

    Two stamp kinds exist: the staged engine's ``_version.py`` and the
    ``brain-linux-*.version`` sidecars beside the VM ELFs. A missing stamp
    contributes nothing — absence means "never staged", which is not
    staleness."""
    found: set[str] = set()
    try:
        text = (vault / ".brain" / "engine" / "brain" / "_version.py").read_text(
            encoding="utf-8")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        if m:
            found.add(m.group(1))
    except OSError:
        pass
    try:
        for stamp in sorted((vault / ".brain" / "bin").glob("brain-linux-*.version")):
            v = stamp.read_text(encoding="utf-8").strip()
            if v:
                found.add(v)
    except OSError:
        pass
    return found


def staging_alerts(home: Path) -> list[dict[str, str]]:
    """Staged Cowork workspaces that lag the engine running on this host.

    This is the channel that did not exist on 2026-08-20, when both vaults'
    workspaces sat at 0.20.19 for two releases: `brain doctor` reported the
    staleness, but only to someone who ran doctor, and `update_alerts` above
    reads a marker that only `brain update` writes — so NOT running the one
    command that fixes the drift was exactly the case that produced silence.
    A finding needs a channel to the session; this is that channel, and it is
    pure file reads like everything else here."""
    # Deferred import: `alerts` imports this module at load time, so a
    # module-level import here would be circular. It also re-runs on every
    # call rather than caching a name once, which is what keeps
    # `monkeypatch.setattr(alerts, "_running_version", ...)` in the test
    # suite effective for this call — a name bound at import time would not
    # see a patch applied afterwards, but this lookup is always fresh.
    from .alerts import _alert, _read_json, _running_version

    running = _running_version()
    if running is None:
        return []
    registry = _read_json(home / ".brainiac" / "workspaces.json")
    if not isinstance(registry, dict):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in registry.get("entries", []):
        if not isinstance(entry, dict) or entry.get("target") != "cowork-vm":
            continue
        raw = entry.get("vault_path")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        stale = sorted(_staged_versions(Path(raw)) - {running})
        if stale:
            out.append(_alert(
                "staging:stale",
                f"Cowork workspace staged at {', '.join(stale)} while this host "
                f"runs {running} — run 'brain update' on the host",
                scope=raw))
    return out


def host_vaults(home: Path) -> list[Path]:
    """Every host-target vault in the workspace registry, deduped, in order."""
    from .alerts import _read_json

    registry = _read_json(home / ".brainiac" / "workspaces.json")
    if not isinstance(registry, dict):
        return []
    seen: list[Path] = []
    for entry in registry.get("entries", []):
        if not isinstance(entry, dict) or entry.get("target") != "host":
            continue
        raw = entry.get("vault_path")
        if not raw:
            continue
        path = Path(raw)
        if path not in seen:
            seen.append(path)
    return seen
