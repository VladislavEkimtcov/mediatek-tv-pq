#!/usr/bin/env bash
# Read, inspect, and monitor MediaTek Smart TV Picture Quality (PQ) settings.
# Works over ADB with no root required.
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

exec python3 "$ROOT/picture.py" "$@"
