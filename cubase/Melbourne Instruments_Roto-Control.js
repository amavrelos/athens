// Athens — Cubase MIDI Remote script (ES5).
//
// Streams Cubase session state to Athens over the `roto-bridge` virtual MIDI
// pair, and applies Athens' control back, using the contract in
// src/roto_reaper/daw/cubase_contract.py:  F0 7D <cmd> <payload...> F7
// (14-bit indices/values, MSB first). tests/test_cubase_source.py is the spec
// this must satisfy — the Python source decodes exactly these frames.
//
// Direction:
//   Cubase -> Athens : the mOn...Change callbacks send contract frames out.
//   Athens -> Cubase : midiInput.mOnSysex parses frames and pushes values in
//                      via surfaceValue.setProcessValue().
//
// Install (the INSTALLED filename MUST be <Vendor>_<Device>.js or Cubase won't
// discover it — the folder + filename must match the makeDeviceDriver() args):
//   ~/Documents/Steinberg/Cubase/MIDI Remote/Driver Scripts/Local/
//       Melbourne Instruments/Roto-Control/Melbourne Instruments_Roto-Control.js
// and create a virtual MIDI port pair named `roto-bridge` (macOS: IAC bus).

var midiremote_api = require('midiremote_api_v1')

//=============================================================================
// contract — mirror of cubase_contract.py
//=============================================================================
// BUMP on EVERY change to this file. The running script announces this in its
// HELLO handshake; Athens compares it to the copy it bundles and, on a
// mismatch, prompts you to restart Cubase (the host caches the script it
// loaded, so an on-disk update isn't live until reload). Edit below? Bump this.
var SCRIPT_VERSION = '8'
// identity + version in one handshake token: "cubase <version>". A pre-version
// script sent just "cubase"; Athens reads a missing version as an older build.
var HELLO_ID = 'cubase ' + SCRIPT_VERSION
var ID = 0x7D
// direction byte — a single IAC pair loops back, so tag every frame and skip
// our own echo (see cubase_contract.py). Cubase acts only on TO_CUBASE.
var DIR = { TO_CUBASE: 0x00, TO_ATHENS: 0x01 }
var CMD = {
    COUNT: 0x01, NAME: 0x02, VOLUME: 0x03, PAN: 0x04, FLAG: 0x05,
    SELECT: 0x06, TRANSPORT: 0x07, HELLO: 0x08, VU: 0x09, PAGE: 0x0A,
    DEVICE_COUNT: 0x10, DEVICE_NAME: 0x11, DEVICE_ENABLED: 0x12,
    FOCUS_DEVICE: 0x13, PARAM_COUNT: 0x14, PARAM_NAME: 0x15,
    PARAM_VALUE: 0x16, PARAM_DISPLAY: 0x17, DIAG: 0x7F
}
var FLAG = { MUTE: 0, SOLO: 1, ARM: 2, MONITOR: 3 }
var NUM_STRIPS = 8          // mix window / plugin param page — the ROTO's knobs
// DirectAccess (MIDI Remote API 1.2+, Cubase 14+) reads a plugin's WHOLE
// parameter list on selection (count + every name/value/display), so Athens'
// identity table does NOT depend on bank bindings. Cap the enumeration so a
// giant plugin can't flood the bridge; 256 == the Logic dialect's learnable
// ceiling (MAX_PLUGIN_PARAMETERS).
var PLUGIN_PARAM_MAX = 256
// WRITE path: DirectAccess by EXACT param index, via the activation pump (see
// the write-pump block below). The parameter BANK keeps exactly PAGE_SIZE
// bindings and is never over-subscribed (32 flat bindings aliased writes
// mod-8) and never paged for writes (its cell order is the plugin's
// Remote-Control-Editor layout — an unobservable permutation of the real
// param list; HW-proven to diverge). The bank's residual jobs: keep the
// subpage binding structure alive and serve as the degraded write window
// (cells 0..7) when DirectAccess writes are unavailable.
var PAGE_SIZE = 8                                // the bank's native width

function u14(v) {
    v = Math.max(0, Math.min(0x3FFF, Math.round(v)))
    return [(v >> 7) & 0x7F, v & 0x7F]
}
function fromU14(a, i) { return (a[i] << 7) | a[i + 1] }
function ascii(s, max) {
    var out = []
    s = String(s); max = max || 32
    for (var i = 0; i < s.length && i < max; ++i) {
        var c = s.charCodeAt(i)
        out.push(c >= 0x20 && c < 0x7F ? c : 0x3F)   // printable ascii or '?'
    }
    return out
}
function frame(cmd, payload) { return [0xF0, ID, DIR.TO_ATHENS, cmd].concat(payload).concat([0xF7]) }

