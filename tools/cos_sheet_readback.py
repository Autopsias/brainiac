#!/usr/bin/env python3
"""FB-02 — read the morning sheet back, unattended, on its own schedule.

s07 publishes a sheet every night; s02 changed HOW the owner answers it — the
page saves ``cos-marks-<date>.json`` into his real ``~/Downloads`` (never
``~/.brain/cos-downloads``, which is the unrelated attachment-fetch staging
directory, and reusing that name for a second meaning is exactly the class of
bug ``_default_downloads_dir``'s own docstring records). Until this script,
nothing scheduled ever read one back: every mark the record has ever learned
from arrived through an attended ``brain cos-feedback`` invocation.

WHAT THIS DOES, once a run:

  1. glob ``cos-marks-*.json`` in the owner's Downloads folder;
  2. for each one, file it through the SAME audited path the shipped command
     uses — ``brain cos-feedback --from-marks`` (``feedback_marks.record_marks``,
     which already runs ``sheet_marks.validate_marks`` and refuses an
     already-consumed ``sheet_id`` before writing anything) — never a
     hand-written row;
  3. move the file OUT of Downloads: into the host-private "consumed"
     directory beside the record on success, or "quarantine" beside it on
     refusal — never left behind to be re-read tomorrow, never deleted;
  4. append ONE report row to a durable, host-private ledger, so a morning
     with no file, a morning with a file that filed zero marks, and a morning
     with a file that was refused all leave DIFFERENT rows on disk instead of
     the same silence (the rule s04 applied to the ingest bridge's zero: see
     ``tools/cos_bridge_skip.py``).

WHY ``sheet_marks.validate_marks`` AND NOT ``feedback_sheet.validate_sheet_state``
(a deliberate deviation from the session brief's literal wording). The two
validators check two different things: ``validate_sheet_state`` is the shape
of the SHEET's own embedded state (``threads``, ``held_out``, ``door_check``,
...) that the ``--from-sheet`` Artifact transport reads; the file this script
finds in Downloads is the narrower ``cos-marks/1`` payload
(``schema``/``sheet_id``/``marks``/``revoked``/...) that ``marks_from_state``
projects OUT of that state and that ``--from-marks`` reads. Calling
``validate_sheet_state`` on a marks file would refuse every valid one — its
required keys are not even present. This script never calls either validator
directly: it lets ``brain cos-feedback --from-marks`` do so internally, which
is also the only way "validated" and "what got written" can never drift apart.

IDENTITY IS THE SHEET's OWN STAMP, NEVER THE FILENAME (unchanged from the
shipped command — ``sheet_select.already_consumed`` is what actually refuses a
second save of the same download). This script still refuses to GUESS a date
from a filename it cannot parse: only ``cos-marks-<date>.json`` or the
browser's own second-save suffix ``cos-marks-<date> (n).json`` is accepted as
a candidate at all; anything else found in Downloads next to it is quarantined
unopened, named, and left for a human to look at rather than skipped.

    python3 tools/cos_sheet_readback.py --vault <vault> --json
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

if __name__ == "__main__":  # tools/ bootstrap, same as every cos_* tool
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brain import cli, config, cos                                # noqa: E402
from brain.cos.feedback import feedback_dir                       # noqa: E402

READBACK_SCHEMA = "cos-morning-readback/1"

STATE_NO_FILE = "no-marks-file"
STATE_PROCESSED = "processed"
#: The directory is there and we cannot read it. NOT the same as empty: on
#: macOS the privacy system denies a launchd agent `~/Downloads` until the
#: owner grants Full Disk Access, and `Path.glob` SWALLOWS that permission
#: error and returns `[]` (probed at mode 000: `is_dir()` True, `glob()` []).
#: Reported as `no-marks-file` it would make the most likely real failure of
#: this job identical to a quiet morning — the exact silence this module
#: exists to prevent.
STATE_UNREADABLE = "downloads-unreadable"

OUTCOME_FILED = "filed"
OUTCOME_QUARANTINED = "quarantined"

#: Exactly what the sheet's Save button writes (`sheet_render.py`), including
#: the browser's own "second save of this download" suffix. Anything else in
#: Downloads that happens to start with ``cos-marks-`` is refused rather than
#: guessed at — see the module docstring.
_FILENAME_RE = re.compile(r"^cos-marks-(\d{4}-\d{2}-\d{2})(?: \(\d+\))?\.json$")


def _default_downloads_dir() -> Path:
    """The owner's REAL Downloads folder — deliberately not a lane, not
    configurable by an env var that could collide with ``BRAIN_COS_DOWNLOADS_DIR``
    (the unrelated attachment-fetch staging directory)."""
    return Path.home() / "Downloads"


def _readback_runs_path(vault: Any) -> Path:
    return feedback_dir(vault) / "readback-runs.jsonl"


def _quarantine_dir(vault: Any) -> Path:
    return feedback_dir(vault) / "downloads-quarantine"


def _consumed_dir(vault: Any) -> Path:
    return feedback_dir(vault) / "consumed-downloads"


def _ensure_private_dir(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(d, 0o700)
    return d


def _move_aside(vault: Any, path: Path, dest_dir_fn) -> str:
    dest_dir = _ensure_private_dir(dest_dir_fn(vault))
    dest = cos._unique_dest(dest_dir, path.name)
    cos._move_dirent(path, dest)
    return str(dest)


def _file_via_cli(vault: Path, path: Path) -> tuple[int, dict[str, Any]]:
    """Invoke ``brain cos-feedback --from-marks`` IN-PROCESS.

    This is the shipped command, not a reimplementation of what it does —
    the same entry point ``test_cos_marks_file.py`` drives. Capturing stdout
    (rather than a plain ``cli.main`` call) is the only reason: the report
    needs the JSON result — ``transport``, ``sheet_id``, marks filed — not
    just the exit code.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["--vault", str(vault), "cos-feedback",
                       "--from-marks", str(path), "--json"])
    try:
        result = json.loads(buf.getvalue())
    except ValueError:
        result = {"detail": buf.getvalue().strip()[:2000] or "(no output)"}
    return rc, result


