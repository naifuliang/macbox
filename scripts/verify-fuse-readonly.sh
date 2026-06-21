#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m unittest \
  tests.test_macbox.MacBoxTests.test_macfuse_status_shape \
  tests.test_macbox.MacBoxTests.test_fuse_readonly_operations_read_real_absolute_paths \
  tests.test_macbox.MacBoxTests.test_fuse_backend_mount_starts_helper_and_records_metadata
./macbox fuse-status --json || true

python3 - <<'PY'
import json
import subprocess
import sys
import tempfile
from pathlib import Path

result = subprocess.run(["./macbox", "fuse-status", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
status = json.loads(result.stdout)
if not status["available"]:
    print("macFUSE unavailable; real read-only mount verification is not runnable on this host.")
    sys.exit(0)
if not status["pythonBinding"]:
    print("macFUSE detected, but Python FUSE binding is unavailable; read-only mount implementation is disabled.")
    sys.exit(0)

with tempfile.TemporaryDirectory(prefix="macbox-fuse-") as td:
    mount = Path(td) / "mnt"
    real = Path(td) / "real.txt"
    real.write_text("readonly")
    session = "verify-readonly"
    try:
        subprocess.run(["./macbox", "mount", "--backend", "fuse", "--name", session, "--mount", str(mount)], check=True)
        virtual = mount / str(real.resolve(strict=False)).lstrip("/")
        if virtual.read_text() != "readonly":
            raise SystemExit(f"read-through failed: {virtual}")
    finally:
        subprocess.run(["./macbox", "unmount", "--name", session], check=False)
PY
