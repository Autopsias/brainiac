#!/usr/bin/env bash
# cowork_session_bootstrap.sh — SOURCE this at the start of each Cowork VM
# session (INT-02). The VM filesystem persists but the shell env does not, so
# PATH + role + model-cache must be re-exported every session.
#
#   source tools/cowork_session_bootstrap.sh        # run from the workspace root
#
# Sets the VM (read+draft-only) role, points fastembed at the bundled model,
# verifies the shipped binaries' integrity, symlinks the arch-matched binary,
# and puts brain on PATH.
export BRAIN_VAULT="${BRAIN_VAULT:-$PWD/vault}"
export BRAIN_ROLE=vm
export BRAIN_RUNTIME_DIR="${BRAIN_RUNTIME_DIR:-$BRAIN_VAULT/.brain}"
export BRAIN_MODEL_CACHE="${BRAIN_MODEL_CACHE:-$BRAIN_RUNTIME_DIR/model}"

# --------------------------------------------------------------------------
# Supply-chain check (hardening pass): verify the shipped binaries against a
# SHA256SUMS manifest BEFORE they are trusted — i.e. before the arch-matched
# ELF is symlinked to `brain` and before $BRAIN_RUNTIME_DIR is PATH-prepended.
# The manifest (one `sha256sum`-format line per binary) is produced on the
# HOST leg by tools/cowork_workspace_install.sh at assembly time.
#
#   - Manifest ABSENT  -> WARN and proceed. Older/manually-assembled
#     workspaces won't have one yet; this check is a supply-chain
#     improvement layered on top of the existing trust model, not a hard
#     gate that retroactively breaks every workspace that predates it.
#   - Manifest PRESENT -> MUST verify clean, or we refuse to trust the
#     binaries at all: no symlink, no PATH export, session bootstrap aborts.
#     A present-but-failing manifest is exactly the case worth catching
#     (a tampered / substituted binary on the shared VM mount).
brain_bin_dir="$BRAIN_RUNTIME_DIR/bin"
brain_sha256sums="$brain_bin_dir/SHA256SUMS"
if [ -f "$brain_sha256sums" ]; then
  brain_sha256sums_cmd=()
  if command -v sha256sum >/dev/null 2>&1; then
    brain_sha256sums_cmd=(sha256sum -c SHA256SUMS)
  elif command -v shasum >/dev/null 2>&1; then
    brain_sha256sums_cmd=(shasum -a 256 -c SHA256SUMS)
  fi
  if [ "${#brain_sha256sums_cmd[@]}" -gt 0 ]; then
    if brain_sha256sums_out=$(cd "$brain_bin_dir" && "${brain_sha256sums_cmd[@]}" 2>&1); then
      echo "[cowork] SHA256SUMS verified OK ($brain_bin_dir)"
    else
      echo "[cowork] REFUSING to trust workspace binaries: SHA256SUMS check FAILED for $brain_bin_dir" >&2
      echo "$brain_sha256sums_out" >&2
      unset brain_bin_dir brain_sha256sums brain_sha256sums_cmd brain_sha256sums_out
      return 1 2>/dev/null || exit 1
    fi
  else
    echo "[cowork] WARN: SHA256SUMS present at $brain_sha256sums but neither sha256sum" \
         "nor shasum is on PATH — cannot verify, proceeding WITHOUT integrity verification" >&2
  fi
  unset brain_sha256sums_cmd brain_sha256sums_out
else
  echo "[cowork] WARN: no SHA256SUMS manifest at $brain_bin_dir — skipping binary" \
       "integrity check (re-run tools/cowork_workspace_install.sh to generate one)" >&2
fi
unset brain_bin_dir brain_sha256sums

ln -sf "bin/brain-linux-$(uname -m)" "$BRAIN_RUNTIME_DIR/brain"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
echo "[cowork] role=$BRAIN_ROLE vault=$BRAIN_VAULT arch=$(uname -m)"
brain status 2>/dev/null || echo "[cowork] no snapshot yet — host must publish one"
