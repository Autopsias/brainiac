#!/usr/bin/env python3
"""Guarded end-to-end public release pipeline (runbook §7.6-§8 in one command).

    python3 tools/publish_public.py v0.19.18 --denylist ~/brainiac-release-groundtruth.txt
    python3 tools/publish_public.py v0.19.18 --denylist <path> --dry-run       # verify only, no gates
    python3 tools/publish_public.py v0.19.18 --denylist <path> --from npm      # resume after a partial run

Owner decision 2026-07-29 (amending the runbook's earlier "publishing is
never scripted" rule): the pipeline ORCHESTRATES the release, but every
irreversible act — TestPyPI upload, PyPI upload, npm publish, public git
push — stops at an interactive gate that states what has been verified, what
is about to happen and why it cannot be undone, and proceeds only when the
operator types the exact version string. Automation composes the steps; the
human still performs each act.

Why this exists (measured, 2026-07-29): the manual chain shipped v0.19.17 to
PyPI WITHOUT the Windows fixes that were already committed — the tag was cut
one commit too early and nothing cross-checked tag content against intent.
The same day's post-mortem found two more latent classes: the clean-room
export copies file BODIES from the working tree (so a concurrent session's
uncommitted work would have shipped), and the only Windows CI signal had been
red for six days on dependabot branches where nobody read it.

Structural guarantees (not gate-dependent):
- Everything is built from a THROWAWAY WORKTREE at the tag. The dev repo's
  working tree — including any concurrent session's uncommitted work — cannot
  reach the artifact.
- The contamination scanner must first find a PLANTED canary (a real term
  from the operator's denylist injected into a scratch copy). A scanner that
  cannot fail is not allowed to pass anything (the 0.16.0 lesson: a blank
  denylist line silently zeroed every scan for twelve releases).
- The built sdist is extracted and scanned again — the artifact that ships,
  not just the tree it was built from.
- The public push happens from a FRESH CLONE of the public repo in a temp
  dir. This repo's `disabled-public-DO-NOT-PUSH` remote is never touched,
  never re-enabled, never pushed (ADR-0001 unchanged).
- No credential is ever read, written, or probed: twine/npm/git run in the
  operator's own authenticated sessions and prompt on their own.

Every phase appends to `_evidence/releases/publish-<version>.md` as it
completes, so an aborted run leaves a truthful partial transcript.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_REMOTE_NAME = "disabled-public-DO-NOT-PUSH"  # fetch URL is the public repo; push URL is disabled and stays so
PUBLIC_REPO = "Autopsias/brainiac"                  # the gh-api slug; the push URL is read from the remote above
DIST_MATRIX_WORKFLOW = "distribution-matrix.yml"

PHASES = [
    "preflight", "worktree", "tests", "export", "build", "windows-ci",
    "testpypi", "pypi", "npm", "public-git", "release-asset", "post-verify",
]


class PublishError(Exception):
    pass


class GateDeclined(Exception):
    pass


def _run(cmd: list[str], *, cwd: Path | None = None, interactive: bool = False,
         timeout: int | None = 1800,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Interactive=True hands the terminal to the child (twine/npm prompting
    for the operator's own credentials — this script never sees them)."""
    if interactive:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


AUTH_URL_RE = re.compile(r"https://\S*(?:npmjs\.com|npmjs\.org)/(?:auth|login)\S*")


