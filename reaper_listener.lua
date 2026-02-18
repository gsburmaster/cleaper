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

local MAX_DB = 12 -- +12dB ceiling — prevents extreme gain that could damage speakers/hearing

local function db_to_vol(db)
  if db <= -150 then return 0 end
  if db > MAX_DB then db = MAX_DB end
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
  if not params.new_name or params.new_name == "" then return nil, "New name cannot be empty" end
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
function handlers.list_installed_fx(params)
  local filter = params.filter and params.filter:lower() or nil
  local category = params.category and params.category:lower() or nil
  local results = {}
  local idx = 0
  while true do
    local ok, name = reaper.EnumInstalledFX(idx)
    if not ok then break end
    local include = true
    if filter and not name:lower():find(filter, 1, true) then
      include = false
    end
    if category then
      local name_lower = name:lower()
      if category == "eq" then
        include = include and (name_lower:find("eq") or name_lower:find("equaliz"))
      elseif category == "compressor" then
        include = include and (name_lower:find("comp") or name_lower:find("compress"))
      elseif category == "reverb" then
        include = include and (name_lower:find("reverb") or name_lower:find("verb") or name_lower:find("room") or name_lower:find("hall"))
      elseif category == "delay" then
        include = include and (name_lower:find("delay") or name_lower:find("echo"))
      elseif category == "distortion" then
        include = include and (name_lower:find("dist") or name_lower:find("satur") or name_lower:find("overdrive") or name_lower:find("drive"))
      elseif category == "limiter" then
        include = include and (name_lower:find("limit"))
      elseif category == "gate" then
        include = include and (name_lower:find("gate") or name_lower:find("expander"))
      elseif category == "chorus" then
        include = include and (name_lower:find("chorus") or name_lower:find("flang") or name_lower:find("phas"))
      end
    end
    if include then
      results[#results+1] = name
    end
    idx = idx + 1
    if #results >= 200 then break end -- cap results
  end
  return {plugins = results, count = #results, total_scanned = idx}
end

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

  -- Clamp value to the plugin's declared min/max range
  local val = params.value
  local _, minval, maxval = reaper.TrackFX_GetParam(track, fx_idx, match_idx)
  if minval and maxval and maxval > minval then
    val = math.max(minval, math.min(maxval, val))
  end
  reaper.TrackFX_SetParam(track, fx_idx, match_idx, val)
  return {track = params.track, fx = params.fx, param = params.param, value = val}
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
  if src == dest then return nil, "Cannot create a send from a track to itself" end
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
    if lb <= 0 then goto continue end -- skip zero/negative length notes
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
  if pos < 0 then return nil, "Position cannot be negative" end
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
  if s >= e then return nil, "Region start must be before end" end
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

-- ============================================================================
-- Metering / Analysis
-- ============================================================================

function handlers.get_track_meter(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  -- Peak values per channel (0-indexed)
  local peak_l = reaper.Track_GetPeakInfo(track, 0)
  local peak_r = reaper.Track_GetPeakInfo(track, 1)
  local function to_db(v) if v < 0.00001 then return -150 end; return 20 * math.log(v, 10) end
  return {
    track = params.track,
    peak_l_db = math.floor(to_db(peak_l) * 10) / 10,
    peak_r_db = math.floor(to_db(peak_r) * 10) / 10,
    peak_l_raw = peak_l,
    peak_r_raw = peak_r,
    clipping = peak_l >= 1.0 or peak_r >= 1.0,
  }
end

function handlers.get_master_meter(params)
  local master = reaper.GetMasterTrack(0)
  local peak_l = reaper.Track_GetPeakInfo(master, 0)
  local peak_r = reaper.Track_GetPeakInfo(master, 1)
  local function to_db(v) if v < 0.00001 then return -150 end; return 20 * math.log(v, 10) end
  return {
    peak_l_db = math.floor(to_db(peak_l) * 10) / 10,
    peak_r_db = math.floor(to_db(peak_r) * 10) / 10,
    clipping = peak_l >= 1.0 or peak_r >= 1.0,
  }
end

-- ============================================================================
-- Render / Bounce
-- ============================================================================

function handlers.render_project(params)
  -- Set output directory if provided
  if params.directory then
    reaper.GetSetProjectInfo_String(0, "RENDER_FILE", params.directory, true)
  end
  -- Set filename pattern if provided
  if params.filename then
    reaper.GetSetProjectInfo_String(0, "RENDER_PATTERN", params.filename, true)
  end
  -- Set bounds: 0=entire project, 1=time selection, 2=entire+markers
  if params.bounds then
    local bounds_map = {project = 0, time_selection = 1, custom = 2}
    local b = bounds_map[params.bounds] or 0
    reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", b, true)
    if params.bounds == "custom" and params.start_time and params.end_time then
      reaper.GetSetProjectInfo(0, "RENDER_STARTPOS", params.start_time, true)
      reaper.GetSetProjectInfo(0, "RENDER_ENDPOS", params.end_time, true)
    end
  end
  -- Set sample rate if provided (validate common rates)
  if params.sample_rate then
    local sr = tonumber(params.sample_rate)
    local valid_rates = {8000,11025,16000,22050,32000,44100,48000,88200,96000,176400,192000}
    local is_valid = false
    for _, r in ipairs(valid_rates) do if sr == r then is_valid = true; break end end
    if not is_valid then return nil, "Invalid sample rate. Use: 44100, 48000, 88200, 96000, or 192000" end
    reaper.GetSetProjectInfo(0, "RENDER_SRATE", sr, true)
  end
  -- Set channels if provided (1=mono, 2=stereo)
  if params.channels then
    reaper.GetSetProjectInfo(0, "RENDER_CHANNELS", params.channels, true)
  end
  -- Trigger render with most recent format settings, auto-close dialog
  reaper.Main_OnCommand(41824, 0)
  -- Read back what was rendered
  local _, render_file = reaper.GetSetProjectInfo_String(0, "RENDER_FILE", "", false)
  local _, render_pattern = reaper.GetSetProjectInfo_String(0, "RENDER_PATTERN", "", false)
  return {
    rendered = true,
    directory = render_file,
    filename_pattern = render_pattern,
  }
end

-- ============================================================================
-- Automation / Envelopes
-- ============================================================================

-- Helper: resolve an envelope on a track by name
-- create: if true, create FX parameter envelopes that don't exist yet
local function resolve_envelope(track, env_name, create)
  if not env_name or env_name == "" then return nil, "No envelope name provided" end
  local env_lower = env_name:lower()

  -- Built-in envelopes
  local builtin = {volume = "Volume", pan = "Pan", mute = "Mute", width = "Width"}
  if builtin[env_lower] then
    local env = reaper.GetTrackEnvelopeByName(track, builtin[env_lower])
    if env then return env, nil, builtin[env_lower] end
    return nil, "Envelope '" .. builtin[env_lower] .. "' not found (may need to show it first)"
  end

  -- FX parameter envelope: "FXName:ParamName"
  local fx_part, param_part = env_name:match("^(.+):(.+)$")
  if fx_part and param_part then
    local fx_idx, fx_err = resolve_fx(track, fx_part)
    if not fx_idx then return nil, fx_err end
    -- Find param
    local param_lower = param_part:lower()
    local count = reaper.TrackFX_GetNumParams(track, fx_idx)
    for p = 0, count - 1 do
      local _, pname = reaper.TrackFX_GetParamName(track, fx_idx, p)
      if pname:lower() == param_lower or pname:lower():find(param_lower, 1, true) then
        local env = reaper.GetFXEnvelope(track, fx_idx, p, create or false)
        if env then return env, nil, fx_part .. ":" .. pname end
        return nil, "Could not create envelope for " .. pname
      end
    end
    return nil, "No parameter matching '" .. param_part .. "' on FX '" .. fx_part .. "'"
  end

  -- Try as-is
  local env = reaper.GetTrackEnvelopeByName(track, env_name)
  if env then return env, nil, env_name end
  return nil, "Envelope '" .. env_name .. "' not found"
end

function handlers.add_envelope_points(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local env, env_err, env_label = resolve_envelope(track, params.envelope, true)
  if not env then return nil, env_err end

  local points = params.points or {}
  if #points > 10000 then return nil, "Too many points (max 10000)" end
  local count = 0
  for _, pt in ipairs(points) do
    local t = tonumber(pt.time)
    local v = tonumber(pt.value)
    if not t or not v then goto continue end
    -- Convert dB to volume scale for volume envelope
    if env_label == "Volume" then
      v = db_to_vol(v)
    elseif env_label == "Pan" then
      v = pan_to_reaper(v)
    end
    local shape = tonumber(pt.shape) or 0 -- 0=linear
    reaper.InsertEnvelopePoint(env, t, v, shape, 0, false, true)
    count = count + 1
    ::continue::
  end
  reaper.Envelope_SortPoints(env)
  return {track = params.track, envelope = env_label, points_added = count}
end

function handlers.get_envelope_points(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local env, env_err, env_label = resolve_envelope(track, params.envelope)
  if not env then return nil, env_err end

  local start_time = tonumber(params.start_time) or 0
  local end_time = tonumber(params.end_time) or math.huge

  local point_count = reaper.CountEnvelopePoints(env)
  local points = {}
  for i = 0, point_count - 1 do
    local _, time, value, shape, tension, selected = reaper.GetEnvelopePoint(env, i)
    if time >= start_time and time <= end_time then
      local display_value = value
      if env_label == "Volume" then display_value = vol_to_db(value) end
      if env_label == "Pan" then display_value = reaper_to_pan(value) end
      points[#points+1] = {
        index = i, time = time, value = display_value,
        raw_value = value, shape = shape
      }
    end
    if #points >= 500 then break end -- cap output
  end
  return {track = params.track, envelope = env_label, points = points, total = point_count}
end

function handlers.clear_envelope(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local env, env_err, env_label = resolve_envelope(track, params.envelope)
  if not env then return nil, env_err end

  local start_time = tonumber(params.start_time)
  local end_time = tonumber(params.end_time)

  if start_time and end_time then
    reaper.DeleteEnvelopePointRange(env, start_time, end_time)
  else
    -- Clear all points
    local count = reaper.CountEnvelopePoints(env)
    for i = count - 1, 0, -1 do
      reaper.DeleteEnvelopePoint(env, i)
    end
  end
  return {track = params.track, envelope = env_label, cleared = true}
end

-- ============================================================================
-- Track Folders / Grouping
-- ============================================================================

function handlers.create_folder(params)
  local name = params.name or "Folder"
  local children = params.children or {}
  if #children == 0 then return nil, "No child tracks specified" end

  -- Resolve all child tracks first, collect their indices
  local child_entries = {}
  for _, child_name in ipairs(children) do
    local ct, cerr = resolve_track(child_name)
    if not ct then return nil, "Child track: " .. cerr end
    local idx = math.floor(reaper.GetMediaTrackInfo_Value(ct, "IP_TRACKNUMBER") - 1)
    child_entries[#child_entries+1] = {track = ct, name = child_name, idx = idx}
  end

  -- Sort children by current index so we can check contiguity
  table.sort(child_entries, function(a, b) return a.idx < b.idx end)

  -- Check that children are contiguous (required for safe folder creation)
  for i = 2, #child_entries do
    if child_entries[i].idx ~= child_entries[i-1].idx + 1 then
      return nil, "Child tracks must be adjacent in the track list for safe folder creation. "
        .. "'" .. child_entries[i-1].name .. "' (index " .. child_entries[i-1].idx .. ") and '"
        .. child_entries[i].name .. "' (index " .. child_entries[i].idx .. ") are not adjacent. "
        .. "Reorder them in REAPER first, or move them next to each other."
    end
  end

  -- Check that none of the children are already folder parents (would corrupt hierarchy)
  for _, c in ipairs(child_entries) do
    local depth = reaper.GetMediaTrackInfo_Value(c.track, "I_FOLDERDEPTH")
    if depth == 1 then
      return nil, "Track '" .. c.name .. "' is already a folder parent. "
        .. "Cannot nest it inside another folder this way — restructure manually in REAPER."
    end
  end

  -- Insert the folder track right before the first child
  local insert_idx = child_entries[1].idx
  reaper.InsertTrackAtIndex(insert_idx, true)
  local folder_track = reaper.GetTrack(0, insert_idx)
  reaper.GetSetMediaTrackInfo_String(folder_track, "P_NAME", name, true)
  reaper.SetMediaTrackInfo_Value(folder_track, "I_FOLDERDEPTH", 1)

  -- Children shifted by 1 due to the insert. Set their depths.
  -- All children are now at indices (insert_idx+1) through (insert_idx+#children).
  for i = 1, #child_entries do
    local ct = reaper.GetTrack(0, insert_idx + i)
    if i < #child_entries then
      reaper.SetMediaTrackInfo_Value(ct, "I_FOLDERDEPTH", 0)
    else
      -- Last child closes the folder
      reaper.SetMediaTrackInfo_Value(ct, "I_FOLDERDEPTH", -1)
    end
  end

  reaper.TrackList_AdjustWindows(false)
  return {folder = name, children_count = #children, index = insert_idx}
end

-- ============================================================================
-- Item Gain & Fades
-- ============================================================================

function handlers.set_item_gain(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local item = reaper.GetTrackMediaItem(track, params.item_index or 0)
  if not item then return nil, "Item not found" end
  local db = tonumber(params.gain_db)
  if not db then return nil, "Invalid gain_db value" end
  reaper.SetMediaItemInfo_Value(item, "D_VOL", db_to_vol(db))
  return {track = params.track, item_index = params.item_index, gain_db = db}
end

function handlers.set_item_fade_in(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local item = reaper.GetTrackMediaItem(track, params.item_index or 0)
  if not item then return nil, "Item not found" end
  local length = tonumber(params.length)
  if not length or length < 0 then return nil, "Invalid fade length" end
  reaper.SetMediaItemInfo_Value(item, "D_FADEINLEN", length)
  if params.shape then
    local shape = math.max(0, math.min(6, math.floor(tonumber(params.shape) or 0)))
    reaper.SetMediaItemInfo_Value(item, "C_FADEINSHAPE", shape)
  end
  reaper.UpdateArrange()
  return {track = params.track, item_index = params.item_index, fade_in = length}
end

function handlers.set_item_fade_out(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local item = reaper.GetTrackMediaItem(track, params.item_index or 0)
  if not item then return nil, "Item not found" end
  local length = tonumber(params.length)
  if not length or length < 0 then return nil, "Invalid fade length" end
  reaper.SetMediaItemInfo_Value(item, "D_FADEOUTLEN", length)
  if params.shape then
    local shape = math.max(0, math.min(6, math.floor(tonumber(params.shape) or 0)))
    reaper.SetMediaItemInfo_Value(item, "C_FADEOUTSHAPE", shape)
  end
  reaper.UpdateArrange()
  return {track = params.track, item_index = params.item_index, fade_out = length}
end

-- ============================================================================
-- Cursor & Time Selection
-- ============================================================================

function handlers.set_cursor_position(params)
  local time = tonumber(params.time)
  if not time then return nil, "Invalid time value" end
  if time < 0 then time = 0 end
  reaper.SetEditCurPos(time, true, false)
  return {cursor_position = time}
end

function handlers.set_time_selection(params)
  local s = tonumber(params.start_time)
  local e = tonumber(params.end_time)
  if not s or not e then return nil, "Invalid start/end time" end
  if s < 0 then s = 0 end
  if e <= s then return nil, "End time must be after start time" end
  reaper.GetSet_LoopTimeRange(true, false, s, e, false)
  return {start_time = s, end_time = e}
end

function handlers.set_loop_points(params)
  local s = tonumber(params.start_time)
  local e = tonumber(params.end_time)
  if not s or not e then return nil, "Invalid start/end time" end
  reaper.GetSet_LoopTimeRange(true, true, s, e, false)
  -- Enable/disable repeat
  if params.enable ~= nil then
    local repeat_state = reaper.GetSetRepeat(-1)
    if params.enable and repeat_state == 0 then
      reaper.GetSetRepeat(1)
    elseif not params.enable and repeat_state == 1 then
      reaper.GetSetRepeat(0)
    end
  end
  return {start_time = s, end_time = e, loop_enabled = reaper.GetSetRepeat(-1) == 1}
end

function handlers.go_to_marker(params)
  -- Accept name (string) or index (number)
  local target = params.marker
  if not target then return nil, "No marker specified" end

  local _, num_markers, num_regions = reaper.CountProjectMarkers(0)
  local total = num_markers + num_regions

  -- Try as number first
  local target_num = tonumber(target)
  if target_num then
    reaper.GoToMarker(0, math.floor(target_num), false)
    return {marker = target_num}
  end

  -- Search by name
  local target_lower = target:lower()
  for i = 0, total - 1 do
    local _, is_region, pos, _, name, idx = reaper.EnumProjectMarkers(i)
    if not is_region and name:lower():find(target_lower, 1, true) then
      reaper.SetEditCurPos(pos, true, false)
      return {marker = name, position = pos}
    end
  end
  -- Also check regions
  for i = 0, total - 1 do
    local _, is_region, pos, _, name, idx = reaper.EnumProjectMarkers(i)
    if is_region and name:lower():find(target_lower, 1, true) then
      reaper.SetEditCurPos(pos, true, false)
      return {region = name, position = pos}
    end
  end
  return nil, "No marker or region matching '" .. target .. "'"
end

-- ============================================================================
-- Phase
-- ============================================================================

function handlers.toggle_phase(params)
  local track, err = resolve_track(params.track)
  if not track then return nil, err end
  local current = reaper.GetMediaTrackInfo_Value(track, "B_PHASE")
  local new_phase = current == 0 and 1 or 0
  reaper.SetMediaTrackInfo_Value(track, "B_PHASE", new_phase)
  return {track = params.track, phase_inverted = new_phase == 1}
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
  get_track_meter = true,
  get_master_meter = true,
  get_envelope_points = true,
  list_installed_fx = true,
  ping = true,
  undo = true,  -- undo/redo manage their own undo state, don't wrap them
  redo = true,
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
