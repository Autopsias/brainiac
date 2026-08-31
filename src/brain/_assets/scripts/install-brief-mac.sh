#!/usr/bin/env bash
# install-brief-mac.sh — install brain-daily-brief as a macOS launchd LaunchAgent (UX-02)
#
# Usage:
#   BRAIN_VAULT=/path/to/vault bash scripts/install-brief-mac.sh
#
# Prerequisites:
#   - brain binary must be on PATH (test: brain --version)
#   - BRAIN_AUDIT_KEY_PEM must be set OR the key stored in the macOS Keychain
#     under service "profile-a-brain-audit-key" (security find-generic-password -s profile-a-brain-audit-key -w)
#
# Uninstall (per-vault label — the script prints the exact paths on install):
#   launchctl unload ~/Library/LaunchAgents/com.brainiac.nightly.<id>.plist
#   rm ~/Library/LaunchAgents/com.brainiac.nightly.<id>.plist
set -euo pipefail

VAULT="${BRAIN_VAULT:?BRAIN_VAULT must be set (path to your brain vault)}"
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPTS/brain-brief-mac.plist"

# Per-vault launchd label (SINGLE SOURCE OF TRUTH: brain.config.nightly_label),
# so two registered vaults never install to one shared label and clobber each
# other's nightly job. Prefer the installed package; fall back to a shell
# reimplementation of vault_slug8 (sha256 of the resolved vault path, first 8).
LABEL=""
BRAIN_BIN="$(command -v brain || true)"
if [ -n "$BRAIN_BIN" ]; then
    VENV_PY="$(dirname "$BRAIN_BIN")/python3"
    [ -x "$VENV_PY" ] || VENV_PY="python3"
    LABEL="$(BRAIN_VAULT="$VAULT" "$VENV_PY" - <<'PY' 2>/dev/null || true
import os
from brain.config import nightly_label
print(nightly_label(os.environ["BRAIN_VAULT"]))
PY
)"
fi
if [ -z "$LABEL" ]; then
    _vr="$(python3 -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$VAULT" 2>/dev/null || echo "$VAULT")"
    _h="$(printf '%s' "$_vr" | shasum -a 256 | cut -c1-8)"
    LABEL="com.brainiac.nightly.$_h"
fi

# THE SAME override brain.config.nightly_plist_path reads. A test (or a
# sandboxed install) that redirects one must redirect both, or a test that
# merges a sweep dir reads, backs up and rewrites the developer's REAL plist --
# $HOME is not enough, because launchctl talks to the login domain by uid.
LAUNCH_AGENTS_DIR="${BRAIN_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LEGACY_LABEL="com.profile-a-brain.daily-brief"
LEGACY_PLIST="$LAUNCH_AGENTS_DIR/$LEGACY_LABEL.plist"
PLIST_DST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
LOGDIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"

# Audit signing key: the drain resolves it from the macOS Keychain AT RUNTIME
# (src/brain/audit.py resolve_signing_key(), env -> keychain fallthrough), so
# the plist does NOT carry the key material. Baking the PEM into the plist
# (the old behavior) left the private key in a plaintext file under
# ~/Library/LaunchAgents. We only bake a value when the operator explicitly
# injected BRAIN_AUDIT_KEY_PEM (custom custody / unattended box) — and the
# plist is chmod 600 below either way.
KEYCHAIN_SERVICE="${BRAIN_AUDIT_KEYCHAIN_SERVICE:-profile-a-brain-audit-key}"
# `set -u` + an unset $USER is a hard abort, not a fallback: launchd, cron and
# any `env -i` caller hand this script an environment without USER, and both
# uses below took the script down at line 75 with "USER: unbound variable"
# BEFORE it could write a plist. Measured 2026-08-30 running
# `brain provision-local` end to end: wire 1 reported `init: exit 1`.
KEY_ACCOUNT="${USER:-$(id -un)}"
AUDIT_KEY="${BRAIN_AUDIT_KEY_PEM:-}"

