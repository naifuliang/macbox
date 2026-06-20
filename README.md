# MacBox

MacBox is a first-pass macOS sandbox terminal manager.

It has two parts:

- `macbox`: a CLI for creating sessions and entering a sandboxed shell.
- `MacBoxManager`: a SwiftUI macOS manager with browser-style session tabs.

## Run the CLI

```sh
chmod +x ./macbox
./macbox
```

Inside a sandbox session:

```sh
echo hello > "$(vpath ~/Desktop/hello-from-macbox.txt)"
mb-changes
mb-apply
```

The real disk is readable. Direct real-disk writes are blocked by
`sandbox-exec`. Staged writes live under `.macbox/sessions/<name>/overlay` and
are copied to the real path only after `apply`.

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

## Current boundary

This version is intentionally conservative. macOS does not provide a simple
per-process transparent copy-on-write filesystem through `sandbox-exec`.
Commands must write through `vpath` to stage a virtual file. Fully transparent
redirection of writes to their original paths needs a heavier filesystem layer
such as macFUSE or a system extension.
