# -*- mode: python ; coding: utf-8 -*-
# One build config for all platforms. Driven by scripts/build-macos.sh,
# scripts/build-windows.ps1 and scripts/build-linux.sh. The product is
# "Athens"; the code package is athens.
import os
import sys

_MAC = sys.platform == "darwin"


def _p(rel):
    # anchor paths to THIS spec (SPECPATH), not the invoking shell's CWD, so a
    # `pyinstaller Athens.spec` run from any directory still resolves correctly
    return os.path.join(SPECPATH, rel)  # noqa: F821 - PyInstaller global


if _MAC:
    _ICON = _p("packaging/Athens.icns")
elif sys.platform.startswith("win"):
    _ICON = _p("packaging/Athens.ico")
else:
    _ICON = None                          # linux: no exe icon

a = Analysis(
    [_p("scripts/app_entry.py")],
    pathex=[_p("src")],                   # find the `athens` package without an install
    binaries=[],
    datas=[
        (_p("src/athens/ui/web"), "athens/ui/web"),
        (_p("reaper"), "reaper"),                      # roto_fx_feed.lua + the toggle
        (_p("cubase"), "cubase"),                      # the MIDI Remote script
    ],
    hiddenimports=["mido.backends.rtmidi"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Athens",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                            # UPX breaks macOS codesigning / trips AV on Windows
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[_ICON] if _ICON else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Athens",
)
if _MAC:
    app = BUNDLE(
        coll,
        name="Athens.app",
        icon=_p("packaging/Athens.icns"),
        bundle_identifier="com.nodalpoint.athens",
    )
