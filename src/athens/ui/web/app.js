/* roto-reaper frontend — zero-build vanilla JS over the WS JSON-RPC API. */
(function () {
  "use strict";

  var QS = new URLSearchParams(location.search);
  var WS_URL = QS.get("ws") || "ws://127.0.0.1:8765";
  // launched with `ui --view diag` → expose internals like plugin hashes
  var DIAG_MODE = QS.get("view") === "diag";
  var ENCODER_FIRST_CC = 12, NUM_KNOBS = 8;

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return document.querySelectorAll(sel); };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // two-step confirm: first click arms (adds .armed, 4s auto-disarm), second
  // fires onConfirm. idle/armed text may be a function for live-state labels.
  function armConfirm(button, idleText, armedText, onConfirm, canArm) {
    var txt = function (t) { return typeof t === "function" ? t() : t; };
    var armed = false;
    var disarm = function () {
      armed = false;
      button.classList.remove("armed");
      button.textContent = txt(idleText);
    };
    disarm();
    button.onclick = function () {
      if (canArm && !canArm()) return;
      if (!armed) {
        armed = true;
        button.classList.add("armed");
        button.textContent = txt(armedText);
        setTimeout(disarm, 4000);
        return;
      }
      disarm();
      onConfirm();
    };
    return button;
  }

  // number inputs enforce min/max only on the spinner; typed input passes
  // through, so every read clamps. hi may be a function (bound depends on mode).
  function clampInt(v, lo, hi) {
    v = Math.round(+v);
    if (!isFinite(v)) v = lo;
    return Math.min(hi, Math.max(lo, v));
  }
  // legal detent counts: 0 (continuous) or 2-10 (1 is meaningless; mirrors the
  // server's _nsteps clamp)
  function coerceSteps(v) { return v === 1 ? 2 : v; }
  function boundNum(inp, lo, hi, onchange) {
    var hiF = typeof hi === "function" ? hi : function () { return hi; };
    inp.min = lo; inp.max = hiF();
    var read = function () { return clampInt(inp.value, lo, hiF()); };
    inp.addEventListener("change", function () {
      inp.max = hiF();
      inp.value = read();
      if (onchange) onchange();
    });
    return read;
  }

  // device palette (indexed real values, inlined via palette.js — fetch() is
  // CORS-blocked under file:// in webviews)
  var PALETTE = window.DEVICE_PALETTE || [{ bg: "#8f8a7e", fg: "#000" }];
  function pal(i) { return PALETTE[i % PALETTE.length] || PALETTE[0]; }

  // picker presentation order: by hue (12 buckets, light->dark inside),
  // greys last — the DEVICE INDEX stays the stored value regardless
  var _palOrdered = null;
  function orderedPalette() {
    if (_palOrdered) return _palOrdered;
    function hsl(hex) {
      var r = parseInt(hex.slice(1, 3), 16) / 255,
          g = parseInt(hex.slice(3, 5), 16) / 255,
          b = parseInt(hex.slice(5, 7), 16) / 255;
      var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
      var h = 0;
      if (d) {
        if (mx === r) h = ((g - b) / d + 6) % 6;
        else if (mx === g) h = (b - r) / d + 2;
        else h = (r - g) / d + 4;
        h *= 60;
      }
      var l = (mx + mn) / 2;
      return { h: h, s: d ? d / (1 - Math.abs(2 * l - 1)) : 0, l: l };
    }
    _palOrdered = PALETTE.map(function (p, i) {
      var c = hsl(p.bg);
      return { index: p.index !== undefined ? p.index : i,
               bg: p.bg, fg: p.fg, h: c.h, s: c.s, l: c.l };
    }).sort(function (a, b) {
      var ag = a.s < 0.18, bg = b.s < 0.18;
      if (ag !== bg) return ag ? 1 : -1;
      if (ag) return b.l - a.l;
      var hb = Math.round(a.h / 30) - Math.round(b.h / 30);
      if (hb) return hb;
      return b.l - a.l;
    });
    return _palOrdered;
  }

  function buildSwatchRow(selectedIndex, onPick) {
    var row = el("div", "swatchrow");
    orderedPalette().forEach(function (p) {
      var s = el("span", "sw" + (p.index === selectedIndex ? " selected" : ""));
      s.style.background = p.bg;
      s.title = "device color " + p.index;
      s.onclick = function () {
        row.querySelectorAll(".sw").forEach(function (x) {
          x.classList.remove("selected");
        });
        s.classList.add("selected");
        onPick(p.index);
      };
      row.appendChild(s);
    });
    return row;
  }

  var state = {
    tracks: [], selected: 0, firstTrack: 0, transport: {}, connected: false,
    devices: [], selectedDevice: 0, params: [], learnMode: 0, deviceMode: "",
    daw: "DAW", dawAlive: true, scriptStale: {},
  };
  var knobs = [], paused = false, actPaused = false;
  // who drives who: "device" = the hardware picks the app view,
  // "app" = clicking Live/Plugin flips the hardware screen, "off" = neither
  var follow = "device";
  try {
    var storedFollow = localStorage.getItem("follow");
    if (storedFollow === "app" || storedFollow === "off") follow = storedFollow;
  } catch (e) {}

  var ws = null, nextId = 1, pending = {};
  function rpc(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      ws.send(JSON.stringify({ id: id, method: method, params: params || {} }));
    });
  }

  function connect() {
    setFoot("connecting to " + WS_URL + " …");
    ws = new WebSocket(WS_URL);
    ws.onopen = function () {
      setFoot("ready");
      $("#foot-ws").textContent = WS_URL;
      rpc("subscribe", { topics: ["device", "tracks", "selected", "transport",
        "devices", "value", "touch", "param", "devparam", "frame", "setups",
        "progress", "learn", "sweep", "device_map_changed", "setup_learned",
        "mode", "setup_selected", "device_plugin_selected", "daw", "notice",
        "script_stale", "trace"] });
      rpc("get_state").then(applyState);
      rpc("get_plugin_links").then(function (l) { pluginLinks = l; })
        .catch(function () {});
      loadSetups();
    };
    ws.onmessage = function (e) {
      var msg = JSON.parse(e.data);
      if (msg.id !== undefined && pending[msg.id]) {
        var p = pending[msg.id]; delete pending[msg.id];
        if (msg.error) { p.reject(msg.error); } else { p.resolve(msg.result); }
      } else if (msg.event) {
        onEvent(msg.event, msg.data);
      }
    };
    ws.onclose = function () {
      setFoot("bridge process unreachable — retrying…");
      $("#dot-device").dataset.state = "error";
      setTimeout(connect, 1000);
    };
  }

  function applyState(s) {
    state.tracks = s.tracks; state.selected = s.selected_track;
    state.firstTrack = s.first_track || 0;
    state.transport = s.transport;
    state.feedRunning = s.feed_running;
    state.daw = s.daw || "DAW";
    state.connected = s.connected;
    state.devices = s.devices || []; state.selectedDevice = s.selected || 0;
    $("#dot-device").dataset.state = s.connected ? "connected" : "disconnected";
    state.serial = !!s.serial;
    // green dot is the connected indicator; only show the chip to CONNECT
    $("#chip-connect").hidden = s.connected;
    if (!s.connected) $("#chip-connect").textContent = "connect ▸";
    state.dawAlive = s.daw_alive !== false;
    state.scriptStale = s.script_stale || {};
    renderReaperLink();
    refreshAll();
    loadPluginView();
    followMode(s.mode);  // the device may have picked its screen before we ran
  }

  function renderReaperLink() {
    // Green/named on REAL liveness, straight from the backend: every source
    // now owns its own honesty (REAPER: feed heartbeat OR OSC with a gone
    // grace; Cubase / Pro Tools: answering on their port). Do NOT gate on
    // tracks>0 — an empty project (0 tracks) is still a live connection, and
    // that gate mislabeled it "closed" (it also hid a connected Cubase whose
    // mixer hadn't pushed yet, showing a misleading "—").
    var up = state.dawAlive !== false;
    $("#dot-reaper").classList.toggle("up", up);
    var t = $("#reaper-text");
    if (t) {
      // a STALE companion script outranks everything: the host is live but
      // running an old copy — a PERSISTENT banner (state, not a transient
      // notice: the check fires ms after launch, before this UI connects)
      var st = state.scriptStale
               && state.scriptStale[(state.daw || "").toLowerCase()];
      if (st) {
        t.textContent = state.daw + " — old script ("
          + (st.loaded ? "v" + st.loaded : "unversioned")
          + "), restart " + state.daw + " to update to v" + st.expected;
        t.title = "Athens installed v" + st.expected + " on disk, but "
          + state.daw + " is still running the previously loaded copy.";
      } else {
        t.textContent = state.daw ? (up ? state.daw : state.daw + " closed") : "—";
        t.title = up ? "" : (feedHint() || "");
      }
    }
  }

  // A DAW was resolved but isn't feeding yet (its feed script isn't running):
  // return DAW-aware connect instructions, or null once it's live.
  function feedHint() {
    if (state.dawAlive !== false) return null;
    var d = (state.daw || "").toLowerCase();
    if (d.indexOf("reaper") >= 0)
      return "REAPER detected — run “roto_fx_feed” from the Actions list to connect "
           + "(set it as a startup action to run automatically)";
    if (d.indexOf("cubase") >= 0)
      return "Cubase detected — enable the Roto-Control MIDI Remote script to connect";
    if (d.indexOf("pro tools") >= 0)
      return "Pro Tools — enable an IAC bus (Audio MIDI Setup), then point its HUI "
           + "(Setup ▸ Peripherals ▸ MIDI Controllers) at the bus Athens bound to";
    return (state.daw || "Your DAW") + " isn’t feeding yet";
  }

  function refreshAll() {
    renderTracks(); renderSurfaceLabels(); renderParams();
    renderChain();
  }

  // "roto ▸ app": follow the hardware between the two performance views
  // only — never yank the user out of an editing view
  function followMode(mode) {
    if (!mode) return;
    state.deviceMode = mode;
    var cur = document.querySelector(".view.active").id.slice(5);
    if (follow === "device" && (cur === "live" || cur === "plugin")) {
      var want = (mode === "plugin" || mode === "smart") ? "plugin" : "live";
      if (want !== cur) switchView(want);
    }
  }

  // "app ▸ roto": a user click on Live/Plugin flips the hardware screen. Skips
  // when the device already matches — app views are coarser than device modes
  // (smart also maps to Plugin), so a redundant push could exit smart mode.
  function pushModeToDevice(view) {
    if (follow !== "app" || !state.connected) return;
    if (view !== "live" && view !== "plugin") return;
    var mode = view === "plugin" ? "plugin" : "mix";
    if (state.deviceMode === mode ||
        (mode === "plugin" && state.deviceMode === "smart")) return;
    rpc("set_device_mode", { mode: mode }).catch(function (e) {
      setFoot("device mode: " + e.message);
    });
  }

  function onEvent(topic, d) {
    switch (topic) {
      case "device":
        state.connected = d.connected;
        $("#dot-device").dataset.state = d.connected ? "connected" : "disconnected";
        $("#chip-connect").hidden = d.connected;
        if (!d.connected) $("#chip-connect").textContent = "connect ▸";
        state.serial = !!d.serial;
        $("#facts").textContent = d.connected ? "" : "not connected";
        break;
      case "progress":
        setFoot(d.stage + " " + d.done + "/" + d.total);
        break;
      case "tracks":
        state.tracks = d.tracks;
        state.firstTrack = d.first_track || 0;
        if (d.feed_running !== undefined) state.feedRunning = d.feed_running;
        renderTracks(); renderSurfaceLabels();
        renderReaperLink();
        break;
      case "daw":
        if (d.name) state.daw = d.name;      // hot-swap: the live DAW changed
        state.dawAlive = d.alive;
        renderTracks(); renderReaperLink();
        renderChain(); renderDevicePanel();  // refresh the "how to connect" hint
        setFoot(d.alive ? state.daw + " connected"
                        : (feedHint() || state.daw + " closed"));
        break;
      case "selected":
        state.selected = d.index; renderTracks(); break;
      case "transport":
        state.transport = d; break;
      case "value": {
        var k = d.cc - ENCODER_FIRST_CC;
        if (k >= 0 && k < NUM_KNOBS) {
          setKnobValue(k, d.value, null);
          activity("knob " + (k + 1) + " → " + Math.round(d.value * 127));
        }
        break;
      }
      case "param": {
        updateParamRow(d.device, d.param, d.value, d.display);
        var pp = diagParams[d.param] || (diagParams[d.param] = {});
        pp.daw = d.value; pp.display = d.display; renderDiagParams();
        break;
      }
      case "devparam": {
        var pv = diagParams[d.param] || (diagParams[d.param] = {});
        pv.devIn = d.value; renderDiagParams();
        break;
      }
      case "touch":
        if (knobs[d.knob]) knobs[d.knob].el.classList.toggle("touched", d.touched);
        if (d.touched) activity("touch knob " + (d.knob + 1));
        break;
      case "devices":
        state.devices = d.devices; state.selectedDevice = d.selected;
        renderChain(); loadParams();
        // the Device-view Links list must follow the live plugin set too, or a
        // map selected before the plugin surfaced stays frozen and unlinkable
        renderDevicePanel();
        break;
      case "learn": onLearn(d); break;
      case "sweep": onSweep(d); break;
      case "notice": setFoot(d.text || ""); break;
      case "script_stale":
        state.scriptStale = d.all || {};
        renderReaperLink();
        break;
      case "device_map_changed": {
        // the device just stored a learn — refresh whatever shows that map
        delete mapCache[d.hash];
        setFoot("device learned " + d.kind + " " + (d.slot + 1));
        learnFeedMsg("device stored " + d.kind + " " + (d.slot + 1));
        if (devMapSel && devMapSel.hash === d.hash &&
            $("#view-device").classList.contains("active")) {
          selectDevMap(devMapSel, true);
        }
        if ($("#view-plugin").classList.contains("active")) {
          loadMappingBadges();
        }
        break;
      }
      case "setup_learned":
        setFoot("setup " + d.setup + ": learned " + d.kind + " " + (d.slot + 1));
        break;
      case "mode":
        followMode(d.mode);
        break;
      case "device_plugin_selected": {
        state.devicePluginHash = d.hash;
        renderDevMaps();
        // follow the hardware into the map it is showing, like touch-sync
        var devViewActive = $("#view-device").classList.contains("active");
        if (devViewActive && d.hash) {
          var m = devMaps.find(function (x) { return x.hash === d.hash; });
          if (m && (!devMapSel || devMapSel.hash !== d.hash)) selectDevMap(m);
        }
        break;
      }
      case "setup_selected":
        state.activeSetup = d.index;
        setFoot("device switched to setup " + (d.index + 1));
        renderSetupList();
        break;
      case "frame": break;                 // superseded by the decoded 'trace'
      case "trace": renderTraceRow(d); break;
      case "setups":
        rpc("list_setups").then(function (list) {
          setups = list; renderSetupList(); renderLibrary();
        });
        if (d.index === curIndex) loadSetup(curIndex);
        break;
    }
  }

  function setFoot(t) { $("#foot-status").textContent = t; }

  /* ================= LIVE VIEW ================= */
  function renderTracks() {
    var strip = $("#trackstrip");
    strip.replaceChildren();
    // feed_running === false means the ReaScript isn't running, so REAPER's
    // OSC bank padding can't be clamped — the track list is likely phantom
    var padded = state.feedRunning === false && state.tracks.length > 0;
    var cnt = $("#track-count");
    if (padded) {
      cnt.textContent = "⚠ feed off";
      cnt.title = "REAPER's OSC can't report the project size, so these may " +
        "be padded/phantom tracks. Load reaper/roto_fx_feed.lua in REAPER " +
        "(Actions → Load ReaScript, then run it) to show the real count.";
    } else {
      cnt.textContent = state.tracks.length ? state.daw + " · " +
        state.tracks.length : "—";
      cnt.title = "";
    }
    // the on-surface window the device paged to (arrows move firstTrack)
    var first = state.firstTrack || 0;
    var last = Math.min(first + 8, state.tracks.length);
    $("#track-window").textContent =
      state.tracks.length > 8 ? "◂ " + (first + 1) + "–" + last + " ▸" : "";
    state.tracks.forEach(function (t) {
      var row = el("div", "lrow" + (t.index === state.selected ? " selected" : ""));
      var onSurface = t.index >= first && t.index < first + 8;
      var win = el("i", "winbar" + (onSurface ? " on" : ""));
      var idx = el("span", "idx mono", t.index + 1);
      var sw = el("span", "swatch");
      sw.style.background = pal(t.colour).bg;
      // unnamed track shows faint "(unnamed)" here but is pushed to the device
      // exactly as REAPER reports it (empty)
      var name = t.name
        ? el("span", "", t.name)                  // untrusted → textContent
        : el("span", "faint", "(unnamed)");
      var ms = el("span", "msbtns");
      var m = el("span", "msbtn" + (t.muted ? " on-m" : ""), "M");
      var s = el("span", "msbtn" + (t.soloed ? " on-s" : ""), "S");
      ms.append(m, s);
      row.append(win, idx, sw, name, ms);
      row.onclick = function () { rpc("select_track", { index: t.index }); };
      strip.appendChild(row);
    });
    $("#bank-desc").textContent = state.tracks.length
      ? "Mixer · tracks " + (first + 1) + "–" + last +
        " · knobs = VOLUME · buttons = MUTE / SOLO"
      : "waiting for " + state.daw + "…";
  }


  function buildSurface() {
    var kwrap = $("#knobs");
    for (var i = 0; i < NUM_KNOBS; i++) {
      var k = el("div", "knob");
      var face = el("div", "knobface");
      var cap = el("div", "knobcap");
      var ptr = el("div", "pointer");
      cap.appendChild(ptr); face.appendChild(cap);
      var name = el("div", "name", "—");
      var val = el("div", "val mono", "");
      k.append(face, name, val);
      kwrap.appendChild(k);
      knobs.push({ el: k, face: face, ptr: ptr, name: name, val: val, value: 0, colour: 0 });
      setKnobValue(i, 0, "");
    }
    ["mute-row", "solo-row"].forEach(function (id, r) {
      var wrap = $("#" + id);
      for (var b = 0; b < 8; b++) {
        wrap.appendChild(el("div", "sbtn", r === 0 ? "MUTE" : "SOLO"));
      }
    });
  }

  function setKnobValue(i, value, display) {
    var k = knobs[i];
    k.value = value;
    var colour = pal(k.colour).bg;
    var deg = 270 * value;
    k.face.style.background =
      "conic-gradient(from 225deg, " + colour + " " + deg + "deg, var(--surface-lo) " + deg + "deg 270deg, transparent 270deg)";
    k.ptr.style.transform = "translate(-50%,-100%) rotate(" + (deg - 135) + "deg)";
    k.val.textContent = (display !== null && display !== undefined && display !== "")
      ? display : Math.round(value * 127);
  }

  function renderSurfaceLabels() {
    var mutes = $$("#mute-row .sbtn"), solos = $$("#solo-row .sbtn");
    var first = state.firstTrack || 0;       // follow the device's bank page
    for (var i = 0; i < NUM_KNOBS; i++) {
      var t = state.tracks[first + i];
      knobs[i].name.textContent = t ? t.name : "—";
      knobs[i].colour = t ? t.colour : 0;
      setKnobValue(i, t ? t.volume : knobs[i].value, null);
      if (mutes[i]) {
        mutes[i].classList.toggle("lit", !!(t && t.muted));
        mutes[i].style.background = t && t.muted ? "var(--error)" : "";
      }
      if (solos[i]) {
        solos[i].classList.toggle("lit", !!(t && t.soloed));
        solos[i].style.background = t && t.soloed ? "var(--dirty)" : "";
      }
    }
  }

  function activity(msg) {
    if (actPaused) return;
    var wrap = $("#activity");
    var t = new Date();
    var row = el("div", "lrow empty",
      String(t.getMinutes()).padStart(2, "0") + ":" +
      String(t.getSeconds()).padStart(2, "0") + "  " + msg);
    wrap.prepend(row);
    while (wrap.children.length > 50) wrap.removeChild(wrap.lastChild);
  }

  /* ================= PLUGIN VIEW ================= */
  var paramRows = {};          // param index -> {row, bar, out}
  var paramFilter = "";
  var mappingBadges = {};      // param index -> "K3"/"B2" from the linked map

  function loadPluginView() {
    rpc("get_devices").then(function (d) {
      state.devices = d.devices; state.selectedDevice = d.selected;
      renderChain(); loadParams();
    }).catch(function () {});
  }

  function loadParams() {
    rpc("device_params", { device: state.selectedDevice }).then(function (list) {
      state.params = list;
      renderParams();
      loadMappingBadges();
    }).catch(function () {});
  }

  function loadMappingBadges() {
    mappingBadges = {};
    var d = state.devices.find(function (x) { return x.index === state.selectedDevice; });
    var link = d && pluginLinks[d.name];
    if (!link) { return; }
    function apply(detail) {
      Object.keys(detail.knobs).forEach(function (s) {
        mappingBadges[detail.knobs[s].param_index] = "K" + (+s + 1);
      });
      Object.keys(detail.switches).forEach(function (s) {
        mappingBadges[detail.switches[s].param_index] = "B" + (+s + 1);
      });
      renderParams();
    }
    if (mapCache[link.hash]) { apply(mapCache[link.hash]); return; }
    rpc("get_device_plugin", { hash: link.hash }).then(function (dd) {
      mapCache[link.hash] = dd;
      apply(dd);
    }).catch(function () {});   // serial not connected: badges just absent
  }

  function renderChain() {
    var wrap = $("#fxchain");
    wrap.replaceChildren();
    var t = state.tracks[state.selected];
    $("#chain-track").textContent = t ? t.name : "";
    if (!state.devices.length) {
      wrap.appendChild(el("p", "hint", feedHint() ||
        ("no plugin focused — open a plugin in " + (state.daw || "your DAW"))));
      return;
    }
    state.devices.forEach(function (d) {
      if (!d.name) return;                 // empty slot (e.g. no instrument) — skip
      var row = el("div", "lrow" + (d.index === state.selectedDevice ? " selected" : ""));
      row.append(el("span", "idx mono", d.index + 1),
                 el("i", "endot" + (d.enabled ? " on" : "")),
                 el("span", "", d.name));
      wrap.appendChild(row);
    });
  }

  function renderParams() {
    var wrap = $("#paramtable");
    wrap.replaceChildren();
    paramRows = {};
    var d = state.devices.find(function (x) { return x.index === state.selectedDevice; });
    var link = d && pluginLinks[d.name];
    $("#plugin-title").textContent = d
      ? d.name + " · " + state.params.length + " params" +
        (link ? "  →  linked: " + link.device_name : "")
      : "—";
    if (!state.params.length) {
      wrap.appendChild(el("p", "hint", state.devices.length
        ? "loading parameters…"
        : (feedHint() || ("focus a plugin window in " + (state.daw || "your DAW")
                          + " to populate this view"))));
      return;
    }
    var filter = paramFilter.toLowerCase();
    state.params.forEach(function (p) {
      if (filter && p.name.toLowerCase().indexOf(filter) < 0 &&
          String(p.index) !== filter) return;
      var row = el("div", "trow params-grid");
      var bar = el("div", "valbar");
      bar.title = "drag to set";
      var fill = el("i"); fill.style.width = (p.value * 100).toFixed(1) + "%";
      bar.appendChild(fill);
      var out = el("span", "mono", p.display || "");
      var stepTag = el("span", "pnum", p.steps ? p.steps + "st" : "");
      var badge = mappingBadges[p.index];
      row.append(el("span", "pnum", p.index),
                 el("span", "pname", p.name),
                 bar,
                 stepTag,
                 out,
                 el("span", badge ? "mapbadge" : "", badge || ""));
      wireParamBar(bar, fill, p.index);
      wrap.appendChild(row);
      paramRows[p.index] = { row: row, fill: fill, out: out };
    });
  }

  function wireParamBar(bar, fill, paramIndex) {
    var lastSend = 0;
    function setFromEvent(ev, force) {
      var r = bar.getBoundingClientRect();
      var v = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
      fill.style.width = (v * 100).toFixed(1) + "%";
      var now = Date.now();
      if (force || now - lastSend > 33) {         // ~30 msg/s while dragging
        lastSend = now;
        rpc("set_device_param",
            { device: state.selectedDevice, param: paramIndex, value: v });
      }
    }
    bar.onpointerdown = function (ev) {
      ev.preventDefault();
      bar.setPointerCapture(ev.pointerId);
      setFromEvent(ev, true);
      bar.onpointermove = function (mv) { setFromEvent(mv, false); };
      bar.onpointerup = function (up) {
        setFromEvent(up, true);
        bar.onpointermove = bar.onpointerup = null;
      };
    };
  }

  function updateParamRow(device, param, value, display) {
    if (device !== state.selectedDevice) return;
    var pr = paramRows[param];
    var p = state.params[param];
    if (p && p.index === param) { p.value = value; p.display = display; }
    if (!pr) return;
    pr.fill.style.width = (value * 100).toFixed(1) + "%";
    pr.out.textContent = display || value.toFixed(3);
    pr.row.classList.remove("flash");
    void pr.row.offsetWidth;               // restart the animation
    pr.row.classList.add("flash");
  }

  // Learn FAILURE is unsignalled — the device sends PLUGIN_LEARN_COMPLETE on
  // success but nothing on failure. So arm a timeout: if no completion lands a
  // few seconds after the last learn/sweep event, treat it as "not confirmed".
  var learnTimer = null;
  function learnArm(active) {
    if (learnTimer) { clearTimeout(learnTimer); learnTimer = null; }
    if (active) learnTimer = setTimeout(learnTimeout, 6000);
  }
  function learnTimeout() {
    learnTimer = null;
    $("#learn-big").textContent = "not confirmed ✕";
    $("#learn-detail").textContent =
      "the device never confirmed the mapping — re-arm LEARN and move the parameter again";
    $("#learn-pill").dataset.state = "off";
    $("#learn-pill").textContent = "LEARN OFF";
    $("#sweepbar").hidden = true;
    learnFeedMsg("✕ learn timed out — no confirmation from the device");
    setFoot("learn didn't confirm — try again");
  }

  var LEARN_LABEL = { 0: "idle", 1: "ARMED — move a parameter in REAPER",
                      2: "learn sent — waiting for the device" };
  function onLearn(d) {
    if (d.mode === -1) {                    // learn complete
      learnArm(false);
      learnFeedMsg("✓ mapping stored on the device");
      $("#learn-big").textContent = "mapped ✓";
      $("#learn-pill").dataset.state = "off";
      $("#learn-pill").textContent = "LEARN OFF";
      $("#sweepbar").hidden = true;
      loadParams();
      return;
    }
    state.learnMode = d.mode;
    var pill = $("#learn-pill");
    pill.dataset.state = d.mode === 1 ? "armed" : "off";
    pill.textContent = d.mode === 1 ? "LEARN ARMED" : "LEARN OFF";
    $("#learn-big").textContent = LEARN_LABEL[d.mode] || "idle";
    $("#learn-detail").textContent = d.mode === 1
      ? "now move the target parameter in REAPER" : "learn is armed from the hardware";
    if (d.mode !== 1) $("#sweepbar").hidden = true;
    learnFeedMsg(d.mode === 1 ? "learn armed on device" : "learn off");
    learnArm(d.mode !== 0);       // waiting for confirmation while active
  }

  function onSweep(d) {
    learnArm(true);               // sweep in progress -> extend the timeout
    var pill = $("#learn-pill");
    pill.dataset.state = "sweep"; pill.textContent = "SWEEPING";
    $("#learn-big").textContent = "measuring param " + d.param;
    var p = state.params[d.param];
    $("#learn-detail").textContent = p && p.name ? p.name : "device-driven sweep";
    $("#sweepbar").hidden = false;
    $("#sweepfill").style.width = ((d.step / 127) * 100).toFixed(0) + "%";
  }

  function learnFeedMsg(msg) {
    var wrap = $("#learnfeed");
    var t = new Date();
    wrap.prepend(el("div", "lrow empty",
      String(t.getHours()).padStart(2, "0") + ":" +
      String(t.getMinutes()).padStart(2, "0") + "  " + msg));
    while (wrap.children.length > 40) wrap.removeChild(wrap.lastChild);
  }

  $("#param-search").oninput = function () {
    paramFilter = this.value.trim();
    renderParams();
  };

  /* ================= DEVICE (stored plugin maps: inspect, EDIT, link) ===== */
  var devMaps = [], devMapSel = null, devMapDetail = null,
      devCtlSel = null, pluginLinks = {}, mapCache = {};

  function loadDeviceMaps() {
    rpc("get_plugin_links").then(function (l) {
      pluginLinks = l; renderDevMaps(); renderDevicePanel();
    });
    rpc("get_current_device_plugin").then(function (r) {
      state.devicePluginHash = r.hash;
      renderDevMaps();
    }).catch(function () {});
    rpc("list_device_plugins").then(function (list) {
      devMaps = list;
      renderDevMaps(); renderDevicePanel();
    }).catch(function (e) {
      devMaps = [];
      renderDevMaps(); renderDevicePanel();
      $("#devmaps").appendChild(el("p", "hint",
        "cannot read the device: " + e.message +
        " — the serial link is needed (plug the ROTO, then connect)"));
    });
  }

  function renderDevMaps() {
    var wrap = $("#devmaps");
    wrap.replaceChildren();
    $("#devmap-count").textContent = devMaps.length || "";
    // maps that fuzzy-match an UNLINKED live plugin get flagged as contenders —
    // a proposal only; EVERY map stays clickable, so the user links manually.
    var contenders = {};
    state.devices.forEach(function (d) {
      if (pluginLinks[d.name]) { return; }
      var best = bestMapFor(d.name);
      if (best) { contenders[best.map.hash] = d.name; }
    });
    devMaps.slice().sort(function (a, b) {
      return a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    }).forEach(function (m) {
      var row = el("div", "lrow" +
        (devMapSel && devMapSel.hash === m.hash ? " selected" : "") +
        (contenders[m.hash] ? " contender" : ""));
      var linked = Object.keys(pluginLinks).some(function (k) {
        return pluginLinks[k].hash === m.hash;
      });
      var onDevice = state.devicePluginHash === m.hash;
      var tag = contenders[m.hash]
        ? el("span", "mono contend", "≈ " + contenders[m.hash])
        : el("span", "mono " + (linked ? "" : "faint"),
             linked ? "linked" : (DIAG_MODE ? m.hash.slice(0, 6) : ""));
      row.append(el("span", "", (onDevice ? "▸ " : "") + m.name),
                 el("span", "spacer"), tag);
      row.title = contenders[m.hash]
        ? "suggested match for " + contenders[m.hash] +
          " — click to select, then link (or link any other map)"
        : (onDevice ? "currently shown on the device" : "");
      row.onclick = function () { selectDevMap(m); };
      wrap.appendChild(row);
    });
    if (!devMaps.length) {
      wrap.appendChild(el("p", "hint", "no maps read yet — press refresh"));
    }
  }

  function selectDevMap(m, force) {
    devMapSel = m; devCtlSel = null;
    renderDevMaps();
    $("#devmap-title").textContent = m.name +
      (DIAG_MODE ? " · " + m.hash.slice(0, 8) + "…" : "");
    $("#btn-devmap-rename").hidden = false;
    var wrap = $("#devmap-controls");
    if (!force && mapCache[m.hash]) {
      devMapDetail = mapCache[m.hash];
      renderDevMapControls(); renderDevicePanel();
      return;
    }
    wrap.replaceChildren();
    wrap.appendChild(el("p", "hint", "reading controls from the device…"));
    rpc("get_device_plugin", { hash: m.hash }).then(function (d) {
      mapCache[m.hash] = d;
      devMapDetail = d;
      renderDevMapControls(); renderDevicePanel();
    }).catch(function (e) {
      wrap.replaceChildren();
      wrap.appendChild(el("p", "hint", "read failed: " + e.message));
    });
    renderDevicePanel();
  }

  var dragSrc = null;    // {kind, slot} while a control row is dragged

  function renderDevMapControls() {
    var wrap = $("#devmap-controls");
    wrap.replaceChildren();
    if (!devMapDetail) return;
    var occupied = Object.keys(devMapDetail.knobs).length +
                   Object.keys(devMapDetail.switches).length;
    [["knobs", "knob", "KNOB"], ["switches", "switch", "BUTTON"]]
      .forEach(function (kk) {
        var bucket = devMapDetail[kk[0]], kind = kk[1], label = kk[2];
        if (kind === "switch" && !Object.keys(bucket).length) return;
        for (var slot = 0; slot < 32; slot++) {
          if (slot % 8 === 0) {
            wrap.appendChild(el("div", "pgroup",
              "PAGE " + (slot / 8 + 1) + " · " + label + "S " +
              (slot + 1) + "–" + (slot + 8)));
          }
          wrap.appendChild(buildDevCtlRow(kind, slot, bucket[String(slot)]));
          if (devCtlSel && devCtlSel.kind === kind && devCtlSel.slot === slot
              && bucket[String(slot)]) {
            wrap.appendChild(buildInlineEditor(kind, slot,
                                               bucket[String(slot)]));
          }
        }
      });
    if (!occupied) {
      wrap.prepend(el("p", "hint", "no controls stored in this map"));
    }
    $("#devmap-foot").textContent = "click a row to edit it in place · " +
      "drag a control onto another slot to move it (occupied slots swap)";
  }

  function buildDevCtlRow(kind, slot, c) {
    var isSel = devCtlSel && devCtlSel.kind === kind && devCtlSel.slot === slot;
    var row = el("div", "trow devmap-grid" +
      (c ? "" : " empty-slot") + (isSel ? " selected" : ""));
    var dot = el("i", "colordot");
    if (c) {
      dot.style.background = pal(c.colour || 0).bg;
    }
    row.append(
      el("span", "pnum", (kind === "knob" ? "knob " : "btn ") + (slot + 1)),
      el("span", "pname", c ? (c.name || "(unnamed)") : "—"),
      el("span", "", c ? String(c.param_index) : ""),
      el("span", "", c ? c.min + "–" + c.max : ""),
      el("span", "", c ? (c.steps ? String(c.steps) : "cont") : ""),
      dot);
    if (c && c.ref) {
      row.title = "DAW: " + c.ref.param_name +
        (c.ref.display ? " · was " + c.ref.display : "") +
        (c.ref.fx_name ? "  (" + c.ref.fx_name + ")" : "");
    }
    if (c) {
      row.onclick = function () {
        var same = devCtlSel && devCtlSel.kind === kind && devCtlSel.slot === slot;
        devCtlSel = same ? null : { kind: kind, slot: slot };
        renderDevMapControls();
      };
      row.draggable = true;
      row.ondragstart = function (ev) {
        dragSrc = { kind: kind, slot: slot };
        ev.dataTransfer.effectAllowed = "move";
      };
    }
    // every slot (incl. empty) is a drop target within the same kind
    row.ondragover = function (ev) {
      if (dragSrc && dragSrc.kind === kind && dragSrc.slot !== slot) {
        ev.preventDefault();
        row.classList.add("droptarget");
      }
    };
    row.ondragleave = function () { row.classList.remove("droptarget"); };
    row.ondrop = function (ev) {
      ev.preventDefault();
      row.classList.remove("droptarget");
      if (!dragSrc || dragSrc.kind !== kind || dragSrc.slot === slot) return;
      var src = dragSrc; dragSrc = null;
      setFoot("moving " + kind + " " + (src.slot + 1) + " → " + (slot + 1) + "…");
      rpc("move_device_plugin_control", {
        hash: devMapSel.hash, kind: kind,
        from_slot: src.slot, to_slot: slot
      }).then(function (r) {
        delete mapCache[devMapSel.hash];
        devCtlSel = null;
        setFoot(r.swap ? "controls swapped" : "control moved");
        selectDevMap(devMapSel, true);
      }).catch(function (e) { setFoot("move failed: " + e.message); });
    };
    return row;
  }

  function buildInlineEditor(kind, slot, c) {
    var box = el("div", "inline-editor");
    var fields = el("div", "ed-fields");
    var preview = el("div", "ed-preview");
    box.append(fields, preview);

    var name = el("input"); name.maxLength = 12; name.value = c.name;
    var budget = el("span", "budget", (12 - c.name.length) + " left");
    var g = el("div", "grid2");
    var mn = el("input"); mn.type = "number"; mn.value = c.min;
    var mx = el("input"); mx.type = "number"; mx.value = c.max;
    g.append(field("Min", mn), field("Max", mx));
    var steps = el("input"); steps.type = "number"; steps.value = c.steps;
    var stepWrap = el("div", "stepnames");
    var stepInputs = [];
    var colour = c.colour || 0;
    var ledOn = c.led_on || 0, ledOff = c.led_off || 0;

    // device ranges: 14-bit values, 0 or 2–10 detents (1 is meaningless)
    var readMin = boundNum(mn, 0, 16383, function () { renderPreview(); });
    var readMax = boundNum(mx, 0, 16383, function () { renderPreview(); });
    var readStepsRaw = boundNum(steps, 0, 10, function () {
      steps.value = readSteps(); renderStepNames(); renderPreview();
    });
    function readSteps() { return coerceSteps(readStepsRaw()); }

    // haptics for continuous knobs: smooth / centre detent / up to two
    // custom detent positions (0-127 across the 300° sweep; blank = none)
    var haptic, ind1, ind2, readInd1, readInd2;
    function indentGuard(inp) {
      inp.min = 0; inp.max = 127; inp.placeholder = "—";
      inp.addEventListener("change", function () {
        if (inp.value !== "") inp.value = clampInt(inp.value, 0, 127);
        renderPreview();
      });
      return function () {
        return inp.value === "" ? null : clampInt(inp.value, 0, 127);
      };
    }
    if (kind === "knob") {
      haptic = el("select");
      [["smooth 300°", "300"], ["centre detent", "centre"]]
        .forEach(function (o) {
          var opt = el("option", "", o[0]); opt.value = o[1];
          opt.selected = (c.haptic === 2) === (o[1] === "centre");
          haptic.appendChild(opt);
        });
      haptic.onchange = renderPreview;
      ind1 = el("input"); ind1.type = "number";
      readInd1 = indentGuard(ind1);
      if (c.indent1 != null && c.indent1 !== 255) ind1.value = c.indent1;
      ind2 = el("input"); ind2.type = "number";
      readInd2 = indentGuard(ind2);
      if (c.indent2 != null && c.indent2 !== 255) ind2.value = c.indent2;
      ind1.oninput = ind2.oninput = renderPreview;
    }

    // live preview of what the ROTO will show: mini display (name in the control
    // colour + readout) over the physical control
    function renderPreview() {
      preview.replaceChildren();
      var col = pal(colour).bg;
      var n = readSteps();
      var screen = el("div", "mini-screen");
      screen.appendChild(el("div", "ms-name", name.value || "(unnamed)"));
      screen.querySelector(".ms-name").style.color = col;
      var readout = kind === "knob"
        ? (n >= 2 ? (stepInputs[0] && stepInputs[0].value || "step 1") : readMin())
        : (stepInputs[0] && stepInputs[0].value || "on");
      screen.appendChild(el("div", "ms-value", String(readout)));
      preview.appendChild(screen);

      if (kind === "knob") {
        var face = el("div", "knobface");
        var deg = 108;                       // preview position ~40%
        face.style.background = "conic-gradient(from 225deg, " + col + " " +
          deg + "deg, var(--surface-lo) " + deg + "deg 270deg, transparent 270deg)";
        var cap = el("div", "knobcap");
        var ptr = el("div", "pointer");
        ptr.style.transform = "translate(-50%,-100%) rotate(" + (deg - 135) + "deg)";
        cap.appendChild(ptr);
        // detent ticks: stepped knobs get N, continuous get the haptic
        // centre / custom indent positions
        if (n >= 2) {
          for (var i = 0; i < n; i++) {
            var t = el("i", "tick");
            t.style.transform = "rotate(" + (-135 + (270 / (n - 1)) * i) +
              "deg) translateY(-31px)";
            face.appendChild(t);
          }
        } else {
          var marks = [];
          if (haptic && haptic.value === "centre") marks.push(63.5);
          if (readInd1 && readInd1() !== null) marks.push(readInd1());
          if (readInd2 && readInd2() !== null) marks.push(readInd2());
          marks.forEach(function (pos) {
            var t = el("i", "tick");
            t.style.transform = "rotate(" + (-135 + 270 * (pos / 127)) +
              "deg) translateY(-31px)";
            face.appendChild(t);
          });
        }
        face.appendChild(cap);
        preview.appendChild(face);
        preview.appendChild(el("div", "hint",
          n >= 2 ? n + " detents"
                 : "continuous · " + readMin() + "–" + readMax()));
      } else {
        var btn = el("div", "preview-btn");
        btn.textContent = name.value || "BTN";
        var on = pal(ledOn).bg;
        btn.style.borderColor = on;
        btn.style.boxShadow = "0 0 12px " + on;
        btn.style.color = pal(ledOn).fg || "#fff";
        btn.style.background = on;
        preview.appendChild(btn);
        var chips = el("div", "ledchips");
        var c1 = el("span", "ledchip"); c1.style.background = pal(ledOn).bg;
        var c2 = el("span", "ledchip"); c2.style.background = pal(ledOff).bg;
        chips.append(el("span", "hint", "on"), c1,
                     el("span", "hint", "off"), c2);
        preview.appendChild(chips);
      }
    }

    name.oninput = function () {
      budget.textContent = (12 - name.value.length) + " left";
      renderPreview();
    };
    mn.oninput = mx.oninput = renderPreview;

    function renderStepNames() {
      stepWrap.replaceChildren();
      stepInputs = [];
      for (var i = 0; i < readSteps(); i++) {
        var si = el("input"); si.maxLength = 12;
        si.value = c.step_names[i] || "";
        si.placeholder = "step " + (i + 1);
        si.oninput = renderPreview;
        stepWrap.appendChild(field("Step " + (i + 1), si));
        stepInputs.push(si);
      }
    }
    steps.oninput = function () { renderStepNames(); renderPreview(); };
    renderStepNames();

    var swrow = buildSwatchRow(colour, function (idx) {
      colour = idx;
      renderPreview();
    });

    var write = el("button", "chipbtn primary", "Save");
    write.onclick = function () {
      write.disabled = true; write.textContent = "saving…";
      var f = { name: name.value.slice(0, 12), min: readMin(), max: readMax(),
                colour: colour, steps: readSteps(),
                step_names: stepInputs.map(function (i) {
                  return i.value.slice(0, 12);
                }) };
      if (kind === "switch") { f.led_on = ledOn; f.led_off = ledOff; }
      if (kind === "knob") {
        f.haptic = haptic.value;
        f.indent1 = readInd1(); f.indent2 = readInd2();
      }
      rpc("set_device_plugin_control", {
        hash: devMapSel.hash, kind: kind, slot: slot, fields: f
      }).then(function () {
        delete mapCache[devMapSel.hash];
        setFoot("saved to device");
        selectDevMap(devMapSel, true);
      }).catch(function (e) {
        write.disabled = false; write.textContent = "Save";
        setFoot("save failed: " + e.message);
      });
    };

    // what the DAW said this control really is (full name, learn-time value)
    if (c.ref) {
      fields.appendChild(el("p", "hint refline",
        "DAW: " + c.ref.param_name +
        (c.ref.display ? " · was " + c.ref.display : "") +
        (c.ref.fx_name ? " (" + c.ref.fx_name + ")" : "")));
    }
    var row1 = el("div", "grid2");
    row1.append(field("Display name", name, budget), field("Steps (0=cont)", steps));
    fields.append(row1, g, stepWrap, field("Color", swrow));
    if (kind === "knob") {
      var hrow = el("div", "grid2");
      var indGrid = el("div", "grid2");
      indGrid.append(field("Detent pos 1", ind1), field("Detent pos 2", ind2));
      hrow.append(field("Haptic (continuous)", haptic), indGrid);
      fields.appendChild(hrow);
    }
    if (kind === "switch") {
      var ledRow = el("div", "grid2");
      var lo = el("input"); lo.type = "number"; lo.value = ledOn;
      var readLo = boundNum(lo, 0, PALETTE.length - 1, renderPreview);
      lo.oninput = function () { ledOn = readLo(); renderPreview(); };
      var lf = el("input"); lf.type = "number"; lf.value = ledOff;
      var readLf = boundNum(lf, 0, PALETTE.length - 1, renderPreview);
      lf.oninput = function () { ledOff = readLf(); renderPreview(); };
      ledRow.append(field("LED on (color #)", lo), field("LED off (color #)", lf));
      fields.appendChild(ledRow);
    }
    fields.appendChild(write);
    renderPreview();
    return box;
  }

  function renderDevicePanel() {
    var wrap = $("#linkpanel");
    wrap.replaceChildren();
    renderLinkSection(wrap);
  }

  function refreshLinks(msg) {
    return rpc("get_plugin_links").then(function (l) {
      pluginLinks = l;
      mappingBadges = {};                // re-derive against the new links
      renderDevMaps(); renderDevicePanel(); loadPluginView();
      if (msg) setFoot(msg);
    });
  }

  // fuzzy plugin<->map name matching (SUGGESTION only; the user confirms the
  // link — no silent auto-matching). One plugin is spelled differently by every
  // host/format ("SQ3" vs "SQ3 x64"), so fold both to a bare alnum token, then
  // score prefix/substring/bigram similarity.
  function canonPlugin(s) {
    return (s || "").toLowerCase()
      .replace(/^\s*(aui?|vsti?|vst3i?|aax|clap|au)\s*:\s*/, "")   // host/format
      .replace(/\s*\([^)]*\)\s*$/, "")                             // (vendor) tail
      .replace(/\s*(x64|x86|64[\s-]?bit|32[\s-]?bit)\s*$/, "")     // arch tag
      .replace(/[^a-z0-9]+/g, "");                                 // fold to alnum
  }
  function diceCoef(a, b) {
    if (a.length < 2 || b.length < 2) return a === b ? 1 : 0;
    var ga = {}, inter = 0, na = 0, nb = 0, i, k;
    for (i = 0; i < a.length - 1; i++) { ga[a.substr(i, 2)] = 1; }
    for (k in ga) { na++; }
    var seen = {};
    for (i = 0; i < b.length - 1; i++) {
      var g = b.substr(i, 2); nb++;
      if (ga[g] && !seen[g]) { inter++; seen[g] = 1; }
    }
    return (2 * inter) / (na + nb);
  }
  function fuzzyScore(plug, map) {
    var a = canonPlugin(plug), b = canonPlugin(map);
    if (!a || !b) return 0;
    if (a === b) return 1;                                    // same after folding
    if (b.indexOf(a) === 0 || a.indexOf(b) === 0) return 0.85; // one is a prefix
    if (b.indexOf(a) >= 0 || a.indexOf(b) >= 0) return 0.65;   // one contains other
    return diceCoef(a, b);                                     // loose similarity
  }
  function bestMapFor(pluginName) {                            // top contender >=0.5
    var best = null;
    devMaps.forEach(function (m) {
      var s = fuzzyScore(pluginName, m.name);
      if (s >= 0.5 && (!best || s > best.score)) best = { map: m, score: s };
    });
    return best;
  }

  function renderLinkSection(wrap) {
    $("#link-map-name").textContent = devMapSel ? devMapSel.name : "";
    // What the DAW has: every live plugin, ALWAYS visible (no map-first gate).
    // A plugin added to a track shows here immediately (devices event) with a
    // fuzzy-suggested on-device map to link to.
    wrap.appendChild(el("p", "caps", (state.daw || "DAW") + " plugins"));
    if (!state.devices.length) {
      wrap.appendChild(el("p", "hint", feedHint() ||
        ("no plugin in view — add or focus one in " + (state.daw || "your DAW"))));
    }
    state.devices.forEach(function (d) {
      if (!d.name) return;                 // empty slot (e.g. no instrument) — skip
      var link = pluginLinks[d.name];
      if (link) {                                     // already linked -> unlink
        var lb = el("button", "chainrow linked");
        lb.append(el("span", "chain", "🔗"), el("span", "", d.name),
                  el("span", "spacer"),
                  el("span", "hint", "🔗 " + link.device_name + " — click to unlink"));
        lb.title = "unlink " + d.name + " from " + link.device_name;
        lb.onclick = function () {
          rpc("unlink_plugin", { reaper_name: d.name })
            .then(function () { refreshLinks("unlinked " + d.name); });
        };
        wrap.appendChild(lb);
        return;
      }
      // unlinked: an explicitly-selected map wins; else the fuzzy contender
      var sugg = devMapSel ? null : bestMapFor(d.name);
      var target = devMapSel || (sugg && sugg.map);
      var b = el("button", "chainrow" + (sugg ? " suggest" : ""));
      var tail = devMapSel ? ("link to " + devMapSel.name)
               : sugg ? ("≈ " + sugg.map.name + " — click to link")
               : "no match — pick a map at left";
      b.append(el("span", "chain off", sugg ? "≈" : "⛓"), el("span", "", d.name),
               el("span", "spacer"), el("span", "hint", tail));
      if (target) {
        b.title = "link " + d.name + " ↔ " + target.name;
        b.onclick = function () {
          rpc("link_plugin", { reaper_name: d.name, hash: target.hash,
                               device_name: target.name })
            .then(function () {
              refreshLinks("🔗 " + d.name + " ↔ " + target.name +
                " — the device re-attaches now");
            });
        };
      }
      wrap.appendChild(b);
    });

    if (!devMapSel) {
      wrap.appendChild(el("p", "hint",
        "“≈” is a fuzzy name match — click to link, or pick a map at left to choose it yourself"));
      return;
    }

    // Links from OTHER DAWs (same map, a name not live now) are deliberately NOT
    // shown: the user only cares about the DAW they're in. Cross-DAW links still
    // work silently (learn once, use everywhere) — manage each from within the
    // DAW that uses it, where it appears in the live list.

    // destructive zone: remove the map from device flash (two-step confirm)
    wrap.appendChild(el("hr"));
    var del = el("button", "dangerbtn");
    armConfirm(del, "Delete “" + devMapSel.name + "” from device…",
               "Click again to permanently delete", function () {
      rpc("delete_device_plugin", { hash: devMapSel.hash }).then(function () {
        delete mapCache[devMapSel.hash];
        devMapSel = null; devMapDetail = null; devCtlSel = null;
        $("#devmap-title").textContent = "—";
        $("#devmap-controls").replaceChildren();
        loadDeviceMaps();
        renderDevicePanel();
        setFoot("map deleted from device");
      }).catch(function (e) { setFoot("delete failed: " + e.message); });
    });
    wrap.appendChild(del);
  }

  $("#btn-devmaps-refresh").onclick = function () {
    mapCache = {};
    loadDeviceMaps();
  };

  $("#btn-devmap-rename").onclick = function () {
    if (!devMapSel) return;
    var title = $("#devmap-title");
    var input = el("input");
    input.maxLength = 12; input.value = devMapSel.name;
    input.className = "search";
    title.replaceChildren(input);
    input.focus(); input.select();
    var done = false;
    function finish(commit) {
      if (done) return;
      done = true;
      var newName = input.value.trim().slice(0, 12);
      if (!commit || !newName || newName === devMapSel.name) {
        selectDevMap(devMapSel);   // restores the title
        return;
      }
      rpc("rename_device_plugin", { hash: devMapSel.hash, name: newName })
        .then(function () {
          devMapSel.name = newName;
          setFoot("renamed on device");
          loadDeviceMaps();
          selectDevMap(devMapSel);
          refreshLinks();
        }).catch(function (e) {
          setFoot("rename failed: " + e.message);
          selectDevMap(devMapSel);
        });
    }
    input.onkeydown = function (ev) {
      if (ev.key === "Enter") finish(true);
      if (ev.key === "Escape") finish(false);
    };
    input.onblur = function () { finish(true); };
  };

  /* ================= SETUPS ================= */
  var KNOB_FIELDS = { name: "", mode: "CC7", channel: 1, param: 0, nrpn: 0,
    min: 0, max: 127, colour: 0, haptic: "KNOB", steps: 0 };
  var SWITCH_FIELDS = { name: "", mode: "CC7", channel: 1, param: 0, nrpn: 0,
    min: 0, max: 127, colour: 0, led_on: 0, led_off: 0, toggle: true };
  var setups = [], curSetup = null, curIndex = null, sel = null;
  var curKind = "knob", curPage = 0, setupFilter = "", multiSel = [];

  function loadSetups(refreshCurrent) {
    rpc("list_setups").then(function (list) {
      setups = list;
      if (curIndex === null && list.length) curIndex = list[0].index;
      renderSetupList();
      renderLibrary();
      if (refreshCurrent !== false && curIndex !== null) loadSetup(curIndex);
    });
  }

  function loadSetup(i) {
    curIndex = i;
    rpc("get_setup", { index: i }).then(function (s) {
      curSetup = s;
      renderSetupList(); renderControlTable(); renderWriteBar();
      if (sel) renderInspector();
    });
  }

  function renderSetupList() {
    var wrap = $("#setuplist");
    wrap.replaceChildren();
    setups.forEach(function (s) {
      var row = el("div", "lrow" + (s.index === curIndex ? " selected" : ""));
      var active = state.activeSetup === s.index;
      if (active) row.title = "active on the device";
      row.append(el("span", "idx mono", String(s.index).padStart(2, "0")),
                 el("span", "", (active ? "▸ " : "") + (s.name || "—")),
                 el("span", "spacer"),
                 el("span", s.dirty ? "dirty mono" : "mono " +
                    (s.deployed ? "" : "faint"),
                    s.dirty ? "●" : (s.deployed ? "✓" : "")));
      row.onclick = function () {
        sel = null; multiSel = []; loadSetup(s.index); renderInspector();
      };
      wrap.appendChild(row);
    });
    if (!setups.length) {
      wrap.appendChild(el("p", "hint", "library is empty — connect the device " +
        "and use Library → Snapshot to pull its setups"));
    }
  }

  function controlOf(kind, slot) {
    if (!curSetup) return null;
    var bucket = kind === "knob" ? curSetup.knobs : curSetup.switches;
    return bucket[String(slot)] || null;
  }

  function addrLabel(c) {
    return c.mode.indexOf("NRPN") === 0 ? "NRPN " + c.nrpn :
           c.mode === "PC" ? "PC " + c.param :
           c.mode === "NOTE" ? "NOTE " + c.param : "CC " + c.param;
  }

  function hapticLabel(c) {
    if (c.haptic === "STEP") return "step·" + (c.steps || 2);
    if (c.haptic === "INDENT") return "indent";
    return "cont";
  }

  // slots the device reserves for its own DAW-mode CCs — flag inline
  var RESERVED_CCS = [12, 13, 14, 15, 16, 17, 18, 19];

  function renderControlTable() {
    var wrap = $("#controltable");
    wrap.replaceChildren();
    renderPageChips();
    if (!curSetup) return;
    var filter = setupFilter.toLowerCase();
    var start = curPage * 8, end = start + 8;
    var any = false;
    for (var slot = 0; slot < 32; slot++) {
      if (!filter && (slot < start || slot >= end)) continue;
      var c = controlOf(curKind, slot);
      if (filter) {
        var hay = (c ? c.name + " " + addrLabel(c) : "") + " " + (slot + 1);
        if (hay.toLowerCase().indexOf(filter) < 0) continue;
      }
      if (!any || (slot % 8 === 0 && filter)) {
        wrap.appendChild(el("div", "pgroup",
          curKind.toUpperCase() + "S · PAGE " + (Math.floor(slot / 8) + 1) +
          " · " + (Math.floor(slot / 8) * 8 + 1) + "–" + (Math.floor(slot / 8) * 8 + 8)));
        any = true;
      }
      var row = el("div", "trow setup-grid" + (c ? "" : " empty-slot") +
        (sel && sel.kind === curKind && sel.slot === slot ? " selected" : "") +
        (multiSel.indexOf(slot) >= 0 ? " multi" : ""));
      var reserved = c && c.mode.indexOf("CC") === 0 &&
        RESERVED_CCS.indexOf(+c.param) >= 0 && +c.channel === 16;
      var dot = el("i", "colordot");
      if (c) {
        dot.style.background = pal(c.colour).bg;
        dot.style.boxShadow = "0 0 4px " + pal(c.colour).bg;
      }
      row.append(
        el("span", "pnum", slot + 1),
        dot,
        el("span", "pname" + (reserved ? " dirty" : ""),
           c ? (c.name || "CH:" + String(c.channel).padStart(2, "0") + "/" + addrLabel(c))
             : "—"),
        el("span", "", c ? c.mode : ""),
        el("span", "", c ? String(c.channel) : ""),
        el("span", "", c ? addrLabel(c) : ""),
        el("span", "", c ? c.min + "–" + c.max : ""),
        el("span", "", c && curKind === "knob" ? hapticLabel(c)
           : (c ? (c.toggle ? "toggle" : "push") : "")),
        el("span", curSetup.dirty && c ? "dirty" : "", curSetup.dirty && c ? "●" : ""));
      row.onclick = (function (sl) {
        return function (ev) {
          if (ev.shiftKey) {
            var at = multiSel.indexOf(sl);
            if (at >= 0) multiSel.splice(at, 1); else multiSel.push(sl);
          } else {
            multiSel = []; sel = { kind: curKind, slot: sl };
          }
          renderControlTable(); renderInspector(); renderBulkBar();
        };
      })(slot);
      wrap.appendChild(row);
    }
  }

  function renderPageChips() {
    var wrap = $("#pagechips");
    wrap.replaceChildren();
    for (var p = 0; p < 4; p++) {
      var b = el("button", p === curPage ? "active" : "", "P" + (p + 1));
      b.onclick = (function (pp) {
        return function () { curPage = pp; renderControlTable(); };
      })(p);
      wrap.appendChild(b);
    }
  }

  function renderBulkBar() {
    var on = multiSel.length > 1;
    $("#bulk-hint").hidden = on;
    $("#bulk-controls").hidden = !on;
    if (on) $("#bulk-hint").textContent = "";
    else $("#bulk-hint").textContent =
      "⇧click to select multiple rows — then set CH, MODE, COLOR once for all";
  }

  $("#bulk-apply").onclick = function () {
    if (!curSetup || multiSel.length < 2) return;
    var ch = $("#bulk-ch").value, mode = $("#bulk-mode").value,
        colour = $("#bulk-colour").value;
    var chain = Promise.resolve();
    multiSel.forEach(function (slot) {
      var c = controlOf(curKind, slot);
      if (!c) return;
      var fields = Object.assign({}, c);
      if (ch) fields.channel = clampInt(ch, 1, 16);
      if (mode) fields.mode = mode;
      if (colour) fields.colour = clampInt(colour, 0, PALETTE.length - 1);
      chain = chain.then(function () {
        return rpc("update_control",
          { index: curIndex, kind: curKind, slot: slot, fields: fields });
      });
    });
    chain.then(function () { multiSel = []; renderBulkBar(); });
  };

  function renderWriteBar() {
    var st = $("#write-status");
    if (!curSetup) { st.textContent = ""; return; }
    st.textContent = curSetup.dirty ? "● unsaved changes — write is explicit (flash)"
      : (curSetup.deployed ? "✓ on device" : "not deployed");
    st.style.color = curSetup.dirty ? "var(--dirty)"
      : (curSetup.deployed ? "var(--ok-strong)" : "");
  }

  function field(labelText, input, extra) {
    var f = el("div", "field");
    var lab = el("label", "", labelText);
    if (extra) lab.appendChild(extra);
    f.append(lab, input);
    return f;
  }

  function renderInspector() {
    var wrap = $("#inspector");
    wrap.replaceChildren();
    if (!sel || !curSetup) {
      $("#insp-title").textContent = "Inspector";
      $("#insp-sub").textContent = "";
      wrap.appendChild(el("p", "hint", "select a control"));
      return;
    }
    var kind = sel.kind, slot = sel.slot;
    $("#insp-title").textContent = kind.toUpperCase() + " " + (slot + 1);
    $("#insp-sub").textContent = "page " + (Math.floor(slot / 8) + 1) +
      " · slot 0x" + slot.toString(16).padStart(2, "0");
    var defaults = kind === "knob" ? KNOB_FIELDS : SWITCH_FIELDS;
    var c = Object.assign({}, defaults, controlOf(kind, slot) || {});

    var name = el("input"); name.maxLength = 12; name.value = c.name;
    var budget = el("span", "budget", (12 - c.name.length) + " left");
    name.oninput = function () {
      var nonAscii = /[^\x20-\x7e]/.test(name.value);
      budget.textContent = nonAscii ? "non-ASCII becomes ?" :
        (12 - name.value.length) + " left";
      budget.className = "budget" + (nonAscii ? " warn" :
        (name.value.length >= 12 ? " over" : ""));
    };
    wrap.appendChild(field("Display name", name, budget));

    var mode = el("select");
    (kind === "knob" ? ["CC7", "CC14", "NRPN7", "NRPN14"]
                     : ["CC7", "CC14", "NRPN7", "NRPN14", "PC", "NOTE"])
      .forEach(function (m) {
        var o = el("option", "", m); o.value = m; o.selected = m === c.mode;
        mode.appendChild(o);
      });
    var res = el("span", "budget",
      c.mode.indexOf("14") > 0 ? "FINE 0–16383" : "COARSE 0–127");
    function resMax() { return mode.value.indexOf("14") > 0 ? 16383 : 127; }
    wrap.appendChild(field("Mode", mode, res));

    var g1 = el("div", "grid2");
    var ch = el("input"); ch.type = "number"; ch.value = c.channel;
    var readCh = boundNum(ch, 1, 16);
    var pr = el("input"); pr.type = "number"; pr.value = c.param;
    var readPr = boundNum(pr, 0, 127);
    g1.append(field("MIDI ch", ch), field("CC / value", pr));
    wrap.appendChild(g1);

    var nr = el("input"); nr.type = "number"; nr.value = c.nrpn;
    var readNr = boundNum(nr, 0, 16383);
    wrap.appendChild(field("NRPN address", nr));

    var g2 = el("div", "grid2");
    var mn = el("input"); mn.type = "number"; mn.value = c.min;
    var mx = el("input"); mx.type = "number"; mx.value = c.max;
    var readMn = boundNum(mn, 0, resMax);
    var readMx = boundNum(mx, 0, resMax);
    mode.onchange = function () {    // switching resolution re-bounds min/max
      res.textContent = mode.value.indexOf("14") > 0
        ? "FINE 0–16383" : "COARSE 0–127";
      mn.max = mx.max = resMax();
      mn.value = readMn(); mx.value = readMx();
    };
    g2.append(field("Min", mn), field("Max", mx));
    wrap.appendChild(g2);

    var colour = c.colour;
    var swLabel = el("span", "budget", "index " + colour);
    var swrow = buildSwatchRow(colour, function (idx) {
      colour = idx;
      swLabel.textContent = "index " + idx;
    });
    wrap.appendChild(field("Color (device palette)", swrow, swLabel));

    var haptic, steps, toggle, ledOn, ledOff;
    if (kind === "knob") {
      haptic = el("select");
      ["KNOB", "STEP", "INDENT"].forEach(function (h) {
        var o = el("option", "", h); o.value = h; o.selected = h === c.haptic;
        haptic.appendChild(o);
      });
      steps = el("input"); steps.type = "number"; steps.value = c.steps;
      var readStepsRaw = boundNum(steps, 0, 10, function () {
        steps.value = readSetupSteps();
      });
      var g3 = el("div", "grid2");
      g3.append(field("Type", haptic), field("Steps", steps));
      wrap.appendChild(g3);
    } else {
      toggle = el("select");
      [["TOGGLE", true], ["PUSH", false]].forEach(function (t) {
        var o = el("option", "", t[0]); o.value = t[0];
        o.selected = c.toggle === t[1];
        toggle.appendChild(o);
      });
      var g4 = el("div", "grid2");
      ledOn = el("input"); ledOn.type = "number"; ledOn.value = c.led_on;
      var readLedOn = boundNum(ledOn, 0, PALETTE.length - 1);
      ledOff = el("input"); ledOff.type = "number"; ledOff.value = c.led_off;
      var readLedOff = boundNum(ledOff, 0, PALETTE.length - 1);
      wrap.appendChild(field("Behaviour", toggle));
      g4.append(field("On color", ledOn), field("Off color", ledOff));
      wrap.appendChild(g4);
    }

    function readSetupSteps() { return coerceSteps(readStepsRaw()); }

    var actions = el("div", "insp-actions");
    var apply = el("button", "chipbtn primary", "Apply");
    apply.onclick = function () {
      var fields = { name: name.value.slice(0, 12), mode: mode.value,
        channel: readCh(), param: readPr(), nrpn: readNr(),
        min: readMn(), max: readMx(), colour: colour };
      if (kind === "knob") {
        fields.haptic = haptic.value; fields.steps = readSetupSteps();
      } else {
        fields.toggle = toggle.value === "TOGGLE";
        fields.led_on = readLedOn(); fields.led_off = readLedOff();
      }
      rpc("update_control", { index: curIndex, kind: kind, slot: slot,
        fields: fields });
    };
    var clear = el("button", "chipbtn", "Clear slot");
    clear.onclick = function () {
      rpc("update_control", { index: curIndex, kind: kind, slot: slot,
        fields: null });
    };
    actions.append(apply, clear);
    wrap.appendChild(actions);
  }

  $("#btn-deploy").onclick = function () {
    if (curIndex !== null) rpc("deploy_setup", { index: curIndex });
  };
  $("#btn-activate").onclick = function () {
    if (curIndex === null) return;
    rpc("activate_setup", { index: curIndex }).then(function () {
      state.activeSetup = curIndex;
      renderSetupList();
      setFoot("setup " + (curIndex + 1) + " active on device");
    }).catch(function (e) { setFoot("activate failed: " + e.message); });
  };
  armConfirm($("#btn-clear-slot"), "Clear device slot…",
             function () { return "Click again to wipe slot " + (curIndex + 1); },
             function () {
    rpc("clear_device_setup", { index: curIndex }).then(function () {
      setFoot("device slot " + (curIndex + 1) + " cleared (library copy kept)");
      loadSetups();
    }).catch(function (e) { setFoot("clear failed: " + e.message); });
  }, function () { return curIndex !== null; });
  $("#setup-search").oninput = function () {
    setupFilter = this.value.trim(); renderControlTable();
  };
  $$("#kindseg .seg-btn").forEach(function (b) {
    b.onclick = function () {
      $$("#kindseg .seg-btn").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      curKind = b.dataset.kind; sel = null; multiSel = [];
      renderControlTable(); renderInspector(); renderBulkBar();
    };
  });
  $("#btn-setup-export").onclick = function () {
    if (curIndex === null) return;
    rpc("export_setup", { index: curIndex }).then(function (data) {
      $("#lib-json").value = JSON.stringify(data, null, 1);
      switchView("library");
      $("#lib-msg").textContent = "exported setup " + curIndex;
    });
  };
  $("#btn-setup-import").onclick = function () {
    switchView("library");
    $("#lib-json").focus();
  };

  /* ================= LIBRARY ================= */
  function renderLibrary() {
    var wrap = $("#liblist");
    wrap.replaceChildren();
    if (!setups.length) {
      wrap.appendChild(el("p", "hint",
        "empty — snapshot the device to build your archive (needs the serial link)"));
    }
    setups.forEach(function (s) {
      var row = el("div", "lrow");
      var chipCls = s.dirty ? "differs" : (s.deployed ? "sync" : "");
      var chipTxt = s.dirty ? "differs" : (s.deployed ? "in sync" : "local only");
      row.append(el("span", "idx mono", String(s.index).padStart(2, "0")),
                 el("span", "", s.name || "—"),
                 el("span", "hint", s.knobs + "k · " + s.switches + "s"),
                 el("span", "spacer"),
                 el("span", "lib-chip " + chipCls, chipTxt));
      var exp = el("button", "chipbtn small", "Export");
      exp.onclick = function () {
        rpc("export_setup", { index: s.index }).then(function (data) {
          $("#lib-json").value = JSON.stringify(data, null, 1);
          $("#lib-msg").textContent = "exported setup " + s.index;
        });
      };
      row.appendChild(exp);
      wrap.appendChild(row);
    });
  }

  $("#btn-import").onclick = function () {
    var msg = $("#lib-msg");
    var data;
    try {
      data = JSON.parse($("#lib-json").value);
    } catch (e) {
      msg.textContent = "invalid JSON";
      return;
    }
    rpc("import_setup", { data: data, index: clampInt($("#lib-slot").value, 0, 63) })
      .then(function (r) { msg.textContent = "imported into slot " + r.index; })
      .catch(function (err) { msg.textContent = "error: " + err.message; });
  };
  $("#btn-dump").onclick = function () {
    rpc("dump_device", {})
      .then(function (r) { setFoot("snapshot complete: " + r.setups + " setups"); })
      .catch(function (e) { setFoot("snapshot failed: " + e.message); });
  };

  /* ================= DIAGNOSTICS ================= */
  var frameCount = 0;
  function renderTraceRow(d) {
    frameCount++;
    if (paused || !$("#view-diag").classList.contains("active")) return;
    var host = $("#trace"); if (!host) return;
    // axis Cubase(left) ▸ Athens ▸ ROTO(right); ▶ = data moving toward the ROTO
    var toRoto = (d.side === "cubase" && d.dir === "rx") ||
                 (d.side === "roto" && d.dir === "tx");
    var cub = pcell(d.side === "cubase" ? d.label : "", "16em", true);
    if (d.side === "cubase") cub.style.color = "var(--selecting)";
    var roto = pcell(d.side === "roto" ? d.label : "", "16em", true);
    if (d.side === "roto") roto.style.color = "var(--ok)";
    var row = el("div", "");
    row.style.whiteSpace = "nowrap";            // keep the 4 cells on one line
    row.title = d.hex || "";                    // untrusted → textContent/title
    row.append(cub, pcell(toRoto ? "▶" : "◀", "2em"), roto,
               pcell(d.comment || "", "12em"));
    host.prepend(row);
    while (host.children.length > 300) host.removeChild(host.lastChild);
    $("#feed-count").textContent = frameCount + " frames";
  }

  // live per-param table: device knob-in -> DAW value -> display string. Sweeping
  // shows whether the value moves smoothly (fine), in steps (enum), or barely
  // (log taper) — the tell for controls that "don't work" on specific plugins.
  var diagParams = {};
  function pcell(txt, w, mono) {
    var s = el("span", mono ? "mono" : "", txt);
    s.style.cssText = "display:inline-block;width:" + w +
      ";overflow:hidden;white-space:nowrap;vertical-align:top";
    return s;
  }
  function renderDiagParams() {
    if (!DIAG_MODE) return;
    var host = $("#param-diag"); if (!host) return;
    host.textContent = "";
    var idxs = Object.keys(diagParams).map(Number).sort(function (a, b) { return a - b; });
    if (!idxs.length) {
      host.append(el("div", "hint", "turn a plugin knob on the device to populate"));
      return;
    }
    idxs.forEach(function (i) {
      var p = diagParams[i];
      var name = (state.params && state.params[i] && state.params[i].name) || ("param " + i);
      var row = el("div", "frow");
      row.append(pcell(String(i), "2.5em", true), pcell(name, "11em"),
                 pcell(p.devIn == null ? "—" : p.devIn.toFixed(3), "5em", true),
                 pcell("→", "1.5em"),
                 pcell(p.daw == null ? "—" : p.daw.toFixed(3), "5em", true),
                 pcell(p.display || "", "12em"));
      host.append(row);
    });
  }
  $("#btn-pause").onclick = function () {
    paused = !paused;
    $("#btn-pause").textContent = paused ? "resume" : "pause";
  };
  $("#btn-act-pause").onclick = function () {
    actPaused = !actPaused;
    $("#btn-act-pause").textContent = actPaused ? "resume" : "pause";
  };

  /* ================= CHROME ================= */
  function switchView(name) {
    $$("#nav .seg-btn").forEach(function (b) {
      b.classList.toggle("active", b.dataset.view === name);
    });
    $$(".view").forEach(function (v) { v.classList.remove("active"); });
    $("#view-" + name).classList.add("active");
    if (name === "plugin") loadPluginView();
    if (name === "device") loadDeviceMaps();
    if (name === "settings") loadSettingsView();
  }
  $$("#nav .seg-btn").forEach(function (btn) {
    btn.onclick = function () {
      switchView(btn.dataset.view);
      pushModeToDevice(btn.dataset.view);   // only acts in "app ▸ roto"
    };
  });

  $("#chip-connect").onclick = function () {
    if (state.connected) return;
    $("#dot-device").dataset.state = "connecting";
    setFoot("connecting…");
    rpc("list_ports").then(function (p) {
      var hit = p.serial.find(function (s) {
        return /roto|usbmodem/i.test(s.device + " " + s.description);
      }) || p.serial[0];
      return rpc("connect", hit ? { serial_port: hit.device } : {});
    }).then(function (r) {
      setFoot("connected: MIDI" +
        (r.serial ? " + serial (fw " + (r.fw || "?") + ")" : " only"));
      rpc("get_state").then(applyState);
    }).catch(function (e) {
      $("#dot-device").dataset.state = "error";
      setFoot("connect failed: " + e.message);
    });
  };

  /* ================= SETTINGS ================= */
  var FOLLOW_HINTS = {
    device: "the ROTO picks the app view (Live ↔ Plugin)",
    app: "clicking Live/Plugin flips the hardware screen (needs the serial link)",
    off: "views and hardware are independent",
  };
  function renderFollow() {
    $$("#follow-seg .seg-btn").forEach(function (b) {
      b.classList.toggle("active", b.dataset.follow === follow);
    });
    $("#follow-hint").textContent = FOLLOW_HINTS[follow];
  }
  $$("#follow-seg .seg-btn").forEach(function (b) {
    b.onclick = function () {
      follow = b.dataset.follow;
      try { localStorage.setItem("follow", follow); } catch (e) {}
      renderFollow();
    };
  });
  renderFollow();

  // the firmware transport grid: labels are per-DAW firmware, actions ours
  var TRANSPORT_FIXED = [
    [28, "PLAY", "play"],
    [29, "STOP", "stop"],
    [30, "RECORD", "record"],
    [31, "CYCLE", "loop toggle"],
  ];
  var TRANSPORT_ASSIGNABLE = [
    [32, "PUNCH"], [33, "(unlabeled)"], [34, "(unlabeled)"],
    [35, "(unlabeled)"],
  ];
  var ACTION_CHOICES = [
    ["metronome", "metronome toggle"],
    ["rewind", "rewind (scrub)"],
    ["fastforward", "fast-forward (scrub)"],
    ["play", "play"], ["stop", "stop"], ["record", "record"],
    ["loop", "loop toggle"],
    ["custom", "custom action id…"],
    ["none", "— nothing"],
  ];
  var transportCfg = {};

  function renderScriptPaths() {
    var host = $("#script-settings"); if (!host) return;
    rpc("get_script_paths").then(function (st) {
      host.textContent = "";
      ["reaper", "cubase"].forEach(function (daw) {
        var s = st[daw] || {};
        // two lines: name + status + button on top, the (long) path below — so
        // the button never collides with the path in the narrow settings column
        var row = el("div", "scriptrow");
        var top = el("div", "scriptrow-head");
        var name = el("span", "scriptrow-name", daw === "reaper" ? "REAPER" : "Cubase");
        var badge = el("span", "hint", s.found ? "✓ found" : "✕ not found");
        var reinstall = el("button", "chipbtn small", "Reinstall");
        reinstall.title = "copy the current script into place, overwriting it (repair)";
        reinstall.onclick = function () { reinstallDaw(daw); };
        var locate = el("button", "chipbtn small", "Locate…");
        locate.onclick = function () { locateDaw(daw); };
        top.append(name, badge, el("span", "spacer"), reinstall, locate);
        if (s.located) {
          var reset = el("button", "chipbtn small", "auto");
          reset.title = "forget this folder, go back to auto-discovery";
          reset.onclick = function () { resetDaw(daw); };
          top.append(reset);
        }
        var path = el("div", "mono hint scriptrow-path",
                      s.path || "(auto — not found)");
        path.title = s.path || "";
        row.append(top, path);
        host.append(row);
      });
    }).catch(function () {
      host.replaceChildren(el("p", "hint", "cannot read script paths"));
    });
  }

  function locateDaw(daw) {
    var api = window.pywebview && window.pywebview.api;
    var human = daw === "reaper"
      ? "Pick REAPER's resource folder (REAPER › Options › Show REAPER resource path)"
      : "Pick Cubase's 'Driver Scripts' folder (or the Steinberg host folder)";
    if (api && api.pick_daw_folder) {
      api.pick_daw_folder(daw).then(function (r) {
        if (r && r.picked) setFoot((r.notes || []).join("; ") || "folder set");
        renderScriptPaths();
      });
    } else {
      var p = window.prompt(human + ":");
      if (p) rpc("set_script_path", { daw: daw, path: p })
        .then(function () { renderScriptPaths(); });
    }
  }

  function resetDaw(daw) {
    var api = window.pywebview && window.pywebview.api;
    if (api && api.clear_daw_folder) {
      api.clear_daw_folder(daw).then(function () { renderScriptPaths(); });
    } else {
      rpc("set_script_path", { daw: daw }).then(function () { renderScriptPaths(); });
    }
  }

  function reinstallDaw(daw) {
    var human = daw === "reaper" ? "REAPER" : "Cubase";
    setFoot("reinstalling " + human + " script…");
    rpc("reinstall_scripts", { daw: daw }).then(function (r) {
      var notes = (r && r.notes) || [];
      // the copy is the easy half — the DAW still has to reload the script
      var reload = daw === "reaper"
        ? "now run the roto_fx_feed action in REAPER"
        : "now refresh MIDI Remote ▸ Scripts in Cubase";
      setFoot(notes.length
        ? (notes.join("; ") + " — " + reload)
        : (human + ": no target folder — use Locate… to point at it"));
      renderScriptPaths();
    }).catch(function () { setFoot("reinstall failed — see the log"); });
  }

  function loadSettingsView() {
    renderFollow();
    renderScriptPaths();
    rpc("get_settings").then(function (s) {
      transportCfg = s.transport || {};
      $("#transport-daw").textContent =
        "actions run in " + (s.daw || state.daw);
      renderTransportSettings();
      renderMixSettings(s.mix || { touch_select: false });
      renderSystemSettings(s.system || { enabled: false });
    }).catch(function (e) {
      $("#transport-settings").replaceChildren(
        el("p", "hint", "cannot read settings: " + e.message));
    });
  }

  function renderMixSettings(mix) {
    var wrap = $("#mix-settings");
    wrap.replaceChildren();
    var row = el("label", "row gap");
    var box = el("input");
    box.type = "checkbox";
    box.checked = !!mix.touch_select;
    box.onchange = function () {
      rpc("set_settings", { patch: { mix_touch_select: box.checked } });
    };
    row.append(box, el("span", "",
                       "Touching a knob selects its track"));
    wrap.appendChild(row);
  }

  function renderSystemSettings(sys) {
    var wrap = $("#system-settings");
    wrap.replaceChildren();
    var row = el("label", "row gap");
    var box = el("input");
    box.type = "checkbox";
    box.checked = !!sys.enabled;
    box.onchange = function () {
      rpc("set_settings", { patch: { system_control: box.checked } })
        .then(loadSettingsView);
    };
    row.append(box, el("span", "",
                       "Enable system control (opt-in permission checks)"));
    wrap.appendChild(row);
    if (!sys.enabled) return;

    function statusLine(ok, okText, badText) {
      return el("p", "hint", (ok ? "✓ " + okText : "✗ " + badText));
    }
    if (!sys.pyobjc) {
      // cannot happen in a healthy install — the frameworks ship with the app
      wrap.appendChild(statusLine(false, "",
        "this build is missing its macOS frameworks — reinstall the app"));
      return;
    }
    wrap.appendChild(statusLine(sys.accessibility,
      "Accessibility permission granted — cursor knob + media keys ready",
      "Accessibility not granted — cursor knob and media keys are inert"));

    var id = sys.identity || {};
    if (sys.accessibility === false) {
      // steer to the settings pane; the one-shot consent dialog is
      // unreliable from the app's worker thread, so we open the list and
      // tell the user exactly which entry to enable
      var grant = el("button", "chipbtn", "Open Accessibility settings…");
      grant.onclick = function () {
        rpc("request_system_permission").then(function () {
          setFoot("turn on “" + (id.name || "Athens") +
                  "” in the list, then Relaunch");
        }).catch(function (e) { setFoot(e.message); });
      };
      wrap.appendChild(grant);

      if (id.bundle) {
        // macOS caches the permission per process — only a real relaunch
        // sees a fresh grant, so that's the primary action, not Re-check
        var relaunch = el("button", "chipbtn primary", "Relaunch Athens");
        relaunch.style.marginLeft = "8px";
        relaunch.onclick = function () {
          setFoot("relaunching…");
          rpc("relaunch_app").catch(function (e) { setFoot(e.message); });
        };
        wrap.appendChild(relaunch);
        wrap.appendChild(el("p", "hint",
          "Turn on “" + (id.name || "Athens") + "” in the list, then click " +
          "Relaunch. macOS only applies the grant to a freshly launched app " +
          "— an in-place re-check can’t see it."));
      } else {
        var recheck = el("button", "chipbtn", "Re-check");
        recheck.style.marginLeft = "8px";
        recheck.onclick = loadSettingsView;
        wrap.appendChild(recheck);
        wrap.appendChild(el("p", "hint",
          "⚠ You launched from the command line, so the entry is “" +
          (id.name || "python") + "” (macOS may even show the terminal or " +
          "host app, e.g. “Terminal”). Granting that is unreliable for system " +
          "control — run the packaged Athens.app instead " +
          "(build: sh scripts/build-macos.sh)."));
      }
    }
    wrap.appendChild(statusLine(sys.brightness_cli,
      "brightness CLI found — Display strip available",
      "optional: no brightness CLI, the Display strip stays hidden"));
  }

  function saveTransport() {
    rpc("set_settings", { patch: { transport: transportCfg } })
      .then(function () { setFoot("transport assignments saved"); })
      .catch(function (e) { setFoot("save failed: " + e.message); });
  }

  function renderTransportSettings() {
    var wrap = $("#transport-settings");
    wrap.replaceChildren();
    TRANSPORT_FIXED.forEach(function (row) {
      var r = el("div", "trow settings-grid");
      r.append(el("span", "mono pnum", "CC " + row[0]),
               el("span", "pname", row[1]),
               el("span", "hint", row[2]), el("span"));
      wrap.appendChild(r);
    });
    TRANSPORT_ASSIGNABLE.forEach(function (row) {
      var cc = row[0];
      var r = el("div", "trow settings-grid");
      var sel = el("select");
      var cur = transportCfg[String(cc)] || "none";
      var isCustom = /^action:/.test(cur);
      ACTION_CHOICES.forEach(function (c) {
        var o = el("option", "", c[1]);
        o.value = c[0];
        o.selected = isCustom ? c[0] === "custom" : c[0] === cur;
        sel.appendChild(o);
      });
      var idIn = el("input");
      idIn.type = "number"; idIn.className = "numinput";
      idIn.placeholder = "action id";
      idIn.hidden = !isCustom;
      if (isCustom) idIn.value = cur.slice(7);
      function commit() {
        var v = sel.value;
        if (v === "custom") {
          idIn.hidden = false;
          if (idIn.value === "") return;         // wait for an id
          v = "action:" + clampInt(idIn.value, 0, 99999999);
        } else {
          idIn.hidden = true;
        }
        transportCfg[String(cc)] = v;
        saveTransport();
      }
      sel.onchange = commit;
      idIn.onchange = commit;
      r.append(el("span", "mono pnum", "CC " + cc),
               el("span", "pname", row[1]), sel, idIn);
      wrap.appendChild(r);
    });
  }

  // theme: "auto" follows the OS live; the button cycles auto -> light -> dark
  var themeMQ = window.matchMedia("(prefers-color-scheme: dark)");
  function themeMode() {
    try { return localStorage.getItem("theme") || "auto"; } catch (e) { return "auto"; }
  }
  function applyTheme() {
    var mode = themeMode();
    var dark = mode === "dark" || (mode === "auto" && themeMQ.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    var b = $("#btn-theme");
    if (b) {
      b.textContent = mode === "auto" ? "◐" : (mode === "dark" ? "●" : "○");
      b.title = "theme: " + mode + " — click to change (auto follows macOS)";
    }
  }
  $("#btn-theme").onclick = function () {
    var order = ["auto", "light", "dark"];
    var next = order[(order.indexOf(themeMode()) + 1) % order.length];
    try { localStorage.setItem("theme", next); } catch (e) {}
    applyTheme();
  };
  if (themeMQ.addEventListener) themeMQ.addEventListener("change", applyTheme);
  else if (themeMQ.addListener) themeMQ.addListener(applyTheme);   // old webkit
  applyTheme();

  buildSurface();
  // Diagnostics nav tab shows only when launched with the diag switch (ui --view diag)
  if (DIAG_MODE) $("#nav-diag").hidden = false;
  // startup view via ?view= (the ui command's --view switch)
  var startView = QS.get("view");
  if (startView && $("#view-" + startView)) switchView(startView);
  connect();
})();
