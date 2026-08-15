#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
  echo "沿用已啟動的統計服務。"
else
  python3 -m albion_tracker serve &
  server_pid=$!
fi
sleep 1
echo "統計頁面：http://127.0.0.1:8765"
sudo "$project_root/bin/albion-capture-linux"
