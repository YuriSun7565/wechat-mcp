# WeChat MCP (macOS)

An MCP server that lets Claude check and control the WeChat (微信) desktop app on macOS.

## Tools

| Tool | What it does |
| --- | --- |
| `wechat_status` | Reports whether WeChat is **running** and whether it is **logged in** (read-only). |
| `open_wechat` | Launches WeChat or brings it to the front. |
| `login_wechat` | Opens WeChat and auto-clicks the login / "进入微信" button in the login window. |

All tools accept `response_format`: `"markdown"` (default) or `"json"`.

### How login detection works

It's a heuristic (so it tolerates WeChat version changes):

- **Not running** → System Events has no `WeChat` process.
- **Logged out** → WeChat is running and shows a small login window (≤ ~520×640) **or** a button whose label matches `进入微信 / 登录 / Log In`.
- **Logged in** → WeChat is running with a normal main window, or running with no window (minimized to the menu bar).

`login_wechat` clicks the login button for you, but the **final step** (scanning the QR code or confirming on your phone) still has to be done by you — that part can't be automated.

## Setup

### 1. Install dependencies (system-level Python)

The `mcp` package needs **Python ≥ 3.10**. macOS ships Python 3.9 at
`/usr/bin/python3`, which is too old (and shouldn't be modified), so use a
modern Python. On Apple Silicon the simplest is Homebrew:

```bash
brew install python                      # installs python3 into /opt/homebrew/bin
```

Then install the dependencies into that system-level interpreter. Homebrew's
Python is "externally managed" (PEP 668), so pass `--break-system-packages`:

```bash
/opt/homebrew/bin/python3 -m pip install --break-system-packages -r requirements.txt
```

Verify it imports:

```bash
/opt/homebrew/bin/python3 -c "import mcp, pydantic; print('ok')"
```

> Prefer isolation? You can still use a virtualenv
> (`/opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`)
> and point the config below at `.venv/bin/python` instead. This repo is set up
> to run against the system-level `/opt/homebrew/bin/python3`.

### 2. Grant Accessibility permission

UI inspection and button-clicking use macOS accessibility. The app that **launches** this server (the Claude desktop app, or Terminal if you run it manually) must be allowed:

> System Settings → Privacy & Security → **Accessibility** → enable the launching app.

Without this, `wechat_status` returns `running/logged_in = unknown` and explains the fix.

### 3. Register the server

Use the **absolute path** to the system-level Python and to `wechat_mcp.py`.

**Claude Code (local)** — add to the global `mcpServers` in `~/.claude.json`
(or run `claude mcp add wechat /opt/homebrew/bin/python3 /ABSOLUTE/PATH/TO/wechat_mcp/wechat_mcp.py`):

```json
{
  "mcpServers": {
    "wechat": {
      "type": "stdio",
      "command": "/opt/homebrew/bin/python3",
      "args": ["/ABSOLUTE/PATH/TO/wechat_mcp/wechat_mcp.py"]
    }
  }
}
```

**Cowork (Claude desktop app)** — add `mcpServers` to
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wechat": {
      "command": "/opt/homebrew/bin/python3",
      "args": ["/ABSOLUTE/PATH/TO/wechat_mcp/wechat_mcp.py"]
    }
  }
}
```

Then **restart** Claude Code (new session / `/mcp` to check) and fully quit &
reopen the Claude desktop app. The three tools become available in both.

> Using a virtualenv instead? Set `"command"` to `/ABSOLUTE/PATH/TO/wechat_mcp/.venv/bin/python`.

## Test it manually

```bash
# Quick smoke test of the detection logic (no MCP client needed):
python3 - <<'PY'
import asyncio, wechat_mcp
print(asyncio.run(wechat_mcp._inspect()))
PY
```

Or inspect with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python3 wechat_mcp.py
```

## Start / stop scripts

In normal use you **don't** need these — the MCP client (Claude Code / Cowork)
launches the stdio server on demand. The scripts are for manually running it in
the background to confirm it boots cleanly and to watch logs.

```bash
./start.sh     # launch in background (uses /opt/homebrew/bin/python3)
./stop.sh      # stop it
```

- `start.sh` keeps an open stdin via a FIFO so the stdio server doesn't hit EOF
  and exit. It writes runtime files under `.run/` (`server.pid`, `holder.pid`,
  `stdin.fifo`, `server.log`) and is a no-op if already running.
- `stop.sh` terminates the server and the stdin holder by PID and cleans up. It
  is idempotent.
- Override the interpreter with `WECHAT_MCP_PYTHON`, e.g.
  `WECHAT_MCP_PYTHON=$(pwd)/.venv/bin/python ./start.sh`.

`server.log` stays empty while the server idles waiting for JSON-RPC input —
that's expected. To actually exercise the tools, connect a client or use the
MCP Inspector (see above).

## Notes & limitations

- macOS only (uses `osascript` / System Events and the `open` command).
- Detection is heuristic; if a future WeChat build changes window sizes or button
  labels, adjust `LOGIN_BUTTON_KEYWORDS` / `LOGIN_WINDOW_MAX_*` near the top of
  `wechat_mcp.py`.
- The server never sends messages or moves data — it only checks state, opens the
  app, and clicks the login button.
