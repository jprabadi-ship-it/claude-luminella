#!/bin/bash
# Build Clauminella.app and Clauminella.dmg.
#
#   tools/build_app.sh                 ad-hoc signature
#   SIGN_ID="Apple Development: ..." tools/build_app.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
APP="$ROOT/dist/Clauminella.app"
DMG="$ROOT/dist/Clauminella.dmg"
SIGN_ID="${SIGN_ID:-}"

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
  local bundle="$1" id="$2"
  find "$bundle" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 |
    xargs -0 -n1 codesign --force --sign "$id" 2>/dev/null || true
  find "$bundle/Contents/MacOS" -type f -perm +111 -print0 |
    xargs -0 -n1 codesign --force --sign "$id" 2>/dev/null || true
  find "$bundle/Contents/Frameworks" -type d -name '*.framework' -print0 2>/dev/null |
    xargs -0 -n1 -I{} codesign --force --sign "$id" {} 2>/dev/null || true
  codesign --force --sign "$id" "$bundle"
}

echo "==> sign"
# --deep is not usable here: it leaves the bundled interpreter and the .so
# files signed by different teams, and dyld then refuses to map them
# ("different Team IDs"). Sign every Mach-O individually, inner-most first,
# with one identity, then seal the bundle.
sign_bundle "$APP" "${SIGN_ID:--}"
codesign --verify --deep --verbose=2 "$APP" 2>&1 | tail -2

echo "==> dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Clauminella" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "==> install"
# The dmg is signed ad-hoc so it carries no certificate expiry, but TCC keys
# permissions to the signing identity: an ad-hoc app looks like a different
# app after every build, and the microphone and accessibility grants fall off.
# So the copy that gets installed is re-signed with a stable local identity.
if [ -z "${NO_INSTALL:-}" ]; then
  LOCAL_ID="${LOCAL_SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null |
    grep -o '"Apple Development: [^"]*"' | head -1 | tr -d '"')}"

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
