#!/usr/bin/env bash
# Builds whisper-cli (speech recognition, whisper.cpp) from a fixed commit for the app bundle.
#
#   bash desktop/build_whisper.sh            # prints the path of the finished whisper-cli
#
# macOS: Metal (GPU) with the shaders built in, no extra libraries (only system frameworks), runs on
# macOS 12+. The release workflow passes the printed path to PyInstaller as LOWRAM_BUNDLE_WHISPER.
set -euo pipefail

COMMIT=d1be6fde11ac6e0407606b4e42fe72d34add8037  # whisper.cpp v1.9.5
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WHISPER_WORK:-"${RUNNER_TEMP:-$ROOT/.cache}/whisper.cpp"}

if [ ! -d "$WORK/.git" ]; then
  mkdir -p "$WORK"
  git -C "$WORK" init -q
  git -C "$WORK" remote add origin https://github.com/ggml-org/whisper.cpp.git
fi
git -C "$WORK" fetch -q --depth 1 origin "$COMMIT"
git -C "$WORK" checkout -q --force FETCH_HEAD
if [ "$(git -C "$WORK" rev-parse HEAD)" != "$COMMIT" ]; then
  echo "unexpected whisper.cpp commit" >&2
  exit 1
fi

flags=(-DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF
       -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_BUILD_EXAMPLES=ON
       -DWHISPER_SDL2=OFF -DWHISPER_CURL=OFF)
if [ "$(uname)" = "Darwin" ]; then
  flags+=(-DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DCMAKE_OSX_DEPLOYMENT_TARGET=12.0
          -DCMAKE_OSX_ARCHITECTURES="$(uname -m)")
fi
cmake -S "$WORK" -B "$WORK/build" "${flags[@]}" > "$WORK/cmake.log" || { cat "$WORK/cmake.log" >&2; exit 1; }
cmake --build "$WORK/build" --config Release --target whisper-cli -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" >&2

BIN="$WORK/build/bin/whisper-cli"
test -x "$BIN"
if [ "$(uname)" = "Darwin" ]; then
  # only system libraries: nothing else would be found inside the app
  if otool -L "$BIN" | tail -n +2 | grep -vE "^\s+(/usr/lib/|/System/Library/)" >&2; then
    echo "whisper-cli links libraries outside the system" >&2
    exit 1
  fi
fi
"$BIN" -h > /dev/null 2>&1 || { echo "whisper-cli does not start" >&2; exit 1; }
echo "$BIN"
