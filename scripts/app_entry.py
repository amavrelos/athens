"""Entry point for the packaged .app: straight into the desktop UI.

CLI use stays `roto-reaper ...`; the bundle exists so end users install
nothing — python, deps and web assets all ride inside the .app."""
import sys

from athens.ui.shell import launch

if __name__ == "__main__":
    sys.exit(launch())
