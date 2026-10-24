// Compile together with DocumentClient.swift, then run the resulting executable.
// URLProtocol supplies every response in memory: no sockets, models or capture.
import Foundation

private struct TestFailure: Error, CustomStringConvertible {
    let description: String
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure(description: message) }
}

private struct StubResponse {
    let json: [String: Any]
    var status = 200
    var server = "AudioAgent "
    var failure: Error?
    var delay: TimeInterval = 0
}

private final class TestProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var responses: [StubResponse] = []
    private static var requests: [URLRequest] = []

    static func install(_ values: [StubResponse]) {
        lock.lock()
        responses = values
        requests = []
        lock.unlock()
    }

    static var received: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        Self.lock.lock()
        Self.requests.append(request)
        let response = Self.responses.isEmpty ? nil : Self.responses.removeFirst()
        Self.lock.unlock()
        guard let response else {
            client?.urlProtocol(self, didFailWithError: URLError(.resourceUnavailable))
            return
        }
        let deliver = { [self] in
            if let error = response.failure {
                client?.urlProtocol(self, didFailWithError: error)
                return
            }
            do {
                let data = try JSONSerialization.data(withJSONObject: response.json)
                let http = HTTPURLResponse(url: request.url!, statusCode: response.status, httpVersion: "HTTP/1.1",
                                           headerFields: ["Server": response.server, "Content-Type": "application/json"])!
                client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch {
                client?.urlProtocol(self, didFailWithError: error)
            }
        }
        if response.delay > 0 {
            DispatchQueue.global().asyncAfter(deadline: .now() + response.delay, execute: deliver)
        } else {
            deliver()
        }
    }
}

private func entry(_ id: Int, text: String? = nil) -> [String: Any] {
    ["id": String(format: "%032x", id), "clip_id": NSNull(), "text": text ?? "正文 \(id)",
     "start_time": Double(id * 10), "end_time": Double(id * 10 + 5),
     "recording_available": false, "display_name": "测试录音"]
}

private func page(_ ids: [Int], total: Int? = nil, next: String? = nil,
                  revision: String = "r1", snapshot: Double = 1000) -> StubResponse {
    StubResponse(json: ["items": ids.map { entry($0) }, "total": total ?? ids.count,
                        "next_cursor": next as Any? ?? NSNull(), "revision": revision, "snapshot": snapshot])
}

private func status(active: Bool = true, pending: Int = 2) -> StubResponse {
    StubResponse(json: ["live": ["active": active, "pending_count": pending]])
}

private func live(_ phase: String, active: Bool? = nil, pending: Int = 0,
                  language: String = "zh", vad: Bool = true, device: String? = "MacBook Pro麦克风",
                  session: String? = "live-session", error: String? = nil) -> [String: Any] {
    ["state": phase, "active": active ?? ["starting", "recording", "stopping"].contains(phase),
     "pending_count": pending, "device": device as Any? ?? NSNull(), "language": language,
     "vad_enabled": vad, "session_id": session as Any? ?? NSNull(), "last_error": error as Any? ?? NSNull()]
}

private func liveStatus(_ phase: String, pending: Int = 0, session: String? = "live-session") -> StubResponse {
    StubResponse(json: ["live": live(phase, pending: pending, session: session),
                        "models": [["name": "local-model"]], "vad": ["available": true]])
}

private func liveReply(_ phase: String, pending: Int = 0) -> StubResponse {
    StubResponse(json: ["live": live(phase, pending: pending)], status: 202)
}

private func devices() -> StubResponse {
    StubResponse(json: ["items": [["index": 0, "name": "Wireless Mic Rx"], ["index": 1, "name": "MacBook Pro麦克风"]]])
}

private func session(_ token: String) -> StubResponse { StubResponse(json: ["token": token]) }

private func thresholdReply(_ db: Double, pending: Bool = false) -> StubResponse {
    let threshold = pow(10, db / 20)
    return StubResponse(json: ["settings": ["trigger_db": db, "trigger_threshold": threshold,
                                            "release_threshold": threshold * 2 / 3], "pending": pending])
}

private func thresholdStatus(configured: Double, applied: Double, pending: Bool = false) -> StubResponse {
    var value = live("recording")
    value["configured_trigger_db"] = configured
    value["trigger_db"] = applied
    value["settings_pending"] = pending
    return StubResponse(json: ["live": value, "models": [["name": "local"]], "vad": ["available": true]])
}

