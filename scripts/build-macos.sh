#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
mkdir -p bin
architecture="$(go env GOARCH)"
output="bin/albion-capture-macos-$architecture"
CGO_ENABLED=1 go build -trimpath -o "$output" ./cmd/albion-capture
echo "已建置：$project_root/$output"
