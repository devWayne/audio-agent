# CLI 与 HTTP API 参考

更新日期：2026-10-24；适用于当前 Python `0.1.0` 源码。HTTP 路径目前没有版本前缀，变更应同时更新本文与契约测试。示例路径和 ID 均为占位信息，不代表真实用户资料。

交互操作见[使用指南](user-guide.md)，安装见[安装指南](installation.md)，架构与数据语义见[技术设计](technical-design.md)。stdio MCP 的工具参数、游标调用与客户端配置分别见 [MCP 使用](mcp-usage.md)和 [MCP 接入](mcp-integration.md)，本文不重复其工具细表。

## 1. CLI 约定

在项目根目录执行：

```bash
.venv/bin/python -m audio_agent --help
./scripts/audio-agent --help
```

安装包同时提供 `audio-agent` console script。`scripts/audio-agent` 使用项目自己的 `.venv/bin/python`。全局 `--data-dir` 必须放在子命令前，例如：

```bash
.venv/bin/python -m audio_agent --data-dir /absolute/path/to/library status
```

未显式覆盖时依次使用 `AUDIO_AGENT_DATA_DIR`、本机保存的 `storage.json`、项目 `.data`。CLI 多数结果为 JSON；错误写入 stderr 并返回退出码 1，正常完成/用户中断通常为 0。`mcp` 的 stdout 是协议流，不可混入自定义日志。

### 1.1 命令表

| 命令 | 参数与默认值 | 行为 |
| --- | --- | --- |
| `doctor` | 无子参数 | 检查 Python/包、ffmpeg 和已存在的兼容模型；不加载模型、不录音。缺少必要运行条件可返回错误。 |
| `devices` | 无子参数 | 调用 ffmpeg 枚举 macOS AVFoundation 输入，返回 `{index,name}[]`；不启动录音会话。 |
| `status` | 无子参数 | 返回 `{storage,capture}`。CLI 会构造资料库对象，因此首次调用可能建立空库；不同于已运行 HTTP 的只读 GET。 |
| `record` | `--device` 必填；`--chunk-seconds 5`；`--retention-hours 24`；可选 `--duration` | 前台连续缓存；Ctrl+C/SIGTERM 保存最后一块后停止。仅采集，不自动 ASR。 |
| `start` | 与 record 相同，但没有 `--duration` | 后台连续缓存；等待真实 PCM 就绪，不只以子进程创建成功作为开始成功。 |
| `stop` | 无子参数 | 按资料库状态和命令身份停止 CLI 后台采集；不向身份不匹配的 PID 发信号。 |
| `import-wav PATH` | `--source manual-import`；可选 `--start-time UNIX_SECONDS` | 稳定快照、必要时本地规范化、按原文件哈希去重；不 ASR。默认开始时间为源文件 mtime 减时长。 |
| `clips` | `--limit 20`，范围 1–100 | 列出已提交片段；底层列表按录音时间倒序，不等同于 Web 入库排序及逻辑过期过滤。 |
| `transcribe` | `--seconds 30`；`--last-utterance`；`--clip ID`；`--language auto`；`--model PATH`；`--copy`；`--text` | 显式本地 ASR。指定 `--clip` 时优先转写该片段；否则处理最近窗口。保存结果后可输出纯文本、复制剪贴板。 |
| `transcripts` | `--limit 5`，范围 1–100 | 读取保留期内已完成转写，按文件修改时间倒序。 |
| `watch FOLDER` | `--interval 2`，至少 0.2 秒；`--stable-seconds 3`；`--include-edits` | 周期导入稳定 WAV；默认跳过 Edit/_edit 文件，不自动 ASR。停止用 Ctrl+C。 |
| `prune` | `--retention-hours 24` | 显式清理录音/导出与转写。返回 `{audio,transcripts_removed}`。不删除原始外部录音或显式人物参考库。 |
| `web` | `--port 8765`，CLI 范围 1–65535 | 启动本机 Web 服务；开服务不采音。按 Ctrl+C 关闭并收尾本服务任务。 |
| `mcp` | 无子参数 | 启动 stdio MCP；不自动采音。详见 MCP 文档。 |
| `mcp-config` | `--client codex`，可选 `claude` | 输出配置文本，不修改客户端配置文件。显式 `--data-dir` 会固定库；未传时跟随位置解析。 |

