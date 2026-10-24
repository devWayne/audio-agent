import Foundation
import Combine

struct TranscriptSegment: Decodable, Equatable {
    let text: String?
    let speakerLabels: [String]
    let speakerIDs: [String]

    enum CodingKeys: String, CodingKey {
        case text
        case speakerLabels = "speaker_labels"
        case speakerIDs = "speaker_ids"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        text = try values.decodeIfPresent(String.self, forKey: .text)
        speakerLabels = try values.decodeIfPresent([String].self, forKey: .speakerLabels) ?? []
        speakerIDs = try values.decodeIfPresent([String].self, forKey: .speakerIDs) ?? []
    }
}

struct TranscriptEntry: Decodable, Identifiable, Equatable {
    let id: String
    let clipID: String?
    let text: String
    let startTime: Double
    let endTime: Double
    let recordingAvailable: Bool
    let displayName: String?
    let segments: [TranscriptSegment]
    let speakerCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, text
        case clipID = "clip_id"
        case startTime = "start_time"
        case endTime = "end_time"
        case recordingAvailable = "recording_available"
        case displayName = "display_name"
        case segments
        case speakerCount = "speaker_count"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        clipID = try values.decodeIfPresent(String.self, forKey: .clipID)
        text = try values.decode(String.self, forKey: .text)
        startTime = try values.decode(Double.self, forKey: .startTime)
        endTime = try values.decode(Double.self, forKey: .endTime)
        recordingAvailable = try values.decode(Bool.self, forKey: .recordingAvailable)
        displayName = try values.decodeIfPresent(String.self, forKey: .displayName)
        segments = try values.decodeIfPresent([TranscriptSegment].self, forKey: .segments) ?? []
        speakerCount = try values.decodeIfPresent(Int.self, forKey: .speakerCount)
    }

    var speakerLabels: [String] {
        var seen = Set<String>()
        return segments.flatMap(\.speakerLabels).filter {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && seen.insert($0).inserted
        }
    }
}

struct TranscriptParagraph: Equatable {
    var text: String
    let speakerLabel: String?
    var speakerIDs: [String] = []
}

