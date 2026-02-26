#!/usr/bin/env python3
"""
REAPER MCP Server — Control REAPER through natural language via Claude.

Usage:
    python mcp_server.py                # Run MCP server (stdio transport)
    python mcp_server.py install        # Auto-configure everything
    python mcp_server.py uninstall      # Remove all configuration
    python mcp_server.py check          # Verify setup and diagnose issues

The MCP server communicates with REAPER through file-based IPC. The Lua listener
(reaper_listener.lua) must be running inside REAPER for commands to work.
"""

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — all from environment, no hardcoded paths
# ---------------------------------------------------------------------------

def get_ipc_dir() -> Path:
    env = os.environ.get("REAPER_MCP_IPC_DIR")
    if env:
        return Path(env)
    return Path.home() / ".reaper-mcp"


def get_project_dir() -> Path:
    """Directory containing this script (the repo root)."""
    return Path(__file__).resolve().parent


IPC_DIR = get_ipc_dir()
CMD_FILE = IPC_DIR / "command.json"
RSP_FILE = IPC_DIR / "response.json"
POLL_INTERVAL = float(os.environ.get("REAPER_MCP_POLL_INTERVAL", "0.03"))
TIMEOUT = float(os.environ.get("REAPER_MCP_TIMEOUT", "10"))


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def get_reaper_resource_path() -> Path | None:
    """Auto-detect REAPER's resource/config directory."""
    candidates = []
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "REAPER")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "REAPER")
    else:  # Linux / other Unix
        candidates.append(Path.home() / ".config" / "REAPER")

    # Also check env override
    env = os.environ.get("REAPER_RESOURCE_PATH")
    if env:
        candidates.insert(0, Path(env))

    for p in candidates:
        if p.exists():
            return p
    return candidates[0] if candidates else None


def get_claude_code_config_path() -> Path:
    return Path.home() / ".claude" / "mcp_servers.json"


def get_claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------

# Serialize all commands — prevents concurrent tool calls from clobbering
# each other's command.json / response.json files.
_ipc_lock = threading.Lock()

def ensure_ipc_dir() -> None:
    IPC_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            IPC_DIR.chmod(stat.S_IRWXU)
        except OSError:
            pass


def send_command(action: str, params: dict | None = None) -> dict:
    """Send a command to REAPER via file IPC and wait for response."""
    with _ipc_lock:
        return _send_command_locked(action, params)


def _send_command_locked(action: str, params: dict | None = None) -> dict:
    ensure_ipc_dir()
    cmd_id = str(uuid.uuid4())
    cmd = {"id": cmd_id, "action": action, "params": params or {}}

    try:
        RSP_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        fd, tmp_path = tempfile.mkstemp(dir=IPC_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cmd, f)
        os.replace(tmp_path, CMD_FILE)
    except OSError as e:
        return {
            "id": cmd_id, "success": False, "result": None,
            "error": f"Failed to write command file: {e}",
        }

    start = time.monotonic()
    while time.monotonic() - start < TIMEOUT:
        if RSP_FILE.exists():
            try:
                content = RSP_FILE.read_text(encoding="utf-8")
                RSP_FILE.unlink(missing_ok=True)
                response = json.loads(content)
                if response.get("id") == cmd_id:
                    return response
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(POLL_INTERVAL)

    return {
        "id": cmd_id, "success": False, "result": None,
        "error": (
            "Timeout — REAPER did not respond. "
            "Make sure reaper_listener.lua is running inside REAPER. "
            "Run 'python mcp_server.py check' for diagnostics."
        ),
    }


# ---------------------------------------------------------------------------
# JSON config file merging
# ---------------------------------------------------------------------------