def _process_one(vault: Path, path: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "filename": path.name, "outcome": "", "reason": "", "sha256": "",
        "transport": "", "sheet_id": "", "marks_filed": None, "moved_to": "",
    }
    if _FILENAME_RE.match(path.name) is None:
        entry["outcome"] = OUTCOME_QUARANTINED
        entry["reason"] = (
            "filename does not match cos-marks-<date>.json (or the "
            "browser's ' (n)' second-save suffix) — refused rather than "
            "guessing its date")
        entry["moved_to"] = _move_aside(vault, path, _quarantine_dir)
        return entry
    try:
        raw = cos._read_nofollow(path)
    except Exception as exc:                                     # noqa: BLE001
        entry["outcome"] = OUTCOME_QUARANTINED
        entry["reason"] = f"unreadable: {type(exc).__name__}: {exc}"
        entry["moved_to"] = _move_aside(vault, path, _quarantine_dir)
        return entry
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    rc, result = _file_via_cli(vault, path)
    if rc == 0:
        consumed = result.get("consumed") or {}
        entry["outcome"] = OUTCOME_FILED
        entry["transport"] = str(result.get("transport") or "")
        entry["sheet_id"] = str(result.get("sheet_id") or "")
        entry["marks_filed"] = int(consumed.get("marks_filed", 0))
        entry["moved_to"] = _move_aside(vault, path, _consumed_dir)
    else:
        entry["outcome"] = OUTCOME_QUARANTINED
        entry["reason"] = str(result.get("detail") or result)
        entry["moved_to"] = _move_aside(vault, path, _quarantine_dir)
    return entry


