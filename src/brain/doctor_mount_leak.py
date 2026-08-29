"""The recurring owner of "the vault is not on the mount" (criterion 7).

``brain doctor`` runs this on every registered Cowork workspace, so the property
survives a later reinstall, restage, crash or manual copy rather than being true
only on cutover day. ``/brainiac-health`` already runs ``brain doctor``, so this
needs no second scheduler.

The verdict is ``STALE``, which is in ``doctor._GATING_STATUSES`` -- it gates the
process exit code. That is the "FAILS" in criterion 7; a row that only printed a
warning would be a check that cannot fail.
"""
from __future__ import annotations

import os
import time

from pathlib import Path
from typing import Any

from .cowork_leak_scan import ScanBudgetExceeded, recoverable_artifacts

__all__ = ["check_cowork_mount_leak", "SURFACE"]

SURFACE = "Cowork workspace note-body leak (VULN-3385)"

# `run_doctor` is called on EVERY `maintain` through the health report, and the
# scanner is uncapped and unpruned by design (that is what closed the five
# measured bypasses). Unbudgeted on the live reference mount it blew a 300 s
# pytest timeout and took six unrelated tests with it, 2026-08-29. Overridable
# so an operator running the check deliberately can scan without a clock.
_SCAN_BUDGET_SECONDS = float(os.environ.get("BRAIN_MOUNT_SCAN_BUDGET", "20"))

# KNOWN LIMIT, stated rather than left to be discovered: the budget is checked
# between DIRECTORIES of the outer walk. `_has_text`, `_zone_yields_bodies` and
# `_scan_deliverables_shelf` each run their own sub-walk with no deadline, so a
# single enormous note zone or shelf can carry one row past the budget. That
# makes a SLOW row, not a hang --- the outer walk still stops at the next
# directory boundary. Raised by llm-review-high 2026-08-29 and NOT measured on a
# real mount, so it is recorded as an unquantified limit, not a fixed defect.

# Only a scan that RAN OUT of budget is remembered, and only for this long. The
# reason is narrow: `run_doctor` is called once per `maintain`, so a test (or a
# nightly) that folds several times pays the full budget on each one --- 8 folds
# x 2 workspaces x 20 s is over the 300 s test timeout, which is how
# `test_kl_orphans_sustained_growth_logs_exactly_one_hot_line` was still failing
# after the budget alone went in (measured 2026-08-29).
#
# A scan that COMPLETED is never cached. Completing means it was cheap, so
# re-running costs nothing --- and it means the result is exact, which is the
# one we must not serve stale. This way the cache can only ever shorten a
# repeated EXPENSIVE scan, never mask a change on a folder we can scan quickly.
_SCAN_TTL_SECONDS = 300.0
_scan_cache: dict[str, tuple[float, ScanBudgetExceeded]] = {}


def _scan_once(ws: str) -> list:
    """`recoverable_artifacts` with the budget, memoising ONLY the timeout."""
    hit = _scan_cache.get(ws)
    if hit is not None and (time.monotonic() - hit[0]) < _SCAN_TTL_SECONDS:
        raise hit[1]
    try:
        return recoverable_artifacts(ws, budget_seconds=_SCAN_BUDGET_SECONDS)
    except ScanBudgetExceeded as exc:
        _scan_cache[ws] = (time.monotonic(), exc)
        raise


