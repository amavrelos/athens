#!/bin/sh
# Linux build: the onedir app folder packed as a tar.gz. Run ON Linux
# (PyInstaller can't cross-compile). NOTE: untested against real DAWs; the UI
# needs system GTK/WebKit (pywebview) and MIDI needs ALSA at runtime.
#
#   pip install -e ".[ui,midi,osc,package]"
#   sh scripts/build-linux.sh
#
# Result: dist/Athens-<version>-linux-<arch>.tar.gz
set -e
cd "$(dirname "$0")/.."

if [ -n "$PYTHON" ]; then PY="$PYTHON"
elif [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif command -v python >/dev/null 2>&1; then PY=python
else PY=python3
fi
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
    echo "error: PyInstaller not found in '$PY' — pip install -e \".[ui,midi,osc,package]\"" >&2
    exit 1
fi

VERSION=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
TARBALL="dist/Athens-${VERSION}-linux-$(uname -m).tar.gz"

echo "== Athens ${VERSION} — Linux build =="
"$PY" -m PyInstaller --noconfirm --clean Athens.spec

rm -f "$TARBALL"
tar -C dist -czf "$TARBALL" Athens

echo
echo "built: ${TARBALL}   (run: ./Athens/Athens)"