// encoders (0..1 values)
function eCount(n) { return frame(CMD.COUNT, u14(n)) }
function eName(i, s) { return frame(CMD.NAME, u14(i).concat(ascii(s))) }
function eVolume(i, v) { return frame(CMD.VOLUME, u14(i).concat(u14(v * 0x3FFF))) }
function ePan(i, v) { return frame(CMD.PAN, u14(i).concat(u14(v * 0x3FFF))) }
function eVu(i, v) { return frame(CMD.VU, u14(i).concat(u14(v * 0x3FFF))) }
function eFlag(i, f, on) { return frame(CMD.FLAG, u14(i).concat([f, on ? 1 : 0])) }
function eSelect(i) { return frame(CMD.SELECT, u14(i)) }
function eTransport(bits) { return frame(CMD.TRANSPORT, [bits & 0x7F]) }
function eHello(tag) { return frame(CMD.HELLO, ascii(tag)) }
function eDiag(s) { return frame(CMD.DIAG, ascii(s, 110)) }   // debug: block status over the bridge
function eDevCount(n) { return frame(CMD.DEVICE_COUNT, u14(n)) }
function eDevName(i, s) { return frame(CMD.DEVICE_NAME, u14(i).concat(ascii(s))) }
function eDevEnabled(i, on) { return frame(CMD.DEVICE_ENABLED, u14(i).concat([on ? 1 : 0])) }
function eFocus(i) { return frame(CMD.FOCUS_DEVICE, u14(i)) }
function eParamCount(n) { return frame(CMD.PARAM_COUNT, u14(n)) }
function eParamName(s, t) { return frame(CMD.PARAM_NAME, u14(s).concat(ascii(t))) }
function eParamValue(s, v) { return frame(CMD.PARAM_VALUE, u14(s).concat(u14(v * 0x3FFF))) }
function eParamDisplay(s, t) { return frame(CMD.PARAM_DISPLAY, u14(s).concat(ascii(t))) }

//=============================================================================
// driver + ports
//=============================================================================
// NOTE: these two names MUST match the install folders exactly:
//   .../Driver Scripts/Local/Melbourne Instruments/Roto-Control/
// and the file should be  Melbourne Instruments_Roto-Control.js  (vendor_device.js).
// ('roto-bridge' below is the VIRTUAL MIDI PORT name, a separate thing.)
var driver = midiremote_api.makeDeviceDriver('Melbourne Instruments',
                                             'Roto-Control', 'Athens')
var midiInput = driver.mPorts.makeMidiInput()
var midiOutput = driver.mPorts.makeMidiOutput()

// macOS presents an IAC port as "IAC Driver <portname>"; also accept the bare
// name in case a setup exposes it that way. Add more units if yours differs.
driver.makeDetectionUnit().detectPortPair(midiInput, midiOutput)
    .expectInputNameEquals('IAC Driver roto-bridge')
    .expectOutputNameEquals('IAC Driver roto-bridge')
driver.makeDetectionUnit().detectPortPair(midiInput, midiOutput)
    .expectInputNameEquals('roto-bridge')
    .expectOutputNameEquals('roto-bridge')

function send(ctx, bytes) { midiOutput.sendMidi(ctx, bytes) }

//=============================================================================
// surface — virtual binding anchors (not physical; Athens IS the surface)
//=============================================================================
var surface = driver.mSurface
var mixKnobs = [], panKnobs = [], muteBtns = [], soloBtns = [],
    selBtns = [], paramKnobs = [], vuAnchors = []
// The MIDI Remote API only drives a surface value's mOn*Change callbacks when it
// has an mMidiBinding — every docs example binds one, and that's what makes
// mOnProcessValueChange fire on host changes; without it our values are inert.
// Bind each to a dummy CC on the bridge INPUT only (no output port -> the API
// emits no CC of its own; our SysEx carries the values). The CCs are never sent;
// they only "arm" the value.
function armCC(el, cc) {
    el.mSurfaceValue.mMidiBinding.setInputPort(midiInput).bindToControlChange(0, cc)
    return el
}
// same, on an explicit channel — the plugin param anchors past the 8 visible
// knobs live on channel 1 so they don't exhaust channel 0's CC map
function armCCch(el, ch, cc) {
    el.mSurfaceValue.mMidiBinding.setInputPort(midiInput).bindToControlChange(ch, cc)
    return el
}
for (var i = 0; i < NUM_STRIPS; ++i) {
    mixKnobs.push(armCC(surface.makeKnob(i, 0, 1, 1), i))           // volume CC 0-7
    panKnobs.push(armCC(surface.makeKnob(i, 4, 1, 1), 8 + i))       // pan    CC 8-15
    muteBtns.push(armCC(surface.makeButton(i, 1, 1, 1), 16 + i))   // mute   CC 16-23
    soloBtns.push(armCC(surface.makeButton(i, 2, 1, 1), 24 + i))   // solo   CC 24-31
    paramKnobs.push(armCC(surface.makeKnob(i, 3, 1, 1), 32 + i))   // param  CC 32-39
    selBtns.push(armCC(surface.makeButton(i, 5, 1, 1), 56 + i))   // select CC 56-63
    vuAnchors.push(armCC(surface.makeKnob(i, 6, 1, 1), 64 + i))   // VU     CC 64-71
}
var playBtn = armCC(surface.makeButton(9, 0, 1, 1), 48)
var recBtn = armCC(surface.makeButton(10, 0, 1, 1), 49)
var loopBtn = armCC(surface.makeButton(11, 0, 1, 1), 50)
var pageNext = armCC(surface.makeButton(12, 0, 1, 1), 72)   // bank paging
var pagePrev = armCC(surface.makeButton(13, 0, 1, 1), 73)

