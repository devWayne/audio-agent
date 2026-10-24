// Compile with StorageConfiguration.swift and run this isolated executable.
// Every path lives in a temporary HOME; no server, models or microphone.
import Foundation

private struct TestFailure: Error, CustomStringConvertible { let description: String }

@main
struct StorageConfigurationTests {
    @MainActor static func main() async throws {
        let manager = FileManager.default
        let temporary = manager.temporaryDirectory.appendingPathComponent("audio-agent-storage-\(UUID().uuidString)")
        let home = temporary.appendingPathComponent("home")
        let project = temporary.appendingPathComponent("project")
        try manager.createDirectory(at: home, withIntermediateDirectories: true)
        try manager.createDirectory(at: project, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: temporary) }
        let config = StorageConfiguration.configURL(home: home)
        let standard = StorageConfiguration.defaultDirectory(projectRoot: project)
        let legacy = project.appendingPathComponent(".data")
        let saved = temporary.appendingPathComponent("本人资料 库")
        var checks = 0
        func check(_ condition: Bool, _ message: String) throws {
            checks += 1
            if !condition { throw TestFailure(description: message) }
        }
        func resolve(_ environment: [String: String] = [:]) throws -> URL {
            try StorageConfiguration.resolve(projectRoot: project, home: home, environment: environment)
        }
        func same(_ first: URL, _ second: URL) -> Bool {
            first.resolvingSymlinksInPath().standardizedFileURL.path == second.resolvingSymlinksInPath().standardizedFileURL.path
        }
        func write(_ text: String) throws {
            try manager.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data(text.utf8).write(to: config)
        }
        func valid(_ path: String) throws {
            let data = try JSONSerialization.data(withJSONObject: ["data_dir": path], options: [.sortedKeys])
            try manager.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: config)
        }
        func mustThrow(_ name: String, _ action: () throws -> URL) throws {
            do {
                _ = try action()
                throw TestFailure(description: "Expected failure: \(name)")
            } catch is StorageConfigurationError { checks += 1 }
        }

        try check(same(try resolve(), standard), "new installs use project .data")
        try check(!manager.fileExists(atPath: config.deletingLastPathComponent().path), "read creates no configuration directory")
        try check(!manager.fileExists(atPath: standard.path), "read creates no library")
        try manager.createDirectory(at: legacy, withIntermediateDirectories: true)
        try check(same(try resolve(), standard), "an empty .data is not a legacy database")
        let database = legacy.appendingPathComponent("audio.sqlite3")
        try Data("unchanged fixture".utf8).write(to: database)
        try check(same(try resolve(), legacy), "existing legacy database takes precedence over default")
        try valid(saved.path)
        try check(same(try resolve(), saved), "saved directory takes precedence over legacy")
        try check(!manager.fileExists(atPath: saved.path), "reading saved directory does not create it")
        try check(same(try resolve(["AUDIO_AGENT_DATA_DIR": "relative-library"]), project.appendingPathComponent("relative-library")), "relative override matches child project cwd")
        try check(same(try resolve(["AUDIO_AGENT_DATA_DIR": "~/chosen"]), home.appendingPathComponent("chosen")), "tilde override uses injected HOME")
        try check(same(try resolve(["AUDIO_AGENT_DATA_DIR": temporary.path]), temporary), "environment overrides saved")
        try check(try String(contentsOf: database, encoding: .utf8) == "unchanged fixture", "legacy database was not opened or changed")

        for broken in ["{", "[]", "{}", "{\"data_dir\":null}", "{\"data_dir\":7}",
                       "{\"data_dir\":\"relative\"}", "{\"data_dir\":\"~/foo\"}", "{\"data_dir\":\"\"}",
                       "{\"data_dir\":\"/tmp/a\",\"data_dir\":\"/tmp/b\"}",
                       "{\"data_dir\":\"/tmp/a\",\"secret\":\"anything\"}",
                       "{\"data_dir\":\"/tmp/a\"} trailing", String(repeating: " ", count: 16_385)] {
            try write(broken)
            try mustThrow("malformed configuration") { try resolve() }
            try check(same(try resolve(["AUDIO_AGENT_DATA_DIR": saved.path]), saved), "override recovers from bad saved JSON")
        }
        try write("{\"data_dir\":\"\\u002ftmp\\u002f中文\\\"录音\"}")
        try check(same(try resolve(), URL(fileURLWithPath: "/tmp/中文\"录音")), "JSON string escapes decode correctly")
        try write("{\"data_\\u0064ir\":\"/tmp/escaped-key\"}")
        try check(same(try resolve(), URL(fileURLWithPath: "/tmp/escaped-key")), "escaped field name is accepted")
        try valid(database.path)
        try mustThrow("saved directory is a file") { try resolve() }
        try mustThrow("override is a file") { try resolve(["AUDIO_AGENT_DATA_DIR": database.path]) }
        try mustThrow("override empty") { try resolve(["AUDIO_AGENT_DATA_DIR": ""]) }
        try mustThrow("override unknown user") { try resolve(["AUDIO_AGENT_DATA_DIR": "~no-such-audio-agent-user-928374/path"]) }
        try manager.removeItem(at: config)
        try manager.createSymbolicLink(at: config, withDestinationURL: database)
        try mustThrow("config symlink") { try resolve() }
        try manager.removeItem(at: config)
        try manager.createDirectory(at: config, withIntermediateDirectories: true)
        try mustThrow("config is directory") { try resolve() }
        try manager.removeItem(at: config)
        try valid(saved.path)
        try check(same(try resolve(), saved), "first resolution")
        let changed = temporary.appendingPathComponent("changed")
        try valid(changed.path)
        try check(same(try resolve(), changed), "next resolution observes Web's updated config")
        let backgroundResolved = try await StorageConfiguration.resolveAsync(projectRoot: project, home: home, environment: [:])
        try check(same(backgroundResolved, changed), "asynchronous resolution reads the same saved config")
        try write("broken")
        do {
            _ = try await StorageConfiguration.resolveAsync(projectRoot: project, home: home, environment: [:])
            throw TestFailure(description: "asynchronous resolution must surface invalid config")
        } catch is StorageConfigurationError { checks += 1 }
        try manager.removeItem(at: config)
        try manager.removeItem(at: database)
        try manager.removeItem(at: legacy)
        try manager.createDirectory(at: standard.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("occupied".utf8).write(to: standard)
        try mustThrow("default is file") { try resolve() }
        try check(same(try resolve(["AUDIO_AGENT_DATA_DIR": saved.path]), saved), "override remains usable when unselected default is a file")
        try manager.removeItem(at: standard)
        let environmentHome = temporary.appendingPathComponent("environment-home")
        let fromEnvironment = try StorageConfiguration.resolve(projectRoot: project, environment: ["HOME": environmentHome.path])
        try check(same(fromEnvironment, StorageConfiguration.defaultDirectory(projectRoot: project)), "HOME determines settings location while default library stays in project")
        try check(!manager.fileExists(atPath: environmentHome.path), "default resolution creates no HOME settings")
        print("StorageConfiguration: \(checks) checks passed")
    }
}
