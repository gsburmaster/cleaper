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

def ensure_ipc_dir() -> None:
    IPC_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            IPC_DIR.chmod(stat.S_IRWXU)
        except OSError:
            pass


def send_command(action: str, params: dict | None = None) -> dict:
    """Send a command to REAPER via file IPC and wait for response."""
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

    # 2. Copy Lua script to REAPER's Scripts directory
    print("[2/4] Setting up REAPER scripts...")
    reaper_path = get_reaper_resource_path()
    lua_src = project_dir / "reaper_listener.lua"

    if reaper_path:
        scripts_dir = reaper_path / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        lua_dst = scripts_dir / "reaper_mcp_listener.lua"
        shutil.copy2(lua_src, lua_dst)
        print(f"  Copied to: {lua_dst}")
    else:
        print(f"  REAPER resource directory not found.")
        print(f"  Manually copy reaper_listener.lua to your REAPER Scripts folder.")

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

    # Remove Lua script from REAPER
    reaper_path = get_reaper_resource_path()
    if reaper_path:
        lua_dst = reaper_path / "Scripts" / "reaper_mcp_listener.lua"
        if lua_dst.exists():
            lua_dst.unlink()
            print(f"  Removed: {lua_dst}")
        else:
            print(f"  Script not found in REAPER Scripts directory")

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
    old_timeout = globals()["TIMEOUT"]
    # Use a short timeout for the ping check
    response = send_command.__wrapped__(2.0, "ping") if hasattr(send_command, "__wrapped__") else _quick_ping()
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
                            "TIPS:\n"
                            "- For EQ: add ReaEQ, then use get_fx_params to find band parameters\n"
                            "- For compression: add ReaComp, check params for threshold/ratio/attack/release\n"
                            "- For sends/buses: create_track for the bus, then create_send from source tracks\n"
                            "- You can chain multiple operations in sequence\n"
                            "- If a track name is ambiguous, the error will list the matches — ask the user to clarify"
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
    ]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
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
