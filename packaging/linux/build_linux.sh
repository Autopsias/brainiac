#!/usr/bin/env bash
# build_linux.sh — build the `brain` one-dir bundle for BOTH Linux arches that
# Cowork VMs run (x86_64 + aarch64), via docker buildx (PKG-02 / INT-02).
#
# Each arch is built inside its own platform container, so PyInstaller freezes a
# native ELF for that arch (PyInstaller cannot cross-compile — it freezes for the
# arch it runs on; buildx gives us that arch via QEMU when not native).
#
# Usage:  packaging/linux/build_linux.sh [arch ...]      default: both
#         packaging/linux/build_linux.sh aarch64         (just one)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
OUT="$REPO/dist/linux"
mkdir -p "$OUT"
ARCHES=("${@:-x86_64 aarch64}")

declare -A PLAT=( [x86_64]=linux/amd64 [aarch64]=linux/arm64 )

for ARCH in ${ARCHES[@]}; do
  plat="${PLAT[$ARCH]:?unknown arch $ARCH}"
  echo "== building brain one-dir for $ARCH ($plat) =="
  # Build inside the platform container; copy the one-dir out via a scratch stage.
  docker buildx build --platform "$plat" --load \
    -f packaging/linux/Dockerfile.build \
    --build-arg ARCH="$ARCH" \
    -t "brain-build:$ARCH" "$REPO"
  # Extract the built bundle from the image.
  cid="$(docker create --platform "$plat" "brain-build:$ARCH")"
  rm -rf "$OUT/brain-$ARCH"
  docker cp "$cid:/out/brain" "$OUT/brain-$ARCH"
  docker rm "$cid" >/dev/null
  echo "wrote $OUT/brain-$ARCH/  (one-dir, $ARCH)"
  sha256sum "$OUT/brain-$ARCH/brain" 2>/dev/null || shasum -a 256 "$OUT/brain-$ARCH/brain"
done
echo "Ship both into the workspace via tools/cowork_workspace_install.sh."
