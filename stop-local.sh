#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
LAUNCH_DOMAIN="gui/$(id -u)"

stop_launch_job() {
  local label="$1"
  if launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
    echo "$label: unloading launchd job"
    launchctl bootout "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1 || true
  fi
}

stop_port() {
  local label="$1"
  local port="$2"
  local pids
  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"

  if [ -z "$pids" ]; then
    echo "$label: nothing listening on port $port"
    return 0
  fi

  echo "$label: stopping listener(s) on port $port: $pids"
  for pid in ${(f)pids}; do
    kill "$pid" >/dev/null 2>&1 || true
  done

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! lsof -ti tcp:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  if lsof -ti tcp:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    local stubborn
    stubborn="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    echo "$label: forcing stop on port $port: $stubborn"
    for pid in ${(f)stubborn}; do
      kill -9 "$pid" >/dev/null 2>&1 || true
    done
  fi
}

stop_launch_job "com.kuon.subtitle-workstation.backend"
stop_launch_job "com.kuon.subtitle-workstation.frontend"
stop_launch_job "com.kuon.subtitle-workstation.srt-api-tunnel"
stop_port "backend" "$BACKEND_PORT"
stop_port "frontend" "$FRONTEND_PORT"
rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"