func transcriptParagraphs(_ entry: TranscriptEntry) -> [TranscriptParagraph] {
    let text = entry.text
    let fallbackLabel: String?
    if let count = entry.speakerCount, count >= 0 {
        fallbackLabel = count == 0 ? "未检测到说话人" : "已区分 \(count) 位说话人 · 逐句归属未对齐"
    } else {
        fallbackLabel = nil
    }
    let fallback = [TranscriptParagraph(text: text, speakerLabel: fallbackLabel)]
    let segments = entry.segments.filter { !($0.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    guard segments.contains(where: { !$0.speakerLabels.isEmpty }) else { return fallback }

    // Locate segment boundaries in the authoritative text. Never reconstruct
    // full text by concatenating segments: timestamps/segments can be partial.
    var starts: [String.Index] = []
    var offset = text.startIndex
    for segment in segments {
        let part = (segment.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard let range = text.range(of: part, range: offset..<text.endIndex),
              text[offset..<range.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return fallback
        }
        starts.append(range.lowerBound)
        offset = range.upperBound
    }
    guard text[offset...].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return fallback }

    var paragraphs: [TranscriptParagraph] = []
    for (index, segment) in segments.enumerated() {
        var seen = Set<String>()
        let labels = segment.speakerLabels.filter {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && seen.insert($0).inserted
        }
        let ids = Array(Set(segment.speakerIDs.filter { !$0.isEmpty })).sorted()
        let mixedCount = max(ids.count, labels.count)
        let label: String
        if mixedCount > 1 {
            label = labels.count > 1 ? "混合发言（\(labels.joined(separator: "、"))）"
                : "混合发言（\(mixedCount) 位说话人：\(labels.first ?? "未区分")）"
        } else {
            label = labels.first ?? "未区分"
        }
        let start = index == 0 ? text.startIndex : starts[index]
        let end = index + 1 < starts.count ? starts[index + 1] : text.endIndex
        let part = String(text[start..<end])
        if paragraphs.last?.speakerLabel == label && paragraphs.last?.speakerIDs == ids {
            paragraphs[paragraphs.count - 1].text += part
        } else {
            paragraphs.append(TranscriptParagraph(text: part, speakerLabel: label, speakerIDs: ids))
        }
    }
    return paragraphs.isEmpty ? fallback : paragraphs
}

func transcriptAnnotatedText(_ entry: TranscriptEntry) -> String {
    if entry.text.isEmpty { return "（未识别到文字）" }
    return transcriptParagraphs(entry).map { paragraph in
        paragraph.speakerLabel.map { "【\($0)】\n\(paragraph.text)" } ?? paragraph.text
    }.joined(separator: "\n")
}

struct DocumentSnapshot {
    let items: [TranscriptEntry]
    let revision: String
}

struct BarServiceStatus {
    let active: Bool
    let pendingCount: Int
    let live: LiveSessionStatus
    let vadAvailable: Bool?
    let hasASRModel: Bool?
}

struct AudioInputDevice: Decodable, Identifiable, Equatable {
    let index: Int
    let name: String
    var id: Int { index }
}

struct LiveThresholdSettings: Decodable, Equatable {
    let triggerDB: Double
    let triggerThreshold: Double
    let releaseThreshold: Double

    enum CodingKeys: String, CodingKey {
        case triggerDB = "trigger_db"
        case triggerThreshold = "trigger_threshold"
        case releaseThreshold = "release_threshold"
    }
}

struct LiveThresholdSettingsResponse: Decodable, Equatable {
    let settings: LiveThresholdSettings
    let pending: Bool
}

struct LiveSessionStatus: Decodable {
    let state: String
    let active: Bool
    let pendingCount: Int
    let device: String?
    let language: String?
    let vadEnabled: Bool?
    let sessionID: String?
    let lastError: String?
    let triggerDB: Double?
    let configuredTriggerDB: Double?
    let settingsPending: Bool?
    let settingsError: String?

    enum CodingKeys: String, CodingKey {
        case state, active, device, language
        case pendingCount = "pending_count"
        case vadEnabled = "vad_enabled"
        case sessionID = "session_id"
        case lastError = "last_error"
        case triggerDB = "trigger_db"
        case configuredTriggerDB = "configured_trigger_db"
        case settingsPending = "settings_pending"
        case settingsError = "settings_error"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        active = try values.decode(Bool.self, forKey: .active)
        state = try values.decodeIfPresent(String.self, forKey: .state) ?? (active ? "recording" : "finished")
        pendingCount = try values.decode(Int.self, forKey: .pendingCount)
        device = try values.decodeIfPresent(String.self, forKey: .device)
        language = try values.decodeIfPresent(String.self, forKey: .language)
        vadEnabled = try values.decodeIfPresent(Bool.self, forKey: .vadEnabled)
        sessionID = try values.decodeIfPresent(String.self, forKey: .sessionID)
        lastError = try values.decodeIfPresent(String.self, forKey: .lastError)
        triggerDB = try values.decodeIfPresent(Double.self, forKey: .triggerDB)
        configuredTriggerDB = try values.decodeIfPresent(Double.self, forKey: .configuredTriggerDB)
        settingsPending = try values.decodeIfPresent(Bool.self, forKey: .settingsPending)
        settingsError = try values.decodeIfPresent(String.self, forKey: .settingsError)
    }
}

private enum DocumentClientError: LocalizedError {
    case invalidAddress
    case wrongService
    case http(Int, String?, String?)
    case invalidResponse
    case incompleteDocument
    case changedDocument
    case timeout
    case connection
    case cancelled
    case invalidThreshold

    var errorDescription: String? {
        switch self {
        case .invalidAddress:
            return "服务地址无效；仅支持 http://127.0.0.1 或 http://localhost 的本地服务。"
        case .wrongService:
            return "这个端口运行的不是 Audio Agent 服务，请检查本地服务地址。"
        case .http(let code, _, let detail):
            return detail.map { "本地服务请求失败（\(code)）：\($0)" }
                ?? "本地服务请求失败（\(code)），请稍后重试。"
        case .invalidResponse:
            return "本地服务返回的数据格式无效，请刷新重试。"
        case .incompleteDocument:
            return "转写分页不完整，本次未替换已有内容。请刷新后再复制全文。"
        case .changedDocument:
            return "转写记录正在更新，本次尚未读取完整。请稍后刷新再复制全文。"
        case .timeout:
            return "连接本地 Audio Agent 服务超时，请确认服务已经启动。"
        case .connection:
            return "无法连接本地 Audio Agent 服务，请确认服务已经启动。"
        case .cancelled:
            return "本次同步已取消，已有转写内容仍然保留。"
        case .invalidThreshold:
            return "收音门槛必须是 -60 到 -18 dBFS 之间的有限数值。"
        }
    }
}

private struct DocumentPage: Decodable {
    let items: [TranscriptEntry]
    let total: Int
    let nextCursor: String?
    let snapshot: Double
    let revision: String

    enum CodingKeys: String, CodingKey {
        case items, total, snapshot, revision
        case nextCursor = "next_cursor"
    }
}

private struct StatusResponse: Decodable {
    struct VAD: Decodable { let available: Bool? }
    struct Model: Decodable { let name: String? }
    let live: LiveSessionStatus
    let vad: VAD?
    let models: [Model]?
}

private struct ServiceErrorResponse: Decodable {
    struct Detail: Decodable { let code: String?; let message: String }
    let error: Detail
}

private struct LiveResponse: Decodable { let live: LiveSessionStatus }
private struct DevicesResponse: Decodable { let items: [AudioInputDevice] }
private struct SessionResponse: Decodable { let token: String }
private actor LiveTokenCache {
    private var token: String?
    func get() -> String? { token }
    func set(_ value: String?) { token = value }
}

private final class LocalOnlySessionDelegate: NSObject, URLSessionTaskDelegate {
    // A GET must never escape to another service through a redirect. Declining
    // every redirect also prevents same-host requests changing port or scheme.
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

final class DocumentClient {
    private let baseURL: URL
    private let session: URLSession
    private let sessionDelegate: LocalOnlySessionDelegate
    private let tokenCache = LiveTokenCache()

    init(baseURL: URL) {
        self.baseURL = baseURL
        let configuration = Self.configuration()
        sessionDelegate = LocalOnlySessionDelegate()
        session = URLSession(configuration: configuration, delegate: sessionDelegate, delegateQueue: nil)
    }

    // In-process URLProtocol injection is only for command-line tests. It keeps
    // the production HTTP configuration and avoids opening sockets in tests.
    init(baseURL: URL, protocolClasses: [AnyClass]) {
        self.baseURL = baseURL
        let configuration = Self.configuration()
        configuration.protocolClasses = protocolClasses
        sessionDelegate = LocalOnlySessionDelegate()
        session = URLSession(configuration: configuration, delegate: sessionDelegate, delegateQueue: nil)
    }

    deinit { session.invalidateAndCancel() }

    private static func configuration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 8
        configuration.timeoutIntervalForResource = 8
        return configuration
    }

    private func endpoint(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "http",
              let host = components.host?.lowercased(), ["127.0.0.1", "localhost"].contains(host),
              components.user == nil, components.password == nil,
              components.port.map({ (1...65535).contains($0) }) ?? true,
              components.path.isEmpty || components.path == "/",
              components.query == nil, components.fragment == nil else {
            throw DocumentClientError.invalidAddress
        }
        components.path = path
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw DocumentClientError.invalidAddress }
        return url
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        try await request(path, method: "GET", query: query)
    }

    private func request<T: Decodable>(_ path: String, method: String, query: [URLQueryItem] = [],
                                      body: Data? = nil, token: String? = nil) async throws -> T {
        try Task.checkCancellation()
        let url = try endpoint(path, query: query)
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8)
        request.httpMethod = method
        request.httpBody = body
        request.httpShouldHandleCookies = false
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let token { request.setValue(token, forHTTPHeaderField: "X-Audio-Agent-Token") }
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            if error.code == .timedOut { throw DocumentClientError.timeout }
            if error.code == .cancelled { throw DocumentClientError.cancelled }
            throw DocumentClientError.connection
        } catch is CancellationError {
            throw DocumentClientError.cancelled
        } catch {
            throw DocumentClientError.connection
        }
        guard let http = response as? HTTPURLResponse else { throw DocumentClientError.invalidResponse }
        let server = http.value(forHTTPHeaderField: "Server")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard server == "AudioAgent" || server.hasPrefix("AudioAgent/") else {
            throw DocumentClientError.wrongService
        }
        guard (200...299).contains(http.statusCode) else {
            let detail = try? JSONDecoder().decode(ServiceErrorResponse.self, from: data).error
            throw DocumentClientError.http(http.statusCode, detail?.code, detail?.message)
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw DocumentClientError.invalidResponse
        }
    }

    private func page(cursor: String?) async throws -> DocumentPage {
        var query = [URLQueryItem(name: "limit", value: "200")]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        let page: DocumentPage = try await get("/api/document", query: query)
        guard page.total >= 0, page.snapshot.isFinite, !page.revision.isEmpty,
              page.items.count <= 200, page.items.count <= page.total,
              page.nextCursor.map({ !$0.isEmpty }) ?? true,
              page.items.allSatisfy({ !$0.id.isEmpty && $0.startTime.isFinite && $0.endTime.isFinite && $0.endTime >= $0.startTime }) else {
            throw DocumentClientError.invalidResponse
        }
        guard Set(page.items.map(\.id)).count == page.items.count else {
            throw DocumentClientError.incompleteDocument
        }
        for (left, right) in zip(page.items, page.items.dropFirst()) {
            guard left.startTime < right.startTime || (left.startTime == right.startTime && left.id < right.id) else {
                throw DocumentClientError.incompleteDocument
            }
        }
        return page
    }

    func fetchDocument(previousRevision: String? = nil) async throws -> DocumentSnapshot? {
        for attempt in 0..<2 {
            do {
                let first = try await page(cursor: nil)
                if first.nextCursor == nil && first.items.count != first.total {
                    throw DocumentClientError.changedDocument
                }
                if let previousRevision, first.revision == previousRevision { return nil }
                var current = first
                var entries: [TranscriptEntry] = []
                var ids = Set<String>()
                var cursors = Set<String>()
                while true {
                    try Task.checkCancellation()
                    guard current.snapshot == first.snapshot, current.revision == first.revision,
                          current.total == first.total else { throw DocumentClientError.changedDocument }
                    for entry in current.items {
                        guard ids.insert(entry.id).inserted else { throw DocumentClientError.incompleteDocument }
                        if let previous = entries.last,
                           entry.startTime < previous.startTime || (entry.startTime == previous.startTime && entry.id < previous.id) {
                            throw DocumentClientError.incompleteDocument
                        }
                        entries.append(entry)
                    }
                    guard entries.count <= first.total else { throw DocumentClientError.incompleteDocument }
                    guard let cursor = current.nextCursor else { break }
                    guard !current.items.isEmpty, entries.count < first.total, cursors.insert(cursor).inserted else {
                        throw DocumentClientError.incompleteDocument
                    }
                    current = try await page(cursor: cursor)
                }
                guard entries.count == first.total else { throw DocumentClientError.changedDocument }
                return DocumentSnapshot(items: entries, revision: first.revision)
            } catch DocumentClientError.changedDocument {
                if attempt == 1 { throw DocumentClientError.changedDocument }
                // Retention or another transcription may change a page while
                // it is being read. Restart once; never commit partial text.
            } catch is CancellationError {
                throw DocumentClientError.cancelled
            }
        }
        throw DocumentClientError.changedDocument
    }

    func fetchStatus() async throws -> BarServiceStatus {
        let response: StatusResponse = try await get("/api/status")
        try validateLive(response.live)
        return BarServiceStatus(active: response.live.active, pendingCount: response.live.pendingCount,
                                live: response.live, vadAvailable: response.vad?.available,
                                hasASRModel: response.models.map { !$0.isEmpty })
    }

    func fetchDevices() async throws -> [AudioInputDevice] {
        let response: DevicesResponse = try await get("/api/devices")
        guard response.items.allSatisfy({ $0.index >= 0 && !$0.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }),
              Set(response.items.map(\.index)).count == response.items.count else {
            throw DocumentClientError.invalidResponse
        }
        return response.items
    }

    private func validateLive(_ live: LiveSessionStatus) throws {
        guard live.pendingCount >= 0, ["starting", "recording", "stopping", "finished", "error"].contains(live.state),
              live.language.map({ ["zh", "auto", "en"].contains($0) }) ?? true else {
            throw DocumentClientError.invalidResponse
        }
        for value in [live.triggerDB, live.configuredTriggerDB].compactMap({ $0 }) {
            guard value.isFinite, value >= -60.000001, value <= -17.999999 else {
                throw DocumentClientError.invalidResponse
            }
        }
    }

    private func validateThresholdSettings(_ response: LiveThresholdSettingsResponse) throws {
        let settings = response.settings
        guard settings.triggerDB.isFinite, (-60...(-18)).contains(settings.triggerDB),
              settings.triggerThreshold.isFinite, settings.releaseThreshold.isFinite,
              settings.triggerThreshold > 0, settings.releaseThreshold > 0,
              abs(settings.triggerThreshold / pow(10, settings.triggerDB / 20) - 1) < 0.000001,
              abs(settings.releaseThreshold / (settings.triggerThreshold * 2 / 3) - 1) < 0.000001 else {
            throw DocumentClientError.invalidResponse
        }
    }

    func fetchThresholdSettings() async throws -> LiveThresholdSettingsResponse {
        let response: LiveThresholdSettingsResponse = try await get("/api/live/settings")
        try validateThresholdSettings(response)
        return response
    }

    func saveThresholdSettings(triggerDB: Double) async throws -> LiveThresholdSettingsResponse {
        guard triggerDB.isFinite, (-60...(-18)).contains(triggerDB) else { throw DocumentClientError.invalidThreshold }
        let response: LiveThresholdSettingsResponse = try await authorizedPost("/api/live/settings", values: ["trigger_db": triggerDB])
        try validateThresholdSettings(response)
        guard abs(response.settings.triggerDB - triggerDB) < 0.000001 else { throw DocumentClientError.invalidResponse }
        return response
    }

    private func sessionToken() async throws -> String {
        if let token = await tokenCache.get() { return token }
        let response: SessionResponse = try await get("/api/session")
        guard !response.token.isEmpty, response.token.count <= 4096,
              response.token.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil else {
            throw DocumentClientError.invalidResponse
        }
        await tokenCache.set(response.token)
        return response.token
    }

    private func authorizedPost<T: Decodable>(_ path: String, values: [String: Any]) async throws -> T {
        let body = try JSONSerialization.data(withJSONObject: values)
        for attempt in 0..<2 {
            let token = try await sessionToken()
            do {
                return try await request(path, method: "POST", body: body, token: token)
            } catch DocumentClientError.http(let status, let code, _) where status == 403 && code == "invalid_token" && attempt == 0 {
                // Only this explicit pre-action rejection is safe to retry.
                // A timeout/500 may have already changed microphone state.
                await tokenCache.set(nil)
            }
        }
        throw DocumentClientError.invalidResponse
    }

    private func liveAction(_ path: String, values: [String: Any]) async throws -> LiveSessionStatus {
        let response: LiveResponse = try await authorizedPost(path, values: values)
        try validateLive(response.live)
        return response.live
    }

    func startListening(device: String, language: String = "zh", vadEnabled: Bool = true) async throws -> LiveSessionStatus {
        try await liveAction("/api/live/start", values: ["device": device, "language": language, "vad_enabled": vadEnabled])
    }

    func stopListening() async throws -> LiveSessionStatus {
        try await liveAction("/api/live/stop", values: [:])
    }
}

