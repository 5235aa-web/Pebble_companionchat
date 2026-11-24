#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "========================================"
echo "  Pebble Companion Chat - Start All"
echo "========================================"
echo

# Prefer an active or local virtual environment, then system python3/python
PYTHON_BIN=""
if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ]; then
  PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
elif [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
elif [ -x "${ROOT_DIR}/venv/bin/python" ]; then
  PYTHON_BIN="${ROOT_DIR}/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "[Error] python3 / python not found in PATH."
  exit 1
fi

if ! "$PYTHON_BIN" -c "import uvicorn" >/dev/null 2>&1; then
  echo "[Error] uvicorn is not installed for: $PYTHON_BIN"
  echo "        Run: $PYTHON_BIN -m pip install -r requirements.txt"
  exit 1
fi

BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "Stopping services..."
  if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then
    kill -TERM "$FRONTEND_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    kill -TERM "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
  wait >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT

echo "[1/2] Starting backend (port 8002) via $PYTHON_BIN ..."
"$PYTHON_BIN" -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8002 &
BACKEND_PID=$!

echo "[2/2] Starting frontendV2 dev server (port 5176)..."
if [ -d "${ROOT_DIR}/frontendV2/node_modules" ]; then
  if command -v node >/dev/null 2>&1 && [ -f "${ROOT_DIR}/frontendV2/node_modules/vite/bin/vite.js" ]; then
    (cd "${ROOT_DIR}/frontendV2" && node node_modules/vite/bin/vite.js --port 5176) &
    FRONTEND_PID=$!
  elif command -v npm >/dev/null 2>&1; then
    (cd "${ROOT_DIR}/frontendV2" && npm run dev) &
    FRONTEND_PID=$!
  else
    echo "  [Skip] node/npm not found in PATH."
  fi
else
  echo "  [Skip] frontendV2/node_modules missing. Run: cd frontendV2 && npm install"
fi

echo
echo "========================================"
echo "  Backend : http://localhost:8002"
if [ -n "$FRONTEND_PID" ]; then
  echo "  Frontend: http://localhost:5176"
fi
echo "  Press Ctrl+C to stop both services."
echo "========================================"
echo

# Wait until either process exits (portable across macOS bash 3.2 and Linux bash)
while true; do
  if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    wait "$BACKEND_PID" || true
    break
  fi
  if [ -n "$FRONTEND_PID" ] && ! kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then
    wait "$FRONTEND_PID" || true
    break
  fi
  sleep 1
done
