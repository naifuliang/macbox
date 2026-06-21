# MacBox

MacBox is a first-pass macOS sandbox terminal manager.

## Status

The current sandbox backend is a **prototype**. It is good enough to validate
the CLI, session model, change tracking, and manager UI, but it is not the final
filesystem architecture.

Current behavior:

- Real disk reads are allowed.
- Real disk writes are blocked by `sandbox-exec`.
- Shell redirections and a small set of shell wrappers stage writes under
  `.macbox/sessions/<name>/overlay`.
- `apply` copies staged changes back to the real paths after write-root checks.

Known boundary:

- This is not a complete transparent copy-on-write filesystem.
- System-protected binaries do not reliably honor `DYLD_INSERT_LIBRARIES`.
- Shell wrappers only cover common interactive commands such as redirection,
  `mkdir`, and `touch`.
- A production backend should move to a mounted overlay filesystem, most likely
  macFUSE on macOS.
- A workspace-only backend is not the MacBox product path. The baseline
  requirement is arbitrary real paths connected through a virtual write layer.

See [docs/sandbox-architecture.md](docs/sandbox-architecture.md) for the
architecture decision and staged roadmap.

It has two parts:

- `macbox`: a CLI for creating sessions and entering a sandboxed shell.
- `MacBoxManager`: a SwiftUI macOS manager with browser-style session tabs.

## Run the CLI

```sh
chmod +x ./macbox
./macbox
```

Inside a sandbox session:

These commands are covered by the prototype shell layer. They are examples, not
a guarantee of general filesystem transparency:

```sh
echo hello > ~/Desktop/hello-from-macbox.txt
mkdir scratch
touch scratch/example.txt
mb-changes
mb-apply
```

Staged writes live under `.macbox/sessions/<name>/overlay` and are copied to the
real path only after `apply`. `vpath` remains available as an explicit escape
hatch when a command is not covered by the prototype wrappers:

```sh
echo hello > "$(vpath ~/Desktop/hello-from-macbox.txt)"
```

`./macbox` creates a fresh sandbox session and enters it immediately. For a
named session, use:

```sh
./macbox new --name demo --write ~/Desktop
./macbox session --name demo
```

## Run the manager

```sh
swift run MacBoxManager
```

The manager opens as a compact glass-style window:

- Top tabs are sessions.
- The plus menu creates either a sandbox session or a plain session.
- The main page is terminal-focused.
- Sandbox file management is hidden by default.
- Click `Files` on a sandbox tab to open the right-side inspector.
- Click `Open` to launch the selected session in macOS Terminal.

## Useful commands

```sh
./macbox list
./macbox list --json
./macbox show --name demo --json
./macbox changes --name demo
./macbox changes --name demo --json
./macbox apply --name demo --clear
./macbox delete --name demo ~/Desktop/old-file.txt
./macbox open-terminal --name demo
./macbox new --name plain --plain
```

## Production Backend Setup

The production backend is planned around macFUSE plus a Python FUSE binding so
MacBox can mount arbitrary real paths behind a virtual copy-on-write layer.
Current commands expose dependency status and guided setup:

```sh
./macbox backend status
./macbox backend doctor
./macbox backend install --backend macfuse --dry-run
./macbox backend install --backend macfuse --open
```

The installer command is explicit by design. It prints the plan by default,
opens the official macFUSE install guide only with `--open`, and only runs the
Homebrew cask path with `--use-brew --execute`. Installing macFUSE alone does
not make the production backend ready; `backend doctor` also checks the Python
FUSE binding and the mounted overlay implementation status.

The mounted backend can read real paths and stage writes into the session
overlay:

```sh
./macbox mount --backend fuse --name demo --mount /tmp/macbox-demo
./macbox unmount --name demo
```

Real mounting requires macFUSE and the Python FUSE binding. Without those
dependencies, `./scripts/verify-fuse-readonly.sh` still validates the helper and
CLI orchestration paths without touching system mount state.

## Verify

```sh
./scripts/verify-prototype.sh
./scripts/verify-backend-installer.sh
./scripts/verify-fuse-readonly.sh
./scripts/verify-fuse-overlay-writes.sh
```

The integration test uses `sandbox-exec`. If the outer execution environment
blocks `sandbox-exec`, the normal test discovery skips that test; run the
integration test directly from a normal macOS shell for full validation:

```sh
python3 -m unittest tests/test_macbox_integration.py
```
