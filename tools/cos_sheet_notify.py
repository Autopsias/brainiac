#!/usr/bin/env python3
"""Tell the owner his sheet is ready, in the morning, once.

WHY. The marks on the nightly sheet are the only evidence that makes the judge
less conservative — measured on `2026-09-06-run267`, it proposed archiving 2 of
20 `read` threads, which is the whole reason the Inbox is not near zero. A page
nobody opens teaches nothing, and until `today.html` existed there was not even
a stable address to bookmark.

IT NEVER GOES SILENT, and that is deliberate. A morning with no sheet is not
"nothing to say": it means last night's run did not finish, which is exactly
the failure the 2026-09-06 scheduled night hit at 02:21 (`session-died`, zero
threads judged, an empty sheet). So there are two messages and no third state:

  * a sheet dated today  -> "<N> thread(s) ready to mark"
  * anything else        -> names what it found instead, and how old it is

READ-ONLY. It opens no mailbox, signs nothing and writes nothing to the vault.

    python3 tools/cos_sheet_notify.py --vault <vault> [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":                      # tools/ bootstrap, as the siblings
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brain import config                                        # noqa: E402
from brain.cos.feedback import sheets_dir                        # noqa: E402

TITLE = "Brainiac"
#: The sheet marks each thread with one of these blocks; counting them is
#: cheaper and steadier than parsing the page's own state JSON, and a count
#: that drifts by a thread is not worth a parse that can fail.
_THREAD_RE = re.compile(r'class="marks"')


def newest_sheet(vault: Path) -> Path | None:
    """The newest DATED sheet. `today.html` is deliberately not in this
    directory, so nothing here has to filter it out."""
    d = sheets_dir(vault)
    if not d.is_dir():
        return None
    dated = sorted(p for p in d.glob("*.html") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    return dated[-1] if dated else None


def thread_count(sheet: Path) -> int:
    try:
        return len(_THREAD_RE.findall(sheet.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


def message(vault: Path, *, today: str) -> str:
    sheet = newest_sheet(vault)
    if sheet is None:
        return ("No sheet has ever been built. Last night's run did not reach "
                "the sheet step.")
    if sheet.stem == today:
        n = thread_count(sheet)
        if not n:
            return ("Today's sheet was built but carries no threads — last "
                    "night judged nothing.")
        return f"{n} thread(s) ready to mark. Open your Brainiac bookmark."
    age = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(sheet.stem)).days
    return (f"No sheet for today. The newest is {sheet.stem}, {age} day(s) old "
            "— last night's run did not finish.")


def notify(text: str) -> None:
    subprocess.run(
        ["/usr/bin/osascript", "-e",
         f'display notification {_applescript(text)} with title {_applescript(TITLE)}'],
        check=False)


def _applescript(s: str) -> str:
    """AppleScript string literal. Backslash first, then the quote — the other
    order double-escapes every backslash it just introduced."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the message instead of posting it")
    args = ap.parse_args(argv)
    try:
        vault = config.vault_root(args.vault)
    except config.VaultNotFoundError as exc:
        print(f"cos_sheet_notify: {exc}", file=sys.stderr)
        return 2
    text = message(Path(vault), today=_dt.date.today().isoformat())
    print(text)
    if not args.dry_run:
        notify(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
