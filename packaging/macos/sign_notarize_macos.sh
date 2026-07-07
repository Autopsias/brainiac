#!/usr/bin/env bash
# sign_notarize_macos.sh — Developer-ID sign + notarize + staple the `brain`
# one-dir bundle.  >>> PENDING EXTERNAL: needs maintainer's Apple Developer ID <<<
#
# Gatekeeper on a managed Mac requires a Developer-ID signature + a notarization
# ticket. The build agent holds no Apple ID, so this is a runbook script: it is
# correct and runnable, but only completes on a machine signed into your
# organization's Apple Developer account.
#
# Prereqs (all PENDING the Apple ID):
#   - "Developer ID Application: <YourOrg> (<TEAMID>)" cert in the login keychain
#   - notarytool credentials stored:  xcrun notarytool store-credentials ...
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLE="$REPO/dist/brain"
ENTITLEMENTS="$REPO/packaging/macos/entitlements.plist"

: "${DEV_ID:?set DEV_ID to 'Developer ID Application: <YourOrg> (<TEAMID>)'}"
: "${NOTARY_PROFILE:?set NOTARY_PROFILE to the notarytool keychain profile name}"

[ -x "$BUNDLE/brain" ] || { echo "no built bundle — run build_macos.sh first" >&2; exit 1; }

echo "== 1. codesign (hardened runtime, least-privilege entitlements) =="
# Sign nested Mach-O first (deep), then the main binary with the runtime option.
find "$BUNDLE/_internal" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 |
  xargs -0 -I{} codesign --force --timestamp --options runtime -s "$DEV_ID" "{}"
codesign --force --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" -s "$DEV_ID" "$BUNDLE/brain"
codesign --verify --deep --strict --verbose=2 "$BUNDLE/brain"

echo "== 2. notarize (zip the bundle, submit, wait) =="
ZIP="$REPO/dist/brain-macos.zip"
/usr/bin/ditto -c -k --keepParent "$BUNDLE" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait

echo "== 3. staple the ticket to the bundle =="
xcrun stapler staple "$BUNDLE/brain" || xcrun stapler staple "$BUNDLE"
spctl --assess --type execute --verbose "$BUNDLE/brain"

echo "== 4. signed hash list (for the approval evidence pack) =="
shasum -a 256 "$BUNDLE/brain"
echo "Signed + notarized + stapled."
