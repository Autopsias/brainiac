"""The publish phase family of `publish_public` — index uploads, clean-venv verification, public git, release asset (batch-2 drain).

Moved verbatim out of `publish_public`; every parent-surface collaborator
(`_run`, `_need`, `_poll`, `_throwaway_venv`, `_clean_venv_check`,
`build_mcpb`, `NPM_PUBLISH_WAIT_SECONDS`, `PublishError`, …) resolves through
the parent module at CALL time, so a test that monkeypatches one on
`publish_public` keeps governing this code exactly as before.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
import re as _re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import publish_public as _pp  # noqa: E402


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
            raise _pp.PublishError(
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
        _pp._need(_pp._run([sys.executable, "-m", "venv", str(venv)]), "venv create")
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
    if deps_from is None and any(_pp._non_pypi_index(a) for a in index_args):
        raise _pp.PublishError(
            f"{label}: refusing to resolve dependencies against a non-PyPI "
            "index; pass deps_from=<locally built wheel>")
    bin_dir = _pp._throwaway_venv(scratch, label)
    pip = bin_dir / "pip"
    brain = bin_dir / "brain"
    install_args = list(index_args)
    if deps_from is not None:
        _pp._poll(lambda: _pp._run([str(pip), "install", "--quiet",
                            "--index-url", "https://pypi.org/simple/",
                            str(deps_from)], timeout=1200),
              what=f"dependencies from PyPI only (for {label})")
        install_args = ["--no-deps", "--force-reinstall", *index_args]
    _pp._poll(lambda: _pp._run([str(pip), "install", "--quiet", *install_args,
                        f"brainiac-cli=={version}"], timeout=1200),
          what=f"pip install from {label}")
    out = _pp._need(_pp._run([str(brain), "--version"]), "brain --version").stdout.strip()
    if version not in out:
        raise _pp.PublishError(f"{label} artifact prints {out!r}, expected {version}")
    return f"installed from {label}; brain --version -> {out}"


def phase_testpypi(artifacts: list[Path], version: str, scratch: Path) -> str:
    """Upload to TestPyPI (operator's own twine auth), verify from a clean venv."""
    # --skip-existing makes a resume safe: an upload that succeeded but whose
    # verification lagged must not turn into a hard "file already exists" on the
    # retry (an index version is permanent; re-uploading is not an option).
    proc = _pp._run([sys.executable, "-m", "twine", "upload", "--skip-existing",
                 "--repository", "testpypi", *[str(p) for p in artifacts]],
                interactive=True)
    _pp._need(proc, "twine upload to testpypi")
    wheels = [p for p in artifacts if p.suffix == ".whl"]
    if not wheels:
        raise _pp.PublishError("testpypi verification needs the locally built wheel "
                           "to install dependencies from PyPI only")
    return _pp._clean_venv_check(
        version,
        ["--index-url", "https://test.pypi.org/simple/"],
        scratch, "testpypi", deps_from=wheels[0])


def phase_pypi(artifacts: list[Path], version: str, scratch: Path) -> str:
    """Upload to production PyPI, then verify the served artifact is
    byte-identical to what was built (sha256 over pip download)."""
    proc = _pp._run([sys.executable, "-m", "twine", "upload", "--skip-existing",
                 *[str(p) for p in artifacts]], interactive=True)
    _pp._need(proc, "twine upload to pypi")
    dl = scratch / "pypi-download"
    dl.mkdir(exist_ok=True)
    pip = _pp._throwaway_venv(scratch, "pypi-verify") / "pip"
    _pp._poll(lambda: _pp._run([str(pip), "download", "--no-deps", "-d", str(dl),
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
        diff = _pp._archive_content_diff(mine, p)
        if diff:
            raise _pp.PublishError(
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
    a, b = _pp._archive_members(built), _pp._archive_members(served)
    return sorted(set(a) ^ set(b)) + sorted(k for k in set(a) & set(b) if a[k] != b[k])


def npm_pack_smoke(export_dir: Path, version: str, scratch: Path) -> str:
    """Pack the npx bootstrap from the EXPORT tree and run it, WITHOUT publishing.

    The publish itself belongs to CI (see `phase_post_verify`), but the tag
    push that triggers it is irreversible, so what shipped has to be proven
    good BEFORE that push rather than after. This is the half of the old
    `phase_npm` that was actually verification: pack the exact tarball the
    workflow will publish, install it into a throwaway prefix, and run
    `--dry-run` through it. A broken bootstrap fails here, while the tag is
    still local and nothing is public.
    """
    pkg = export_dir / "packaging" / "npm" / "brainiac-install"
    pack_dir = scratch / "npm-pack"
    pack_dir.mkdir(exist_ok=True)
    _pp._need(_pp._run(["npm", "pack", "--pack-destination", str(pack_dir)], cwd=pkg), "npm pack")
    tarballs = list(pack_dir.glob(f"brainiac-install-{version}.tgz"))
    if not tarballs:
        raise _pp.PublishError(f"npm pack produced no brainiac-install-{version}.tgz — version skew")
    prefix = pack_dir / "install-prefix"
    _pp._need(_pp._run(["npm", "install", "-g", "--prefix", str(prefix),
                "--install-strategy=hoisted", str(tarballs[0])], cwd=pack_dir, timeout=600),
          "npm install of packed tarball")
    _pp._need(_pp._run([str(prefix / "bin" / "brainiac-install"), "--dry-run"]),
          "brainiac-install --dry-run smoke")
    return f"packed brainiac-install-{version}.tgz and ran --dry-run through it (exit 0)"


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
    url_proc = _pp._need(_pp._run(["git", "remote", "get-url", _pp.PUBLIC_REMOTE_NAME], cwd=_pp.REPO_ROOT),
                     f"reading the public repo URL from remote {_pp.PUBLIC_REMOTE_NAME}")
    url = url_proc.stdout.strip()
    clone = scratch / "public-clone"
    _pp._need(_pp._run(["git", "clone", "--depth", "50", url, str(clone)], timeout=600), "clone public repo")
    default_branch = _pp._need(_pp._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone),
                           "detect default branch").stdout.strip()

    _pp.sync_export_into_clone(export_dir, clone)
    # The built dist/ must not ship in the git tree (public .gitignore carries
    # dist/, but the equality sync above would re-add it if present).
    shutil.rmtree(clone / "dist", ignore_errors=True)

    _pp._need(_pp._run(["git", "add", "-A"], cwd=clone), "git add")
    diff = _pp._need(_pp._run(["git", "diff", "--cached", "--stat"], cwd=clone), "git diff --stat")
    stat_tail = "\n".join(diff.stdout.strip().splitlines()[-5:]) or "(no changes?)"
    if not diff.stdout.strip():
        raise _pp.PublishError("public clone shows no changes vs the export — is this version already published?")

    # Final scan on the EXACT tree that will be pushed (minus .git).
    terms = _pp._load_denylist_terms(denylist)
    _pp.scanner_self_test(terms)
    with tempfile.TemporaryDirectory(prefix="pubscan-") as d:
        probe = Path(d) / "tree"
        shutil.copytree(clone, probe, ignore=shutil.ignore_patterns(".git"))
        hits = _pp._scan_tree(probe, terms)
    if hits:
        raise _pp.PublishError(f"contamination scan found {hits} hit(s) in the final public tree — hard gate")

    _pp._need(_pp._run(["git", "commit", "-q", "-m", f"release: v{version}"], cwd=clone), "git commit")
    _pp._need(_pp._run(["git", "tag", "-a", f"v{version}", "-m", f"brainiac v{version}"], cwd=clone), "git tag")

    gate_fn("public-git", f"push v{version} to {url} ({default_branch} + tag)",
            "a public push is visible immediately and can only be superseded, "
            "not unpublished — and pushing THIS TAG also publishes "
            f"brainiac-install@{version} to npm, which is permanent per "
            "version; this is the last gate",
            [f"squashed commit on {default_branch} from the verified export",
             "final-tree contamination scan: 0 hits (self-test passed)",
             f"diffstat tail: {stat_tail}",
             "PyPI publish verified for this version",
             "npm tarball packed from this export and smoke-tested; the tag "
             "push fires npm-publish.yml, which publishes it over OIDC with "
             "a provenance attestation"])
    _pp._need(_pp._run(["git", "push", "origin", default_branch], cwd=clone, interactive=True,
               timeout=600), "git push branch")
    _pp._need(_pp._run(["git", "push", "origin", f"v{version}"], cwd=clone, interactive=True,
               timeout=600), "git push tag")
    head = _pp._need(_pp._run(["git", "rev-parse", "HEAD"], cwd=clone), "rev-parse").stdout.strip()
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
    bin_dir = _pp._throwaway_venv(scratch, "mcpb-engine")
    _pp._poll(lambda: _pp._run([str(bin_dir / "pip"), "install", "--quiet",
                        f"brainiac-cli[mcp]=={version}"], timeout=1200),
          what=f"pip install brainiac-cli[mcp]=={version} for the handshake gate")
    env = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])}
    script = export_dir / "packaging" / "mcpb" / "build.sh"
    if not script.exists():
        raise _pp.PublishError(f"{script} is missing from the export tree — "
                           f"packaging/mcpb is no longer exported?")
    _pp._need(_pp._run(["bash", str(script)], cwd=export_dir, env=env, timeout=900),
          "packaging/mcpb/build.sh (validate + pack + MCP handshake gate)")
    bundle = export_dir / "dist" / "brainiac.mcpb"
    if not bundle.exists():
        raise _pp.PublishError(f"build.sh reported success but {bundle} does not exist")
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
    bundle = _pp.build_mcpb(export_dir, version, scratch)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    size_kb = bundle.stat().st_size / 1024

    gate_fn("release-asset", f"publish GitHub release v{version} on {_pp.PUBLIC_REPO}",
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

    def _upload_onto_existing() -> str:
        _pp._need(_pp._run(["gh", "release", "upload", f"v{version}", str(bundle),
                    "--repo", _pp.PUBLIC_REPO, "--clobber"], timeout=600),
              "gh release upload")
        return "re-uploaded the asset onto the existing release"

    # Resume safety: a run that created the release and then died must not fail
    # here on "release already exists" — re-upload over it instead.
    existing = _pp._run(["gh", "release", "view", f"v{version}", "--repo", _pp.PUBLIC_REPO])
    if existing.returncode == 0:
        action = _upload_onto_existing()
    else:
        created = _pp._run(["gh", "release", "create", f"v{version}", str(bundle),
                    "--repo", _pp.PUBLIC_REPO, "--title", f"brainiac v{version}",
                    "--notes", notes], timeout=600)
        if created.returncode == 0:
            action = "created the release"
        elif "already exists" in (created.stdout or "") + (created.stderr or ""):
            # The check above is a SNAPSHOT and this phase now races a robot.
            # Since 2026-08-20 the previous phase's tag push fires
            # npm-publish.yml, whose `github-release` job creates the Release
            # itself — measured on v0.20.23: CI created it at 13:21:57, the
            # view returned "not found" before that, and the create then died
            # `HTTP 422 Release.tag_name already exists`, failing a release
            # whose PyPI, git and npm legs had all landed. Re-check by DOING
            # the other branch rather than trusting the stale snapshot.
            action = _upload_onto_existing() + " (created by CI while this phase ran)"
        else:
            raise _pp.PublishError(
                f"gh release create failed (exit {created.returncode})\n"
                f"{created.stdout}{created.stderr}")

    back = scratch / "asset-verify"
    back.mkdir(exist_ok=True)
    _pp._poll(lambda: _pp._run(["gh", "release", "download", f"v{version}", "--repo", _pp.PUBLIC_REPO,
                        "--pattern", "brainiac.mcpb", "--dir", str(back), "--clobber"],
                       timeout=600),
          what=f"gh release download of brainiac.mcpb from v{version}",
          seconds=180, every=10)
    served = hashlib.sha256((back / "brainiac.mcpb").read_bytes()).hexdigest()
    if served != digest:
        raise _pp.PublishError(
            f"the release serves a brainiac.mcpb whose sha256 ({served[:16]}…) differs "
            f"from the built bundle ({digest[:16]}…) — do not point users at it")
    return f"{action}; brainiac.mcpb served, sha256 verified ({digest[:16]}…)"


def phase_post_verify(version: str, scratch: Path) -> str:
    """The consumption paths a new user actually takes, from clean environments.

    npm is published by `npm-publish.yml` on the tag push, not by this script,
    so this phase WAITS for the registry rather than assuming it. The wait is
    keyed on the public tag: no tag means the workflow was never triggered and
    npm cannot be expected, so the gap is reported instead of failing a
    release whose PyPI and git legs both landed.
    """
    lines = [_pp._clean_venv_check(version, [], scratch, "pypi-final")]
    tag_visible = _pp._run(
        ["gh", "api", f"repos/{_pp.PUBLIC_REPO}/git/refs/tags/v{version}"]).returncode == 0
    lines.append(f"public tag v{version}: {'visible' if tag_visible else 'NOT VISIBLE YET'}")
    if not tag_visible:
        lines.append(f"npm brainiac-install@{version}: NOT EXPECTED YET (the tag that "
                     f"triggers npm-publish.yml is not on the public repo, so that "
                     f"channel is still on its previous version)")
        return "\n".join(lines)

    # ~25s of workflow plus the registry's own read-after-write lag. A poll, not
    # a sleep: a run that publishes fast must not pay the worst case.
    try:
        _pp._poll(lambda: _pp._run(["npm", "view", f"brainiac-install@{version}", "version"]),
                  what=f"npm view brainiac-install@{version} (published by npm-publish.yml "
                       f"on the tag push)",
                  ok=lambda p: p.returncode == 0 and version in p.stdout,
                  seconds=_pp.NPM_PUBLISH_WAIT_SECONDS, every=15)
    except _pp.PublishError as exc:
        raise _pp.PublishError(
            f"{exc}\n\nPyPI and the public repo both landed; only npm is missing. "
            f"Read the workflow run, then re-fire it — neither needs this pipeline:\n"
            f"    gh run list --repo {_pp.PUBLIC_REPO} --workflow npm-publish.yml --limit 3\n"
            f"    gh workflow run npm-publish.yml --repo {_pp.PUBLIC_REPO} -f tag=v{version}"
        ) from exc
    lines.append(f"npm serves brainiac-install@{version} (published by npm-publish.yml)")

    npx = _pp._run(["npx", "--yes", f"brainiac-install@{version}", "--dry-run"], timeout=600)
    lines.append(f"npx brainiac-install@{version} --dry-run: exit {npx.returncode}")
    if npx.returncode != 0:
        raise _pp.PublishError("npx verification failed:\n" + (npx.stdout or "") + (npx.stderr or ""))
    return "\n".join(lines)
