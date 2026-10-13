#!/usr/bin/env bash
# Скачивает TLA+ Tools (SANY + TLC) в tools/tla2tools.jar
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)/tools"
mkdir -p "$DIR"
URL="${TLA_URL:-https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar}"
curl -fsSL -o "$DIR/tla2tools.jar" "$URL"
echo "OK: $DIR/tla2tools.jar"
