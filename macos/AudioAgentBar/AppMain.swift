import AppKit
import SwiftUI
import Combine
import AVFoundation

struct AppConfiguration {
    let project: URL
    let port: Int
    func dataDirectory() async throws -> URL { try await StorageConfiguration.resolveAsync(projectRoot: project) }
    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    static func load() throws -> AppConfiguration {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "AudioAgentProjectPath") as? String,
              path.hasPrefix("/"),
              let port = Bundle.main.object(forInfoDictionaryKey: "AudioAgentPort") as? Int,
              (1024...65535).contains(port) else {
            throw BarLaunchError.message("应用配置不完整，请在 Audio Agent 项目中重新运行构建脚本。")
        }
        return AppConfiguration(project: URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL,
                                port: port)
    }
}

enum BarLaunchError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(value) = self { return value }; return nil }
}

private final class LocalOnlySession: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

/// Starting this service never starts capture. Only the explicit listening
/// control in the menu bar or web console enables capture and automatic ASR.
/// The shared service intentionally survives closing this menu bar client.
@MainActor
final class BackendService {
    let configuration: AppConfiguration
    private let delegate = LocalOnlySession()
    private lazy var session: URLSession = {
        let value = URLSessionConfiguration.ephemeral
        value.urlCache = nil
        value.httpCookieStorage = nil
        value.connectionProxyDictionary = [:]
        value.timeoutIntervalForRequest = 3
        value.timeoutIntervalForResource = 4
        return URLSession(configuration: value, delegate: delegate, delegateQueue: nil)
    }()
    private var process: Process?

    init(configuration: AppConfiguration) { self.configuration = configuration }

    private func verifyService() async throws {
        // The Web console can change the saved directory while this client is
        // open. Never retain the root resolved at application launch.
        let expectedDirectory = try await configuration.dataDirectory()
        let (data, response) = try await session.data(from: configuration.baseURL.appendingPathComponent("api/status"))
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw BarLaunchError.message("端口 \(configuration.port) 的服务暂不可用。请检查控制台；应用不会关闭或替换已有服务。")
        }
        let server = (http.value(forHTTPHeaderField: "Server") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard server == "AudioAgent" || server.hasPrefix("AudioAgent/") else {
            throw BarLaunchError.message("端口 \(configuration.port) 被其他服务使用，无法连接 Audio Agent。")
        }
        guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let storage = value["storage"] as? [String: Any],
              let root = storage["root"] as? String,
              value["live"] is [String: Any] else {
            throw BarLaunchError.message("此端口的音频库与本应用不同，请先检查后台的数据目录。")
        }
        let actualDirectory = await Task.detached(priority: .userInitiated) {
            URL(fileURLWithPath: root).resolvingSymlinksInPath().standardizedFileURL
        }.value
        guard actualDirectory.path == expectedDirectory.path else {
            throw BarLaunchError.message("此端口的音频库与本应用不同，请先检查后台的数据目录。")
        }
    }

