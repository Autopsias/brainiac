#!/usr/bin/env python3
"""brain_daily — create (or open) today's daily note in a Brainiac vault.

Second-brain parity with an old daily-note habit, done the
native Brainiac way: renders the `templates/daily.md` sections, optionally seeds
the Session Summary from `brain brief`, and captures a signed+indexed
`type: daily` note via `brain capture`.

Idempotent: if today's note already exists it prints the path and exits 0 (never
a second copy). Default classification is **Confidential** — a personal daily log
tends to carry deal detail, and this matches the Daily-zone floor the migration
used; pass --classification to override.

Usage:
  python3 tools/brain_daily.py --vault /path/to/vault [--date YYYY-MM-DD]
                               [--brief] [--classification TIER]

Schedule it (host) to reproduce the old auto-morning-note behaviour, e.g. a
launchd/cron entry or a fold in the existing brain-nightly routine:
  brain_daily.py --vault "$VAULT" --brief
"""
import argparse, datetime, pathlib, re, shutil, subprocess, sys

TEMPLATE_SECTIONS = ["## Session Summary", "## Work Done", "## Open Threads", "## Next Session"]


def brain_bin() -> str:
    return shutil.which("brain") or str(pathlib.Path.home() / ".local/bin/brain")


def note_exists(vault: str, note_id: str) -> bool:
    """True if the note is already in the corpus (get succeeds) or on disk."""
    try:
        r = subprocess.run([brain_bin(), "--vault", vault, "get", note_id],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and note_id in (r.stdout or ""):
            return True
    except Exception:
        pass
    # disk fallback (draft in capture-inbox or signed under brain/)
    for p in pathlib.Path(vault).rglob(f"{note_id}.md"):
        return True
    return False


def seed_from_brief(vault: str) -> str:
    """Best-effort: a few bullet lines from `brain brief` to prime the summary."""
    try:
        r = subprocess.run([brain_bin(), "--vault", vault, "brief", "-n", "5", "--no-drain"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return ""
        lines = [ln.rstrip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        # keep it short — first ~8 non-noise lines, indented as bullets
        picked = [ln for ln in lines if not ln.startswith(("Fetching", "==", "--"))][:8]
        return "\n".join(f"- {ln.lstrip('- ').strip()}" for ln in picked)
    except Exception:
        return ""


def build_body(date: datetime.date, brief: str) -> str:
    weekday = date.strftime("%A")
    out = [f"# {date.isoformat()} ({weekday})", ""]
    for sec in TEMPLATE_SECTIONS:
        out.append(sec)
        if sec == "## Session Summary" and brief:
            out.append(brief)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Create today's Brainiac daily note.")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--brief", action="store_true", help="seed Session Summary from `brain brief`")
    ap.add_argument("--classification", default="Confidential",
                    choices=["Public", "Internal", "Confidential", "Restricted", "MNPI"])
    a = ap.parse_args()

    if a.date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
            print(f"ERROR: --date must be YYYY-MM-DD, got {a.date!r}", file=sys.stderr)
            return 2
        date = datetime.date.fromisoformat(a.date)
    else:
        date = datetime.date.today()

    note_id = f"daily-{date.isoformat()}"
    vault = a.vault

    if note_exists(vault, note_id):
        print(f"daily note already exists: {note_id} (nothing to do)")
        return 0

    body = build_body(date, seed_from_brief(vault) if a.brief else "")
    r = subprocess.run(
        [brain_bin(), "--vault", vault, "capture", "--type", "daily",
         "--id", note_id, "--classification", a.classification],
        input=body, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        return r.returncode
    print(f"OK: daily note {note_id} captured ({a.classification}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