`CaptureConfig.chunk_seconds` 支持 0.1–300 秒，仅用于 CLI 连续缓存。Web/菜单栏没有周期切片参数。CLI 最近窗口 ASR 要求 `0 < seconds <= 600`；`--last-utterance` 使用能量活动选择窗口，不是 Silero VAD 或说话人识别。CLI `--language` 支持本地 Whisper 的语言代码，HTTP 为了保持界面契约仅开放 `auto/zh/en`。

快捷脚本 `scripts/transcribe-last` 等价于最近 60 秒、选择最后能量活动、复制并输出纯文本；它会运行本地模型。不要把它当作只读历史查询。

### 1.1 MCP 与 Skill 接入脚本

独立接入脚本不属于 HTTP 写接口：

- `.venv/bin/python scripts/install-agent-integration --client codex|claude [--scope project|user] [--workspace ABS] [--data-dir ABS] [--dry-run]`：默认 project 和调用时当前目录，实际执行时从每组可选值中选一个。预览不写文件；正式执行合并本服务配置并安装 Skill。成功 JSON 区分 `installed`、`unchanged`、`dry-run`，包含目标路径、备份与警告；冲突退出 1，保留未知配置或用户改过的 Skill。
- `.venv/bin/python scripts/check-mcp-connection [--data-dir ABS] [--timeout 20]`：超时范围 1–120 秒，只做 stdio 初始化、六工具发现和 `audio_status`，不读取转写、不采音或推理。显式转发所需资料库/FFmpeg 环境；成功 `stdio_probe_passed` 仅表示后端协议可用，客户端连接和 Skill 发现仍是 `not_checked`。

完整路径规则和三层验收见 [Agent 一键接入](agent-setup.md)。Web 的复制安装指令按钮只写剪贴板，没有新增 HTTP 安装接口。

## 2. HTTP 基本约定

- CLI `web` 默认 `http://127.0.0.1:8765`；菜单栏构建配置默认 `http://127.0.0.1:8767`。下文用 `BASE_URL` 表示实际服务地址。
- `create_server(root, host="127.0.0.1", port=8765)` 仅接受 loopback；程序化测试允许 `port=0` 自动选择空闲端口，CLI 不接受 0。
- ID 为 32 位小写十六进制 UUID；说话人局部 ID 为 `speaker_1` 等，不是跨录音稳定身份。
- 绝对时间为 Unix 秒；ASR segments 和 speaker turns 的 start/end 为录音内相对秒数。
- JSON 使用 UTF-8。有限数字字段拒绝 `NaN`、Infinity 和不适用的布尔值。
- 成功响应带 `Cache-Control: no-store`。错误统一为 `{"error":{"code":"...","message":"..."}}`，不返回 traceback。
- HEAD 走同一路由和校验，返回对应响应头但没有正文。没有 WebSocket/SSE；实时页面通过轮询完整片段状态获得新文字。

`GET /` 返回工作台 HTML；`/app.js`、`/document.js`、`/app.css` 等静态资源仅从包内 `web_assets` 目录和后缀白名单读取，不提供任意本地文件服务。

### 2.1 Token 与同源

先 `GET /api/session` 取得内存会话 token。所有 POST 必须带 `X-Audio-Agent-Token`。服务重启后旧 token 失效；遇到明确 `403 invalid_token` 可重新取 token 后重试一次，不能对其他失败无条件重放开始、登记或迁移请求。

```http
POST /api/live/settings HTTP/1.1
Host: 127.0.0.1:8765
Origin: http://127.0.0.1:8765
X-Audio-Agent-Token: <current-session-token>
Content-Type: application/json
Content-Length: 21

{"trigger_db": -44.4}
```

