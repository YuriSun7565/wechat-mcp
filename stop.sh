#!/usr/bin/env bash
#
# stop.sh — stop a server started by start.sh.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$HERE/.run"
PID_FILE="$RUN_DIR/server.pid"
HOLDER_FILE="$RUN_DIR/holder.pid"
FIFO="$RUN_DIR/stdin.fifo"

stopped=0

kill_pidfile() {
  local f="$1" label="$2"
  if [[ -f "$f" ]]; then
    local pid; pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      # give it a moment, then force if still alive
      for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      echo "🛑 stopped $label (pid $pid)"
      stopped=1
    fi
    rm -f "$f"
  fi
}

kill_pidfile "$PID_FILE" "wechat MCP server"
kill_pidfile "$HOLDER_FILE" "stdin holder"
rm -f "$FIFO"

if [[ "$stopped" -eq 0 ]]; then
  echo "ℹ️  nothing to stop (no running server found)."
fi
