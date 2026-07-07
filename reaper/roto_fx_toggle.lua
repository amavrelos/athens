--[[
  roto_fx_toggle.lua — one-click start/stop for the roto-reaper feed. Bind to a
  toolbar button/key. Installed next to the feed by athens, so it stays current.

    running?  read the feed's heartbeat ("reaper <os.time()>", rewritten ~1/s)
              and check it's fresh.
    stop:     drop the feed's graceful `quit` sentinel — stops it however it was
              launched (Actions list OR __startup.lua), which "ReaScript task
              control > Terminate" can't do for a dofile launch.
    start:    load the feed script; its own defer loop takes over.

  Toggle state reflects the action just taken. If the feed is stopped externally
  (athens' exit touches the same `quit` file) the button reads stale until the
  next click — which always acts on the REAL state, so one click still corrects it.
]]--

local RES  = reaper.GetResourcePath()
local DIR  = RES .. "/roto-reaper"
local FEED = RES .. "/Scripts/roto_fx_feed.lua"

local function feed_running()
  local f = io.open(DIR .. "/heartbeat", "r")
  if not f then return false end
  local s = f:read("*a"); f:close()
  local ts = tonumber(s and s:match("(%d+)"))            -- "reaper <os.time()>"
  return ts ~= nil and os.difftime(os.time(), ts) <= 3
end

local _, _, section, cmd = reaper.get_action_context()

if feed_running() then
  local q = io.open(DIR .. "/quit", "w")                 -- graceful stop sentinel
  if q then q:write("1"); q:close() end
  reaper.SetToggleCommandState(section, cmd, 0)
else
  reaper.SetToggleCommandState(section, cmd, 1)
  dofile(FEED)                                            -- start: its defer loop runs on
end
reaper.RefreshToolbar2(section, cmd)
