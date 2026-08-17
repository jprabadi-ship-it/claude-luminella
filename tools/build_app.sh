#!/bin/bash
# Build Clauminella.app and Clauminella.dmg.
#
#   tools/build_app.sh                       ad-hoc signature, local install
#   SIGN_ID="Apple Development: ..." …       sign with a named identity
#   NO_INSTALL=1 …                           build only
#   NOTARIZE=1 …                             Developer ID + notarize + staple
#
# NOTARIZE=1 needs two things that only the account holder can create:
#   * a "Developer ID Application" certificate (Xcode > Settings > Accounts >
#     Manage Certificates, or the developer portal)
#   * credentials for the notary service, by either route:
#
#     App Store Connect API key (preferred -- it is just files, and does not
#     depend on a keychain item that a second notarytool cannot see). Put this
#     in ~/.claude/luminella/notary.env, outside the repository:
#         NOTARY_KEY=$HOME/private/AuthKey_XXXXXXXXXX.p8
#         NOTARY_KEY_ID=XXXXXXXXXX
#         NOTARY_ISSUER=00000000-0000-0000-0000-000000000000
#
#     Or a saved notarytool profile, used when NOTARY_KEY is unset:
#         xcrun notarytool store-credentials clauminella \
#           --apple-id <id> --team-id <team> --password <app-specific password>
#     Override the profile name with NOTARY_PROFILE.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
APP="$ROOT/dist/Clauminella.app"
DMG="$ROOT/dist/Clauminella.dmg"
ENTITLEMENTS="$ROOT/tools/entitlements.plist"
NOTARY_PROFILE="${NOTARY_PROFILE:-clauminella}"

find_identity() {
  # Trailing "|| true": with pipefail a grep that matches nothing fails the
  # whole pipeline, and under set -e the assignment that calls this would exit
  # the script before it could explain why.
  security find-identity -v -p codesigning 2>/dev/null |
    grep -o "\"$1[^\"]*\"" | head -1 | tr -d '"' || true
}

DEVELOPER_ID="$(find_identity 'Developer ID Application')"

# Notarisation is only possible with a Developer ID certificate, and only that
# certificate produces a build that opens on someone else's Mac.
if [ -n "${NOTARIZE:-}" ] && [ -z "$DEVELOPER_ID" ]; then
  echo "NOTARIZE=1 but no 'Developer ID Application' certificate is installed." >&2
  echo "Create one in Xcode > Settings > Accounts > Manage Certificates." >&2
  exit 1
fi

SIGN_ID="${SIGN_ID:-${DEVELOPER_ID:-}}"