# Managed endpoints (BRAIN_MANAGED=1) resolve the key ONLY from the OS keystore
# — audit.py ignores env custody entirely there. Baking the PEM into the plist
# would leave a private key sitting in a plaintext file that nothing will ever
# read: strictly worse than not writing it. Drop it and let the runtime
# keychain lookup do its job.
if [ -n "${BRAIN_MANAGED:-}" ] && [ "${BRAIN_MANAGED}" != "0" ] && [ -n "$AUDIT_KEY" ]; then
    echo "NOTE: managed mode — not baking BRAIN_AUDIT_KEY_PEM into the plist" >&2
    echo "      (managed endpoints resolve the key only from the OS keystore)." >&2
    AUDIT_KEY=""
fi

if [ -z "$AUDIT_KEY" ]; then
    if ! security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEY_ACCOUNT" >/dev/null 2>&1; then
        echo "WARNING: No Keychain entry for '$KEYCHAIN_SERVICE'." >&2
        echo "         Captures will not be signed (drain fails closed). Run 'brain audit-key'" >&2
        echo "         (create-if-absent, never rotates) — no reinstall needed after." >&2
    fi
fi

mkdir -p "$LOGDIR" "$LAUNCH_AGENTS_DIR"

# THE PLIST IS THE DURABLE HOME OF THE SWEEP LIST, for every caller.
# This script re-renders the whole body on every run and reinstalls when it
# differs, so an UNSET $BRAIN_WORKSPACE_SWEEP_DIRS used to bake an empty string
# -- and any `brain init --full --apply` from a bare shell (a reinstall, a
# second vault, an old doc, `brain update`) silently wiped a list somebody had
# configured. Unset now means "keep what is installed"; an explicitly SET value
# still wins, including an explicit empty one (the operator's intent).
if [ -n "${BRAIN_WORKSPACE_SWEEP_DIRS+set}" ]; then
    SWEEP_DIRS="$BRAIN_WORKSPACE_SWEEP_DIRS"
elif [ -f "$PLIST_DST" ]; then
    SWEEP_DIRS="$(/usr/libexec/PlistBuddy -c \
        "Print :EnvironmentVariables:BRAIN_WORKSPACE_SWEEP_DIRS" \
        "$PLIST_DST" 2>/dev/null || true)"
else
    SWEEP_DIRS=""
fi
# EVERY value substituted into the plist below goes through this, because each
# one is escaped TWICE over and both stages are load-bearing.
#
# 1. The value lands inside an XML <string>, so `&` and `<` in a folder name
#    would render a plist nothing can parse -- and the PlistBuddy round-trip
#    above reads the value back out unescaped, so it would corrupt a little
#    more on every run.
# 2. ...and THEN it becomes a sed REPLACEMENT, where `\`, `&` and the `|`
#    delimiter are metacharacters of their own. A bare `&` in a replacement
#    means "the whole match", so the `&` stage 1 just wrote as `&amp;`
#    re-inserts the literal placeholder text: probed 2026-08-30,
#    BRAIN_WORKSPACE_SWEEP_DIRS=/tmp/A&B<C rendered as
#    `/tmp/ASWEEP_DIRS_PLACEHOLDERamp;BSWEEP_DIRS_PLACEHOLDERlt;C`, and
#    VAULT=/tmp/A&B<C rendered `<string>/tmp/AVAULT_PATHB<C</string>` --- an
#    unparseable plist from an ORDINARY path (`R&D`, `Smith & Co`).
#
# It is a function because doing it for the sweep list alone left the two
# substitutions directly below it raw (probed 2026-08-30). $AUDIT_KEY needs no
# metacharacter escaping -- base64 and the PEM header contain none of \ & | < --
# and $LABEL is `com.brainiac.nightly.<hex8>` by construction. It is NOT used
# on $AUDIT_KEY, though: a real PEM carries embedded NEWLINES (multi-line,
# `-----BEGIN...-----\n<body>\n-----END...-----\n`), and a `sed -e` REPLACEMENT
# cannot hold a raw newline without a `\`-continuation before each one -- an
# unescaped one is a parse error, not silent corruption. AUDIT_KEY gets its own
# newline-safe substitution below instead of going through this sed pipeline.
xml_sed_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' \
                     | sed -e 's/[\\&|]/\\&/g'
}

SWEEP_DIRS_SED="$(xml_sed_escape "$SWEEP_DIRS")"
VAULT_SED="$(xml_sed_escape "$VAULT")"
HOME_SED="$(xml_sed_escape "$HOME")"
SCRIPTS_SED="$(xml_sed_escape "$SCRIPTS")"