private func actionBody(_ request: URLRequest) throws -> [String: Any] {
    if let data = request.httpBody { return try JSONSerialization.jsonObject(with: data) as! [String: Any] }
    guard let stream = request.httpBodyStream else { throw TestFailure(description: "POST body missing") }
    stream.open()
    defer { stream.close() }
    var data = Data()
    var bytes = [UInt8](repeating: 0, count: 4096)
    while stream.hasBytesAvailable {
        let count = stream.read(&bytes, maxLength: bytes.count)
        if count <= 0 { break }
        data.append(bytes, count: count)
    }
    return try JSONSerialization.jsonObject(with: data) as! [String: Any]
}

private func waitForRequests(_ count: Int) async throws {
    for _ in 0..<200 {
        if TestProtocol.received.count >= count { return }
        try await Task.sleep(nanoseconds: 5_000_000)
    }
    throw TestFailure(description: "Expected asynchronous request did not arrive")
}

private func client(_ url: String = "http://127.0.0.1:8767") -> DocumentClient {
    DocumentClient(baseURL: URL(string: url)!, protocolClasses: [TestProtocol.self])
}

private func decodedEntry(_ value: [String: Any]) throws -> TranscriptEntry {
    try JSONDecoder().decode(TranscriptEntry.self, from: JSONSerialization.data(withJSONObject: value))
}

private func annotatedPage(_ value: [String: Any], revision: String) -> StubResponse {
    StubResponse(json: ["items": [value], "total": 1, "next_cursor": NSNull(), "revision": revision, "snapshot": 1000])
}

private func fails(_ contains: String? = nil, _ operation: () async throws -> Void) async throws {
    do {
        try await operation()
    } catch {
        if let contains {
            try require(error.localizedDescription.contains(contains), "Unexpected error: \(error.localizedDescription)")
        }
        return
    }
    throw TestFailure(description: "Expected operation to reject an invalid/incomplete response")
}

