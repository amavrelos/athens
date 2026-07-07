--[[
  roto_fx_feed.lua — REAPER companion for the roto-reaper bridge (plugin mode)

  Why this exists: REAPER's OSC surface can't enumerate a track's FX chain with
  full parameter names (it volunteers one "device FX" at a time). Plugin mode
  needs the whole chain and every param name (for identity hashes), and the learn
  handshake needs "which param did the user just grab". ReaScript reads all of it.

  IPC is small files under <REAPER resource path>/roto-reaper/, written atomically
  (tmp + rename); the Python side (roto_reaper/daw/fx_feed.py) derives the same dir:

    chain.json   we -> py   selected track + full FX/param inventory,
                            rewritten only when the chain structure changes
    live.json    we -> py   last-touched param + watched param values,
                            rewritten while values move (tiny, fast)
    watch.txt    py -> we   "fx param" lines: params to stream at full rate
                            (the ones sitting on hardware controls)

  Files are state, not queues: each carries a seq counter and the newest complete
  state, so a missed write is harmless.

  Install: Athens copies this into <REAPER resource>/Scripts/ on launch. In REAPER:
  Actions > Load ReaScript, pick it, run it (a defer loop keeps it alive); add to
  __startup.lua for auto-start, Terminate from the same list.
--]]

local DIR = reaper.GetResourcePath() .. "/roto-reaper"
local CHAIN = DIR .. "/chain.json"
local LIVE = DIR .. "/live.json"
local WATCH = DIR .. "/watch.txt"
local LEARN = DIR .. "/learn.txt"      -- present = learn armed (move-detect on)
local CMD = DIR .. "/cmd.txt"          -- py -> we: 'focus N' / 'enable N 0|1'
local BEAT = DIR .. "/heartbeat"       -- we -> py: liveness ticked ~1/s; its
                                       -- mtime stops advancing when REAPER quits

local WATCH_POLL_TICKS = 15   -- re-read watch.txt / learn.txt every ~0.5s
local BEAT_TICKS = 30         -- rewrite the heartbeat every ~1s (proves alive
                              -- even when the session is otherwise idle)
local LEARN_MOVE = 0.03       -- param must move >3% since arm to count as "grabbed"
-- Inventory cap per FX = the protocol ceiling: a param index is a 14-bit SysEx
-- number (CONTROL_MAPPED / LEARN_PARAM), so 16384 is the highest index the device
-- can reference. Params beyond it are unreachable by the protocol, not by choice.
local MAX_PARAMS = 16384
local EPS = 1e-5

reaper.RecursiveCreateDirectory(DIR, 0)

-- seed sequence counters from wall-clock so a hot-restart (quit + reload)
-- never re-emits a seq the still-running Python side already consumed
local _seq_base = os.time() % 1000000
local chain_seq, live_seq, touched_seq = _seq_base, _seq_base, _seq_base
local last_sig = nil
local tick = 0
local watch = {}              -- { {fx=, param=}, ... } from watch.txt
local watch_raw = nil
local watched_vals = {}       -- "fx:param" -> last value written
local last_touch = { fx = -1, param = -1, v = -1, d = "", n = "" }
local last_track_count = -1   -- project track count, mirrored into live.json
local learn_armed = false     -- learn.txt present?
local learn_base = nil        -- {param -> value} snapshot for learn_fx
local learn_fx = -1
local learn_reported = -1     -- last param offered by move-detection

-- JSON string with control/quote/backslash escaping
local function jstr(s)
  s = tostring(s or "")
  s = s:gsub('[%c\\"]', function(ch)
    if ch == "\\" then return "\\\\" end
    if ch == '"' then return '\\"' end
    return string.format("\\u%04x", ch:byte())
  end)
  return '"' .. s .. '"'
end

local function write_atomic(path, text)
  local tmp = path .. ".tmp"
  local f = io.open(tmp, "w")
  if not f then return end
  f:write(text)
  f:close()
  if not os.rename(tmp, path) then   -- Windows can't rename over; retry once
    os.remove(path)
    os.rename(tmp, path)
  end
end

