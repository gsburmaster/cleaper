# REAPER MCP Server

Control [REAPER](https://www.reaper.fm/) through natural language with Claude.

> "Add a high-pass at 120Hz on vocals"
> "Create a drum bus and route all drum tracks to it"
> "Set the tempo to 140 and solo the bass"

Works with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Claude Desktop](https://claude.ai/download) via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Quick Start

```bash
git clone https://github.com/username/reaper-mcp.git
cd reaper-mcp
./install.sh
```

That's it for the terminal side. The installer:
- Creates a Python virtual environment and installs dependencies
- Detects your REAPER installation and copies the listener script
- Auto-configures Claude Code and Claude Desktop

**One manual step in REAPER** (first time only):
1. Open REAPER
2. Actions > Show action list > **Load...**
3. Select `reaper_mcp_listener.lua` from your REAPER Scripts folder
4. Run it
5. *(Optional)* Right-click the action > **Run at startup** so it auto-starts with REAPER

Then restart Claude Code / Claude Desktop and ask: *"Get the REAPER session state"*

### Windows

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python mcp_server.py install
```

## How It Works

```
Claude ──MCP──▶ mcp_server.py ──file IPC──▶ reaper_listener.lua ──ReaScript──▶ REAPER
```

Communication happens through JSON files in `~/.reaper-mcp/`. No network sockets, no dependencies inside REAPER. The Lua script polls at ~30Hz — more than fast enough for DAW operations.

Everything Claude does is wrapped in REAPER undo blocks. **Ctrl+Z undoes any action.**

## Commands

| Category | What you can do |
|----------|----------------|
| **Session** | Read full project state — tracks, FX chains, routing, items, markers, tempo |
| **Transport** | Play, stop, pause, record, set tempo |
| **Tracks** | Create, delete, rename, set volume/pan, mute, solo, arm |
| **FX** | Add/remove plugins, get/set parameters, bypass/enable |
| **Routing** | Create/remove sends, set send volume |
| **MIDI** | Create MIDI items, insert notes |
| **Items** | Delete, move, split media items |
| **Markers** | Add markers and regions |
| **Undo** | Undo/redo any action |

## CLI

```bash
python mcp_server.py              # Run MCP server (normal usage — Claude does this)
python mcp_server.py install      # Auto-configure everything
python mcp_server.py uninstall    # Remove all config
python mcp_server.py check        # Diagnose issues
```

## Configuration

All settings are environment variables. Defaults work for most setups.

| Variable | Default | Description |
|----------|---------|-------------|
| `REAPER_MCP_IPC_DIR` | `~/.reaper-mcp` | IPC file directory |
| `REAPER_MCP_POLL_INTERVAL` | `0.03` | Poll interval in seconds |
| `REAPER_MCP_TIMEOUT` | `10` | Response timeout in seconds |
| `REAPER_RESOURCE_PATH` | *(auto-detected)* | Override REAPER's resource directory |

## Troubleshooting

Run `python mcp_server.py check` — it tests every component and tells you exactly what's wrong.

| Problem | Fix |
|---------|-----|
| Timeout errors | Run the listener in REAPER: Actions > Show action list > find `reaper_mcp_listener` > Run |
| Plugin not found | Use the name from REAPER's FX browser. Built-ins: `ReaEQ`, `ReaComp`, `ReaDelay` |
| Ambiguous track name | Be more specific — "Kick" matches "Kick In" and "Kick Out" |
| MCP server not showing up | Run `python mcp_server.py install` then restart Claude |

## Security

- **Local only** — all communication through local files, no network
- **Owner-only permissions** — IPC directory is `700` on Unix
- **Atomic writes** — temp file + rename prevents partial reads
- **Action allowlist** — Lua listener rejects unknown commands
- **Input validation** — size limits, depth limits, value clamping

## Architecture

**Why file-based IPC?** REAPER's Lua doesn't ship luasocket reliably. File polling at 30Hz is plenty fast and works everywhere. You can debug by reading the JSON files yourself.

**Why not OSC?** MCP gives Claude typed, described tool interfaces. Claude knows what each tool does and when to use it — no mapping table needed.

## Contributing

Contributions welcome. Please open an issue first to discuss.

Adding a new command:
1. Add the handler in `reaper_listener.lua` (auto-added to the allowlist)
2. Add the `Tool` in `mcp_server.py`
3. Test with REAPER

## License

[MIT](LICENSE)