//=============================================================================
// mapping — bind the anchors to host values on a single always-on page
//=============================================================================
var page = driver.mMapping.makePage('roto')
var host = page.mHostAccess

// Each block is guarded so ONE wrong host-API call can't stop the whole script
// loading — it logs and the rest keeps working. Per-block status also rides the
// bridge on a WHO probe (console.log is unreliable in Cubase 14).
var DIAG = { active: 'no' }
function guard(name, fn) {
    try { fn(); DIAG[name] = 'ok' }
    catch (e) { DIAG[name] = 'FAIL: ' + e; console.log('roto: ' + name + ' -> ' + DIAG[name]) }
}

// --- mixer: a bank of NUM_STRIPS channels ---
// The bank's absolute offset isn't reported by the API, so we stream strip index
// (0..7) as the track index — correct for <=8 tracks; larger projects page.
guard('mixer', function () {
    // Exclude in/out channels (per the Steinberg docs example).
    // includeAudioChannels/includeInstrumentChannels DON'T EXIST in this API.
    var bank = host.mMixConsole.makeMixerBankZone()
        .excludeInputChannels().excludeOutputChannels()
    var vuLast = []
    for (var s = 0; s < NUM_STRIPS; ++s) {
        (function (strip) {
            var ch = bank.makeMixerBankChannel()
            page.makeValueBinding(mixKnobs[strip].mSurfaceValue, ch.mValue.mVolume)
            var vol = mixKnobs[strip].mSurfaceValue
            // surface-value callbacks: (ctx, newValue, oldValue) / (ctx, objTitle, valTitle)
            vol.mOnProcessValueChange = function (ctx, v) { trackVol[strip] = v; send(ctx, eVolume(strip, v)) }
            // KNOWN Cubase-engine caveat (forum: "mOnTitleChange broken for
            // some bindings", since ~12.0.60): this callback on a
            // MixerBankChannel volume binding can stop firing after
            // mNextBank/mPrevBank — values keep flowing but names go stale on
            // the new window. Host-side defect; if it bites, re-select a track
            // to refresh names (values/mutes/selection are unaffected).
            vol.mOnTitleChange = function (ctx, o) { trackName[strip] = o; send(ctx, eName(strip, o)) }
            // pan / mute / solo / select / VU each bind INDEPENDENTLY — a quirk
            // in one must not skip the others. Per-feature status lands in DIAG,
            // so a WHO probe reports which bindings the host API accepted.
            guard('pan', function () {
                page.makeValueBinding(panKnobs[strip].mSurfaceValue, ch.mValue.mPan)
                panKnobs[strip].mSurfaceValue.mOnProcessValueChange = function (ctx, v) {
                    trackPan[strip] = v; send(ctx, ePan(strip, v)) }
            })
            guard('mute', function () {
                page.makeValueBinding(muteBtns[strip].mSurfaceValue, ch.mValue.mMute).setTypeToggle()
                muteBtns[strip].mSurfaceValue.mOnProcessValueChange = function (ctx, v) {
                    trackMute[strip] = v >= 0.5; send(ctx, eFlag(strip, FLAG.MUTE, v >= 0.5)) }
            })
            guard('solo', function () {
                page.makeValueBinding(soloBtns[strip].mSurfaceValue, ch.mValue.mSolo).setTypeToggle()
                soloBtns[strip].mSurfaceValue.mOnProcessValueChange = function (ctx, v) {
                    trackSolo[strip] = v >= 0.5; send(ctx, eFlag(strip, FLAG.SOLO, v >= 0.5)) }
            })
            guard('select', function () {
                // selection: fire only for the newly-SELECTED track (v -> 1)
                page.makeValueBinding(selBtns[strip].mSurfaceValue, ch.mValue.mSelected)
                selBtns[strip].mSurfaceValue.mOnProcessValueChange = function (ctx, v) {
                    if (v >= 0.5) send(ctx, eSelect(strip)) }
            })
            guard('vu', function () {
                // VU meter, throttled — skip sub-2% wiggles so it can't flood
                page.makeValueBinding(vuAnchors[strip].mSurfaceValue, ch.mValue.mVUMeter)
                vuAnchors[strip].mSurfaceValue.mOnProcessValueChange = function (ctx, v) {
                    if (Math.abs(v - (vuLast[strip] || 0)) < 0.02) return
                    vuLast[strip] = v; send(ctx, eVu(strip, v)) }
            })
        })(s)
    }
    // paging: the ROTO arrows scroll Cubase's 8-channel bank window
    guard('paging', function () {
        page.makeActionBinding(pageNext.mSurfaceValue, bank.mAction.mNextBank)
        page.makeActionBinding(pagePrev.mSurfaceValue, bank.mAction.mPrevBank)
    })
})

