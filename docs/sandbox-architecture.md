# Sandbox Architecture

## Phase 0 Decision

The current MacBox sandbox backend is frozen as a prototype backend. It is not
the long-term filesystem architecture.

The prototype backend combines:

- `sandbox-exec` to deny real disk writes.
- An overlay directory at `.macbox/sessions/<name>/overlay`.
- Shell-line rewriting for redirections such as `>`, `>>`, and `<`.
- zsh wrappers for a small set of common commands such as `mkdir` and `touch`.
- An experimental `DYLD_INSERT_LIBRARIES` interpose library for non-protected
  processes.

This validates the product workflow, but it cannot provide complete transparent
copy-on-write behavior.

## Why The Prototype Is Not Enough

`sandbox-exec` is an allow/deny mechanism. It does not rewrite paths. Once a
program opens `/Users/example/file.txt` for writing, the sandbox can only allow
or reject that write.

`DYLD_INSERT_LIBRARIES` is also not a complete solution on macOS:

- Protected system binaries may ignore or reject dynamic library injection.
- Static or specially launched processes may bypass the hook.
- GUI applications and child process trees are difficult to cover reliably.

Shell wrappers are useful for a demo, but they do not cover programs that open
files internally, such as editors, package managers, build tools, or GUI apps.

## Target Architecture

The production backend should be a mounted overlay filesystem:

```text
Terminal / App
    |
    v
MacBox virtual root mount
    |
    +-- read miss -> real disk
    +-- write     -> overlay
    +-- delete    -> tombstone
    +-- rename    -> overlay operation
    |
    v
Apply Changes -> checked copy back to real disk
```

On macOS, the practical first target is a macFUSE backend. A workspace-only
backend is not a production path for MacBox because the product requirement is
arbitrary real paths behind a virtual write layer.

## Backend Contract

Phase 1 introduces a backend boundary:

```text
SandboxBackend
  create(session)
  ensure(session)
  prepareShell(session, command)
  prepareApp(session, executable, args)
  openTerminalCommand(session)
  realToVirtual(path)
  prepareVirtualPath(path)
  listChanges(session)
  listSessions()
  apply(session)
  discard(session)
  markDelete(path)
```

The CLI should depend on this contract, not on the prototype overlay layout.
The current implementation provides `PrototypeBackend`, which preserves the
Phase 0 behavior while making `FuseBackend` a separate future implementation.

## Stage Roadmap And Acceptance

### Phase 0: Freeze Prototype

Acceptance:

- `python3 -m unittest discover -s tests` passes.
- `python3 -m unittest tests/test_macbox_integration.py` passes from a normal
  macOS shell with `sandbox-exec` available.
- `swift build` passes.
- README clearly marks the current backend as prototype.
- This document records why the backend must be replaced.

### Phase 1: Backend Interface

Acceptance:

- CLI session, path, changes, apply, delete, rewrite, and environment flows call
  through a backend abstraction.
- CLI shell/app launch flows execute backend-provided launch specs instead of
  hard-coding the prototype `sandbox-exec` command shape.
- Existing prototype behavior still passes all Phase 0 tests.
- Backend contract tests cover create, changes, apply, discard, delete, launch
  specs, and path mapping.

### Phase 2: macFUSE Detection And Mount Command Shape

Acceptance:

- MacBox can detect whether macFUSE is installed.
- `macbox fuse-status --json` reports macFUSE filesystem/framework/command and
  Python binding availability.
- `macbox mount --backend fuse --name demo --mount <path>` has a stable command
  shape and reports a clear unavailable/dependency error when macFUSE or Python
  FUSE bindings are missing.
- `scripts/verify-fuse-readonly.sh` passes on hosts without macFUSE by
  explicitly reporting that mount verification is not runnable.
- On a host with macFUSE and Python FUSE bindings, the next acceptance target is:
  `macbox mount --name demo --mount <path>` creates a read-only virtual root,
  reading through the mount falls back to the real disk, and
  `macbox unmount --name demo` cleans up the mount.

### Phase 3: Backend Installer And Doctor

Acceptance:

- `macbox backend status --json` reports the default prototype backend, the
  production FUSE backend, macFUSE detection, Homebrew detection, and that
  arbitrary virtual paths are required but not ready yet.
- `macbox backend doctor --json` returns actionable checks and exits non-zero
  when blocking dependencies or implementation gaps remain.
- `macbox backend install --backend macfuse --dry-run` prints an install plan
  without changing the machine.
- `macbox backend install --backend macfuse --open` starts the official guided
  installer flow.
- `macbox backend install --backend macfuse --use-brew --execute` only runs
  Homebrew when the user explicitly asks for that path.
- `scripts/verify-backend-installer.sh` passes on hosts without macFUSE.

### Phase 4: Mounted Read-Only Virtual Root

Acceptance:

- With macFUSE installed, `macbox mount --name demo --mount <path>` creates a
  virtual root.
- Reads through the virtual root fall back to the real disk.
- The mount presents arbitrary absolute real paths under the virtual root.
- `macbox unmount --name demo` cleans up the mount.
- `scripts/verify-fuse-readonly.sh` validates helper logic and mount
  orchestration on all hosts; when macFUSE and the Python binding are present,
  it also performs a real mount/read/reject-write/unmount check.

### Phase 5: Overlay Writes

Acceptance:

- Writes through the virtual root land in overlay, not on the real disk.
- `mkdir`, `touch`, `rm`, `mv`, `cp`, and shell redirection work without shell
  wrappers.
- `changes` accurately reports writes, deletes, and renames.

### Phase 6: Session Execution From Virtual Root

Acceptance:

- `./macbox` starts a shell inside the virtual root.
- Relative paths in the shell map back to their real absolute paths for change
  reporting.
- Exiting the shell leaves the real disk unchanged until apply.

### Phase 7: Apply, Discard, Diff

Acceptance:

- `apply` writes changes back only inside configured write roots.
- `discard` clears overlay and tombstones.
- `diff` shows staged content changes.
- Apply creates backups before overwriting or deleting real paths.

### Phase 8: GUI Integration

Acceptance:

- GUI sandbox sessions start from the virtual root.
- File sidebar shows backend changes and diffs.
- Apply and discard work from the GUI.

### Phase 9: Compatibility Matrix

Acceptance:

- Compatibility results are recorded for shell tools, editors, package managers,
  git workflows, Claude Code, and basic GUI app saves.

### Phase 10: Recovery

Acceptance:

- MacBox can recover or clean orphan mounts.
- Session locks prevent concurrent apply.
- Existing running sessions are listed correctly after app restart.
