# Audio Agent 工具参考

本参考与技能一起安装，不依赖源码目录。以连接服务实际返回的工具 schema 为准；遇到版本差异先读取 schema，不自行扩大参数范围。

## 六个工具

| 工具 | 参数 | 结果与行为 |
| --- | --- | --- |
| `audio_status` | 无 | `{storage, capture}`；缓存计数、位置及采音状态。不读取转写正文。 |
| `audio_list_clips` | `limit` 默认 `20`，整数 `1–100` | 录音数组，通常含 `id/path/start_time/end_time/duration/source`，按录音时间新到旧。没有分页参数。 |
| `audio_recent_transcripts` | `limit` 默认 `5`，整数 `1–100` | 已保存转写数组，按文件修改时间新到旧；可能包含同一录音多次识别，不是按录音去重后的文档，不动态补人物标签。 |
| `audio_get_updates` | `cursor` 默认 `null`；`limit` 默认 `20`，严格整数 `1–100`；`wait_seconds` 默认 `0`，有限数 `0–30` | 文档增量页，详见下节；读取已保存正文和最新人物标签，不运行推理。 |
| `audio_transcribe_clip` | 必填 `clip_id`；`language` 默认 `"auto"` | 识别一条已保存录音，返回新转写并存盘。ID 来自列表，不是路径。 |
| `audio_transcribe_recent` | `seconds` 默认 `30`，有限数 `0 < seconds <= 600`；`last_utterance` 默认 `false`；`language` 默认 `"auto"` | 选择相对于当前时间的已落盘音频并识别，不等待未来音频、不采音。 |

`language` 可用 `auto`、`zh`、`en` 等模型支持的语言代码。`last_utterance` 只是按声音能量截取最后活动区间，不保证语义上是一句话。跨录音的时间空隙会被省略拼接；转写 `segments` 的偏移相对于选中的输出音频，不能直接当作墙钟时间。

识别结果通常含 `id/text/segments/model/backend/language/created_at/duration_seconds/elapsed_seconds`。片段转写另有 `clip_id` 等来源信息，范围转写另有 `selection`。`language: "auto"` 表示请求的模式，不应当作实际检测语言。`elapsed_seconds` 包含子进程启动、模型加载、音频规范化和识别，不包括所有外围存储操作。

## MCP 返回值解包

先检查 MCP `CallToolResult.isError`。不同客户端可能已经解包；直接使用其展示的结构化对象。底层 SDK 当前对 `dict` 结果可能仅提供 JSON 文本 content，对列表可能提供 `structuredContent.result` 及多个文本 content。遵循实际 `outputSchema`；不要假设所有结果都在 `structuredContent`，也不要只取数组返回的第一块文本。

## 增量页与游标

返回形状：

```json
{
  "items": [],
  "next_cursor": "opaque-cursor",
  "has_more": false,
  "initial": false,
  "timed_out": true
}
```

每个条目保留完整 `text`，并含 `id`、`document_key`、`update_sequence`、录音起止时间、来源与 `recording_available`；可含 `clip_id`、`segments`、`speaker_count`。`segments` 可包含相对时间以及 `speaker_ids`、`speaker_labels`。音频可能已过期但文字仍可读，不因 `recording_available: false` 丢弃正文。

- **首次**：省略游标，返回最近 `limit` 条保留中的文档，所选条目按录音时间升序；建立整个当前库的基线，`initial: true`、`has_more: false`。即使指定等待也立即返回。这个 `false` 不表示全库条目都已返回。
- **后续**：传上次 `next_cursor`。新文档、重新识别、人物分析和人物标签变更都可成为更新，按 `update_sequence` 升序返回。多次未读修改可能合并成最新版本。
- **去重**：同一录音最新转写的键为 `clip:<clip_id>`；无片段关联的结果用 `transcript:<id>`。按键替换整条记录，不按人物名称或转写 ID 合并。同名的两个 speaker ID 也可能是不同人。
- **本人参考变化**：用户在 Web 追加、替换或移除本人参考后，已有分析的标签可在读时更新；使用同一键覆盖。不要把参考样本数当成人数，也不要自行回填“我”。六个 MCP 工具均不提供样本管理操作。
- **分页**：`has_more: true` 时传新游标继续，通常令 `wait_seconds: 0` 排空已有页。即使 `items` 为空也保存 `next_cursor`。
- **等待**：没有更新时，`wait_seconds: 0` 立即返回且 `timed_out: false`；有等待并达到时限则 `timed_out: true`。空页/等待超时是成功结果，不是连接错误。
- **失效**：游标与当前 MCP 进程及库绑定；重启或换库不能复用。旧游标错误后省略游标重建，不伪称补齐完整历史。不同客户端各存自己的游标，互不消费。
- **保留期**：只有当前保留中的文档参与计算；没有删除事件、持久事件日志或无限历史回放。

本接口仅在客户端发起调用时工作，不向 Codex/Claude 主动推送、不自动唤醒 Agent，也不触发 ASR。服务端没有可依赖的固定 RPM 配额；避免高频空轮询。持续任务可每次等 `20–30` 秒、单个在途请求、错误退避，并受用户给定的时长/完成条件限制。不要把一次“看最近记录”扩成持续监控。

## 常见失败与恢复

| 结果 | 下一步 |
| --- | --- |
| 无片段、无转写或空增量页 | 如实说明当前查询范围没有结果；不启动录音补齐。 |
| 游标失效 | 丢弃旧游标，重新建立最近文档基线并说明范围。 |
| ASR/说话人推理锁忙 | 保留请求目标，说明正在处理其他任务；不要并发重试或持续抢锁。 |
| 本地模型/依赖缺失 | 告知缺少项。读取已有文字仍优先尝试；模型准备作为用户另行授权的操作。 |
| ASR 超时或客户端断线 | 识别可能已经完成并保存；先检查近期记录的片段 ID/时间，再决定是否重试。 |
| 资料库已迁移、旧固定路径或权限错误 | 让客户端更新路径并重连；不要选择空目录规避错误。 |
| 当前客户端没有工具 | 说明连接尚未在本会话生效，提示按客户端流程重载；不要编造调用结果。 |

`audio_status` 不提供完整 Web 实时任务队列。查询成功不意味着存在可用模型或采音权限；这些能力只在相应操作真正执行时才能验证。
