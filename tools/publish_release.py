#!/usr/bin/env python3
"""S05 (GV-02, ADR-0004 Ruling 7) — the ONE command for the scriptable half of
a release: package/lockstep validation -> clean-room export -> contamination
scan (hard gate) -> local tag. Everything this script does is a dry-run/local
op; nothing here ever pushes or re-enables the disabled remote (Ruling 7 step 7
stays a HUMAN act, always — see docs/release-runbook.md §8).

This does not replace the runbook — it operationalizes runbook §2-§5 (the
part that was previously "copy these six shell commands in order by hand")
into one command with one pass/fail, so a release can't skip a step by
mistake. Runbook §1 (preconditions: full test suite, soak report) and §6-§9
(migration doc, human publish, yank procedure) stay exactly as documented —
they are either not scriptable (a human judgment call) or already covered by
other tooling (pytest).

Usage:
    python3 tools/publish_release.py --check                 # steps only, no tag
    python3 tools/publish_release.py --denylist ~/brainiac-release-groundtruth.txt
    python3 tools/publish_release.py --denylist <path> --tag  # also cuts the local tag

Exit 0 only if every gate passes. Exit 1 on any gate failure — the exact
gate that failed is printed; there is no override flag (ADR-0004 Ruling 7 §4:
"any hit stops the release; there is no override flag").

Timing note: the contamination scan's ``_evidence/`` pass (not the export
pass — that's fast) can take a couple of minutes on a repo with large
benchmark/soak artifacts under ``_evidence/`` — this is the runbook's own
documented §5 second pass, not new overhead. Use ``--check`` for a fast
iteration loop (package validate + export only, no scan/tag) while drafting
a release, then a full ``--denylist`` run once before cutting the tag.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class PublishError(Exception):
    pass


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def step_package_validate() -> str:
    """Runbook §2 — packaging lockstep gate (ADR-0004 Ruling 5, ADR-0005
    Ruling 1/5). Reuses tools/package_clients.py --validate-only verbatim."""
    proc = _run([sys.executable, str(REPO_ROOT / "tools" / "package_clients.py"), "--validate-only"])
    if proc.returncode != 0:
        raise PublishError(f"package_clients.py --validate-only failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "OK"


def step_export(output_dir: Path) -> Path:
    """Runbook §4 — clean-room export (ADR-0001). Regenerates the export tree
    fresh every run; also runs export_cleanroom.py's own internal exported-
    stamp assertion (ADR-0005 Ruling 1)."""
    proc = _run([sys.executable, str(REPO_ROOT / "tools" / "export_cleanroom.py"), "--output", str(output_dir)])
    if proc.returncode != 0:
        raise PublishError(f"export_cleanroom.py failed:\n{proc.stdout}\n{proc.stderr}")
    return output_dir


def step_contamination_scan(export_dir: Path, denylist: Path) -> dict:
    """Runbook §5 — HARD GATE on the export tree (no override); the companion
    ``_evidence/`` pass is informational-only (`_evidence` never ships) and is
    NOT gated — it carries known-benign synthetic-fixture/eval-golden-set
    terms that a hard gate would trip on every release. Redacted counts only,
    never the matched term or line (same posture as the runbook's own scan
    command)."""
    if not denylist.exists():
        raise PublishError(f"denylist not found: {denylist} (external, never committed — see runbook §5)")
    # CRITICAL (fixed 2026-07-12): the raw denylist is an ANNOTATED file — it
    # carries `#` comment lines and blank separators. Passed straight to
    # `grep -F -f`, a single EMPTY pattern line makes grep emit ZERO output for
    # the WHOLE scan (empirically reproduced: bare-term `-f` finds 46 hits, the
    # full annotated denylist finds 0) — a SILENT FALSE PASS that shipped a real
    # term to PyPI before this was caught by hand. Feed grep ONLY the bare
    # terms: strip blank lines and `#`-comments into a temp file first.
    terms = [ln for ln in denylist.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not terms:
        raise PublishError(f"denylist {denylist} has no usable terms after stripping comments/blanks")
    with tempfile.NamedTemporaryFile("w", suffix=".denylist", delete=False,
                                     encoding="utf-8") as tf:
        tf.write("\n".join(terms) + "\n")
        clean_denylist = tf.name
    # -I skips binary files: _evidence/ carries multi-GB benchmark/eval
    # artifacts (indexes, model caches) that a binary-unaware grep chokes on
    # for minutes with zero signal (see docs/release-runbook.md §5). -f takes
    # its own argument (not bundled into -rFoiI) — this repo's `grep` resolves
    # to ugrep on macOS, which parses a bundled "-f<path>" as flag "-f" +
    # filename "I"; splitting -f out is portable across both.
    #
    # Prefer ripgrep when installed (2026-07-22): BSD grep matches line-by-
    # line, and _evidence/ carries multi-MB SINGLE-LINE JSON dumps — 530
    # case-insensitive fixed patterns against a 35 MB line effectively never
    # terminates (observed: a release run wedged >40 min at 100% CPU). rg's
    # matcher is line-length-insensitive and finishes the same sweep in
    # seconds. Same semantics for our purpose: -F fixed strings, -o one hit
    # per match, -i case-insensitive; hidden files included to match grep -r.
    # -w (owner decision 2026-07-22): a denylist term hits only as a whole
    # word — short entries otherwise fire as substrings inside ordinary
    # English words (and quoting an example here would itself trip the gate —
    # this comment stays abstract). Hyphen/slash/dot still bound words, so
    # "term-vault" slug forms keep matching.
    #
    # THE SPLIT PASS (owner decision 2026-08-07, from the Codex cloud review).
    # `-w` alone left two real hiding places open, because `_` is a word
    # character and a case transition is not a word boundary: a term joined by
    # an underscore, and a term inside a camelCase identifier, both scored ZERO
    # while sitting in the export. Measured on a planted synthetic term in six
    # positions: `-w` alone caught 3 of 5 real embeddings, plain substring
    # caught all 5 but also fired on an innocent longer word, and this pass
    # caught all 5 with no false alarm.
    #
    # So every scan runs TWICE: once over the bytes as they are, and once over
    # a copy where `_` and camelCase boundaries are rewritten to spaces.
    #
    # WHAT THE SPLIT PASS COSTS (measured on the first real run, 2026-08-07 —
    # an earlier version of this comment claimed it cost nothing, and that was
    # wrong). It does not reintroduce the ORIGINAL noise: a term inside an
    # ordinary longer word still does not match, because splitting only ADDS
    # word boundaries. But it creates a DIFFERENT false-positive class — an
    # ordinary word sitting in the denylist now matches when it appears as an
    # identifier COMPONENT. The first real run found exactly one export hit: a
    # numpy keyword argument whose two halves split into an ordinary maths word
    # that happened to be a denylist entry. 214 of that denylist's 275 terms are
    # <= 9 characters, so this class is not rare.
    #
    # (This comment deliberately does NOT quote the offending identifier. The
    # same rule as .gitleaksignore: naming the flagged text inside a scanned
    # file makes the file itself a finding — which is precisely what the first
    # draft of this comment did, turning 1 hit into 3.)
    #
    # That is why the two passes are COUNTED SEPARATELY rather than unioned into
    # one number. Both still block the release — a gate with an override is not
    # a gate — but the operator can tell in one line whether they are looking at
    # a term present as written (treat as a leak) or a denylist entry that is
    # really just an English word (fix the denylist).
    #
    # NOTE the rg-vs-grep asymmetry, unchanged and deliberate: the fallback
    # `grep -rFoiI` has no `-w`, so it is a pure substring scan. It is STRICTER
    # than the rg path (more noise, no misses), which is the safe direction for
    # a fallback. The split pass runs on both.
    def _rg_scan(target: Path) -> list[str]:
        proc = subprocess.run(
            ["rg", "-Foiw", "--hidden", "--no-ignore", "-f", clean_denylist,
             str(target)],
            capture_output=True, text=True,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def _grep_scan(target: Path) -> list[str]:
        proc = subprocess.run(
            ["grep", "-rFoiI", "-f", clean_denylist, str(target)],
            capture_output=True, text=True,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()]

    _one_pass = _rg_scan if shutil.which("rg") else _grep_scan

    def _scan(target: Path) -> tuple[int, int]:
        """(direct hits, split-pass-only hits). Reported SEPARATELY because
        they need different triage, learned the first time this ran for real
        (2026-08-07): the split pass broke a numpy keyword argument into two
        words and matched an ordinary maths word sitting in the denylist.
        A direct hit is a term present as written — treat it as a leak until
        proven otherwise. A split-pass hit means a denylist term appeared as a
        COMPONENT of an identifier: either a real term buried in a name, or an
        ordinary English word that should not have been a denylist entry. Both
        still block the release; the breakdown is what makes triage a minute
        instead of an afternoon."""
        if not target.exists():
            return (0, 0)
        direct = len(_one_pass(target))
        with tempfile.TemporaryDirectory(prefix="contamination-split-") as tmp:
            mirror = _write_split_mirror(target, Path(tmp))
            split_total = len(_one_pass(mirror))
        # The split mirror is a superset: it only ADDS word boundaries, so
        # every direct hit reappears there. Report the delta as split-only.
        return (direct, max(0, split_total - direct))

    try:
        export_direct, export_split = _scan(export_dir)
        evidence_direct, evidence_split = _scan(REPO_ROOT / "_evidence")
    finally:
        Path(clean_denylist).unlink(missing_ok=True)
    return {
        "export_hit_count": export_direct + export_split,   # the hard gate
        "export_direct_hits": export_direct,
        "export_split_hits": export_split,
        "evidence_hit_count": evidence_direct + evidence_split,
    }


#: A case transition inside a word: the boundary camelCase hides a term behind.
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")


def split_identifier_boundaries(text: str) -> str:
    """Rewrite `_` and camelCase boundaries to spaces so a whole-word scan can
    see a term buried in an identifier. ONLY adds boundaries — a term inside an
    ordinary longer word stays unmatched, which is the property `-w` was added
    for in the first place."""
    return _CAMEL_BOUNDARY.sub(r"\1 \2", text.replace("_", " "))


def _write_split_mirror(src: Path, dest_root: Path) -> Path:
    """A boundary-split copy of every TEXT file under ``src``.

    Binary files are skipped, matching the `-I` / rg behaviour of the first
    pass — `_evidence/` carries multi-GB indexes and model caches that carry no
    denylist signal and would only cost minutes. Undecodable bytes are dropped
    rather than failing the scan: a scan that dies on one odd file is a scan
    that gets skipped."""
    dest_root.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        paths = [src]
        base = src.parent
    else:
        paths = [p for p in src.rglob("*") if p.is_file()]
        base = src
    for path in paths:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:      # binary: same skip as the first pass
            continue
        out = dest_root / path.relative_to(base)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            out.write_text(
                split_identifier_boundaries(raw.decode("utf-8", errors="ignore")),
                encoding="utf-8")
        except OSError:
            continue
    return dest_root


def step_local_tag(version: str) -> str:
    """Runbook §3 — local tag ONLY, never pushed. Refuses if the tag already
    exists (idempotent-safe: re-running --tag on an already-tagged commit is a
    no-op error, not a silent duplicate)."""
    tag = f"v{version}"
    existing = _run(["git", "tag", "-l", tag])
    if existing.stdout.strip():
        raise PublishError(f"tag {tag} already exists locally — nothing to do")
    proc = _run(["git", "tag", "-a", tag, "-m", f"release: cut {tag}"])
    if proc.returncode != 0:
        raise PublishError(f"git tag failed:\n{proc.stderr}")
    return tag


def read_pyproject_version() -> str:
    import re

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m
    return m.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--denylist", default=None, help="path to the external ground-truth denylist (never committed)")
    parser.add_argument("--tag", action="store_true", help="also cut the local v<X.Y.Z> tag on success (never pushes)")
    parser.add_argument("--check", action="store_true", help="run the scriptable gates only; skip the contamination scan and tag")
    args = parser.parse_args()

    version = read_pyproject_version()
    print(f"Publishing pyproject.toml SSOT version {version} — scriptable gates only; the push (Ruling 7 step 7) stays a HUMAN act.\n")

    try:
        print("[1/3] package_clients.py --validate-only ...")
        print(f"  {step_package_validate()}")

        with tempfile.TemporaryDirectory(prefix="brainiac-export-") as scratch:
            export_dir = Path(scratch) / "export"
            print("[2/3] export_cleanroom.py --output <scratch> ...")
            step_export(export_dir)
            print(f"  exported to a scratch dir (discarded on exit)")

            if args.check:
                print("  (--check: skipping contamination scan + tag)")
            else:
                if not args.denylist:
                    raise PublishError("--denylist is required unless --check is passed (runbook §5 hard gate)")
                print("[3/3] contamination scan (hard gate, redacted counts only) ...")
                counts = step_contamination_scan(export_dir, Path(args.denylist).expanduser())
                print(f"  export hits: {counts['export_hit_count']} (expected 0)")
                print(
                    f"    direct     : {counts['export_direct_hits']} "
                    "(term present as written — treat as a leak until proven otherwise)"
                )
                print(
                    f"    split-only : {counts['export_split_hits']} "
                    "(term found only as a COMPONENT of an identifier — either a real "
                    "term buried in a name, or an ordinary word that should not be a "
                    "denylist entry)"
                )
                print(
                    f"  _evidence hits: {counts['evidence_hit_count']} "
                    "(informational — _evidence never ships in the export; not "
                    "gated here, see runbook §5 manual adjudication)"
                )
                if counts["export_hit_count"] > 0:
                    raise PublishError(
                        f"contamination scan found {counts['export_hit_count']} hit(s) in the export tree "
                        f"({counts['export_direct_hits']} direct, {counts['export_split_hits']} split-only) — "
                        "no override flag; fix the export exclude list, the tracked-file mistake, or (for a "
                        "split-only hit on an ordinary word) the denylist entry, then re-run"
                    )

            if args.tag:
                tag = step_local_tag(version)
                print(f"\nLocal tag created: {tag} (never pushed — remote stays DISABLED://cleanroom-export-only)")

    except PublishError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1

    print("\nAll scriptable release gates passed. Remaining steps are runbook §6-§9 "
          "(migration doc already current, HUMAN publish, yank procedure if ever needed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
