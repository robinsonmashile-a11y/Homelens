#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start_homelens.sh — Start the Homelens pipeline scheduler.
#
# Usage:
#   ./start_homelens.sh          # start scheduler in background
#   ./start_homelens.sh --now    # run pipeline once immediately (foreground)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/scheduler.pid"
LOG_FILE="$SCRIPT_DIR/logs/scheduler.log"
PYTHON="${PYTHON:-python3}"

mkdir -p "$SCRIPT_DIR/logs"

# ── --now: run once in foreground ───────────────────────────────────────────
if [[ "${1:-}" == "--now" ]]; then
  echo "Running pipeline once (foreground)…"
  cd "$SCRIPT_DIR"
  "$PYTHON" scripts/scheduler.py --now
  exit 0
fi

# ── Check if already running ─────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "Scheduler already running (PID $PID). Use stop_homelens.sh first."
    exit 1
  else
    echo "Stale PID file found — removing."
    rm -f "$PID_FILE"
  fi
fi

# ── Start scheduler in background ────────────────────────────────────────────
cd "$SCRIPT_DIR"
nohup "$PYTHON" scripts/scheduler.py >> "$LOG_FILE" 2>&1 &
BGPID=$!

# Give it a moment to write its own PID file
sleep 1

if kill -0 "$BGPID" 2>/dev/null; then
  echo "Scheduler started (PID $BGPID)"
  echo "Log: $LOG_FILE"
  echo "Dashboard: open dashboard/index.html in your browser"
else
  echo "ERROR: Scheduler failed to start — check $LOG_FILE"
  exit 1
fi