if [ -n "${NOTARIZE:-}" ]; then
  NOTARY_ENV="$HOME/.claude/luminella/notary.env"
  # shellcheck disable=SC1090
  [ -f "$NOTARY_ENV" ] && . "$NOTARY_ENV"

  if [ -n "${NOTARY_KEY:-}" ]; then
    if [ ! -f "$NOTARY_KEY" ]; then
      echo "NOTARY_KEY points at $NOTARY_KEY, which does not exist." >&2
      exit 1
    fi
    NOTARY_AUTH=(--key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER")
  else
    NOTARY_AUTH=(--keychain-profile "$NOTARY_PROFILE")
  fi

  # Ask the notary service something harmless before building anything. The
  # credentials failing is otherwise discovered after a three minute build,
  # and the build gets thrown away.
  echo "==> notary credentials"
  if ! xcrun notarytool history "${NOTARY_AUTH[@]}" --limit 1 >/dev/null 2>&1; then
    echo "The notary service rejected these credentials." >&2
    if [ -n "${NOTARY_KEY:-}" ]; then
      echo "Check NOTARY_KEY / NOTARY_KEY_ID / NOTARY_ISSUER in $NOTARY_ENV." >&2
    else
      echo "No usable profile named '$NOTARY_PROFILE'. Note there are several" >&2
      echo "notarytool binaries on this Mac (Xcode, Xcode-beta, CommandLineTools)" >&2
      echo "and a profile saved by one is not always visible to another." >&2
      echo "An App Store Connect API key avoids that -- see the notes at the" >&2
      echo "top of this script." >&2
    fi
    exit 1
  fi
  echo "    ok"
fi

echo "==> icon"
"$PY" tools/make_icon.py

echo "==> stage resources"
rm -rf build/stage && mkdir -p build/stage
cp hooks/luminella_hook.py build/stage/hook.py

echo "==> py2app"
rm -rf build/bdist.macosx* build/lib dist/Clauminella.app
"$PY" setup.py py2app >/dev/null

sign_bundle() {
  # --deep is not usable here: it leaves the bundled interpreter and the .so
  # files signed by different teams, and dyld then refuses to map them
  # ("different Team IDs"). Sign every Mach-O individually, inner-most first,
  # with one identity, then seal the bundle.
  #
  # A Developer ID signature additionally needs the hardened runtime and a
  # secure timestamp, or notarisation rejects it. The entitlements go on the
  # bundle only; the inner Mach-Os are signed without them.
  local bundle="$1" id="$2"
  local opts=()
  case "$id" in
    "Developer ID Application"*) opts=(--options runtime --timestamp) ;;
  esac

  find "$bundle" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 |
    xargs -0 -n1 codesign --force "${opts[@]}" --sign "$id" 2>/dev/null || true
  find "$bundle/Contents/MacOS" -type f -perm +111 -print0 |
    xargs -0 -n1 codesign --force "${opts[@]}" --sign "$id" 2>/dev/null || true
  find "$bundle/Contents/Frameworks" -type d -name '*.framework' -print0 2>/dev/null |
    xargs -0 -n1 -I{} codesign --force "${opts[@]}" --sign "$id" {} 2>/dev/null || true

  if [ ${#opts[@]} -gt 0 ]; then
    codesign --force "${opts[@]}" --entitlements "$ENTITLEMENTS" --sign "$id" "$bundle"
  else
    codesign --force --sign "$id" "$bundle"
  fi
}

echo "==> sign"
if [ -n "$SIGN_ID" ]; then
  echo "    identity: $SIGN_ID"
else
  echo "    identity: ad-hoc (this build will not open on another Mac)"
fi
sign_bundle "$APP" "${SIGN_ID:--}"
codesign --verify --deep --verbose=2 "$APP" 2>&1 | tail -2

echo "==> dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Clauminella" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

if [ -n "${NOTARIZE:-}" ]; then
  echo "==> notarize"
  # The dmg carries its own signature, and it is the dmg that gets stapled --
  # the ticket has to travel with the file people actually download.
  codesign --force --timestamp --sign "$DEVELOPER_ID" "$DMG"
  xcrun notarytool submit "$DMG" "${NOTARY_AUTH[@]}" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  echo "    notarized and stapled"
fi

echo "==> install"
# The dmg is signed ad-hoc so it carries no certificate expiry, but TCC keys
# permissions to the signing identity: an ad-hoc app looks like a different
# app after every build, and the microphone and accessibility grants fall off.
# So the copy that gets installed is re-signed with a stable local identity.
if [ -z "${NO_INSTALL:-}" ]; then
  # Prefer Developer ID once it exists, so the copy being tested here is signed
  # the same way as the copy people download. Switching identity changes the
  # bundle's designated requirement, so macOS asks for microphone and
  # accessibility again -- once, on the first build after the switch.
  LOCAL_ID="${LOCAL_SIGN_ID:-${DEVELOPER_ID:-$(find_identity 'Apple Development')}}"

  # Match on the bundle directory alone. Spelling out the executable name here
  # is how a stale process survived three builds after the rename: the pattern
  # still said Luminella, matched nothing, and the old binary kept running
  # while the files on disk were replaced underneath it.
  pkill -f "/Clauminella.app/" 2>/dev/null || true
  sleep 1.5
  if pgrep -f "/Clauminella.app/" >/dev/null 2>&1; then
    echo "WARNING: an old instance is still running -- forcing"
    pkill -9 -f "/Clauminella.app/" 2>/dev/null || true
    sleep 1
  fi
  rm -rf /Applications/Clauminella.app
  cp -R "$APP" /Applications/Clauminella.app

  if [ -n "$LOCAL_ID" ]; then
    sign_bundle /Applications/Clauminella.app "$LOCAL_ID"
    echo "installed, signed as: $LOCAL_ID"
  else
    echo "installed UNSIGNED -- no Apple Development identity found;"
    echo "macOS will ask for permissions again after every build"
  fi
  open /Applications/Clauminella.app
  echo "launched: /Applications/Clauminella.app"
else
  echo "skipped (NO_INSTALL set)"
fi

echo
echo "app: $APP"
echo "dmg: $DMG  ($(du -h "$DMG" | cut -f1))"
