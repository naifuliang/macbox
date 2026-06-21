#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m unittest \
  tests.test_macbox.MacBoxTests.test_docker_status_reports_cli_without_daemon \
  tests.test_macbox.MacBoxTests.test_docker_install_plan_is_guide_only \
  tests.test_macbox.MacBoxTests.test_backend_status_includes_container_backend \
  tests.test_macbox.MacBoxTests.test_docker_backend_prepare_shell_builds_isolated_run_command \
  tests.test_macbox.MacBoxTests.test_docker_runner_stages_changes_without_real_write \
  tests.test_macbox.MacBoxTests.test_docker_apply_refuses_external_host_changes \
  tests.test_macbox.MacBoxTests.test_docker_apply_requires_baseline_for_staged_paths

status="$(./macbox docker-status --json || true)"
python3 - "$status" <<'PY'
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

status = json.loads(sys.argv[1])
if not (status.get("available") and status.get("daemon")):
    print("Docker daemon unavailable; real Docker run verification is not runnable on this host.")
    print("Start Docker Desktop/OrbStack and rerun this script for the end-to-end check.")
    raise SystemExit(0)

name = f"docker-verify-{uuid.uuid4().hex[:8]}"
with tempfile.TemporaryDirectory(prefix="macbox-docker-real-") as td:
    root = Path(td)
    real = root / "created.txt"
    result = subprocess.run(
        [
            "./macbox",
            "run",
            "--backend",
            "docker",
            "--name",
            name,
            "--write",
            str(root),
            "--",
            "python",
            "-c",
            "from pathlib import Path; Path('created.txt').write_text('docker')",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(result.returncode)
    if real.exists():
        raise SystemExit(f"real file should not be written: {real}")
    changes = subprocess.run(["./macbox", "changes", "--name", name, "--json"], check=True, text=True, stdout=subprocess.PIPE)
    payload = json.loads(changes.stdout)
    if not any(item["realPath"] == str(real.resolve(strict=False)) for item in payload):
        raise SystemExit("created file was not reported as a pending Docker change")
    print("Docker backend real run verification passed.")
PY
