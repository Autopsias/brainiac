"""Stuck capture-draft alert (UPD-01), split out of ``alerts`` for file size."""

from __future__ import annotations

import re
from pathlib import Path

from . import config as _config

# The hourly nightly drains capture drafts on every firing, so a draft still
# sitting in the inbox after this many hours has been REFUSED by the drain
# (duplicate-id, stale-base, update-needs-owner, ...) and will never promote
# on its own. Until 2026-08-30 nothing reported this: a 19-Aug rule update
# sat refused for 11 days while the session that staged it believed it had
# applied (UPD-01).
STUCK_DRAFT_HOURS = 48


def stuck_draft_alerts(vault: Path, name: str) -> list[dict[str, str]]:
    """Capture drafts the drain keeps refusing (UPD-01). Pure mtime reads."""
    import time

    from .alerts import _alert

    cutoff = time.time() - STUCK_DRAFT_HOURS * 3600
    stuck: list[str] = []
    for d in (_config.capture_inbox_dir(vault), vault / ".brain" / "drafts"):
        try:
            entries = sorted(d.glob("*.md"))
        except OSError:
            continue
        for f in entries:
            try:
                if f.stat().st_mtime < cutoff:
                    # Same posture as _FINDING_KEY_RE: a draft filename is
                    # untrusted-leg-authored and this line is rendered into
                    # the host SessionStart context — never verbatim.
                    stuck.append(re.sub(r"[^A-Za-z0-9._-]", "_", f.name)[:64])
            except OSError:
                continue
    if not stuck:
        return []
    shown = ", ".join(stuck[:3]) + ("…" if len(stuck) > 3 else "")
    return [_alert(
        "stuck-drafts",
        f"{len(stuck)} capture draft(s) stuck >{STUCK_DRAFT_HOURS}h ({shown}) — "
        "run `brain sync` and read the skip reasons; an `update-needs-owner` "
        "refusal applies with `brain write`", name)]
