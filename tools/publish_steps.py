"""Orchestrate public release phases."""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from tools import publish_public as _source
from tools.publish_public import (
    Evidence,
    GateDeclined,
    PHASES,
    PublishError,
    REPO_ROOT,
    _run,
    gate,
    phase_build,
    phase_export,
    phase_local_deploy,
    phase_npm,
    phase_post_verify,
    phase_preflight,
    phase_public_git,
    phase_pypi,
    phase_release_asset,
    phase_testpypi,
    phase_tests,
    phase_windows_ci,
    phase_worktree,
)

__doc__ = _source.__doc__


def _make_gate_fn(
    args: argparse.Namespace, version: str, ev: Evidence
) -> Callable[[str, str, str, list[str]], None]:
    def gate_fn(key: str, act: str, why: str, verified: list[str]) -> None:
        provenance = gate(version, act, why, verified, dry_run=args.dry_run,
                          confirmed=key in args.confirm,
                          consent_note=args.consent_note)
        ev.record(f"gate:{key}", "authorized", provenance)

    return gate_fn


def _run_initial_phases(
    args: argparse.Namespace,
    tag: str,
    denylist: Path,
    ev: Evidence,
    should_run: Callable[[str], bool],
    verified: list[str],
    scratch: Path,
) -> tuple[Path, Path, list[Path]]:
    if not should_run("pypi"):
        pypi_expectation: bool | None = True
    elif args.from_phase in ("testpypi", "pypi"):
        pypi_expectation = None
    else:
        pypi_expectation = False
    summary = phase_preflight(tag, expect_published=pypi_expectation)
    ev.record("preflight", "OK", summary)
    verified.append(summary)
    print(f"[1/{len(PHASES)}] preflight: {summary}")

    worktree = phase_worktree(tag, scratch)
    ev.record("worktree", "OK", str(worktree))
    verified.append(f"clean worktree at {tag}")
    print(f"[2/{len(PHASES)}] worktree: clean at {tag}")

    if args.skip_tests:
        ev.record("tests", "SKIPPED",
                  "--skip-tests passed; operator asserts the suite ran for this tag")
        verified.append("tests: SKIPPED by operator flag")
        print(f"[3/{len(PHASES)}] tests: SKIPPED (--skip-tests)")
    else:
        summary = phase_tests(worktree)
        ev.record("tests", "OK", summary)
        verified.append(f"suite: {summary}")
        print(f"[3/{len(PHASES)}] tests: {summary}")

    export_dir = phase_export(worktree, scratch, denylist)
    ev.record("export", "OK", "0 hits; scanner self-test passed")
    verified.append("export contamination scan: 0 hits (scanner self-test passed)")
    print(f"[4/{len(PHASES)}] export + contamination gate: 0 hits (self-test passed)")

    artifacts = phase_build(export_dir, tag.removeprefix("v"), denylist)
    names = ", ".join(path.name for path in artifacts)
    ev.record("build", "OK", names)
    verified.append(f"built from export: {names}")
    print(f"[5/{len(PHASES)}] build from export: {names} (sdist re-scanned: 0 hits)")

    summary = phase_windows_ci(args.accept_windows_ci)
    ev.record("windows-ci", "OK", summary)
    verified.append(f"windows CI: {summary}")
    print(f"[6/{len(PHASES)}] windows CI signal: {summary}")
    return worktree, export_dir, artifacts


def _run_upload_phases(
    args: argparse.Namespace,
    version: str,
    scratch: Path,
    ev: Evidence,
    should_run: Callable[[str], bool],
    verified: list[str],
    gate_fn: Callable[[str, str, str, list[str]], None],
    export_dir: Path,
    artifacts: list[Path],
    denylist: Path,
) -> None:
    if should_run("testpypi"):
        gate_fn("testpypi", "upload to TestPyPI",
                "uploads are permanent per version — a bad artifact burns the number "
                "even on the test index", verified)
        summary = phase_testpypi(artifacts, version, scratch)
        ev.record("testpypi", "OK", summary)
        verified.append(f"testpypi: {summary}")
        print(f"[7/{len(PHASES)}] testpypi: {summary}")
    else:
        print(f"[7/{len(PHASES)}] testpypi: skipped (--from {args.from_phase})")

    if should_run("pypi"):
        gate_fn("pypi", "upload to PRODUCTION PyPI",
                "this instantly becomes what every `pip install brainiac-cli` gets; "
                "it can be yanked but never unpublished", verified)
        summary = phase_pypi(artifacts, version, scratch)
        ev.record("pypi", "OK", summary)
        verified.append(f"pypi: {summary}")
        print(f"[8/{len(PHASES)}] pypi: {summary}")
    else:
        print(f"[8/{len(PHASES)}] pypi: skipped (--from {args.from_phase})")

    if should_run("npm"):
        summary = phase_npm(export_dir, version, scratch, gate_fn)
        ev.record("npm", "OK", summary)
        verified.append(f"npm: {summary}")
        print(f"[9/{len(PHASES)}] npm: {summary}")
    else:
        print(f"[9/{len(PHASES)}] npm: skipped (--from {args.from_phase})")

    if should_run("public-git"):
        summary = phase_public_git(export_dir, version, scratch, denylist, gate_fn)
        ev.record("public-git", "OK", summary)
        verified.append(summary)
        print(f"[10/{len(PHASES)}] public git: {summary}")
    else:
        print(f"[10/{len(PHASES)}] public git: skipped (--from {args.from_phase})")

    if should_run("release-asset"):
        summary = phase_release_asset(export_dir, version, scratch, gate_fn)
        ev.record("release-asset", "OK", summary)
        verified.append(summary)
        print(f"[11/{len(PHASES)}] release asset: {summary}")
    else:
        print(f"[11/{len(PHASES)}] release asset: skipped (--from {args.from_phase})")


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

    verified: list[str] = []
    scratch = Path(tempfile.mkdtemp(prefix=f"publish-{version}-"))
    worktree = None
    gate_fn = _make_gate_fn(args, version, ev)
    def should_run(phase: str) -> bool:
        return PHASES.index(phase) >= start_idx
    try:
        print(f"publish_public: {tag} — scratch at {scratch}")
        worktree, export_dir, artifacts = _run_initial_phases(
            args, tag, denylist, ev, should_run, verified, scratch
        )
        _run_upload_phases(
            args, version, scratch, ev, should_run, verified, gate_fn,
            export_dir, artifacts, denylist
        )

        # A resume that started past the npm phase never published it, so the
        # npx path cannot exist for this version yet.
        summary = phase_post_verify(version, scratch, npm_published=should_run("npm"))
        ev.record("post-verify", "OK", summary)
        print(f"[12/{len(PHASES)}] post-verify:\n{summary}")

        # Publishing is not deploying: without this, the release is live on
        # every public channel while THIS host keeps staging the previous
        # engine into Cowork (measured: 0.20.19 survived two releases).
        summary = phase_local_deploy(version)
        ev.record("deploy", "OK", summary)
        print(f"[13/{len(PHASES)}] deploy:\n{summary}")

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