上例说明头部约定；实际客户端应计算 body 的 UTF-8 字节长度，而不是照抄 Content-Length。服务校验来访 IP、唯一 Host、实际端口和 Origin；`localhost` 与 `127.0.0.1` 即使都指本机，也不能在一次请求的 Host/Origin 中混用。没有 Origin 的原生或 CLI 请求可用，但写 token 仍必需。跨站 `Sec-Fetch-Site: cross-site` 被拒绝，不提供 CORS。

JSON body 上限 **16 KiB**，必须提供唯一、有效的 Content-Length，不接受 Transfer-Encoding/chunked 上传。socket timeout 为 30 秒。请求路径不能为代理绝对 URL，参数最多 20 个，不允许同名参数重复。

### 2.2 读取不启动处理

下面所有 GET 都不会开启麦克风、执行 ASR、分析说话人、登记声纹或执行保留清理。`GET /api/devices` 会启动短暂 ffmpeg 枚举进程；模型状态会检查已存在文件/依赖，但不把“可发现”当成实际模型已通过推理验证。`GET /api/mcp` 只生成说明，不连接 Claude/Codex。

## 3. 状态、设备、资料库与 MCP 信息

| 方法 / 路径 | 关键响应 |
| --- | --- |
| `GET /api/session` | `{token}`；同一服务实例使用同一内存 token。 |
| `GET /api/status` | `{storage,capture,models,transcript_count,live,vad}`；storage 为逻辑保留期内数量/时长/字节统计，capture 是实际采集状态，live 是当前 Web 会话。 |
| `GET /api/devices` | `{items:[{index,name}]}`；设备列表为空或枚举失败会报错，不自动选择替代设备。 |
| `GET /api/storage` | `{current_dir,default_dir,configured_dir,config_path,source,config_error,paths,retention_hours,can_change,busy_reasons}`；paths 包含 recordings/transcripts。 |
| `GET /api/mcp` | `{transport,server_name,connection_status,data_dir,project_dir,python_path,follows_storage_setting,configuration_note,clients,tools,examples}`。connection_status 固定为 `unknown`。详细配置说明见 [MCP 接入](mcp-integration.md)。 |

`POST /api/storage` 请求体必须精确包含：

```json
{"directory":"/absolute/path/to/new-library","copy_existing":true}
```

成功为 200，返回 `{storage,copied:{files,bytes},previous_dir}`。同步复制并应用新库，同一 HTTP 端口不变，旧库保留；不支持只改路径、不复制已有资料的模式。目标为空/新目录、父目录可用、无 symlink、与旧库无父子关系；有录音、模型任务、待处理队列或固定环境变量覆盖时返回 `409 storage_busy`。配置错误、目标非空等为 `400 invalid_storage_settings`；磁盘/权限错误依失败阶段返回通用错误。迁移后现存 CLI/MCP 需要重启或重连，不能继续向旧库写入。详见[存储与隐私](storage-and-privacy.md)。

## 4. 实时会话与门槛

### 4.1 开始、停止、轮询

`POST /api/live/start`：

```json
{"device":"设备列表中的精确名称或索引字符串","language":"zh","vad_enabled":true}
```

`device` 必须为非空字符串。`language` 可为 `zh/auto/en`，省略为 `zh`；`vad_enabled` 必须为布尔值，**HTTP 省略时为 false**，Web/菜单栏会显式传入其选择。返回 `202 {live}` 仅表示开始请求已受理，必须继续轮询确认真实 PCM 到达。

`POST /api/live/stop` 请求体为 `{}`，只停止该服务创建的实时会话，不能借此停止外部 CLI 采集。停止和最后一段处理未结束时返回 `202 {live}`，已完成时为 `200 {live}`。

`GET /api/live` 返回 `{live}`。重要字段：