    func ensureRunning() async throws {
        do { try await verifyService(); return }
        catch let error as URLError where error.code == .cannotConnectToHost {
            // Only a refused connection permits a new service process.
        }
        catch { throw error }

        if process?.isRunning != true {
            let dataDirectory = try await configuration.dataDirectory()
            let python = configuration.project.appendingPathComponent(".venv/bin/python")
            guard FileManager.default.isExecutableFile(atPath: python.path),
                  FileManager.default.fileExists(atPath: configuration.project.appendingPathComponent("audio_agent/web.py").path) else {
                throw BarLaunchError.message("未找到本地运行环境。项目可能已移动，请恢复项目目录并重新构建应用。")
            }
            let logs = dataDirectory.appendingPathComponent("logs", isDirectory: true)
            try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let log = logs.appendingPathComponent("menu-bar-service.log")
            if !FileManager.default.fileExists(atPath: log.path) {
                FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            let handle = try FileHandle(forWritingTo: log)
            try handle.seekToEnd()
            defer { try? handle.close() }
            let child = Process()
            child.executableURL = python
            child.currentDirectoryURL = configuration.project
            // Python resolves the same saved setting; a forced --data-dir
            // would pin future launches to a library the Web UI moved away from.
            child.arguments = ["-m", "audio_agent", "web", "--port", String(configuration.port)]
            var environment = ProcessInfo.processInfo.environment
            // Finder has a minimal PATH. Existing bundled tools and models stay local.
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            environment["PYTHONUNBUFFERED"] = "1"
            child.environment = environment
            child.standardInput = FileHandle.nullDevice
            child.standardOutput = handle
            child.standardError = handle
            try child.run()
            process = child
        }
        for _ in 0..<40 {
            try await Task.sleep(nanoseconds: 250_000_000)
            do { try await verifyService(); return }
            catch let error as URLError where error.code == .cannotConnectToHost {
                if process?.isRunning == false {
                    throw BarLaunchError.message("本地服务启动失败。请检查当前资料库的 logs/menu-bar-service.log。")
                }
            }
            catch { throw error }
        }
        throw BarLaunchError.message("本地服务尚未就绪，请稍后点击刷新。")
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private var store: DocumentStore!
    private var backend: BackendService!
    private var configuration: AppConfiguration!
    private var timer: Timer?
    private var tick = 0
    private var refreshing = false
    private var controlling = false
    private var observation: AnyCancellable?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        do { configuration = try AppConfiguration.load() }
        catch {
            let alert = NSAlert(); alert.messageText = "Audio Agent 无法启动"; alert.informativeText = error.localizedDescription
            alert.runModal(); NSApp.terminate(nil); return
        }
        backend = BackendService(configuration: configuration)
        store = DocumentStore(client: DocumentClient(baseURL: configuration.baseURL))
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.autosaveName = "AudioAgentMenuBar"
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Audio Agent 最近转写")
            button.image?.isTemplate = true
            button.setAccessibilityLabel("Audio Agent 最近转写")
            button.target = self
            button.action = #selector(togglePopover)
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        popover.behavior = .transient
        popover.delegate = self
        popover.contentSize = NSSize(width: 440, height: 610)
        popover.contentViewController = NSHostingController(rootView: BarContentView(
            store: store,
            onRefresh: { [weak self] in self?.requestRefresh() },
            onToggleListening: { [weak self] in self?.requestListeningToggle() },
            onOpenConsole: { [weak self] in self?.openConsole() },
            onOpenStorage: { [weak self] in self?.openStorage() },
            onQuit: { NSApp.terminate(nil) }))
        observation = store.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async { self?.updateStatusIcon() }
        }
        updateStatusIcon()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.tick += 1
                if self.refreshing { return }
                if self.popover.isShown { await self.store.refresh() }
                else if self.tick % 5 == 0 { await self.store.refreshStatus() }
            }
        }
        requestRefresh()
    }

    private func updateStatusIcon() {
        guard let store, let button = statusItem?.button else { return }
        button.image = NSImage(systemSymbolName: store.isListening ? "waveform.circle.fill" : "waveform",
                               accessibilityDescription: "Audio Agent 最近转写")
        button.image?.isTemplate = true
        button.toolTip = "Audio Agent · \(store.statusText) · 点击查看与复制转写"
    }

    func requestRefresh() {
        guard !refreshing else { return }
        refreshing = true
        Task { @MainActor in
            defer { refreshing = false }
            do {
                try await backend.ensureRunning()
                await store.refresh()
                if !store.liveIsBusy { await store.loadDevices() }
            }
            catch { store.reportConnectionError(error.localizedDescription) }
        }
    }

    func requestListeningToggle() {
        guard !controlling, store.canToggleLive else { return }
        // Preserve the button's explicit intent across asynchronous checks;
        // a stale Stop must never become a Start after a service restart.
        let wantsStart = !store.liveIsBusy
        controlling = true
        Task { @MainActor in
            defer { controlling = false }
            do {
                if wantsStart {
                    try await backend.ensureRunning()
                    guard await microphoneAccessGranted() else {
                        store.reportListeningError("麦克风访问未获允许。请在系统设置 → 隐私与安全性 → 麦克风中允许 Audio Agent，然后再点击开始。")
                        return
                    }
                }
                await store.setListening(wantsStart)
            } catch { store.reportConnectionError(error.localizedDescription) }
        }
    }

    private func microphoneAccessGranted() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined:
            // Ask only after an explicit Start. Merely reading/copying text
            // must not request microphone permission or open an audio device.
            return await withCheckedContinuation { continuation in
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    continuation.resume(returning: granted)
                }
            }
        default: return false
        }
    }

    @objc private func toggleListeningFromMenu() { requestListeningToggle() }

    @objc private func togglePopover() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            let listening = menu.addItem(withTitle: store.liveButtonTitle, action: #selector(toggleListeningFromMenu), keyEquivalent: "")
            listening.target = self
            listening.isEnabled = store.canToggleLive && !controlling
            menu.autoenablesItems = false
            menu.addItem(.separator())
            let console = menu.addItem(withTitle: "打开后台控制台", action: #selector(openConsole), keyEquivalent: "")
            console.target = self
            menu.addItem(.separator())
            let quit = menu.addItem(withTitle: "退出菜单栏（后台继续运行）", action: #selector(quit), keyEquivalent: "q")
            quit.target = self
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
            return
        }
        if popover.isShown { popover.performClose(nil) }
        else { showPopover() }
    }

    private func showPopover() {
        guard let button = statusItem.button else { return }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        requestRefresh()
    }

    @objc private func openConsole() {
        popover.performClose(nil)
        NSWorkspace.shared.open(configuration.baseURL)
    }

    private func openStorage() {
        popover.performClose(nil)
        var components = URLComponents(url: configuration.baseURL, resolvingAgainstBaseURL: false)
        components?.fragment = "storage"
        if let url = components?.url { NSWorkspace.shared.open(url) }
    }

    @objc private func quit() { NSApp.terminate(nil) }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPopover(); return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        // Do not signal the shared Python service: it may be recording for the
        // web console. It is also intentionally independent if we launched it.
    }
}

@main
struct AudioAgentMenuBar {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
