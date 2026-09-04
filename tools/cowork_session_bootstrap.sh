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

# OPTIONAL per-vault VM egress ceiling (owner ruling 2026-08-17; VULN-3386,
# external pentest 2026-08: the session's own shell could export
# $BRAIN_VM_MAX_EGRESS_TIER and raise its own cap, and the old unsigned
# `vm-egress-tier` mount file had the same hole — its own comment said it
# "RECORDS an owner decision, it does not ENFORCE one"). The ceiling a VM
# session actually enforces now comes ONLY from the HOST-SIGNED
# `vm-egress-tier.signed` file in the runtime dir, verified against the
# pinned anchor staged at install. The engine reads it directly — nothing to
# export here — and this script deliberately no longer exports a ceiling:
# env values are attacker-settable inside the session and are ignored for
# raising a VM cap. Raise it from the HOST with:
#   brain vm-egress-tier Restricted     # (host-broker; signs tier + vault_id)
# and drop back to the shipped Internal cap with `brain vm-egress-tier`.

# --------------------------------------------------------------------------
# Supply-chain check (hardening pass): verify the shipped binaries against a
# SHA256SUMS manifest BEFORE they are trusted — i.e. before the arch-matched
# ELF is symlinked to `brain` and before $BRAIN_RUNTIME_DIR is PATH-prepended.
# The manifest (one `sha256sum`-format line per binary) is produced on the
# HOST leg by tools/cowork_workspace_install.sh at assembly time.
#
#   - Manifest ABSENT  -> REFUSE. An unverifiable set of binaries is not a
#     hardened supply chain (owner ruling 2026-09-02, L3): a workspace staged
#     before this manifest existed, or staged by hand, will not bootstrap
#     until it is re-staged with `brain provision-local` (or
#     tools/cowork_workspace_install.sh), which generates SHA256SUMS.
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
  echo "[cowork] REFUSING: no SHA256SUMS manifest at $brain_bin_dir — cannot verify" \
       "binary integrity. Re-stage this workspace with 'brain provision-local'" \
       "(or tools/cowork_workspace_install.sh) to generate one." >&2
  unset brain_bin_dir brain_sha256sums
  return 1 2>/dev/null || exit 1
fi
unset brain_bin_dir brain_sha256sums

# `ln -sf` does NOT replace a directory at the target -- it descends and makes the
# link INSIDE it, silently, so PATH then finds no `brain` at all. `-n` handles the
# symlinked-directory case; a real directory there is a broken install and has to
# be said out loud rather than worked around.
if [ -d "$BRAIN_RUNTIME_DIR/brain" ] && [ ! -L "$BRAIN_RUNTIME_DIR/brain" ]; then
  echo "[cowork] ERROR: $BRAIN_RUNTIME_DIR/brain is a directory, not the engine" \
       "symlink -- re-run tools/cowork_workspace_install.sh" >&2
  return 1 2>/dev/null || exit 1
fi
ln -sfn "bin/brain-linux-$(uname -m)" "$BRAIN_RUNTIME_DIR/brain"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
echo "[cowork] role=$BRAIN_ROLE vault=$BRAIN_VAULT arch=$(uname -m)"
brain status 2>/dev/null || echo "[cowork] no snapshot yet — host must publish one"
