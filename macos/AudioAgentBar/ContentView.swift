import AppKit
import SwiftUI

/// Opening the view only reads saved text. Listening starts solely through the
/// explicit control action supplied by the owner.
@MainActor
struct BarContentView: View {
    @ObservedObject var store: DocumentStore
    let onRefresh: () -> Void
    let onToggleListening: () -> Void
    let onOpenConsole: () -> Void
    let onOpenStorage: () -> Void
    let onQuit: () -> Void

    @State private var query = ""
    @State private var copiedID: String?
    @State private var feedback: String?
    @State private var feedbackIsError = false
    @State private var feedbackTask: Task<Void, Never>?
    @State private var thresholdSaveTask: Task<Void, Never>?

    private let accent = Color(red: 0.16, green: 0.46, blue: 0.39)

    private var searchTerm: String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var matchingItems: [TranscriptEntry] {
        guard !searchTerm.isEmpty else { return store.items }
        return store.items.filter {
            $0.text.localizedCaseInsensitiveContains(searchTerm)
                || ($0.displayName?.localizedCaseInsensitiveContains(searchTerm) ?? false)
                || $0.speakerLabels.contains { $0.localizedCaseInsensitiveContains(searchTerm) }
        }
    }

    private var visibleItems: [TranscriptEntry] {
        Array(matchingItems.reversed())
    }

    private var hasSavedText: Bool { !store.items.isEmpty }

    var body: some View {
        VStack(spacing: 0) {
            header
            listeningControls
            thresholdControls
            searchAndActions
            if let message = stateMessage {
                stateBanner(message)
            }
            Divider()
            document
            Divider()
            footer
        }
        .frame(width: 440)
        .frame(minHeight: 400, idealHeight: 600, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
        .tint(accent)
        .task { await store.refreshThresholdSettings() }
        .onDisappear {
            feedbackTask?.cancel()
            feedbackTask = nil
            feedback = nil
            copiedID = nil
            if store.thresholdIsEditing {
                Task { await store.endThresholdEditing() }
            }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "waveform")
                .font(.system(size: 20, weight: .medium))
                .foregroundStyle(accent)
                .frame(width: 32, height: 32)
                .background(accent.opacity(0.09), in: RoundedRectangle(cornerRadius: 10))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("Audio Agent")
                    .font(.system(size: 15, weight: .semibold))
                HStack(spacing: 5) {
                    Circle()
                        .fill(store.connected ? (store.isListening ? accent : Color.secondary.opacity(0.6)) : Color.orange)
                        .frame(width: 5, height: 5)
                    Text(store.statusText)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                    if store.pendingCount > 0 {
                        Text("· \(store.pendingCount) 段待识别")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }
                .accessibilityElement(children: .combine)
            }
            Spacer(minLength: 6)
            Button(action: onRefresh) {
                Group {
                    if store.isLoading {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 13, weight: .medium))
                    }
                }
                .frame(width: 28, height: 28)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(store.isLoading)
            .help("刷新全部转写")
            .accessibilityLabel("刷新全部转写")
            Button(action: onOpenConsole) {
                Image(systemName: "arrow.up.right.square")
                    .font(.system(size: 14))
                    .frame(width: 28, height: 28)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("打开详细控制台")
            .accessibilityLabel("打开详细控制台")
        }
        .padding(.horizontal, 12)
        .padding(.top, 10)
        .padding(.bottom, 8)
    }

