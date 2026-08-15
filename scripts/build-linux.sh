#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
mkdir -p bin
CGO_ENABLED=1 go build -trimpath -o bin/albion-capture-linux ./cmd/albion-capture
echo "已建置：$project_root/bin/albion-capture-linux"
