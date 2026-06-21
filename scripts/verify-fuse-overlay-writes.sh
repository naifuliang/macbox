#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m unittest \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_create_write_stages_without_real_write \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_failed_write_open_does_not_leave_copy_up \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_mkdir_can_recreate_tombstoned_path \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_recreated_tombstoned_entries_can_be_deleted_again \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_read_prefers_staged_file \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_unlink_marks_tombstone_and_hides_real_file \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_deleted_directory_hides_children \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_rejects_unsafe_directory_deletes_and_existing_mkdir \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_rmdir_uses_virtual_children_after_tombstones \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_rename_stages_new_path_and_tombstones_old_path \
  tests.test_macbox.MacBoxTests.test_fuse_overlay_write_to_symlink_does_not_modify_real_target

python3 - <<'PY'
import json
import subprocess
import sys
import tempfile
from pathlib import Path

status_result = subprocess.run(["./macbox", "fuse-status", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
status = json.loads(status_result.stdout)
if not status["available"]:
    print("macFUSE unavailable; real overlay write mount verification is not runnable on this host.")
    sys.exit(0)
if not status["pythonBinding"]:
    print("macFUSE detected, but Python FUSE binding is unavailable; real overlay write verification is disabled.")
    sys.exit(0)

with tempfile.TemporaryDirectory(prefix="macbox-fuse-overlay-") as td:
    mount = Path(td) / "mnt"
    real = Path(td) / "real.txt"
    real.write_text("real")
    session = "verify-overlay"
    try:
        subprocess.run(["./macbox", "mount", "--backend", "fuse", "--name", session, "--mount", str(mount)], check=True)
        virtual = mount / str(real.resolve(strict=False)).lstrip("/")
        virtual.write_text("virtual")
        if real.read_text() != "real":
            raise SystemExit("real file was modified before apply")
        changes = subprocess.run(["./macbox", "changes", "--name", session, "--json"], check=True, text=True, stdout=subprocess.PIPE)
        data = json.loads(changes.stdout)
        if not any(item["realPath"] == str(real.resolve(strict=False)) for item in data):
            raise SystemExit("overlay write was not reported by changes")
    finally:
        subprocess.run(["./macbox", "unmount", "--name", session], check=False)
PY

echo "FUSE overlay write verification passed."