def _workspaces(registry_entries: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    """``(workspace_path, cut_over)`` per attached FOLDER, in registry order.

    ``workspace_path`` is the folder a Cowork sandbox attaches; ``vault_path``
    is one level inside it before the cutover. Scanning ``vault_path`` would
    miss a second copy of the corpus dropped anywhere else under the attached
    folder, which is exactly the "a manual copy puts it back" case this check
    exists for -- so the SCAN is always the whole folder.

    ``cut_over`` is true when EVERY vault registered against that folder lives
    outside it. It selects the row's status, not what is scanned; see
    :func:`check_cowork_mount_leak`.

    Host-target entries are included deliberately: the registry records the
    same ``workspace_path`` for both targets, and an attached folder is
    attachable whatever an entry's ``target`` says.
    """
    from .cowork_staging import vault_is_relocated

    order: list[str] = []
    relocated: dict[str, bool] = {}
    known: dict[str, bool] = {}
    for entry in registry_entries or []:
        raw = entry.get("workspace_path") or ""
        if not raw:
            continue
        # Canonicalise: two entries naming the same folder through a symlink,
        # a trailing slash or a relative path are ONE attached folder, and
        # grouping them by raw string split them into two rows with different
        # verdicts.
        try:
            ws = str(Path(raw).resolve())
        except OSError:
            ws = raw
        if ws not in relocated:
            order.append(ws)
            relocated[ws] = True
            known[ws] = False
        vault = entry.get("vault_path") or ""
        if not vault:
            continue  # says nothing either way; `known` stays False
        known[ws] = True
        if not vault_is_relocated(vault, ws):
            relocated[ws] = False
    # FAILS CLOSED on an entry that never named a vault: "no vault_path" is the
    # ABSENCE of evidence, not evidence of a completed cutover. Reading it as
    # cut over made the row STALE (gating) on a workspace that had not moved
    # anything --- a gate firing on a malformed registry rather than on a leak.
    return [(ws, relocated[ws] and known[ws]) for ws in order]


def check_cowork_mount_leak(registry_entries: list[dict[str, Any]]) -> list[dict]:
    """One row per registered Cowork workspace folder.

    ``current`` == that folder yields no note body by path, by glob or by
    content search.

    A folder that DOES yield one reports at one of two statuses, and the split
    is deliberate:

    * **cut over** (every registered vault lives outside the folder) -> ``stale``,
      which is in ``doctor._GATING_STATUSES`` and therefore FAILS the run. This
      is the "keep it off" gate: once the vault has moved, a restage, a
      reinstall, a crash or a manual copy that puts a body back is a failure.
    * **not cut over yet** (a vault still sits inside the folder) -> ``manual-required``,
      reported in full with the same remediation but NOT gating. The finding
      there is VULN-3385 itself, which is open, known, and has a plan; making
      it gate would red every ``brain doctor`` and every ``brain update`` (which
      exits 1 on any gating row) on this machine until the cutover lands, for a
      condition no operator can clear except by doing the cutover. A gate that
      blocks the ordinary path for a known-open finding is a gate people learn
      to bypass.

    STATED so the split is auditable rather than discovered later: until a
    vault is relocated, this row cannot fail a run.
    """
    from .doctor import CURRENT, MANUAL_REQUIRED, NOT_DETECTABLE, STALE, _row

    rows: list[dict] = []
    for ws, cut_over in _workspaces(registry_entries):
        surface = f"{SURFACE} — {ws}"
        if not Path(ws).is_dir():
            rows.append(_row(surface, NOT_DETECTABLE,
                             "registered workspace folder does not exist on this host"))
            continue
        partial = False
        try:
            leaks = _scan_once(ws)
        except ScanBudgetExceeded as exc:
            # A partial scan that already found something is still a true
            # finding; only a partial scan that found NOTHING is uninformative,
            # and that case must NOT read as CURRENT. Reporting the timeout as
            # an all-clear is the "clean because the input was empty" bug this
            # whole module exists to prevent.
            if not exc.found:
                rows.append(_row(
                    surface, NOT_DETECTABLE,
                    f"scan did not finish within {_SCAN_BUDGET_SECONDS:.0f}s "
                    "— this is NOT an all-clear; re-run `brain doctor` when the "
                    "disk is quiet, or scan this folder directly",
                    raw={"workspace_path": ws, "cut_over": cut_over,
                         "scan_complete": False, "artifacts": []}))
                continue
            leaks = exc.found
            partial = True
        if not leaks:
            rows.append(_row(surface, CURRENT,
                             "no vault tree, snapshot, derived index, escaping symlink "
                             "or staged original inside the attached folder",
                             raw={"workspace_path": ws, "cut_over": cut_over,
                                  "scan_complete": True, "artifacts": []}))
            continue
        by_kind: dict[str, int] = {}
        for a in leaks:
            by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(by_kind.items()))
        atleast = "at least " if partial else ""
        detail = (f"{atleast}{len(leaks)} artefact(s) inside the attached folder yield note "
                  f"bodies ({summary}): " + "; ".join(str(a.path) for a in leaks[:4])
                  + (" …" if len(leaks) > 4 else ""))
        if not cut_over:
            detail += (" — the vault still lives inside this folder, so this is "
                       "VULN-3385 itself, not a regression of the fix")
        rows.append(_row(
            surface, STALE if cut_over else MANUAL_REQUIRED, detail,
            remediation=("a Cowork sandbox reads this folder with ordinary file tools. "
                         "Move the vault off the mount, publish the snapshot to a "
                         "host-only $BRAIN_SNAPSHOT_DIR and keep the derived index under "
                         "$BRAIN_INDEX_DIR — see docs/operations/"
                         "closed-stacks-s07-cutover-and-reversal.md"),
            raw={"workspace_path": ws, "cut_over": cut_over,
                 "scan_complete": not partial,
                 "artifacts": [{"kind": a.kind, "path": str(a.path), "detail": a.detail}
                               for a in leaks]}))
    return rows