// Match the Web document's local-date + local-time range, without trimming or
// reconstructing authoritative text from ASR segments.
func transcriptTimestamp(_ seconds: Double, includeDate: Bool = true) -> String {
    guard seconds.isFinite, abs(seconds) < 253_402_300_800 else { return "时间未记录" }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.timeZone = .current
    formatter.dateFormat = includeDate ? "yyyy/MM/dd" : "HH:mm:ss"
    return formatter.string(from: Date(timeIntervalSince1970: seconds))
}

func transcriptTimeRange(_ entry: TranscriptEntry) -> String {
    "\(transcriptTimestamp(entry.startTime, includeDate: false))–\(transcriptTimestamp(entry.endTime, includeDate: false))"
}

@MainActor
final class DocumentStore: ObservableObject {
    @Published private(set) var items: [TranscriptEntry] = []
    @Published private(set) var isLoading = false
    @Published private(set) var error: String?
    @Published private(set) var connected = false
    @Published private(set) var isListening = false
    @Published private(set) var pendingCount = 0
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var livePhase = "finished"
    @Published private(set) var liveActionPending = false
    @Published private(set) var liveError: String?
    @Published private(set) var availableDevices: [AudioInputDevice] = []
    @Published var selectedDevice = ""
    @Published var thresholdDraftDB = 20 * log10(0.006) {
        didSet {
            if !thresholdAdoptingDraft {
                thresholdDraftIsLocal = true
                thresholdDraftRevision &+= 1
            }
        }
    }
    @Published private(set) var thresholdConfiguredDB: Double?
    @Published private(set) var thresholdAppliedDB: Double?
    @Published private(set) var thresholdSettingsPending = false
    @Published private(set) var thresholdIsEditing = false
    @Published private(set) var thresholdSaving = false
    @Published private(set) var thresholdLoaded = false
    @Published private(set) var thresholdError: String?