@MainActor
private func testThresholdSettings() async throws {
    let service = client()
    TestProtocol.install([thresholdReply(-58.1, pending: true)])
    let loaded = try await service.fetchThresholdSettings()
    try require(loaded.settings.triggerDB == -58.1 && loaded.pending, "Settings must decode saved desired dB and pending separately")
    try require(TestProtocol.received.count == 1 && TestProtocol.received[0].httpMethod == "GET"
                && TestProtocol.received[0].url?.path == "/api/live/settings", "Reading threshold must not mutate or start microphone")

    TestProtocol.install([])
    for value in [Double.nan, Double.infinity, -61, -17] {
        try await fails("-60 到 -18") { _ = try await service.saveThresholdSettings(triggerDB: value) }
    }
    try require(TestProtocol.received.isEmpty, "Invalid threshold must be rejected before token/network requests")
    TestProtocol.install([StubResponse(json: ["settings": ["trigger_db": -32, "trigger_threshold": 0.5,
                                                          "release_threshold": 0.1], "pending": false])])
    try await fails("格式无效") { _ = try await service.fetchThresholdSettings() }

    let badToken = StubResponse(json: ["error": ["code": "invalid_token", "message": "expired"]], status: 403)
    TestProtocol.install([session("old-settings-token"), badToken, session("new-settings-token"), thresholdReply(-32)])
    _ = try await service.saveThresholdSettings(triggerDB: -32)
    let savedRequests = TestProtocol.received
    try require(savedRequests.map { $0.url!.path } == ["/api/session", "/api/live/settings", "/api/session", "/api/live/settings"],
                "Settings should reuse the one-time invalid_token retry contract")
    let body = try actionBody(savedRequests.last!)
    try require(body.count == 1 && body["trigger_db"] as? Double == -32, "Settings POST must contain only trigger_db")
    try require(savedRequests.last?.value(forHTTPHeaderField: "X-Audio-Agent-Token") == "new-settings-token",
                "Settings write must carry in-memory session token")
    TestProtocol.install([thresholdReply(-40)])
    try await fails("格式无效") { _ = try await service.saveThresholdSettings(triggerDB: -32) }
    try require(TestProtocol.received.count == 1, "An acknowledgement for a different threshold must not be accepted or retried")

    let store = DocumentStore(client: client())
    TestProtocol.install([])
    try require(!store.thresholdLoaded && store.thresholdConfiguredDB == nil && store.thresholdAppliedDB == nil,
                "Initial placeholder must not pretend a threshold has been saved or applied")
    TestProtocol.install([thresholdStatus(configured: -58.1, applied: -58.1), page([1])])
    await store.refresh()
    try require(store.thresholdLoaded && store.thresholdDraftDB == -58.1 && store.thresholdConfiguredDB == -58.1
                && store.thresholdAppliedDB == -58.1 && !store.thresholdHasUnsavedChanges && store.canCopyAll,
                "Initial status must load actual server settings without changing them")
    try require(TestProtocol.received.allSatisfy { $0.httpMethod == "GET" }, "Status synchronization must never auto-save a default")
    store.beginThresholdEditing()
    TestProtocol.install([])
    for value in [-50.0, -45, -40, -35, -32] { store.thresholdDraftDB = value }
    try require(TestProtocol.received.isEmpty, "Slider frames must not each issue a settings write")
    TestProtocol.install([thresholdStatus(configured: -45, applied: -58.1, pending: true)])
    await store.refreshStatus()
    try require(store.thresholdDraftDB == -32 && store.thresholdConfiguredDB == -45 && store.thresholdAppliedDB == -58.1
                && store.thresholdIsEditing && store.thresholdHasUnsavedChanges,
                "Polling may update applied/configured values but must preserve a dragging draft")
    TestProtocol.install([session("store-threshold-token"), thresholdReply(-32, pending: true)])
    await store.endThresholdEditing()
    try require(TestProtocol.received.filter { $0.httpMethod == "POST" }.count == 1,
                "Ending a drag should save once")
    try require(!store.thresholdSaving && !store.thresholdIsEditing && !store.thresholdHasUnsavedChanges
                && store.thresholdConfiguredDB == -32 && store.thresholdAppliedDB == -58.1 && store.thresholdSettingsPending,
                "Pending save must retain old applied threshold until a segment boundary")
    try require(store.thresholdStatusText.contains("下一段生效"), "Pending state must be visible instead of claiming immediate application")
    TestProtocol.install([thresholdStatus(configured: -32, applied: -32)])
    await store.refreshStatus()
    try require(!store.thresholdSettingsPending && store.thresholdAppliedDB == -32, "Later live status confirms application")

    store.thresholdDraftDB = -35  // Keyboard/AX can create an unsaved draft without a drag callback.
    TestProtocol.install([thresholdStatus(configured: -44.4, applied: -44.4)])
    await store.refreshStatus()
    try require(store.thresholdDraftDB == -35 && store.thresholdHasUnsavedChanges && store.canSaveThreshold,
                "An unsubmitted keyboard/preset draft must survive remote settings changes")
    TestProtocol.install([StubResponse(json: [:], failure: URLError(.timedOut))])
    await store.saveThreshold()
    try require(TestProtocol.received.count == 1 && store.thresholdError != nil && store.thresholdConfiguredDB == -44.4
                && store.thresholdAppliedDB == -44.4 && store.thresholdDraftDB == -35 && store.canSaveThreshold,
                "Failed save must preserve draft and last confirmed values, allowing explicit retry")
    try require(store.canCopyAll && store.error == nil && store.isListening,
                "Settings errors must not disconnect known microphone state or disable copying")
    TestProtocol.install([thresholdReply(-35)])
    await store.saveThreshold()
    try require(store.thresholdError == nil && !store.thresholdHasUnsavedChanges && store.thresholdAppliedDB == -35,
                "Explicit retry can confirm and apply the retained draft")

    var oldRead = thresholdReply(-48)
    oldRead.delay = 0.15
    TestProtocol.install([oldRead, thresholdReply(-30)])
    async let readBeforeSave: Void = store.refreshThresholdSettings()
    try await waitForRequests(1)
    store.thresholdDraftDB = -30
    await store.saveThreshold()
    _ = await readBeforeSave
    try require(store.thresholdConfiguredDB == -30 && store.thresholdAppliedDB == -30 && store.thresholdDraftDB == -30,
                "Delayed settings GET must not roll back a completed POST")

    var oldStatus = thresholdStatus(configured: -55, applied: -55)
    oldStatus.delay = 0.15
    TestProtocol.install([oldStatus, thresholdReply(-28, pending: true)])
    async let statusBeforeSave: Void = store.refreshStatus()
    try await waitForRequests(1)
    store.thresholdDraftDB = -28
    await store.saveThreshold()
    _ = await statusBeforeSave
    try require(store.thresholdConfiguredDB == -28 && store.thresholdAppliedDB == -30 && store.thresholdSettingsPending,
                "Delayed ordinary status must not overwrite configured/applied/pending after a POST")

    var delayedSave = thresholdReply(-32)
    delayedSave.delay = 0.15
    TestProtocol.install([delayedSave, thresholdReply(-36)])
    store.thresholdDraftDB = -32
    async let saving: Void = store.saveThreshold()
    try await waitForRequests(1)
    store.beginThresholdEditing()
    store.thresholdDraftDB = -36
    await store.endThresholdEditing()
    _ = await saving
    let queuedBodies = try TestProtocol.received.map { try actionBody($0)["trigger_db"] as? Double }
    try require(queuedBodies == [-32, -36] && store.thresholdDraftDB == -36 && store.thresholdConfiguredDB == -36,
                "A newer explicit draft during save must survive the response and coalesce into one later save")

    var delayedFailure = StubResponse(json: [:], failure: URLError(.timedOut))
    delayedFailure.delay = 0.1
    TestProtocol.install([delayedFailure])
    store.thresholdDraftDB = -33
    async let duplicateOne: Void = store.saveThreshold()
    async let duplicateTwo: Void = store.saveThreshold()
    _ = await (duplicateOne, duplicateTwo)
    try require(TestProtocol.received.count == 1 && store.thresholdError != nil && store.thresholdDraftDB == -33,
                "Duplicate save callbacks must neither overlap nor blindly resend a failed value")

    let disconnected = DocumentStore(client: client())
    TestProtocol.install([StubResponse(json: [:], failure: URLError(.cannotConnectToHost))])
    await disconnected.refreshThresholdSettings()
    try require(!disconnected.thresholdLoaded && disconnected.thresholdConfiguredDB == nil
                && disconnected.thresholdAppliedDB == nil && disconnected.thresholdError != nil,
                "A failed initial read must not expose placeholder settings as confirmed")
    TestProtocol.install([thresholdReply(-32, pending: true)])
    await disconnected.refreshThresholdSettings()
    try require(disconnected.thresholdConfiguredDB == -32 && disconnected.thresholdAppliedDB == nil
                && disconnected.thresholdSettingsPending, "Pending-only settings response cannot invent an applied value")
    try require(TestProtocol.received.allSatisfy { $0.httpMethod == "GET" }, "Loading settings remains read-only")
}