// --- transport ---
var tState = { play: false, rec: false, loop: false }
function sendTransport(ctx) {
    send(ctx, eTransport((tState.play ? 1 : 0) | (tState.rec ? 2 : 0) | (tState.loop ? 4 : 0)))
}
guard('transport', function () {
    page.makeValueBinding(playBtn.mSurfaceValue, host.mTransport.mValue.mStart).setTypeToggle()
    page.makeValueBinding(recBtn.mSurfaceValue, host.mTransport.mValue.mRecord).setTypeToggle()
    page.makeValueBinding(loopBtn.mSurfaceValue, host.mTransport.mValue.mCycleActive).setTypeToggle()
    playBtn.mSurfaceValue.mOnProcessValueChange = function (ctx, v) { tState.play = v >= 0.5; sendTransport(ctx) }
    recBtn.mSurfaceValue.mOnProcessValueChange = function (ctx, v) { tState.rec = v >= 0.5; sendTransport(ctx) }
    loopBtn.mSurfaceValue.mOnProcessValueChange = function (ctx, v) { tState.loop = v >= 0.5; sendTransport(ctx) }
})

// --- plugin: the SELECTED track's plugins — switch WITHOUT changing track.
//     Slot 0 = the track's INSTRUMENT; slots 1.. = insert effects. Each is a real
//     plugin slot (mParameterBankZone + mOnChangePluginIdentity) on its own
//     subpage, so FOCUS_DEVICE(idx) absolute-selects it and its param bank hits
//     the knobs — covers instrument tracks, not just insert chains. ---
var NUM_INSERTS = 8
var insertNames = []          // slot -> plugin name, for the ROTO's plugin list
var insertActs = []           // slot -> hidden button that activates its subpage
var insertDA = []             // slot -> DirectAccess handle (null on Cubase <=13)
var dropWarned = {}           // writes we already flagged as un-drivable (DIAG dedupe)
// --- DirectAccess write pump state (the write path) ---
var daWriteQueue = {}         // absIdx -> value (latest wins) awaiting a drain
var daPumpBusy = false        // an activation pulse is in flight to drain it
var daPumpTs = 0              // when it was pulsed (watchdog re-pulses if lost)
var daObjCache = []           // slot -> {id, n} params-object (from enumeration)
var daValuesBad = false       // last enumeration read ALL-ZERO values (degenerate
//                               DA state) — value flood withheld, retry armed
var daValuesRetries = 0       // bounded idle-driven retries of the above
var daValuesLastTry = 0       // clock() of the last retry (backoff pacing)
var currentSlot = 0           // which plugin subpage is live (restored on activate)
// clock for the pump watchdog — Cubase's JS engine has Date but no timers; the
// fallback (paranoia) advances per call so deadlines still eventually pass
var clock = (typeof Date !== 'undefined' && Date.now)
    ? function () { return Date.now() }
    : (function () { var t = 0; return function () { return (t += 50) } })()
var pluginParamCount = NUM_STRIPS  // focused-plugin param count for eParamCount;
//                                    DirectAccess keeps it honest, else it stays at
//                                    the 8 visible knobs (unchanged fallback).
// makeDirectAccess exists only on API 1.2+ (Cubase 14+); gate every DA call on it.
var HAS_DIRECT_ACCESS = !!host.makeDirectAccess
// last-known mixer state per strip so the HELLO handshake can replay it on a
// mid-session connect (the mixer callbacks are change-only). Mirrors insertNames.
var trackName = [], trackVol = [], trackPan = [], trackMute = [], trackSolo = []

// Replay the cached mixer onto a (re)connecting Athens — eCount seeds the strips,
// then every known name/volume/pan/mute/solo is re-sent, so the ROTO fills in
// with NO gesture in Cubase. The mixer analogue of pushInsertList.
function pushMixer(ctx) {
    send(ctx, eCount(NUM_STRIPS))
    for (var s = 0; s < NUM_STRIPS; ++s) {
        if (trackName[s] != null) send(ctx, eName(s, trackName[s]))
        if (trackVol[s] != null) send(ctx, eVolume(s, trackVol[s]))
        if (trackPan[s] != null) send(ctx, ePan(s, trackPan[s]))
        if (trackMute[s] != null) send(ctx, eFlag(s, FLAG.MUTE, trackMute[s]))
        if (trackSolo[s] != null) send(ctx, eFlag(s, FLAG.SOLO, trackSolo[s]))
    }
}

//=============================================================================
// DirectAccess WRITE PUMP — drive ANY param by its EXACT DA/VST index.
//
// Why this shape: the bank's cell order is the plugin's Remote-Control-Editor
// layout — an arbitrary permutation of the real param list (HW-proven: page-18
// cells held different params than DA 144-151) that cannot be observed (bank
// titles never fire). So writes must NOT go through the bank at all.
// setParameterProcessValue writes by exact index, but needs a LIVE
// activeMapping — mOnSysex has none, and a cached one is a silent no-op
// (HW-proven). mOnActivate DOES get a live mapping (our DA enumeration runs in
// it, HW-proven working) — and pulsing sub.mAction.mActivate re-fires it ON
// DEMAND, even for the already-active slot (HW-proven: repeated enumerations).
// So: queue the write, pulse the activation, drain the queue inside
// mOnActivate with its fresh mapping. Self-clocking: drains re-pulse while
// writes keep arriving; values coalesce per param (latest wins).
//=============================================================================
function writeParam(ctx, absIdx, v) {
    daWriteQueue[absIdx] = v
    pumpDAWrites(ctx)
}

