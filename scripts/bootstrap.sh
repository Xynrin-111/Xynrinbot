#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "提示：建议直接运行 bash scripts/run.sh"
exec bash scripts/run.sh --bootstrap-only