def merge_json_config(path: Path, key_path: list[str], value: dict) -> bool:
    """
    Safely merge a value into a nested JSON config file.
    Creates the file and parent directories if needed.
    Returns True if the file was modified.
    """
    config = {}
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                config = json.loads(text)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: Could not parse {path}: {e}")
            print(f"  Skipping — please add the config manually.")
            return False

    # Navigate to the right nesting level
    current = config
    for key in key_path[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]

    final_key = key_path[-1]
    if final_key in current and current[final_key] == value:
        return False  # Already configured

    current[final_key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return True


def remove_json_config_key(path: Path, key_path: list[str]) -> bool:
    """Remove a key from a nested JSON config file. Returns True if modified."""
    if not path.exists():
        return False
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    current = config
    for key in key_path[:-1]:
        if key not in current:
            return False
        current = current[key]

    if key_path[-1] not in current:
        return False

    del current[key_path[-1]]
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Install / Uninstall / Check
# ---------------------------------------------------------------------------

def get_mcp_server_config() -> dict:
    """Build the MCP server config entry for this installation."""
    project_dir = get_project_dir()
    if sys.platform == "win32":
        python_path = str(project_dir / ".venv" / "Scripts" / "python.exe")
    else:
        python_path = str(project_dir / ".venv" / "bin" / "python")

    # Fall back to current interpreter if venv doesn't exist yet
    if not Path(python_path).exists():
        python_path = str(Path(sys.executable).resolve())

    return {
        "command": python_path,
        "args": [str(project_dir / "mcp_server.py")],
    }


def cmd_install():
    """Auto-configure everything: IPC dir, REAPER scripts, Claude configs."""
    project_dir = get_project_dir()
    config = get_mcp_server_config()

    print("REAPER MCP Server — Install")
    print()

    # 1. IPC directory
    print("[1/4] Creating IPC directory...")
    ensure_ipc_dir()
    print(f"  {IPC_DIR}")

    # 2. Copy Lua script and JSFX analyzer to REAPER
    print("[2/4] Setting up REAPER scripts...")
    reaper_path = get_reaper_resource_path()
    lua_src = project_dir / "reaper_listener.lua"
    jsfx_src = project_dir / "mcp_analyzer.jsfx"

    if reaper_path:
        scripts_dir = reaper_path / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        lua_dst = scripts_dir / "reaper_mcp_listener.lua"
        shutil.copy2(lua_src, lua_dst)
        print(f"  Copied to: {lua_dst}")

        # Install JSFX analyzer
        effects_dir = reaper_path / "Effects"
        effects_dir.mkdir(parents=True, exist_ok=True)
        jsfx_dst = effects_dir / "mcp_analyzer.jsfx"
        if jsfx_src.exists():
            shutil.copy2(jsfx_src, jsfx_dst)
            print(f"  Copied to: {jsfx_dst}")
        else:
            print(f"  Warning: mcp_analyzer.jsfx not found in project directory")
    else:
        print(f"  REAPER resource directory not found.")
        print(f"  Manually copy reaper_listener.lua to your REAPER Scripts folder.")
        print(f"  Manually copy mcp_analyzer.jsfx to your REAPER Effects folder.")

    # 3. Configure Claude Code
    print("[3/4] Configuring Claude Code...")
    cc_path = get_claude_code_config_path()
    if merge_json_config(cc_path, ["reaper"], config):
        print(f"  Updated: {cc_path}")
    else:
        print(f"  Already configured: {cc_path}")

    # 4. Configure Claude Desktop
    print("[4/4] Configuring Claude Desktop...")
    cd_path = get_claude_desktop_config_path()
    if merge_json_config(cd_path, ["mcpServers", "reaper"], config):
        print(f"  Updated: {cd_path}")
    else:
        print(f"  Already configured: {cd_path}")

    # Summary
    print()
    print("=" * 50)
    print("Setup complete! Two things left:")
    print()
    print("1. LOAD THE LISTENER IN REAPER (one-time setup):")
    if reaper_path:
        print(f"   - Open REAPER")
        print(f"   - Actions > Show action list > Load...")
        print(f"   - Select: {scripts_dir / 'reaper_mcp_listener.lua'}")
        print(f"   - Run it once")
        print(f"   - (Optional) Right-click the action > 'Run at startup'")
        print(f"     so it starts automatically with REAPER")
    else:
        print(f"   - Open REAPER")
        print(f"   - Actions > Show action list > Load...")
        print(f"   - Select: {lua_src}")
        print(f"   - Run it")
    print()
    print("2. RESTART CLAUDE:")
    print("   - Restart Claude Code or Claude Desktop to pick up the config")
    print()
    print("Then try: 'Get the REAPER session state'")
    print()


def cmd_uninstall():
    """Remove all configuration."""
    project_dir = get_project_dir()

    print("REAPER MCP Server — Uninstall")
    print()

    # Remove from Claude Code config
    cc_path = get_claude_code_config_path()
    if remove_json_config_key(cc_path, ["reaper"]):
        print(f"  Removed from Claude Code config: {cc_path}")
    else:
        print(f"  Not found in Claude Code config")

    # Remove from Claude Desktop config
    cd_path = get_claude_desktop_config_path()
    if remove_json_config_key(cd_path, ["mcpServers", "reaper"]):
        print(f"  Removed from Claude Desktop config: {cd_path}")
    else:
        print(f"  Not found in Claude Desktop config")

    # Remove Lua script and JSFX from REAPER
    reaper_path = get_reaper_resource_path()
    if reaper_path:
        lua_dst = reaper_path / "Scripts" / "reaper_mcp_listener.lua"
        if lua_dst.exists():
            lua_dst.unlink()
            print(f"  Removed: {lua_dst}")
        else:
            print(f"  Script not found in REAPER Scripts directory")
        jsfx_dst = reaper_path / "Effects" / "mcp_analyzer.jsfx"
        if jsfx_dst.exists():
            jsfx_dst.unlink()
            print(f"  Removed: {jsfx_dst}")

    # Remove IPC directory
    if IPC_DIR.exists():
        shutil.rmtree(IPC_DIR)
        print(f"  Removed IPC directory: {IPC_DIR}")

    print()
    print("Uninstall complete. Restart Claude Code / Claude Desktop.")
    print()


def cmd_check():
    """Diagnose setup issues."""
    project_dir = get_project_dir()
    all_ok = True

    print("REAPER MCP Server — Setup Check")
    print()

    # Python version
    v = sys.version_info
    py_ok = v.major >= 3 and v.minor >= 10
    status = "OK" if py_ok else "FAIL"
    print(f"  [{status}] Python {v.major}.{v.minor}.{v.micro}", end="")
    if not py_ok:
        print(" (need 3.10+)", end="")
        all_ok = False
    print()

    # MCP package
    try:
        import mcp  # noqa: F401
        print(f"  [OK] mcp package installed")
    except ImportError:
        print(f"  [FAIL] mcp package not installed — run: pip install mcp")
        all_ok = False

    # IPC directory
    ipc_ok = IPC_DIR.exists()
    status = "OK" if ipc_ok else "MISSING"
    print(f"  [{status}] IPC directory: {IPC_DIR}")
    if not ipc_ok:
        all_ok = False

    # REAPER resource path
    reaper_path = get_reaper_resource_path()
    if reaper_path and reaper_path.exists():
        print(f"  [OK] REAPER resource path: {reaper_path}")
        lua_dst = reaper_path / "Scripts" / "reaper_mcp_listener.lua"
        if lua_dst.exists():
            print(f"  [OK] Listener script installed in REAPER")
        else:
            print(f"  [MISSING] Listener script not in REAPER Scripts dir")
            all_ok = False
        jsfx_dst = reaper_path / "Effects" / "mcp_analyzer.jsfx"
        if jsfx_dst.exists():
            print(f"  [OK] MCP Analyzer JSFX installed")
        else:
            print(f"  [MISSING] MCP Analyzer JSFX not in REAPER Effects dir")
            all_ok = False
    else:
        print(f"  [MISSING] REAPER resource path not found")
        print(f"           Set REAPER_RESOURCE_PATH env var if REAPER is installed elsewhere")
        all_ok = False

    # Claude Code config
    cc_path = get_claude_code_config_path()
    if cc_path.exists():
        try:
            cc = json.loads(cc_path.read_text())
            if "reaper" in cc:
                print(f"  [OK] Claude Code config")
            else:
                print(f"  [MISSING] 'reaper' entry not in Claude Code config")
                all_ok = False
        except (json.JSONDecodeError, OSError):
            print(f"  [FAIL] Claude Code config is malformed: {cc_path}")
            all_ok = False
    else:
        print(f"  [MISSING] Claude Code config: {cc_path}")

    # Claude Desktop config
    cd_path = get_claude_desktop_config_path()
    if cd_path.exists():
        try:
            cd = json.loads(cd_path.read_text())
            if "reaper" in cd.get("mcpServers", {}):
                print(f"  [OK] Claude Desktop config")
            else:
                print(f"  [MISSING] 'reaper' entry not in Claude Desktop config")
        except (json.JSONDecodeError, OSError):
            print(f"  [FAIL] Claude Desktop config is malformed")
    else:
        print(f"  [--] Claude Desktop config not found (OK if not using Desktop)")

    # Ping REAPER (quick check)
    print()
    print("  Pinging REAPER...", end="", flush=True)
    response = _quick_ping()
    if response.get("success"):
        result = response.get("result", {})
        print(f" Connected!")
        print(f"  [OK] REAPER {result.get('reaper_version', '?')} — "
              f"{result.get('track_count', '?')} tracks in '{result.get('project', '?')}'")
    else:
        print(f" Not responding")
        print(f"  [--] REAPER listener not running (start it from REAPER's Actions menu)")
        all_ok = False

    print()
    if all_ok:
        print("Everything looks good!")
    else:
        print("Some issues found. Run 'python mcp_server.py install' to fix.")
    print()


def _quick_ping() -> dict:
    """Ping REAPER with a short timeout."""
    ensure_ipc_dir()
    cmd_id = str(uuid.uuid4())
    cmd = {"id": cmd_id, "action": "ping", "params": {}}

    try:
        RSP_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd, tmp_path = tempfile.mkstemp(dir=IPC_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cmd, f)
        os.replace(tmp_path, CMD_FILE)
    except OSError:
        return {"success": False}

    start = time.monotonic()
    while time.monotonic() - start < 2.0:
        if RSP_FILE.exists():
            try:
                content = RSP_FILE.read_text(encoding="utf-8")
                RSP_FILE.unlink(missing_ok=True)
                response = json.loads(content)
                if response.get("id") == cmd_id:
                    return response
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.05)
    return {"success": False}


