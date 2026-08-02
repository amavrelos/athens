# Cubase setup

Cubase needs **two** things, not one:

1. the **MIDI Remote script** (Athens installs this for you), and
2. a **virtual MIDI port pair called `roto-bridge`**, which you have to create yourself.

The second one is the step people miss. The script and Athens talk to each other
*through* that port — without it the script loads but never binds, Athens never
hears Cubase, and you end up staring at a DAW indicator that says something else
entirely. Athens now tells you when the port is missing, but it can't create it
for you.

## 1. Create the `roto-bridge` port

### macOS

Audio MIDI Setup → **Window → Show MIDI Studio** → double-click **IAC Driver** →
tick **Device is online** → add a port named `roto-bridge`.

macOS shows it as "IAC Driver roto-bridge". That's fine — both Athens and the
script match by substring, case-insensitively.

### Windows

Windows has no built-in virtual MIDI, so there's nothing to switch on — you need
a small free utility. Install
[loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html):

```
winget install --id TobiasErichsen.loopMIDI --exact --silent --accept-package-agreements --accept-source-agreements
```

Then open loopMIDI, type `roto-bridge` into the **New port-name** field, and
click **+**.

**Now right-click the loopMIDI tray icon and enable "Autostart loopMIDI".** This
isn't optional housekeeping: loopMIDI's ports only exist while loopMIDI is
running, so without autostart the port disappears at your next reboot and Cubase
silently stops being detected all over again.

## 2. The script

Athens installs and updates `Melbourne Instruments_Roto-Control.js` into every
Steinberg host it finds, every time it launches. Normally there's nothing to do.

If you'd rather place it by hand, it has to land at this **exact** path — the
folder names and the filename all matter, or Cubase quietly ignores it:

**macOS**
```
~/Documents/Steinberg/<your Cubase>/MIDI Remote/Driver Scripts/Local/Melbourne Instruments/Roto-Control/Melbourne Instruments_Roto-Control.js
```

**Windows**
```
%USERPROFILE%\Documents\Steinberg\<your Cubase>\MIDI Remote\Driver Scripts\Local\Melbourne Instruments\Roto-Control\Melbourne Instruments_Roto-Control.js
```

(`scripts/install-cubase.sh` does this for you on macOS and Linux. It's a POSIX
shell script — it does not run on Windows.)

## 3. Point Cubase at it

Open **Studio → MIDI Remote Manager**. The script lists as *Roto-Control /
Melbourne Instruments / Athens* and binds to the `roto-bridge` ports on its own.
If it isn't there, hit refresh — and if it's still not there, relaunch Cubase,
which rescans the Driver Scripts folder at startup.

## 4. Start Athens

That's it. Athens detects Cubase automatically; you don't need to tell it which
DAW you're on.

## Which Cubase?

Cubase 12 and up, since that's where the MIDI Remote API arrives. Mixer,
transport, track names and selection all work from 12.

**Cubase 14+ gets you full plugin parameters.** Athens reads a plugin's whole
parameter list through the DirectAccess API, which only exists in 14 and later.
On 12 and 13 the plugin view still works, but you only see the first 8
parameters — that's the ceiling of the older API, not a bug.

## When it doesn't work

**Athens shows the wrong DAW, or "no DAW detected".** Almost always the missing
`roto-bridge` port — go back to step 1. Athens shows you the specific remedy for
your platform when it spots this.

**It worked yesterday, now Cubase isn't detected (Windows).** Your loopMIDI port
is gone because loopMIDI isn't running. Enable Autostart (step 1).

**The port exists in loopMIDI but nothing lists it (Windows).** The newer Windows
MIDI Services stack had a bug where dynamically-created ports weren't always
visible to applications. The fix has been rolling out since April 2026 — update
Windows. Enabling Autostart helps here too, since the port then exists from logon
rather than appearing mid-session.

**Plugin parameters move on their own, or the wrong parameter jumps.** Check your
MIDI routing before you suspect Athens. Cubase's **All MIDI Inputs** includes the
Roto-Control and the `roto-bridge` ports by default, so the device's raw MIDI
lands on your record-armed track and whatever CC map that plugin has picks it up
directly. Go to **Studio → Studio Setup → MIDI Port Setup** and untick **In "All
MIDI Inputs"** for both the Roto-Control ports and `roto-bridge`. This one is
worth doing up front — it looks exactly like a bug in Athens and isn't.

**All your knob values read zero after updating the script.** Reloading a MIDI
Remote script in place can leave Cubase handing out zeros for every parameter.
Athens retries automatically and usually heals within a second or two; if it
doesn't, fully quit and reopen Cubase.

**You updated Athens and things look stale.** Cubase caches the script it loaded
at startup, so a newly-installed version isn't live until it reloads. Athens
notices this and tells you to restart Cubase — that prompt is worth believing.
