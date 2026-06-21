import AppKit
import Darwin
import SwiftUI

struct Session: Identifiable, Decodable, Equatable {
    let id: String
    let name: String
    let sandboxed: Bool?
    let status: String?
    let overlayPath: String?
    let writeRoots: [String]?
    let pendingChanges: Int?
    let changes: [FileChange]

    var isSandboxed: Bool { sandboxed ?? true }
    var displayStatus: String { status ?? "idle" }
}

struct FileChange: Identifiable, Decodable, Equatable {
    let change: String
    let kind: String
    let realPath: String
    let overlayPath: String?
    let size: Int?

    var id: String { "\(change):\(realPath)" }
    var fileName: String { URL(fileURLWithPath: realPath).lastPathComponent }
}

struct TerminalLaunch {
    let executable: URL
    let arguments: [String]
    let currentDirectory: URL
    let environment: [String: String]
}

@MainActor
final class MacBoxStore: ObservableObject {
    @Published var allSessions: [Session] = []
    @Published var sessions: [Session] = []
    @Published var selectedID: Session.ID?
    @Published var visibleSessionIDs: [Session.ID] = []
    @Published var errorMessage: String?
    private var initializedTabs = false

    private var root: URL {
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        if FileManager.default.fileExists(atPath: cwd.appendingPathComponent("macbox").path) {
            return cwd
        }
        let appParent = Bundle.main.bundleURL.deletingLastPathComponent()
        if FileManager.default.fileExists(atPath: appParent.appendingPathComponent("macbox").path) {
            return appParent
        }
        return cwd
    }

    private var cli: URL {
        root.appendingPathComponent("macbox")
    }

    func terminalLaunch(for session: Session) -> TerminalLaunch {
        var env = ProcessInfo.processInfo.environment
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"

        if session.isSandboxed {
            return TerminalLaunch(
                executable: cli,
                arguments: ["session", "--name", session.name],
                currentDirectory: root,
                environment: env
            )
        }

        let shell = env["SHELL"].flatMap { $0.isEmpty ? nil : $0 } ?? "/bin/zsh"
        return TerminalLaunch(
            executable: URL(fileURLWithPath: shell),
            arguments: ["-l"],
            currentDirectory: root,
            environment: env
        )
    }