    private var listeningControls: some View {
        VStack(alignment: .leading, spacing: 5) {
            Button(action: onToggleListening) {
                HStack(spacing: 7) {
                    if store.liveActionPending {
                        ProgressView()
                            .controlSize(.small)
                            .tint(.white)
                    } else {
                        Image(systemName: store.liveIsBusy ? "stop.fill" : "mic.fill")
                            .font(.system(size: 12, weight: .semibold))
                    }
                    Text(store.liveButtonTitle)
                        .font(.system(size: 13, weight: .semibold))
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 9)
                .foregroundStyle(.white)
                .background((store.liveIsBusy ? Color(red: 0.73, green: 0.23, blue: 0.20) : accent)
                    .opacity(store.canToggleLive ? 1 : 0.5), in: RoundedRectangle(cornerRadius: 7))
                .contentShape(RoundedRectangle(cornerRadius: 7))
            }
            .buttonStyle(.plain)
            .disabled(!store.canToggleLive)
            .accessibilityLabel(store.liveButtonTitle)
            .help(store.liveIsBusy ? "停止麦克风，已录片段会继续完成识别" : "使用下方所选麦克风，开始监听并自动转写")

            if !store.availableDevices.isEmpty {
                HStack(spacing: 5) {
                    Text("输入")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                    Picker("麦克风输入设备", selection: $store.selectedDevice) {
                        ForEach(store.availableDevices) { device in
                            Text(device.name).tag(device.name)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                    .controlSize(.small)
                    .frame(maxWidth: .infinity)
                    .disabled(store.liveIsBusy || store.liveActionPending || !store.connected)
                    .accessibilityLabel("麦克风输入设备")
                }
            } else {
                Text("尚未读取输入设备，可点击右上角刷新。")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if store.livePhase == "stopping" {
                Text(store.pendingCount > 0
                     ? "正在停止监听；已录的 \(store.pendingCount) 段音频仍会继续识别。"
                     : "正在停止监听并保存最后一段声音。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !store.isListening && store.pendingCount > 0 {
                Text("麦克风已停止，正在处理 \(store.pendingCount) 段已录音频，不再录音。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let error = store.liveError, !error.isEmpty {
                HStack(alignment: .top, spacing: 5) {
                    Image(systemName: "exclamationmark.circle.fill")
                    Text(error)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                .font(.system(size: 11))
                .foregroundStyle(Color(nsColor: .systemRed))
                .accessibilityElement(children: .combine)
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 8)
    }

    private var thresholdControls: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 7) {
                Text("收音门槛")
                    .font(.system(size: 11, weight: .semibold))
                Text(store.thresholdLoaded ? String(format: "%.1f dBFS", store.thresholdDraftDB) : "读取中…")
                    .font(.system(size: 10).monospacedDigit())
                    .foregroundStyle(.secondary)
                Spacer(minLength: 4)
                thresholdPreset("默认", value: -44.4)
                thresholdPreset("较高", value: -32)
            }
            Slider(value: $store.thresholdDraftDB, in: -60 ... -18, step: 0.1) { editing in
                if editing {
                    thresholdSaveTask?.cancel()
                    store.beginThresholdEditing()
                } else {
                    Task { await store.endThresholdEditing() }
                }
            }
            .controlSize(.small)
            .disabled(!store.thresholdLoaded || store.thresholdSaving || !store.connected)
            .accessibilityLabel("收音门槛")
            .accessibilityValue(String(format: "%.1f dBFS", store.thresholdDraftDB))
            .help("向右调高，较小的背景声更难触发录音；松手自动保存，不中断当前声段。")
            .onChange(of: store.thresholdDraftDB) { _ in
                // Keyboard and accessibility adjustments may not emit drag
                // callbacks. Coalesce those edits, while mouse drags save on release.
                thresholdSaveTask?.cancel()
                guard !store.thresholdIsEditing, store.thresholdHasUnsavedChanges else { return }
                thresholdSaveTask = Task { @MainActor in
                    do { try await Task.sleep(nanoseconds: 300_000_000) }
                    catch { return }
                    guard !Task.isCancelled, !store.thresholdIsEditing else { return }
                    await store.saveThreshold()
                }
            }
            HStack {
                Text("更灵敏")
                Spacer()
                Text("调高 · 少录背景声")
            }
            .font(.system(size: 9))
            .foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 5) {
                if store.thresholdSaving {
                    ProgressView().controlSize(.mini)
                }
                Text(store.thresholdStatusText)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if store.thresholdHasUnsavedChanges && !store.thresholdIsEditing {
                    Button(store.thresholdError == nil ? "保存" : "重试保存") {
                        Task { await store.saveThreshold() }
                    }
                    .font(.system(size: 10))
                    .buttonStyle(.borderless)
                    .disabled(!store.canSaveThreshold)
                } else if !store.thresholdLoaded && store.thresholdError != nil {
                    Button("重新读取") {
                        Task { await store.refreshThresholdSettings() }
                    }
                    .font(.system(size: 10))
                    .buttonStyle(.borderless)
                }
            }
            if let error = store.thresholdError {
                Text(error)
                    .font(.system(size: 10))
                    .foregroundStyle(Color(nsColor: .systemRed))
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("仅按音量筛选；较响的电视人声仍可能录入，调高也可能漏掉轻声。")
                .font(.system(size: 9))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 8)
    }

    private func thresholdPreset(_ title: String, value: Double) -> some View {
        Button(title) {
            store.thresholdDraftDB = value
            Task { await store.saveThreshold() }
        }
        .font(.system(size: 10))
        .buttonStyle(.bordered)
        .controlSize(.mini)
        .disabled(!store.thresholdLoaded || store.thresholdSaving || !store.connected)
        .help(String(format: "设为 %.1f dBFS 并保存", value))
        .accessibilityLabel("收音门槛\(title)预设")
    }

    private var searchAndActions: some View {
        VStack(spacing: 7) {
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("搜索文字或说话人", text: $query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                    .accessibilityLabel("搜索完整转写或说话人备注")
                if !query.isEmpty {
                    Button {
                        query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.plain)
                    .help("清除搜索")
                    .accessibilityLabel("清除搜索")
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.primary.opacity(0.07)))

            HStack(spacing: 8) {
                Text(searchTerm.isEmpty ? "\(store.items.count) 段完整记录 · 最近在前" : "找到 \(matchingItems.count) / \(store.items.count) 段")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 4)
                if !searchTerm.isEmpty {
                    Button("复制搜索结果") {
                        copy(searchText, message: "已复制 \(matchingItems.count) 段搜索结果")
                    }
                    .font(.system(size: 10))
                    .buttonStyle(.borderless)
                    .disabled(!store.canCopyAll || matchingItems.isEmpty)
                    .help("仅复制当前搜索匹配的记录，按录音时间排列")
                }
                Button {
                    copy(store.fullText, message: "已复制全部 \(store.items.count) 段转写")
                } label: {
                    Label("复制全部", systemImage: "doc.on.doc")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(!store.canCopyAll)
                .help("复制当前保留的全部完整转写，包含时间和说话人标签；不受搜索筛选影响")
                .accessibilityLabel("复制全部 \(store.items.count) 段完整转写，不受搜索筛选影响")
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 8)
    }

    private var stateMessage: String? {
        if let error = store.error, !error.isEmpty {
            return error
        }
        if !store.connected && !store.isLoading {
            return "未连接本地服务。请打开控制台检查服务，再刷新。"
        }
        return nil
    }

    private func stateBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "exclamationmark.circle")
                .font(.system(size: 12))
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 4) {
                Text(store.connected ? "更新未完成" : "本地连接暂不可用")
                    .font(.system(size: 11, weight: .medium))
                Text(message)
                    .font(.system(size: 10))
                    .lineLimit(3)
                    .help(message)
                if hasSavedText {
                    Text("下方保留上次读取的文字；成功刷新后恢复全文复制。")
                        .font(.system(size: 10))
                }
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(Color(nsColor: .secondaryLabelColor))
        .padding(9)
        .background(Color.orange.opacity(0.075), in: RoundedRectangle(cornerRadius: 7))
        .padding(.horizontal, 12)
        .padding(.bottom, 8)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var document: some View {
        if store.isLoading && !hasSavedText {
            emptyState(symbol: nil, title: "正在读取完整转写", detail: "正在从本机工作台整理全部记录。", loading: true)
        } else if !hasSavedText && stateMessage != nil {
            emptyState(symbol: "network.slash", title: "暂时无法读取转写", detail: "尚未确认本地记录。可以刷新重试，或打开控制台检查服务。")
        } else if !hasSavedText {
            emptyState(
                symbol: store.connected ? "text.alignleft" : "network.slash",
                title: store.connected ? "还没有转写记录" : "等待连接本地工作台",
                detail: store.connected ? "点击上方「开始监听并自动转写」，完成的文字会出现在这里；也可在控制台导入录音。" : "这里会保留你已读取的文字。打开控制台连接后，即可查看全部转写。"
            )
        } else if visibleItems.isEmpty {
            emptyState(symbol: "magnifyingglass", title: "没有匹配的文字", detail: "换一个关键词，或清除搜索查看全部记录。")
        } else {
            ScrollView(.vertical) {
                LazyVStack(alignment: .leading, spacing: 5) {
                    ForEach(visibleItems, id: \.id) { entry in
                        transcriptCard(entry)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
            }
            .background(Color(nsColor: .underPageBackgroundColor).opacity(0.45))
            .accessibilityLabel(searchTerm.isEmpty ? "全部转写，最近在前" : "转写搜索结果，最近在前")
        }
    }

    private func emptyState(symbol: String?, title: String, detail: String, loading: Bool = false) -> some View {
        VStack(spacing: 8) {
            if loading {
                ProgressView().controlSize(.small).padding(.bottom, 3)
            } else if let symbol {
                Image(systemName: symbol)
                    .font(.system(size: 26, weight: .light))
                    .foregroundStyle(accent.opacity(0.65))
                    .padding(.bottom, 4)
                    .accessibilityHidden(true)
            }
            Text(title)
                .font(.system(size: 14, weight: .medium))
            Text(detail)
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 290)
            if !loading {
                Button(searchTerm.isEmpty ? "打开控制台" : "清除搜索") {
                    if searchTerm.isEmpty { onOpenConsole() } else { query = "" }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .padding(.top, 3)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func transcriptCard(_ entry: TranscriptEntry) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(timeRange(entry))
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                if entry.speakerCount == nil && entry.speakerLabels.isEmpty {
                    Text("未区分")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                Spacer(minLength: 4)
                Button {
                    copy(transcriptAnnotatedText(entry), message: "这段完整文字及人物标注已复制", entryID: entry.id)
                } label: {
                    Label(copiedID == entry.id ? "已复制" : "复制", systemImage: copiedID == entry.id ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 10))
                }
                .buttonStyle(.borderless)
                .disabled(entry.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("复制这段完整文字及已有说话人标签")
                .accessibilityLabel("复制 \(timeRange(entry)) 的完整文字")
            }
            if entry.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text("这段录音未识别到文字。")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(transcriptParagraphs(entry).enumerated()), id: \.offset) { _, paragraph in
                        annotatedParagraph(paragraph)
                            .font(.system(size: 13))
                            .foregroundStyle(Color(nsColor: .labelColor))
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(Color.primary.opacity(0.06)))
    }

    private func annotatedParagraph(_ paragraph: TranscriptParagraph) -> Text {
        guard let label = paragraph.speakerLabel else { return Text(paragraph.text) }
        return Text(label + "  ")
            .font(.system(size: 11, weight: .medium))
            .foregroundColor(accent)
            + Text(paragraph.text)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if let feedback {
                    Label(feedback, systemImage: feedbackIsError ? "exclamationmark.circle" : "checkmark.circle.fill")
                        .foregroundStyle(feedbackIsError ? Color.orange : accent)
                        .accessibilityLabel(feedback)
                } else if store.isLoading {
                    Text("正在更新，已有文字仍可阅读")
                        .foregroundStyle(.secondary)
                } else if let date = store.lastUpdated {
                    Text("更新于 \(date.formatted(.dateTime.hour().minute().second()))")
                        .foregroundStyle(.secondary)
                } else {
                    Label("本机存储", systemImage: "lock")
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
            }
            .font(.system(size: 10))
            .frame(minHeight: 13)

            HStack(alignment: .center, spacing: 10) {
                Button(action: onOpenConsole) {
                    Label("打开控制台", systemImage: "macwindow")
                        .font(.system(size: 11))
                }
                .buttonStyle(.borderless)
                .help("在控制台收听录音、设置人物备注和本人声纹；匿名编号仅在各自录音内有效")
                Button(action: onOpenStorage) {
                    Label("存储位置", systemImage: "folder")
                        .font(.system(size: 11))
                }
                .buttonStyle(.borderless)
                .help("查看录音和转写目录，或复制资料到新的本地目录")
                Spacer(minLength: 4)
                Button(action: onQuit) {
                    Text("退出菜单栏")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
                .help("只退出菜单栏应用，后台监听不会停止")
                .accessibilityLabel("退出菜单栏应用，后台监听不会停止")
            }
            Text("仅显示当前保留的记录；长期保存请在控制台下载。退出菜单栏不会停止后台监听。")
                .font(.system(size: 9))
                .foregroundStyle(.tertiary)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var searchText: String {
        matchingItems.map { entry in
            "\(timeRange(entry, includeYear: true))\n\(transcriptAnnotatedText(entry))"
        }.joined(separator: "\n\n")
    }

    private func timeRange(_ entry: TranscriptEntry, includeYear: Bool = false) -> String {
        let start = Date(timeIntervalSince1970: entry.startTime)
        let end = Date(timeIntervalSince1970: entry.endTime)
        let dateFormatter = DateFormatter()
        dateFormatter.locale = Locale(identifier: "zh_CN")
        dateFormatter.dateFormat = includeYear ? "yyyy-MM-dd HH:mm:ss" : "MM-dd HH:mm:ss"
        let endFormatter = DateFormatter()
        endFormatter.locale = Locale(identifier: "zh_CN")
        endFormatter.dateFormat = Calendar.current.isDate(start, inSameDayAs: end) ? "HH:mm:ss" : (includeYear ? "yyyy-MM-dd HH:mm:ss" : "MM-dd HH:mm:ss")
        return "\(dateFormatter.string(from: start))–\(endFormatter.string(from: end))"
    }

    private func copy(_ text: String, message: String, entryID: String? = nil) {
        feedbackTask?.cancel()
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        let success = pasteboard.setString(text, forType: .string)
        copiedID = success ? entryID : nil
        feedbackIsError = !success
        feedback = success ? message : "复制未完成，请重试或手动选择文字。"
        feedbackTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard !Task.isCancelled else { return }
            feedback = nil
            copiedID = nil
        }
    }
}