-- Plugin mode follows the FOCUSED FX window (not track selection), so clicking
-- another track's FX button switches the device. Sticky: when focus leaves all FX
-- windows we keep the last one (tweaking the arrange view won't blank it); falls
-- back to the selected track until any FX is focused this session.
local last_focused = { track = nil, fx = 0 }

local function focused_target()
  local retval, tracknum, _, fxnum = reaper.GetFocusedFX()
  -- retval 1 = track FX (2 = take/item FX, ignored); high bit = input/rec FX
  if retval == 1 and fxnum < 0x1000000 then
    local track = (tracknum == 0) and reaper.GetMasterTrack(0)
                  or reaper.GetTrack(0, tracknum - 1)
    if track then
      last_focused.track = track
      last_focused.fx = fxnum
    end
  end
  local track = last_focused.track or reaper.GetSelectedTrack2(0, 0, true)
  return track, last_focused.fx
end

-- cheap per-tick fingerprint: guid + focused-fx + fx names/enabled/param counts.
-- NUL delimiter (impossible in an FX name) so no name can forge a collision.
local function chain_signature(track, focused_fx)
  if not track then return "none" end
  local n = reaper.TrackFX_GetCount(track)
  local parts = { reaper.GetTrackGUID(track), "f" .. focused_fx, n }
  for fx = 0, n - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, fx, "")
    parts[#parts + 1] = name
    parts[#parts + 1] = reaper.TrackFX_GetEnabled(track, fx) and "1" or "0"
    parts[#parts + 1] = reaper.TrackFX_GetNumParams(track, fx)
  end
  return table.concat(parts, "\0")
end

local function dump_chain(track, focused_fx)
  chain_seq = chain_seq + 1
  if not track then
    write_atomic(CHAIN, string.format(
        '{"seq":%d,"track":{"index":-1},"focused_fx":-1,"fx":[]}', chain_seq))
    return
  end
  local tidx = math.floor(
      reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER")) - 1
  local _, tname = reaper.GetTrackName(track)
  local nfx = reaper.TrackFX_GetCount(track)
  if focused_fx >= nfx then focused_fx = nfx > 0 and 0 or -1 end
  local out = { string.format(
      '{"seq":%d,"track":{"index":%d,"name":%s,"guid":%s},"focused_fx":%d,"fx":[',
      chain_seq, tidx, jstr(tname), jstr(reaper.GetTrackGUID(track)), focused_fx) }
  local n = reaper.TrackFX_GetCount(track)
  for fx = 0, n - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, fx, "")
    local np = math.min(reaper.TrackFX_GetNumParams(track, fx), MAX_PARAMS)
    local pp = {}
    for p = 0, np - 1 do
      local _, pname = reaper.TrackFX_GetParamName(track, fx, p, "")
      local v = reaper.TrackFX_GetParamNormalized(track, fx, p)
      local _, d = reaper.TrackFX_GetFormattedParamValue(track, fx, p, "")
      -- REAPER's quantisation info, so the learn sweep can classify stepped
      -- params without display-string timing. q = step count (2 = toggle),
      -- 0 = continuous/unknown.
      local q = 0
      local ok, step, _, _, istoggle = reaper.TrackFX_GetParameterStepSizes(track, fx, p)
      if ok then
        if istoggle then
          q = 2
        elseif step and step > 0 then
          local n = math.floor(1.0 / step + 0.5) + 1
          if n >= 2 and n <= 127 then q = n end
        end
      end
      pp[#pp + 1] = string.format('{"name":%s,"v":%.5f,"d":%s,"q":%d}',
                                  jstr(pname), v, jstr(d), q)
    end
    out[#out + 1] = string.format('%s{"name":%s,"enabled":%s,"params":[%s]}',
        fx > 0 and "," or "", jstr(name),
        reaper.TrackFX_GetEnabled(track, fx) and "true" or "false",
        table.concat(pp, ","))
  end
  out[#out + 1] = "]}"
  write_atomic(CHAIN, table.concat(out))
end

local function read_watch()
  local f = io.open(WATCH, "r")
  if not f then return end
  local content = f:read("*a")
  f:close()
  if content == watch_raw then return end
  watch_raw = content
  watch = {}
  for fx, p in content:gmatch("(%d+)%s+(%d+)") do
    watch[#watch + 1] = { fx = tonumber(fx), param = tonumber(p) }
  end
  watched_vals = {}          -- force a fresh values write
end

-- last-touched FX param on the given (selected) track, or nil
local function last_touched_on(track)
  if reaper.GetTouchedOrFocusedFX then          -- REAPER 7+
    local ok, tr, item, _, fx, parm = reaper.GetTouchedOrFocusedFX(0)
    -- fx < 0x1000000 rejects input/monitoring-chain FX (they set the high bit);
    -- their fxidx would otherwise be handed to TrackFX_* as a giant index
    if ok and item == -1 and fx >= 0 and fx < 0x1000000 and parm >= 0 then
      -- trackidx is documented 0-based (-1 = master); compare defensively
      local cand = reaper.GetTrack(0, tr)
      if cand and reaper.GetTrackGUID(cand) == reaper.GetTrackGUID(track) then
        return fx, parm
      end
    end
  elseif reaper.GetLastTouchedFX then
    local ok, tracknum, fxnum, paramnum = reaper.GetLastTouchedFX()
    -- tracknumber: 0 = master, else 1-based; high bytes flag take/record FX
    if ok and fxnum < 0x1000000 and tracknum >= 1 then
      local cand = reaper.GetTrack(0, tracknum - 1)
      if cand and reaper.GetTrackGUID(cand) == reaper.GetTrackGUID(track) then
        return fxnum, paramnum
      end
    end
  end
end

local function write_live(track)
  live_seq = live_seq + 1
  -- project track count rides along: REAPER's OSC pads its bank with
  -- feedback for non-existent tracks, so OSC alone can't tell project size
  local parts = { string.format('{"seq":%d,"tracks":%d',
                                live_seq, reaper.CountTracks(0)) }
  if last_touch.fx >= 0 then
    -- the name rides along so params beyond the chain.json inventory cap
    -- (giant plugins) can still be learned with a correct identity hash
    parts[#parts + 1] = string.format(
        ',"touched":{"seq":%d,"fx":%d,"param":%d,"v":%.5f,"d":%s,"n":%s}',
        touched_seq, last_touch.fx, last_touch.param, last_touch.v,
        jstr(last_touch.d), jstr(last_touch.n))
  end
  local vv = {}
  if track then
    local n = reaper.TrackFX_GetCount(track)
    for _, w in ipairs(watch) do
      if w.fx < n then
        local v = reaper.TrackFX_GetParamNormalized(track, w.fx, w.param)
        local _, d = reaper.TrackFX_GetFormattedParamValue(track, w.fx, w.param, "")
        vv[#vv + 1] = string.format('[%d,%d,%.5f,%s]', w.fx, w.param, v, jstr(d))
      end
    end
  end
  parts[#parts + 1] = ',"values":[' .. table.concat(vv, ",") .. "]}"
  write_atomic(LIVE, table.concat(parts))
end

-- commands from the bridge (device-side overlay actions): applied to the
-- current plugin-context track, then the file is consumed
local function apply_commands(track)
  local f = io.open(CMD, "r")
  if not f then return end
  local content = f:read("*a")
  f:close()
  os.remove(CMD)
  if not track then return end
  for verb, a, b in content:gmatch("(%a+)%s+(%d+)%s*(%d*)") do
    local fx = tonumber(a)
    if verb == "focus" and fx < reaper.TrackFX_GetCount(track) then
      reaper.TrackFX_Show(track, fx, 3)          -- show floating -> focuses
    elseif verb == "enable" and fx < reaper.TrackFX_GetCount(track) then
      reaper.TrackFX_SetEnabled(track, fx, b == "1")
    end
  end
end

-- returns false to stop (quit file), true to keep looping
local function loop_body()
  tick = tick + 1
  if tick % BEAT_TICKS == 0 then
    -- liveness + identity: the mtime advancing proves REAPER is alive; the
    -- "reaper" tag lets Athens' auto-detect know WHICH DAW is feeding it
    -- (the Cubase script announces "cubase" the same way over its MIDI pair)
    local hb = io.open(BEAT, "w")
    if hb then hb:write("reaper " .. tostring(os.time())); hb:close() end
  end
  if tick % WATCH_POLL_TICKS == 0 then
    -- graceful shutdown: `touch <dir>/quit` stops this instance (lets an
    -- upgraded script take over without REAPER's task-control dialog)
    local qf = io.open(DIR .. "/quit", "r")
    if qf then
      qf:close()
      os.remove(DIR .. "/quit")
      return false
    end
    read_watch()
    local lf = io.open(LEARN, "r")
    local now_armed = lf ~= nil
    if lf then lf:close() end
    if now_armed ~= learn_armed then
      learn_armed = now_armed
      learn_base = nil          -- (re)snapshot on the next loop
    end
  end

  local track, focused_fx = focused_target()
  apply_commands(track)
  local sig = chain_signature(track, focused_fx)
  if sig ~= last_sig then
    last_sig = sig
    dump_chain(track, focused_fx)
  end

  local dirty = false
  local ntracks = reaper.CountTracks(0)
  if ntracks ~= last_track_count then
    last_track_count = ntracks
    dirty = true
  end
  if track then
    -- last-touched runs only when learn is NOT armed; while armed, move-detection
    -- below is the sole "grabbed param" source. (Must be a plain if, not `and/or`
    -- — that collapses last_touched_on's two returns.)
    local fx, parm
    if not learn_armed then fx, parm = last_touched_on(track) end
    if fx and parm and (fx ~= last_touch.fx or parm ~= last_touch.param) then
      -- fire ONLY on a DIFFERENT param (identity change), not on value change: a
      -- modulated param (LFO/envelope) never settles, and re-firing on jitter
      -- would flood DAW-initiated learn with the same param.
      local v = reaper.TrackFX_GetParamNormalized(track, fx, parm)
      local _, d = reaper.TrackFX_GetFormattedParamValue(track, fx, parm, "")
      local _, pname = reaper.TrackFX_GetParamName(track, fx, parm, "")
      last_touch = { fx = fx, param = parm, v = v, d = d, n = pname }
      touched_seq = touched_seq + 1
      dirty = true
    end

    -- MOVE-DETECTION for learn: REAPER's last-touched misses moves in a plugin's
    -- OWN UI (and picks the wrong slot on "reserved"-heavy plugins). While armed,
    -- snapshot the focused FX and offer whichever param moves most past a
    -- threshold — the real "grab" signal a plugin-native move gives us.
    if learn_armed then
      local nfx = reaper.TrackFX_GetCount(track)
      if focused_fx >= 0 and focused_fx < nfx then
        local np = math.min(reaper.TrackFX_GetNumParams(track, focused_fx), MAX_PARAMS)
        if learn_base == nil or learn_fx ~= focused_fx then
          learn_fx, learn_reported, learn_base = focused_fx, -1, {}
          for p = 0, np - 1 do
            learn_base[p] = reaper.TrackFX_GetParamNormalized(track, focused_fx, p)
          end
        elseif tick % 2 == 0 then     -- scan ~15Hz, cheap even for big plugins
          local best_p, best_d = -1, LEARN_MOVE
          for p = 0, np - 1 do
            local v = reaper.TrackFX_GetParamNormalized(track, focused_fx, p)
            local delta = math.abs(v - (learn_base[p] or v))
            if delta > best_d then best_d, best_p = delta, p end
          end
          if best_p >= 0 and best_p ~= learn_reported then
            learn_reported = best_p
            local v = reaper.TrackFX_GetParamNormalized(track, focused_fx, best_p)
            local _, d = reaper.TrackFX_GetFormattedParamValue(track, focused_fx, best_p, "")
            local _, pname = reaper.TrackFX_GetParamName(track, focused_fx, best_p, "")
            last_touch = { fx = focused_fx, param = best_p, v = v, d = d, n = pname }
            touched_seq = touched_seq + 1
            dirty = true
          end
        end
      end
    elseif learn_base ~= nil then
      learn_base, learn_fx, learn_reported = nil, -1, -1
    end

    local n = reaper.TrackFX_GetCount(track)
    for _, w in ipairs(watch) do
      if w.fx < n then
        local key = w.fx .. ":" .. w.param
        local v = reaper.TrackFX_GetParamNormalized(track, w.fx, w.param)
        if math.abs(v - (watched_vals[key] or -1)) > EPS then
          watched_vals[key] = v
          dirty = true
        end
      end
    end
  end
  if dirty then write_live(track) end
  return true
end

-- pcall wrapper: a transient nil from a REAPER API (e.g. an FX removed
-- mid-scan) must NOT kill the defer loop and silently take the whole feed
-- down — log it and keep going. Only a clean quit stops the loop.
local function loop()
  local ok, cont = pcall(loop_body)
  if not ok then
    reaper.ShowConsoleMsg("roto_fx_feed error (continuing): " .. tostring(cont) .. "\n")
    reaper.defer(loop)
  elseif cont then
    reaper.defer(loop)
  end
end

-- A pre-existing quit sentinel at startup is unambiguously stale (crash,
-- Terminate, or the toggle racing a feed that was already dead) — consume it, or
-- the first quit-check ~0.5s in would kill this brand-new instance.
os.remove(DIR .. "/quit")
reaper.defer(loop)
