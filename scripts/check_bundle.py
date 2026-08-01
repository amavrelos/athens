"""Post-build assertions on the PyInstaller output: the datas actually landed.

Run from the repo root after `python -m PyInstaller Athens.spec` (the bundle
job in .github/workflows/ci.yml). Every path here is load-bearing, not a
sample: shell._web_dir() resolves sys._MEIPASS/athens/ui/web, the DAW
installers copy out of reaper/ and cubase/, and pywebview locates its WebView2
interop DLLs via os.path.dirname(webview.__file__)/lib — a DLL that landed
anywhere else is as missing as one that didn't land. 0.1.1's ERR_FILE_NOT_FOUND
spent a diagnosis cycle on "maybe the datas never landed" precisely because
nothing observed a built bundle; this closes that permanently.
"""
import sys
from pathlib import Path

problems: list[str] = []


def need(path: Path, why: str) -> None:
    if not path.exists():
        problems.append(f"{path}  <- {why}")


def main() -> int:
    dist = Path("dist")

    # ---- the onedir tree (all platforms) ------------------------------------
    root = dist / "Athens" / "_internal"
    exe = dist / "Athens" / ("Athens.exe" if sys.platform.startswith("win")
                             else "Athens")
    need(exe, "the app binary itself")
    need(root / "athens/ui/web/index.html",
         "the page _serve_web serves; missing = the 0.1.1 packaging hypothesis")
    need(root / "athens/ui/web/app.js", "web dir must have recursed, not top-level only")
    need(root / "reaper/roto_fx_feed.lua", "REAPER feed script (datas: reaper/)")
    need(root / "cubase/Melbourne Instruments_Roto-Control.js",
         "Cubase MIDI Remote script (datas: cubase/)")

    # ---- Windows: pywebview's WebView2 pieces -------------------------------
    # collected by webview/__pyinstaller/hook-webview.py; pywebview resolves
    # them at runtime under <package>/lib, so the exact location is the test
    if sys.platform.startswith("win"):
        lib = root / "webview/lib"
        need(lib / "Microsoft.Web.WebView2.Core.dll", "WebView2 interop (hook datas)")
        need(lib / "Microsoft.Web.WebView2.WinForms.dll", "WebView2 interop (hook datas)")
        if not any(root.rglob("WebView2Loader.dll")):
            problems.append(f"{root}/**/WebView2Loader.dll  <- native loader "
                            "(webview/lib/runtimes); without it WebView2 can't start")

    # ---- macOS: the .app wrapper and the Info.plist block from Athens.spec --
    if sys.platform == "darwin":
        app = dist / "Athens.app"
        need(app / "Contents/MacOS/Athens", "bundle executable")
        # PyInstaller 6 splits Contents/Frameworks + Contents/Resources with
        # cross-symlinks — accept the page at either root
        if not any((app / c / "athens/ui/web/index.html").exists()
                   for c in ("Contents/Frameworks", "Contents/Resources")):
            problems.append(f"{app}/Contents/(Frameworks|Resources)"
                            "/athens/ui/web/index.html  <- UI assets in the bundle")
        plist_path = app / "Contents/Info.plist"
        need(plist_path, "bundle metadata")
        if plist_path.exists():
            import plistlib
            plist = plistlib.loads(plist_path.read_bytes())
            if plist.get("CFBundleExecutable") != "Athens":
                problems.append("Info.plist CFBundleExecutable != 'Athens'")
            ats = plist.get("NSAppTransportSecurity", {})
            if ats.get("NSAllowsLocalNetworking") is not True:
                problems.append("Info.plist lost NSAllowsLocalNetworking "
                                "(Athens.spec info_plist block)")
            domains = ats.get("NSExceptionDomains", {})
            for host in ("127.0.0.1", "localhost"):
                if host not in domains:
                    problems.append(f"Info.plist NSExceptionDomains missing {host} "
                                    "(Athens.spec info_plist block)")

    if problems:
        print("bundle check FAILED — missing or wrong:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("bundle check OK: UI assets, DAW scripts"
          + (", WebView2 DLLs" if sys.platform.startswith("win") else "")
          + (", .app Info.plist" if sys.platform == "darwin" else "")
          + " all present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
