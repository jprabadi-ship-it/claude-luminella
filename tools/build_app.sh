#!/bin/bash
# Build Luminella.app and Luminella.dmg.
#
#   tools/build_app.sh                 ad-hoc signature
#   SIGN_ID="Apple Development: ..." tools/build_app.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
APP="$ROOT/dist/Luminella.app"
DMG="$ROOT/dist/Luminella.dmg"
SIGN_ID="${SIGN_ID:-}"

echo "==> icon"
"$PY" tools/make_icon.py

echo "==> stage resources"
rm -rf build/stage && mkdir -p build/stage
cp hooks/luminella_hook.py build/stage/hook.py

echo "==> py2app"
rm -rf build/bdist.macosx* build/lib dist/Luminella.app
"$PY" setup.py py2app >/dev/null

echo "==> sign"
# --deep is not usable here: it leaves the bundled interpreter and the .so
# files signed by different teams, and dyld then refuses to map them
# ("different Team IDs"). Sign every Mach-O individually, inner-most first,
# with one identity, then seal the bundle.
ID="${SIGN_ID:--}"

find "$APP" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 |
  xargs -0 -n1 codesign --force --sign "$ID" 2>/dev/null || true

find "$APP/Contents/MacOS" -type f -perm +111 -print0 |
  xargs -0 -n1 codesign --force --sign "$ID" 2>/dev/null || true

find "$APP/Contents/Frameworks" -type d -name '*.framework' -print0 2>/dev/null |
  xargs -0 -n1 -I{} codesign --force --sign "$ID" {} 2>/dev/null || true

codesign --force --sign "$ID" "$APP"
codesign --verify --deep --verbose=2 "$APP" 2>&1 | tail -3

echo "==> dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Luminella" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "==> install"
# Accessibility and microphone grants are tied to the app the user pointed at,
# so the copy in /Applications has to be the one that actually runs. Building
# without installing leaves the granted app and the running app out of step.
if [ -z "${NO_INSTALL:-}" ]; then
  pkill -f "Luminella.app/Contents/MacOS/Luminella" 2>/dev/null || true
  sleep 1
  rm -rf /Applications/Luminella.app
  cp -R "$APP" /Applications/Luminella.app
  open /Applications/Luminella.app
  echo "installed and launched: /Applications/Luminella.app"
else
  echo "skipped (NO_INSTALL set)"
fi

echo
echo "app: $APP"
echo "dmg: $DMG  ($(du -h "$DMG" | cut -f1))"