| 字段 | 含义 |
| --- | --- |
| `state` | `starting / recording / stopping / finished / error`。 |
| `active` | 会话仍在运行或收尾；不等于麦克风仍开。 |
| `session_id`, `device`, `language`, `started_at` | 当前会话标识与启动信息。 |
| `mode`, `activity_state` | Web 为 activity；活动状态为 `listening` 或 `recording`。 |
| `observed_seconds`, `audio_seconds` | 已消费 PCM 秒数、已提交 WAV 秒数；静音可使前者增加而后者不增加。 |
| `clips_count`, `transcribed_count`, `pending_count` | 本会话片段、成功文字及待处理数量。pending 包括当前和暂存在数据库的积压。 |
| `deferred_count`, `waiting_for_speaker` | 说话人占用共享推理锁时的数据库暂存数量/等待提示。 |
| `silent_count`, `failed_count`, `current_clip_id` | 跳过/失败计数及当前 ASR 片段。 |
| `audio_level`, `audio_peak_level`, `last_audio_at` | 真实 PCM 电平、近期最大 20 ms RMS、最近收到数据时间；不是模拟动画或语音概率。 |
| `vad_enabled`, `vad_engine`, `vad_ready`, `vad_probability` | 请求配置、实际后端、已初始化状态及最近推理概率。 |
| `last_error`, `speaker_error` | 会话/ASR 错误与独立后续说话人错误。 |
| `latest_text`, `latest_transcript_id`, `recent_transcripts` | 本会话即时预览；recent 只保留少量内存记录，完整历史应读 `/api/document`。 |

另含门槛 configured/applied/pending 及 pre-roll/end-silence 参数。`chunk_seconds` 在动态模式为 null，不表示按固定秒数提交。收到 `stopping` 后不要立即再次 start，应等 `active=false`；冲突为 `409 live_busy`。启动、设备或权限错误可能稍后出现在 `last_error`，202 不是成功录音凭证。

### 4.2 热更新门槛

`GET /api/live/settings` 与 `POST /api/live/settings` 成功均返回：

```json
{
  "settings": {
    "trigger_db": -44.4,
    "trigger_threshold": 0.006025595860743578,
    "release_threshold": 0.004017063907162385
  },
  "pending": false
}
```

POST 仅接受 `{"trigger_db": -44.4}`；范围为 **-60 到 -18 dBFS**，必须是有限数字。threshold 由 `10^(trigger_db/20)` 得到，release 为其 `2/3`。设置先保存，进行中的声音事件结束后才应用，不截断发言。

响应 settings 是期望/已保存配置；live 的 `trigger_db/trigger_threshold` 是已应用值，`configured_trigger_db/configured_trigger_threshold` 是保存值，`settings_pending` 表示尚待边界应用。客户端拖动中的草稿不能被轮询覆盖，保存失败不能显示“已生效”。

## 5. 录音库、上传与播放

`GET /api/clips?q=&status=all&limit=50&offset=0` 返回 `{items,total,limit,offset}`。

- `limit` 为 1–200；`offset` 为 0–1,000,000。
- `status` 为 `all/transcribed/pending`。搜索匹配 ID、来源、显示文件名和已有转写文本。
- 按入库时间倒序、录音开始时间和 ID 排序；只显示逻辑未过期录音。
- 每项包含 clip 元数据及 `display_name/label/filename/transcribed/transcript_count/latest_transcript`。`latest_transcript` 含 id/text/created_at；响应不暴露内部 WAV path。

`POST /api/import` 是 **原始 WAV body**，不是 multipart 表单。接受 `audio/wav`、`audio/x-wav`、`audio/wave`、`audio/vnd.wave` 或 `application/octet-stream`；仍会验证文件签名和真实 WAV。最大 **512 MiB**，最少 12 字节。

`X-Filename` 使用 UTF-8 百分号编码（浏览器可 `encodeURIComponent`），解码后必须是 `.wav` 文件名，不含路径分隔符/控制字符，UTF-8 长度不超过 255 字节。缺省 `recording.wav`。服务暂存、校验、规范化并去重后返回 `201 {clip}`。上传中断或转换失败会清理该次暂存文件，不自动 ASR。

