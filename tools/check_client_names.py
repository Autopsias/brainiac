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
than one, so the shared half is IMPORTED, never restated — this module calls
``publish_release.split_identifier_boundaries`` rather than keeping its own
copy of the rule.

**And it looks inside identifiers** (2026-08-25). A word character includes the
underscore, so a whole-word pass reads ``CLIENTCODE_Rollout_Memo.docx`` as ONE
word and no term inside it can ever match. That is not a corner case: a
filename, a slug, or a generated symbol is exactly the shape a client name
arrives in. The measured cost of the gap was five releases -- a real client
document filename entered in ``e1e2bd9``, passed this hook, passed the release
scan, and shipped publicly in v0.20.28 and v0.20.29 before the release
scanner's split pass caught it on 2026-08-25. Splitting only ADDS boundaries,
so the 2026-08-17 ruling that ordinary English must not fire is untouched:
``tender`` still does not match inside ``tenderness``.

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
#: NOTHING is skipped by name any more (2026-08-25).
#:
#: This file used to skip ITSELF, on the reasoning that a scanner's own demo
#: plants canary terms. The reasoning was wrong in the one way that matters:
#: this file SHIPS -- it is in the clean-room export -- so a term written into
#: it is a term that publishes. The release scan does not skip it, so the two
#: gates disagreed, and the gap was not theoretical: while fixing the v0.20.28
#: leak I wrote a REAL denylisted term into the docstring below as the example,
#: committed it past this hook, and the release scan caught it three commits
#: later. A gate that exempts itself is a gate with a hole exactly its own
#: shape. The canaries here are synthetic words, and that is the rule.
SKIP_FILES: tuple[str, ...] = ()


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


def export_excluded_suffixes() -> tuple[str, ...]:
    """The SUFFIXES the export drops, mirrored for the same reason as the
    prefixes above — and missing until 2026-08-25, which is that docstring's
    own warning coming true.

    `export_cleanroom` grew `EXCLUDE_SUFFIXES` on 2026-07-12, when
    `docs/operations/*-evidence.md` files were found leaking real terms into
    the export. This gate mirrored only the PREFIX half, so it kept scanning
    files the export can no longer ship: 48 hits across six evidence files,
    every one unshippable. A gate that fires on work that cannot leak is the
    gate people learn to pass with SKIP=.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from export_cleanroom import EXCLUDE_SUFFIXES  # type: ignore

        return tuple(EXCLUDE_SUFFIXES)
    except Exception:  # noqa: BLE001 — a gate must not die on an import
        return ("-evidence.md",)


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


def _split_boundaries(line: str) -> str:
    """The release scanner's own rule, imported so the two can never disagree.

    Falls back to the identity ONLY if `tools/` is unreachable — this runs as a
    pre-commit hook under system python, and a hook that dies on an import is a
    hook that gets disabled. A skipped split is announced by `main`, never
    silent.
    """
    try:
        from tools.publish_release import split_identifier_boundaries
    except ImportError:  # pragma: no cover - see _SPLIT_UNAVAILABLE below
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from publish_release import split_identifier_boundaries
        except ImportError:
            globals()["_SPLIT_UNAVAILABLE"] = True
            return line
    return split_identifier_boundaries(line)


#: Set by `_split_boundaries` when the shared rule could not be imported, so a
#: degraded scan is REPORTED rather than passing as a clean one.
_SPLIT_UNAVAILABLE = False


def scan(files: list[str], terms: list[str]) -> list[tuple[str, int]]:
    """(path, line_no) per hit. Whole-word, case-insensitive — matching the
    release scanner rather than a looser substring pass, which fires on
    ordinary English words that merely contain a term.

    Each line is matched TWICE: as written, and with identifier boundaries
    split. The second pass is what sees a term buried in a filename or a
    symbol; see the module docstring for the five releases that cost.

    **The terms are NOT split, and that was measured, not assumed.** Splitting
    them too looks like the symmetric completion of this fix -- it would catch a
    term spanning a camelCase boundary, which the text-only split breaks apart
    and loses. It was written, then run over all 1475 tracked files, and it
    turned one camelCase term into the phrase ``final report``, which fired in
    17 files of ordinary documentation. That is exactly the drowning the owner
    ruled against on 2026-08-17, and a gate nobody can read is a gate nobody
    obeys. The text-only split found the real leak with ONE hit and zero false
    positives on the same corpus.

    So the limit is stated rather than closed: a term whose casing differs from
    how it appears inside an identifier is not found by this pass. The remedy
    is the denylist -- list the form that appears in the text -- not a looser
    scanner."""
    if not terms:
        return []
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b",
                         re.IGNORECASE)
    hits: list[tuple[str, int]] = []
    skip = export_excluded_prefixes()
    skip_suffixes = export_excluded_suffixes()
    for f in files:
        if f.startswith(skip) or f.endswith(skip_suffixes) or f in SKIP_FILES:
            continue
        p = Path(f)
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: the release gate skips these too
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) or pattern.search(_split_boundaries(line)):
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
    if _SPLIT_UNAVAILABLE:
        # Never silent: a scan that ran with only half its passes is a weaker
        # gate wearing a clean gate's exit code.
        print("client-name gate: DEGRADED — the identifier-splitting pass "
              "could not import tools/publish_release.py, so this run saw "
              "only whole words. A term inside a filename or symbol would "
              "not have fired.")
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
            # THE SHAPE THAT SHIPPED (2026-08-25). `_` is a word character, so
            # a whole-word pass reads this whole filename as one word. This is
            # the real incident's shape: a single-word term as one component.
            hit.write_text("# see `CODE1638_Rollout_Memo.docx`\n", encoding="utf-8")
            assert scan(["a.py"], ["rollout"]) == [("a.py", 1)], (
                "a term buried in an identifier was missed — this is the gap "
                "that shipped a client filename in v0.20.28 and v0.20.29")
            # THE STATED LIMIT, pinned so nobody rediscovers it the hard way
            # AND so nobody "fixes" it the way that was already measured and
            # rejected (see `scan`'s docstring: splitting the terms too fired
            # `final report` across 17 documentation files).
            hit.write_text("path = 'CODE_AcmeCorp_Memo.docx'\n", encoding="utf-8")
            assert scan(["a.py"], ["AcmeCorp"]) == [], (
                "if this now HITS, the terms are being split — re-read why "
                "that was reverted before keeping it")
            # ...and camelCase, the other half of the split rule
            hit.write_text("acmeCorpHandler = 1\n", encoding="utf-8")
            assert scan(["a.py"], ["corp"]) == [("a.py", 1)], "camelCase missed"
            # The KNOWN NEGATIVE for the split pass, and it is the whole reason
            # the 2026-08-17 ruling exists: splitting only ADDS boundaries, so
            # ordinary English still must not fire.
            hit.write_text("tenderness and surrender\n", encoding="utf-8")
            assert scan(["a.py"], ["tender"]) == [], (
                "splitting made an ordinary English word fire — this is what "
                "drowned the gate before 2026-08-17")
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