    private let client: DocumentClient
    private var revision: String?
    private var hasCompleteDocument = false
    private var requestInFlight = false
    private var documentError: String?
    private var connectionError: String?
    private var liveRevision: UInt64 = 0
    private var liveSessionActive = false
    private var currentLiveDevice: String?
    private var currentLanguage = "zh"
    private var currentVADEnabled = true
    private var liveActionIntent: Bool?
    private var deviceLoadInFlight = false
    private var hasASRModel: Bool?
    private var vadAvailable: Bool?
    private var thresholdRevision: UInt64 = 0
    private var thresholdDraftRevision: UInt64 = 0
    private var thresholdDraftIsLocal = false
    private var thresholdAdoptingDraft = false
    private var thresholdReadInFlight = false
    private var thresholdSaveQueued = false
    private var thresholdRequestError: String?
    private var thresholdServerError: String?

    init(client: DocumentClient) { self.client = client }

    var canCopyAll: Bool { hasCompleteDocument && error == nil && !items.isEmpty }
    var thresholdHasUnsavedChanges: Bool {
        guard thresholdDraftIsLocal else { return false }
        guard let configured = thresholdConfiguredDB else { return true }
        return !thresholdDraftDB.isFinite || abs(thresholdDraftDB - configured) > 0.000001
    }
    var canSaveThreshold: Bool {
        thresholdLoaded && connected && !thresholdSaving && !thresholdIsEditing
            && thresholdDraftDB.isFinite && (-60.0 ... -18.0).contains(thresholdDraftDB)
            && (thresholdHasUnsavedChanges || thresholdError != nil)
    }
    var thresholdStatusText: String {
        if thresholdSaving { return "正在保存收音门槛…" }
        guard thresholdLoaded, let configured = thresholdConfiguredDB else { return "尚未读取收音门槛" }
        let saved = String(format: "%.1f", configured)
        let applied = thresholdAppliedDB.map { String(format: "%.1f", $0) }
        if thresholdIsEditing || thresholdHasUnsavedChanges {
            return applied.map { "草稿尚未保存 · 当前生效 \($0) dBFS" } ?? "草稿尚未保存"
        }
        if thresholdSettingsPending {
            return applied.map { "已保存 \(saved) dBFS · 当前 \($0) dBFS，下一段生效" }
                ?? "已保存 \(saved) dBFS · 等待当前声音结束后生效"
        }
        return applied.map { "当前生效 \($0) dBFS" } ?? "已保存 \(saved) dBFS · 等待读取生效状态"
    }
    var liveIsBusy: Bool { liveSessionActive || ["starting", "recording", "stopping"].contains(livePhase) }
    var canToggleLive: Bool { !liveActionPending && livePhase != "stopping" }
    var liveButtonTitle: String {
        if liveActionPending { return liveActionIntent == false ? "正在停止…" : "正在开始…" }
        if livePhase == "stopping" { return "正在停止…" }
        return liveIsBusy ? "停止监听" : "开始监听并自动转写"
    }

