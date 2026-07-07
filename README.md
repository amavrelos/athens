# Athens

Athens lets **Cubase**, **Reaper**, and **Pro Tools** talk to Melbourne Instruments' Roto-Control.

> **Very alpha — use at your own risk.** It can't really do any harm to either your Roto-Control or your DAW, but in any case, you've been warned!

## Why Athens?

As of right now, the official Roto-Control integration covers Ableton, Logic, and Bitwig. Athens adds **Cubase** and **Reaper** as first-class citizens — and **Pro Tools** as... second? (More on that below.)

What it does is act as a "Man in the Middle" (MITM), providing a translation service between the Roto-Control and your DAW.

Reaper was the first DAW I tried to support, and the way it works turned out to be much closer to Logic Pro than to Ableton or Bitwig. So Logic Pro became the "language" that Athens speaks to the Roto-Control — whether you're running Reaper or Cubase underneath. (Yes, I named it Athens!)

## Architecture

Classic frontend/backend split:

```
Roto-Control  <--- Logic Pro protocol --->  [ Athens ]  <--- translation --->  Reaper / Cubase / Pro Tools
```

## DAW support

Reaper and Cubase support all the Logic Pro capabilities — or at least everything I've reverse-engineered off the 3.2 firmware.

- **Reaper** needs a Lua script to run. It was the easiest DAW to get going, with a few quirks nevertheless.
- **Cubase** is a different beast (JavaScript?... really?).
- **Pro Tools** — honestly, I have no idea about Pro Tools. I just downloaded the intro version. I couldn't find much on how to approach it, so I worked with MIX mode over HUI. It's very limited right now — no plugin mode, no VU meters — but it works!

If anyone has more info on how to approach Pro Tools plugin mode, I'd love to hear it.

## Install

Grab the build for your OS from the [**Releases**](../../releases) page and run it — Python, the UI, and every dependency are bundled inside, so there's nothing else to install.

Since this is alpha the builds aren't signed/notarized yet, so each OS throws a scary-but-harmless warning the first time:

- **macOS** — download `Athens-<version>-<arch>.dmg` (`arm64` = Apple Silicon, `x86_64` = Intel), drag **Athens** into Applications, then **right-click → Open → Open** (a plain double-click gets blocked). If it says *"Athens is damaged"*, that's just the download quarantine — clear it and reopen:
  ```
  xattr -dr com.apple.quarantine /Applications/Athens.app
  ```
- **Windows** — run `Athens-Setup-<version>.exe` (or unzip the portable `Athens-<version>-win.zip` and run `Athens.exe`). At the SmartScreen prompt: **More info → Run anyway**. Athens needs Microsoft's **WebView2 runtime** — the installer points you to it if it's missing.
- **Linux** (untested) — extract `Athens-<version>-linux-<arch>.tar.gz` and run `./Athens/Athens`. Needs system GTK/WebKit and ALSA.

Plug the Roto-Control in over USB, launch Athens, and it'll pick up the device and whichever DAW you're running.

## Setting up your DAW

Reaper and Cubase each need their companion script (the Lua / the JavaScript I moaned about above). **Athens drops them into place automatically every time it starts**, so normally there's nothing to do.

If that didn't take — locked-down permissions, a portable install, a folder in a weird spot — there are two fallbacks.

**From inside Athens** — **Settings → DAW scripts**:
- **Reinstall** — force-copies the script again (fixes a missing or edited one).
- **Locate…** — point Athens at your DAW's folder if it lives somewhere non-standard.

Then reload it in the DAW (see the note at the end).

**By hand** — the scripts live in this repo:

*Reaper* — `reaper/roto_fx_feed.lua` (plus the optional `reaper/roto_fx_toggle.lua`):
1. **Options → Show REAPER resource path in explorer/finder** to open Reaper's resource folder.
2. Copy both `.lua` files into its **`Scripts/`** subfolder.
3. **Actions → Show action list… → Load ReaScript…**, pick **`roto_fx_feed.lua`**, and **Run** it — that's the feed Athens listens to. (Add it as a startup action to run it automatically; `roto_fx_toggle.lua` is a one-click on/off for it.)
4. For OSC "Mix" mode, `reaper/roto-reaper.ReaperOSC` is a ready-made surface config — **Preferences → Control/OSC/web → Add → OSC**.

*Cubase* — `cubase/Melbourne Instruments_Roto-Control.js`, which has to land at this **exact** path (the sub-folders and the filename all matter, or Cubase quietly ignores it):
```
Documents/Steinberg/<your Cubase>/MIDI Remote/Driver Scripts/Local/Melbourne Instruments/Roto-Control/Melbourne Instruments_Roto-Control.js
```
(under `~/Documents/…` on macOS, `%USERPROFILE%\Documents\…` on Windows.) Then open **Studio → MIDI Remote Manager** (or relaunch Cubase) so it rescans and finds it.

*Pro Tools* — nothing to copy: it speaks **HUI** over a macOS **IAC bus**. Enable an IAC bus in **Audio MIDI Setup**, then in Pro Tools **Setup → Peripherals → MIDI Controllers** add a **HUI** with Receive From / Send To both set to that bus. Athens binds to the same bus when it launches.

**After Athens updates a script**, the DAW still has to reload it: in Reaper, re-run the `roto_fx_feed` action; in Cubase, refresh the MIDI Remote Manager (or relaunch).

## Plugin linking (AU / VST)

Something I really wanted to add is a link between Roto-Control plugins and their AU/VST counterparts.

The problem: when the Roto-Control "learns" a plugin, it stores it under a name tied to the plugin's format (AU or VST). This drove me crazy at first — in Logic I could see a plugin's parameters, but in Cubase I couldn't, because the same plugin was stored under a different name.

The fix is easy. With Athens you can link an AU/VST (under whatever name you like) to a specific plugin in the Roto-Control. So you always "see" the same plugin no matter which DAW you're accessing it from.

I know it seems like nothing — but I really don't want to relearn my plugins twice, and remember two different sets of knob/button mappings for the same thing.
