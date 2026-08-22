"""The owner's STANDING answer to every COS ingestion batch.

Owner ruling 2026-08-21: *"I will always approve ALL content including MNPI
going into Brainiac."* The per-batch owner question therefore asks a question
whose answer never varies, and a gate with no real decision behind it is not a
control — it is a queue that strands content (25 candidates sat unclaimable
for twelve days behind one).

What this does NOT do is remove the gate. The batch is still built, still
signed, still enqueued, and still consumed through ``consume_answers`` with
its per-candidate content CAS intact. Only the keystroke is standing: the
answer is recorded here, stamped with the ruling, and replayed into the
question the moment it is asked. Clearing this file restores the manual gate
with no other change.

HOST-PRIVATE, and that placement is load-bearing. This record decides whether
content is signed into the vault without a human looking. Under
``<vault>/.brain/`` or ``<vault>/overlay/`` it would sit on the Cowork
VirtioFS mount, where the untrusted VM leg could write one — the same mistake
INT-01 (approved queue), INT-02 (drift dispositions), INT-04 (attachment
anchors) and INT-05 (writer lock) each had to be moved off the mount to fix.
It uses their one shared resolver, so it cannot be subtly weaker than they
are.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ..config_hostpaths import HostPathUnsafe
from ._approval import _host_private_base, _proven_off_mount, approved_vault_identity
from ._io import _write_atomic
from ._layout import _ts

STANDING_APPROVAL_SCHEMA = "cos_standing_approval/v1"
_STANDING_DIRNAME = "cos-standing-approval"
STANDING_ANSWER = "accept all"


def standing_approval_path(vault=None) -> Path:
    """Where this vault's standing answer lives, PROVEN off every VM root."""
    return _proven_off_mount(
        _host_private_base() / _STANDING_DIRNAME / f"{approved_vault_identity(vault)}.json",
        vault, what="standing approval")


def standing_approval(vault=None) -> dict[str, Any] | None:
    """The recorded standing answer, or ``None`` when the manual gate applies.

    Fails CLOSED on anything it cannot read as a well-formed v1 record with
    the one answer this file is allowed to carry: a corrupt or half-written
    record must leave the owner question standing, never auto-answer it.
    """
    try:
        rec = json.loads(standing_approval_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError, HostPathUnsafe):
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("schema") != STANDING_APPROVAL_SCHEMA:
        return None
    if rec.get("answer") != STANDING_ANSWER:
        return None
    return rec


def set_standing_approval(vault, *, reason: str, now: _dt.datetime | None = None
                          ) -> dict[str, Any]:
    """Record the owner's standing accept-all. HOST-broker only."""
    path = standing_approval_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(path.parent, 0o700)
    rec = {"schema": STANDING_APPROVAL_SCHEMA, "answer": STANDING_ANSWER,
           "reason": str(reason)[:500],
           "recorded": _ts(now or _dt.datetime.now(_dt.timezone.utc))}
    _write_atomic(path, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"),
                  mode=MODE_HOST_PRIVATE)
    return rec


def clear_standing_approval(vault) -> bool:
    """Restore the manual per-batch gate. Returns whether one was removed."""
    try:
        path = standing_approval_path(vault)
    except HostPathUnsafe:
        return False
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


__all__ = ["STANDING_APPROVAL_SCHEMA", "STANDING_ANSWER", "standing_approval_path",
           "standing_approval", "set_standing_approval", "clear_standing_approval"]
