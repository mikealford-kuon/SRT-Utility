#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

./stop-local.sh || true
DETACH="${DETACH:-0}" ./start-local.sh