    var fullText: String {
        items.map { item in
            "\(transcriptTimestamp(item.startTime)) \(transcriptTimeRange(item))\n\(transcriptAnnotatedText(item))"
        }.joined(separator: "\n\n")
    }

    var statusText: String {
        if isLoading { return "正在同步完整转写…" }
        if error != nil { return connected ? "转写暂未更新" : "服务未连接" }
        if !connected { return "等待本地服务" }
        if livePhase == "starting" { return "正在启动监听…" }
        if livePhase == "stopping" {
            return pendingCount > 0 ? "正在停止 · \(pendingCount) 段待转写" : "正在停止监听"
        }
        if isListening { return pendingCount > 0 ? "正在监听 · \(pendingCount) 段待转写" : "正在监听" }
        if pendingCount > 0 { return "正在整理 \(pendingCount) 段转写" }
        return items.isEmpty ? "已连接 · 暂无转写" : "已连接 · \(items.count) 段完整转写"
    }

    func reportConnectionError(_ message: String) {
        connectionError = message
        connected = false
        error = message
    }

    func reportListeningError(_ message: String) {
        liveError = message
    }

    func refresh() async {
        guard !requestInFlight else { return }
        requestInFlight = true
        isLoading = true
        defer { requestInFlight = false; isLoading = false }
        var receivedStatus: BarServiceStatus?
        let statusRevision = liveRevision
        let settingsRevision = thresholdRevision
        do {
            let status = try await client.fetchStatus()
            receivedStatus = status
            let snapshot = try await client.fetchDocument(previousRevision: revision)
            if let snapshot {
                // Publish only after the full snapshot has passed every page
                // check. A failed refresh retains the previous complete batch.
                items = snapshot.items
                revision = snapshot.revision
                hasCompleteDocument = true
            } else if !hasCompleteDocument {
                throw DocumentClientError.incompleteDocument
            }
            applyStatus(status, revision: statusRevision, settingsRevision: settingsRevision)
            documentError = nil
            connectionError = nil
            error = nil
            lastUpdated = Date()
        } catch {
            let message = error.localizedDescription
            if let receivedStatus {
                applyStatus(receivedStatus, revision: statusRevision, settingsRevision: settingsRevision)
                connectionError = nil
                documentError = message
            } else {
                if statusRevision == liveRevision && !liveActionPending {
                    connected = false
                    connectionError = message
                }
            }
            self.error = connectionError ?? documentError
        }
    }

