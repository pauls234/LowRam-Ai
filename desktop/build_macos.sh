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
plutil -extract CFBundleIdentifier raw "$APP/Contents/Info.plist" | grep -qx "ai.lowram.studio"
# PyInstaller signs ad hoc (required on Apple Silicon); re-sign if the seal is broken.
if ! codesign --verify --deep --strict "$APP" 2>/dev/null; then
  codesign --force --deep --sign - "$APP"
  codesign --verify --deep --strict "$APP"
fi

echo "==> Zip"
ditto -c -k --keepParent "$APP" "$ZIP"
echo "Built $APP and $ZIP ($(du -h "$ZIP" | cut -f1))"