# Render the plist to a scratch file first so we can diff against whatever is
# already installed — the nightly-diff contract (docs/install/plugin-distribution.md
# §3): reinstall/reload iff the rendered body actually changed, otherwise report
# unchanged and touch nothing (idempotent re-runs, no needless launchctl churn).
PLIST_NEW="$(mktemp "${TMPDIR:-/tmp}/brain-brief-mac.plist.XXXXXX")"
trap 'rm -f "$PLIST_NEW"' EXIT

sed -e "s|LABEL_PLACEHOLDER|$LABEL|g" \
    -e "s|SCRIPTS_DIR|$SCRIPTS_SED|g" \
    -e "s|VAULT_PATH|$VAULT_SED|g" \
    -e "s|HOME_DIR|$HOME_SED|g" \
    -e "s|SWEEP_DIRS_PLACEHOLDER|$SWEEP_DIRS_SED|g" \
    "$PLIST_SRC" > "$PLIST_NEW"

# AUDIT_KEY_PEM_PLACEHOLDER: substituted OUTSIDE the sed pipeline above with
# awk, whose replacement text is an ordinary string (no per-`-e`-argument
# newline restriction) rather than a sed script fragment --- the same
# multi-line value that breaks `sed -e "s|X|$AUDIT_KEY|g"` (probed 2026-08-31,
# running `provision-local` end to end with a synthetic Ed25519 test key:
# "sed: 1: ...: unescaped newline inside substitute pattern") assigns straight
# through `ENVIRON["AUDIT_KEY"]` here. Trailing newline(s) are stripped by the
# `$( )` around printf below (command substitution always strips them),
# matching what the OLD single-line sed substitution effectively produced too.
# NOT guarded on a non-empty $AUDIT_KEY. Managed mode (line 74) and a box with
# no key both set it to "", and the placeholder MUST still be replaced --- by
# nothing. A guard here skipped the awk and left the literal string
# `AUDIT_KEY_PEM_PLACEHOLDER` baked into the plist as BRAIN_AUDIT_KEY_PEM
# (probed 2026-08-31 against the pre-change script, which rendered
# `<string></string>`), handing every managed endpoint a garbage PEM instead of
# the empty value that makes the runtime fall back to the Keychain. `split("")`
# yields no fields, so `pem[1]` is "" and the placeholder collapses to nothing.
AUDIT_KEY_TRIMMED="$(printf '%s' "$AUDIT_KEY")"
AUDIT_KEY="$AUDIT_KEY_TRIMMED" awk '
        function xml_awk_escape(s) {
            # Same two-char XML entity escape as xml_sed_escape above --- `&`
            # first, so `<` does not re-encode the `&` that step just wrote.
            # No sub()/gsub()-replacement-side "&" concern here: this gsub
            # RUNS the escape, it is not used as another calls REPLACEMENT
            # argument (that is the substr()-based split below, precisely to
            # avoid needing a second round of escaping on top of this one).
            gsub(/&/, "\\&amp;", s); gsub(/</, "\\&lt;", s); return s
        }
        {
            pos = index($0, "AUDIT_KEY_PEM_PLACEHOLDER")
            if (!pos) { print; next }
            n = split(ENVIRON["AUDIT_KEY"], pem, "\n")
            prefix = substr($0, 1, pos - 1)
            suffix = substr($0, pos + length("AUDIT_KEY_PEM_PLACEHOLDER"))
            # substr()/printf concatenation only, below --- never sub()/gsub()
            # with pem[i] as the REPLACEMENT argument, which would reinterpret
            # any literal "&" the escape above just wrote as "insert match".
            printf "%s%s", prefix, xml_awk_escape(pem[1])
            for (i = 2; i <= n; i++) printf "\n%s", xml_awk_escape(pem[i])
            print suffix
        }
    ' "$PLIST_NEW" > "$PLIST_NEW.awk" && mv "$PLIST_NEW.awk" "$PLIST_NEW"

