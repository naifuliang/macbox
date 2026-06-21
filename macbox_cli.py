#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
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


def rcfile_path(name: str) -> Path:
    return sandbox_root(name) / "shellrc"


def deletes_path(name: str) -> Path:
    return sandbox_root(name) / "deletes.txt"


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


def should_virtualize_path(path: str) -> bool:
    if not path:
        return False
    if path.startswith("-"):
        return False
    if "://" in path:
        return False
    return True


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


def cmd_init(args: argparse.Namespace) -> int:
    ensure_sandbox(args.name, args.read, args.write)
    print(f"created session: {sandbox_root(args.name)}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name or f"session-{uuid.uuid4().hex[:8]}"
    if args.plain:
        root = sandbox_root(name)
        root.mkdir(parents=True, exist_ok=True)
        write_metadata(name, sandboxed=False, status="idle", readRoots=[], writeRoots=[], overlayPath=None)
        print(name)
        return 0
    ensure_sandbox(name, args.read, args.write)
    print(name)
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    ensure_sandbox(args.name)
    vp = virtual_path(args.name, args.real_path)
    if args.mkdir:
        if args.directory:
            vp.mkdir(parents=True, exist_ok=True)
        else:
            vp.parent.mkdir(parents=True, exist_ok=True)
    print(vp)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ensure_sandbox(args.name, args.read, args.write)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    rewritten_stdin = None
    if not command:
        shell = os.environ.get("SHELL", "/bin/zsh")
        command = [shell, "-i"]
        if Path(shell).name in ("bash", "zsh"):
            command = [shell, "-i"] if Path(shell).name == "zsh" else [shell, "--rcfile", str(rcfile_path(args.name)), "-i"]
        if not sys.stdin.isatty():
            stdin_data = sys.stdin.read()
            lines = stdin_data.splitlines(keepends=True)
            rewritten_stdin = "".join(
                rewrite_shell_line(args.name, line.removesuffix("\n").removesuffix("\r")) + ("\n" if line.endswith("\n") else "")
                for line in lines
            )
    elif len(command) >= 3 and Path(command[0]).name == "zsh" and command[1] == "-lc":
        command = [command[0], command[1], rewrite_shell_line(args.name, command[2]), *command[3:]]
    env = sandbox_environment(args.name)
    if not args.command and Path(env.get("SHELL", "/bin/zsh")).name == "zsh":
        zhome = sandbox_root(args.name) / "home"
        env["ZDOTDIR"] = str(zhome)
        (zhome / ".zshrc").write_text(rcfile_path(args.name).read_text())
    write_metadata(args.name, status="running", lastCommand=" ".join(command), pid=os.getpid())
    try:
        proc = subprocess.run(
            ["sandbox-exec", "-f", str(profile_path(args.name)), *command],
            env=env,
            input=rewritten_stdin,
            text=rewritten_stdin is not None,
        )
        return proc.returncode
    finally:
        write_metadata(args.name, status="idle", pid=None, lastExitedAt=now_iso())


def cmd_session(args: argparse.Namespace) -> int:
    return cmd_run(args)


def cmd_open_terminal(args: argparse.Namespace) -> int:
    exe = project_root() / "macbox"
    meta = read_metadata(args.name)
    if meta.get("sandboxed") is False:
        sandbox_root(args.name).mkdir(parents=True, exist_ok=True)
        command = f'cd "{quote_sb(project_root())}" && exec "${{SHELL:-/bin/zsh}}" -i'
    else:
        ensure_sandbox(args.name, args.read, args.write)
        command = f'cd "{quote_sb(project_root())}" && "{quote_sb(exe)}" session --name "{quote_sb(args.name)}"'
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
    ensure_sandbox(args.name, args.read, args.write)
    app = normalize_abs(args.app)
    exe = find_app_executable(app)
    env = sandbox_environment(args.name)
    print(f"starting {app} via {exe}")
    proc = subprocess.run(["sandbox-exec", "-f", str(profile_path(args.name)), str(exe), *args.args], env=env)
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
    ensure_sandbox(name)
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
    ensure_sandbox(args.name)
    changes = collect_changes(args.name)
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
            ensure_sandbox(name)
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
    sessions = collect_sessions()
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
        ensure_sandbox(args.name)
        data = read_metadata(args.name)
        changes = collect_changes(args.name)
    data.update({
        "name": args.name,
        "path": str(sandbox_root(args.name)),
        "changes": changes,
    })
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"name: {args.name}")
        print(f"status: {data.get('status', 'idle')}")
        print(f"overlay: {data.get('overlayPath', overlay_root(args.name))}")
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


def cmd_apply(args: argparse.Namespace) -> int:
    ensure_sandbox(args.name)
    cfg = read_config(args.name)
    entries = list(iter_overlay_entries(args.name) or [])
    deletes = [normalize_abs(p) for p in deletes_path(args.name).read_text().splitlines() if p]
    if not entries and not deletes:
        print("no pending changes")
        return 0
    backup = sandbox_root(args.name) / "backups" / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
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
    if args.clear:
        shutil.rmtree(overlay_root(args.name))
        overlay_root(args.name).mkdir(parents=True, exist_ok=True)
        deletes_path(args.name).write_text("")
    print(f"applied {applied} change(s)")
    print(f"backup: {backup}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    ensure_sandbox(args.name)
    real = normalize_abs(args.real_path)
    with deletes_path(args.name).open("a") as fh:
        fh.write(str(real) + "\n")
    print(f"marked for delete: {real}")
    return 0


def cmd_rewrite(args: argparse.Namespace) -> int:
    print(rewrite_shell_line(args.name, args.line))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MacBox: a small macOS sandbox runner with explicit apply.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create or update a sandbox")
    p.add_argument("--name", default="default")
    p.add_argument("--read", action="append", default=[], help="read root to document/allow")
    p.add_argument("--write", action="append", default=[], help="real root that Apply Changes may modify")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new", help="create a new sandbox session")
    p.add_argument("--name", default=None)
    p.add_argument("--plain", action="store_true", help="create a normal non-sandbox session")
    p.add_argument("--read", action="append", default=[], help="read root to document/allow")
    p.add_argument("--write", action="append", default=[], help="real root that Apply Changes may modify")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("run", help="run a command or interactive shell under sandbox-exec")
    p.add_argument("--name", default="default")
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("session", help="enter a sandboxed Terminal session")
    p.add_argument("--name", default="default")
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("open-terminal", help="open a sandboxed session in macOS Terminal")
    p.add_argument("--name", default="default")
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.set_defaults(func=cmd_open_terminal)

    p = sub.add_parser("run-app", help="best-effort launch of a .app bundle executable")
    p.add_argument("--name", default="default")
    p.add_argument("--read", action="append", default=None)
    p.add_argument("--write", action="append", default=None)
    p.add_argument("app")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run_app)

    p = sub.add_parser("path", help="map a real absolute path to its virtual overlay path")
    p.add_argument("--name", default="default")
    p.add_argument("--mkdir", action="store_true")
    p.add_argument("--directory", action="store_true")
    p.add_argument("real_path")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("rewrite", help=argparse.SUPPRESS)
    p.add_argument("--name", default="default")
    p.add_argument("line")
    p.set_defaults(func=cmd_rewrite)

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
