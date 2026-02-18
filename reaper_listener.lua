-- reaper_listener.lua — REAPER MCP Listener
-- https://github.com/username/reaper-mcp
--
-- Runs inside REAPER as a deferred script (Actions > Run ReaScript).
-- Polls for command files from the MCP server and executes them via ReaScript API.
-- All mutating commands are wrapped in undo blocks so everything is Ctrl+Z-able.
--
-- IPC directory can be overridden with the REAPER_MCP_IPC_DIR environment variable.
-- Default: ~/.reaper-mcp/ (macOS/Linux) or %USERPROFILE%/.reaper-mcp/ (Windows)

-- ============================================================================
-- Configuration
-- ============================================================================

local function get_ipc_dir()
  local dir = os.getenv("REAPER_MCP_IPC_DIR")
  if dir then
    -- Normalize trailing separator
    if dir:sub(-1) ~= "/" and dir:sub(-1) ~= "\\" then
      dir = dir .. "/"
    end
    return dir
  end
  -- Cross-platform default
  local home = os.getenv("HOME") or os.getenv("USERPROFILE")
  if not home then
    reaper.ShowConsoleMsg("REAPER MCP: ERROR — Cannot determine home directory.\n")
    return nil
  end
  return home .. "/.reaper-mcp/"
end

local IPC_DIR = get_ipc_dir()
if not IPC_DIR then return end

local CMD_FILE = IPC_DIR .. "command.json"
local RSP_FILE = IPC_DIR .. "response.json"
local POLL_INTERVAL = 0.033 -- ~30Hz polling
local MAX_CMD_SIZE = 1024 * 1024 -- 1MB max command size (safety limit)

-- ============================================================================
-- Minimal JSON encoder/decoder
-- REAPER's Lua environment does not include a JSON library.
-- This is a self-contained recursive descent parser and encoder.
-- ============================================================================

local json = {}