    func refreshStatus() async {
        guard !requestInFlight else { return }
        requestInFlight = true
        defer { requestInFlight = false }
        let statusRevision = liveRevision
        let settingsRevision = thresholdRevision
        do {
            applyStatus(try await client.fetchStatus(), revision: statusRevision, settingsRevision: settingsRevision)
            connectionError = nil
            // A status-only poll cannot validate or repair an incomplete
            // document, so it must not re-enable copying after that failure.
            error = documentError
        } catch {
            if statusRevision == liveRevision && !liveActionPending {
                reportConnectionError(error.localizedDescription)
            }
        }
    }

    private func applyStatus(_ status: BarServiceStatus, revision: UInt64, settingsRevision: UInt64) {
        guard revision == liveRevision && !liveActionPending else { return }
        connected = true
        hasASRModel = status.hasASRModel
        vadAvailable = status.vadAvailable
        applyLive(status.live)
        applyThresholdStatus(status.live, revision: settingsRevision)
    }

    private func applyLive(_ live: LiveSessionStatus) {
        livePhase = live.state
        liveSessionActive = live.active
        isListening = ["starting", "recording"].contains(live.state)
        pendingCount = live.pendingCount
        currentLiveDevice = live.device
        // A fresh server has no session: retain the native zh + VAD default.
        // A user's existing session may deliberately use another setting.
        if live.sessionID != nil {
            currentLanguage = live.language ?? "zh"
            currentVADEnabled = live.vadEnabled ?? true
        }
        if let device = live.device, live.active,
           availableDevices.contains(where: { $0.name == device }) { selectedDevice = device }
        if let message = live.lastError, !message.isEmpty { liveError = message }
    }