function pumpDAWrites(ctx) {
    if (daPumpBusy) return                 // a drain is in flight; it re-pumps
    var act = insertActs[currentSlot]
    if (!act || !HAS_DIRECT_ACCESS) {      // no pump possible on this host
        drainFallback(ctx)
        return
    }
    daPumpBusy = true
    daPumpTs = clock()
    act.mSurfaceValue.setProcessValue(ctx, 1)
    act.mSurfaceValue.setProcessValue(ctx, 0)
}

function pumpWatchdog(ctx) {
    // an activation pulse can in principle get lost — don't wedge the queue
    if (daPumpBusy && clock() - daPumpTs > 1500) {
        daPumpBusy = false
        pumpDAWrites(ctx)
    }
    // all-zero enumeration recovery: re-pulse the activation to re-enumerate.
    // Ticked by mOnIdle AND every inbound frame, so the first retry lands in
    // ~200ms (then 1s spacing) — the old keepalive hook only fired after 4s+
    // of wire silence, which stretched the heal to ~12s on hardware.
    if (daValuesBad && daValuesRetries < 10 && !daPumpBusy
            && insertActs[currentSlot]
            && clock() - daValuesLastTry > (daValuesRetries === 0 ? 200 : 1000)) {
        daValuesRetries++
        daValuesLastTry = clock()
        insertActs[currentSlot].mSurfaceValue.setProcessValue(ctx, 1)
        insertActs[currentSlot].mSurfaceValue.setProcessValue(ctx, 0)
    }
}

function drainDAWrites(ctx, mapping, idx) {
    var q = daWriteQueue
    daWriteQueue = {}
    var keys = [], k
    for (k in q) keys.push(k)
    if (keys.length === 0) return
    var da = insertDA[idx]
    if (!da || mapping == null || !da.setParameterProcessValue) {
        daWriteQueueRestoreFallback(ctx, q, keys)
        return
    }
    try {
        da.activate(mapping)
        var t = daObjCache[idx]
        if (!t) t = daObjCache[idx] = daParamsObject(da, mapping)
        for (var i = 0; i < keys.length; ++i) {
            var abs = +keys[i]
            if (abs >= t.n) continue
            var tag = da.getParameterTagByIndex(mapping, t.id, abs)
            da.setParameterProcessValue(mapping, t.id, tag, q[keys[i]])
            // ECHO the applied value back: with the bank callbacks muted this
            // is Athens' ONLY live value feed — without it its cache freezes at
            // enumeration time and every plugin re-entry floods STALE values
            // (knobs anchor to old positions). Also the DAW-confirm echo the
            // device's haptics expect.
            send(ctx, eParamValue(abs, q[keys[i]]))
            var dd = da.getParameterDisplayValue(mapping, t.id, tag)
            if (dd) send(ctx, eParamDisplay(abs, dd))
        }
    } catch (e) {
        if (!dropWarned.dawrite) {
            dropWarned.dawrite = true
            send(ctx, eDiag('DirectAccess write failed (' + e + ') — falling '
                + 'back to the bank window'))
        }
        daWriteQueueRestoreFallback(ctx, q, keys)
    }
}

function daWriteQueueRestoreFallback(ctx, q, keys) {
    // Degraded mode (no DA writes on this host): only the bank's native
    // window is safely writable — its cell order past that is an unknowable
    // permutation, and a wrong-param write is the one unforgivable outcome.
    for (var i = 0; i < keys.length; ++i) {
        var abs = +keys[i]
        if (abs < PAGE_SIZE) {
            paramKnobs[abs].mSurfaceValue.setProcessValue(ctx, q[keys[i]])
        } else if (!dropWarned[abs]) {
            dropWarned[abs] = true
            send(ctx, eDiag('param ' + abs + ' not drivable without '
                + 'DirectAccess writes'))
        }
    }
}

function drainFallback(ctx) {
    var q = daWriteQueue
    daWriteQueue = {}
    var keys = [], k
    for (k in q) keys.push(k)
    if (keys.length) daWriteQueueRestoreFallback(ctx, q, keys)
}

