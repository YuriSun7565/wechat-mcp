#!/usr/bin/env bash
#
# start.sh — launch the WeChat MCP server for manual run / testing.
#
# NOTE: In normal use you do NOT need this script. The MCP client
# (Claude Code / Cowork) launches the stdio server on demand using the
# command configured in its MCP config. This script is for manually
# running the server in the background so you can confirm it boots
# cleanly and watch its logs.
#
# Because the server speaks stdio, an open stdin is kept alive via a FIFO
# so the process doesn't hit EOF and exit immediately.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${WECHAT_MCP_PYTHON:-/opt/homebrew/bin/python3}"   # system-level python (override via env)
SCRIPT="$HERE/wechat_mcp.py"

RUN_DIR="$HERE/.run"
PID_FILE="$RUN_DIR/server.pid"
HOLDER_FILE="$RUN_DIR/holder.pid"
FIFO="$RUN_DIR/stdin.fifo"
LOG="$RUN_DIR/server.log"

mkdir -p "$RUN_DIR"

# Already running?
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "✅ already running (pid $(cat "$PID_FILE")). Logs: $LOG"
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "❌ python not found/executable: $PYTHON" >&2
  echo "   Set WECHAT_MCP_PYTHON to your interpreter, e.g.:" >&2
  echo "   WECHAT_MCP_PYTHON=\$(which python3) ./start.sh" >&2
  exit 1
fi

# Fresh FIFO; a background writer holds it open so the server's stdin never EOFs.
rm -f "$FIFO"
mkfifo "$FIFO"
sleep 2147483647 > "$FIFO" &
echo $! > "$HOLDER_FILE"

# Launch the server in the background.
nohup "$PYTHON" "$SCRIPT" < "$FIFO" > "$LOG" 2>&1 &
echo $! > "$PID_FILE"

sleep 1
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "✅ started wechat MCP (pid $(cat "$PID_FILE"))"
  echo "   python: $PYTHON"
  echo "   logs:   $LOG"
else
  echo "❌ server exited immediately — check $LOG" >&2
  "$HERE/stop.sh" >/dev/null 2>&1 || true
  exit 1
fi
