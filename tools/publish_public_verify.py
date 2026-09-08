"""Proving that the index is serving OUR code, after CI published it.

Split out of `publish_public_uploads.py` on 2026-08-25, when the PyPI upload
moved to `.github/workflows/pypi-publish.yml` and this file grew past the size
ratchet. The seam is real rather than convenient: everything here answers one
question — is the artifact PyPI hands a user the artifact this machine built
from this tag — and none of it uploads anything.

Resolution goes through `publish_public` (`_pp`) exactly as the sibling upload
module does, so there is one place that owns the shared helpers.
"""
from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path

import tools.publish_public as _pp


def wait_for_pypi(version: str, scratch: Path) -> str:
    """Block until PyPI serves `version`, published by the tag push.

    Called TWICE, deliberately: once immediately after `phase_public_git`, so
    that everything downstream may assume the index is live, and again from
    `phase_post_verify` for a resumed run that skipped the first. It is cheap
    when the version is already served — one `pip index` call — and the whole
    point is that no later phase has to think about the race.

    That first call is not optional. `phase_release_asset` pip-installs
    `brainiac-cli[mcp]==version` straight from PyPI for its MCP handshake gate,
    and it runs right after the push. While this pipeline did the upload itself,
    PyPI was already live by then; now the tag push is what publishes, so
    without this wait the handshake races a workflow that has to BUILD first —
    two jobs and an artifact handoff — against `_poll`'s 300s default.
    """
    # NOT `sys.executable -m pip`: under `uv run` the launching interpreter has
    # no pip, and that failure once repeated for five minutes AFTER a good
    # upload. Every pip call comes from a throwaway venv, which ensurepip fills.
    pip = _pp._throwaway_venv(scratch, "pypi-wait") / "pip"
    try:
        _pp._poll(lambda: _pp._run([str(pip), "index", "versions",
                                    "brainiac-cli", "--disable-pip-version-check"]),
                  what="pip index versions brainiac-cli (published by pypi-publish.yml "
                       "on the tag push)",
                  ok=lambda p: p.returncode == 0 and version in (p.stdout or ""),
                  seconds=_pp.PYPI_PUBLISH_WAIT_SECONDS, every=20)
    except _pp.PublishError as exc:
        raise _pp.PublishError(
            f"{exc}\n\nThe public repo and its tag landed; PyPI is missing. A failed "
            f"upload publishes NOTHING, so no version is burned — read the run, then "
            f"re-fire it with the tag. Neither needs this pipeline:\n"
            f"    gh run list --repo {_pp.PUBLIC_REPO} --workflow pypi-publish.yml --limit 3\n"
            f"    gh workflow run pypi-publish.yml --repo {_pp.PUBLIC_REPO} "
            f"-f target=pypi -f ref=v{version}\n"
            f"If the run failed at the upload step itself, the trusted-publisher form and "
            f"the workflow filename disagree: pypi.org -> brainiac-cli -> Manage -> "
            f"Publishing must name pypi-publish.yml and environment `pypi`."
        ) from exc
    return f"PyPI serves brainiac-cli=={version} (published by pypi-publish.yml)"


def verify_served_artifacts(artifacts: list[Path], version: str, scratch: Path) -> str:
    """Prove PyPI is serving OUR code, member by member.

    This is the half of the old `phase_pypi` that was verification. The upload
    half went to CI on 2026-08-25 (`.github/workflows/pypi-publish.yml`, fired
    by the tag push), for the same reason npm's did a week earlier -- plus one
    that is specific to PyPI: SLSA defines a laptop build as Build L0, and PyPI
    accepts a PEP 740 attestation ONLY from a Trusted Publisher, so a local
    twine upload could never carry provenance no matter how well guarded.

    The comparison below is what makes that move safe to verify. It was already
    written to survive a rebuild -- see the container-bytes note further down,
    from the 0.19.19 post-mortem -- and a CI build IS a rebuild. So the property
    it asserts is unchanged: whatever PyPI serves must contain the same code as
    what this machine built from the same tag.
    """
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