# ---------------------------------------------------------------------------
# MCP Server (only imported when actually running as server)
# ---------------------------------------------------------------------------

def run_server():
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        Prompt,
        PromptMessage,
        GetPromptResult,
    )

    server = Server("reaper")

    def make_result(response: dict) -> list[TextContent]:
        if response.get("success"):
            text = json.dumps(response.get("result", {}), indent=2)
        else:
            text = f"Error: {response.get('error', 'Unknown error')}"
        return [TextContent(type="text", text=text)]

    # -- Prompt: REAPER Assistant --
    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return [
            Prompt(
                name="reaper-assistant",
                description=(
                    "REAPER DAW assistant — gives Claude context about how to "
                    "use the REAPER tools effectively"
                ),
            ),
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
        if name != "reaper-assistant":
            raise ValueError(f"Unknown prompt: {name}")
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            "You are connected to REAPER (a digital audio workstation) via MCP tools. "
                            "You can control the DAW through natural language.\n\n"
                            "WORKFLOW:\n"
                            "1. ALWAYS call get_session_state first to see what tracks, FX, routing, "
                            "and items exist in the project before making changes.\n"
                            "2. Use the tool results to understand the current state before acting.\n"
                            "3. When asked to do something, translate it into the appropriate tool calls.\n\n"
                            "CONVENTIONS:\n"
                            "- Track names use partial, case-insensitive matching ('vox' finds 'Vox Lead')\n"
                            "- Volume is in dB (0 = unity gain, -6 = half volume)\n"
                            "- Pan is -100 (full left) to 100 (full right)\n"
                            "- FX parameters: call get_fx_params first to see parameter names and ranges\n"
                            "- Everything you do is wrapped in undo blocks — the user can Ctrl+Z any action\n\n"
                            "METERING:\n"
                            "- Use get_track_meter / get_master_meter to check levels (playback must be running)\n"
                            "- Check for clipping before and after making changes\n\n"
                            "TIPS:\n"
                            "- For EQ: add ReaEQ, then use get_fx_params to find band parameters\n"
                            "- For compression: add ReaComp, check params for threshold/ratio/attack/release\n"
                            "- For sends/buses: create_track for the bus, then create_send from source tracks\n"
                            "- Don't know which plugin? Call list_installed_fx with a category filter\n"
                            "- For automation: add_envelope_points with 'Volume', 'Pan', or 'FXName:ParamName'\n"
                            "- For organization: create_folder to group tracks (e.g., all drums)\n"
                            "- For rendering: render_project uses REAPER's last format settings\n"
                            "- toggle_phase for multi-mic phase issues (kick, snare, DI/amp)\n"
                            "- You can chain multiple operations in sequence\n"
                            "- If a track name is ambiguous, the error will list the matches — ask the user to clarify\n\n"
                            "VIBE-BASED SOUND DESIGN:\n"
                            "When the user describes sounds with creative/vague terms, translate them into concrete FX.\n"
                            "- Call get_preferences FIRST to check for user plugin and style preferences\n"
                            "- Call get_session_state to see existing FX (don't duplicate)\n"
                            "- Use list_installed_fx to check for preferred or third-party plugins\n"
                            "- Use apply_fx_chain to apply multiple FX with params in a single call\n"
                            "- Multiple small moves > one big move (gentle EQ + light compression, not extreme settings)\n"
                            "- Always explain what you're doing and why\n"
                            "- When the user says 'I like X for Y' or 'always use X', call set_preference to save it\n\n"
                            "REAPER BUILT-IN PLUGIN PARAMETER REFERENCE:\n"
                            "Use these param names directly with apply_fx_chain (no need to call get_fx_params first):\n"
                            "- ReaEQ: 'Band N Freq', 'Band N Gain', 'Band N BW' (bandwidth), "
                            "'Band N Type' (0=band, 1=lowshelf, 2=highshelf, 3=highpass, 4=lowpass, 5=notch, "
                            "6=bandpass, 8=allpass), 'Band N Enabled' (where N = 1-8)\n"
                            "- ReaComp: 'Thresh' (dB, 0 to -60), 'Ratio', 'Attack' (ms), 'Release' (ms), "
                            "'Knee', 'Pre-Comp' (lookahead), 'Wet', 'Dry'\n"
                            "- ReaDelay: 'Length' (ms), 'Feedback', 'Wet', 'Dry', 'Lowpass', 'Highpass'\n"
                            "- ReaVerbate: 'Room Size', 'Dampening', 'Stereo Width', 'Wet', 'Dry', 'Initial Delay'\n"
                            "- ReaXcomp: multi-band compressor with per-band thresholds\n"
                            "- JS: Saturation: 'Drive', 'Output', 'Mix'\n\n"
                            "COMMON VIBES → FX TRANSLATION:\n"
                            "- 'warm' → gentle low-shelf boost (+2-3dB ~200-300Hz), slight HF rolloff, light saturation\n"
                            "- 'bright/airy' → high-shelf boost ~8-12kHz, slight presence boost 3-5kHz\n"
                            "- 'moist/lush' → chorus or short modulated delay, gentle reverb\n"
                            "- 'punchy' → compression with fast attack, medium release, moderate ratio\n"
                            "- 'spacious/wide' → stereo reverb, stereo delay, Haas effect\n"
                            "- 'crispy/gritty' → saturation/distortion, presence boost\n"
                            "- 'thick/fat' → low-mid boost, compression, maybe parallel compression\n"
                            "- 'clean' → remove or reduce existing FX, cut muddy frequencies\n"
                            "- 'tight' → compression with fast attack, gate for noise, cut low frequencies\n"
                            "- 'sparkle' → high-frequency shelf boost, exciter/saturation on highs\n"
                            "- 'muddy (fix)' → cut 200-500Hz, add clarity with high boost\n"
                            "- 'sit in the mix' → EQ to carve space, compression for dynamics, volume adjustment\n"
                            "- 'lo-fi' → bit reduction, tape saturation, bandpass filter, vinyl noise\n"
                            "- 'radio' → bandpass 300Hz-3kHz, light compression, subtle distortion\n"
                            "These are starting points — adjust based on context, instrument, and user preferences.\n\n"
                            "SPECTRAL ANALYSIS & LUFS METERING:\n"
                            "- Use analyze_track to get 5-band spectral energy, peak/RMS, and crest factor\n"
                            "- Use get_loudness for short-term/integrated LUFS and K-weighted RMS\n"
                            "- Playback MUST be running for live readings — warn the user if stopped\n"
                            "- The MCP Analyzer JSFX is added automatically (transparent, no audio modification)\n"
                            "- Use spectral data to make informed EQ decisions rather than guessing\n\n"
                            "MIX AUDIT:\n"
                            "- Run audit_mix to diagnose the entire project at once\n"
                            "- Fix errors first (clipping), then warnings (hot tracks, low headroom), then info\n"
                            "- Playback should be running for accurate level checks\n\n"
                            "GAIN STAGING:\n"
                            "- auto_gain_stage adjusts faders to target -18dBFS (or custom target)\n"
                            "- Must run during a representative loud section (chorus, drop)\n"
                            "- Only adjusts faders, not item gain — fully reversible with Ctrl+Z\n\n"
                            "INSTRUMENT TEMPLATES:\n"
                            "- get_instrument_templates returns EQ+compression presets for specific instruments\n"
                            "- Workflow: get_instrument_templates('vocals') → pass fx_chain to apply_fx_chain\n"
                            "- 16 templates available, all using REAPER built-in plugins\n\n"
                            "FREQUENCY CONFLICTS:\n"
                            "- detect_frequency_conflicts finds masking between tracks\n"
                            "- Suggests complementary EQ carving — cut on one track, boost on the other\n\n"
                            "SIDECHAIN:\n"
                            "- setup_sidechain creates routing + configures compressor/gate automatically\n"
                            "- Common patterns: kick→bass (tight low end), vocal→music bus (ducking), kick→synth pad (pumping)\n\n"
                            "MASTERING:\n"
                            "- Use 'master' as the track name to target the master bus with any tool\n"
                            "- Mastering chain order: EQ → multiband comp → glue comp → limiter\n"
                            "- LUFS targets: Spotify/YouTube -14, Apple Music -16, broadcast -24, CD -9\n\n"
                            "SESSION CLEANUP:\n"
                            "- prepare_session removes empty tracks and creates standard buses\n"
                            "- set_track_color for visual organization\n"
                            "- Common color scheme: drums=red, bass=orange, guitars=green, vocals=blue, synths=purple, buses=grey"
                        ),
                    ),
                )
            ]
        )

    # -- Tool definitions --
    TOOLS = [
        Tool(
            name="ping",
            description=(
                "Check if REAPER is connected and the listener is running. "
                "Returns REAPER version, project name, and track count. "
                "Use this to verify the connection before doing work."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_session_state",
            description=(
                "Get the full state of the current REAPER project. CALL THIS FIRST before "
                "any other action to understand what tracks, FX, routing, and items exist. "
                "Returns track names, volumes, pans, mute/solo state, FX chains with all "
                "parameter names and current values, sends, items, markers, tempo, and "
                "time signature."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        # Transport
        Tool(
            name="play",
            description="Start playback in REAPER.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="stop",
            description="Stop playback in REAPER.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="pause",
            description="Pause playback in REAPER.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="record",
            description="Start recording in REAPER.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_tempo",
            description="Set the project tempo in BPM.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bpm": {
                        "type": "number",
                        "description": "Tempo in beats per minute (1-960)",
                    },
                },
                "required": ["bpm"],
            },
        ),
        Tool(
            name="get_transport_state",
            description="Get current transport state (playing, paused, recording, position, tempo).",
            inputSchema={"type": "object", "properties": {}},
        ),
        # Tracks
        Tool(
            name="create_track",
            description="Create a new track in the project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new track"},
                    "index": {
                        "type": "integer",
                        "description": "Insert position (0-based). Omit to add at end.",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="delete_track",
            description="Delete a track by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {
                        "type": "string",
                        "description": "Track name (case-insensitive, partial match OK)",
                    },
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="rename_track",
            description="Rename a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Current track name (partial match OK)"},
                    "new_name": {"type": "string", "description": "New name for the track"},
                },
                "required": ["track", "new_name"],
            },
        ),
        Tool(
            name="set_track_volume",
            description="Set a track's volume in dB. 0 dB = unity gain.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "db": {"type": "number", "description": "Volume in dB (e.g., -6, 0, +3)"},
                },
                "required": ["track", "db"],
            },
        ),
        Tool(
            name="set_track_pan",
            description="Set a track's pan. -100 = full left, 0 = center, 100 = full right.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "pan": {"type": "number", "description": "Pan position (-100 to 100)"},
                },
                "required": ["track", "pan"],
            },
        ),
        Tool(
            name="mute_track",
            description="Mute a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="unmute_track",
            description="Unmute a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="solo_track",
            description="Solo a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="unsolo_track",
            description="Unsolo a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="arm_track",
            description="Arm a track for recording.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="unarm_track",
            description="Disarm a track (stop record arm).",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        # FX
        Tool(
            name="list_installed_fx",
            description=(
                "List FX plugins installed on the system. Use this when the user asks to "
                "add an effect but doesn't specify which plugin — call this to see what's "
                "available and either pick the best match or ask the user to choose. "
                "Supports filtering by name and by category (eq, compressor, reverb, delay, "
                "distortion, limiter, gate, chorus)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Search string to filter plugin names (e.g., 'SSL', 'Fab')",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by type: eq, compressor, reverb, delay, distortion, limiter, gate, chorus",
                        "enum": ["eq", "compressor", "reverb", "delay", "distortion", "limiter", "gate", "chorus"],
                    },
                },
            },
        ),
        Tool(
            name="add_fx",
            description=(
                "Add an FX plugin to a track. Uses REAPER's FX browser search, so partial "
                "names work (e.g., 'ReaEQ', 'ReaComp', 'ReaDelay'). If the user doesn't "
                "specify a plugin, call list_installed_fx first to see what's available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "plugin_name": {
                        "type": "string",
                        "description": "Plugin name (e.g., 'ReaEQ', 'ReaComp')",
                    },
                },
                "required": ["track", "plugin_name"],
            },
        ),
        Tool(
            name="remove_fx",
            description="Remove an FX plugin from a track by name or index.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "fx": {"type": "string", "description": "FX name (partial match OK) or index"},
                },
                "required": ["track", "fx"],
            },
        ),
        Tool(
            name="get_fx_params",
            description=(
                "Get all parameters of an FX plugin with current values and ranges. "
                "Call this before set_fx_param to discover parameter names and valid ranges."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "fx": {"type": "string", "description": "FX name (partial match OK)"},
                },
                "required": ["track", "fx"],
            },
        ),
        Tool(
            name="set_fx_param",
            description=(
                "Set a parameter on an FX plugin. Parameter names are matched fuzzily. "
                "Use get_fx_params first to find parameter names and value ranges."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "fx": {"type": "string", "description": "FX name (partial match OK)"},
                    "param": {"type": "string", "description": "Parameter name (fuzzy match)"},
                    "value": {"type": "number", "description": "Value (check get_fx_params for range)"},
                },
                "required": ["track", "fx", "param", "value"],
            },
        ),
        Tool(
            name="bypass_fx",
            description="Bypass (disable) an FX plugin on a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "fx": {"type": "string", "description": "FX name (partial match OK)"},
                },
                "required": ["track", "fx"],
            },
        ),
        Tool(
            name="enable_fx",
            description="Enable (un-bypass) an FX plugin on a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "fx": {"type": "string", "description": "FX name (partial match OK)"},
                },
                "required": ["track", "fx"],
            },
        ),
        Tool(
            name="apply_fx_chain",
            description=(
                "Add multiple FX plugins to a track with pre-configured parameters in a single operation. "
                "Use this for applying complete effect chains — especially when interpreting creative/vibe-based "
                "sound descriptions. All FX are added in one undo block (Ctrl+Z reverts everything). "
                "Parameter names are fuzzy-matched. Values are clamped to plugin ranges."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "fx_chain": {
                        "type": "array",
                        "description": "FX plugins to add with parameter settings",
                        "items": {
                            "type": "object",
                            "properties": {
                                "plugin": {"type": "string", "description": "Plugin name (e.g., 'ReaEQ')"},
                                "params": {
                                    "type": "object",
                                    "description": "Parameter name → value pairs",
                                    "additionalProperties": {"type": "number"},
                                },
                            },
                            "required": ["plugin"],
                        },
                    },
                },
                "required": ["track", "fx_chain"],
            },
        ),
        # Routing
        Tool(
            name="create_send",
            description="Create a send from one track to another (bus routing, parallel processing).",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source track name"},
                    "dest": {"type": "string", "description": "Destination track name"},
                    "volume_db": {"type": "number", "description": "Send volume in dB (default 0)"},
                },
                "required": ["source", "dest"],
            },
        ),
        Tool(
            name="remove_send",
            description="Remove a send between two tracks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source track name"},
                    "dest": {"type": "string", "description": "Destination track name"},
                },
                "required": ["source", "dest"],
            },
        ),
        Tool(
            name="set_send_volume",
            description="Set the volume of an existing send between two tracks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source track name"},
                    "dest": {"type": "string", "description": "Destination track name"},
                    "db": {"type": "number", "description": "Send volume in dB"},
                },
                "required": ["source", "dest", "db"],
            },
        ),
        # Items & MIDI
        Tool(
            name="create_midi_item",
            description="Create an empty MIDI item on a track.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "start_beat": {"type": "number", "description": "Start position in beats"},
                    "length_beats": {"type": "number", "description": "Length in beats"},
                },
                "required": ["track", "start_beat", "length_beats"],
            },
        ),
        Tool(
            name="insert_midi_notes",
            description=(
                "Insert MIDI notes into a MIDI item. Each note needs pitch (0-127, "
                "60 = middle C), start_beat, and length_beats. Velocity defaults to 100."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "item_index": {"type": "integer", "description": "MIDI item index (default 0)"},
                    "notes": {
                        "type": "array",
                        "description": "Notes to insert",
                        "items": {
                            "type": "object",
                            "properties": {
                                "pitch": {"type": "integer", "description": "MIDI note (0-127, 60=C4)"},
                                "velocity": {"type": "integer", "description": "Velocity (1-127, default 100)"},
                                "start_beat": {"type": "number", "description": "Start in beats"},
                                "length_beats": {"type": "number", "description": "Duration in beats"},
                                "channel": {"type": "integer", "description": "MIDI channel (1-16, default 1)"},
                            },
                            "required": ["pitch", "start_beat", "length_beats"],
                        },
                    },
                },
                "required": ["track", "notes"],
            },
        ),
        Tool(
            name="delete_item",
            description="Delete a media item from a track by index.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "item_index": {"type": "integer", "description": "Item index on the track"},
                },
                "required": ["track", "item_index"],
            },
        ),
        Tool(
            name="move_item",
            description="Move a media item to a new position in seconds.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "item_index": {"type": "integer", "description": "Item index on the track"},
                    "position": {"type": "number", "description": "New position in seconds"},
                },
                "required": ["track", "item_index", "position"],
            },
        ),
        Tool(
            name="split_item_at",
            description="Split a media item at a position in seconds.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "item_index": {"type": "integer", "description": "Item index on the track"},
                    "position": {"type": "number", "description": "Split position in seconds"},
                },
                "required": ["track", "item_index", "position"],
            },
        ),
        # Markers & Regions
        Tool(
            name="add_marker",
            description="Add a project marker at a position in seconds.",
            inputSchema={
                "type": "object",
                "properties": {
                    "position": {"type": "number", "description": "Position in seconds (default: edit cursor)"},
                    "name": {"type": "string", "description": "Marker name"},
                },
            },
        ),
        Tool(
            name="add_region",
            description="Add a project region between two positions in seconds.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "Region start in seconds"},
                    "finish": {"type": "number", "description": "Region end in seconds"},
                    "name": {"type": "string", "description": "Region name"},
                },
                "required": ["start", "finish"],
            },
        ),
        # Undo/Redo
        Tool(
            name="undo",
            description="Undo the last action in REAPER (Ctrl+Z).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="redo",
            description="Redo the last undone action in REAPER (Ctrl+Shift+Z).",
            inputSchema={"type": "object", "properties": {}},
        ),
        # -- Metering / Analysis --
        Tool(
            name="get_track_meter",
            description=(
                "Read the current peak level of a track in dB. Use this to check levels, "
                "detect clipping, or compare loudness between tracks. Returns left/right "
                "peak values and a clipping flag. Playback must be running for live readings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="get_master_meter",
            description=(
                "Read the current peak level of the master bus in dB. Use to check if the "
                "mix is clipping or to gauge overall loudness. Playback must be running."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        # -- Render / Bounce --
        Tool(
            name="render_project",
            description=(
                "Render/bounce the project to an audio file. Uses REAPER's most recent "
                "render format settings (WAV, MP3, etc. — whatever the user last configured). "
                "You can set the output directory, filename, bounds, and sample rate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Output directory path. Omit to use REAPER's current setting.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Filename pattern (e.g., 'mix_v2'). Omit for default.",
                    },
                    "bounds": {
                        "type": "string",
                        "description": "What to render: 'project' (entire), 'time_selection', or 'custom'",
                        "enum": ["project", "time_selection", "custom"],
                    },
                    "start_time": {
                        "type": "number",
                        "description": "Start time in seconds (only with bounds='custom')",
                    },
                    "end_time": {
                        "type": "number",
                        "description": "End time in seconds (only with bounds='custom')",
                    },
                    "sample_rate": {
                        "type": "integer",
                        "description": "Sample rate in Hz (e.g., 44100, 48000, 96000)",
                    },
                    "channels": {
                        "type": "integer",
                        "description": "Number of channels (1=mono, 2=stereo)",
                    },
                },
            },
        ),
        # -- Automation / Envelopes --
        Tool(
            name="add_envelope_points",
            description=(
                "Add automation points to a track envelope. Envelope can be 'Volume', "
                "'Pan', 'Mute', or an FX parameter as 'FXName:ParamName' (e.g., "
                "'ReaEQ:Frequency'). Volume values are in dB, pan in -100..100. "
                "Shapes: 0=linear, 1=square, 2=S-curve, 3=fast start, 4=fast end, 5=bezier."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "envelope": {
                        "type": "string",
                        "description": "Envelope name: 'Volume', 'Pan', 'Mute', or 'FXName:ParamName'",
                    },
                    "points": {
                        "type": "array",
                        "description": "Automation points to add",
                        "items": {
                            "type": "object",
                            "properties": {
                                "time": {"type": "number", "description": "Position in seconds"},
                                "value": {
                                    "type": "number",
                                    "description": "Value (dB for Volume, -100..100 for Pan, raw for FX params)",
                                },
                                "shape": {
                                    "type": "integer",
                                    "description": "Curve shape (0=linear, 1=square, 2=S-curve, default 0)",
                                },
                            },
                            "required": ["time", "value"],
                        },
                    },
                },
                "required": ["track", "envelope", "points"],
            },
        ),
        Tool(
            name="get_envelope_points",
            description=(
                "Read existing automation points from a track envelope. Returns time, "
                "value, and shape for each point. Use to understand existing automation "
                "before adding or modifying points."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "envelope": {
                        "type": "string",
                        "description": "Envelope name: 'Volume', 'Pan', 'Mute', or 'FXName:ParamName'",
                    },
                    "start_time": {"type": "number", "description": "Filter: start time in seconds"},
                    "end_time": {"type": "number", "description": "Filter: end time in seconds"},
                },
                "required": ["track", "envelope"],
            },
        ),
        Tool(
            name="clear_envelope",
            description=(
                "Clear automation points from a track envelope. Optionally specify a time "
                "range to clear only points within that range."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "envelope": {
                        "type": "string",
                        "description": "Envelope name: 'Volume', 'Pan', 'Mute', or 'FXName:ParamName'",
                    },
                    "start_time": {"type": "number", "description": "Start of range to clear (seconds)"},
                    "end_time": {"type": "number", "description": "End of range to clear (seconds)"},
                },
                "required": ["track", "envelope"],
            },
        ),
        # -- Track Folders / Grouping --
        Tool(
            name="create_folder",
            description=(
                "Create a folder track and move specified child tracks into it. "
                "Use for organizing tracks into groups (e.g., all drums under a 'Drums' folder)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Folder track name"},
                    "children": {
                        "type": "array",
                        "description": "Track names to put inside the folder",
                        "items": {"type": "string"},
                    },
                },
                "required": ["name", "children"],
            },
        ),
        # -- Item Gain & Fades --
        Tool(
            name="set_item_gain",
            description="Set the gain (volume) of a media item in dB.",
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "item_index": {"type": "integer", "description": "Item index on the track (default 0)"},
                    "gain_db": {"type": "number", "description": "Gain in dB (0 = unity)"},
                },
                "required": ["track", "gain_db"],
            },
        ),
        Tool(
            name="set_item_fade_in",
            description=(
                "Set the fade-in on a media item. Shapes: 0=linear, 1=exponential, "
                "2=S-curve, 3=exponential (alt), 4=fast start, 5=fast end, 6=bezier."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "item_index": {"type": "integer", "description": "Item index on the track (default 0)"},
                    "length": {"type": "number", "description": "Fade-in length in seconds"},
                    "shape": {"type": "integer", "description": "Fade shape (0-6, default 0=linear)"},
                },
                "required": ["track", "length"],
            },
        ),
        Tool(
            name="set_item_fade_out",
            description=(
                "Set the fade-out on a media item. Shapes: 0=linear, 1=exponential, "
                "2=S-curve, 3=exponential (alt), 4=fast start, 5=fast end, 6=bezier."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                    "item_index": {"type": "integer", "description": "Item index on the track (default 0)"},
                    "length": {"type": "number", "description": "Fade-out length in seconds"},
                    "shape": {"type": "integer", "description": "Fade shape (0-6, default 0=linear)"},
                },
                "required": ["track", "length"],
            },
        ),
        # -- Cursor & Time Selection --
        Tool(
            name="set_cursor_position",
            description="Move the edit cursor to a position in seconds.",
            inputSchema={
                "type": "object",
                "properties": {
                    "time": {"type": "number", "description": "Position in seconds from project start"},
                },
                "required": ["time"],
            },
        ),
        Tool(
            name="set_time_selection",
            description="Set the time selection (highlighted region) in REAPER.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_time": {"type": "number", "description": "Selection start in seconds"},
                    "end_time": {"type": "number", "description": "Selection end in seconds"},
                },
                "required": ["start_time", "end_time"],
            },
        ),
        Tool(
            name="set_loop_points",
            description="Set the loop region and optionally enable/disable looping.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_time": {"type": "number", "description": "Loop start in seconds"},
                    "end_time": {"type": "number", "description": "Loop end in seconds"},
                    "enable": {"type": "boolean", "description": "Enable (true) or disable (false) repeat/loop"},
                },
                "required": ["start_time", "end_time"],
            },
        ),
        Tool(
            name="go_to_marker",
            description="Jump the edit cursor to a marker or region by name or index number.",
            inputSchema={
                "type": "object",
                "properties": {
                    "marker": {
                        "type": "string",
                        "description": "Marker name (partial match) or index number",
                    },
                },
                "required": ["marker"],
            },
        ),
        # -- Phase --
        Tool(
            name="toggle_phase",
            description=(
                "Flip the polarity (phase) of a track. Essential for multi-mic setups "
                "(kick in/out, snare top/bottom, DI vs amp)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "description": "Track name (partial match OK)"},
                },
                "required": ["track"],
            },
        ),
        # -- User preferences (handled locally, not routed to REAPER) --
        Tool(
            name="set_preference",
            description=(
                "Save a user mixing preference that persists across sessions. Use for plugin "
                "preferences (e.g., 'compressor_plugin' → 'FabFilter Pro-C 2'), mixing style notes, or "
                "custom vibe definitions. Call this when the user expresses a preference."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Preference key (e.g., 'compressor_plugin', 'style', 'moist_means')",
                    },
                    "value": {
                        "type": "string",
                        "description": "Preference value",
                    },
                },
                "required": ["key", "value"],
            },
        ),
        Tool(
            name="get_preferences",
            description=(
                "Get all saved user mixing preferences. Call this before interpreting vibe-based "
                "requests to respect the user's plugin and style preferences."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        # -- Analysis & Metering --
        Tool(
            name="analyze_track",
            description=(
                "Get spectral analysis of a track — 5-band energy (sub/low/mid/high-mid/high), "
                "peak levels, RMS levels, and crest factor. Requires playback to be running. "
                "Automatically adds the MCP Analyzer JSFX if not present (transparent — does not "
                "modify audio). Use this to diagnose frequency balance, identify problem areas, "
                "and make informed EQ decisions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {
                        "type": "string",
                        "description": "Track name (or 'master' for master bus)",
                    },
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="get_loudness",
            description=(
                "Get LUFS loudness metering for a track — short-term LUFS (3s window), "
                "integrated LUFS (since reset), and K-weighted RMS. Returns platform loudness "
                "targets (Spotify -14, YouTube -14, Apple Music -16, broadcast -24, CD -9). "
                "Requires playback to be running. Use for loudness matching and mastering."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {
                        "type": "string",
                        "description": "Track name (or 'master' for master bus)",
                    },
                    "reset": {
                        "type": "boolean",
                        "description": "Reset integrated LUFS counter before reading (default false)",
                    },
                },
                "required": ["track"],
            },
        ),
        Tool(
            name="audit_mix",
            description=(
                "Run a comprehensive mix diagnostic. Checks all tracks for: clipping (peak >= 0dBFS), "
                "hot levels (> -6dB), very quiet tracks, master bus clipping/headroom, missing HPF, "
                "empty tracks, tracks with no FX, and inverted phase. Returns issues sorted by "
                "severity (error > warning > info). Playback should be running for level checks."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="auto_gain_stage",
            description=(
                "Automatically adjust track faders so all tracks peak near a target level "
                "(default -18 dBFS). Requires playback to be running during a representative "
                "loud section (e.g., chorus). Skips folder tracks and silent tracks. "
                "Wrapped in undo block — Ctrl+Z to revert. Does NOT modify item gain, only faders."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {
                        "type": "string",
                        "description": "Specific track name, or omit for all tracks",
                    },
                    "target_db": {
                        "type": "number",
                        "description": "Target peak level in dBFS (default -18)",
                    },
                },
            },
        ),
        Tool(
            name="get_instrument_templates",
            description=(
                "Get instrument-specific FX chain templates using REAPER built-in plugins. "
                "Returns EQ + compression settings tailored to the instrument. Templates available: "
                "vocals, kick, snare, hi_hat, overheads, bass_electric, bass_synth, guitar_clean, "
                "guitar_distorted, acoustic_guitar, piano_keys, synth_pad, synth_lead, strings, "
                "brass, master. Pass the result's fx_chain directly to apply_fx_chain."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instrument": {
                        "type": "string",
                        "description": (
                            "Instrument name to get template for (e.g., 'vocals', 'kick'). "
                            "Omit to list all available templates."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="detect_frequency_conflicts",
            description=(
                "Detect frequency masking conflicts between tracks. Uses two detection modes: "
                "(1) EQ heuristic — scans EQ plugins for overlapping boosts within ~1 octave, "
                "(2) Spectral — compares MCP Analyzer energy bands if present. "
                "Returns conflicting track pairs with frequency ranges and suggestions for "
                "complementary EQ carving."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="setup_sidechain",
            description=(
                "Set up sidechain compression or gating between two tracks. Creates a send from "
                "trigger to target on channels 3/4 (sidechain input), adds ReaComp/ReaGate, "
                "and configures preset parameters. Common uses: kick→bass (tighten low end), "
                "vocal→music (ducking), kick→synth (pumping effect)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trigger": {
                        "type": "string",
                        "description": "Source/trigger track name (e.g., 'Kick')",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target track to compress/gate (e.g., 'Bass')",
                    },
                    "effect": {
                        "type": "string",
                        "enum": ["compress", "gate"],
                        "description": "Sidechain effect type (default: compress)",
                    },
                    "intensity": {
                        "type": "string",
                        "enum": ["gentle", "moderate", "heavy"],
                        "description": "Compression/gate intensity preset (default: moderate)",
                    },
                },
                "required": ["trigger", "target"],
            },
        ),
        Tool(
            name="prepare_session",
            description=(
                "Clean up and prepare a session for mixing. Removes empty tracks (no items, FX, "
                "sends, or receives — preserves folders). Creates standard bus tracks if missing: "
                "Drum Bus, Vocal Bus, Instrument Bus, FX Bus. Wrapped in undo block."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "description": "Cleanup options",
                        "properties": {
                            "remove_empty": {
                                "type": "boolean",
                                "description": "Remove empty tracks (default true)",
                            },
                            "create_buses": {
                                "type": "boolean",
                                "description": "Create standard bus tracks if missing (default true)",
                            },
                        },
                    },
                },
            },
        ),
        Tool(
            name="set_track_color",
            description=(
                "Set a track's color in REAPER for visual organization. Common scheme: "
                "drums=red (255,0,0), bass=orange (255,165,0), guitars=green (0,180,0), "
                "vocals=blue (0,100,255), synths=purple (150,0,255), buses=grey (128,128,128)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track": {
                        "type": "string",
                        "description": "Track name",
                    },
                    "r": {
                        "type": "integer",
                        "description": "Red component (0-255)",
                    },
                    "g": {
                        "type": "integer",
                        "description": "Green component (0-255)",
                    },
                    "b": {
                        "type": "integer",
                        "description": "Blue component (0-255)",
                    },
                },
                "required": ["track", "r", "g", "b"],
            },
        ),
    ]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    PREFS_FILE = IPC_DIR / "preferences.json"

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        # Preference tools are handled locally (no REAPER round-trip)
        if name == "set_preference":
            prefs = json.loads(PREFS_FILE.read_text()) if PREFS_FILE.exists() else {}
            prefs[arguments["key"]] = arguments["value"]
            PREFS_FILE.write_text(json.dumps(prefs, indent=2))
            return [TextContent(type="text", text=json.dumps({"saved": arguments["key"], "value": arguments["value"]}))]

        if name == "get_preferences":
            prefs = json.loads(PREFS_FILE.read_text()) if PREFS_FILE.exists() else {}
            return [TextContent(type="text", text=json.dumps(prefs, indent=2))]

        if name == "get_instrument_templates":
            templates_path = get_project_dir() / "templates.json"
            if not templates_path.exists():
                return [TextContent(type="text", text="Error: templates.json not found")]
            templates = json.loads(templates_path.read_text())
            instrument = (arguments.get("instrument") or "").strip().lower()
            if not instrument:
                # Return all template names and descriptions
                summary = {k: v["description"] for k, v in templates.items()}
                return [TextContent(type="text", text=json.dumps(summary, indent=2))]
            # Fuzzy match: exact first, then partial
            if instrument in templates:
                return [TextContent(type="text", text=json.dumps(templates[instrument], indent=2))]
            matches = [k for k in templates if instrument in k or k in instrument]
            if len(matches) == 1:
                return [TextContent(type="text", text=json.dumps(templates[matches[0]], indent=2))]
            if len(matches) > 1:
                return [TextContent(type="text", text=f"Ambiguous instrument '{instrument}', matches: {', '.join(matches)}")]
            return [TextContent(type="text", text=f"No template found for '{instrument}'. Available: {', '.join(templates.keys())}")]

        response = await asyncio.to_thread(send_command, name, arguments)
        return make_result(response)

    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        # Default: run MCP server
        run_server()
        return

    cmd = sys.argv[1].lower().lstrip("-")
    if cmd == "install":
        cmd_install()
    elif cmd == "uninstall":
        cmd_uninstall()
    elif cmd == "check":
        cmd_check()
    elif cmd == "help":
        print(__doc__)
    else:
        print(f"Unknown command: {sys.argv[1]}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
