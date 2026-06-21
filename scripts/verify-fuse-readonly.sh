#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m unittest tests.test_macbox.MacBoxTests.test_macfuse_status_shape
./macbox fuse-status --json || true

python3 - <<'PY'
import json
import subprocess
import sys

result = subprocess.run(["./macbox", "fuse-status", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
status = json.loads(result.stdout)
if not status["available"]:
    print("macFUSE unavailable; read-only mount verification is not runnable on this host.")
    sys.exit(0)
if not status["pythonBinding"]:
    print("macFUSE detected, but Python FUSE binding is unavailable; read-only mount implementation is disabled.")
    sys.exit(0)

raise SystemExit("macFUSE and Python binding are available, but read-only mount implementation is not complete yet.")
PY
