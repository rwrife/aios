#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/distro/alpine/build.env"
BUILD=${BUILD_DIR:-$ROOT/distro/alpine/.work/apps}
DEST=${DESTDIR:-$ROOT/distro/alpine/.work/stage}
mkdir -p "$BUILD" "$DEST/usr/local/bin" "$DEST/usr/local/share/aios"
git config --global --add safe.directory "$BUILD/llama"
cmake -S "$ROOT/apps/shell" -B "$BUILD/shell" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
cmake --build "$BUILD/shell" -j "${JOBS:-4}"
DESTDIR="$DEST" cmake --install "$BUILD/shell"
cp -R "$ROOT/examples" "$DEST/usr/local/share/aios/"
if [ ! -d "$BUILD/llama/.git" ]; then
  git init "$BUILD/llama"
  git -C "$BUILD/llama" remote add origin https://github.com/ggml-org/llama.cpp.git
fi
if ! git -C "$BUILD/llama" cat-file -e "$LLAMA_REF^{commit}" 2>/dev/null; then
  git -C "$BUILD/llama" fetch --depth 1 origin "$LLAMA_REF"
fi
git -C "$BUILD/llama" checkout --detach "$LLAMA_REF"
cmake -S "$BUILD/llama" -B "$BUILD/llama-build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=ON -DLLAMA_CURL=ON \
  -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF
cmake --build "$BUILD/llama-build" --target llama-cli llama-server -j "${JOBS:-4}"
cp "$BUILD/llama-build/bin/llama-cli" "$BUILD/llama-build/bin/llama-server" "$DEST/usr/local/bin/"
cp "$BUILD/llama/LICENSE" "$DEST/usr/local/share/aios/LLAMA-LICENSE"
printf '%s\n' "$LLAMA_REF" > "$DEST/usr/local/share/aios/llama-revision"
git config --global --add safe.directory "$BUILD/whisper"
if [ ! -d "$BUILD/whisper/.git" ]; then
  git init "$BUILD/whisper"
  git -C "$BUILD/whisper" remote add origin https://github.com/ggml-org/whisper.cpp.git
fi
if ! git -C "$BUILD/whisper" cat-file -e "$WHISPER_REF^{commit}" 2>/dev/null; then
  git -C "$BUILD/whisper" fetch --depth 1 origin "$WHISPER_REF"
fi
git -C "$BUILD/whisper" checkout --detach "$WHISPER_REF"
cmake -S "$BUILD/whisper" -B "$BUILD/whisper-build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DBUILD_SHARED_LIBS=OFF \
  -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON
cmake --build "$BUILD/whisper-build" --target whisper-cli -j "${JOBS:-4}"
cp "$BUILD/whisper-build/bin/whisper-cli" "$DEST/usr/local/bin/"
cp "$BUILD/whisper/LICENSE" "$DEST/usr/local/share/aios/WHISPER-LICENSE"
printf '%s\n' "$WHISPER_REF" > "$DEST/usr/local/share/aios/whisper-revision"
# Copy application files after the long compiler step so incremental builds use
# the current frontend/backend together.
mkdir -p "$DEST/usr/local/share/aios/aios"
cp "$ROOT/apps/aios/"*.py "$ROOT/apps/aios/models.json" "$DEST/usr/local/share/aios/aios/"
python3 "$ROOT/scripts/stage-model.py" "$BUILD" "$DEST"