# ponytail: real launchctl talks to the login launchd domain by uid, not by
# $HOME — a sandboxed HOME does NOT sandbox launchctl. BRAIN_LAUNCHD_DRY_RUN=1
# (or a non-default HOME, detected below) skips the real bootstrap/load calls
# so test/sandbox runs never touch the real user's launchd domain.
if [ -z "${BRAIN_LAUNCHD_DRY_RUN:-}" ] && [ "$HOME" != "$(eval echo ~"$KEY_ACCOUNT")" ]; then
    BRAIN_LAUNCHD_DRY_RUN=1
fi

# One-time migration off the legacy SHARED label: if the old single-label plist
# exists (and isn't this vault's per-vault plist), unload + remove it so the two
# host vaults stop competing for one launchd job. Idempotent — absent after the
# first migrated install.
if [ "$LEGACY_PLIST" != "$PLIST_DST" ] && [ -f "$LEGACY_PLIST" ]; then
    if [ "${BRAIN_LAUNCHD_DRY_RUN:-0}" = "1" ]; then
        echo "migration DRY-RUN: would retire legacy shared label $LEGACY_LABEL"
    else
        launchctl unload "$LEGACY_PLIST" 2>/dev/null || true
        rm -f "$LEGACY_PLIST"
        echo "migrated: retired legacy shared nightly label $LEGACY_LABEL (now per-vault)"
    fi
fi

if [ -f "$PLIST_DST" ] && cmp -s "$PLIST_NEW" "$PLIST_DST"; then
    echo "nightly: unchanged (label: $LABEL) — nothing to reload"
    echo "  Status: launchctl list $LABEL"
    exit 0
fi

cp "$PLIST_NEW" "$PLIST_DST"
chmod 600 "$PLIST_DST"

if [ "${BRAIN_LAUNCHD_DRY_RUN:-0}" = "1" ]; then
    echo "DRY-RUN (sandboxed HOME or BRAIN_LAUNCHD_DRY_RUN=1) — plist written, launchd NOT reloaded. Would run:"
    echo "  launchctl unload '$PLIST_DST'"
    echo "  launchctl load -w '$PLIST_DST'"
    echo "  To load it for real, re-run this from a normal (non-sandboxed) terminal, or run the two commands above."
else
    # (Re)load the agent.
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load -w "$PLIST_DST"
fi

echo "✓ Installed: $LABEL (hourly umbrella, vault: $VAULT)"

# --- brain-synthesis (the SECOND sanctioned task; THE LOCK host=2) ---------
# One per HOST (label has no vault id): the script iterates every registered
# vault in ~/.brainiac/workspaces.json, so re-running this installer for a
# second vault just refreshes the same host-level agent. Weekly Sun 08:00.
SYNTH_SRC="$SCRIPTS/brain-synthesis-mac.plist"
SYNTH_DST="$LAUNCH_AGENTS_DIR/com.brainiac.synthesis.plist"
if [ -f "$SYNTH_SRC" ]; then
    SYNTH_NEW="$(mktemp "${TMPDIR:-/tmp}/brain-synthesis-mac.plist.XXXXXX")"
    # Same two-stage escaping as the nightly block above: a raw $SCRIPTS or
    # $HOME carrying `&`, `<` or `|` corrupts this plist too (see the worked
    # example at the top of the nightly substitution).
    sed -e "s|SCRIPTS_DIR|$SCRIPTS_SED|g" -e "s|HOME_DIR|$HOME_SED|g" \
        "$SYNTH_SRC" > "$SYNTH_NEW"
    if ! cmp -s "$SYNTH_NEW" "$SYNTH_DST" 2>/dev/null; then
        cp "$SYNTH_NEW" "$SYNTH_DST"
        chmod 600 "$SYNTH_DST"
        if [ "${BRAIN_LAUNCHD_DRY_RUN:-0}" = "1" ]; then
            echo "DRY-RUN — synthesis plist written, launchd NOT reloaded."
        else
            launchctl unload "$SYNTH_DST" 2>/dev/null || true
            launchctl load -w "$SYNTH_DST"
        fi
        echo "✓ Installed: com.brainiac.synthesis (weekly Sun 08:00, all registered vaults)"
    fi
    rm -f "$SYNTH_NEW"
fi
echo "  Logs:      $LOGDIR/brief-YYYY-MM-DD.log"
echo "  Status:    launchctl list $LABEL"
echo "  Dry-run:   launchctl start $LABEL"
echo "  Uninstall: launchctl unload '$PLIST_DST' && rm '$PLIST_DST'"