function json.decode(str)
  if not str or str == "" then return nil, "empty input" end
  if #str > MAX_CMD_SIZE then return nil, "input exceeds size limit" end

  local pos = 1
  local depth = 0
  local MAX_DEPTH = 20

  local function skip_ws()
    pos = str:find("[^ \t\n\r]", pos) or (#str + 1)
  end

  local function peek() return str:sub(pos, pos) end

  local parse_value -- forward declaration

  local function parse_string()
    pos = pos + 1 -- skip opening quote
    local parts = {}
    while pos <= #str do
      local c = str:sub(pos, pos)
      if c == '"' then
        pos = pos + 1
        return table.concat(parts)
      elseif c == '\\' then
        pos = pos + 1
        local esc = str:sub(pos, pos)
        if esc == '"' then parts[#parts+1] = '"'
        elseif esc == '\\' then parts[#parts+1] = '\\'
        elseif esc == '/' then parts[#parts+1] = '/'
        elseif esc == 'n' then parts[#parts+1] = '\n'
        elseif esc == 'r' then parts[#parts+1] = '\r'
        elseif esc == 't' then parts[#parts+1] = '\t'
        elseif esc == 'b' then parts[#parts+1] = '\b'
        elseif esc == 'f' then parts[#parts+1] = '\f'
        elseif esc == 'u' then
          local hex = str:sub(pos+1, pos+4)
          pos = pos + 4
          local cp = tonumber(hex, 16)
          if cp then parts[#parts+1] = string.char(cp) end
        end
        pos = pos + 1
      else
        parts[#parts+1] = c
        pos = pos + 1
      end
    end
    return table.concat(parts)
  end

  local function parse_number()
    local start = pos
    if str:sub(pos, pos) == '-' then pos = pos + 1 end
    while pos <= #str and str:sub(pos, pos):match("[%d]") do pos = pos + 1 end
    if pos <= #str and str:sub(pos, pos) == '.' then
      pos = pos + 1
      while pos <= #str and str:sub(pos, pos):match("[%d]") do pos = pos + 1 end
    end
    if pos <= #str and str:sub(pos, pos):match("[eE]") then
      pos = pos + 1
      if pos <= #str and str:sub(pos, pos):match("[%+%-]") then pos = pos + 1 end
      while pos <= #str and str:sub(pos, pos):match("[%d]") do pos = pos + 1 end
    end
    return tonumber(str:sub(start, pos - 1))
  end

  local function parse_object()
    depth = depth + 1
    if depth > MAX_DEPTH then return nil end
    pos = pos + 1 -- skip {
    local obj = {}
    skip_ws()
    if peek() == '}' then pos = pos + 1; depth = depth - 1; return obj end
    while true do
      skip_ws()
      if peek() ~= '"' then depth = depth - 1; return obj end
      local key = parse_string()
      skip_ws()
      pos = pos + 1 -- skip :
      skip_ws()
      obj[key] = parse_value()
      skip_ws()
      if peek() == ',' then pos = pos + 1
      elseif peek() == '}' then pos = pos + 1; depth = depth - 1; return obj
      else depth = depth - 1; return obj end
    end
  end

  local function parse_array()
    depth = depth + 1
    if depth > MAX_DEPTH then return nil end
    pos = pos + 1 -- skip [
    local arr = {}
    skip_ws()
    if peek() == ']' then pos = pos + 1; depth = depth - 1; return arr end
    while true do
      skip_ws()
      arr[#arr+1] = parse_value()
      skip_ws()
      if peek() == ',' then pos = pos + 1
      elseif peek() == ']' then pos = pos + 1; depth = depth - 1; return arr
      else depth = depth - 1; return arr end
    end
  end

  parse_value = function()
    skip_ws()
    local c = peek()
    if c == '"' then return parse_string()
    elseif c == '{' then return parse_object()
    elseif c == '[' then return parse_array()
    elseif c == 't' then pos = pos + 4; return true
    elseif c == 'f' then pos = pos + 5; return false
    elseif c == 'n' then pos = pos + 4; return nil
    else return parse_number()
    end
  end

  local result = parse_value()
  return result
end

function json.encode(val)
  if val == nil then return "null" end
  local t = type(val)
  if t == "boolean" then return val and "true" or "false" end
  if t == "number" then
    if val ~= val then return "null" end -- NaN
    if val == math.huge or val == -math.huge then return "null" end
    return string.format("%.14g", val)
  end
  if t == "string" then
    val = val:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n')
             :gsub('\r', '\\r'):gsub('\t', '\\t')
    return '"' .. val .. '"'
  end
  if t == "table" then
    -- Detect array vs object: consecutive integer keys starting at 1
    local is_array = true
    local max_i = 0
    for k, _ in pairs(val) do
      if type(k) ~= "number" or k < 1 or math.floor(k) ~= k then
        is_array = false; break
      end
      if k > max_i then max_i = k end
    end
    if is_array and max_i == #val then
      local parts = {}
      for i = 1, #val do parts[i] = json.encode(val[i]) end
      return "[" .. table.concat(parts, ",") .. "]"
    else
      local parts = {}
      for k, v in pairs(val) do
        parts[#parts+1] = json.encode(tostring(k)) .. ":" .. json.encode(v)
      end
      return "{" .. table.concat(parts, ",") .. "}"
    end
  end
  return "null"
end

-- ============================================================================
-- File I/O helpers
-- ============================================================================

local function read_file(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local content = f:read("*a")
  f:close()
  return content
end

local function write_file(path, content)
  -- Atomic write: write to temp file, then rename.
  -- Prevents the MCP server from reading a partially-written response.
  local tmp = path .. ".tmp"
  local f = io.open(tmp, "w")
  if not f then return false end
  f:write(content)
  f:close()
  os.rename(tmp, path)
  return true
end

local function file_exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

local function delete_file(path)
  os.remove(path)
end

-- ============================================================================
-- Track resolution
-- Case-insensitive, partial match. Exact matches take priority.
-- Returns (track, nil) on success, (nil, error_string) on failure.
-- ============================================================================

local function resolve_track(name)
  if not name or name == "" then return nil, "No track name provided" end
  local name_lower = name:lower()
  local matches = {}
  local num_tracks = reaper.CountTracks(0)

  for i = 0, num_tracks - 1 do
    local track = reaper.GetTrack(0, i)
    local _, track_name = reaper.GetTrackName(track)
    if track_name:lower() == name_lower then
      return track, nil -- exact match, return immediately
    end
    if track_name:lower():find(name_lower, 1, true) then
      matches[#matches+1] = {track = track, name = track_name}
    end
  end

  if #matches == 1 then return matches[1].track, nil end
  if #matches == 0 then
    return nil, "No track found matching '" .. name .. "'"
  end
  local names = {}
  for _, m in ipairs(matches) do names[#names+1] = m.name end
  return nil, "Ambiguous track name '" .. name .. "', matches: " .. table.concat(names, ", ")
end

-- ============================================================================
-- FX resolution
-- Case-insensitive, partial match. Accepts name (string) or index (number).
-- ============================================================================

local function resolve_fx(track, fx_name)
  if type(fx_name) == "number" then
    if fx_name >= 0 and fx_name < reaper.TrackFX_GetCount(track) then
      return fx_name, nil
    end
    return nil, "FX index " .. fx_name .. " out of range"
  end

  local fx_name_lower = fx_name:lower()
  local count = reaper.TrackFX_GetCount(track)
  local matches = {}

  for i = 0, count - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, i)
    if name:lower() == fx_name_lower then return i, nil end
    if name:lower():find(fx_name_lower, 1, true) then
      matches[#matches+1] = {index = i, name = name}
    end
  end

  if #matches == 1 then return matches[1].index, nil end
  if #matches == 0 then return nil, "No FX found matching '" .. fx_name .. "'" end
  local names = {}
  for _, m in ipairs(matches) do names[#names+1] = m.name end
  return nil, "Ambiguous FX name '" .. fx_name .. "', matches: " .. table.concat(names, ", ")
end

-- ============================================================================
-- Unit conversions
-- dB <-> REAPER volume (0-4 scale, 1.0 = 0dB)
-- Pan: user-facing -100..100 <-> REAPER -1..1
-- ============================================================================

local function db_to_vol(db)
  if db <= -150 then return 0 end
  return 10 ^ (db / 20)
end

local function vol_to_db(vol)
  if vol < 0.00001 then return -150 end
  return 20 * math.log(vol, 10)
end

local function pan_to_reaper(pan) return math.max(-1, math.min(1, pan / 100)) end
local function reaper_to_pan(pan) return pan * 100 end

-- ============================================================================
-- Allowed actions (whitelist)
-- Only actions in this set will be executed. Anything else is rejected.
-- ============================================================================

local ALLOWED_ACTIONS = {}

-- ============================================================================
-- Command handlers
-- Each returns (result_table) on success or (nil, error_string) on failure.
-- ============================================================================

local handlers = {}

-- Session state — gives Claude full context about the current project
function handlers.get_session_state(params)
  local state = {}
  state.tempo = reaper.Master_GetTempo()
  local _, ts_num, ts_den = reaper.TimeMap_GetTimeSigAtTime(0, 0)
  state.time_signature = {numerator = ts_num, denominator = ts_den}

  -- Transport
  local play_state = reaper.GetPlayState()
  state.transport = {
    playing = (play_state & 1) ~= 0,
    paused = (play_state & 2) ~= 0,
    recording = (play_state & 4) ~= 0,
    position = reaper.GetPlayPosition(),
    cursor = reaper.GetCursorPosition()
  }

  -- Tracks
  state.tracks = {}
  local num_tracks = reaper.CountTracks(0)
  for i = 0, num_tracks - 1 do
    local track = reaper.GetTrack(0, i)
    local _, name = reaper.GetTrackName(track)
    local vol = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
    local pan = reaper.GetMediaTrackInfo_Value(track, "D_PAN")
    local mute = reaper.GetMediaTrackInfo_Value(track, "B_MUTE")
    local solo = reaper.GetMediaTrackInfo_Value(track, "I_SOLO")
    local armed = reaper.GetMediaTrackInfo_Value(track, "I_RECARM")

    local track_info = {
      index = i,
      name = name,
      volume_db = math.floor(vol_to_db(vol) * 100) / 100,
      pan = math.floor(reaper_to_pan(pan) * 100) / 100,
      muted = mute == 1,
      soloed = solo > 0,
      armed = armed == 1,
      fx = {},
      sends = {},
      items = {}
    }

    -- FX chain (cap params per FX to prevent huge payloads)
    local fx_count = reaper.TrackFX_GetCount(track)
    for j = 0, fx_count - 1 do
      local _, fx_name = reaper.TrackFX_GetFXName(track, j)
      local enabled = reaper.TrackFX_GetEnabled(track, j)
      local fx_info = {index = j, name = fx_name, enabled = enabled, params = {}}
      local param_count = reaper.TrackFX_GetNumParams(track, j)
      for p = 0, math.min(param_count - 1, 49) do
        local _, pname = reaper.TrackFX_GetParamName(track, j, p)
        local val = reaper.TrackFX_GetParam(track, j, p)
        local _, minval, maxval = reaper.TrackFX_GetParam(track, j, p)
        fx_info.params[#fx_info.params+1] = {
          index = p, name = pname, value = val, min = minval, max = maxval
        }
      end
      track_info.fx[#track_info.fx+1] = fx_info
    end

    -- Sends
    local send_count = reaper.GetTrackNumSends(track, 0)
    for j = 0, send_count - 1 do
      local dest = reaper.GetTrackSendInfo_Value(track, 0, j, "P_DESTTRACK")
      local _, dest_name = reaper.GetTrackName(dest)
      local send_vol = reaper.GetTrackSendInfo_Value(track, 0, j, "D_VOL")
      track_info.sends[#track_info.sends+1] = {
        index = j,
        dest_track = dest_name,
        volume_db = math.floor(vol_to_db(send_vol) * 100) / 100
      }
    end

    -- Items
    local item_count = reaper.CountTrackMediaItems(track)
    for j = 0, item_count - 1 do
      local item = reaper.GetTrackMediaItem(track, j)
      local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
      local item_len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
      local item_name = ""
      local take = reaper.GetActiveTake(item)
      if take then
        _, item_name = reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", "", false)
      end
      track_info.items[#track_info.items+1] = {
        index = j, position = item_pos, length = item_len, name = item_name
      }
    end

    state.tracks[#state.tracks+1] = track_info
  end

  -- Markers and regions
  state.markers = {}
  state.regions = {}
  local _, num_markers, num_regions = reaper.CountProjectMarkers(0)
  local total = num_markers + num_regions
  for i = 0, total - 1 do
    local _, is_region, pos, rgnend, name, idx = reaper.EnumProjectMarkers(i)
    if is_region then
      state.regions[#state.regions+1] = {index = idx, start = pos, finish = rgnend, name = name}
    else
      state.markers[#state.markers+1] = {index = idx, position = pos, name = name}
    end
  end

  return state
end

-- Transport
function handlers.play() reaper.Main_OnCommand(1007, 0); return {status = "playing"} end
function handlers.stop() reaper.Main_OnCommand(1016, 0); return {status = "stopped"} end
function handlers.pause() reaper.Main_OnCommand(1008, 0); return {status = "paused"} end
function handlers.record() reaper.Main_OnCommand(1013, 0); return {status = "recording"} end

function handlers.set_tempo(params)
  local bpm = tonumber(params.bpm)
  if not bpm or bpm < 1 or bpm > 960 then return nil, "BPM must be between 1 and 960" end
  reaper.SetCurrentBPM(0, bpm, true)
  return {tempo = bpm}
end

function handlers.get_transport_state()
  local play_state = reaper.GetPlayState()
  return {
    playing = (play_state & 1) ~= 0,
    paused = (play_state & 2) ~= 0,
    recording = (play_state & 4) ~= 0,
    position = reaper.GetPlayPosition(),
    cursor = reaper.GetCursorPosition(),
    tempo = reaper.Master_GetTempo()
  }
end

-- Tracks
function handlers.create_track(params)
  local name = params.name or "New Track"
  local idx = params.index or reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(idx, true)
  local track = reaper.GetTrack(0, idx)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", name, true)
  return {name = name, index = idx}
end

function handlers.delete_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.DeleteTrack(track)
  return {deleted = params.track}
end

function handlers.rename_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", params.new_name, true)
  return {old_name = params.track, new_name = params.new_name}
end

function handlers.set_track_volume(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local db = tonumber(params.db)
  if not db then return nil, "Invalid dB value" end
  reaper.SetMediaTrackInfo_Value(track, "D_VOL", db_to_vol(db))
  return {track = params.track, volume_db = db}
end

function handlers.set_track_pan(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local pan = tonumber(params.pan)
  if not pan then return nil, "Invalid pan value" end
  reaper.SetMediaTrackInfo_Value(track, "D_PAN", pan_to_reaper(pan))
  return {track = params.track, pan = pan}
end

function handlers.mute_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", 1)
  return {track = params.track, muted = true}
end

function handlers.unmute_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", 0)
  return {track = params.track, muted = false}
end

function handlers.solo_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.SetMediaTrackInfo_Value(track, "I_SOLO", 1)
  return {track = params.track, soloed = true}
end

function handlers.unsolo_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.SetMediaTrackInfo_Value(track, "I_SOLO", 0)
  return {track = params.track, soloed = false}
end

function handlers.arm_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.SetMediaTrackInfo_Value(track, "I_RECARM", 1)
  return {track = params.track, armed = true}
end

function handlers.unarm_track(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  reaper.SetMediaTrackInfo_Value(track, "I_RECARM", 0)
  return {track = params.track, armed = false}
end

-- FX / Plugins
function handlers.add_fx(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local plugin = params.plugin_name
  if not plugin or plugin == "" then return nil, "No plugin name provided" end
  local idx = reaper.TrackFX_AddByName(track, plugin, false, -1)
  if idx == -1 then return nil, "Plugin '" .. plugin .. "' not found" end
  return {track = params.track, plugin = plugin, fx_index = idx}
end

function handlers.remove_fx(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local fx_idx, fx_err = resolve_fx(track, params.fx)
  if not fx_idx then return nil, fx_err end
  reaper.TrackFX_Delete(track, fx_idx)
  return {track = params.track, removed = params.fx}
end

function handlers.get_fx_params(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local fx_idx, fx_err = resolve_fx(track, params.fx)
  if not fx_idx then return nil, fx_err end

  local _, fx_name = reaper.TrackFX_GetFXName(track, fx_idx)
  local result = {track = params.track, fx = fx_name, params = {}}
  local count = reaper.TrackFX_GetNumParams(track, fx_idx)
  for p = 0, count - 1 do
    local _, pname = reaper.TrackFX_GetParamName(track, fx_idx, p)
    local val = reaper.TrackFX_GetParam(track, fx_idx, p)
    local _, minval, maxval = reaper.TrackFX_GetParam(track, fx_idx, p)
    result.params[#result.params+1] = {
      index = p, name = pname, value = val, min = minval, max = maxval
    }
  end
  return result
end

function handlers.set_fx_param(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local fx_idx, fx_err = resolve_fx(track, params.fx)
  if not fx_idx then return nil, fx_err end
  if not params.param or params.param == "" then return nil, "No parameter name provided" end
  if not params.value then return nil, "No value provided" end

  -- Find param by name (case-insensitive, partial match)
  local param_name_lower = params.param:lower()
  local count = reaper.TrackFX_GetNumParams(track, fx_idx)
  local match_idx = nil
  local matches = {}
  for p = 0, count - 1 do
    local _, pname = reaper.TrackFX_GetParamName(track, fx_idx, p)
    if pname:lower() == param_name_lower then
      match_idx = p; break
    end
    if pname:lower():find(param_name_lower, 1, true) then
      matches[#matches+1] = {index = p, name = pname}
    end
  end
  if not match_idx then
    if #matches == 1 then
      match_idx = matches[1].index
    elseif #matches == 0 then
      return nil, "No parameter matching '" .. params.param .. "'"
    else
      local names = {}
      for _, m in ipairs(matches) do names[#names+1] = m.name end
      return nil, "Ambiguous param '" .. params.param .. "', matches: " .. table.concat(names, ", ")
    end
  end

  reaper.TrackFX_SetParam(track, fx_idx, match_idx, params.value)
  return {track = params.track, fx = params.fx, param = params.param, value = params.value}
end

function handlers.bypass_fx(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local fx_idx, fx_err = resolve_fx(track, params.fx)
  if not fx_idx then return nil, fx_err end
  reaper.TrackFX_SetEnabled(track, fx_idx, false)
  return {track = params.track, fx = params.fx, bypassed = true}
end

function handlers.enable_fx(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local fx_idx, fx_err = resolve_fx(track, params.fx)
  if not fx_idx then return nil, fx_err end
  reaper.TrackFX_SetEnabled(track, fx_idx, true)
  return {track = params.track, fx = params.fx, enabled = true}
end

-- Routing
function handlers.create_send(params)
  local src, err = resolve_track(params.source)
  if not src then return nil, "Source: " .. err end
  local dest, derr = resolve_track(params.dest)
  if not dest then return nil, "Dest: " .. derr end
  local send_idx = reaper.CreateTrackSend(src, dest)
  if params.volume_db then
    local db = tonumber(params.volume_db)
    if db then
      reaper.SetTrackSendInfo_Value(src, 0, send_idx, "D_VOL", db_to_vol(db))
    end
  end
  return {source = params.source, dest = params.dest, send_index = send_idx}
end

function handlers.remove_send(params)
  local src, err = resolve_track(params.source)
  if not src then return nil, "Source: " .. err end
  local dest, derr = resolve_track(params.dest)
  if not dest then return nil, "Dest: " .. derr end
  local send_count = reaper.GetTrackNumSends(src, 0)
  for i = 0, send_count - 1 do
    local d = reaper.GetTrackSendInfo_Value(src, 0, i, "P_DESTTRACK")
    if d == dest then
      reaper.RemoveTrackSend(src, 0, i)
      return {source = params.source, dest = params.dest, removed = true}
    end
  end
  return nil, "No send found from '" .. params.source .. "' to '" .. params.dest .. "'"
end

function handlers.set_send_volume(params)
  local src, err = resolve_track(params.source)
  if not src then return nil, "Source: " .. err end
  local dest, derr = resolve_track(params.dest)
  if not dest then return nil, "Dest: " .. derr end
  local db = tonumber(params.db)
  if not db then return nil, "Invalid dB value" end
  local send_count = reaper.GetTrackNumSends(src, 0)
  for i = 0, send_count - 1 do
    local d = reaper.GetTrackSendInfo_Value(src, 0, i, "P_DESTTRACK")
    if d == dest then
      reaper.SetTrackSendInfo_Value(src, 0, i, "D_VOL", db_to_vol(db))
      return {source = params.source, dest = params.dest, volume_db = db}
    end
  end
  return nil, "No send found from '" .. params.source .. "' to '" .. params.dest .. "'"
end

-- Items & MIDI
function handlers.create_midi_item(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local start_beat = tonumber(params.start_beat)
  local length_beats = tonumber(params.length_beats)
  if not start_beat or not length_beats then return nil, "Invalid beat values" end
  local start_time = reaper.TimeMap2_beatsToTime(0, start_beat)
  local end_time = reaper.TimeMap2_beatsToTime(0, start_beat + length_beats)
  reaper.CreateNewMIDIItemInProj(track, start_time, end_time)
  return {track = params.track, start_beat = start_beat, length_beats = length_beats}
end

function handlers.insert_midi_notes(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local item_idx = params.item_index or 0
  local item = reaper.GetTrackMediaItem(track, item_idx)
  if not item then return nil, "Item index " .. item_idx .. " not found" end
  local take = reaper.GetActiveTake(item)
  if not take then return nil, "No active take on item" end
  if not reaper.TakeIsMIDI(take) then return nil, "Item is not a MIDI item" end

  local notes = params.notes or {}
  if #notes > 10000 then return nil, "Too many notes (max 10000 per call)" end
  local count = 0
  for _, note in ipairs(notes) do
    local pitch = tonumber(note.pitch)
    local vel = tonumber(note.velocity) or 100
    local sb = tonumber(note.start_beat)
    local lb = tonumber(note.length_beats)
    if not pitch or not sb or not lb then goto continue end
    pitch = math.max(0, math.min(127, math.floor(pitch)))
    vel = math.max(1, math.min(127, math.floor(vel)))
    local start_ppq = reaper.MIDI_GetPPQPosFromProjQN(take, sb)
    local end_ppq = reaper.MIDI_GetPPQPosFromProjQN(take, sb + lb)
    local chan = math.max(0, math.min(15, (tonumber(note.channel) or 1) - 1))
    reaper.MIDI_InsertNote(take, false, false, start_ppq, end_ppq, chan, pitch, vel, true)
    count = count + 1
    ::continue::
  end
  reaper.MIDI_Sort(take)
  return {track = params.track, notes_inserted = count}
end

function handlers.delete_item(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local item = reaper.GetTrackMediaItem(track, params.item_index or 0)
  if not item then return nil, "Item not found" end
  reaper.DeleteTrackMediaItem(track, item)
  return {track = params.track, deleted_index = params.item_index}
end

function handlers.move_item(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local item = reaper.GetTrackMediaItem(track, params.item_index or 0)
  if not item then return nil, "Item not found" end
  local pos = tonumber(params.position)
  if not pos then return nil, "Invalid position" end
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", pos)
  reaper.UpdateArrange()
  return {track = params.track, item_index = params.item_index, new_position = pos}
end

function handlers.split_item_at(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local item = reaper.GetTrackMediaItem(track, params.item_index or 0)
  if not item then return nil, "Item not found" end
  local pos = tonumber(params.position)
  if not pos then return nil, "Invalid position" end
  local new_item = reaper.SplitMediaItem(item, pos)
  if not new_item then return nil, "Split failed at position " .. pos end
  return {track = params.track, split_position = pos}
end

-- Markers & Regions
function handlers.add_marker(params)
  local pos = tonumber(params.position) or reaper.GetCursorPosition()
  local idx = reaper.AddProjectMarker(0, false, pos, 0, params.name or "", -1)
  return {index = idx, position = pos, name = params.name}
end

function handlers.add_region(params)
  local s = tonumber(params.start)
  local e = tonumber(params.finish)
  if not s or not e then return nil, "Invalid start/finish values" end
  local idx = reaper.AddProjectMarker(0, true, s, e, params.name or "", -1)
  return {index = idx, start = s, finish = e, name = params.name}
end

-- Health check
function handlers.ping()
  return {
    status = "ok",
    reaper_version = reaper.GetAppVersion(),
    project = reaper.GetProjectName(0),
    track_count = reaper.CountTracks(0),
  }
end

-- Undo/Redo
function handlers.undo()
  reaper.Main_OnCommand(40029, 0)
  return {action = "undo"}
end

function handlers.redo()
  reaper.Main_OnCommand(40030, 0)
  return {action = "redo"}
end

-- Build the allowed actions set from the handlers table
for action_name, _ in pairs(handlers) do
  ALLOWED_ACTIONS[action_name] = true
end

-- ============================================================================
-- Command dispatcher
-- Validates action against allowlist, wraps mutations in undo blocks.
-- ============================================================================

local READONLY_ACTIONS = {
  get_session_state = true,
  get_fx_params = true,
  get_transport_state = true,
  ping = true,
}

local function execute_command(cmd)
  local action = cmd.action
  local params = cmd.params or {}

  -- Validate action against allowlist
  if not ALLOWED_ACTIONS[action] then
    return {
      id = cmd.id, success = false, result = nil,
      error = "Unknown or disallowed action: " .. tostring(action)
    }
  end

  local handler = handlers[action]

  -- Wrap mutating commands in undo blocks
  local is_readonly = READONLY_ACTIONS[action]
  if not is_readonly then
    reaper.Undo_BeginBlock()
  end

  local ok, result_or_err, err = pcall(handler, params)

  if not is_readonly then
    reaper.Undo_EndBlock("MCP: " .. action, -1)
  end

  if not ok then
    return {id = cmd.id, success = false, result = nil, error = "Lua error: " .. tostring(result_or_err)}
  end

  -- Handler returned (nil, error_string)
  if result_or_err == nil then
    return {id = cmd.id, success = false, result = nil, error = err or "Command returned no result"}
  end

  return {id = cmd.id, success = true, result = result_or_err, error = nil}
end

-- ============================================================================
-- Main polling loop
-- Uses reaper.defer() for cooperative scheduling inside REAPER.
-- ============================================================================

local last_poll = 0

local function poll()
  local now = reaper.time_precise()
  if now - last_poll < POLL_INTERVAL then
    reaper.defer(poll)
    return
  end
  last_poll = now

  if file_exists(CMD_FILE) then
    local content = read_file(CMD_FILE)
    delete_file(CMD_FILE)

    if content and content ~= "" then
      if #content > MAX_CMD_SIZE then
        write_file(RSP_FILE, json.encode({
          id = "unknown", success = false, result = nil,
          error = "Command exceeds maximum size limit"
        }))
      else
        local cmd, parse_err = json.decode(content)
        if cmd and cmd.action then
          local response = execute_command(cmd)
          write_file(RSP_FILE, json.encode(response))
        elseif parse_err then
          write_file(RSP_FILE, json.encode({
            id = "unknown", success = false, result = nil,
            error = "Failed to parse command: " .. tostring(parse_err)
          }))
        end
      end
    end
  end

  reaper.defer(poll)
end

-- ============================================================================
-- Startup
-- ============================================================================

reaper.ShowConsoleMsg("REAPER MCP Listener v1.0.0\n")
reaper.ShowConsoleMsg("  IPC directory: " .. IPC_DIR .. "\n")
reaper.ShowConsoleMsg("  Watching for commands...\n")
poll()
