#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONT_DIR="${ROOT_DIR}/frontendV2"

echo "========================================"
echo "  Pebble Companion Chat - Frontend V2"
echo "========================================"
echo

if [ ! -d "${FRONT_DIR}/node_modules" ]; then
  echo "  [Missing deps] frontendV2/node_modules not found."
  echo
  echo "  Please install dependencies first:"
  echo "    cd \"${FRONT_DIR}\""
  echo "    npm install"
  exit 1
fi

cd "$FRONT_DIR"
if command -v node >/dev/null 2>&1 && [ -f "node_modules/vite/bin/vite.js" ]; then
  exec node node_modules/vite/bin/vite.js --port 5176
elif command -v npm >/dev/null 2>&1; then
  exec npm run dev
else
  echo "  [Error] node / npm not found in PATH."
  exit 1
fi
