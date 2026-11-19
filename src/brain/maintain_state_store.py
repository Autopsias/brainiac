"""F104 — read and write ``maintain-state`` where the engine may trust it.

``<vault>/.brain/maintain-state.json`` is on the mount a Cowork VM can write.
The engine reads it to decide which date-gated branches are due
(``maintenance_escalation.maintain_branches``) and to raise every liveness and
regression alarm for them (``branch_escalation``, ``invariants.liveness_finding``,
``invariants.state_regressions``). A VM that wrote ``last_run: today`` there
silenced both at once, including the integrity fold that would catch its own
tampering.

So the AUTHORITATIVE copy is host-private (``config.maintain_state_private_path``)
and the mount file is a display projection the engine writes and never reads
back — the rule ``remediation_state`` already follows for the remediation rows.

Three properties, each deliberate:

* **Unreadable is not healthy.** An unreadable or non-dict private copy, or a
  store location that cannot be proven off the mount, reads as ``{}``: every
  branch never-run, so nothing is suppressed and no liveness alarm is hidden.
* **One-time carry-forward.** A host upgraded from before this change has only
  the mount file. While NO host-private copy exists, that file is read as the
  starting state (``last_run`` markers, ratcheted floors, counters) and the next
  ``save`` makes the private copy authoritative. The window closes for good after
  that save; a forged row in it can suppress a branch for at most one cadence,
  because the next real run rewrites it. Nothing a VM does can bring it back —
  the private path is derived from the host's own path and configuration.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

from .atomic_temp import write_text_atomic


def load(vault: str | os.PathLike[str] | None) -> dict[str, Any]:
    """The verified maintain state, or ``{}`` when it cannot be read or trusted."""
    from . import config

    try:
        path = config.maintain_state_private_path(vault)
    except config.HostPathUnsafe:
        return {}
    if not path.exists():
        path = config.maintain_state_path(vault)  # one-time carry-forward, see module doc
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save(vault: str | os.PathLike[str] | None, state: dict[str, Any]) -> None:
    """Atomic write of the authoritative copy, then of the mount projection.

    The private copy goes first, so a crash between the two leaves the
    authoritative side at least as new as the display side. A failed private
    write raises, like a failed mount write always did — a run that cannot
    record its own progress must not look like a clean one."""
    from . import config

    text = json.dumps(state, indent=2, sort_keys=True)
    private = config.maintain_state_private_path(vault)
    private.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(private.parent, 0o700)
    write_text_atomic(private, text)
    mount = config.maintain_state_path(vault)
    mount.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(mount, text)


# --- freshness record for the host-signed mount files -----------------------
# A signed `current.json` / `exceptions.json` sits on the mount a VM can write,
# and the signature covers only the file's own fields, so a VM can copy an older
# signed file back over the new one. The host therefore remembers, off the mount,
# the exact `generated_at` of the last copy IT wrote and refuses anything older.

def parse_stamp(value: Any) -> datetime.datetime | None:
    """An ISO timestamp as an aware UTC datetime (a naive or date-only value
    reads as UTC), or ``None`` when it is missing or unparseable."""
    try:
        at = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=datetime.timezone.utc)


def _freshness_path(vault: str | os.PathLike[str] | None) -> Path:
    from . import config

    p = config.maintain_state_private_path(vault)
    return p.with_name(p.name.replace("maintain-state-", "signed-freshness-", 1))


def _freshness_record(vault: str | os.PathLike[str] | None) -> dict[str, Any]:
    from . import config

    try:
        data = json.loads(_freshness_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError, config.HostPathUnsafe):
        return {}
    return data if isinstance(data, dict) else {}


def last_signed_at(vault: str | os.PathLike[str] | None, kind: str) -> datetime.datetime | None:
    """The newest ``generated_at`` this host recorded for ``kind``, or ``None``
    when none exists yet (first run after an upgrade) or it cannot be read."""
    return parse_stamp(_freshness_record(vault).get(kind))


def record_signed_at(vault: str | os.PathLike[str] | None, kind: str, stamp: str) -> None:
    """Remember ``stamp`` as the newest ``generated_at`` written for ``kind``.
    Only ever moves forward. Raises like :func:`save`; callers stay best-effort."""
    from . import config

    new = parse_stamp(stamp)
    old = last_signed_at(vault, kind)
    if new is None or (old is not None and new <= old):
        return
    path = _freshness_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(path.parent, 0o700)
    write_text_atomic(path, json.dumps({**_freshness_record(vault), kind: stamp}, sort_keys=True))
