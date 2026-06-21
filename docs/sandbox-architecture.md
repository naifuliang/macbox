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

On macOS, the practical first target is a macFUSE backend.

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

### Phase 2: macFUSE Read-Only Mount

Acceptance:

- MacBox can detect whether macFUSE is installed.
- `macbox mount --name demo --mount <path>` creates a read-only virtual root.
- Reading through the mount falls back to the real disk.
- `macbox unmount --name demo` cleans up the mount.

### Phase 3: Overlay Writes

Acceptance:

- Writes through the virtual root land in overlay, not on the real disk.
- `mkdir`, `touch`, `rm`, `mv`, `cp`, and shell redirection work without shell
  wrappers.
- `changes` accurately reports writes, deletes, and renames.

### Phase 4: Session Execution From Virtual Root

Acceptance:

- `./macbox` starts a shell inside the virtual root.
- Relative paths in the shell map back to their real absolute paths for change
  reporting.
- Exiting the shell leaves the real disk unchanged until apply.

### Phase 5: Apply, Discard, Diff

Acceptance:

- `apply` writes changes back only inside configured write roots.
- `discard` clears overlay and tombstones.
- `diff` shows staged content changes.
- Apply creates backups before overwriting or deleting real paths.

### Phase 6: GUI Integration

Acceptance:

- GUI sandbox sessions start from the virtual root.
- File sidebar shows backend changes and diffs.
- Apply and discard work from the GUI.

### Phase 7: Compatibility Matrix

Acceptance:

- Compatibility results are recorded for shell tools, editors, package managers,
  git workflows, Claude Code, and basic GUI app saves.

### Phase 8: Recovery

Acceptance:

- MacBox can recover or clean orphan mounts.
- Session locks prevent concurrent apply.
- Existing running sessions are listed correctly after app restart.