`GET /api/clips/{id}/audio` 返回 `audio/wav`，支持单个 `Range: bytes=start-end`、`bytes=start-` 或 `bytes=-suffix`。完整响应 200，部分 206，包含 Accept-Ranges/Content-Range；多范围、越界或无效范围为 416。录音过期、数据库行缺失或文件缺失为 404。只允许按 ID 读取，不接受任意本地文件路径。

## 6. 转写任务与完整文档

### 6.1 手动转写

`POST /api/transcribe` 请求 `{"clip_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","language":"auto"}`，language 可省略；返回 `202 {job}`。一个服务只接受一个 queued/running 手动 ASR，模型锁忙或已有任务为 `409 transcription_busy`。

`GET /api/jobs` 返回 `{items}`，新任务在前；`GET /api/jobs/{id}` 返回 `{job}`。job 字段为 `id/clip_id/language/status/created_at/started_at/completed_at/result/error`，状态 `queued/running/completed/failed`。任务内存不跨服务重启保留；成功文本会独立写入资料库。result 仅完成时可用，调用者必须检查最终状态。

### 6.2 转写历史

`GET /api/transcripts?clip_id=&q=&limit=50&offset=0` 返回 `{items,total,limit,offset}`。分页范围同 clips；clip_id 可选且须有效，q 匹配 text。它返回保留期内各次转写，重转写会有多条，不等同于去重后的完整文档。

`GET /api/transcripts/{id}` 返回完整记录，并尝试附加当前公共说话人标签；不存在、已过期或有效数据未找到为 404。正文和 segments 不截断。

### 6.3 完整文档

`GET /api/document?limit=200&cursor=...&source=...` 返回：

```json
{
  "items": [],
  "total": 0,
  "next_cursor": null,
  "snapshot": 1792790400.0,
  "revision": "opaque-change-fingerprint"
}
```

- `limit` 默认 200，范围 1–200；只允许 limit/cursor/source 三个参数。
- 无 cursor 建立新快照；后续必须保留相同 limit/source，直到 next_cursor 为 null。
- source 是完整来源精确匹配，如 `live:<session_id>`，不是模糊搜索。省略表示所有来源；不要传 `source=all` 表示所有来源。
- 每个 clip 只出现该快照内最新有效转写；没有 clip 的窗口转写按其自身 ID 保留。
- 后端按录音 `(start_time,id)` 升序。想“最新在上”的客户端应收齐全量后统一倒序。
- items 包含 `id,clip_id,text,segments,source,created_at,start_time,end_time,source_kind,display_name,recording_available`，并保留可用语言/backend等字段。已分析的 segments 可有 `speaker_ids[]/speaker_labels[]`，同一段可能多位说话人。
- 音频过期而文本仍保留时仍可显示文本，recording_available 为 false。
- metadata 不一致、重复 ID/游标或数量不完整时，应重取整份文档；不能开放“复制全部”给半份快照。
- 游标为不透明、有界、进程内签名值；服务重启或参数变化需重建。非法游标/重复参数/未知参数为 `400 invalid_document_query`。

revision 包括转写 metadata 和说话人 sidecar、本人参考、人物参考文件的变化；更新名字/备注也会让客户端刷新。它不是客户端连接状态，也不包含私有声纹内容。

## 7. 说话人分析与身份参考

### 7.1 状态与分析

