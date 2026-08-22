"""The pre-publish phase family of `publish_public` — tag preflight, worktree tests, clean-room export, build, Windows CI (batch-2 drain).

Moved verbatim out of `publish_public`; every parent-surface collaborator
(`_run`, `_need`, `PublishError`, …) resolves through the parent module at CALL
time, so a test that monkeypatches one on `publish_public` keeps governing this
code exactly as before.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import publish_public as _pp  # noqa: E402


def pytest_failure_summary(stdout: str, *, max_names: int = 40) -> str:
    """Every FAILED name plus the count line. The old version printed the last 3
    lines, which on a 4-failure run named only 2 of them -- the hidden pair sent
    the operator diagnosing the wrong test. Names first, count last.

    Pass stdout AND stderr. pytest writes a startup failure -- an unrecognised
    argument, a collection error, a missing plugin -- to STDERR, so a summary
    built from stdout alone came back EMPTY and the phase reported "test suite
    failed in the tag worktree:" with nothing after the colon (measured on the
    v0.20.23 dry run). An empty failure report is a report that cannot inform.
    """
    lines = stdout.strip().splitlines()
    failed = [ln for ln in lines if ln.startswith("FAILED ")]
    counts = [ln for ln in lines if re.search(r"\d+ (failed|passed|error)", ln)]
    out = failed[:max_names]
    if len(failed) > max_names:
        out.append(f"... and {len(failed) - max_names} more FAILED lines")
    if counts:
        out.append(counts[-1])
    return ("\n".join(out) or "\n".join(lines[-3:])
            or "(pytest produced no output at all -- it did not start; "
               "check the invocation and the interpreter's plugins)")


def suite_parallel_args(python: str) -> tuple[list[str], str]:
    """The parallel flags for THIS interpreter, or none plus the reason.

    AGENTS.md §8's parallel form needs pytest-xdist and pytest-timeout, which
    live in the `dev` extra. The pipeline runs under whichever interpreter has
    pytest+build+twine, and on this host that is `~/.brainiac/venv` -- which
    carries bare pytest. So the parallel invocation added in ae6efb1 aborted
    the phase in 3 seconds with `unrecognized arguments: -n --dist --timeout`,
    and had been broken in the SHIPPING context since it landed, because no
    release ran in between to exercise it.

    Degrade, never hard-fail: a missing test accelerator must not block a
    release. But say so in the phase summary -- silently running sequentially
    is a 12-minute regression wearing a pass.
    """
    if _pp._run([python, "-c", "import xdist, pytest_timeout"]).returncode != 0:
        return [], (f"SEQUENTIALLY -- {python} has no pytest-xdist/pytest-timeout; "
                    f"`{python} -m pip install -e '.[dev]'` halves this phase")
    return (["-n", "8", "--dist", "loadfile", "--timeout", "300"],
            "in parallel (-n 8 --dist loadfile, AGENTS.md §8)")


# The canonical parallel suite invocation (AGENTS.md §8). `--dist loadfile`
# keeps every test in one FILE on one worker, which is what makes the parallel
# run stable — the fcntl lock tests, the autouse env isolation and the node
# suite all assume file-local ordering. Measured 2026-08-13: 14:53 sequential
# -> ~3:15 parallel, identical pass set. The one deselect asserts against LIVE
# host COS ledgers and is machine-bound by design; it is deselected in the
# sequential gates too, so parallel coverage equals sequential coverage.
SUITE_DESELECT = (
    "tests/test_cos_runverify_body_corpus.py::"
    "test_corpus_join_zero_false_positives_across_every_real_historical_run")


SUITE_CACHE = Path(__file__).resolve().parents[1] / "_evidence" / "releases" / ".suite-pass.json"


def _suite_cache_read(sha: str) -> str | None:
    try:
        return json.loads(SUITE_CACHE.read_text(encoding="utf-8")).get(sha)
    except (OSError, ValueError, AttributeError):
        return None


def _suite_cache_write(sha: str, summary: str) -> None:
    try:
        data = json.loads(SUITE_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[sha] = summary
    # Keep the file small; a release only ever reads its own sha.
    for old in list(data)[:-20]:
        data.pop(old)
    SUITE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SUITE_CACHE.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def worktree_sha(worktree: Path) -> str | None:
    """The commit the throwaway worktree is checked out at."""
    proc = _pp._run(["git", "rev-parse", "HEAD"], cwd=worktree)
    return proc.stdout.strip() if proc.returncode == 0 else None


def phase_tests(worktree: Path, *, reuse_pass_for_sha: str | None = None) -> str:
    """Run the suite in the tag worktree.

    ``reuse_pass_for_sha`` is set ONLY on a resume: an identical commit has an
    identical suite result, so re-running it costs 3-18 minutes to learn what a
    prior phase of the SAME release already proved. Keyed on the commit sha and
    never the tag name — a tag that moved (a fix cut after a failed run) has a
    different sha and re-runs, which is exactly the v0.20.22 case.
    """
    if reuse_pass_for_sha:
        cached = _suite_cache_read(reuse_pass_for_sha)
        if cached:
            return f"{cached} [reused: same commit {reuse_pass_for_sha[:9]} passed earlier this release]"
    parallel_args, mode = _pp.suite_parallel_args(sys.executable)
    proc = _pp._run(
        [sys.executable, "-m", "pytest", "-q", *parallel_args,
         "--deselect", SUITE_DESELECT],
        cwd=worktree, timeout=3600)
    # stdout AND stderr: pytest reports a startup failure on stderr, and a
    # summary built from stdout alone came back blank (v0.20.23 dry run).
    summary = _pp.pytest_failure_summary(f"{proc.stdout}\n{proc.stderr}")
    if proc.returncode != 0:
        raise _pp.PublishError(
            f"test suite failed in the tag worktree (ran {mode}):\n{summary}")
    line = f"{summary.splitlines()[-1] if summary else 'passed'} — ran {mode}"
    sha = worktree_sha(worktree)
    if sha:
        _suite_cache_write(sha, line)
    return line


def _load_denylist_terms(denylist: Path) -> list[str]:
    terms = [ln for ln in denylist.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not terms:
        raise _pp.PublishError(f"denylist {denylist} has no usable terms after stripping comments/blanks")
    return terms


def _scan_tree(target: Path, terms: list[str]) -> int:
    """Whole-word, case-insensitive fixed-string scan; returns hit count.
    Same rg/grep split as tools/publish_release.py (ripgrep preferred: BSD
    grep effectively hangs on multi-MB single-line JSON)."""
    with tempfile.NamedTemporaryFile("w", suffix=".denylist", delete=False,
                                     encoding="utf-8") as tf:
        tf.write("\n".join(terms) + "\n")
        pat = tf.name
    try:
        if shutil.which("rg"):
            proc = _pp._run(["rg", "-Foiw", "--hidden", "--no-ignore", "-f", pat, str(target)])
        else:
            proc = _pp._run(["grep", "-rFoiwI", "-f", pat, str(target)])
        return len([ln for ln in (proc.stdout or "").splitlines() if ln.strip()])
    finally:
        Path(pat).unlink(missing_ok=True)


def scanner_self_test(terms: list[str], scan=_scan_tree) -> None:
    """Prove the scanner CAN fail before trusting any all-clear it produces.
    Plants a real denylist term in a scratch tree; the scan must find it.
    (The 0.16.0 contamination gate returned 0 for every release for months —
    a gate that cannot fail is indistinguishable from a gate that passes.)"""
    with tempfile.TemporaryDirectory(prefix="canary-") as d:
        canary = Path(d) / "canary.txt"
        canary.write_text(f"planted {terms[0]} canary\n", encoding="utf-8")
        found = scan(Path(d), terms)
    if found < 1:
        raise _pp.PublishError(
            "contamination scanner FAILED ITS SELF-TEST: a planted denylist "
            "term was not detected — every all-clear it has produced is "
            "untrustworthy; fix the scanner before releasing anything")


def phase_export(worktree: Path, scratch: Path, denylist: Path) -> Path:
    """Clean-room export FROM THE TAG WORKTREE + contamination hard gate,
    with the scanner self-test run first."""
    export_dir = scratch / "export"
    _pp._need(_pp._run([sys.executable, str(worktree / "tools" / "export_cleanroom.py"),
                "--output", str(export_dir), "--repo-root", str(worktree)],
               cwd=worktree), "export_cleanroom.py")
    terms = _pp._load_denylist_terms(denylist)
    _pp.scanner_self_test(terms)
    hits = _pp._scan_tree(export_dir, terms)
    if hits:
        raise _pp.PublishError(
            f"contamination scan found {hits} hit(s) in the export tree — "
            f"hard gate, no override; scrub the tracked files and re-tag")
    return export_dir


def phase_build(export_dir: Path, version: str, denylist: Path) -> list[Path]:
    """Build sdist+wheel FROM THE EXPORT (never the dev tree), then extract
    and scan the sdist — gate the artifact that ships, not its inputs.

    Prefer `uv build` (self-contained, and immune to the trap below); fall
    back to `python -m build`. NOTE the probe must not run from the repo
    root: this repo's `build/` OUTPUT DIRECTORY shadows the pypa `build`
    module there ("'build' is a package and cannot be directly executed"),
    which reads as build-not-installed when it is."""
    if shutil.which("uv"):
        _pp._need(_pp._run(["uv", "build"], cwd=export_dir, timeout=1200), "uv build")
    else:
        with tempfile.TemporaryDirectory(prefix="probe-") as neutral:
            if _pp._run([sys.executable, "-m", "build", "--version"],
                    cwd=Path(neutral)).returncode != 0:
                raise _pp.PublishError(
                    "neither uv nor the 'build' package is available — install one "
                    "in YOUR env (python3 -m pip install --upgrade build twine), "
                    "never this repo's")
        _pp._need(_pp._run([sys.executable, "-m", "build"], cwd=export_dir, timeout=1200),
              "python -m build")
    dist = export_dir / "dist"
    artifacts = sorted(dist.glob(f"brainiac_cli-{version}*"))
    wheels = [p for p in artifacts if p.suffix == ".whl"]
    sdists = [p for p in artifacts if p.name.endswith(".tar.gz")]
    if not wheels or not sdists:
        built = [p.name for p in dist.glob("*")]
        raise _pp.PublishError(
            f"expected brainiac_cli-{version} wheel + sdist, found: {built} — "
            f"version skew between tag and build")
    terms = _pp._load_denylist_terms(denylist)
    with tempfile.TemporaryDirectory(prefix="sdist-scan-") as d:
        with tarfile.open(sdists[0]) as tar:
            tar.extractall(d, filter="data")
        hits = _pp._scan_tree(Path(d), terms)
    if hits:
        raise _pp.PublishError(
            f"contamination scan found {hits} hit(s) INSIDE the built sdist — "
            f"the tree scan missed something the artifact carries; hard gate")
    return artifacts


def phase_windows_ci(accept_reason: str | None) -> str:
    """The only automated Windows signal lives on the public repo's
    distribution-matrix workflow — and its failures land on dependabot
    branches where they read as noise (that is how the fcntl blocker sat red
    for six days while 0.19.11 shipped). Read the latest run on ANY branch.

    Caveat the gate cannot close: GitHub runners use pwsh 7, so the
    PowerShell-5.1 encoding class never reproduces there — that class is
    guarded by tests/test_windows_portability.py instead."""
    proc = _pp._run(["gh", "api",
                 f"repos/{_pp.PUBLIC_REPO}/actions/workflows/{_pp.DIST_MATRIX_WORKFLOW}/runs?per_page=1"])
    if proc.returncode != 0:
        if accept_reason:
            return f"UNREACHABLE, explicitly accepted: {accept_reason}"
        raise _pp.PublishError(
            "cannot read the distribution-matrix workflow (gh api failed) — "
            "pass --accept-windows-ci '<reason>' to proceed without the signal")
    runs = json.loads(proc.stdout).get("workflow_runs", [])
    if not runs:
        if accept_reason:
            return f"NO RUNS, explicitly accepted: {accept_reason}"
        raise _pp.PublishError("distribution-matrix has no runs — pass --accept-windows-ci '<reason>'")
    latest = runs[0]
    line = (f"{latest['conclusion']} on {latest['head_branch']} at {latest['created_at']}"
            f" ({latest['html_url']})")
    if latest["conclusion"] != "success":
        if accept_reason:
            return f"latest run {line} — explicitly accepted: {accept_reason}"
        raise _pp.PublishError(
            f"latest distribution-matrix run is {line}.\n"
            f"A red run on a dependabot branch is STILL a real Windows failure "
            f"(2026-07-23 lesson). Read the log; pass --accept-windows-ci "
            f"'<reason>' only if the failure is genuinely unrelated")
    return line