    func refresh() {
        do {
            let data = try run(["list", "--json"])
            var decoded = try JSONDecoder().decode([Session].self, from: data)
            if !decoded.contains(where: { $0.name == "terminal" && !$0.isSandboxed }) {
                _ = try run(["new", "--name", "terminal", "--plain"])
                let retry = try run(["list", "--json"])
                decoded = try JSONDecoder().decode([Session].self, from: retry)
            }
            allSessions = decoded
            if !initializedTabs {
                visibleSessionIDs = ["terminal"]
                selectedID = "terminal"
                initializedTabs = true
            }
            sessions = decoded.filter { visibleSessionIDs.contains($0.id) }
            if selectedID == nil || !sessions.contains(where: { $0.id == selectedID }) || selectedID == "plain-demo" {
                selectedID = decoded.first(where: { $0.name == "terminal" })?.id ?? decoded.first?.id
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func createSession(sandboxed: Bool) {
        let name = "session-\(String(UUID().uuidString.prefix(8)).lowercased())"
        do {
            if sandboxed {
                _ = try run(["new", "--name", name, "--write", NSHomeDirectory()])
            } else {
                _ = try run(["new", "--name", name, "--plain"])
            }
            if !visibleSessionIDs.contains(name) {
                visibleSessionIDs.append(name)
            }
            selectedID = name
            refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func openSessionFromHistory(_ session: Session) {
        if !visibleSessionIDs.contains(session.id) {
            visibleSessionIDs.append(session.id)
        }
        selectedID = session.id
        refresh()
    }

    func closeVisibleSession(_ session: Session) {
        visibleSessionIDs.removeAll { $0 == session.id }
        if visibleSessionIDs.isEmpty {
            visibleSessionIDs = ["terminal"]
        }
        selectedID = visibleSessionIDs.last
        refresh()
    }

    func openTerminal(for session: Session) {
        do {
            _ = try run(["open-terminal", "--name", session.name])
            refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func applyChanges(for session: Session) {
        do {
            _ = try run(["apply", "--name", session.name, "--clear"])
            refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func execute(_ command: String, in session: Session) -> String {
        let trimmed = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        if trimmed == "help" {
            return "Built-ins: help, clear\nSessions run commands with zsh. Sandbox sessions run through macbox."
        }

        let process = Process()
        process.currentDirectoryURL = root
        if session.isSandboxed {
            process.executableURL = cli
            process.arguments = ["run", "--name", session.name, "--", "/bin/zsh", "-lc", trimmed]
        } else {
            process.executableURL = URL(fileURLWithPath: "/bin/zsh")
            process.arguments = ["-lc", trimmed]
        }

        let output = Pipe()
        let error = Pipe()
        process.standardOutput = output
        process.standardError = error
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return error.localizedDescription
        }

        let stdout = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let stderr = String(data: error.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let combined = stdout + stderr
        if combined.isEmpty, process.terminationStatus != 0 {
            return "command exited with status \(process.terminationStatus)"
        }
        return combined.trimmingCharacters(in: .newlines)
    }

    private func run(_ arguments: [String]) throws -> Data {
        let process = Process()
        process.executableURL = cli
        process.arguments = arguments
        process.currentDirectoryURL = root

        let output = Pipe()
        let error = Pipe()
        process.standardOutput = output
        process.standardError = error
        try process.run()
        process.waitUntilExit()

        let data = output.fileHandleForReading.readDataToEndOfFile()
        if process.terminationStatus != 0 {
            let message = String(data: error.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? "macbox failed"
            throw NSError(domain: "MacBox", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: message])
        }
        return data
    }
}

struct VisualEffect: NSViewRepresentable {
    let material: NSVisualEffectView.Material
    let blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}

struct TitlebarDoubleClickZone: NSViewRepresentable {
    func makeNSView(context: Context) -> DoubleClickZoomView {
        DoubleClickZoomView()
    }

    func updateNSView(_ nsView: DoubleClickZoomView, context: Context) {}
}

final class DoubleClickZoomView: NSView {
    private var restoreFrame: NSRect?

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            guard let window, let screen = window.screen ?? NSScreen.main else { return }
            if let restoreFrame {
                window.setFrame(restoreFrame, display: true, animate: true)
                self.restoreFrame = nil
            } else {
                self.restoreFrame = window.frame
                window.setFrame(screen.visibleFrame, display: true, animate: true)
            }
        } else {
            super.mouseDown(with: event)
        }
    }
}

enum SidePanel {
    case history
    case files
}

enum LayoutMode: String, CaseIterable, Identifiable {
    case single
    case twoColumns
    case grid2x2
    case grid3x2

    var id: String { rawValue }

    var title: String {
        switch self {
        case .single: return "Single"
        case .twoColumns: return "Two Columns"
        case .grid2x2: return "2 x 2"
        case .grid3x2: return "3 x 2"
        }
    }

    var icon: String {
        switch self {
        case .single: return "rectangle"
        case .twoColumns: return "rectangle.split.2x1"
        case .grid2x2: return "square.grid.2x2"
        case .grid3x2: return "rectangle.grid.3x2"
        }
    }

    var capacity: Int {
        switch self {
        case .single: return 1
        case .twoColumns: return 2
        case .grid2x2: return 4
        case .grid3x2: return 6
        }
    }
}

struct ContentView: View {
    @StateObject private var store = MacBoxStore()
    @State private var sidePanel: SidePanel?
    @State private var layoutMode: LayoutMode = .single

    private var selected: Session? {
        store.sessions.first { $0.id == store.selectedID }
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            VisualEffect(material: .underWindowBackground, blendingMode: .behindWindow)
                .ignoresSafeArea()
            Color.black.opacity(0.18)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                TabStrip(store: store, selected: selected, sidePanel: $sidePanel, layoutMode: $layoutMode)
                TerminalPage(store: store, session: selected, sidePanel: $sidePanel, layoutMode: layoutMode)
            }
            .background(.black.opacity(0.04))
            .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .stroke(.white.opacity(0.12), lineWidth: 1)
            )
            .padding(8)
        }
        .frame(minWidth: 920, minHeight: 580)
        .onAppear { store.refresh() }
    }
}

struct TabStrip: View {
    @ObservedObject var store: MacBoxStore
    let selected: Session?
    @Binding var sidePanel: SidePanel?
    @Binding var layoutMode: LayoutMode

    var body: some View {
        ZStack {
            VisualEffect(material: .hudWindow, blendingMode: .withinWindow)
                .opacity(0.72)
            Color.black.opacity(0.36)

            HStack(spacing: 6) {
                HStack(spacing: 6) {
                    ForEach(store.sessions.prefix(6)) { session in
                        SessionTab(
                            session: session,
                            selected: store.selectedID == session.id,
                            onClose: {
                                store.closeVisibleSession(session)
                                sidePanel = nil
                            }
                        )
                            .onTapGesture {
                                store.selectedID = session.id
                                sidePanel = nil
                            }
                    }
                    NewSessionButton(store: store, sidePanel: $sidePanel)
                }
                .padding(.leading, 10)

                Spacer(minLength: 8)

                Button {
                    withAnimation(.snappy) {
                        sidePanel = sidePanel == .history ? nil : .history
                    }
                } label: {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.system(size: 14, weight: .medium))
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(.plain)
                .help("History")

                Button {
                    withAnimation(.snappy) {
                        sidePanel = sidePanel == .files ? nil : .files
                    }
                } label: {
                    Image(systemName: "folder")
                        .font(.system(size: 14, weight: .medium))
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(.plain)
                .help("Files")

                Menu {
                    ForEach(LayoutMode.allCases) { mode in
                        Button {
                            layoutMode = mode
                        } label: {
                            Label(mode.title, systemImage: mode.icon)
                        }
                    }
                } label: {
                    Image(systemName: layoutMode.icon)
                        .font(.system(size: 14, weight: .medium))
                        .frame(width: 28, height: 28)
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .help("Layout")
            }
            .padding(.trailing, 10)
        }
        .frame(height: 44)
    }
}

struct NewSessionButton: View {
    @ObservedObject var store: MacBoxStore
    @Binding var sidePanel: SidePanel?

    var body: some View {
        Menu {
            Button("New Session") {
                store.createSession(sandboxed: false)
                sidePanel = nil
            }
            Button("New Sandbox Session") {
                store.createSession(sandboxed: true)
                sidePanel = nil
            }
        } label: {
            Image(systemName: "plus")
                .font(.system(size: 16, weight: .medium))
                .frame(width: 30, height: 30)
                .background(Color.white.opacity(0.05))
                .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("New Session")
    }
}

struct SessionTab: View {
    let session: Session
    let selected: Bool
    let onClose: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 8) {
            Text(session.name)
                .font(.system(size: 13, weight: selected ? .semibold : .medium))
                .lineLimit(1)
            if hovering {
                Button(action: onClose) {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: 18, height: 18)
                        .background(Color.red.opacity(0.88))
                        .clipShape(Circle())
                }
                .buttonStyle(.plain)
                .transition(.opacity)
            }
            if session.isSandboxed, (session.pendingChanges ?? 0) > 0 {
                Text("\(session.pendingChanges ?? 0)")
                    .font(.caption2.weight(.bold))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(.cyan.opacity(0.2))
                    .clipShape(Capsule())
            }
        }
        .padding(.horizontal, 13)
        .frame(minWidth: 144, maxWidth: 220, minHeight: 32)
        .foregroundStyle(selected ? .white : .white.opacity(0.72))
        .background(selected ? Color.black.opacity(0.96) : Color(red: 0.20, green: 0.20, blue: 0.20).opacity(0.82))
        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .stroke(selected ? .white.opacity(0.08) : .black.opacity(0.22), lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onHover { inside in
            withAnimation(.easeInOut(duration: 0.12)) {
                hovering = inside
            }
        }
    }
}

struct TerminalPage: View {
    @ObservedObject var store: MacBoxStore
    let session: Session?
    @Binding var sidePanel: SidePanel?
    let layoutMode: LayoutMode

    var body: some View {
        if let session {
            HStack(spacing: 0) {
                TerminalLayout(store: store, fallbackSession: session, layoutMode: layoutMode)

                if sidePanel == .history {
                    Divider().opacity(0.28)
                    HistorySidebar(store: store)
                        .frame(width: 320)
                        .transition(.move(edge: .trailing).combined(with: .opacity))
                } else if sidePanel == .files {
                    Divider().opacity(0.28)
                    FileSidebar(store: store, session: session)
                        .frame(width: 340)
                        .transition(.move(edge: .trailing).combined(with: .opacity))
                }
            }
        } else {
            EmptyTerminal(store: store)
        }
    }
}

struct TerminalLayout: View {
    @ObservedObject var store: MacBoxStore
    let fallbackSession: Session
    let layoutMode: LayoutMode

    private var panes: [Session] {
        let open = store.sessions.isEmpty ? [fallbackSession] : store.sessions
        return Array(open.prefix(layoutMode.capacity))
    }

    var body: some View {
        Group {
            switch layoutMode {
            case .single:
                pane(for: fallbackSession)
            case .twoColumns:
                HStack(spacing: 1) {
                    ForEach(panes) { session in
                        pane(for: session)
                    }
                    if panes.count == 1 {
                        EmptyPane()
                    }
                }
            case .grid2x2:
                LazyVGrid(columns: gridColumns(2), spacing: 1) {
                    ForEach(0..<4, id: \.self) { index in
                        if index < panes.count {
                            pane(for: panes[index])
                        } else {
                            EmptyPane()
                        }
                    }
                }
                .background(Color(red: 0.08, green: 0.08, blue: 0.08))
            case .grid3x2:
                LazyVGrid(columns: gridColumns(3), spacing: 1) {
                    ForEach(0..<6, id: \.self) { index in
                        if index < panes.count {
                            pane(for: panes[index])
                        } else {
                            EmptyPane()
                        }
                    }
                }
                .background(Color(red: 0.08, green: 0.08, blue: 0.08))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }

    private func gridColumns(_ count: Int) -> [GridItem] {
        Array(repeating: GridItem(.flexible(), spacing: 1), count: count)
    }

    private func pane(for session: Session) -> some View {
        TerminalSurface(store: store, session: session)
            .id(session.id)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
            .simultaneousGesture(TapGesture().onEnded {
                store.selectedID = session.id
            })
    }
}

struct EmptyPane: View {
    var body: some View {
        ZStack {
            Color.black
            Text("Open a session")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.white.opacity(0.28))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct TerminalSurface: View {
    @ObservedObject var store: MacBoxStore
    let session: Session

    var body: some View {
        TerminalPTYView(
            launch: store.terminalLaunch(for: session),
            banner: banner
        )
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var banner: String {
        var base: [(String, Bool)] = [
            ("MacBox Terminal 0.1.0", false),
            ("Interactive PTY session. Type commands directly.", true),
            ("", true),
            ("Session: \(session.name)  ·  \(session.isSandboxed ? "sandbox" : "session")", true)
        ]
        if session.isSandboxed {
            base.append(("Sandbox is enabled. Writes are staged until Apply Changes.", false))
            base.append(("Use: vpath /real/path  ·  mb-changes  ·  mb-apply", true))
        } else {
            base.append(("Session ready.", false))
        }
        base.append(("", true))
        return base.map(\.0).joined(separator: "\n") + "\n"
    }
}

struct TerminalPTYView: NSViewRepresentable {
    let launch: TerminalLaunch
    let banner: String

    func makeNSView(context: Context) -> TerminalPTYTextView {
        let view = TerminalPTYTextView()
        view.configure(launch: launch, banner: banner)
        return view
    }

    func updateNSView(_ nsView: TerminalPTYTextView, context: Context) {
        nsView.focusIfNeeded()
    }
}

final class TerminalPTYTextView: NSScrollView {
    private let textView = TerminalTextView()
    private var process: Process?
    private var masterFile: FileHandle?
    private var masterFD: Int32 = -1
    private var slaveFD: Int32 = -1
    private var configured = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        drawsBackground = true
        backgroundColor = .black
        borderType = .noBorder
        hasVerticalScroller = true
        autohidesScrollers = true

        textView.minSize = .zero
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = true
        textView.autoresizingMask = [.width]
        textView.textContainer?.containerSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = false
        textView.drawsBackground = true
        textView.backgroundColor = .black
        textView.textColor = .white
        textView.insertionPointColor = .white
        textView.font = .monospacedSystemFont(ofSize: 16, weight: .semibold)
        textView.isEditable = false
        textView.isSelectable = true
        documentView = textView
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        masterFile?.readabilityHandler = nil
        if let process, process.isRunning {
            process.terminate()
        }
    }

    func configure(launch: TerminalLaunch, banner: String) {
        guard !configured else { return }
        configured = true
        append(banner)
        startProcess(launch: launch)
    }

    func focusIfNeeded() {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.window?.firstResponder !== self.textView else { return }
            self.window?.makeFirstResponder(self.textView)
        }
    }

    private func startProcess(launch: TerminalLaunch) {
        var term = termios()
        tcgetattr(STDIN_FILENO, &term)
        var size = winsize()
        size.ws_row = 40
        size.ws_col = 120

        guard openpty(&masterFD, &slaveFD, nil, &term, &size) == 0 else {
            append("Failed to create pty: \(String(cString: strerror(errno)))\n")
            return
        }

        let master = FileHandle(fileDescriptor: masterFD, closeOnDealloc: true)
        let slaveIn = FileHandle(fileDescriptor: slaveFD, closeOnDealloc: false)
        let slaveOut = FileHandle(fileDescriptor: slaveFD, closeOnDealloc: false)
        let slaveErr = FileHandle(fileDescriptor: slaveFD, closeOnDealloc: true)
        masterFile = master
        textView.inputHandler = { [weak self] data in
            self?.writeToPTY(data)
        }

        let proc = Process()
        proc.executableURL = launch.executable
        proc.arguments = launch.arguments
        proc.currentDirectoryURL = launch.currentDirectory
        proc.environment = launch.environment
        proc.standardInput = slaveIn
        proc.standardOutput = slaveOut
        proc.standardError = slaveErr
        proc.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                self?.append("\n[process exited: \(finished.terminationStatus)]\n")
            }
        }

        master.readabilityHandler = { [weak self] file in
            let data = file.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                self?.applyTerminalOutput(text)
            }
        }

        do {
            try proc.run()
            process = proc
        } catch {
            append("Failed to start shell: \(error.localizedDescription)\n")
        }
    }

    private func writeToPTY(_ data: Data) {
        do {
            try masterFile?.write(contentsOf: data)
        } catch {
            append("\n[input error: \(error.localizedDescription)]\n")
        }
    }

    private func append(_ text: String) {
        guard !text.isEmpty else { return }
        let attributed = NSAttributedString(
            string: text,
            attributes: [
                .foregroundColor: NSColor.white,
                .font: NSFont.monospacedSystemFont(ofSize: 16, weight: .semibold)
            ]
        )
        textView.textStorage?.append(attributed)
        textView.scrollRangeToVisible(NSRange(location: textView.string.count, length: 0))
    }

    private func deletePreviousCharacter() {
        guard let storage = textView.textStorage, storage.length > 0 else { return }
        storage.deleteCharacters(in: NSRange(location: storage.length - 1, length: 1))
        textView.scrollRangeToVisible(NSRange(location: storage.length, length: 0))
    }

    private func applyTerminalOutput(_ text: String) {
        var iterator = text.makeIterator()
        while let char = iterator.next() {
            if char == "\u{1B}" {
                skipEscapeSequence(&iterator)
                continue
            }
            if char == "\r" || char == "\n" {
                append("\n")
                continue
            }
            if char == "\u{08}" || char == "\u{7F}" {
                deletePreviousCharacter()
                continue
            }
            append(String(char))
        }
    }

    private func skipEscapeSequence(_ iterator: inout String.Iterator) {
        guard let next = iterator.next() else { return }
        if next == "[" {
            while let char = iterator.next() {
                if ("@"..."~").contains(char) { break }
            }
        }
    }

    private func stopProcess() {
        masterFile?.readabilityHandler = nil
        if let process, process.isRunning {
            process.terminate()
        }
        process = nil
        masterFile = nil
    }
}

final class TerminalTextView: NSTextView {
    var inputHandler: ((Data) -> Void)?

    override var acceptsFirstResponder: Bool { true }

    override func keyDown(with event: NSEvent) {
        if event.modifierFlags.contains(.command) {
            super.keyDown(with: event)
            return
        }

        if let data = data(for: event) {
            inputHandler?(data)
        }
    }

    override func paste(_ sender: Any?) {
        guard let string = NSPasteboard.general.string(forType: .string),
              let data = string.data(using: .utf8) else { return }
        inputHandler?(data)
    }

    private func data(for event: NSEvent) -> Data? {
        let sequence: String?
        switch event.keyCode {
        case 36:
            sequence = "\r"
        case 51:
            sequence = "\u{7F}"
        case 123:
            sequence = "\u{1B}[D"
        case 124:
            sequence = "\u{1B}[C"
        case 125:
            sequence = "\u{1B}[B"
        case 126:
            sequence = "\u{1B}[A"
        case 53:
            sequence = "\u{1B}"
        default:
            sequence = event.characters
        }
        return sequence?.data(using: .utf8)
    }
}

struct HistorySidebar: View {
    @ObservedObject var store: MacBoxStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("History")
                .font(.headline)
            Text("Open a previous session in this window.")
                .font(.caption)
                .foregroundStyle(.secondary)

            Divider().opacity(0.28)

            ScrollView {
                VStack(spacing: 8) {
                    ForEach(store.allSessions) { session in
                        Button {
                            store.openSessionFromHistory(session)
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(session.name)
                                        .font(.system(size: 13, weight: .medium))
                                    Text(session.isSandboxed ? "sandbox" : "session")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if store.visibleSessionIDs.contains(session.id) {
                                    Image(systemName: "checkmark")
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .padding(10)
                            .background(.white.opacity(0.06))
                            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(16)
        .background(Color(red: 0.08, green: 0.08, blue: 0.08))
    }
}

struct FileSidebar: View {
    @ObservedObject var store: MacBoxStore
    let session: Session

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Files")
                    .font(.headline)
                Spacer()
                Text("\(session.changes.count)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if session.isSandboxed {
                InfoLine(title: "Overlay", value: session.overlayPath ?? "-")
                InfoLine(title: "Writable roots", value: (session.writeRoots ?? ["/"]).joined(separator: ", "))
            } else {
                InfoLine(title: "Session", value: "Regular session")
            }

            Divider().opacity(0.28)

            if session.changes.isEmpty {
                Spacer()
                VStack(spacing: 8) {
                    Image(systemName: "doc")
                        .font(.system(size: 30))
                        .foregroundStyle(.secondary)
                    Text(session.isSandboxed ? "No pending file changes" : "File change tracking is available for sandbox sessions")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                Spacer()
            } else {
                List(session.changes) { item in
                    HStack(spacing: 9) {
                        Image(systemName: item.change == "delete" ? "trash" : icon(for: item.kind))
                            .frame(width: 18)
                            .foregroundStyle(item.change == "delete" ? .red : .cyan)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(item.fileName)
                                .font(.system(size: 13, weight: .medium))
                                .lineLimit(1)
                            Text(item.realPath)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    .padding(.vertical, 3)
                }
                .scrollContentBackground(.hidden)
            }

            Button {
                store.applyChanges(for: session)
            } label: {
                Label("Apply Changes", systemImage: "checkmark.circle")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(session.changes.isEmpty)
        }
        .padding(16)
        .background(.white.opacity(0.06))
    }

    private func icon(for kind: String) -> String {
        switch kind {
        case "dir": return "folder"
        case "symlink": return "link"
        default: return "doc.text"
        }
    }
}

struct EmptyTerminal: View {
    @ObservedObject var store: MacBoxStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("$ macbox")
                .font(.system(size: 14, design: .monospaced))
            Text("Starting a session...")
                .font(.system(size: 13, design: .monospaced))
                .foregroundStyle(.secondary)
            if let message = store.errorMessage, !message.isEmpty {
                Text(message)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(.red)
                    .lineLimit(3)
            }
            Spacer()
        }
        .padding(18)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.black.opacity(0.36))
    }
}

struct InfoLine: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 12, design: .monospaced))
                .lineLimit(2)
                .textSelection(.enabled)
        }
    }
}

struct StatusDot: View {
    let active: Bool

    var body: some View {
        Circle()
            .fill(active ? .green : .secondary)
            .frame(width: 8, height: 8)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow?
    private var doubleClickMonitor: Any?
    private var restoreFrame: NSRect?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let view = ContentView()
            .preferredColorScheme(.dark)

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1040, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "MacBox"
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.isOpaque = false
        window.backgroundColor = .clear
        window.center()
        window.contentView = NSHostingView(rootView: view)
        window.makeKeyAndOrderFront(nil)
        self.window = window
        self.doubleClickMonitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self, weak window] event in
            guard let self, let window, event.window === window, event.clickCount == 2 else {
                return event
            }
            let point = event.locationInWindow
            let inTopBlankArea = point.y > window.frame.height - 64 && point.x > 130
            if inTopBlankArea {
                self.toggleWindowZoom(window)
                return nil
            }
            return event
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    private func toggleWindowZoom(_ window: NSWindow) {
        if let restoreFrame {
            window.setFrame(restoreFrame, display: true, animate: true)
            self.restoreFrame = nil
            return
        }
        guard let screen = window.screen ?? NSScreen.main else { return }
        restoreFrame = window.frame
        window.setFrame(screen.visibleFrame, display: true, animate: true)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
