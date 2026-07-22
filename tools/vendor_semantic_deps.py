"""Stage the offline semantic-search deps the Cowork device VM can't pip-install
(DV-04, 2026-07-09).

The device VM (pinned Python ``VM_PYTHON`` = 3.10, no outbound network, base
image ships NONE of the compiled deps) needs ``onnxruntime`` + ``numpy``
(inference), ``tokenizers`` (query tokenisation) and ``sqlite-vec`` (vec0 ANN).
So the HOST (which does have network) downloads the per-arch cp310/abi3 wheels
and unpacks them into ``<brain_dir>/vendor/<arch>/``; the shim puts the
engine FIRST on ``PYTHONPATH`` with ``vendor/<arch>`` after it, and the VM
imports the third-party deps locally — no pip, no egress.

Supply-chain hardening (codex 2026-07-19/20): a vendored wheel is untrusted
input. The engine is placed BEFORE the vendor dir on ``PYTHONPATH``; every wheel
is locked by exact version + filename + PyPI SHA256, every member is checked
against wheel RECORD, extraction is restricted to the package's expected
top-level roots, and the complete generation is validated before it replaces a
working one. Zip-slip and engine/auto-exec shadows are refused as well.

This module is the SINGLE source of that logic, shared by
``tools/cowork_workspace_install.sh`` (fresh install) and ``brain update``'s
workspace re-stage (``src/brain/update.py``) so the two never drift — the exact
class of bug that left ``brain update`` shipping a stale artifact before.

``tokenizers`` ships an ``abi3`` wheel (one build works across py3.x); ``sqlite-vec``
ships a ``py3-none`` wheel. Both are pinned to the linux manylinux2014 target the
Cowork VM runs. Pure stdlib — safe to import from anywhere.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ARCHS = ("aarch64", "x86_64")

PINNED_VERSIONS = {
    "tokenizers": "0.23.1",
    "sqlite-vec": "0.1.9",
    "onnxruntime": "1.23.2",
    "numpy": "2.2.6",
    "coloredlogs": "15.0.1",
    "flatbuffers": "25.12.19",
    "packaging": "26.2",
    "protobuf": "7.35.1",
    "sympy": "1.14.0",
    "humanfriendly": "10.0",
    "mpmath": "1.3.0",
}

# Exact wheel lock, verified against PyPI's published SHA256 digests on
# 2026-07-20. The filename is part of the lock: an index may not substitute a
# different build with the same version. Values are (distribution, sha256).
_PURE_WHEELS = {
    "coloredlogs-15.0.1-py2.py3-none-any.whl": (
        "coloredlogs", "612ee75c546f53e92e70049c9dbfcc18c935a2b9a53b66085ce9ef6a6e5c0934"),
    "flatbuffers-25.12.19-py2.py3-none-any.whl": (
        "flatbuffers", "7634f50c427838bb021c2d66a3d1168e9d199b0607e6329399f04846d42e20b4"),
    "humanfriendly-10.0-py2.py3-none-any.whl": (
        "humanfriendly", "1697e1a8a8f550fd43c2865cd84542fc175a61dcb779b6fee18cf6b6ccba1477"),
    "mpmath-1.3.0-py3-none-any.whl": (
        "mpmath", "a0b2b9fe80bbcd81a6647ff13108738cfb482d481d826cc0e02f5b35e5c88d2c"),
    "packaging-26.2-py3-none-any.whl": (
        "packaging", "5fc45236b9446107ff2415ce77c807cee2862cb6fac22b8a73826d0693b0980e"),
    "sympy-1.14.0-py3-none-any.whl": (
        "sympy", "e091cc3e99d2141a0ba2847328f5479b05d94a6635cb96148ccb3f34671bd8f5"),
}

TRUSTED_WHEELS = {
    "aarch64": {
        **_PURE_WHEELS,
        "numpy-2.2.6-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl": (
            "numpy", "efd28d4e9cd7d7a8d39074a4d44c63eda73401580c5c76acda2ce969e0a38e83"),
        "onnxruntime-1.23.2-cp310-cp310-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl": (
            "onnxruntime", "8f7d1fe034090a1e371b7f3ca9d3ccae2fabae8c1d8844fb7371d1ea38e8e8d2"),
        "protobuf-7.35.1-cp310-abi3-manylinux2014_aarch64.whl": (
            "protobuf", "11d6b0ec246892d85215b0a13ca6e0233cf5284b68f0ac02646427f4ff88a799"),
        "sqlite_vec-0.1.9-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl": (
            "sqlite-vec", "4e921e592f24a5f9a18f590b6ddd530eb637e2d474e3b1972f9bbeb773aa3cb9"),
        "tokenizers-0.23.1-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl": (
            "tokenizers", "1bf13402aff9bc533c89cb849ec3b412dc3fbeacc9744840e423d7bf3f7dc0e3"),  # gitleaks:allow -- public PyPI SHA256
    },
    "x86_64": {
        **_PURE_WHEELS,
        "numpy-2.2.6-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl": (
            "numpy", "fc7b73d02efb0e18c000e9ad8b83480dfcd5dfd11065997ed4c6747470ae8915"),
        "onnxruntime-1.23.2-cp310-cp310-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl": (
            "onnxruntime", "4ca88747e708e5c67337b0f65eed4b7d0dd70d22ac332038c9fc4635760018f7"),
        "protobuf-7.35.1-cp310-abi3-manylinux2014_x86_64.whl": (
            "protobuf", "74758715c53d7158fb76caf4f0cfdacc5329a4b1bb994f865d6cf302d413a1c4"),
        "sqlite_vec-0.1.9-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.manylinux1_x86_64.whl": (
            "sqlite-vec", "1515727990b49e79bcaf75fdee2ffc7d461f8b66905013231251f1c8938e7786"),
        "tokenizers-0.23.1-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl": (
            "tokenizers", "5075b405006415ea148a992d093699c66eb01952bf59f4d5727089a98bda45a4"),  # gitleaks:allow -- public PyPI SHA256
    },
}

_PACKAGE_ROOTS = {
    "tokenizers": {"tokenizers"},
    "sqlite-vec": {"sqlite_vec"},
    "onnxruntime": {"onnxruntime"},
    "numpy": {"numpy", "numpy.libs"},
    "coloredlogs": {"coloredlogs", "coloredlogs.pth"},
    "flatbuffers": {"flatbuffers"},
    "packaging": {"packaging"},
    "protobuf": {"google"},
    "sympy": {"sympy", "isympy.py", "sympy-1.14.0.data"},
    "humanfriendly": {"humanfriendly"},
    "mpmath": {"mpmath"},
}

# THE single pinned VM interpreter version. The Cowork Linux VM runs Python
# 3.10 ONLY (field finding 2026-07-18: cp311 wheels staged for a 3.10 VM caused
# a 10-run EmbedderUnavailable outage). Every pip-download job below derives
# its --python-version from this, and check_wheel_tag() refuses any wheel whose
# tag the pinned interpreter cannot import. Keep in lockstep with
# src/brain/doctor.py's _VM_PYTHON.
VM_PYTHON = "3.10"
_VM_PY_NODOT = VM_PYTHON.replace(".", "")  # "310"

# The zero-install shim, with the engine ahead of the vendored deps on
# PYTHONPATH (codex 2026-07-19 — an untrusted wheel must not shadow the engine).
# Kept here so the installer and the updater write the identical file.
SHIM_CONTENT = """#!/bin/sh
# zero-install shim: run the staged brain engine from source, with the ENGINE
# first on the path and the vendored semantic deps (tokenizers/sqlite-vec)
# AFTER it, so real query embedding works offline in the VM (DV-04) while an
# untrusted vendored wheel cannot shadow the brain engine (codex 2026-07-19).
DIR="$(cd "$(dirname "$0")" && pwd)"
ARCH="$(uname -m)"
PYTHONPATH="$DIR/engine:$DIR/vendor/$ARCH${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m brain.cli "$@"
"""


# Top-level names an untrusted wheel must never be allowed to drop into the
# vendor dir: the engine package itself, and the two modules CPython auto-imports
# at startup (a wheel placing `sitecustomize.py`/`usercustomize.py` on the path
# runs arbitrary code with no import statement). Engine-first PYTHONPATH already
# stops `brain` shadowing; this is belt-and-braces + covers the auto-exec pair.
_FORBIDDEN_TOPLEVEL = frozenset({
    "brain", "sitecustomize", "usercustomize", "sitecustomize.py", "usercustomize.py",
})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_distribution(wheel: Path, arch: str) -> str:
    """Return the locked distribution for ``wheel`` after checking its exact
    filename and SHA256. Hash validation happens before opening the ZIP."""
    locked = TRUSTED_WHEELS.get(arch)
    if locked is None:
        raise ValueError(f"unsupported vendor architecture {arch!r}")
    metadata = locked.get(wheel.name)
    if metadata is None:
        raise ValueError(f"wheel {wheel.name!r} is absent from the trusted {arch} lock")
    distribution, expected_hash = metadata
    actual_hash = _sha256_file(wheel)
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise ValueError(
            f"wheel {wheel.name!r} SHA256 mismatch: expected {expected_hash}, got {actual_hash}"
        )
    return distribution


def _dist_info_dir(distribution: str) -> str:
    return f"{distribution.replace('-', '_')}-{PINNED_VERSIONS[distribution]}.dist-info"


def _verify_record(zf: zipfile.ZipFile, *, distribution: str) -> None:
    """Verify every extracted file against the wheel's signed-by-hash RECORD.

    RECORD does not establish publisher identity—the outer trusted SHA256 lock
    does that—but it independently detects corrupt members and prevents files
    omitted from the wheel manifest from reaching ``vendor/``.
    """
    record_name = f"{_dist_info_dir(distribution)}/RECORD"
    names = [info.filename for info in zf.infolist() if not info.is_dir()]
    if len(names) != len(set(names)):
        raise ValueError(f"vendored {distribution} wheel contains duplicate ZIP members")
    if record_name not in names:
        raise ValueError(f"vendored {distribution} wheel has no expected {record_name}")

    try:
        record_text = zf.read(record_name).decode("utf-8")
        rows = list(csv.reader(io.StringIO(record_text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"vendored {distribution} wheel has an invalid RECORD") from exc

    recorded: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3:
            raise ValueError(f"vendored {distribution} wheel has a malformed RECORD row")
        path, digest_field, size_field = row
        if not path or path in recorded:
            raise ValueError(f"vendored {distribution} wheel has duplicate/empty RECORD paths")
        recorded[path] = (digest_field, size_field)

    if set(recorded) != set(names):
        missing = sorted(set(names) - set(recorded))
        extra = sorted(set(recorded) - set(names))
        raise ValueError(
            f"vendored {distribution} wheel RECORD membership mismatch "
            f"(unrecorded={missing}, missing={extra})"
        )

    for path in names:
        digest_field, size_field = recorded[path]
        if path == record_name:
            if digest_field or size_field:
                raise ValueError(f"vendored {distribution} wheel RECORD self-row must be unhashed")
            continue
        if not digest_field.startswith("sha256=") or not size_field:
            raise ValueError(f"vendored {distribution} wheel RECORD lacks sha256/size for {path!r}")
        body = zf.read(path)
        expected_digest = digest_field.removeprefix("sha256=")
        actual_digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ValueError(f"vendored {distribution} wheel RECORD hash mismatch for {path!r}")
        try:
            expected_size = int(size_field)
        except ValueError as exc:
            raise ValueError(
                f"vendored {distribution} wheel RECORD has invalid size for {path!r}"
            ) from exc
        if expected_size != len(body):
            raise ValueError(f"vendored {distribution} wheel RECORD size mismatch for {path!r}")


def _safe_extract(zf: zipfile.ZipFile, dest: Path, *, distribution: str) -> None:
    """Extract a wheel, refusing members that escape ``dest`` (zip-slip) or
    that would shadow the engine / auto-exec at a top-level name. A poisoned
    wheel is untrusted input (codex 2026-07-19)."""
    if distribution not in PINNED_VERSIONS:
        raise ValueError(f"unexpected vendored distribution {distribution!r}")
    wheel_name = distribution.replace("-", "_")
    allowed = _PACKAGE_ROOTS[distribution] | {
        f"{wheel_name}-{PINNED_VERSIONS[distribution]}.dist-info",
    }
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"vendored wheel member {member!r} escapes {dest} "
                             f"(zip-slip) — refusing extraction")
        top = member.split("/", 1)[0]
        if top in _FORBIDDEN_TOPLEVEL:
            raise ValueError(f"vendored wheel member {member!r} would shadow a "
                             f"protected top-level name — refusing extraction")
        if top not in allowed:
            raise ValueError(f"vendored {distribution} wheel member {member!r} has "
                             f"unexpected top-level path {top!r} — refusing extraction")
    zf.extractall(dest)


def _download_jobs(arch: str, python_exe: str) -> tuple[list[str], ...]:
    if arch not in ARCHS:
        raise ValueError(f"unsupported vendor architecture {arch!r}")
    common = [
        python_exe, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
        "--python-version", _VM_PY_NODOT, "--implementation", "cp",
    ]
    jobs = []
    for distribution, version in PINNED_VERSIONS.items():
        # onnxruntime 1.23.2 starts at manylinux_2_28; the rest publish either
        # manylinux2014 or platform-independent wheels.
        platform = f"manylinux_2_28_{arch}" if distribution == "onnxruntime" else f"manylinux2014_{arch}"
        jobs.append([*common, "--platform", platform, f"{distribution}=={version}"])
    return tuple(jobs)


def _replace_generation(staging: Path, arch_dir: Path) -> None:
    """Promote a fully verified staging tree, preserving the old generation
    until the new one has passed all validation."""
    backup = Path(tempfile.mkdtemp(prefix=f".{arch_dir.name}.backup-", dir=arch_dir.parent))
    backup.rmdir()  # reserve a unique sibling name for the atomic rename below
    moved_old = False
    try:
        if arch_dir.exists():
            os.replace(arch_dir, backup)
            moved_old = True
        os.replace(staging, arch_dir)
    except OSError:
        if moved_old and backup.exists() and not arch_dir.exists():
            os.replace(backup, arch_dir)
        raise
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def _has_deps(arch_dir: Path) -> bool:
    return (
        (arch_dir / "tokenizers" / "tokenizers.abi3.so").exists()
        and (arch_dir / "sqlite_vec" / "vec0.so").exists()
        and (arch_dir / "onnxruntime").is_dir()
        and (arch_dir / "numpy").is_dir()
    )


def check_wheel_tag(wheel_name: str, vm_python: str = VM_PYTHON) -> bool:
    """True iff the wheel's filename tags are importable by the pinned VM
    interpreter (regression guard, 2026-07-18: cp311 wheels staged for the
    3.10-only VM caused the EmbedderUnavailable outage). Acceptable:
    ``cp310``, ``abi3`` built at or below 3.10 (e.g. cp39-abi3), and pure
    ``py3-none-any``. Refused: cp311/cp312/..., or any unparseable name."""
    major, minor = (int(x) for x in vm_python.split("."))
    stem = wheel_name[:-len(".whl")] if wheel_name.endswith(".whl") else wheel_name
    parts = stem.split("-")
    if len(parts) < 5:  # name-version[-build]-python-abi-platform
        return False
    py_tag, abi_tag = parts[-3], parts[-2]
    if abi_tag not in (f"cp{major}{minor}", "abi3", "none"):
        return False

    def _py_ok(t: str) -> bool:
        if t in ("py3", "py2.py3", f"py{major}{minor}"):
            return True
        m = re.fullmatch(r"cp(\d)(\d+)", t)
        if not m:
            return False
        maj, mnr = int(m.group(1)), int(m.group(2))
        # abi3 is forward-compatible: cp39-abi3 imports fine on 3.10.
        return maj == major and (mnr <= minor if abi_tag == "abi3" else mnr == minor)

    return any(_py_ok(t) for t in py_tag.split("."))


def _download_and_unpack(arch: str, arch_dir: Path, python_exe: str) -> bool:
    try:
        jobs = _download_jobs(arch, python_exe)
    except ValueError as exc:
        print(f"[vendor] REFUSING staging request: {exc}", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory() as td:
        downloads = Path(td)
        for job in jobs:
            result = subprocess.run([*job, "-d", str(downloads)], capture_output=True)
            if result.returncode != 0:
                requirement = next(arg for arg in job if "==" in arg)
                print(f"[vendor] failed to download locked {requirement}", file=sys.stderr)
                return False

        expected_names = set(TRUSTED_WHEELS[arch])
        wheels = sorted(downloads.glob("*.whl"))
        actual_names = {wheel.name for wheel in wheels}
        if actual_names != expected_names:
            print(
                f"[vendor] REFUSING {arch} wheel set: expected exactly the trusted lock "
                f"(missing={sorted(expected_names - actual_names)}, "
                f"unexpected={sorted(actual_names - expected_names)})",
                file=sys.stderr,
            )
            return False

        arch_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{arch}.stage-", dir=arch_dir.parent))
        try:
            for wheel in wheels:
                if not check_wheel_tag(wheel.name):
                    raise ValueError(
                        f"wheel tag for {wheel.name} is incompatible with Python {VM_PYTHON}"
                    )
                # Authenticate the whole wheel before treating it as a ZIP, then
                # verify its internal RECORD and package-root allowlist.
                distribution = _trusted_distribution(wheel, arch)
                with zipfile.ZipFile(wheel) as zf:
                    _verify_record(zf, distribution=distribution)
                    _safe_extract(zf, staging, distribution=distribution)
            if not _has_deps(staging):
                raise ValueError("verified wheel generation lacks required semantic libraries")
            _replace_generation(staging, arch_dir)
            return True
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            print(f"[vendor] REFUSING {arch} wheel generation: {exc}", file=sys.stderr)
            return False
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def stage_vendor(brain_dir, *, archs=ARCHS, python_exe: str | None = None,
                 force: bool = False) -> dict:
    """Ensure ``<brain_dir>/vendor/<arch>/`` holds tokenizers + sqlite-vec for
    each arch. Skips an arch already staged (the deps are version-stable) unless
    ``force``. Returns ``{arch: 'present'|'staged'|'failed'}`` — a failed arch is
    advisory (the VM degrades to lexical), never an exception."""
    brain_dir = Path(brain_dir)
    python_exe = python_exe or sys.executable
    out: dict[str, str] = {}
    for arch in archs:
        arch_dir = brain_dir / "vendor" / arch
        if _has_deps(arch_dir) and not force:
            out[arch] = "present"
            continue
        out[arch] = "staged" if _download_and_unpack(arch, arch_dir, python_exe) else "failed"
    return out


def write_shim(brain_dir) -> Path:
    """Write the vendored-deps-aware zero-install shim to ``<brain_dir>/brain``."""
    p = Path(brain_dir) / "brain"
    p.write_text(SHIM_CONTENT, encoding="utf-8")
    p.chmod(0o755)
    return p


def _demo() -> None:
    """ponytail self-check: shim writer is deterministic + executable; the
    vendor staged-detector agrees with the on-disk .so files (no network)."""
    with tempfile.TemporaryDirectory() as td:
        bd = Path(td)
        p = write_shim(bd)
        assert p.read_text(encoding="utf-8") == SHIM_CONTENT
        assert p.stat().st_mode & 0o111  # executable
        assert not _has_deps(bd / "vendor" / "aarch64")  # nothing staged yet
        (bd / "vendor" / "aarch64" / "tokenizers").mkdir(parents=True)
        (bd / "vendor" / "aarch64" / "tokenizers" / "tokenizers.abi3.so").write_bytes(b"")
        (bd / "vendor" / "aarch64" / "sqlite_vec").mkdir(parents=True)
        (bd / "vendor" / "aarch64" / "sqlite_vec" / "vec0.so").write_bytes(b"")
        (bd / "vendor" / "aarch64" / "onnxruntime").mkdir(parents=True)
        (bd / "vendor" / "aarch64" / "numpy").mkdir(parents=True)
        assert _has_deps(bd / "vendor" / "aarch64")
        # ABI pin guard (2026-07-18): cp310/abi3/pure pass, cp311 refused
        assert check_wheel_tag("numpy-1.26.4-cp310-cp310-manylinux_2_28_aarch64.whl")
        assert not check_wheel_tag("numpy-1.26.4-cp311-cp311-manylinux_2_28_aarch64.whl")
    print("OK: vendor_semantic_deps self-check passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        _demo()
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else "."
        print(stage_vendor(target))