def _run_pty(cmd: list[str], *, cwd: Path | None = None,
             timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a command attached to a pseudo-terminal, streaming its output.

    npm's one-time-password step has two modes and it picks by asking whether
    stdout is a terminal. On a TTY it prints a click-to-authorize URL and waits
    for the browser round-trip; with no TTY it refuses outright (`EOTP`, "requires
    a one-time password"), which is a dead end for any non-interactive caller —
    a relayed authenticator code expires before a run reaches the publish step.
    A pty gets the URL flow, and the operator authorizes by clicking.

    Output is echoed as it arrives (so the URL is visible in a log being
    tailed, not just at exit) and returned in `stdout` for the caller to scan.
    A `Press ENTER`-style prompt is answered automatically — the browser round
    trip, not the keypress, is the actual human gate.
    """
    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True)
    os.close(slave)
    chunks: list[str] = []
    pending = ""
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise PublishError(
                    f"{cmd[0]} timed out after {timeout}s waiting for browser authorization")
            if not select.select([master], [], [], 1.0)[0]:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 4096)
            except OSError:      # EIO — the child closed the pty
                break
            if not data:
                break
            text = data.decode("utf-8", "replace")
            chunks.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()
            pending = (pending + text)[-400:]
            if re.search(r"press\s+(?:enter|any key)", pending, re.I):
                os.write(master, b"\n")
                pending = ""
    finally:
        os.close(master)
    return subprocess.CompletedProcess(cmd, proc.wait(), "".join(chunks), "")


def _need(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if proc.returncode != 0:
        out = getattr(proc, "stdout", "") or ""
        err = getattr(proc, "stderr", "") or ""
        raise PublishError(f"{what} failed (exit {proc.returncode})\n{out}\n{err}")
    return proc


# --------------------------------------------------------------------------
# evidence transcript — appended per phase so a crash leaves a partial record
# --------------------------------------------------------------------------

class Evidence:
    def __init__(self, version: str):
        self.path = REPO_ROOT / "_evidence" / "releases" / f"publish-{version}.md"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                f"# Publish transcript — {version}\n\n"
                f"Started {_dt.datetime.now().astimezone().isoformat(timespec='seconds')} "
                f"by tools/publish_public.py. Appended per phase; an aborted run\n"
                f"leaves this record truthful and partial.\n\n", encoding="utf-8")

    def record(self, phase: str, status: str, detail: str = "") -> None:
        stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        body = f"## {phase} — {status}\n{stamp}\n"
        if detail:
            body += f"\n{detail.rstrip()}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(body + "\n")


# --------------------------------------------------------------------------
# the human gate
# --------------------------------------------------------------------------

def gate(version: str, act: str, why: str, verified: list[str], *,
         input_fn=input, dry_run: bool = False,
         confirmed: bool = False, consent_note: str | None = None) -> str:
    """Stop before an irreversible act. Two consent shapes, both deliberate:

    - Terminal run: the operator types the exact version string — a
      keystroke naming what ships, not a 'y'.
    - Harness run (`--confirm <act> --consent-note "..."`): the act was
      pre-authorized by the owner IN THE SAME SESSION (e.g. answering an
      AskUserQuestion decision card after reading the verified summary);
      the note recording who/how goes into the evidence transcript. A
      pre-authorization names ONE act — there is no --confirm-everything.

    Returns a one-line provenance string for the evidence transcript."""
    print(f"\n{'─' * 62}")
    print(f"GATE: {act}")
    print(f"  Why this gate exists: {why}")
    print("  Verified so far:")
    for item in verified:
        print(f"    - {item}")
    if dry_run:
        print("  (--dry-run: stopping here, nothing uploaded or pushed)")
        raise GateDeclined(f"dry-run stop before: {act}")
    if confirmed:
        print(f"  pre-authorized: {consent_note}")
        return f"pre-authorized — {consent_note}"
    if not sys.stdin.isatty():
        raise PublishError(
            f"gate '{act}' needs consent but there is no terminal to ask on — "
            f"either run interactively, or pass --confirm <act> with "
            f"--consent-note '<who authorized it and how>' after the owner "
            f"approved this specific act in-session")
    answer = input_fn(f"  Type the version to proceed [{version}], anything else aborts: ").strip()
    if answer != version:
        raise GateDeclined(f"operator declined at gate: {act} (typed {answer!r})")
    return "operator typed the version at the interactive gate"


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------

def phase_preflight(tag: str, *, expect_published: bool | None = False) -> str:
    """Tag exists; tag name matches the pyproject version AT THE TAG; and the
    PyPI state matches what the run's starting phase implies.

    `expect_published` is deliberately tri-state, because "is this version
    supposed to be on PyPI already?" has three honest answers:

    * ``False`` — a fresh run. Publishing a version PyPI already serves would
      mean the tag's contents and the published artifact can differ silently.
    * ``None`` — a resume INTO the upload phases (`--from testpypi` / `pypi`).
      The upload may or may not have gone through before the run died; twine
      runs with ``--skip-existing``, so either state is fine and neither is
      evidence of a problem.
    * ``True`` — a resume PAST the upload (`--from npm` / `public-git` /
      `post-verify`). Here the version being absent is the anomaly: skipping an
      upload that never happened would ship an npm bootstrap pointing at
      nothing.

    A single boolean got this wrong in both directions: it dead-ended every
    post-upload resume on the already-published guard, and then dead-ended
    `--from pypi` for the opposite reason.
    """
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise PublishError(f"tag must look like vX.Y.Z, got {tag!r}")
    version = tag[1:]
    _need(_run(["git", "rev-parse", "--verify", f"refs/tags/{tag}"], cwd=REPO_ROOT),
          f"tag {tag} not found locally")
    show = _need(_run(["git", "show", f"{tag}:pyproject.toml"], cwd=REPO_ROOT),
                 f"pyproject.toml missing at {tag}")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', show.stdout)
    tagged_version = m.group(1) if m else None
    if tagged_version != version:
        # THE 0.19.17 failure class: the tag and the code it points at disagree.
        raise PublishError(
            f"tag {tag} points at a commit whose pyproject version is "
            f"{tagged_version!r} — the tag was cut on the wrong commit")
    # PyPI monotonicity — query the public JSON API (read-only, no auth).
    proc = _run(["curl", "-fsS", "--max-time", "30",
                 "https://pypi.org/pypi/brainiac-cli/json"])
    if proc.returncode == 0:
        data = json.loads(proc.stdout)
        released = set(data.get("releases", {}))
        latest = data["info"]["version"]
        if expect_published is None:
            state = "already serves" if version in released else "does not yet serve"
            return (f"tag/version consistent; PyPI {state} {version} "
                    f"(resume into the upload phases — either state is fine)")
        if expect_published:
            if version not in released:
                raise PublishError(
                    f"resuming past the pypi phase, but PyPI does not serve {version} — "
                    f"do not skip an upload that never happened")
            return f"tag/version consistent; PyPI already serves {version} (resume past upload)"
        if version in released:
            raise PublishError(
                f"{version} is already on PyPI — uploads are permanent per "
                f"version; cut a new patch instead of re-publishing")
        def _key(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split(".")[:3] if x.isdigit())
        if _key(version) <= _key(latest):
            raise PublishError(
                f"{version} is not above PyPI's current latest {latest} — "
                f"publishing it would not become the default install")
        return f"tag/version consistent; PyPI latest {latest}, {version} is new and above it"
    return "tag/version consistent; PyPI unreachable (offline?) — monotonicity NOT verified"


def phase_worktree(tag: str, scratch: Path) -> Path:
    """A throwaway worktree at the tag: the ONLY tree anything downstream
    reads. Structurally excludes the dev repo's (possibly dirty) working tree."""
    wt = scratch / "worktree"
    _need(_run(["git", "worktree", "add", "--detach", str(wt), tag], cwd=REPO_ROOT),
          "worktree add")
    status = _need(_run(["git", "status", "--porcelain"], cwd=wt), "worktree status")
    if status.stdout.strip():
        raise PublishError(f"fresh worktree at {tag} is not clean:\n{status.stdout}")
    return wt


def pytest_failure_summary(stdout: str, *, max_names: int = 40) -> str:
    """Every FAILED name plus the count line. The old version printed the last 3
    lines, which on a 4-failure run named only 2 of them -- the hidden pair sent
    the operator diagnosing the wrong test. Names first, count last."""
    lines = stdout.strip().splitlines()
    failed = [ln for ln in lines if ln.startswith("FAILED ")]
    counts = [ln for ln in lines if re.search(r"\d+ (failed|passed|error)", ln)]
    out = failed[:max_names]
    if len(failed) > max_names:
        out.append(f"... and {len(failed) - max_names} more FAILED lines")
    if counts:
        out.append(counts[-1])
    return "\n".join(out) or "\n".join(lines[-3:])


def phase_tests(worktree: Path) -> str:
    proc = _run([sys.executable, "-m", "pytest", "-q"], cwd=worktree, timeout=3600)
    summary = pytest_failure_summary(proc.stdout)
    if proc.returncode != 0:
        raise PublishError(f"test suite failed in the tag worktree:\n{summary}")
    return summary.splitlines()[-1] if summary else "passed"


def _load_denylist_terms(denylist: Path) -> list[str]:
    terms = [ln for ln in denylist.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not terms:
        raise PublishError(f"denylist {denylist} has no usable terms after stripping comments/blanks")
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
            proc = _run(["rg", "-Foiw", "--hidden", "--no-ignore", "-f", pat, str(target)])
        else:
            proc = _run(["grep", "-rFoiwI", "-f", pat, str(target)])
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
        raise PublishError(
            "contamination scanner FAILED ITS SELF-TEST: a planted denylist "
            "term was not detected — every all-clear it has produced is "
            "untrustworthy; fix the scanner before releasing anything")


def phase_export(worktree: Path, scratch: Path, denylist: Path) -> Path:
    """Clean-room export FROM THE TAG WORKTREE + contamination hard gate,
    with the scanner self-test run first."""
    export_dir = scratch / "export"
    _need(_run([sys.executable, str(worktree / "tools" / "export_cleanroom.py"),
                "--output", str(export_dir), "--repo-root", str(worktree)],
               cwd=worktree), "export_cleanroom.py")
    terms = _load_denylist_terms(denylist)
    scanner_self_test(terms)
    hits = _scan_tree(export_dir, terms)
    if hits:
        raise PublishError(
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
        _need(_run(["uv", "build"], cwd=export_dir, timeout=1200), "uv build")
    else:
        with tempfile.TemporaryDirectory(prefix="probe-") as neutral:
            if _run([sys.executable, "-m", "build", "--version"],
                    cwd=Path(neutral)).returncode != 0:
                raise PublishError(
                    "neither uv nor the 'build' package is available — install one "
                    "in YOUR env (python3 -m pip install --upgrade build twine), "
                    "never this repo's")
        _need(_run([sys.executable, "-m", "build"], cwd=export_dir, timeout=1200),
              "python -m build")
    dist = export_dir / "dist"
    artifacts = sorted(dist.glob(f"brainiac_cli-{version}*"))
    wheels = [p for p in artifacts if p.suffix == ".whl"]
    sdists = [p for p in artifacts if p.name.endswith(".tar.gz")]
    if not wheels or not sdists:
        built = [p.name for p in dist.glob("*")]
        raise PublishError(
            f"expected brainiac_cli-{version} wheel + sdist, found: {built} — "
            f"version skew between tag and build")
    terms = _load_denylist_terms(denylist)
    with tempfile.TemporaryDirectory(prefix="sdist-scan-") as d:
        with tarfile.open(sdists[0]) as tar:
            tar.extractall(d, filter="data")
        hits = _scan_tree(Path(d), terms)
    if hits:
        raise PublishError(
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
    proc = _run(["gh", "api",
                 f"repos/{PUBLIC_REPO}/actions/workflows/{DIST_MATRIX_WORKFLOW}/runs?per_page=1"])
    if proc.returncode != 0:
        if accept_reason:
            return f"UNREACHABLE, explicitly accepted: {accept_reason}"
        raise PublishError(
            "cannot read the distribution-matrix workflow (gh api failed) — "
            "pass --accept-windows-ci '<reason>' to proceed without the signal")
    runs = json.loads(proc.stdout).get("workflow_runs", [])
    if not runs:
        if accept_reason:
            return f"NO RUNS, explicitly accepted: {accept_reason}"
        raise PublishError("distribution-matrix has no runs — pass --accept-windows-ci '<reason>'")
    latest = runs[0]
    line = (f"{latest['conclusion']} on {latest['head_branch']} at {latest['created_at']}"
            f" ({latest['html_url']})")
    if latest["conclusion"] != "success":
        if accept_reason:
            return f"latest run {line} — explicitly accepted: {accept_reason}"
        raise PublishError(
            f"latest distribution-matrix run is {line}.\n"
            f"A red run on a dependabot branch is STILL a real Windows failure "
            f"(2026-07-23 lesson). Read the log; pass --accept-windows-ci "
            f"'<reason>' only if the failure is genuinely unrelated")
    return line


def _poll(run_once, *, what: str, ok=None, seconds: int = 300,
          every: int = 15) -> subprocess.CompletedProcess:
    """Retry a post-upload check until it passes or the window closes.

    Every package index and registry serves reads from a cache that trails its
    own write path, so the first check after a successful upload can legitimately
    404. Failing there reports a COMPLETED upload as a failed release, and the
    obvious next move -- re-publish -- is impossible for a permanent version.
    """
    ok = ok or (lambda p: p.returncode == 0)
    deadline = time.monotonic() + seconds
    while True:
        proc = run_once()
        if ok(proc):
            return proc
        if time.monotonic() > deadline:
            raise PublishError(
                f"{what} still failing {seconds}s after upload — the upload itself "
                f"may well have succeeded, so check the index/registry before "
                f"retrying anything\n{proc.stdout}{proc.stderr}")
        time.sleep(every)


def _throwaway_venv(scratch: Path, label: str) -> Path:
    """A fresh venv, returned as its bin dir. Its pip is the ONLY pip this
    pipeline uses: the launching interpreter may legitimately have none (running
    under `uv run`, for instance), and `sys.executable -m pip` then fails with
    "No module named pip" -- which looks like a broken release rather than a
    broken invocation. `venv` seeds pip via ensurepip, so this always has one."""
    venv = scratch / f"venv-{label}"
    if not venv.exists():
        _need(_run([sys.executable, "-m", "venv", str(venv)]), "venv create")
    return venv / ("Scripts" if os.name == "nt" else "bin")


#: Hosts pip may resolve DEPENDENCIES from. Production PyPI and its CDN only.
_PYPI_HOSTS = frozenset({"pypi.org", "www.pypi.org", "files.pythonhosted.org"})


def _non_pypi_index(arg: str) -> bool:
    """True when ``arg`` is an index URL pointing somewhere other than PyPI.

    Host equality, never a substring: ``test.pypi.org`` ends with ``pypi.org``
    and contains ``pypi.org/simple``, so both of the obvious shortcuts accept
    exactly the index this is meant to reject."""
    if not arg.startswith(("http://", "https://")):
        return False
    return (urllib.parse.urlsplit(arg).hostname or "").lower() not in _PYPI_HOSTS


def _clean_venv_check(version: str, index_args: list[str], scratch: Path, label: str,
                      *, deps_from: Path | None = None) -> str:
    """Install brainiac-cli==version into a throwaway venv and require
    `brain --version` to print exactly the version. Never this repo's venv.

    ``deps_from`` splits the install in two, and is REQUIRED whenever
    ``index_args`` names an index other than PyPI (Codex cloud review,
    2026-08-07). pip picks a candidate by VERSION across every configured
    index, not by preferring PyPI for dependencies -- so a single
    `--index-url testpypi --extra-index-url pypi` install lets anyone who
    registers one of our unconstrained dependency names on TestPyPI, at a
    higher version, win the resolution. The payload then executes here via a
    build hook or a `.pth` the moment `brain --version` starts Python, on the
    release host, which at that point holds PyPI, npm and git credentials.

    The split removes the race without losing the round-trip test:

      1. install the LOCALLY BUILT artifact with PyPI as the only index, which
         resolves and installs every dependency from PyPI and nowhere else;
      2. force-reinstall just our own package, `--no-deps`, from the index
         under test -- so what `brain --version` runs is genuinely the bytes
         that index served back.
    """
    # Refuse BEFORE any side effect -- no venv, no network. A guard that only
    # fires after the expensive part is a guard you are tempted to skip.
    #
    # Compare the HOST, not a substring: `test.pypi.org` contains the literal
    # text `pypi.org`, so a substring check silently accepts the one index this
    # guard exists to reject. (Caught by its own test, 2026-08-07.)
    if deps_from is None and any(_non_pypi_index(a) for a in index_args):
        raise PublishError(
            f"{label}: refusing to resolve dependencies against a non-PyPI "
            "index; pass deps_from=<locally built wheel>")
    bin_dir = _throwaway_venv(scratch, label)
    pip = bin_dir / "pip"
    brain = bin_dir / "brain"
    install_args = list(index_args)
    if deps_from is not None:
        _poll(lambda: _run([str(pip), "install", "--quiet",
                            "--index-url", "https://pypi.org/simple/",
                            str(deps_from)], timeout=1200),
              what=f"dependencies from PyPI only (for {label})")
        install_args = ["--no-deps", "--force-reinstall", *index_args]
    _poll(lambda: _run([str(pip), "install", "--quiet", *install_args,
                        f"brainiac-cli=={version}"], timeout=1200),
          what=f"pip install from {label}")
    out = _need(_run([str(brain), "--version"]), "brain --version").stdout.strip()
    if version not in out:
        raise PublishError(f"{label} artifact prints {out!r}, expected {version}")
    return f"installed from {label}; brain --version -> {out}"


def phase_testpypi(artifacts: list[Path], version: str, scratch: Path) -> str:
    """Upload to TestPyPI (operator's own twine auth), verify from a clean venv."""
    # --skip-existing makes a resume safe: an upload that succeeded but whose
    # verification lagged must not turn into a hard "file already exists" on the
    # retry (an index version is permanent; re-uploading is not an option).
    proc = _run([sys.executable, "-m", "twine", "upload", "--skip-existing",
                 "--repository", "testpypi", *[str(p) for p in artifacts]],
                interactive=True)
    _need(proc, "twine upload to testpypi")
    wheels = [p for p in artifacts if p.suffix == ".whl"]
    if not wheels:
        raise PublishError("testpypi verification needs the locally built wheel "
                           "to install dependencies from PyPI only")
    return _clean_venv_check(
        version,
        ["--index-url", "https://test.pypi.org/simple/"],
        scratch, "testpypi", deps_from=wheels[0])


def phase_pypi(artifacts: list[Path], version: str, scratch: Path) -> str:
    """Upload to production PyPI, then verify the served artifact is
    byte-identical to what was built (sha256 over pip download)."""
    proc = _run([sys.executable, "-m", "twine", "upload", "--skip-existing",
                 *[str(p) for p in artifacts]], interactive=True)
    _need(proc, "twine upload to pypi")
    dl = scratch / "pypi-download"
    dl.mkdir(exist_ok=True)
    pip = _throwaway_venv(scratch, "pypi-verify") / "pip"
    _poll(lambda: _run([str(pip), "download", "--no-deps", "-d", str(dl),
                        f"brainiac-cli=={version}"], timeout=600),
          what="pip download from pypi")
    local = {p.name: p for p in artifacts}
    served_names = []
    for p in sorted(dl.glob("brainiac_cli-*")):
        mine = local.get(p.name)
        if mine is None:
            continue
        served_names.append(p.name)
        if hashlib.sha256(p.read_bytes()).hexdigest() == \
           hashlib.sha256(mine.read_bytes()).hexdigest():
            continue
        # Container bytes differing is EXPECTED across runs: neither wheels nor
        # sdists are reproducible by default (zip/tar entries carry mtimes and an
        # ordering), so a resumed run that rebuilt its artifacts always sees a
        # different archive hash than the one uploaded earlier. Comparing archive
        # bytes therefore cried tampering on a healthy 0.19.19 release. What
        # actually matters is whether the CODE differs, so compare member by
        # member -- which still catches a genuinely altered artifact.
        diff = _archive_content_diff(mine, p)
        if diff:
            raise PublishError(
                f"PyPI serves a {p.name} whose CONTENTS differ from what was built "
                f"-- investigate immediately before publishing anything else.\n"
                f"differing members: {', '.join(diff[:20])}")
    checked = ", ".join(served_names) or "(none downloadable yet)"
    return f"contents verified member-by-member against built artifacts: {checked}"


def _archive_members(path: Path) -> dict[str, str]:
    """{member name: sha256 of its bytes} for a wheel/zip or an sdist tarball.
    Names are compared without their leading top-level directory so a rebuild's
    identical payload matches regardless of archive-level packaging noise."""
    out: dict[str, str] = {}
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not info.is_dir():
                    out[info.filename] = hashlib.sha256(z.read(info.filename)).hexdigest()
        return out
    with tarfile.open(path) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            rel = member.name.split("/", 1)[-1]
            out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _archive_content_diff(built: Path, served: Path) -> list[str]:
    """Member names whose CONTENTS differ (or exist on only one side)."""
    a, b = _archive_members(built), _archive_members(served)
    return sorted(set(a) ^ set(b)) + sorted(k for k in set(a) & set(b) if a[k] != b[k])


def phase_npm(export_dir: Path, version: str, scratch: Path,
              gate_fn) -> str:
    """Pack + smoke-test the npx bootstrap from the EXPORT tree, gate, publish,
    verify the registry serves the new version."""
    # Resume safety: a publish that succeeded but whose verification lagged (or
    # whose later phase failed) must not be attempted twice — npm versions are
    # permanent, and a second publish is a hard error, not a no-op.
    already = _run(["npm", "view", f"brainiac-install@{version}", "version"])
    if already.returncode == 0 and version in already.stdout:
        return f"npm already serves brainiac-install@{version} (nothing re-published)"
    pkg = export_dir / "packaging" / "npm" / "brainiac-install"
    pack_dir = scratch / "npm-pack"
    pack_dir.mkdir(exist_ok=True)
    _need(_run(["npm", "pack", "--pack-destination", str(pack_dir)], cwd=pkg), "npm pack")
    tarballs = list(pack_dir.glob(f"brainiac-install-{version}.tgz"))
    if not tarballs:
        raise PublishError(f"npm pack produced no brainiac-install-{version}.tgz — version skew")
    prefix = pack_dir / "install-prefix"
    _need(_run(["npm", "install", "-g", "--prefix", str(prefix),
                "--install-strategy=hoisted", str(tarballs[0])], cwd=pack_dir, timeout=600),
          "npm install of packed tarball")
    _need(_run([str(prefix / "bin" / "brainiac-install"), "--dry-run"]),
          "brainiac-install --dry-run smoke")
    gate_fn("npm", "publish brainiac-install to npm",
            "npm publishes are permanent per version; the bootstrap must point "
            "at a PyPI version that exists (it does — the pypi phase passed)",
            [f"tarball packed at {version} and smoke-tested (--dry-run exit 0)",
             "PyPI publish verified in the previous phase"])
    print("\nnpm publish: if 2FA is on, npm prints an authorization URL below — open it and\n"
          "approve; the publish continues by itself (waiting up to 15 minutes).\n", flush=True)
    published = _run_pty(["npm", "publish", "--access", "public", "--auth-type", "web"],
                         cwd=pkg, timeout=900)
    if published.returncode != 0:
        url = AUTH_URL_RE.search(published.stdout)
        hint = f"\nAuthorization URL npm offered: {url.group(0)}" if url else ""
        raise PublishError(f"npm publish failed (exit {published.returncode})\n"
                           f"{published.stdout}{hint}")
    _poll(lambda: _run(["npm", "view", f"brainiac-install@{version}", "version"]),
          what=f"npm view brainiac-install@{version}",
          ok=lambda p: p.returncode == 0 and version in p.stdout,
          seconds=180, every=10)
    return f"npm serves brainiac-install@{version}"


def sync_export_into_clone(export_dir: Path, clone: Path) -> None:
    """Make the clone's tree EQUAL the export tree, preserving only .git.
    Deliberately not rsync --exclude (an unanchored exclude once deleted every
    nested manifest.json on the public repo — v0.19.0 post-mortem): delete
    everything except .git, copy the export in, byte-for-byte equality."""
    for child in clone.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for child in export_dir.iterdir():
        dst = clone / child.name
        shutil.copytree(child, dst) if child.is_dir() else shutil.copy2(child, dst)


def phase_public_git(export_dir: Path, version: str, scratch: Path,
                     denylist: Path, gate_fn) -> str:
    """Fresh clone of the public repo -> tree := export -> squashed commit ->
    tag -> re-scan the final tree -> gate -> push FROM THE CLONE."""
    url_proc = _need(_run(["git", "remote", "get-url", PUBLIC_REMOTE_NAME], cwd=REPO_ROOT),
                     f"reading the public repo URL from remote {PUBLIC_REMOTE_NAME}")
    url = url_proc.stdout.strip()
    clone = scratch / "public-clone"
    _need(_run(["git", "clone", "--depth", "50", url, str(clone)], timeout=600), "clone public repo")
    default_branch = _need(_run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone),
                           "detect default branch").stdout.strip()

    sync_export_into_clone(export_dir, clone)
    # The built dist/ must not ship in the git tree (public .gitignore carries
    # dist/, but the equality sync above would re-add it if present).
    shutil.rmtree(clone / "dist", ignore_errors=True)

    _need(_run(["git", "add", "-A"], cwd=clone), "git add")
    diff = _need(_run(["git", "diff", "--cached", "--stat"], cwd=clone), "git diff --stat")
    stat_tail = "\n".join(diff.stdout.strip().splitlines()[-5:]) or "(no changes?)"
    if not diff.stdout.strip():
        raise PublishError("public clone shows no changes vs the export — is this version already published?")

    # Final scan on the EXACT tree that will be pushed (minus .git).
    terms = _load_denylist_terms(denylist)
    scanner_self_test(terms)
    with tempfile.TemporaryDirectory(prefix="pubscan-") as d:
        probe = Path(d) / "tree"
        shutil.copytree(clone, probe, ignore=shutil.ignore_patterns(".git"))
        hits = _scan_tree(probe, terms)
    if hits:
        raise PublishError(f"contamination scan found {hits} hit(s) in the final public tree — hard gate")

    _need(_run(["git", "commit", "-q", "-m", f"release: v{version}"], cwd=clone), "git commit")
    _need(_run(["git", "tag", "-a", f"v{version}", "-m", f"brainiac v{version}"], cwd=clone), "git tag")

    gate_fn("public-git", f"push v{version} to {url} ({default_branch} + tag)",
            "a public push is visible immediately and can only be superseded, "
            "not unpublished; this is the last gate",
            [f"squashed commit on {default_branch} from the verified export",
             f"final-tree contamination scan: 0 hits (self-test passed)",
             f"diffstat tail: {stat_tail}",
             "PyPI + npm phases verified for this version"])
    _need(_run(["git", "push", "origin", default_branch], cwd=clone, interactive=True,
               timeout=600), "git push branch")
    _need(_run(["git", "push", "origin", f"v{version}"], cwd=clone, interactive=True,
               timeout=600), "git push tag")
    head = _need(_run(["git", "rev-parse", "HEAD"], cwd=clone), "rev-parse").stdout.strip()
    return f"pushed {head[:9]} to {default_branch} + tag v{version}"


def build_mcpb(export_dir: Path, version: str, scratch: Path) -> Path:
    """Build the Claude Desktop `.mcpb` FROM THE EXPORT TREE, with its own
    handshake gate pointed at the just-published engine.

    `packaging/mcpb/build.sh` already validates the manifest, packs the
    bundle, and completes a real MCP `initialize`/`list_tools` against the
    shim — reuse it rather than reimplementing any of that here. All this
    adds is WHICH engine the handshake talks to: a throwaway venv holding
    `brainiac-cli[mcp]==version` straight from PyPI, first on PATH. So the
    gate proves the bundle about to ship works against the engine that just
    shipped, instead of against whatever the release operator happens to
    have installed (which, mid-release, is the PREVIOUS version).

    build.sh version-stamps `packaging/mcpb/manifest.json` in place. That is
    why this runs AFTER public-git: the pushed tree is already final, and the
    only tree mutated here is the throwaway export.
    """
    bin_dir = _throwaway_venv(scratch, "mcpb-engine")
    _poll(lambda: _run([str(bin_dir / "pip"), "install", "--quiet",
                        f"brainiac-cli[mcp]=={version}"], timeout=1200),
          what=f"pip install brainiac-cli[mcp]=={version} for the handshake gate")
    env = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])}
    script = export_dir / "packaging" / "mcpb" / "build.sh"
    if not script.exists():
        raise PublishError(f"{script} is missing from the export tree — "
                           f"packaging/mcpb is no longer exported?")
    _need(_run(["bash", str(script)], cwd=export_dir, env=env, timeout=900),
          "packaging/mcpb/build.sh (validate + pack + MCP handshake gate)")
    bundle = export_dir / "dist" / "brainiac.mcpb"
    if not bundle.exists():
        raise PublishError(f"build.sh reported success but {bundle} does not exist")
    return bundle


def phase_release_asset(export_dir: Path, version: str, scratch: Path,
                        gate_fn) -> str:
    """Attach the `.mcpb` to a GitHub release on the public repo at v<version>.

    Why this phase exists (2026-07-31): `docs/install/README.md` Path G tells
    Chat-tab users to download `brainiac.mcpb` "from a release" — and the
    public repo had no releases at all, for any version. The path dead-ended,
    and the bundle had to be hand-built and emailed one user at a time. The
    `.mcpb` is a few-KB Node stdio shim, identical for every platform, so
    there was never a build for a consumer to do.

    The published asset is downloaded back and compared by sha256 to the file
    that was uploaded: a release that exists is not evidence that the right
    bytes are on it.
    """
    bundle = build_mcpb(export_dir, version, scratch)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    size_kb = bundle.stat().st_size / 1024

    gate_fn("release-asset", f"publish GitHub release v{version} on {PUBLIC_REPO}",
            "a release is public the moment it is created; the asset can be "
            "replaced but the release itself is visible immediately",
            [f"{bundle.name} built from the export tree ({size_kb:.1f} KB)",
             "MCP handshake passed: read verb set only, no write verbs exposed",
             f"handshake ran against brainiac-cli[mcp]=={version} from PyPI",
             f"sha256 {digest[:16]}…",
             f"tag v{version} already pushed in the previous phase"])

    notes = (
        f"Brainiac v{version}.\n\n"
        f"**Engine** (needed by every surface): `uv tool install brainiac-cli` / "
        f"`pip install brainiac-cli`, or `npx brainiac-install`.\n\n"
        f"**Claude Desktop Chat tab:** download `brainiac.mcpb` below and double-click "
        f"it (or Settings → Extensions → Advanced settings → Install Extension…). It is "
        f"a small Node stdio shim that spawns the engine you already installed — it "
        f"never vendors its own copy, and exposes read-only verbs under the same "
        f"classification egress gate as the CLI. There is nothing to build: the same "
        f"file works on macOS and Windows. Full steps: `docs/install/README.md` Path G.\n\n"
        f"See `CHANGELOG.md` section `[{version}]` for what changed."
    )

    # Resume safety: a run that created the release and then died must not fail
    # here on "release already exists" — re-upload over it instead.
    existing = _run(["gh", "release", "view", f"v{version}", "--repo", PUBLIC_REPO])
    if existing.returncode == 0:
        _need(_run(["gh", "release", "upload", f"v{version}", str(bundle),
                    "--repo", PUBLIC_REPO, "--clobber"], timeout=600),
              "gh release upload")
        action = "re-uploaded the asset onto the existing release"
    else:
        _need(_run(["gh", "release", "create", f"v{version}", str(bundle),
                    "--repo", PUBLIC_REPO, "--title", f"brainiac v{version}",
                    "--notes", notes], timeout=600),
              "gh release create")
        action = "created the release"

    back = scratch / "asset-verify"
    back.mkdir(exist_ok=True)
    _poll(lambda: _run(["gh", "release", "download", f"v{version}", "--repo", PUBLIC_REPO,
                        "--pattern", "brainiac.mcpb", "--dir", str(back), "--clobber"],
                       timeout=600),
          what=f"gh release download of brainiac.mcpb from v{version}",
          seconds=180, every=10)
    served = hashlib.sha256((back / "brainiac.mcpb").read_bytes()).hexdigest()
    if served != digest:
        raise PublishError(
            f"the release serves a brainiac.mcpb whose sha256 ({served[:16]}…) differs "
            f"from the built bundle ({digest[:16]}…) — do not point users at it")
    return f"{action}; brainiac.mcpb served, sha256 verified ({digest[:16]}…)"


def phase_post_verify(version: str, scratch: Path, *, npm_published: bool = True) -> str:
    """The consumption paths a new user actually takes, from clean environments."""
    lines = [_clean_venv_check(version, [], scratch, "pypi-final")]
    proc = _run(["gh", "api", f"repos/{PUBLIC_REPO}/git/refs/tags/v{version}"])
    lines.append(f"public tag v{version}: {'visible' if proc.returncode == 0 else 'NOT VISIBLE YET'}")
    if not npm_published:
        # Verifying a channel the operator deliberately skipped would fail the
        # whole run over an act nobody performed -- and a red final phase on a
        # release whose PyPI and git legs both landed reads as "the release
        # broke". Report the gap instead; the caller already knows why.
        lines.append(f"npx brainiac-install@{version}: SKIPPED (npm not published "
                     f"in this run — that channel is still on its previous version)")
        return "\n".join(lines)
    npx = _run(["npx", "--yes", f"brainiac-install@{version}", "--dry-run"], timeout=600)
    lines.append(f"npx brainiac-install@{version} --dry-run: exit {npx.returncode}")
    if npx.returncode != 0:
        raise PublishError("npx verification failed:\n" + (npx.stdout or "") + (npx.stderr or ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="the LOCAL release tag to publish, e.g. v0.19.18")
    parser.add_argument("--denylist", required=True,
                        help="external ground-truth denylist (never committed)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every verification, stop at the first gate")
    parser.add_argument("--from", dest="from_phase", choices=PHASES, default=None,
                        help="resume from this phase (verification phases before it re-run "
                             "cheaply; upload phases are skipped only if BEFORE this one)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the full suite (recorded in evidence — only when the "
                             "same tag's suite already ran this session)")
    parser.add_argument("--accept-windows-ci", metavar="REASON", default=None,
                        help="proceed despite a missing/red distribution-matrix signal; "
                             "the reason is recorded in the evidence transcript")
    parser.add_argument("--confirm", action="append", default=[], metavar="ACT",
                        choices=["testpypi", "pypi", "npm", "public-git", "release-asset"],
                        help="pre-authorize ONE irreversible act (repeatable). For "
                             "harness-driven runs where the owner approved that "
                             "specific act in-session; requires --consent-note")
    parser.add_argument("--consent-note", default=None,
                        help="who authorized the --confirm'd acts and how (e.g. "
                             "'owner via AskUserQuestion, session 2026-07-30'); "
                             "recorded verbatim in the evidence transcript")
    args = parser.parse_args()
    if args.confirm and not args.consent_note:
        parser.error("--confirm requires --consent-note '<who authorized it and how>'")

    tag = args.tag
    version = tag[1:] if tag.startswith("v") else tag
    tag = f"v{version}"
    denylist = Path(args.denylist).expanduser()
    ev = Evidence(version)
    start_idx = PHASES.index(args.from_phase) if args.from_phase else 0

    def do(phase: str):
        return PHASES.index(phase) >= start_idx

    def gate_fn(key: str, act: str, why: str, verified: list[str]) -> None:
        provenance = gate(version, act, why, verified, dry_run=args.dry_run,
                          confirmed=key in args.confirm,
                          consent_note=args.consent_note)
        ev.record(f"gate:{key}", "authorized", provenance)

    verified: list[str] = []
    scratch = Path(tempfile.mkdtemp(prefix=f"publish-{version}-"))
    worktree = None
    try:
        print(f"publish_public: {tag} — scratch at {scratch}")

        # See phase_preflight for why this is tri-state, not a boolean.
        if not do("pypi"):
            pypi_expectation: bool | None = True    # resuming past the upload
        elif args.from_phase in ("testpypi", "pypi"):
            pypi_expectation = None                 # resuming INTO the uploads
        else:
            pypi_expectation = False                # fresh run
        summary = phase_preflight(tag, expect_published=pypi_expectation)
        ev.record("preflight", "OK", summary); verified.append(summary)
        print(f"[1/{len(PHASES)}] preflight: {summary}")

        worktree = phase_worktree(tag, scratch)
        ev.record("worktree", "OK", str(worktree)); verified.append(f"clean worktree at {tag}")
        print(f"[2/{len(PHASES)}] worktree: clean at {tag}")

        if args.skip_tests:
            ev.record("tests", "SKIPPED", "--skip-tests passed; operator asserts the suite ran for this tag")
            verified.append("tests: SKIPPED by operator flag")
            print(f"[3/{len(PHASES)}] tests: SKIPPED (--skip-tests)")
        else:
            summary = phase_tests(worktree)
            ev.record("tests", "OK", summary); verified.append(f"suite: {summary}")
            print(f"[3/{len(PHASES)}] tests: {summary}")

        export_dir = phase_export(worktree, scratch, denylist)
        ev.record("export", "OK", "0 hits; scanner self-test passed")
        verified.append("export contamination scan: 0 hits (scanner self-test passed)")
        print(f"[4/{len(PHASES)}] export + contamination gate: 0 hits (self-test passed)")

        artifacts = phase_build(export_dir, version, denylist)
        names = ", ".join(p.name for p in artifacts)
        ev.record("build", "OK", names); verified.append(f"built from export: {names}")
        print(f"[5/{len(PHASES)}] build from export: {names} (sdist re-scanned: 0 hits)")

        summary = phase_windows_ci(args.accept_windows_ci)
        ev.record("windows-ci", "OK", summary); verified.append(f"windows CI: {summary}")
        print(f"[6/{len(PHASES)}] windows CI signal: {summary}")

        if do("testpypi"):
            gate_fn("testpypi", "upload to TestPyPI",
                    "uploads are permanent per version — a bad artifact burns the number "
                    "even on the test index",
                    verified)
            summary = phase_testpypi(artifacts, version, scratch)
            ev.record("testpypi", "OK", summary); verified.append(f"testpypi: {summary}")
            print(f"[7/{len(PHASES)}] testpypi: {summary}")
        else:
            print(f"[7/{len(PHASES)}] testpypi: skipped (--from {args.from_phase})")

        if do("pypi"):
            gate_fn("pypi", "upload to PRODUCTION PyPI",
                    "this instantly becomes what every `pip install brainiac-cli` gets; "
                    "it can be yanked but never unpublished",
                    verified)
            summary = phase_pypi(artifacts, version, scratch)
            ev.record("pypi", "OK", summary); verified.append(f"pypi: {summary}")
            print(f"[8/{len(PHASES)}] pypi: {summary}")
        else:
            print(f"[8/{len(PHASES)}] pypi: skipped (--from {args.from_phase})")

        if do("npm"):
            summary = phase_npm(export_dir, version, scratch, gate_fn)
            ev.record("npm", "OK", summary); verified.append(f"npm: {summary}")
            print(f"[9/{len(PHASES)}] npm: {summary}")
        else:
            print(f"[9/{len(PHASES)}] npm: skipped (--from {args.from_phase})")

        if do("public-git"):
            summary = phase_public_git(export_dir, version, scratch, denylist, gate_fn)
            ev.record("public-git", "OK", summary); verified.append(summary)
            print(f"[10/{len(PHASES)}] public git: {summary}")
        else:
            print(f"[10/{len(PHASES)}] public git: skipped (--from {args.from_phase})")

        if do("release-asset"):
            summary = phase_release_asset(export_dir, version, scratch, gate_fn)
            ev.record("release-asset", "OK", summary); verified.append(summary)
            print(f"[11/{len(PHASES)}] release asset: {summary}")
        else:
            print(f"[11/{len(PHASES)}] release asset: skipped (--from {args.from_phase})")

        # A resume that started past the npm phase never published it, so the
        # npx path cannot exist for this version yet.
        summary = phase_post_verify(version, scratch, npm_published=do("npm"))
        ev.record("post-verify", "OK", summary)
        print(f"[12/{len(PHASES)}] post-verify:\n{summary}")

        ev.record("DONE", "OK", f"v{version} fully published and verified")
        print(f"\nDONE — transcript: {ev.path}")
        return 0

    except GateDeclined as exc:
        ev.record("ABORTED", "operator", str(exc))
        print(f"\nStopped: {exc}\nNothing after this point was uploaded or pushed. "
              f"Transcript: {ev.path}")
        return 2
    except PublishError as exc:
        ev.record("FAILED", "gate", str(exc))
        print(f"\nFAILED: {exc}\nTranscript: {ev.path}", file=sys.stderr)
        return 1
    finally:
        if worktree is not None:
            _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
