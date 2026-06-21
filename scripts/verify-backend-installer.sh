#!/usr/bin/env bash
set -euo pipefail

python3 -m unittest \
  tests.test_macbox.MacBoxTests.test_backend_status_declares_arbitrary_paths_not_ready \
  tests.test_macbox.MacBoxTests.test_backend_doctor_reports_missing_macfuse_as_blocking \
  tests.test_macbox.MacBoxTests.test_backend_doctor_reports_implementation_gap_after_dependencies \
  tests.test_macbox.MacBoxTests.test_backend_install_plan_prefers_brew_when_requested \
  tests.test_macbox.MacBoxTests.test_backend_install_execute_requires_explicit_brew \
  tests.test_macbox.MacBoxTests.test_backend_install_rejects_open_and_execute_together \
  tests.test_macbox.MacBoxTests.test_backend_install_rejects_json_with_action_flags \
  tests.test_macbox.MacBoxTests.test_backend_install_execute_runs_brew_when_explicit

./macbox backend status --json >/tmp/macbox-backend-status.json
./macbox backend install --backend macfuse --dry-run >/tmp/macbox-backend-install.txt
./macbox backend install --backend macfuse --json >/tmp/macbox-backend-install.json
if ./macbox backend doctor --json >/tmp/macbox-backend-doctor.json; then
  echo "Backend doctor reports ready."
else
  echo "Backend doctor reports blockers; this is expected until dependencies and mounted overlay implementation are ready."
fi

python3 - <<'PY'
import json
from pathlib import Path

status = json.loads(Path("/tmp/macbox-backend-status.json").read_text())
assert status["productionBackend"] == "fuse"
assert status["arbitraryVirtualPaths"]["required"] is True
assert status["arbitraryVirtualPaths"]["ready"] is False

plan = json.loads(Path("/tmp/macbox-backend-install.json").read_text())
assert plan["backend"] == "macfuse"
assert plan["backendReady"] is False
assert "macfuseInstalled" in plan

doctor = json.loads(Path("/tmp/macbox-backend-doctor.json").read_text())
assert "nextActions" in doctor
PY

echo "Backend installer verification passed."
