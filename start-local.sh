#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
RUN_DIR="$ROOT_DIR/.run"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
APP_HOST="${APP_HOST:-0.0.0.0}"
DETACH="${DETACH:-0}"

ensure_not_running() {
  for pid_file in "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"; do
    if [ -f "$pid_file" ]; then
      local existing_pid
      existing_pid="$(cat "$pid_file")"
      if [ -n "$existing_pid" ] && kill -0 "$existing_pid" >/dev/null 2>&1; then
        echo "Subtitle Workstation already appears to be running (pid $existing_pid)." >&2
        echo "Run ./stop-local.sh first, or kill the existing process." >&2
        exit 1
      fi
      rm -f "$pid_file"
    fi
  done
}

wait_for_http() {
  local url="$1"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cleanup() {
  local exit_code=$?
  trap - INT TERM EXIT
  "$ROOT_DIR/stop-local.sh" >/dev/null 2>&1 || true
  exit "$exit_code"
}

if [ ! -d "$BACKEND_DIR" ] || [ ! -d "$FRONTEND_DIR" ]; then
  echo "Expected backend/ and frontend/ under: $ROOT_DIR" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Missing Python interpreter: $PYTHON_BIN" >&2
  echo "Tip: install Python 3.13 or rerun with PYTHON_BIN=python3" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required but not installed." >&2
  exit 1
fi

mkdir -p "$RUN_DIR"
ensure_not_running

if [ ! -d "$BACKEND_DIR/.venv" ]; then
  echo "Creating backend virtualenv with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv "$BACKEND_DIR/.venv"
fi

if [ ! -x "$BACKEND_DIR/.venv/bin/pip" ]; then
  echo "Backend virtualenv is missing pip: $BACKEND_DIR/.venv" >&2
  exit 1
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "Installing frontend dependencies..."
  (cd "$FRONTEND_DIR" && npm install)
fi

echo "Ensuring backend dependencies are installed..."
"$BACKEND_DIR/.venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt" >/dev/null

echo
echo "Starting Subtitle Workstation"
echo "- Backend bind:  http://$APP_HOST:$BACKEND_PORT"
echo "- Frontend bind: http://$APP_HOST:$FRONTEND_PORT"
echo "- Local API docs: http://127.0.0.1:$BACKEND_PORT/docs"
echo

(
  cd "$BACKEND_DIR"
  source .venv/bin/activate
  exec uvicorn app.main:app --host "$APP_HOST" --port "$BACKEND_PORT"
) >"$RUN_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$BACKEND_PID_FILE"

(
  cd "$FRONTEND_DIR"
  exec npm run dev -- --host "$APP_HOST" --port "$FRONTEND_PORT" --strictPort
) >"$RUN_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "$FRONTEND_PID" > "$FRONTEND_PID_FILE"

if ! wait_for_http "http://127.0.0.1:$BACKEND_PORT/health"; then
  echo "Backend failed to start. Check .run/backend.log" >&2
  cleanup
fi

if ! wait_for_http "http://127.0.0.1:$FRONTEND_PORT"; then
  echo "Frontend failed to start. Check .run/frontend.log" >&2
  cleanup
fi

if [ "$DETACH" = "1" ]; then
  echo "Started in detached mode."
  echo "- backend pid: $BACKEND_PID"
  echo "- frontend pid: $FRONTEND_PID"
  echo "Use ./stop-local.sh to stop both."
  exit 0
fi

echo "Press Ctrl+C once to stop both servers."
echo
trap cleanup INT TERM EXIT
tail -f "$RUN_DIR/backend.log" "$RUN_DIR/frontend.log" &
TAIL_PID=$!
wait "$BACKEND_PID" "$FRONTEND_PID"
kill "$TAIL_PID" >/dev/null 2>&1 || true