@main
struct DocumentClientTests {
    @MainActor
    static func main() async throws {
        let service = client()
        TestProtocol.install([page(Array(1...200), total: 203, next: "cursor_one"), page([201, 202, 203], total: 203)])
        let complete = try await service.fetchDocument()
        try require(complete?.items.count == 203, "Must fetch all pages, not just first 200")
        try require(complete?.items.last?.id == String(format: "%032x", 203), "Must keep chronological order")
        let requests = TestProtocol.received
        try require(requests.count == 2, "Expected exactly two pages")
        try require(requests.allSatisfy { $0.httpMethod == "GET" && $0.value(forHTTPHeaderField: "Origin") == nil }, "Read requests must not mutate or send Origin")
        let query = URLComponents(url: requests[1].url!, resolvingAgainstBaseURL: false)!.queryItems!
        try require(query.contains(URLQueryItem(name: "cursor", value: "cursor_one")), "Cursor must be forwarded intact")
        try require(!query.contains { $0.name == "source" }, "Default document must include all sources")
        try require(requests.allSatisfy { $0.timeoutInterval == 8 && $0.httpShouldHandleCookies == false }, "Requests must use timeout and no cookies")

        TestProtocol.install([page([1], revision: "same")])
        let unchanged = try await service.fetchDocument(previousRevision: "same")
        try require(unchanged == nil && TestProtocol.received.count == 1, "Unchanged revision should avoid fetching whole document")

        TestProtocol.install([page([1], total: 2, next: "a"), page([2], total: 2, revision: "new"),
                              page([3, 4], revision: "stable", snapshot: 2000)])
        let retried = try await service.fetchDocument()
        try require(retried?.revision == "stable" && retried?.items.count == 2, "Snapshot changes should restart once")
        try require(TestProtocol.received.count == 3, "One restart should read a fresh first page")

        TestProtocol.install([page([1], total: 2, next: "a"), page([2], total: 3),
                              page([1], total: 2, next: "b"), page([2], total: 2, snapshot: 9999)])
        try await fails("正在更新") { _ = try await service.fetchDocument() }
        try require(TestProtocol.received.count == 4, "Updates retry at most once")

        TestProtocol.install([page([1], total: 2, next: "a"), page([1], total: 2)])
        try await fails("分页不完整") { _ = try await service.fetchDocument() }
        TestProtocol.install([page([1], total: 3, next: "a"), page([2], total: 3, next: "a")])
        try await fails("分页不完整") { _ = try await service.fetchDocument() }
        TestProtocol.install([page([1, 1])])
        try await fails("分页不完整") { _ = try await service.fetchDocument(previousRevision: "r1") }

        for address in ["https://127.0.0.1:8767", "http://example.com", "http://127.0.0.2:8767", "http://user@localhost:8767"] {
            TestProtocol.install([])
            let invalid = client(address)
            try await fails("服务地址无效") { _ = try await invalid.fetchStatus() }
            try require(TestProtocol.received.isEmpty, "Invalid hosts must be rejected before issuing requests")
        }
        TestProtocol.install([StubResponse(json: ["live": ["active": false, "pending_count": 0]], server: "OtherService")])
        try await fails("不是 Audio Agent") { _ = try await service.fetchStatus() }
        TestProtocol.install([StubResponse(json: ["error": ["message": "测试错误"]], status: 503)])
        try await fails("503") { _ = try await service.fetchStatus() }
        TestProtocol.install([StubResponse(json: [:], failure: URLError(.timedOut))])
        try await fails("超时") { _ = try await service.fetchStatus() }
        TestProtocol.install([status(pending: -1)])
        try await fails("格式无效") { _ = try await service.fetchStatus() }

        let store = DocumentStore(client: service)
        let authoritative = "  保留开头空格\n第二行\n"
        let raw = StubResponse(json: ["items": [entry(1, text: authoritative)], "total": 1,
                                     "next_cursor": NSNull(), "revision": "store1", "snapshot": 1000])
        TestProtocol.install([status(), raw])
        await store.refresh()
        try require(store.connected && store.canCopyAll && store.error == nil, "Complete refresh enables copying")
        try require(store.isListening && store.pendingCount == 2 && !store.isLoading, "Must publish service status")
        try require(store.fullText.hasSuffix(authoritative), "Full text must preserve all original whitespace")
        let saved = store.items
        let updated = store.lastUpdated

        TestProtocol.install([status(active: false, pending: 0), page([2], total: 2, next: "a", revision: "store2"),
                              StubResponse(json: ["error": ["message": "模拟第二页失败"]], status: 500)])
        await store.refresh()
        try require(store.items == saved && store.lastUpdated == updated, "Failed pagination must retain complete old batch")
        try require(store.error != nil && !store.canCopyAll && !store.isLoading, "Partial result must never be copyable")
        TestProtocol.install([status(active: false, pending: 0)])
        await store.refreshStatus()
        try require(store.connected && store.error != nil && !store.canCopyAll, "Status-only refresh cannot repair incomplete document")
        try require(TestProtocol.received.count == 1 && TestProtocol.received[0].url?.path == "/api/status", "Background status poll must not request document")

        TestProtocol.install([status(), raw])
        await store.refresh()
        try require(store.error == nil && store.canCopyAll && store.items == saved, "Unchanged full revision can recover a failed refresh")
        store.reportConnectionError("后台服务尚未启动")
        try require(!store.connected && !store.canCopyAll && store.items == saved, "External connection error retains content and blocks copy")
        TestProtocol.install([status()])
        await store.refreshStatus()
        try require(store.connected && store.error == nil && store.canCopyAll, "Status can recover a connection-only error")

        TestProtocol.install([status(), raw])
        async let refreshOne: Void = store.refresh()
        async let refreshTwo: Void = store.refresh()
        _ = await (refreshOne, refreshTwo)
        try require(TestProtocol.received.count == 2, "Concurrent refresh calls must not duplicate the status/document transaction")

        TestProtocol.install([status(), page([], revision: "empty")])
        await store.refresh()
        try require(store.items.isEmpty && !store.canCopyAll && store.fullText.isEmpty, "Empty complete library clears old content")

        // Explicit controls: construction and ordinary refresh must remain GET-only.
        let controlClient = client()
        let controls = DocumentStore(client: controlClient)
        TestProtocol.install([liveStatus("finished", session: nil), raw])
        await controls.refresh()
        try require(TestProtocol.received.allSatisfy { $0.httpMethod == "GET" }, "Initialization/refresh must never open microphone")
        try require(controls.canCopyAll && controls.canToggleLive && !controls.liveIsBusy, "Finished session can start explicitly")
        controls.reportListeningError("系统尚未授权麦克风")
        try require(controls.liveError == "系统尚未授权麦克风" && controls.connected && controls.error == nil && controls.canCopyAll,
                    "Native permission errors must not disconnect the service or disable copying existing text")
        TestProtocol.install([devices()])
        await controls.loadDevices()
        try require(controls.selectedDevice == "MacBook Pro麦克风", "Built-in mic must win over RX at index zero")
        controls.selectedDevice = "Wireless Mic Rx"
        TestProtocol.install([devices()])
        await controls.loadDevices()
        try require(controls.selectedDevice == "Wireless Mic Rx", "A valid explicit device choice must be preserved")
        controls.selectedDevice = "MacBook Pro麦克风"

        TestProtocol.install([session("token-one"), liveReply("starting")])
        await controls.setListening(true)
        let startingRequests = TestProtocol.received
        try require(startingRequests.map { $0.url!.path } == ["/api/session", "/api/live/start"], "Start must acquire in-memory session token")
        let startingBody = try actionBody(startingRequests[1])
        try require(startingBody["device"] as? String == "MacBook Pro麦克风" && startingBody["language"] as? String == "zh" && startingBody["vad_enabled"] as? Bool == true, "Native start must request selected mic, Chinese and real VAD")
        try require(startingRequests[1].value(forHTTPHeaderField: "X-Audio-Agent-Token") == "token-one", "POST requires session token header")
        try require(startingRequests[1].value(forHTTPHeaderField: "Origin") == nil, "Native POST must not invent Origin")
        try require(controls.livePhase == "starting" && controls.isListening && controls.liveButtonTitle == "停止监听", "Accepted starting must immediately expose stop")
        try require(controls.statusText == "正在启动监听…" && controls.canToggleLive,
                    "Starting must not claim audio arrived, while explicit Stop remains available")
        TestProtocol.install([liveReply("stopping", pending: 3)])
        await controls.setListening(false)
        try require(TestProtocol.received.count == 1 && TestProtocol.received[0].url?.path == "/api/live/stop", "Stop must reuse token and never enumerate/start")
        let stoppingBody = try actionBody(TestProtocol.received[0])
        try require(stoppingBody.isEmpty, "Stop uses empty JSON object")
        try require(!controls.isListening && controls.liveIsBusy && !controls.canToggleLive && controls.livePhase == "stopping", "ASR drain must not be presented as microphone listening")
        try require(controls.liveButtonTitle == "正在停止…", "Stopping label must be explicit")
        TestProtocol.install([liveStatus("finished", pending: 3)])
        await controls.refreshStatus()
        try require(!controls.liveIsBusy && !controls.isListening && controls.canToggleLive, "Pending ASR alone must not mean microphone on")

        TestProtocol.install([StubResponse(json: ["error": ["code": "invalid_token", "message": "令牌失效"]], status: 403),
                              session("token-two"), liveReply("recording")])
        await controls.setListening(true)
        let refreshedToken = TestProtocol.received
        try require(refreshedToken.map { $0.url!.path } == ["/api/live/start", "/api/session", "/api/live/start"], "Only explicit invalid_token rejection refreshes token and retries once")
        try require(refreshedToken.last?.value(forHTTPHeaderField: "X-Audio-Agent-Token") == "token-two", "Retry must use new token")

        TestProtocol.install([StubResponse(json: [:], failure: URLError(.timedOut))])
        await controls.setListening(false)
        try require(TestProtocol.received.count == 1 && controls.liveError != nil, "Lost stop response must not blindly resend")
        try require(controls.isListening && controls.canCopyAll && controls.error == nil, "Control error must preserve known state and copyable document")
        TestProtocol.install([liveReply("finished")])
        await controls.setListening(false)
        try require(!controls.liveIsBusy, "Explicit stop can reconcile later")

        TestProtocol.install([StubResponse(json: ["error": ["code": "live_busy", "message": "另一个会话正在运行"]], status: 409)])
        await controls.setListening(true)
        try require(TestProtocol.received.count == 1 && TestProtocol.received[0].url?.path == "/api/live/start", "Start conflict must never become a stop or be retried")
        try require(controls.liveError?.contains("另一个会话") == true && controls.canCopyAll, "Show explicit service error without damaging document")
        TestProtocol.install([StubResponse(json: ["error": ["code": "permission_denied", "message": "麦克风权限未允许"]], status: 403)])
        await controls.setListening(true)
        try require(TestProtocol.received.count == 1 && controls.liveError?.contains("麦克风权限") == true, "Other 403 responses must not refresh token/retry")

        // A delayed old status must not turn an accepted Start back into off.
        var oldFinished = liveStatus("finished")
        oldFinished.delay = 0.2
        TestProtocol.install([oldFinished, liveReply("recording")])
        async let staleStartPoll: Void = controls.refreshStatus()
        try await waitForRequests(1)
        await controls.setListening(true)
        _ = await staleStartPoll
        try require(controls.isListening && controls.livePhase == "recording", "Old GET response must not overwrite completed Start")

        // Likewise, stop intent stays stop even while an old recording response arrives.
        var oldRecording = liveStatus("recording")
        oldRecording.delay = 0.2
        TestProtocol.install([oldRecording, liveReply("finished")])
        async let staleStopPoll: Void = controls.refreshStatus()
        try await waitForRequests(1)
        await controls.setListening(false)
        _ = await staleStopPoll
        try require(!controls.isListening && controls.livePhase == "finished", "Old GET response must not overwrite completed Stop")
        try require(TestProtocol.received.filter { $0.httpMethod == "POST" }.allSatisfy { $0.url?.path == "/api/live/stop" }, "Fixed stop intent may never reverse into start")

        var delayedStart = liveReply("recording")
        delayedStart.delay = 0.1
        TestProtocol.install([delayedStart])
        async let startOne: Void = controls.setListening(true)
        async let startTwo: Void = controls.setListening(true)
        _ = await (startOne, startTwo)
        try require(TestProtocol.received.count == 1, "Live actions must not reenter")

        var staleFailure = StubResponse(json: [:], failure: URLError(.timedOut))
        staleFailure.delay = 0.2
        TestProtocol.install([staleFailure, liveReply("finished")])
        async let failedOldRefresh: Void = controls.refresh()
        try await waitForRequests(1)
        await controls.setListening(false)
        _ = await failedOldRefresh
        try require(controls.connected && controls.error == nil && controls.canCopyAll,
                    "An old failed status request must not disconnect or disable copying after a successful action")

        let invalidTokenClient = client()
        let invalidTokenReply = StubResponse(json: ["error": ["code": "invalid_token", "message": "令牌失效"]], status: 403)
        TestProtocol.install([session("old-token"), invalidTokenReply, session("new-token"), invalidTokenReply])
        try await fails("403") { _ = try await invalidTokenClient.stopListening() }
        try require(TestProtocol.received.count == 4 && TestProtocol.received.filter { $0.httpMethod == "POST" }.count == 2,
                    "A second invalid_token rejection must stop, never loop")

        let unavailable = DocumentStore(client: client())
        TestProtocol.install([StubResponse(json: ["live": live("finished", vad: false, session: nil),
                                                 "models": [["name": "local"]], "vad": ["available": false]])])
        await unavailable.refreshStatus()
        TestProtocol.install([devices()])
        await unavailable.loadDevices()
        TestProtocol.install([])
        await unavailable.setListening(true)
        try require(TestProtocol.received.isEmpty && unavailable.liveError?.contains("VAD") == true,
                    "Fresh native session must not silently downgrade VAD when unavailable")
        let missingDevice = DocumentStore(client: client())
        TestProtocol.install([StubResponse(json: ["items": []])])
        await missingDevice.setListening(true)
        try require(TestProtocol.received.count == 1 && TestProtocol.received[0].httpMethod == "GET" && missingDevice.liveError != nil,
                    "Missing device must expose selector/error without POSTing start")

        TestProtocol.install([StubResponse(json: ["live": live("finished", language: "en", vad: false)])])
        await controls.refreshStatus()
        TestProtocol.install([liveReply("starting")])
        await controls.setListening(true)
        let inherited = try actionBody(TestProtocol.received[0])
        try require(inherited["language"] as? String == "en" && inherited["vad_enabled"] as? Bool == false, "Explicit existing session settings may be reused without silent fallback")

        let legacy = try decodedEntry(entry(1))
        try require(legacy.segments.isEmpty && legacy.speakerCount == nil && transcriptAnnotatedText(legacy) == legacy.text,
                    "Older documents without speaker fields must decode and copy unchanged")
        let fullSpeakerText = "  你好。\n\n早上好 👩‍💻。  "
        var labeled = entry(1, text: fullSpeakerText)
        labeled["speaker_count"] = 2
        labeled["segments"] = [["text": "你好。", "speaker_labels": ["我"]],
                               ["text": "早上好 👩‍💻。", "speaker_labels": ["设计同事"]]]
        let decoded = try decodedEntry(labeled)
        let paragraphs = transcriptParagraphs(decoded)
        try require(paragraphs.map(\.text).joined() == fullSpeakerText, "Speaker rendering must preserve all authoritative text and whitespace")
        try require(paragraphs.map(\.speakerLabel) == ["我", "设计同事"], "Decode and render personal and custom speaker labels")
        try require(transcriptAnnotatedText(decoded) == "【我】\n  你好。\n\n\n【设计同事】\n早上好 👩‍💻。  ",
                    "Copy must retain labels, Unicode, and original text slices")
        try require(decoded.speakerLabels == ["我", "设计同事"], "Speaker search must include backend custom names")

        var mixed = labeled
        mixed["segments"] = [["text": "你好。", "speaker_labels": ["说话人 1", "设计同事", "设计同事"]],
                             ["text": "早上好 👩‍💻。", "speaker_labels": ["说话人 1", "设计同事"]]]
        let mixedParagraphs = transcriptParagraphs(try decodedEntry(mixed))
        try require(mixedParagraphs.count == 1 && mixedParagraphs[0].speakerLabel == "混合发言（说话人 1、设计同事）"
                    && mixedParagraphs[0].text == fullSpeakerText, "Mixed speech must remain mixed, deduplicate tags and merge adjacent identical speakers")

        var sameName = labeled
        sameName["segments"] = [["text": "你好。", "speaker_labels": ["张三"], "speaker_ids": ["speaker_1"]],
                                ["text": "早上好 👩‍💻。", "speaker_labels": ["张三"], "speaker_ids": ["speaker_2"]]]
        let sameNameEntry = try decodedEntry(sameName)
        let separatePeople = transcriptParagraphs(sameNameEntry)
        try require(separatePeople.count == 2 && separatePeople[0].speakerIDs != separatePeople[1].speakerIDs
                    && separatePeople.map(\.text).joined() == fullSpeakerText,
                    "Equal display names must not merge different speaker identities into one turn")
        try require(transcriptAnnotatedText(sameNameEntry).components(separatedBy: "【张三】").count == 3,
                    "Copy must retain the separate turns of same-named people")
        sameName["segments"] = [["text": fullSpeakerText, "speaker_labels": ["张三", "张三"],
                                 "speaker_ids": ["speaker_1", "speaker_2"]]]
        let sameNameMixed = try decodedEntry(sameName)
        let sameNameParagraph = transcriptParagraphs(sameNameMixed)
        try require(sameNameParagraph[0].speakerLabel == "混合发言（2 位说话人：张三）"
                    && sameNameParagraph[0].text == fullSpeakerText,
                    "Distinct speaker IDs must keep a mixed-speech label even when their names are identical")

        var incomplete = labeled
        incomplete["segments"] = [["text": "你好。", "speaker_labels": ["我"]]]
        let fallback = transcriptParagraphs(try decodedEntry(incomplete))
        try require(fallback.count == 1 && fallback[0].text == fullSpeakerText
                    && fallback[0].speakerLabel == "已区分 2 位说话人 · 逐句归属未对齐",
                    "Incomplete segment alignment must keep full text without assigning the rest to the first speaker")
        incomplete["segments"] = []
        incomplete["speaker_count"] = 0
        let withoutSpeakers = try decodedEntry(incomplete)
        try require(transcriptParagraphs(withoutSpeakers)[0].speakerLabel == "未检测到说话人",
                    "Completed analysis with zero speakers must differ from an unanalysed recording")

        let speakerStore = DocumentStore(client: client())
        TestProtocol.install([status(), page([1], revision: "without-labels")])
        await speakerStore.refresh()
        TestProtocol.install([status(), annotatedPage(labeled, revision: "speaker-labels")])
        await speakerStore.refresh()
        try require(speakerStore.items.first?.segments == decoded.segments && speakerStore.fullText.contains("【设计同事】"),
                    "A later speaker annotation on an existing record must replace the native item and enter full copy")
        labeled["segments"] = [["text": "你好。", "speaker_labels": ["我"]],
                               ["text": "早上好 👩‍💻。", "speaker_labels": ["小林"]]]
        TestProtocol.install([status(), annotatedPage(labeled, revision: "renamed-speaker")])
        await speakerStore.refresh()
        try require(speakerStore.fullText.contains("【小林】") && !speakerStore.fullText.contains("设计同事"),
                    "Editing a person remark must refresh copied labels without changing transcript text")
        try require(TestProtocol.received.allSatisfy { $0.httpMethod == "GET" }, "Reading labels must never start analysis, enrollment, or microphone")
        try await testThresholdSettings()
        print("DocumentClientTests: all checks passed (no network or microphone)")
    }
}
