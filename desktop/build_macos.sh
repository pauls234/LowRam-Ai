#!/usr/bin/env bash
# Build "LowRAM AI.app" and a zip for distribution (macOS only).
#
#   pip install -r requirements.txt -r requirements-desktop.txt
#   bash desktop/build_macos.sh
#
# Output: dist/LowRAM AI.app and dist/LowRAM-AI-<version>-macOS-<arch>.zip
# Environment: PYTHON (default python3), LOWRAM_TARGET_ARCH (arm64/x86_64/universal2).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ "$(uname -s)" != Darwin ]]; then
  echo "build_macos.sh only runs on macOS (it needs sips, iconutil and ditto)." >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
VERSION="$("$PYTHON" -c 'import backend; print(backend.__version__)')"
ARCH="${LOWRAM_TARGET_ARCH:-$(uname -m)}"
BUILD="$ROOT/build/macos"
ICONSET="$BUILD/LowRAM-AI.iconset"
ICNS="$BUILD/LowRAM-AI.icns"
APP="dist/LowRAM AI.app"
ZIP="dist/LowRAM-AI-$VERSION-macOS-$ARCH.zip"

echo "==> Icon"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  for scale in 1 2; do
    pixels=$((size * scale))
    name="icon_${size}x${size}$([[ $scale == 2 ]] && echo @2x).png"
    src="desktop/assets/icon-$pixels.png"
    if [[ -f "$src" ]]; then
      sips -s format png "$src" --out "$ICONSET/$name" >/dev/null
    else  # fall back to scaling the 1024 px master
      sips -s format png -z "$pixels" "$pixels" desktop/assets/icon-1024.png --out "$ICONSET/$name" >/dev/null
    fi
  done
done
iconutil -c icns "$ICONSET" -o "$ICNS"

echo "==> PyInstaller ($VERSION, $ARCH)"
rm -rf "$APP" "dist/LowRAM AI" "$ZIP"
LOWRAM_ICNS="$ICNS" "$PYTHON" -m PyInstaller --noconfirm --clean \
  --distpath dist --workpath build/pyinstaller desktop/LowRAM-AI.spec

echo "==> Check bundle"
test -x "$APP/Contents/MacOS/LowRAM AI"
test -f "$APP/Contents/Resources/LowRAM-AI.icns"
PLIST="$APP/Contents/Info.plist"
plutil -extract CFBundleIdentifier raw "$PLIST" | grep -qx "ai.lowram.studio"
plutil -extract CFBundleShortVersionString raw "$PLIST" | grep -qx "$VERSION"
plutil -extract NSLocalNetworkUsageDescription raw "$PLIST" >/dev/null
plutil -extract LSMinimumSystemVersion raw "$PLIST" | grep -qx "12.0"

echo "==> Ad-hoc signature"
# Not a Developer ID signature (see docs/DESKTOP.md), but Apple Silicon needs a
# valid seal over the whole bundle after PyInstaller has assembled it.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> Zip"
# ditto keeps symlinks (Python.framework inside the bundle), permissions and
# extended attributes; plain `zip` would break the signature.
ditto -c -k --keepParent "$APP" "$ZIP"
CHECK="$BUILD/zip-check"
rm -rf "$CHECK"
mkdir -p "$CHECK"
ditto -x -k "$ZIP" "$CHECK"
codesign --verify --deep --strict "$CHECK/LowRAM AI.app"
test -x "$CHECK/LowRAM AI.app/Contents/MacOS/LowRAM AI"
rm -rf "$CHECK"
echo "Built $APP and $ZIP ($(du -h "$ZIP" | cut -f1))"