// DirectAccess enumeration: read the focused plugin's ENTIRE parameter list and
// stream it to Athens (count + name + value + display), so a track/plugin change
// populates the identity table with no gesture. `mapping` is the activeMapping
// handed to mOnChangePluginIdentity / a subpage mOnActivate — DirectAccess needs
// it to resolve the live object. Fully guarded: any DA hiccup returns false and
// the reactive bank callbacks (still wired below) carry on exactly as before.
// DirectAccess object resolution: getBaseObjectID on an INSTRUMENT slot lands
// on the slot's host wrapper — 3 params ('Freeze', 'Activate Output',
// 'Extract Sound...'), NOT the synth (HW-confirmed: SQ3 enumerated as those 3).
// The synth is a CHILD object. Walk the child tree (breadth-first, bounded)
// and take the object with the MOST parameters — the wrapper never wins.
// Feature-checked: pre-1.3 builds without child introspection keep the base.
function daParamsObject(da, mapping) {
    var base = da.getBaseObjectID(mapping)
    var bestId = base, bestN = 0
    try { bestN = da.getNumberOfParameters(mapping, base) } catch (e) { bestN = 0 }
    if (!da.getNumberOfChildObjects || !da.getChildObjectID)
        return { id: bestId, n: bestN }
    var queue = [base], guard = 0
    while (queue.length > 0 && guard < 64) {
        guard++
        var id = queue.shift()
        var kids = 0
        try { kids = da.getNumberOfChildObjects(mapping, id) } catch (e2) { kids = 0 }
        for (var k = 0; k < kids; ++k) {
            var cid, n
            try {
                cid = da.getChildObjectID(mapping, id, k)
                n = da.getNumberOfParameters(mapping, cid)
            } catch (e3) { continue }
            if (n > bestN) { bestId = cid; bestN = n }
            queue.push(cid)
        }
    }
    return { id: bestId, n: bestN }
}

function pushDirectParams(idx, mapping, ctx) {
    // ENTERING a (possibly new) plugin context: the previous plugin must not
    // leak into this one — reset the advertised count (a failed enumeration
    // otherwise reports plugin A's count for plugin B) and the drop-warn dedupe.
    pluginParamCount = NUM_STRIPS
    dropWarned = {}
    var da = insertDA[idx]
    if (!da || mapping == null) return false
    try {
        da.activate(mapping)
        var target = daParamsObject(da, mapping)   // the SYNTH, not the wrapper
        daObjCache[idx] = target                    // reused by the write pump
        var objId = target.id
        var n = target.n
        if (!(n > 0)) return false
        if (n > PLUGIN_PARAM_MAX) n = PLUGIN_PARAM_MAX
        pluginParamCount = n
        send(ctx, eParamCount(n))
        var i, tag
        for (i = 0; i < n; ++i) {
            tag = da.getParameterTagByIndex(mapping, objId, i)
            send(ctx, eParamName(i, da.getParameterTitle(mapping, objId, tag, 32)))
        }
        // VALUE pass, separate + defended: a degenerate DA state (seen on HW
        // right after an in-place script reload) returns 0.0 for EVERY param
        // while titles read fine. Flooding those zeros anchors every mapped
        // knob to 0 — worse than no data. Detect (a >8-param plugin with not a
        // single non-zero value is not a real state), retry once, and if still
        // flat send NO values; daValuesBad arms a keepalive-driven retry.
        var nonzero = 0
        for (var attempt = 0; attempt < 2 && nonzero === 0; ++attempt) {
            nonzero = 0
            for (i = 0; i < n; ++i) {
                tag = da.getParameterTagByIndex(mapping, objId, i)
                if (da.getParameterProcessValue(mapping, objId, tag) !== 0)
                    nonzero++
            }
        }
        if (n > 8 && nonzero === 0) {
            daValuesBad = true
            daValuesLastTry = clock()
            send(ctx, eDiag('DA values read all-zero (' + n + ' params) — '
                + 'no value flood; retrying shortly'))
            return true
        }
        daValuesBad = false
        daValuesRetries = 0
        for (i = 0; i < n; ++i) {
            tag = da.getParameterTagByIndex(mapping, objId, i)
            // getParameterProcessValue is normalised 0..1, matching eParamValue.
            send(ctx, eParamValue(i, da.getParameterProcessValue(mapping, objId, tag)))
            var disp = da.getParameterDisplayValue(mapping, objId, tag)
            if (disp) send(ctx, eParamDisplay(i, disp))
        }
        return true
    } catch (e) {
        console.log('roto: DirectAccess enumerate failed -> ' + e)
        return false
    }
}

