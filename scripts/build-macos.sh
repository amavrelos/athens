#!/bin/sh
# Build the macOS installer: a self-contained Athens.app packaged into a
# drag-to-Applications Athens.dmg. Run this ON macOS (PyInstaller can't
# cross-compile — the Windows build is scripts/build-windows.ps1).
#
#   pip install -e ".[ui,midi,osc,package]"
#   sh scripts/build-macos.sh
#
# Results:
#   dist/Athens.app                 (self-contained app — python + deps + UI inside)
#   dist/Athens-<version>.dmg       (the installer)
set -e
cd "$(dirname "$0")/.."

# the interpreter that has PyInstaller: prefer $PYTHON, then the repo's .venv,
# then `python`, then python3 — so a bare run uses the venv without activating it
if [ -n "$PYTHON" ]; then PY="$PYTHON"
elif [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif command -v python >/dev/null 2>&1; then PY=python
else PY=python3
fi
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
    echo "error: PyInstaller not found in '$PY'." >&2
    echo "  run:  pip install -e \".[ui,midi,osc,package]\"   (into the venv)" >&2
    echo "  or:   PYTHON=/path/to/python sh scripts/build-macos.sh" >&2
    exit 1
fi

VERSION=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')
# arch-suffixed: CI builds arm64 (macos-latest) AND x86_64 (macos-13); the two
# DMGs must not collide as release assets
DMG="dist/Athens-${VERSION}-$(uname -m).dmg"

echo "== Athens ${VERSION} — macOS build =="

# (re)build Athens.icns from the versioned iconset (source of truth)
iconutil -c icns packaging/Athens.iconset -o packaging/Athens.icns

"$PY" -m PyInstaller --noconfirm --clean Athens.spec

# ad-hoc sign for a stable identity, so the Accessibility grant attaches to
# "Athens" rather than the launching terminal
codesign --force --deep -s - dist/Athens.app

echo "== packaging ${DMG} =="
rm -f "$DMG"
STAGE=$(mktemp -d)
cp -R dist/Athens.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Athens ${VERSION}" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "built:     dist/Athens.app"
echo "installer: ${DMG}"