    private func adoptThresholdDraft(_ value: Double) {
        thresholdAdoptingDraft = true
        thresholdDraftDB = value
        thresholdAdoptingDraft = false
        thresholdDraftIsLocal = false
    }

    private func publishThresholdError() {
        thresholdError = thresholdRequestError ?? thresholdServerError
    }

    private func applyThresholdStatus(_ live: LiveSessionStatus, revision: UInt64) {
        guard revision == thresholdRevision, !thresholdSaving else { return }
        let mayAdoptDraft = !thresholdIsEditing && !thresholdDraftIsLocal
        if let configured = live.configuredTriggerDB {
            thresholdConfiguredDB = configured
            thresholdLoaded = true
            if mayAdoptDraft { adoptThresholdDraft(configured) }
        }
        if let applied = live.triggerDB { thresholdAppliedDB = applied }
        if let pending = live.settingsPending { thresholdSettingsPending = pending }
        thresholdServerError = live.settingsError
        publishThresholdError()
    }

    private func applyThresholdSettings(_ response: LiveThresholdSettingsResponse, adoptDraft: Bool) {
        thresholdConfiguredDB = response.settings.triggerDB
        thresholdSettingsPending = response.pending
        thresholdLoaded = true
        // The settings endpoint explicitly confirms equality when pending is
        // false. A pending response must not invent an applied microphone value.
        if !response.pending { thresholdAppliedDB = response.settings.triggerDB }
        if adoptDraft { adoptThresholdDraft(response.settings.triggerDB) }
    }

    func beginThresholdEditing() {
        thresholdIsEditing = true
    }

    func endThresholdEditing() async {
        thresholdIsEditing = false
        await saveThreshold()
    }

    func refreshThresholdSettings() async {
        guard !thresholdReadInFlight, !thresholdSaving else { return }
        thresholdReadInFlight = true
        defer { thresholdReadInFlight = false }
        let expectedRevision = thresholdRevision
        do {
            let response = try await client.fetchThresholdSettings()
            guard expectedRevision == thresholdRevision, !thresholdSaving else { return }
            applyThresholdSettings(response, adoptDraft: !thresholdIsEditing && !thresholdDraftIsLocal)
            thresholdRequestError = nil
            publishThresholdError()
        } catch {
            guard expectedRevision == thresholdRevision, !thresholdSaving else { return }
            thresholdRequestError = "无法读取收音门槛：\(error.localizedDescription)"
            publishThresholdError()
        }
    }