function pushInsertList(ctx) {
    // report through the LAST populated slot, not up to the first gap: slot 0 is
    // the instrument (empty on audio tracks) and inserts can sit past it, so a
    // contiguous-from-0 count would drop them. Gaps go out as empty names;
    // Athens skips empty-name devices.
    var n = 0
    for (var i = 0; i < NUM_INSERTS; i++) if (insertNames[i]) n = i + 1
    send(ctx, eDevCount(n))
    for (var i = 0; i < n; ++i) send(ctx, eDevName(i, insertNames[i] || ''))
    send(ctx, eParamCount(pluginParamCount))   // real count once DirectAccess ran
}
guard('plugin', function () {
    var chan = host.mTrackSelection.mMixerChannel
    var inserts = chan.mInsertAndStripEffects.mInserts
    var area = page.makeSubPageArea('RotoPlugins')
    // the 8 param-knob surface values are shared across subpages; set callbacks
    // ONCE — they fire for the ACTIVE subpage's binding (the focused plugin).
    for (var q = 0; q < PAGE_SIZE; ++q) {
        (function (col) {
            var sv = paramKnobs[col].mSurfaceValue
            // NO value/display/name emissions from the bank callbacks: the
            // bank's cell order is the RCE permutation, so a cell number is
            // NOT a param index — labeling these would corrupt Athens' table
            // (DirectAccess enumeration is the one authoritative read).
            sv.mOnProcessValueChange = function (ctx, v) {}
            sv.mOnDisplayValueChange = function (ctx, value, units) {}
            sv.mOnTitleChange = function (ctx, o, valTitle) {
                // objectTitle IS the plugin name, and this re-fires when the
                // subpage re-binds (mOnActivate) — UNLIKE the change-only identity
                // callbacks. It's the ONLY signal for an already-loaded plugin on
                // an already-selected track (nothing "changed"), so adopt it.
                if (o && insertNames[currentSlot] !== o) {
                    insertNames[currentSlot] = o
                    pushInsertList(ctx)
                }
            }
        })(q)
    }
    // bind one plugin slot (instrument slot OR insert viewer) to its own subpage
    function bindSlot(idx, pluginSlot) {
        var sub = area.makeSubPage('P' + idx)
        var bank = pluginSlot.mParameterBankZone
        if (bank.setWrapAround) bank.setWrapAround(false)
        // EXACTLY PAGE_SIZE bindings — never over-subscribe the 8-wide bank
        // (32 bindings aliased writes mod-8: the OSC2_mix -> OSC2_octave bleed).
        // The bank is NEVER paged for writes (its cell order is the RCE
        // permutation); it exists to keep the subpage structure + identity
        // callbacks alive and as the degraded cells-0..7 write window.
        for (var q = 0; q < PAGE_SIZE; ++q)
            page.makeValueBinding(paramKnobs[q].mSurfaceValue,
                                  bank.makeParameterValue()).setSubPage(sub)
        // DirectAccess handle for this slot — the authoritative full-param reader
        // (Cubase 14+). Built once at setup, as Steinberg advise; used only inside
        // the callbacks below, with their activeMapping.
        insertDA[idx] = HAS_DIRECT_ACCESS ? (function () {
            try { return host.makeDirectAccess(pluginSlot) } catch (e) { return null }
        })() : null
        pluginSlot.mOnChangePluginIdentity = function (ctx, m, name) {
            insertNames[idx] = name || ''
            // plugin swapped IN this slot: if it's the one on the knobs, forget
            // the bank position (the new plugin re-bound it), drop any queued
            // write (stale intent for the OLD plugin) and re-read the full list.
            if (idx === currentSlot) {
                daObjCache[idx] = null     // new plugin: stale DA object handle
                daWriteQueue = {}          //  and any queued intent is stale too
                daValuesBad = false        // fresh context: reset the retry arm
                daValuesRetries = 0
                pushDirectParams(idx, m, ctx)
            }
            pushInsertList(ctx)                                      // change-only
        }
        // Enumerate the moment this slot becomes the live subpage — device/app
        // FOCUS_DEVICE, the initial page activate, or a plain track selection that
        // re-points the slot. This is what ships the full detail set on selection.
        // try/catch: harmless if a pre-1.2 API lacks subpage mOnActivate (then the
        // identity callback above still covers plugin loads / selections).
        try {
            sub.mOnActivate = function (ctx, m) {
                // THE WRITE PUMP LANDS HERE: `m` is a LIVE activeMapping, the
                // one context where DirectAccess calls actually work. Drain
                // queued writes FIRST (exact-index setParameterProcessValue),
                // then re-pump if more arrived mid-flight.
                var pumped = daPumpBusy && currentSlot === idx
                if (currentSlot !== idx) {
                    daWriteQueue = {}      // queued intent was for another slot
                    currentSlot = idx
                }
                drainDAWrites(ctx, m, idx)
                daPumpBusy = false
                var more = false, mk
                for (mk in daWriteQueue) { more = true; break }
                if (more) pumpDAWrites(ctx)
                // a pump-triggered re-activation must NOT re-flood the full
                // enumeration (this path fires per drained write burst)
                if (!pumped) pushDirectParams(idx, m, ctx)
            }
        } catch (e) { console.log('roto: subpage mOnActivate unsupported -> ' + e) }
        // The "already-loaded plugin" signal lives ONCE, in the shared param-knob
        // mOnTitleChange above. Don't re-derive it from bank.mOnTitleChange here.
        var act = armCC(surface.makeButton(14 + idx, 1, 1, 1), 80 + idx)
        page.makeActionBinding(act.mSurfaceValue, sub.mAction.mActivate)
        act.mSurfaceValue.mOnProcessValueChange = function (ctx, val) {
            if (val >= 0.5) { currentSlot = idx; send(ctx, eFocus(idx)) }
        }
        insertActs[idx] = act
    }
    bindSlot(0, chan.mInstrumentPluginSlot)                          // the instrument
    if (inserts) for (var s = 1; s < NUM_INSERTS; ++s)              // then the inserts
        (function (idx) {
            bindSlot(idx, inserts.makeInsertEffectViewer('Roto' + idx).accessSlotAtIndex(idx - 1))
        })(s)
})
console.log('roto: Roto-Control script loaded')

