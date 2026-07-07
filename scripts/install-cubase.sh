#!/bin/sh
# MANUAL FALLBACK — Athens auto-installs/updates this script on every launch
# (src/athens/daw/script_install.py); use this only to install without running
# the app. Cubase only loads the MIDI Remote script from an exact path:
#
#   <Driver Scripts>/Local/<Vendor>/<Device>/<Vendor>_<Device>.js
#
# where <Vendor>/<Device> match the makeDeviceDriver() call — a differently-named
# file is silently ignored. This copies the repo source to that exact name in
# every Steinberg host under ~/Documents/Steinberg.
#
#   sh scripts/install-cubase.sh
#
# Then create a virtual MIDI port pair named "roto-bridge" (macOS: Audio MIDI
# Setup -> IAC Driver -> add a "roto-bridge" port) and run:
#   roto-reaper ui --daw cubase
set -e
cd "$(dirname "$0")/.."

# MUST match makeDeviceDriver('<VENDOR>', '<DEVICE>', ...) in the script below.
VENDOR="Melbourne Instruments"
DEVICE="Roto-Control"
SRC="cubase/${VENDOR}_${DEVICE}.js"   # repo file shares the install name

[ -f "$SRC" ] || { echo "error: $SRC not found (run from the repo root)"; exit 1; }

found=0
for base in "$HOME"/Documents/Steinberg/*/"MIDI Remote/Driver Scripts"; do
    [ -d "$base" ] || continue        # unmatched glob / not a host folder
    found=1
    dest="$base/Local/$VENDOR/$DEVICE"
    mkdir -p "$dest"
    cp "$SRC" "$dest/${VENDOR}_${DEVICE}.js"
    echo "installed: $dest/${VENDOR}_${DEVICE}.js"
done

if [ "$found" -eq 0 ]; then
    echo "No Steinberg 'Driver Scripts' folder found under ~/Documents/Steinberg."
    echo "Open Cubase or Nuendo once to create it, then re-run this script."
    exit 1
fi

echo
echo "Done. In Cubase: MIDI Remote Manager -> Scripts tab -> refresh (circular"
echo "arrow). If it still doesn't appear, quit and relaunch Cubase — it scans"
echo "the Driver Scripts folder at startup."
echo "It lists as: Roto-Control / Melbourne Instruments / Athens."
