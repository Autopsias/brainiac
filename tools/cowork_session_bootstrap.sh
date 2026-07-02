#!/usr/bin/env bash
# cowork_session_bootstrap.sh — SOURCE this at the start of each Cowork VM
# session (INT-02). The VM filesystem persists but the shell env does not, so
# PATH + role + model-cache must be re-exported every session.
#
#   source tools/cowork_session_bootstrap.sh        # run from the workspace root
#
# Sets the VM (read+draft-only) role, points fastembed at the bundled model,
# symlinks the arch-matched binary, and puts brain on PATH.
export BRAIN_VAULT="${BRAIN_VAULT:-$PWD/vault}"
export BRAIN_ROLE=vm
export BRAIN_RUNTIME_DIR="${BRAIN_RUNTIME_DIR:-$BRAIN_VAULT/.brain}"
export BRAIN_MODEL_CACHE="${BRAIN_MODEL_CACHE:-$BRAIN_RUNTIME_DIR/model}"
ln -sf "bin/brain-linux-$(uname -m)" "$BRAIN_RUNTIME_DIR/brain"
export PATH="$BRAIN_RUNTIME_DIR:$PATH"
echo "[cowork] role=$BRAIN_ROLE vault=$BRAIN_VAULT arch=$(uname -m)"
brain status 2>/dev/null || echo "[cowork] no snapshot yet — host must publish one"