//=============================================================================
// Athens -> Cubase : apply control frames by pushing values back to the host
//=============================================================================
midiInput.mOnSysex = function (activeDevice, message) {
    if (message.length < 5 || message[0] !== 0xF0 || message[1] !== ID) return
    pumpWatchdog(activeDevice)   // every frame: unwedge a lost pump pulse
    if (message[2] !== DIR.TO_CUBASE) return        // ignore our own state echoes
    var cmd = message[3]
    var p = message.slice(4, message.length - 1)   // payload (drop F7)
    if (cmd === CMD.VOLUME) {
        var i = fromU14(p, 0)
        if (i < NUM_STRIPS) mixKnobs[i].mSurfaceValue.setProcessValue(activeDevice, fromU14(p, 2) / 0x3FFF)
    } else if (cmd === CMD.PAN) {
        var pi = fromU14(p, 0)
        if (pi < NUM_STRIPS) panKnobs[pi].mSurfaceValue.setProcessValue(activeDevice, fromU14(p, 2) / 0x3FFF)
    } else if (cmd === CMD.SELECT) {
        var si = fromU14(p, 0)
        if (si < NUM_STRIPS) selBtns[si].mSurfaceValue.setProcessValue(activeDevice, 1)
    } else if (cmd === CMD.FLAG) {
        var idx = fromU14(p, 0), f = p[2], on = p[3] ? 1 : 0
        if (idx < NUM_STRIPS && f === FLAG.MUTE) muteBtns[idx].mSurfaceValue.setProcessValue(activeDevice, on)
        if (idx < NUM_STRIPS && f === FLAG.SOLO) soloBtns[idx].mSurfaceValue.setProcessValue(activeDevice, on)
    } else if (cmd === CMD.PARAM_VALUE) {
        var slot = fromU14(p, 0)
        // Route through the paged bank (DirectAccess writes are impossible from
        // here — no activeMapping in mOnSysex, HW-confirmed dead knobs). Any
        // index < PLUGIN_PARAM_MAX is drivable; off-page writes queue while the
        // bank pages over (see writeParam).
        if (slot < PLUGIN_PARAM_MAX) {
            writeParam(activeDevice, slot, fromU14(p, 2) / 0x3FFF)
        } else if (!dropWarned[slot]) {
            dropWarned[slot] = true
            send(activeDevice, eDiag('PARAM_VALUE slot ' + slot
                + ' beyond ' + PLUGIN_PARAM_MAX + ' — not drivable'))
        }
    } else if (cmd === CMD.TRANSPORT) {
        var bits = p[0]
        playBtn.mSurfaceValue.setProcessValue(activeDevice, (bits & 0x01) ? 1 : 0)
        recBtn.mSurfaceValue.setProcessValue(activeDevice, (bits & 0x02) ? 1 : 0)
        loopBtn.mSurfaceValue.setProcessValue(activeDevice, (bits & 0x04) ? 1 : 0)
    } else if (cmd === CMD.PAGE) {
        var pb = p[0] ? pageNext : pagePrev            // pulse to fire the action
        pb.mSurfaceValue.setProcessValue(activeDevice, 1)
        pb.mSurfaceValue.setProcessValue(activeDevice, 0)
    } else if (cmd === CMD.FOCUS_DEVICE) {
        var di = fromU14(p, 0)                      // activate that insert's subpage
        if (insertActs[di]) {
            insertActs[di].mSurfaceValue.setProcessValue(activeDevice, 1)
            insertActs[di].mSurfaceValue.setProcessValue(activeDevice, 0)
        }
    } else if (cmd === CMD.HELLO) {
        send(activeDevice, eHello(HELLO_ID))
        // A TAGGED hello (payload present, e.g. "ka") is Athens' liveness
        // keepalive: the echo above is the whole answer. Only a bare WHO
        // (reconnect) gets the full replay below, so idle sessions aren't re-pushed.
        if (p.length === 0) {
            for (var dk in DIAG) send(activeDevice, eDiag(dk + '=' + DIAG[dk]))
            pushMixer(activeDevice)              // replay the mixer (count+names+values)
            // Re-announce the current plugin(s) on every (re)connect: identity
            // callbacks fire only on CHANGE, so a plugin loaded before Athens
            // connected would otherwise never be sent. insertNames[] survives
            // Athens restarts (the script keeps running), so this replays it with
            // no Cubase gesture.
            pushInsertList(activeDevice)
        }
    }
}

// idle tick: watchdog for a lost pump pulse, so a wedged queue self-heals even
// when no further MIDI arrives. Cubase 13+; mOnSysex ticks it too.
driver.mOnIdle = function (activeDevice) { pumpWatchdog(activeDevice) }

// on activate: announce identity (for Athens' auto-detect) then seed the session
page.mOnActivate = function (ctx) {
    DIAG.active = 'yes'
    send(ctx, eHello(HELLO_ID))
    send(ctx, eCount(NUM_STRIPS))
    // Bring the live plugin subpage online immediately: replay cached names, then
    // (re)activate the current slot so its parameter bank binds and reports the
    // loaded plugin's name + values with no re-select.
    pushInsertList(ctx)
    var live = insertActs[currentSlot] || insertActs[0]
    if (live) {
        live.mSurfaceValue.setProcessValue(ctx, 1)
        live.mSurfaceValue.setProcessValue(ctx, 0)
    }
}
