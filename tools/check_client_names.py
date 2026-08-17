#!/usr/bin/env python3
"""Client-name gate on STAGED files — the release gate, moved earlier.

Why this exists (2026-08-17): a real client name reached six shipped
``tools/cos_*`` files between v0.20.13 and v0.20.14 and sat there for a day.
Nothing caught it — ``gitleaks`` finds secrets, not client names, and
``gearbox harvest`` carries no denylist — so the ONLY guard was
``publish_public.py``'s contamination scan, which fires at release time,
after the name is already committed and pushed. This runs the same check at
commit time, where the fix is a one-line edit instead of a re-tag.

**Same semantics as the release scanner, deliberately** (see
``tools/publish_release.py::step_contamination_scan``): whole-word, fixed-string,
case-insensitive matching over the bare denylist terms, with blank lines and
``#`` comments stripped. Two gates that disagree about what a hit is are worse
than one, so if that scanner's rules change, change these with them.

**Degrades to a pass when the denylist is absent, and says so.** The denylist
is external and never committed (it names real clients), so CI and every other
machine legitimately lack it. A missing denylist is NOT a failure — but it is
announced, because a silent skip is how a gate stops existing.

Exit 0 clean or skipped, 1 on a hit. Prints the file and line but NEVER the
matched term (same redaction posture as the release scan).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

DENYLIST_ENV = "BRAINIAC_DENYLIST"
DEFAULT_DENYLIST = Path.home() / "brainiac-release-groundtruth.txt"
#: Only this gate's own machinery is skipped by name.
SKIP_FILES = ("tools/check_client_names.py",)


def export_excluded_prefixes() -> tuple[str, ...]:
    """The paths the clean-room export DROPS, read from the export itself.

    This gate guards what SHIPS, so its scope must be the export's scope. Two
    lists would drift: on its first run this gate flagged `tests/` — synthetic
    fixtures deliberately written to imitate corpus prose, which the export
    has excluded as corpus-derived since 2026-07-12 and which therefore cannot
    leak. A gate stricter than the thing it mirrors fires on ordinary work,
    and a gate people learn to skip is not a gate.

    Falls back to a conservative subset if the export module cannot be
    imported — never to an empty tuple, which would scan everything and
    reintroduce exactly that noise.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from export_cleanroom import EXCLUDE_PREFIXES  # type: ignore

        return tuple(EXCLUDE_PREFIXES)
    except Exception:  # noqa: BLE001 — a gate must not die on an import
        return ("_archive/", "_plans/", "_evidence/", "_workspace/",
                "_decisions/", "tests/")


def denylist_path() -> Path:
    return Path(os.environ.get(DENYLIST_ENV) or DEFAULT_DENYLIST).expanduser()


def bare_terms(path: Path) -> list[str]:
    """Strip comments/blanks — an empty pattern makes the whole scan vacuous
    (the 2026-07-12 silent false pass that shipped a real term to PyPI)."""
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def staged_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         capture_output=True, text=True, check=False)
    return [f for f in out.stdout.splitlines() if f.strip()]


def scan(files: list[str], terms: list[str]) -> list[tuple[str, int]]:
    """(path, line_no) per hit. Whole-word, case-insensitive — matching the
    release scanner rather than a looser substring pass, which fires on
    ordinary English words that merely contain a term."""
    if not terms:
        return []
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b",
                         re.IGNORECASE)
    hits: list[tuple[str, int]] = []
    skip = export_excluded_prefixes()
    for f in files:
        if f.startswith(skip) or f in SKIP_FILES:
            continue
        p = Path(f)
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: the release gate skips these too
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append((f, n))
    return hits


def main() -> int:
    dl = denylist_path()
    if not dl.is_file():
        print(f"client-name gate: SKIPPED — no denylist at {dl} "
              f"(set ${DENYLIST_ENV} to point at one). The release gate still "
              f"scans the full export.")
        return 0
    terms = bare_terms(dl)
    if not terms:
        print(f"client-name gate: FAILED — {dl} has no usable terms after "
              "stripping comments/blanks; an empty pattern passes everything.")
        return 1
    hits = scan(staged_files(), terms)
    if not hits:
        return 0
    print(f"\n=== Client-name gate: {len(hits)} hit(s) in staged files ===")
    for f, n in hits:
        print(f"  {f}:{n}")
    print("\nA denylisted term appears in a file you are committing. The term "
          "itself is not printed. Reword the line, or if the term is ordinary "
          "English rather than a client name, take it out of the denylist "
          "deliberately.\n"
          "Bypass for one commit only, with the reason in the body: "
          "SKIP=client-names git commit ...\n")
    return 1


def _demo() -> None:
    """ponytail self-check — probed with a KNOWN POSITIVE and a known negative,
    because a scanner that reports clean on broken input is worse than none."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        hit = td / "a.py"
        hit.write_text("x = 'AcmeCorp deal'\nok = 1\n", encoding="utf-8")
        miss = td / "b.py"
        miss.write_text("nothing sensitive\n", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(td)
        try:
            assert scan(["a.py"], ["acmecorp"]) == [("a.py", 1)], "known positive missed"
            assert scan(["b.py"], ["acmecorp"]) == [], "false positive"
            # whole-word: a term inside a longer word is NOT a hit
            hit.write_text("acmecorporation = 1\n", encoding="utf-8")
            assert scan(["a.py"], ["acmecorp"]) == [], "substring fired as whole word"
            # an empty term list must never silently pass everything
            assert scan(["a.py"], []) == []
        finally:
            os.chdir(cwd)
    print("OK: client-name gate self-check passed (known positive + negative)")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
        raise SystemExit(0)
    raise SystemExit(main())