    func saveThreshold() async {
        guard !thresholdIsEditing else { return }
        if thresholdSaving { thresholdSaveQueued = true; return }
        guard thresholdDraftDB.isFinite, (-60.0 ... -18.0).contains(thresholdDraftDB) else {
            thresholdRequestError = DocumentClientError.invalidThreshold.localizedDescription
            publishThresholdError()
            return
        }
        guard thresholdHasUnsavedChanges || thresholdError != nil else {
            if let configured = thresholdConfiguredDB { adoptThresholdDraft(configured) }
            return
        }
        let submitted = thresholdDraftDB
        let draftRevision = thresholdDraftRevision
        thresholdSaving = true
        thresholdSaveQueued = false
        thresholdRevision &+= 1
        thresholdRequestError = nil
        publishThresholdError()
        do {
            let response = try await client.saveThresholdSettings(triggerDB: submitted)
            applyThresholdSettings(response, adoptDraft: draftRevision == thresholdDraftRevision && !thresholdIsEditing)
            thresholdRequestError = nil
        } catch {
            // Preserve the draft and the last confirmed server values. A lost
            // response is not confirmation, and never triggers a blind retry.
            thresholdRequestError = "收音门槛保存未确认：\(error.localizedDescription) 可重试保存。"
        }
        publishThresholdError()
        thresholdRevision &+= 1
        thresholdSaving = false
        let sendNewerDraft = thresholdSaveQueued && abs(thresholdDraftDB - submitted) > 0.000001
        thresholdSaveQueued = false
        if sendNewerDraft && !thresholdIsEditing { await saveThreshold() }
    }

    func loadDevices() async {
        guard !deviceLoadInFlight else { return }
        deviceLoadInFlight = true
        defer { deviceLoadInFlight = false }
        do {
            let devices = try await client.fetchDevices()
            availableDevices = devices
            if devices.isEmpty {
                selectedDevice = ""
                liveError = "未发现可用麦克风，请连接设备并检查麦克风权限。"
            } else if let currentLiveDevice, liveSessionActive, devices.contains(where: { $0.name == currentLiveDevice }) {
                selectedDevice = currentLiveDevice
            } else if !selectedDevice.isEmpty {
                if !devices.contains(where: { $0.name == selectedDevice }) {
                    selectedDevice = ""
                    liveError = "此前选择的麦克风已不可用，请重新选择设备。"
                }
            } else {
                let builtIn = devices.first { device in
                    let name = device.name.lowercased()
                    return name.contains("macbook") || name.contains("built-in") || name.contains("builtin") || name.contains("内置")
                }
                selectedDevice = (builtIn ?? devices[0]).name
            }
        } catch {
            liveError = "无法获取麦克风列表：\(error.localizedDescription)"
        }
    }

    func toggleListening() async {
        let wantsStart = !liveIsBusy
        await setListening(wantsStart)
    }

    func setListening(_ enabled: Bool) async {
        guard !liveActionPending else { return }
        if enabled && liveIsBusy {
            if livePhase == "stopping" { liveError = "上一段正在停止并完成转写，请稍后再开始。" }
            return
        }
        liveActionPending = true
        liveActionIntent = enabled
        liveError = nil
        liveRevision &+= 1
        let settingsRevision = thresholdRevision
        defer {
            liveRevision &+= 1
            liveActionPending = false
            liveActionIntent = nil
        }
        if enabled {
            if availableDevices.isEmpty { await loadDevices() }
            guard !selectedDevice.isEmpty, availableDevices.contains(where: { $0.name == selectedDevice }) else {
                liveError = liveError ?? "请先选择一个可用麦克风。"
                return
            }
            if hasASRModel == false {
                liveError = "没有找到本地转写模型，请先在设置中配置模型。"
                return
            }
            if currentVADEnabled && vadAvailable == false {
                liveError = "本地 VAD 尚未准备好，请检查模型及运行依赖；未关闭 VAD 或开始录音。"
                return
            }
        }
        do {
            let live: LiveSessionStatus
            if enabled {
                live = try await client.startListening(device: selectedDevice, language: currentLanguage, vadEnabled: currentVADEnabled)
            } else {
                live = try await client.stopListening()
            }
            connected = true
            applyLive(live)
            applyThresholdStatus(live, revision: settingsRevision)
        } catch {
            // Keep all document/copy state intact. Do not blindly retry an
            // action whose response was lost; a read-only poll can reconcile.
            liveError = "\(enabled ? "开始" : "停止")监听未确认：\(error.localizedDescription) 请刷新状态确认后再操作。"
        }
    }
}
