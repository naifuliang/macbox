#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(".macbox")
SANDBOX_DIR = APP_DIR / "sessions"


def project_root() -> Path:
    return Path(__file__).resolve().parent


def sandbox_root(name: str) -> Path:
    return project_root() / SANDBOX_DIR / name


def overlay_root(name: str) -> Path:
    return sandbox_root(name) / "overlay"


def profile_path(name: str) -> Path:
    return sandbox_root(name) / "profile.sb"


def interpose_source_path() -> Path:
    return project_root() / "macbox_interpose.c"


def interpose_library_path() -> Path:
    return project_root() / ".macbox" / "libmacbox_interpose.dylib"


def mount_record_path(name: str) -> Path:
    return sandbox_root(name) / "mount.json"


def fuse_helper_path() -> Path:
    return project_root() / "macbox_fuse.py"


def wait_for_mount(mount: Path, proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if os.path.ismount(mount):
            return True
        time.sleep(0.1)
    return False


def rcfile_path(name: str) -> Path:
    return sandbox_root(name) / "shellrc"


def deletes_path(name: str) -> Path:
    return sandbox_root(name) / "deletes.txt"


def docker_runner_path(name: str) -> Path:
    return sandbox_root(name) / "docker_runner.py"


def docker_baseline_path(name: str) -> Path:
    return sandbox_root(name) / "docker_baseline.json"


def config_path(name: str) -> Path:
    return sandbox_root(name) / "config.env"


def metadata_path(name: str) -> Path:
    return sandbox_root(name) / "metadata.json"


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def quote_sb(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def normalize_abs(path: str) -> Path:
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return Path(expanded).resolve(strict=False)


def virtual_path(name: str, real_path: str) -> Path:
    real = normalize_abs(real_path)
    rel = str(real).lstrip("/")
    return overlay_root(name) / rel


def ensure_session_storage(name: str) -> None:
    root = sandbox_root(name)
    for sub in ("overlay", "tmp", "home", "cache", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    if not deletes_path(name).exists():
        deletes_path(name).write_text("")


def should_virtualize_path(path: str) -> bool:
    if not path:
        return False
    if path.startswith("-"):
        return False
    if "://" in path:
        return False
    return True


def macfuse_status() -> dict:
    filesystem = Path("/Library/Filesystems/macfuse.fs")
    framework = Path("/Library/Frameworks/macFUSE.framework")
    mount_command = shutil.which("mount_macfuse") or shutil.which("mount_osxfuse")
    try:
        __import__("fuse")
        python_binding = True
    except Exception:
        python_binding = False
    available = filesystem.exists() or framework.exists() or bool(mount_command)
    return {
        "available": available,
        "filesystem": str(filesystem) if filesystem.exists() else None,
        "framework": str(framework) if framework.exists() else None,
        "mountCommand": mount_command,
        "pythonBinding": python_binding,
    }


def docker_status() -> dict:
    docker = shutil.which("docker")
    if docker and Path(docker).name != "docker":
        docker = None
    status = {
        "available": bool(docker),
        "path": docker,
        "daemon": False,
        "serverVersion": None,
        "context": None,
        "error": None,
    }
    if not docker:
        return status
    try:
        result = subprocess.run(
            [docker, "version", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        status["error"] = str(exc)
        return status
    if result.returncode != 0:
        status["error"] = (result.stderr or result.stdout or "").strip()
        return status
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    server = payload.get("Server") if isinstance(payload, dict) else None
    client = payload.get("Client") if isinstance(payload, dict) else None
    status["daemon"] = bool(server)
    if isinstance(server, dict):
        status["serverVersion"] = server.get("Version")
    if isinstance(client, dict):
        status["context"] = client.get("Context")
    return status


def backend_status() -> dict:
    macfuse = macfuse_status()
    docker = docker_status()
    return {
        "defaultBackend": "prototype",
        "productionBackend": "fuse",
        "containerBackend": "docker",
        "arbitraryVirtualPaths": {
            "required": True,
            "ready": False,
            "backend": "fuse",
            "readOnlyMountImplemented": True,
            "writeOverlayImplemented": True,
            "sessionExecutionImplemented": False,
            "note": "MacBox production sandboxing requires sessions and apps to launch from the mounted FUSE backend; workspace-only backends are not the mainline.",
        },
        "containerSandbox": {
            "backend": "docker",
            "ready": docker["available"] and docker["daemon"],
            "sessionExecutionImplemented": True,
            "hostMode": "selected roots copied into an isolated container workspace",
            "note": "Docker mode is isolated and useful for CLI/dev workflows, but it is not a macOS-native arbitrary-path filesystem.",
        },
        "macfuse": macfuse,
        "docker": docker,
        "homebrew": {
            "available": shutil.which("brew") is not None,
            "path": shutil.which("brew"),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "macVersion": platform.mac_ver()[0],
        },
    }


def backend_install_plan(backend: str = "macfuse", use_brew: bool = False) -> dict:
    if backend not in ("macfuse", "docker"):
        raise SystemExit(f"unsupported backend installer: {backend}")
    if backend == "docker":
        status = docker_status()
        return {
            "backend": backend,
            "backendReady": bool(status["available"] and status["daemon"]),
            "dockerInstalled": bool(status["available"]),
            "dockerDaemonRunning": bool(status["daemon"]),
            "requiresAdminApproval": False,
            "requiresSystemExtensionApproval": False,
            "guideUrl": "https://docs.docker.com/desktop/install/mac-install/",
            "steps": [
                {
                    "type": "open",
                    "url": "https://docs.docker.com/desktop/install/mac-install/",
                    "note": "Install Docker Desktop, OrbStack, or another Docker-compatible runtime.",
                },
                {
                    "type": "verify",
                    "command": "./macbox docker-status",
                    "note": "Re-run after starting the Docker daemon.",
                },
                {
                    "type": "verify",
                    "command": "./scripts/verify-docker-backend.sh",
                    "note": "Run the Docker backend acceptance check.",
                },
            ],
        }
    brew = shutil.which("brew")
    steps: list[dict[str, str]] = []
    if use_brew and brew:
        steps.append({
            "type": "command",
            "command": f"{brew} install --cask macfuse",
            "note": "Runs the macFUSE cask installer. macOS may ask for administrator approval and a reboot.",
        })
    else:
        steps.append({
            "type": "open",
            "url": "https://macfuse.github.io/",
            "note": "Download and run the official macFUSE installer. Approve the system extension when macOS asks.",
        })
        if brew:
            steps.append({
                "type": "optional-command",
                "command": f"{brew} install --cask macfuse",
                "note": "Alternative Homebrew install path. Use --use-brew --execute to run it from MacBox.",
            })
    steps.append({
        "type": "verify",
        "command": "./macbox backend doctor",
        "note": "Re-run after installation to confirm macFUSE is visible to MacBox.",
    })
    return {
        "backend": backend,
        "backendReady": False,
        "macfuseInstalled": macfuse_status()["available"],
        "requiresAdminApproval": True,
        "requiresSystemExtensionApproval": True,
        "guideUrl": "https://macfuse.github.io/",
        "steps": steps,
    }


def backend_doctor_report() -> dict:
    status = backend_status()
    macfuse = status["macfuse"]
    checks = [
        {
            "id": "arbitrary-virtual-paths",
            "ok": False,
            "severity": "blocking",
            "message": "Production arbitrary-path virtual writes require session execution from the mounted backend.",
        },
        {
            "id": "macfuse-installed",
            "ok": bool(macfuse["available"]),
            "severity": "blocking",
            "message": "macFUSE is installed" if macfuse["available"] else "macFUSE is not installed or not visible.",
        },
        {
            "id": "macfuse-mount-command",
            "ok": bool(macfuse["mountCommand"]),
            "severity": "info",
            "message": f"mount command: {macfuse['mountCommand']}" if macfuse["mountCommand"] else "mount_macfuse/mount_osxfuse not found in PATH; fusepy may still mount through the macFUSE framework.",
        },
        {
            "id": "python-fuse-binding",
            "ok": bool(macfuse["pythonBinding"]),
            "severity": "blocking",
            "message": "Python FUSE binding is available" if macfuse["pythonBinding"] else "Python FUSE binding is not available in this interpreter.",
        },
        {
            "id": "homebrew",
            "ok": bool(status["homebrew"]["available"]),
            "severity": "info",
            "message": f"Homebrew: {status['homebrew']['path']}" if status["homebrew"]["available"] else "Homebrew not found; official installer flow is still supported.",
        },
        {
            "id": "docker-cli",
            "ok": bool(status["docker"]["available"]),
            "severity": "info",
            "message": f"Docker CLI: {status['docker']['path']}" if status["docker"]["available"] else "Docker CLI not found; Docker backend is unavailable.",
        },
        {
            "id": "docker-daemon",
            "ok": bool(status["docker"]["daemon"]),
            "severity": "info",
            "message": "Docker daemon is running" if status["docker"]["daemon"] else "Docker daemon is not running; Docker backend can be installed but not executed yet.",
        },
    ]
    blocking = [check for check in checks if check["severity"] == "blocking" and not check["ok"]]
    next_actions: list[str] = []
    if any(check["id"] == "macfuse-installed" for check in blocking):
        next_actions.append("Install macFUSE with './macbox backend install --backend macfuse --open' or '--use-brew --execute'.")
    if any(check["id"] == "python-fuse-binding" for check in blocking):
        next_actions.append("Install a Python FUSE binding for the interpreter that runs MacBox.")
    if any(check["id"] == "arbitrary-virtual-paths" for check in blocking):
        next_actions.append("Wire shell and app session execution through the mounted FUSE backend.")
    if not next_actions:
        next_actions.append("Backend dependencies are present; continue mounted overlay verification.")
    return {
        "ready": not blocking,
        "checks": checks,
        "status": status,
        "nextActions": next_actions,
        "nextAction": " ".join(next_actions),
        "installPlan": backend_install_plan("macfuse"),
    }


def read_config(name: str) -> dict[str, list[str]]:
    cfg = {"read": [], "write": ["/"]}
    path = config_path(name)
    if not path.exists():
        return cfg
    for line in path.read_text().splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in cfg:
            cfg[key] = [p for p in value.split(os.pathsep) if p]
    return cfg


def write_config(name: str, reads: list[str], writes: list[str]) -> None:
    root = sandbox_root(name)
    root.mkdir(parents=True, exist_ok=True)
    normalized_reads = [str(normalize_abs(p)) for p in reads]
    normalized_writes = [str(normalize_abs(p)) for p in writes] or ["/"]
    config_path(name).write_text(
        "read=" + os.pathsep.join(normalized_reads) + "\n"
        "write=" + os.pathsep.join(normalized_writes) + "\n"
    )


def read_metadata(name: str) -> dict:
    path = metadata_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def write_metadata(name: str, **updates) -> None:
    root = sandbox_root(name)
    root.mkdir(parents=True, exist_ok=True)
    data = read_metadata(name)
    data.setdefault("id", name)
    data.setdefault("name", name)
    data.setdefault("createdAt", now_iso())
    data.update(updates)
    data["updatedAt"] = now_iso()
    metadata_path(name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def path_allowed(path: Path, allowed_roots: list[str]) -> bool:
    path_s = str(path.resolve(strict=False))
    for root in allowed_roots:
        if root == "/":
            return True
        root_s = str(normalize_abs(root)).rstrip("/")
        if path_s == root_s or path_s.startswith(root_s + "/"):
            return True
    return False


def sandbox_profile(name: str) -> str:
    session = sandbox_root(name).resolve(strict=False)
    overlay = overlay_root(name).resolve(strict=False)
    temp = sandbox_root(name).resolve(strict=False) / "tmp"
    home = sandbox_root(name).resolve(strict=False) / "home"
    cache = sandbox_root(name).resolve(strict=False) / "cache"
    lines = [
        '(version 1)',
        '(allow default)',
        '',
        '; Real disk is readable by default, but writes are blocked.',
        '; Writes are allowed only inside the MacBox overlay and process temp dirs.',
        f'(allow file-write* (subpath "{quote_sb(session)}"))',
        f'(allow file-write* (subpath "{quote_sb(overlay)}"))',
        f'(allow file-write* (subpath "{quote_sb(temp)}"))',
        f'(allow file-write* (subpath "{quote_sb(home)}"))',
        f'(allow file-write* (subpath "{quote_sb(cache)}"))',
        '(allow file-write* (subpath "/dev"))',
        '(deny file-write* (require-all',
        f'  (require-not (subpath "{quote_sb(session)}"))',
        f'  (require-not (subpath "{quote_sb(overlay)}"))',
        f'  (require-not (subpath "{quote_sb(temp)}"))',
        f'  (require-not (subpath "{quote_sb(home)}"))',
        f'  (require-not (subpath "{quote_sb(cache)}"))',
        '  (require-not (subpath "/dev"))))',
    ]
    return "\n".join(lines) + "\n"


def ensure_interpose_library() -> Path | None:
    src = interpose_source_path()
    dst = interpose_library_path()
    if not src.exists():
        return None
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "clang",
        "-dynamiclib",
        "-O2",
        "-Wall",
        "-Wextra",
        "-arch",
        "arm64",
        "-arch",
        "arm64e",
        "-o",
        str(dst),
        str(src),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        message = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"warning: transparent path mapping disabled; failed to build interpose library: {message}", file=sys.stderr)
        return None
    return dst


def ensure_sandbox(name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> None:
    root = sandbox_root(name)
    for sub in ("overlay", "tmp", "home", "cache"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    if not config_path(name).exists() or reads is not None or writes is not None:
        write_config(name, reads or [], writes or ["/"])
    profile_path(name).write_text(sandbox_profile(name))
    rcfile_path(name).write_text(shell_rc(name))
    deletes_path(name).touch(exist_ok=True)
    cfg = read_config(name)
    write_metadata(
        name,
        sandboxed=True,
        status=read_metadata(name).get("status", "idle"),
        readRoots=cfg["read"],
        writeRoots=cfg["write"],
        overlayPath=str(overlay_root(name)),
    )


def sandbox_environment(name: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "MACBOX_NAME": name,
        "MB_ROOT": str(overlay_root(name)),
        "TMPDIR": str(sandbox_root(name) / "tmp"),
        "HOME": str(sandbox_root(name) / "home"),
        "XDG_CACHE_HOME": str(sandbox_root(name) / "cache"),
        "MACBOX_SANDBOX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SHELL_SESSIONS_DISABLE": "1",
    })
    dylib = ensure_interpose_library()
    if dylib:
        existing = env.get("DYLD_INSERT_LIBRARIES")
        env["DYLD_INSERT_LIBRARIES"] = str(dylib) if not existing else f"{dylib}:{existing}"
        env["DYLD_FORCE_FLAT_NAMESPACE"] = "1"
        env["MACBOX_TRANSPARENT_OVERLAY"] = "1"
    return env


def shell_rc(name: str) -> str:
    exe = project_root() / "macbox"
    py = quote_sb(sys.executable)
    cli = quote_sb(project_root() / "macbox_cli.py")
    macbox_cmd = f'"{py}" "{cli}"'
    return f"""export MACBOX_NAME="{name}"
export MB_ROOT="{overlay_root(name)}"
export TMPDIR="{sandbox_root(name) / 'tmp'}"
export HOME="{sandbox_root(name) / 'home'}"
export XDG_CACHE_HOME="{sandbox_root(name) / 'cache'}"
export MACBOX_SANDBOX=1
export PYTHONDONTWRITEBYTECODE=1
export SHELL_SESSIONS_DISABLE=1
unset HISTFILE
export SAVEHIST=0
export PS1="macbox:{name} \\w $ "
export PROMPT="%F{{cyan}}macbox:{name}%f %1~ %# "
alias mb-changes='{macbox_cmd} changes --name "{name}"'
alias mb-apply='{macbox_cmd} apply --name "{name}"'
alias mb-delete='{macbox_cmd} delete --name "{name}"'
vpath() {{ {macbox_cmd} path --name "{name}" --mkdir "$@"; }}
_macbox_path() {{ {macbox_cmd} path --name "{name}" --mkdir "$@"; }}
_macbox_dir_path() {{ {macbox_cmd} path --name "{name}" --mkdir "$@"; }}
mkdir() {{
  local args=()
  local paths=()
  local expect_value=0
  for arg in "$@"; do
    if (( expect_value )); then
      args+=("$arg")
      expect_value=0
    elif [[ "$arg" == "--" ]]; then
      args+=("$arg")
    elif [[ "$arg" == "-m" ]]; then
      args+=("$arg")
      expect_value=1
    elif [[ "$arg" == -* ]]; then
      args+=("$arg")
    else
      paths+=("$(_macbox_dir_path "$arg")")
    fi
  done
  command mkdir "${{args[@]}}" "${{paths[@]}}"
}}
touch() {{
  local args=()
  local paths=()
  for arg in "$@"; do
    if [[ "$arg" == -* ]]; then
      args+=("$arg")
    else
      paths+=("$(_macbox_path "$arg")")
    fi
  done
  command touch "${{args[@]}}" "${{paths[@]}}"
}}
cat() {{
  local args=()
  local paths=()
  for arg in "$@"; do
    if [[ "$arg" == -* ]]; then
      args+=("$arg")
    else
      local mapped="$({macbox_cmd} path --name "{name}" "$arg")"
      if [[ -e "$mapped" ]]; then
        paths+=("$mapped")
      else
        paths+=("$arg")
      fi
    fi
  done
  command cat "${{args[@]}}" "${{paths[@]}}"
}}
macbox-rewrite-line() {{
  BUFFER=$({macbox_cmd} rewrite --name "{name}" -- "$BUFFER")
  zle accept-line
}}
zle -N macbox-rewrite-line
bindkey '^M' macbox-rewrite-line
bindkey '^J' macbox-rewrite-line
echo "MacBox sandbox: {name}"
echo "Real disk is readable. Writes are redirected into: $MB_ROOT"
echo "Use: mb-changes  |  mb-apply  |  vpath /real/path"
"""


def rewrite_shell_line(name: str, line: str) -> str:
    def mapped_path(path: str) -> str:
        mapped = virtual_path(name, path.replace(r"\ ", " "))
        return str(mapped)

    result: list[str] = []
    mkdir_parents: list[str] = []

    def note_parent(path: str) -> None:
        parent = str(Path(path).parent)
        if parent not in mkdir_parents:
            mkdir_parents.append(parent)

    i = 0
    quote: str | None = None
    while i < len(line):
        char = line[i]
        if quote:
            result.append(char)
            if char == quote:
                quote = None
            elif char == "\\" and i + 1 < len(line):
                i += 1
                result.append(line[i])
            i += 1
            continue

        if char in ("'", '"'):
            quote = char
            result.append(char)
            i += 1
            continue

        op_start = i
        if char.isdigit():
            j = i
            while j < len(line) and line[j].isdigit():
                j += 1
            if j < len(line) and line[j] in ("<", ">"):
                i = j
            else:
                result.append(char)
                i += 1
                continue
        elif char == "&" and line.startswith("&>>", i):
            i += 1
        elif char not in ("<", ">"):
            result.append(char)
            i += 1
            continue

        if line.startswith(">>", i):
            i += 2
        elif i < len(line) and line[i] in ("<", ">"):
            i += 1
        else:
            result.append(line[op_start])
            i = op_start + 1
            continue

        op_text = line[op_start:i]
        is_write_redirect = ">" in op_text
        result.append(op_text)
        while i < len(line) and line[i].isspace():
            result.append(line[i])
            i += 1

        if i >= len(line):
            continue

        if line[i] in ("'", '"'):
            path_quote = line[i]
            path_start = i + 1
            j = path_start
            while j < len(line) and line[j] != path_quote:
                if line[j] == "\\" and j + 1 < len(line):
                    j += 2
                else:
                    j += 1
            path = line[path_start:j]
            if should_virtualize_path(path):
                mapped = mapped_path(path)
                if is_write_redirect:
                    note_parent(mapped)
                result.append(f'"{mapped}"')
            else:
                result.append(line[i:j + 1])
            i = min(j + 1, len(line))
            continue

        path_start = i
        while i < len(line) and not line[i].isspace() and line[i] not in ";&|<>":
            i += 1
        path = line[path_start:i]
        if should_virtualize_path(path):
            mapped = mapped_path(path)
            if is_write_redirect:
                note_parent(mapped)
            result.append(f'"{mapped}"')
        else:
            result.append(path)

    rewritten = "".join(result)
    if mkdir_parents:
        parents = " ".join(shlex.quote(parent) for parent in mkdir_parents)
        return f"command mkdir -p -- {parents}; {rewritten}"
    return rewritten


class SandboxBackend:
    name = "backend"

    def create(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None, plain: bool = False) -> str:
        raise NotImplementedError

    def ensure(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> None:
        raise NotImplementedError

    def real_to_virtual(self, name: str, real_path: str) -> Path:
        raise NotImplementedError

    def prepare_virtual_path(self, name: str, real_path: str, mkdir: bool = False, directory: bool = False) -> Path:
        raise NotImplementedError

    def prepare_shell(self, name: str, command: list[str], stdin_data: str | None = None) -> "LaunchSpec":
        raise NotImplementedError

    def prepare_app(self, name: str, executable: Path, args: list[str]) -> "LaunchSpec":
        raise NotImplementedError

    def open_terminal_command(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> str:
        raise NotImplementedError

    def list_changes(self, name: str) -> list[dict]:
        raise NotImplementedError

    def list_sessions(self) -> list[dict]:
        raise NotImplementedError

    def environment(self, name: str) -> dict[str, str]:
        raise NotImplementedError

    def rewrite_line(self, name: str, line: str) -> str:
        raise NotImplementedError

    def apply(self, name: str, clear: bool = False) -> tuple[int, Path | None]:
        raise NotImplementedError

    def discard(self, name: str) -> None:
        raise NotImplementedError

    def mark_delete(self, name: str, real_path: str) -> Path:
        raise NotImplementedError


class PrototypeBackend(SandboxBackend):
    name = "prototype"

    def create(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None, plain: bool = False) -> str:
        if plain:
            root = sandbox_root(name)
            root.mkdir(parents=True, exist_ok=True)
            write_metadata(name, sandboxed=False, status="idle", readRoots=[], writeRoots=[], overlayPath=None, backend=self.name)
            return name
        self.ensure(name, reads, writes)
        return name

    def ensure(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> None:
        ensure_sandbox(name, reads, writes)
        write_metadata(name, backend=self.name)

    def real_to_virtual(self, name: str, real_path: str) -> Path:
        return virtual_path(name, real_path)

    def prepare_virtual_path(self, name: str, real_path: str, mkdir: bool = False, directory: bool = False) -> Path:
        self.ensure(name)
        vp = self.real_to_virtual(name, real_path)
        if mkdir:
            if directory:
                vp.mkdir(parents=True, exist_ok=True)
            else:
                vp.parent.mkdir(parents=True, exist_ok=True)
        return vp

    def prepare_shell(self, name: str, command: list[str], stdin_data: str | None = None) -> "LaunchSpec":
        self.ensure(name)
        rewritten_stdin = None
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            shell = os.environ.get("SHELL", "/bin/zsh")
            command = [shell, "-i"]
            if Path(shell).name in ("bash", "zsh"):
                command = [shell, "-i"] if Path(shell).name == "zsh" else [shell, "--rcfile", str(rcfile_path(name)), "-i"]
            if stdin_data is not None:
                lines = stdin_data.splitlines(keepends=True)
                rewritten_stdin = "".join(
                    self.rewrite_line(name, line.removesuffix("\n").removesuffix("\r")) + ("\n" if line.endswith("\n") else "")
                    for line in lines
                )
        elif len(command) >= 3 and Path(command[0]).name == "zsh" and command[1] == "-lc":
            command = [command[0], command[1], self.rewrite_line(name, command[2]), *command[3:]]

        env = self.environment(name)
        if Path(env.get("SHELL", "/bin/zsh")).name == "zsh" and command and Path(command[0]).name == "zsh" and "-i" in command:
            zhome = sandbox_root(name) / "home"
            env["ZDOTDIR"] = str(zhome)
            (zhome / ".zshrc").write_text(rcfile_path(name).read_text())

        return LaunchSpec(
            argv=["sandbox-exec", "-f", str(profile_path(name)), *command],
            env=env,
            cwd=project_root(),
            stdin=rewritten_stdin,
            text=rewritten_stdin is not None,
            display_command=" ".join(command),
        )

    def prepare_app(self, name: str, executable: Path, args: list[str]) -> "LaunchSpec":
        self.ensure(name)
        return LaunchSpec(
            argv=["sandbox-exec", "-f", str(profile_path(name)), str(executable), *args],
            env=self.environment(name),
            cwd=project_root(),
            display_command=" ".join([str(executable), *args]),
        )

    def open_terminal_command(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> str:
        exe = project_root() / "macbox"
        meta = read_metadata(name)
        if meta.get("sandboxed") is False:
            sandbox_root(name).mkdir(parents=True, exist_ok=True)
            return f'cd "{quote_sb(project_root())}" && exec "${{SHELL:-/bin/zsh}}" -i'
        self.ensure(name, reads, writes)
        return f'cd "{quote_sb(project_root())}" && "{quote_sb(exe)}" session --name "{quote_sb(name)}"'

    def list_changes(self, name: str) -> list[dict]:
        return collect_changes(name)

    def list_sessions(self) -> list[dict]:
        return collect_sessions()

    def environment(self, name: str) -> dict[str, str]:
        return sandbox_environment(name)

    def rewrite_line(self, name: str, line: str) -> str:
        return rewrite_shell_line(name, line)

    def apply(self, name: str, clear: bool = False) -> tuple[int, Path | None]:
        self.ensure(name)
        cfg = read_config(name)
        entries = list(iter_overlay_entries(name) or [])
        deletes = [normalize_abs(p) for p in deletes_path(name).read_text().splitlines() if p]
        if not entries and not deletes:
            return 0, None
        backup = sandbox_root(name) / "backups" / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        applied = 0
        for target in deletes:
            if not path_allowed(target, cfg["write"]):
                raise SystemExit(f"refusing delete outside configured write roots: {target}")
            backup_existing(target, backup)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            applied += 1
        for src, real in entries:
            target = Path(str(real))
            if not path_allowed(target, cfg["write"]):
                raise SystemExit(f"refusing write outside configured write roots: {target}")
            backup_existing(target, backup)
            copy_entry(src, target)
            applied += 1
        if clear:
            self.discard(name)
        return applied, backup

    def discard(self, name: str) -> None:
        self.ensure(name)
        shutil.rmtree(overlay_root(name))
        overlay_root(name).mkdir(parents=True, exist_ok=True)
        deletes_path(name).write_text("")

    def mark_delete(self, name: str, real_path: str) -> Path:
        self.ensure(name)
        real = normalize_abs(real_path)
        with deletes_path(name).open("a") as fh:
            fh.write(str(real) + "\n")
        return real


class FuseBackend(SandboxBackend):
    name = "fuse"

    def status(self) -> dict:
        return macfuse_status()

    def require_available(self) -> dict:
        status = self.status()
        if not status["available"]:
            raise SystemExit(
                "macFUSE is not available. Install macFUSE before using the fuse backend: "
                "https://macfuse.github.io/"
            )
        if not status["pythonBinding"]:
            raise SystemExit(
                "macFUSE appears to be installed, but the Python FUSE binding is unavailable. "
                "The mounted FUSE overlay backend is disabled in this build."
            )
        return status

    def create(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None, plain: bool = False) -> str:
        if plain:
            raise SystemExit("plain sessions do not use the fuse backend")
        self.ensure(name, reads, writes)
        return name

    def ensure(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> None:
        root = sandbox_root(name)
        for sub in ("overlay", "tmp", "home", "cache", "mounts"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        if not config_path(name).exists() or reads is not None or writes is not None:
            write_config(name, reads or [], writes or ["/"])
        cfg = read_config(name)
        write_metadata(
            name,
            backend=self.name,
            sandboxed=True,
            status=read_metadata(name).get("status", "idle"),
            readRoots=cfg["read"],
            writeRoots=cfg["write"],
            overlayPath=str(overlay_root(name)),
            mountPath=read_metadata(name).get("mountPath"),
        )

    def mount_readonly(
        self,
        name: str,
        mount_path: str,
        reads: list[str] | None = None,
        writes: list[str] | None = None,
        foreground: bool = False,
    ) -> Path:
        self.require_available()
        self.ensure(name, reads, writes)
        mount = normalize_abs(mount_path)
        mount.mkdir(parents=True, exist_ok=True)
        helper = fuse_helper_path()
        if not helper.exists():
            raise SystemExit(f"missing FUSE helper: {helper}")
        command = [
            sys.executable,
            str(helper),
            "--session",
            name,
            "--mount",
            str(mount),
            "--overlay",
            str(overlay_root(name)),
            "--deletes",
            str(deletes_path(name)),
            "--foreground",
        ]
        if foreground:
            proc = subprocess.Popen(command, cwd=project_root())
            if not wait_for_mount(mount, proc):
                if proc.poll() is None:
                    proc.terminate()
                raise SystemExit(f"FUSE helper did not mount {mount}")
            write_metadata(
                name,
                backend=self.name,
                status="mounted",
                mountPath=str(mount),
                mountPid=proc.pid,
                mountStartedAt=now_iso(),
                readOnly=False,
            )
            mount_record_path(name).write_text(json.dumps({
                "backend": self.name,
                "mountPath": str(mount),
                "pid": proc.pid,
                "readOnly": False,
                "foreground": True,
                "startedAt": now_iso(),
                "command": command,
            }, indent=2) + "\n")
            returncode = proc.wait()
            write_metadata(name, status="idle", mountPath=None, mountPid=None, readOnly=None)
            record = mount_record_path(name)
            if record.exists():
                record.unlink()
            if returncode != 0:
                raise SystemExit(returncode)
        else:
            logs = sandbox_root(name) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            stdout = (logs / "fuse.out.log").open("a")
            stderr = (logs / "fuse.err.log").open("a")
            proc = subprocess.Popen(command, cwd=project_root(), stdout=stdout, stderr=stderr, text=True)
            stdout.close()
            stderr.close()
            if not wait_for_mount(mount, proc):
                message = logs / "fuse.err.log"
                code = proc.poll()
                if code is None:
                    proc.terminate()
                    raise SystemExit(f"FUSE helper did not mount {mount} before timeout. See {message}")
                raise SystemExit(f"FUSE helper exited early with {code}. See {message}")
            write_metadata(
                name,
                backend=self.name,
                status="mounted",
                mountPath=str(mount),
                mountPid=proc.pid,
                mountStartedAt=now_iso(),
                readOnly=False,
            )
            mount_record_path(name).write_text(json.dumps({
                "backend": self.name,
                "mountPath": str(mount),
                "pid": proc.pid,
                "readOnly": False,
                "startedAt": now_iso(),
                "command": command,
            }, indent=2) + "\n")
        return mount

    def unmount(self, name: str) -> None:
        data = read_metadata(name)
        mount = data.get("mountPath")
        if not mount:
            print(f"no recorded mount for session: {name}")
            return
        result = subprocess.run(["/sbin/umount", mount], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SystemExit(f"failed to unmount {mount}: {detail or result.returncode}")
        write_metadata(name, status="idle", mountPath=None, mountPid=None, readOnly=None)
        record = mount_record_path(name)
        if record.exists():
            record.unlink()

    def real_to_virtual(self, name: str, real_path: str) -> Path:
        data = read_metadata(name)
        mount = data.get("mountPath")
        if not mount:
            raise SystemExit(f"fuse session is not mounted: {name}")
        real = normalize_abs(real_path)
        return Path(mount) / str(real).lstrip("/")

    def prepare_virtual_path(self, name: str, real_path: str, mkdir: bool = False, directory: bool = False) -> Path:
        path = self.real_to_virtual(name, real_path)
        if mkdir:
            target = path if directory else path.parent
            target.mkdir(parents=True, exist_ok=True)
        return path

    def prepare_shell(self, name: str, command: list[str], stdin_data: str | None = None) -> "LaunchSpec":
        raise SystemExit("fuse backend shell launch requires a mounted session")

    def prepare_app(self, name: str, executable: Path, args: list[str]) -> "LaunchSpec":
        raise SystemExit("fuse backend app launch requires a mounted session")

    def open_terminal_command(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> str:
        raise SystemExit("fuse backend terminal launch requires a mounted session")

    def list_changes(self, name: str) -> list[dict]:
        return collect_changes(name)

    def list_sessions(self) -> list[dict]:
        return collect_sessions()

    def environment(self, name: str) -> dict[str, str]:
        return os.environ.copy()

    def rewrite_line(self, name: str, line: str) -> str:
        return line

    def apply(self, name: str, clear: bool = False) -> tuple[int, Path | None]:
        return PrototypeBackend().apply(name, clear)

    def discard(self, name: str) -> None:
        PrototypeBackend().discard(name)

    def mark_delete(self, name: str, real_path: str) -> Path:
        return PrototypeBackend().mark_delete(name, real_path)


DOCKER_RUNNER = r'''#!/usr/bin/env python3
import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def copy_root(src, dst):
    src = Path(src)
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)


def same_file(a, b):
    if not a.exists() or not b.exists():
        return False
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if a.is_dir() or b.is_dir():
        return a.is_dir() and b.is_dir()
    return filecmp.cmp(a, b, shallow=False)


def stage_path(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def describe_path(path):
    path = Path(path)
    if path.is_symlink():
        return {"type": "symlink", "target": os.readlink(path)}
    if path.is_dir():
        return {"type": "dir"}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"type": "file", "sha256": digest.hexdigest(), "size": path.stat().st_size}
    return {"type": "missing"}


def iter_paths(root):
    root = Path(root)
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        yield path


def sync_changes(host_root, base_root, work_root, overlay_root, deletes_file):
    host_root = Path(host_root)
    base_root = Path(base_root)
    work_root = Path(work_root)
    overlay_root = Path(overlay_root)
    deletes = Path(deletes_file)
    deletes.parent.mkdir(parents=True, exist_ok=True)
    deletes.touch(exist_ok=True)
    baseline = {}

    seen = set()
    for path in iter_paths(work_root):
        rel = path.relative_to(work_root)
        seen.add(rel)
        base = base_root / rel
        host = host_root / rel
        if same_file(path, base):
            continue
        baseline[str(host)] = describe_path(base)
        stage_path(path, overlay_root / str(host).lstrip("/"))

    for path in iter_paths(base_root):
        rel = path.relative_to(base_root)
        if rel in seen:
            continue
        host = host_root / rel
        baseline[str(host)] = describe_path(path)
        with deletes.open("a") as fh:
            fh.write(str(host) + "\n")
    return baseline


def main():
    roots = [Path(p).resolve(strict=False) for p in os.environ["MACBOX_DOCKER_ROOTS"].split(os.pathsep) if p]
    session_root = Path(os.environ.get("MACBOX_DOCKER_SESSION", "/macbox/session"))
    roots_base = Path(os.environ.get("MACBOX_DOCKER_ROOTS_BASE", "/macbox/roots"))
    work_base = Path(os.environ.get("MACBOX_DOCKER_WORK", "/macbox/work"))
    overlay = session_root / "overlay"
    deletes = session_root / "deletes.txt"
    baseline_file = session_root / "docker_baseline.json"
    command = sys.argv[1:] or [os.environ.get("SHELL", "/bin/sh")]
    work_base.mkdir(parents=True, exist_ok=True)

    for idx, root in enumerate(roots):
        copy_root(roots_base / str(idx), work_base / str(idx))

    cwd = work_base / "0"
    proc = subprocess.run(command, cwd=cwd)

    baseline = {}
    deletes.write_text("")
    for idx, root in enumerate(roots):
        baseline.update(sync_changes(root, roots_base / str(idx), work_base / str(idx), overlay, deletes))
    baseline_file.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
'''


class DockerBackend(SandboxBackend):
    name = "docker"

    def status(self) -> dict:
        return docker_status()

    def require_available(self) -> dict:
        status = self.status()
        if not status["available"]:
            raise SystemExit("Docker CLI is not available. Install Docker Desktop, OrbStack, or another compatible runtime.")
        if not status["daemon"]:
            detail = f": {status['error']}" if status.get("error") else ""
            raise SystemExit(f"Docker daemon is not running{detail}")
        return status

    def create(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None, plain: bool = False) -> str:
        if plain:
            raise SystemExit("plain sessions do not use the docker backend")
        self.ensure(name, reads, writes)
        return name

    def ensure(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> None:
        ensure_session_storage(name)
        if not config_path(name).exists() or reads is not None or writes is not None:
            roots = writes or reads or [str(project_root())]
            write_config(name, reads or roots, writes or roots)
        cfg = read_config(name)
        docker_runner_path(name).write_text(DOCKER_RUNNER)
        if not docker_baseline_path(name).exists():
            docker_baseline_path(name).write_text("{}\n")
        write_metadata(
            name,
            backend=self.name,
            sandboxed=True,
            status=read_metadata(name).get("status", "idle"),
            readRoots=cfg["read"],
            writeRoots=cfg["write"],
            overlayPath=str(overlay_root(name)),
            dockerImage=read_metadata(name).get("dockerImage", "python:3.12-slim"),
        )

    def roots(self, name: str) -> list[Path]:
        cfg = read_config(name)
        return [normalize_abs(root) for root in (cfg["write"] or cfg["read"] or [str(project_root())])]

    def real_to_virtual(self, name: str, real_path: str) -> Path:
        return virtual_path(name, real_path)

    def prepare_virtual_path(self, name: str, real_path: str, mkdir: bool = False, directory: bool = False) -> Path:
        self.ensure(name)
        path = self.real_to_virtual(name, real_path)
        if mkdir:
            target = path if directory else path.parent
            target.mkdir(parents=True, exist_ok=True)
        return path

    def prepare_shell(self, name: str, command: list[str], stdin_data: str | None = None) -> "LaunchSpec":
        self.ensure(name)
        self.require_available()
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            command = ["/bin/sh"]
        roots = self.roots(name)
        argv = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "-e",
            "MACBOX_DOCKER_ROOTS=" + os.pathsep.join(str(root) for root in roots),
            "-v",
            f"{docker_runner_path(name).resolve(strict=False)}:/macbox/session/docker_runner.py:ro",
            "-v",
            f"{overlay_root(name).resolve(strict=False)}:/macbox/session/overlay:rw",
            "-v",
            f"{deletes_path(name).resolve(strict=False)}:/macbox/session/deletes.txt:rw",
            "-v",
            f"{docker_baseline_path(name).resolve(strict=False)}:/macbox/session/docker_baseline.json:rw",
        ]
        for idx, root in enumerate(roots):
            if not root.exists() or not root.is_dir():
                raise SystemExit(f"docker backend root must be an existing directory: {root}")
            argv.extend(["-v", f"{root}:/macbox/roots/{idx}:ro"])
        image = read_metadata(name).get("dockerImage", "python:3.12-slim")
        argv.extend([image, "python", "/macbox/session/docker_runner.py", *command])
        return LaunchSpec(
            argv=argv,
            env=os.environ.copy(),
            cwd=project_root(),
            stdin=stdin_data,
            text=stdin_data is not None,
            display_command=" ".join(command),
        )

    def prepare_app(self, name: str, executable: Path, args: list[str]) -> "LaunchSpec":
        raise SystemExit("docker backend does not run macOS .app bundles")

    def open_terminal_command(self, name: str, reads: list[str] | None = None, writes: list[str] | None = None) -> str:
        exe = project_root() / "macbox"
        self.ensure(name, reads, writes)
        return f'cd "{quote_sb(project_root())}" && "{quote_sb(exe)}" session --backend docker --name "{quote_sb(name)}"'

    def list_changes(self, name: str) -> list[dict]:
        return collect_changes(name)

    def list_sessions(self) -> list[dict]:
        return collect_sessions()

    def environment(self, name: str) -> dict[str, str]:
        return os.environ.copy()

    def rewrite_line(self, name: str, line: str) -> str:
        return line

    def apply(self, name: str, clear: bool = False) -> tuple[int, Path | None]:
        self.ensure(name)
        try:
            baseline = json.loads(docker_baseline_path(name).read_text())
        except json.JSONDecodeError:
            raise SystemExit(f"invalid Docker baseline metadata: {docker_baseline_path(name)}")
        changes = collect_changes(name)
        missing = [item["realPath"] for item in changes if item["realPath"] not in baseline]
        if missing:
            raise SystemExit(f"refusing Docker apply without baseline for: {missing[0]}")
        for item in changes:
            real = normalize_abs(item["realPath"])
            expected = baseline[item["realPath"]]
            if not baseline_matches(real, expected):
                raise SystemExit(f"refusing Docker apply because real path changed since container run: {real}")
        result = PrototypeBackend().apply(name, clear)
        if clear:
            docker_baseline_path(name).write_text("{}\n")
        write_metadata(name, backend=self.name)
        return result

    def discard(self, name: str) -> None:
        ensure_session_storage(name)
        shutil.rmtree(overlay_root(name))
        overlay_root(name).mkdir(parents=True, exist_ok=True)
        deletes_path(name).write_text("")
        docker_baseline_path(name).write_text("{}\n")

    def mark_delete(self, name: str, real_path: str) -> Path:
        ensure_session_storage(name)
        real = normalize_abs(real_path)
        with deletes_path(name).open("a") as fh:
            fh.write(str(real) + "\n")
        return real


def sandbox_backend() -> SandboxBackend:
    return PrototypeBackend()


def fuse_backend() -> FuseBackend:
    return FuseBackend()


def docker_backend() -> DockerBackend:
    return DockerBackend()


def backend_by_name(name: str) -> SandboxBackend:
    if name == "prototype":
        return sandbox_backend()
    if name == "docker":
        return docker_backend()
    if name == "fuse":
        return fuse_backend()
    raise SystemExit(f"unsupported backend: {name}")


@dataclass
class LaunchSpec:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    stdin: str | None = None
    text: bool = False
    display_command: str = ""


def cmd_init(args: argparse.Namespace) -> int:
    backend_by_name(args.backend).ensure(args.name, args.read, args.write)
    print(f"created session: {sandbox_root(args.name)}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name or f"session-{uuid.uuid4().hex[:8]}"
    backend_by_name(args.backend).create(name, args.read, args.write, plain=args.plain)
    print(name)
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    backend = backend_by_name(args.backend)
    vp = backend.prepare_virtual_path(args.name, args.real_path, mkdir=args.mkdir, directory=args.directory)
    print(vp)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    backend = backend_by_name(args.backend)
    backend.ensure(args.name, args.read, args.write)
    command = args.command
    stdin_data = None if command or sys.stdin.isatty() else sys.stdin.read()
    spec = backend.prepare_shell(args.name, command, stdin_data=stdin_data)
    write_metadata(args.name, status="running", lastCommand=spec.display_command, pid=os.getpid())
    try:
        proc = subprocess.run(
            spec.argv,
            cwd=spec.cwd,
            env=spec.env,
            input=spec.stdin,
            text=spec.text,
        )
        return proc.returncode
    finally:
        write_metadata(args.name, status="idle", pid=None, lastExitedAt=now_iso())


def cmd_session(args: argparse.Namespace) -> int:
    return cmd_run(args)


def cmd_open_terminal(args: argparse.Namespace) -> int:
    backend_name = read_metadata(args.name).get("backend", args.backend)
    command = backend_by_name(backend_name).open_terminal_command(args.name, args.read, args.write)
    script = f'tell application "Terminal" to do script "{command.replace(chr(34), chr(92) + chr(34))}"'
    subprocess.run(["osascript", "-e", script], check=True)
    write_metadata(args.name, status="opening", lastCommand="Terminal session")
    return 0


def find_app_executable(app: Path) -> Path:
    info = app / "Contents" / "Info.plist"
    macos = app / "Contents" / "MacOS"
    if not macos.is_dir():
        raise SystemExit(f"not an app bundle: {app}")
    candidates = [p for p in macos.iterdir() if p.is_file() and os.access(p, os.X_OK)]
    if not candidates:
        raise SystemExit(f"no executable found in {macos}")
    stem = app.name.removesuffix(".app")
    for candidate in candidates:
        if candidate.name == stem:
            return candidate
    return candidates[0]


def cmd_run_app(args: argparse.Namespace) -> int:
    backend = backend_by_name(args.backend)
    backend.ensure(args.name, args.read, args.write)
    app = normalize_abs(args.app)
    exe = find_app_executable(app)
    spec = backend.prepare_app(args.name, exe, args.args)
    print(f"starting {app} via {exe}")
    proc = subprocess.run(spec.argv, cwd=spec.cwd, env=spec.env, input=spec.stdin, text=spec.text)
    return proc.returncode


def iter_overlay_entries(name: str):
    root = overlay_root(name)
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".DS_Store" in rel.parts:
            continue
        if path.is_dir() and not path.is_symlink() and any(path.iterdir()):
            continue
        yield path, Path("/") / rel


def collect_changes(name: str) -> list[dict]:
    ensure_session_storage(name)
    changes = []
    for overlay, real in iter_overlay_entries(name):
        kind = "dir" if overlay.is_dir() else "file"
        if overlay.is_symlink():
            kind = "symlink"
        size = 0
        if overlay.is_file():
            size = overlay.stat().st_size
        changes.append({
            "change": "write",
            "kind": kind,
            "realPath": str(real),
            "overlayPath": str(overlay),
            "size": size,
        })
    deletes = [p for p in deletes_path(name).read_text().splitlines() if p]
    for deleted in deletes:
        changes.append({
            "change": "delete",
            "kind": "path",
            "realPath": deleted,
            "overlayPath": None,
            "size": 0,
        })
    return changes


def cmd_changes(args: argparse.Namespace) -> int:
    backend_name = read_metadata(args.name).get("backend", "prototype")
    changes = backend_by_name(backend_name).list_changes(args.name)
    if args.json:
        print(json.dumps(changes, indent=2))
        return 0
    if not changes:
        print("no pending changes")
        return 0
    for item in changes:
        label = item["change"] if item["change"] == "delete" else item["kind"]
        print(f"{label:7} {item['realPath']}")
    return 0


def collect_sessions() -> list[dict]:
    root = project_root() / SANDBOX_DIR
    if not root.exists():
        return []
    sessions = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        name = path.name
        data = read_metadata(name)
        if data.get("sandboxed") is False:
            changes = []
        else:
            backend_by_name(data.get("backend", "prototype")).ensure(name)
            data = read_metadata(name)
            changes = collect_changes(name)
        data.update({
            "name": name,
            "path": str(path),
            "pendingChanges": len(changes),
            "changes": changes,
        })
        sessions.append(data)
    return sessions


def cmd_list(args: argparse.Namespace) -> int:
    sessions = sandbox_backend().list_sessions()
    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0
    if not sessions:
        print("no sessions")
        return 0
    for session in sessions:
        print(f"{session['name']:18} {session.get('status', 'idle'):8} {session['pendingChanges']} change(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    data = read_metadata(args.name)
    if data.get("sandboxed") is False:
        changes = []
    else:
        backend_by_name(data.get("backend", "prototype")).ensure(args.name)
        data = read_metadata(args.name)
        changes = backend_by_name(data.get("backend", "prototype")).list_changes(args.name)
    data.update({
        "name": args.name,
        "path": str(sandbox_root(args.name)),
        "changes": changes,
    })
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"name: {args.name}")
        print(f"backend: {data.get('backend', 'prototype' if data.get('sandboxed') is not False else 'plain')}")
        print(f"status: {data.get('status', 'idle')}")
        print(f"storage: {data.get('overlayPath', overlay_root(args.name))}")
        print(f"changes: {len(data['changes'])}")
    return 0


def copy_entry(src: Path, dst: Path) -> None:
    if src.is_dir() and not src.is_symlink():
        dst.mkdir(parents=True, exist_ok=True)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        os.symlink(os.readlink(src), dst)
        return
    shutil.copy2(src, dst)


def backup_existing(path: Path, backup_root: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    rel = str(path).lstrip("/")
    target = backup_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        shutil.copytree(path, target, dirs_exist_ok=True)
    else:
        shutil.copy2(path, target, follow_symlinks=False)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_host_path(path: Path) -> dict:
    if path.is_symlink():
        return {"type": "symlink", "target": os.readlink(path)}
    if path.is_dir():
        return {"type": "dir"}
    if path.is_file():
        return {"type": "file", "sha256": file_sha256(path), "size": path.stat().st_size}
    return {"type": "missing"}


def baseline_matches(path: Path, expected: dict) -> bool:
    current = describe_host_path(path)
    if current.get("type") != expected.get("type"):
        return False
    if current["type"] == "file":
        return current.get("sha256") == expected.get("sha256") and current.get("size") == expected.get("size")
    if current["type"] == "symlink":
        return current.get("target") == expected.get("target")
    return True


def cmd_apply(args: argparse.Namespace) -> int:
    backend_name = read_metadata(args.name).get("backend", "prototype")
    applied, backup = backend_by_name(backend_name).apply(args.name, clear=args.clear)
    if applied == 0:
        print("no pending changes")
        return 0
    print(f"applied {applied} change(s)")
    print(f"backup: {backup}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    backend_name = read_metadata(args.name).get("backend", "prototype")
    real = backend_by_name(backend_name).mark_delete(args.name, args.real_path)
    print(f"marked for delete: {real}")
    return 0


def cmd_rewrite(args: argparse.Namespace) -> int:
    backend_name = read_metadata(args.name).get("backend", "prototype")
    print(backend_by_name(backend_name).rewrite_line(args.name, args.line))
    return 0


def cmd_fuse_status(args: argparse.Namespace) -> int:
    status = fuse_backend().status()
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        label = "available" if status["available"] else "unavailable"
        print(f"macFUSE: {label}")
        print(f"filesystem: {status['filesystem'] or '-'}")
        print(f"framework: {status['framework'] or '-'}")
        print(f"mount command: {status['mountCommand'] or '-'}")
        print(f"python binding: {'yes' if status['pythonBinding'] else 'no'}")
    return 0 if status["available"] else 2


def cmd_docker_status(args: argparse.Namespace) -> int:
    status = docker_backend().status()
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"docker: {'available' if status['available'] else 'unavailable'}")
        print(f"path: {status['path'] or '-'}")
        print(f"daemon: {'running' if status['daemon'] else 'not running'}")
        print(f"server version: {status['serverVersion'] or '-'}")
        print(f"context: {status['context'] or '-'}")
        if status.get("error"):
            print(f"error: {status['error']}")
    return 0 if status["available"] and status["daemon"] else 2


def print_backend_status_text(status: dict) -> None:
    macfuse = status["macfuse"]
    docker = status["docker"]
    virtual = status["arbitraryVirtualPaths"]
    container = status["containerSandbox"]
    print(f"default backend: {status['defaultBackend']}")
    print(f"production backend: {status['productionBackend']}")
    print(f"arbitrary virtual paths: {'ready' if virtual['ready'] else 'not ready'}")
    print(f"macFUSE: {'available' if macfuse['available'] else 'unavailable'}")
    print(f"mount command: {macfuse['mountCommand'] or '-'}")
    print(f"python binding: {'yes' if macfuse['pythonBinding'] else 'no'}")
    print(f"container backend: {status['containerBackend']} ({'ready' if container['ready'] else 'not ready'})")
    print(f"docker: {'running' if docker['daemon'] else 'not running' if docker['available'] else 'unavailable'}")
    print(f"homebrew: {status['homebrew']['path'] or '-'}")


def cmd_backend_status(args: argparse.Namespace) -> int:
    status = backend_status()
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print_backend_status_text(status)
    return 0


def cmd_backend_doctor(args: argparse.Namespace) -> int:
    report = backend_doctor_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ready"] else 2
    print(f"backend ready: {'yes' if report['ready'] else 'no'}")
    for check in report["checks"]:
        marker = "ok" if check["ok"] else "missing"
        print(f"{marker:7} {check['id']}: {check['message']}")
    print(f"next: {report['nextAction']}")
    return 0 if report["ready"] else 2


def print_install_plan(plan: dict) -> None:
    print(f"backend: {plan['backend']}")
    print(f"backend ready: {'yes' if plan['backendReady'] else 'no'}")
    if plan["backend"] == "macfuse":
        print(f"macFUSE currently installed: {'yes' if plan['macfuseInstalled'] else 'no'}")
        print("installation requires macOS administrator/system extension approval.")
    elif plan["backend"] == "docker":
        print(f"Docker currently installed: {'yes' if plan['dockerInstalled'] else 'no'}")
        print(f"Docker daemon running: {'yes' if plan['dockerDaemonRunning'] else 'no'}")
    for idx, step in enumerate(plan["steps"], start=1):
        action = step.get("command") or step.get("url", "")
        print(f"{idx}. {step['type']}: {action}")
        print(f"   {step['note']}")


def cmd_backend_install(args: argparse.Namespace) -> int:
    if args.open and args.execute:
        raise SystemExit("--open and --execute cannot be used together")
    if args.json and (args.open or args.execute):
        raise SystemExit("--json cannot be combined with --open or --execute")
    plan = backend_install_plan(args.backend, use_brew=args.use_brew)
    if args.json:
        print(json.dumps(plan, indent=2))
        return 0
    if args.dry_run or not (args.open or args.execute):
        print_install_plan(plan)
        if not args.dry_run:
            print("no action taken; pass --open or --execute to start the guided install.")
        return 0
    if args.open:
        subprocess.run(["open", plan["guideUrl"]], check=False)
        print(f"opened installer guide: {plan['guideUrl']}")
        return 0
    if args.execute:
        if args.backend != "macfuse":
            raise SystemExit(f"backend installer for {args.backend} is guide-only; use --open")
        brew = shutil.which("brew")
        if not args.use_brew:
            raise SystemExit("--execute currently requires --use-brew so the command is explicit")
        if not brew:
            raise SystemExit("Homebrew is not available; use --open for the official installer flow")
        command = [brew, "install", "--cask", "macfuse"]
        print("+ " + " ".join(shlex.quote(part) for part in command))
        return subprocess.run(command).returncode
    return 0


def prompt_yes_no(question: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{question} [{suffix}] ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def run_setup_command(command: list[str], execute: bool) -> int:
    printable = " ".join(shlex.quote(part) for part in command)
    print(f"+ {printable}")
    if not execute:
        return 0
    return subprocess.run(command).returncode


def cmd_setup(args: argparse.Namespace) -> int:
    report = backend_doctor_report()
    status = report["status"]
    macfuse = status["macfuse"]
    brew = status["homebrew"]["path"]

    print("MacBox setup")
    print("------------")
    print("Goal: install dependencies for the mounted FUSE backend.")
    print("Note: product readiness still requires session execution through the mounted backend.")
    print(f"macFUSE: {'installed' if macfuse['available'] else 'missing'}")
    print(f"Python FUSE binding: {'installed' if macfuse['pythonBinding'] else 'missing'}")
    print(f"Homebrew: {brew or 'not found'}")
    print()

    if args.dry_run:
        print("Dry run. No install actions will be started.")

    failures: list[int] = []

    if not macfuse["available"]:
        if brew and args.use_brew:
            command = [brew, "install", "--cask", "macfuse"]
            execute = args.yes or (not args.dry_run and prompt_yes_no("Install macFUSE with Homebrew now?"))
            rc = run_setup_command(command, execute and not args.dry_run)
            if execute and not args.dry_run and rc:
                failures.append(rc)
        else:
            print("macFUSE requires a macOS system extension approval.")
            print("Official guide: https://macfuse.github.io/")
            should_open = args.open or (
                not args.yes and not args.dry_run and prompt_yes_no("Open the official macFUSE install guide now?")
            )
            if should_open and not args.dry_run:
                subprocess.run(["open", "https://macfuse.github.io/"], check=False)
                print("Opened macFUSE guide.")
            if brew:
                print(f"Homebrew alternative: {brew} install --cask macfuse")
    else:
        print("macFUSE is already visible to MacBox.")

    if not macfuse["pythonBinding"]:
        command = [sys.executable, "-m", "pip", "install", "fusepy"]
        should_install = args.install_python_binding or (
            not args.yes and not args.dry_run and prompt_yes_no("Install Python FUSE binding with this Python interpreter now?")
        )
        if should_install or args.dry_run:
            rc = run_setup_command(command, should_install and not args.dry_run)
            if should_install and not args.dry_run and rc:
                failures.append(rc)
        elif args.yes:
            print("Skipping Python FUSE binding install; pass --install-python-binding --yes to install fusepy.")
    else:
        print("Python FUSE binding is already available.")

    print()
    print("After installing, run:")
    print("  ./macbox backend doctor")
    print("  ./scripts/verify-fuse-overlay-writes.sh")
    return failures[0] if failures else 0


def cmd_mount(args: argparse.Namespace) -> int:
    if args.backend != "fuse":
        raise SystemExit(f"unsupported backend for mount: {args.backend}")
    mount = fuse_backend().mount_readonly(args.name, args.mount, args.read, args.write, foreground=args.foreground)
    if args.foreground:
        print(f"foreground mount exited: {args.name}: {mount}")
    else:
        print(f"mounted {args.name}: {mount}")
    return 0


def cmd_unmount(args: argparse.Namespace) -> int:
    fuse_backend().unmount(args.name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MacBox: a small macOS sandbox runner with explicit apply.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create or update a sandbox")
    p.add_argument("--name", default="default")
    p.add_argument("--backend", default="prototype", choices=["prototype", "docker"])
    p.add_argument("--read", action="append", default=[], help="read root to document/allow")
    p.add_argument("--write", action="append", default=[], help="real root that Apply Changes may modify")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new", help="create a new sandbox session")
    p.add_argument("--name", default=None)
    p.add_argument("--backend", default="prototype", choices=["prototype", "docker"])
    p.add_argument("--plain", action="store_true", help="create a normal non-sandbox session")
    p.add_argument("--read", action="append", default=[], help="read root to document/allow")
    p.add_argument("--write", action="append", default=[], help="real root that Apply Changes may modify")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("run", help="run a command or interactive shell in a sandbox backend")
    p.add_argument("--name", default="default")
    p.add_argument("--backend", default="prototype", choices=["prototype", "docker"])
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("session", help="enter a sandboxed Terminal session")
    p.add_argument("--name", default="default")
    p.add_argument("--backend", default="prototype", choices=["prototype", "docker"])
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("open-terminal", help="open a sandboxed session in macOS Terminal")
    p.add_argument("--name", default="default")
    p.add_argument("--backend", default="prototype", choices=["prototype", "docker"])
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.set_defaults(func=cmd_open_terminal)

    p = sub.add_parser("run-app", help="best-effort launch of a .app bundle executable")
    p.add_argument("--name", default="default")
    p.add_argument("--backend", default="prototype", choices=["prototype", "docker"])
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.add_argument("app")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run_app)

    p = sub.add_parser("path", help="map a real absolute path to its virtual overlay path")
    p.add_argument("--name", default="default")
    p.add_argument("--backend", default="prototype", choices=["prototype", "docker"])
    p.add_argument("--mkdir", action="store_true")
    p.add_argument("--directory", action="store_true")
    p.add_argument("real_path")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("rewrite", help=argparse.SUPPRESS)
    p.add_argument("--name", default="default")
    p.add_argument("line")
    p.set_defaults(func=cmd_rewrite)

    p = sub.add_parser("fuse-status", help="show macFUSE availability for the future fuse backend")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fuse_status)

    p = sub.add_parser("docker-status", help="show Docker availability for the container backend")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_docker_status)

    p = sub.add_parser("setup", help="guided setup for the mounted FUSE backend")
    p.add_argument("--dry-run", action="store_true", help="show actions without opening pages or running installers")
    p.add_argument("--open", action="store_true", help="open the official macFUSE install guide when needed")
    p.add_argument("--use-brew", action="store_true", help="prefer Homebrew for macFUSE installation")
    p.add_argument("--install-python-binding", action="store_true", help="install fusepy for the current Python interpreter")
    p.add_argument("--yes", action="store_true", help="run explicit install steps without interactive prompts")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("backend", help="manage production backend dependencies")
    backend_sub = p.add_subparsers(dest="backend_cmd", required=True)

    bp = backend_sub.add_parser("status", help="show backend dependency status")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_backend_status)

    bp = backend_sub.add_parser("doctor", help="run backend readiness checks")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_backend_doctor)

    bp = backend_sub.add_parser("install", help="show or start guided backend installation")
    bp.add_argument("--backend", default="macfuse", choices=["macfuse", "docker"])
    bp.add_argument("--use-brew", action="store_true", help="prefer the Homebrew cask install plan")
    bp.add_argument("--dry-run", action="store_true", help="print the plan and do not open or execute anything")
    bp.add_argument("--open", action="store_true", help="open the official installer guide")
    bp.add_argument("--execute", action="store_true", help="run the explicit install command when available")
    bp.add_argument("--json", action="store_true")
    bp.set_defaults(func=cmd_backend_install)

    p = sub.add_parser("mount", help="mount a sandbox backend")
    p.add_argument("--backend", default="fuse", choices=["fuse"])
    p.add_argument("--name", default="default")
    p.add_argument("--mount", required=True, help="mount point path")
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.add_argument("--foreground", action="store_true", help="run the FUSE helper in the foreground")
    p.set_defaults(func=cmd_mount)

    p = sub.add_parser("unmount", help="unmount a sandbox backend")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_unmount)

    p = sub.add_parser("changes", help="list pending virtual writes")
    p.add_argument("--name", default="default")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("list", help="list sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one session")
    p.add_argument("--name", default="default")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("apply", help="apply virtual writes to the real disk")
    p.add_argument("--name", default="default")
    p.add_argument("--clear", action="store_true", help="clear overlay after successful apply")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("delete", help="mark a real path for deletion on next apply")
    p.add_argument("--name", default="default")
    p.add_argument("real_path")
    p.set_defaults(func=cmd_delete)
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = os.sys.argv[1:]
    if not argv:
        argv = ["session", "--name", f"session-{uuid.uuid4().hex[:8]}"]
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
