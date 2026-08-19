#!/usr/bin/env bash
# build_brain_binary_linux.sh — build the Cowork VM ELFs for BOTH Linux arches
# from any host with Docker (INT-02).
#
# WHY THIS EXISTS: tools/build_brain_binary.sh builds for the CURRENT arch only,
# so running it on the macOS host produces brain-darwin-arm64 — useless to the
# VM. Its own "Cross-arch" note said to drive it under buildx but nothing ever
# did, so dist/brain-linux-* sat at the hand-built 2026-07-13 v0.17.0 while the
# engine shipped 0.20.12. This script is that missing driver: it runs the SAME
# inner script inside a per-arch container, so there is ONE build definition.
#
# python:3.10-slim matches the Cowork VM baseline (cp310 — a cp311 vendor set
# caused the 10-run outage). The ELF bundles its own interpreter, so the VM's
# Python does not have to match; the image is pinned for build reproducibility,
# not runtime compatibility.
#
# Usage:
#   tools/build_brain_binary_linux.sh                 # both arches
#   tools/build_brain_binary_linux.sh aarch64         # just the one Cowork runs
#
# NOTE ON SPEED: the arch matching your host builds natively (fast). The other
# runs under qemu emulation and can take 10-30x longer. Build aarch64 first —
# that is what the Cowork VM runs.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUTDIR="$REPO/dist"
IMAGE="python:3.10-slim"

declare -A PLATFORM=([aarch64]=linux/arm64 [x86_64]=linux/amd64)
arches=("$@")
[ ${#arches[@]} -gt 0 ] || arches=(aarch64 x86_64)

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker is not running" >&2; exit 2; }

# The bind mount must actually arrive. Docker Desktop only shares configured
# paths, and a repo outside them mounts EMPTY: the container then fails with
# "Directory '.' is not installable. Neither 'setup.py' nor 'pyproject.toml'
# found" -- a message indistinguishable from a genuine packaging error, in a
# tree where pyproject.toml plainly exists. `tools/release.py` downgrades this
# script's non-zero exit to a WARNING, so a version bump could complete with
# dist/brain-linux-* still stamped at the PREVIOUS release and nothing in the
# log saying the artifact was stale (measured 2026-08-18: a worktree under
# /private/tmp, mid-release). Prove the mount before spending 20 minutes on a
# build that cannot work.
probe_plat="${PLATFORM[$(uname -m)]:-linux/arm64}"
if ! docker run --rm --platform "$probe_plat" -v "$REPO":/repo -w /repo \
     "$IMAGE" test -f /repo/pyproject.toml 2>/dev/null; then
  echo "[build-linux] REFUSING: the bind mount of $REPO arrives EMPTY inside the" >&2
  echo "  container (pyproject.toml is not visible at /repo). Docker Desktop does" >&2
  echo "  not share this path -- Settings > Resources > File sharing. Build from a" >&2
  echo "  path under \$HOME instead; a worktree in a temp dir will not work." >&2
  exit 2
fi

for arch in "${arches[@]}"; do
  plat="${PLATFORM[$arch]:-}"
  [ -n "$plat" ] || { echo "unknown arch: $arch (want aarch64 or x86_64)" >&2; exit 2; }
  echo "[build-linux] $arch ($plat) via $IMAGE"
  # --build-deps: PyInstaller needs a linker; the wheels for onnxruntime /
  # tokenizers are manylinux so nothing else compiles.
  docker run --rm --platform "$plat" \
    -v "$REPO":/repo -w /repo "$IMAGE" \
    bash -eu -c '
      apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils >/dev/null
      python3 -m pip install --quiet --upgrade pip
      python3 -m pip install --quiet pyinstaller .
      tools/build_brain_binary.sh /repo/dist
    '
  out="$OUTDIR/brain-linux-$arch"
  [ -f "$out" ] || { echo "[build-linux] FAILED: $out was not produced" >&2; exit 1; }
  echo "[build-linux] wrote $out ($(du -h "$out" | cut -f1))"
done

# Freshness is the whole point of this script, so state it rather than implying
# it: a stale ELF is invisible without a version to compare.
echo
echo "[build-linux] built: ${arches[*]}"
echo "[build-linux] stage them with tools/cowork_workspace_install.sh (or brain update)."
