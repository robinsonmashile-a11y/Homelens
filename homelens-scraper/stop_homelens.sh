#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# stop_homelens.sh — Stop the Homelens pipeline scheduler.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/scheduler.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file found — scheduler may not be running."
  exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Sent SIGTERM to scheduler (PID $PID)"
  # Wait up to 5 seconds for clean exit
  for i in 1 2 3 4 5; do
    sleep 1
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "Scheduler stopped."
      rm -f "$PID_FILE"
      exit 0
    fi
  done
  echo "Process still running — sending SIGKILL"
  kill -9 "$PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "Scheduler force-stopped."
else
  echo "No process with PID $PID — removing stale PID file."
  rm -f "$PID_FILE"
fi
