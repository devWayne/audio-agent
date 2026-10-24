import Foundation

enum StorageConfigurationError: LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        if case let .invalid(message) = self { return message }
        return nil
    }
}

/// Read-only resolution shared with Python's storage_config precedence.
/// Resolving a location never creates a directory or opens a database.
enum StorageConfiguration {
    static func configURL(home: URL) -> URL {
        home.appendingPathComponent("Library/Application Support/Audio Agent/storage.json")
    }

    static func defaultDirectory(projectRoot: URL) -> URL {
        projectRoot.appendingPathComponent(".data", isDirectory: true)
    }

    /// Filesystem access may wait for a macOS privacy authorization prompt.
    /// Keep that wait away from the menu bar's main actor.
    static func resolveAsync(projectRoot: URL, home: URL? = nil,
                             environment: [String: String] = ProcessInfo.processInfo.environment) async throws -> URL {
        try await Task.detached(priority: .userInitiated) {
            try resolve(projectRoot: projectRoot, home: home, environment: environment)
        }.value
    }

    static func resolve(projectRoot: URL, home: URL? = nil,
                        environment: [String: String] = ProcessInfo.processInfo.environment) throws -> URL {
        let userHome = try directory(home?.path ?? environment["HOME"] ?? FileManager.default.homeDirectoryForCurrentUser.path,
                                     absolute: true, base: projectRoot, home: home ?? FileManager.default.homeDirectoryForCurrentUser)
        // An explicit environment override is also a recovery route when the
        // saved JSON is malformed. Relative values match the child process cwd.
        if let override = environment["AUDIO_AGENT_DATA_DIR"] {
            return try directory(override, absolute: false, base: projectRoot, home: userHome)
        }
        if let saved = try readSaved(configURL(home: userHome), projectRoot: projectRoot, home: userHome) {
            return saved
        }
        let legacy = projectRoot.appendingPathComponent(".data", isDirectory: true)
        let database = legacy.appendingPathComponent("audio.sqlite3")
        let databaseAttributes = try? FileManager.default.attributesOfItem(atPath: database.resolvingSymlinksInPath().path)
        if databaseAttributes?[.type] as? FileAttributeType == .typeRegular {
            return try directory(legacy.path, absolute: true, base: projectRoot, home: userHome)
        }
        return try directory(defaultDirectory(projectRoot: projectRoot).path, absolute: true, base: projectRoot, home: userHome)
    }

    private static func directory(_ value: String, absolute: Bool, base: URL, home: URL) throws -> URL {
        guard !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !value.contains("\0") else {
            throw StorageConfigurationError.invalid("数据目录不能为空或包含空字符。")
        }
        if absolute && !value.hasPrefix("/") {
            throw StorageConfigurationError.invalid("已保存的数据目录必须为绝对路径，请检查 storage.json。")
        }
        var expanded = value
        if !absolute {
            if value == "~" { expanded = home.path }
            else if value.hasPrefix("~/") { expanded = home.appendingPathComponent(String(value.dropFirst(2))).path }
            else if value.hasPrefix("~") {
                expanded = (value as NSString).expandingTildeInPath
                guard !expanded.hasPrefix("~") else {
                    throw StorageConfigurationError.invalid("无法解析数据目录中的用户主目录。")
                }
            }
        }
        let url = (expanded.hasPrefix("/") ? URL(fileURLWithPath: expanded, isDirectory: true)
                   : base.appendingPathComponent(expanded, isDirectory: true)).standardizedFileURL.resolvingSymlinksInPath()
        var isDirectory: ObjCBool = false
        if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory), !isDirectory.boolValue {
            throw StorageConfigurationError.invalid("数据目录指向文件，请在存储位置设置中选择目录。")
        }
        return url
    }

    private static func readSaved(_ url: URL, projectRoot: URL, home: URL) throws -> URL? {
        do {
            let attributes: [FileAttributeKey: Any]
            do { attributes = try FileManager.default.attributesOfItem(atPath: url.path) }
            catch let error as CocoaError where error.code == .fileReadNoSuchFile { return nil }
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  let size = attributes[.size] as? NSNumber, size.intValue <= 16_384 else {
                throw StorageConfigurationError.invalid("存储配置必须是大小有效的普通 JSON 文件，不能是符号链接。")
            }
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            let data = try handle.read(upToCount: 16_385) ?? Data()
            guard data.count <= 16_384 else {
                throw StorageConfigurationError.invalid("存储配置文件过大。")
            }
            let saved = try decodeSingleDirectory(data)
            return try directory(saved, absolute: true, base: projectRoot, home: home)
        } catch let error as StorageConfigurationError { throw error }
        catch {
            throw StorageConfigurationError.invalid("无法读取存储配置 \(url.path)。请修复配置，或通过 AUDIO_AGENT_DATA_DIR 指定资料库。")
        }
    }

    // A single-field parser rejects duplicate and unknown keys rather than
    // allowing JSONDecoder's last-key-wins behavior to select another library.
    private static func decodeSingleDirectory(_ data: Data) throws -> String {
        let bytes = Array(data)
        var index = 0
        func invalid() -> StorageConfigurationError {
            .invalid("存储配置只能包含字符串字段 data_dir，请检查 storage.json。")
        }
        func whitespace() {
            while index < bytes.count && [UInt8(32), 9, 10, 13].contains(bytes[index]) { index += 1 }
        }
        func consume(_ expected: UInt8) throws {
            whitespace()
            guard index < bytes.count && bytes[index] == expected else { throw invalid() }
            index += 1
        }
        func string() throws -> String {
            whitespace()
            let start = index
            try consume(34)
            var escaped = false
            while index < bytes.count {
                let byte = bytes[index]
                index += 1
                if byte == 34 && !escaped {
                    return try JSONDecoder().decode(String.self, from: Data(bytes[start..<index]))
                }
                if byte == 92 && !escaped { escaped = true } else { escaped = false }
            }
            throw invalid()
        }
        try consume(123)
        guard try string() == "data_dir" else { throw invalid() }
        try consume(58)
        let value = try string()
        try consume(125)
        whitespace()
        guard index == bytes.count else { throw invalid() }
        return value
    }
}