| 方法 / 路径 | 请求 / 响应 |
| --- | --- |
| `GET /api/speakers/status` | `{available,backend,reason,voiceprint,voiceprint_multisample,voiceprint_max_samples,automatic_enabled,automatic_error,queued_count,running_count}`。当前 multisample 为 true、max_samples 为 10；available 只是本地依赖和文件就绪检查。 |
| `POST /api/speakers/settings` | 仅 `{"automatic_enabled":true或false}`；200 返回同状态结构。默认关闭；开启后只接后续新实时 ASR 成功片段，含当前会话；不补历史。 |
| `POST /api/speakers/analyze` | `{clip_id,num_speakers?}`，人数为 null/省略或 1–10 整数；202 返回 `{job}`。录音需未过期且时长不超过 600 秒。 |
| `GET /api/speaker-jobs` | `{items}`；独立于 ASR jobs，任务 `kind=diarization`，包含 automatic/status/waiting_reason/result/error 等。没有单独的 `/api/speaker-jobs/{id}` 路由。 |
| `GET /api/clips/{id}/speakers` | `{analysis:null}` 或公共 analysis；包括版本 id、speaker_count、turns、speakers。 |

公共 speaker 包含 `id,label,speech_seconds,reference_seconds,can_enroll,identity`。identity 可空；有效时含名字/备注/display_label、is_self、profile_id、source、相似度 score。source 区分 `user_reference` 和 `voiceprint_match`。所有接口都不输出 embedding 或原生分析中的任意私有字段。

队列默认最多 12 个 queued/running 任务，模型忙会等待，失败不会让已完成 ASR 变成失败。关闭自动开关只阻止后续自动入队；手动分析仍可显式发起，已排队任务可继续。模型不可用、队列满等为 `409 speaker_unavailable`，参数错误为 `400 invalid_speaker_request`。

### 7.2 “我”的多条参考

`POST /api/voiceprint/enroll` 必须提供 `clip_id`、`speaker_id`、`analysis_id`、`confirm`；可选 `mode`：

```json
{
  "clip_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "speaker_id":"speaker_1",
  "analysis_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "confirm":true,
  "mode":"append"
}
```

调用者须试听并明确确认选择正确；backend 校验 confirm 精确为 true、当前分析版本与有效向量。登记没有固定最低秒数，但短样本不保证匹配稳定。过期分析必须重新确认。成功为 `200 {voiceprint}`，不会重新训练模型或自动加入其他语音。

| `mode` | 含义 |
| --- | --- |
| `append` | 追加为本人参考，保留原有样本；最多 10 条。同一 clip/analysis/speaker 绑定重复追加为幂等操作，不增加数量。 |
| `replace` | 明确将整组本人参考换成当前这一条；不是替换某个指定样本。 |
| 省略 | 为兼容旧客户端，仍按 `replace` 处理。新版网页默认追加，并明确发送 `mode:"append"`。 |

追加去重只限相同 `clip_id + analysis_id + speaker_id`。同一录音重新分析会产生新的 `analysis_id`，再次追加可能新增样本并重复计权；建议选不同录音的独立参考，不要通过反复分析同一段来增加样本数。

同一份分析中的不同 speaker 不能都登记为“我”；已绑定其他人物的声音也不能自动合并进本人。非法模式、超过上限、无效向量、绑定冲突或过期分析返回 `400 invalid_speaker_request`，不应静默改为替换。

`voiceprint` 状态包含 `enrolled,label,created_at,reference_seconds,sample_count,total_reference_seconds,samples`，并提供最后一个现存样本的 `clip_id,analysis_id,speaker_id`（无样本时为 null）。`reference_seconds` 为所有本人样本参考秒数总和，兼容旧字段；`total_reference_seconds` 明确表达同一总量。每个公开样本包含 `id,created_at,clip_id,speaker_id,analysis_id,reference_seconds,model_compatible`，不含 embedding。`model_compatible` 当前为 `null`，表示状态请求不计算或加载当前模型，不表示已经兼容；实际匹配只使用与当前分析模型 ID 一致的样本。

`POST /api/voiceprint/samples/remove` body 精确为 `{"sample_id":"cccccccccccccccccccccccccccccccc"}`，示例 ID 应换成状态返回的真实样本 ID。返回 `200 {voiceprint}`，仅删除这一条参考；不存在或非法 ID 返回 `400 invalid_speaker_request`。删除最后一条会移除 `voiceprint.json`，状态变为未登记。客户端不能根据列表序号构造 ID。

