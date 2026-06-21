#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m unittest discover -s tests

if sandbox-exec -p $'(version 1)\n(allow default)\n' /usr/bin/true >/dev/null 2>&1; then
  python3 -m unittest tests/test_macbox_integration.py
else
  echo "Skipping sandbox-exec integration test: sandbox-exec is unavailable in this environment." >&2
fi

swift build