def readback(vault: Any, downloads_dir: Path) -> dict[str, Any]:
    """Run one morning's read-back. Pure aside from the filesystem it reads
    and the two audited paths it writes through (the record, and this report).

    A DIRECTORY THAT DOES NOT EXIST IS EMPTY, not an error: a fresh host that
    has never had Finder create ``~/Downloads`` yet has no marks file either.
    A directory that exists and REFUSES to be listed is a THIRD state
    (``STATE_UNREADABLE``) — see that constant for why it cannot be folded
    into either of the other two.
    """
    downloads_dir = Path(downloads_dir)
    unreadable = ""
    try:
        names = sorted(os.listdir(downloads_dir))
    except FileNotFoundError:
        names = []
    except NotADirectoryError:
        names = []
        unreadable = f"{downloads_dir} is not a directory"
    except OSError as exc:
        names = []
        unreadable = f"{type(exc).__name__}: {exc}"
    candidates = [downloads_dir / n for n in names
                  if n.startswith("cos-marks-") and n.endswith(".json")]
    files = [] if unreadable else [_process_one(vault, p) for p in candidates]
    report = {
        "schema": READBACK_SCHEMA,
        "ts": cos.timestamp(cos.utcnow()),
        "downloads_dir": str(downloads_dir),
        "state": (STATE_UNREADABLE if unreadable
                  else STATE_PROCESSED if files else STATE_NO_FILE),
        "unreadable": unreadable,
        "files_found": len(files),
        "files_filed": sum(1 for f in files if f["outcome"] == OUTCOME_FILED),
        "files_quarantined": sum(1 for f in files
                                 if f["outcome"] == OUTCOME_QUARANTINED),
        "marks_filed_total": sum(f["marks_filed"] or 0 for f in files
                                 if f["outcome"] == OUTCOME_FILED),
        "zero_mark_files": sum(1 for f in files if f["outcome"] == OUTCOME_FILED
                               and f["marks_filed"] == 0),
        "files": files,
    }
    # THE VAULT IS THREADED, and it has to be: `_append_lock_path` proves the
    # lock dir off every VM-visible root, which resolves the vault root, and a
    # caller that passes none makes that resolve `$BRAIN_VAULT` and then the CWD.
    # This job runs from launchd with `/` as its CWD and only `--vault` in argv,
    # so three live mornings (2026-09-07..09) died here at exit 2 — after the
    # marks would have been filed, before the morning was recorded. The two other
    # callers in this lane (`feedback.feedback_path`, `sheet_select.consumed_path`)
    # have passed it since 2026-08-18; this one was missed. NO TEST COULD SEE IT:
    # `conftest`'s session fixture replaces `host_lock_dir` with a stub that
    # ignores the vault, so the whole class is invisible to the suite — which is
    # why the check below asserts the ARGUMENT, not the behaviour.
    cos.append_jsonl(_readback_runs_path(vault), report, vault=vault)
    return report


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vault", default=None,
                   help="vault root (default: $BRAIN_VAULT or ./vault)")
    p.add_argument("--downloads-dir", type=Path, default=None,
                   help="where the browser saves cos-marks-<date>.json "
                        "(default: ~/Downloads)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="print the run report as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        vault = config.vault_root(args.vault)
    except config.VaultNotFoundError as exc:
        print(f"cos_sheet_readback: {exc}", file=sys.stderr)
        return 2
    downloads_dir = args.downloads_dir or _default_downloads_dir()
    try:
        report = readback(vault, downloads_dir)
    except Exception as exc:                                     # noqa: BLE001
        # LOUD, never a silent failure: a scheduled job that dies quietly
        # looks identical to a morning with nothing to read.
        print(f"cos_sheet_readback: unexpected failure: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"cos_sheet_readback: state={report['state']} "
              f"files_found={report['files_found']} "
              f"filed={report['files_filed']} "
              f"quarantined={report['files_quarantined']} "
              f"marks_filed_total={report['marks_filed_total']} "
              f"zero_mark_files={report['zero_mark_files']}")
        if report["state"] == STATE_UNREADABLE:
            print(f"  UNREADABLE {report['downloads_dir']}: "
                  f"{report['unreadable']}", file=sys.stderr)
        for f in report["files"]:
            if f["outcome"] == OUTCOME_QUARANTINED:
                print(f"  QUARANTINED {f['filename']}: {f['reason']} -> "
                      f"{f['moved_to']}", file=sys.stderr)
    # Nonzero on a quarantine so the launchd log (and anything watching its
    # exit code) surfaces it — a refused file is not a crash, but it is not
    # silent either. An unreadable Downloads folder exits 2, like the other
    # failures the job cannot work around on its own.
    if report["state"] == STATE_UNREADABLE:
        return 2
    return 1 if report["files_quarantined"] else 0


if __name__ == "__main__":
    sys.exit(main())