`POST /api/voiceprint/clear` body 为 `{}`，返回 `200 {voiceprint}`。移除全部显式本人参考；已有分析/文本不删除，读时标签恢复匿名或其他有效匹配。

旧版单参考只读兼容，读取不会自动改写文件；首次追加或替换等写入会保存 v2 `samples` 格式，删除全部则直接移除文件。状态、登记和移除都不会把样本原始向量发送给客户端。

### 7.3 其他人物与备注

- `GET /api/speaker-profiles` → `{items,voiceprint}`。
- `POST /api/speaker-profiles/enroll` → 必填 clip_id、speaker_id、analysis_id、confirm、name、note；可选 replace_profile_id。返回 `{items,voiceprint,profile}`。
- `POST /api/speaker-profiles/update` → 精确 `{profile_id,name,note}`，修改显示信息，返回 `{items,voiceprint,profile}`。
- `POST /api/speaker-profiles/remove` → 精确 `{profile_id}`，返回 `{items,voiceprint}`。

name 去首尾空白后为 1–80 字符，note 可空、最长 200 字符，不能含控制字符；名字“我”保留给本人登记操作。最多 100 位其他人物，**每人仍只有一条参考**，不接受本人接口的 `mode` 或样本列表操作。公开 profile 包含 id/name/note/display_label、created_at/updated_at、reference_seconds、clip_id/speaker_id/analysis_id，不含向量。

显式 replace_profile_id 可把同一人物参考更新为另一个 clip/speaker，保留人物 ID 和创建时间；若新选 speaker 已绑定本人或另一人物则拒绝，不会隐式覆盖别人。当前 clip 内的确认来源优先，后续自动匹配还需满足模型版本、相似度与区分差距条件，详见[技术设计](technical-design.md)。

## 8. 常见错误与客户端处理

| HTTP | 常见 code | 处理原则 |
| --- | --- | --- |
| 400 | `invalid_path`、`invalid_parameter`、`invalid_pagination`、`invalid_document_query` | 修正请求或从第一页重建；不继续拼接已失效分页。 |
| 400 | `invalid_live_request`、`invalid_live_settings`、`invalid_transcription`、`invalid_speaker_request`、`invalid_storage_settings` | 读 message 修正字段/值，不静默关闭 VAD 或替换设备。 |
| 400 | `incomplete_body`、`unsupported_transfer`、`invalid_wav`、`invalid_filename` | 上传/请求无效；不要把客户端发送完等同于入库成功。 |
| 403 | `loopback_only`、`invalid_host`、`invalid_origin` | 地址/同源不满足要求；不能通过开放远程绑定绕过。 |
| 403 | `invalid_token` | 重新 GET session，仅对此原因允许一次有界重试。 |
| 404 | `not_found`、`clip_not_found`、`transcript_not_found`、`job_not_found` | 资源已清理、服务已重启或路径不存在；保留已有文本视图，更新可用状态。 |
| 409 | `transcription_busy`、`live_busy`、`speaker_unavailable`、`storage_busy` | 等待任务/会话完成或处理提示，不反向执行用户动作。 |
| 411 / 413 / 415 | `length_required` / `upload_too_large` / `content_type` | 提供准确长度、符合大小限制和正确 MIME。 |
| 416 | `invalid_range` | 重置播放器请求范围。 |
| 500 | `storage_error`、`invalid_storage`、`internal_error` | 检查空间、权限、结构；不得声称操作已提交。 |
| 503 | `server_stopping` | 服务正在收尾，稍后重新连接。 |

部分低层 RuntimeError 会被统一映射为 `400 invalid_request`，客户端应显示服务的中文 message，而不是只匹配一份穷举错误码。未知 code 也必须作为失败处理。

GET 成功不代表录音、转写、模型质量或客户端 MCP 连接成功；202 也不代表任务最终完成。接口的关键验收是最终状态和完整结果，相关回归方法见[开发指南](development.md)。
