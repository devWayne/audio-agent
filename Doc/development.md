# 开发、测试与贡献

更新日期：2026-10-24。当前 Python 包版本为 `0.1.0`，菜单栏构建脚本声明 `0.1.4 (5)`。本文命令以项目根目录为工作目录；构建与部署分开，编译成功不代表已替换正在运行的应用或服务。

开发前阅读根目录 [AGENTS.md](../AGENTS.md)、[产品设计](product-design.md)和[技术设计](technical-design.md)。安装前置条件、模型获取与使用限制分别见[安装指南](installation.md)和[本地模型](models.md)。

## 1. 环境与依赖

Python 声明支持 `>=3.11`，当前开发命令推荐 3.12。macOS 采集需要提供 AVFoundation 的 ffmpeg。菜单栏构建脚本使用 Apple Command Line Tools/Xcode 的 Swift 编译器，Swift 5 语言模式、Apple Silicon macOS 13 target；不能据此声称已验证 Intel Mac 或独立可分发安装。

```bash
uv sync --python 3.12 --extra asr --extra speech --extra dev
.venv/bin/python -m audio_agent --help
.venv/bin/python -m audio_agent doctor
```

首次 `uv sync` 会安装软件依赖，可能访问包索引；这与运行时不下载模型的约束不同。`doctor` 不运行模型或打开麦克风，但会检查本地工具和模型文件。缺少 ffmpeg 时可先运行无采集的单元测试，再按安装指南补齐。

| 依赖组 | 当前声明 | 用途 |
| --- | --- | --- |
| 基础 | `mcp>=1.26,<2`、`tomlkit>=0.13,<1` | stdio MCP，以及保留原 TOML 注释/配置的接入安装器。 |
| `asr` | `pywhispercpp>=1.4,<2` | 本地 whisper.cpp 绑定。 |
| `speech` | `onnxruntime>=1.20,<2`、`sherpa-onnx>=1.12,<3` | Silero VAD、离线说话人分析和向量提取。 |
| `dev` | `pytest>=8,<10`、`pytest-asyncio>=1,<2` | Python 单元、异步和接口回归。 |
| 外部工具 | ffmpeg、Node.js、Swift 工具链 | 音频转换/采集、文档逻辑测试、原生客户端编译。 |

`uv.lock` 记录解析版本。升级运行库时同时审查 lockfile、原生 ABI、模型格式和离线 smoke；不要把重新安装依赖与自动下载用户模型捆绑。已有 Handy 模型只读复用，开发程序不得改 Handy 的历史和设置。

## 2. 代码导览

```text
audio_agent/
  cli.py, __main__.py       # CLI入口、前台/后台采集控制
  config.py                # 项目与ffmpeg入口
  storage_config.py        # 只读位置解析与显式配置保存
  storage_migration.py     # 持锁复制、提交/回滚、旧库fence
  store.py                 # SQLite/WAV、稳定导入、快照和保留
  capture.py               # ffmpeg采集与真实PCM状态
  activity.py, vad.py      # 动态分段、本地Silero适配
  live.py                  # Web会话、顺序ASR与停止收尾
  pipeline.py              # 推理锁、稳定快照、转写保存
  asr.py, asr_worker.py     # 本地Whisper与一次性worker
  speakers.py, speaker_worker.py
  speaker_service.py       # 队列、显式参考、公共说话人标签
  document.py              # 全文、最新版本选择、稳定分页
  updates.py               # 每客户端独立增量水位
  mcp_server.py, mcp_info.py
  agent_install.py         # MCP + Skill 安装、预览、保留配置和失败恢复
  web.py                   # 本机HTTP与静态资源
  web_assets/              # 无打包器的HTML/CSS/JavaScript
  watcher.py               # 稳定WAV文件夹导入
macos/AudioAgentBar/
  AppMain.swift            # 菜单栏生命周期、服务检查和显式权限申请
  ContentView.swift        # 弹窗UI
  DocumentClient.swift     # HTTP契约、分页、控制与状态竞态处理
  StorageConfiguration.swift
tests/                     # Python、Node、Swift测试
scripts/                   # 本地入口、构建与显式验证脚本
skills/audio-agent/        # 可移植的 Skill、工具参考和界面元数据
Doc/                       # 当前对外文档
Doc/research/              # 有日期和状态边界的选型调研
```

修改模块时以调用链确定范围。例如改动声纹 public 标签，需要同时检查 Web 文档、MCP updates 和 Swift 的段落/复制呈现；改动资料库位置，需要检查 Python、菜单栏 resolver、已有 MCP 参数和旧对象写入 fence。不要只让单一页面看起来正确。

## 3. 日常验证：不打开麦克风

### 3.1 Python 回归

```bash
.venv/bin/python -m pytest -q
```

多数用例使用临时目录、合成 PCM、fake capture/analyzer 和本机测试端口；MCP 测试会走真实 stdio 子进程协议，但禁止其打开麦克风或调用推理。音频格式转换测试在 ffmpeg 存在时可能运行真实转换。

特别注意：`tests/test_vad.py` 的两个 `test_real_installed_vad_*` 用例，在本地 Silero 权重与运行库存在时，会对静音/已存在的公开 fixture 做真实离线 VAD；缺少条件则 skip，不下载。它们不采集麦克风。若本次验证明确禁止任何真实模型推理，可先使用：

```bash
.venv/bin/python -m pytest -q -k 'not test_real_installed_vad'
```

这不是完整模型验收；报告中必须注明排除了哪类测试。不能把 skip 当成通过模型可用性验证。

按改动定位的测试入口：

| 改动范围 | 重点测试 |
| --- | --- |
| WAV/时间轴/导入 | `test_store.py`、`test_store_names.py`、`test_watcher.py`、`test_pipeline.py` |
| 动态分段/VAD/采集 | `test_activity.py`、`test_vad.py`、`test_capture.py`、`test_live_activity.py` |
| 会话/门槛 | `test_live.py`、`test_live_settings.py`、`test_web_live.py` |
| 说话人与人物参考 | `test_speakers.py`、`test_speaker_service.py`、`test_speaker_profiles.py`、`test_speaker_automatic.py`、`test_web_speech.py` |
| 全文/分页/增量 | `test_document.py`、`test_updates.py`、`test_web_contract.py`、`test_mcp.py` |
| 存储配置/迁移 | `test_storage_config.py`、`test_storage_migration.py`、`test_web_storage.py` |
| Web边界/MCP信息页 | `test_web.py`、`test_web_mcp.py` |

跨模块存储、时间轴、采集、watcher 或 MCP 变更在交付前运行全套 Python 测试。开发中可先跑目标文件；已有测试全部通过后，只有新增改动或未解决风险才需要重复扩大验证。

### 3.2 JavaScript 文档逻辑

```bash
node --test tests/document.test.cjs tests/agent-setup.test.cjs
node --check audio_agent/web_assets/app.js
node --check audio_agent/web_assets/document.js
```

测试使用 Node 内置 `node:test`，不依赖浏览器或 npm 打包流程。覆盖完整分页、不同 ID 的同时间条目、标签分组、混合说话人和原文空白保持。它不替代浏览器的布局、键盘操作、音频播放或权限交互检查。

### 3.3 真实 HTTP、合成音频

```bash
.venv/bin/python scripts/verify_web.py
```

默认使用临时库和合成 WAV，创建本机随机端口 HTTP 服务，验证页面、会话、导入、全文、播放范围和保护条件；结束时收尾服务。默认不采音、不 ASR。若显式加 `--audio ... --transcribe`，会变成真实模型测试，必须另行标明。

不要用生产库跑回归。自建服务验证可选择独立库和端口：

```bash
.venv/bin/python -m audio_agent --data-dir "$PWD/.data/development-library" web --port 8766
```

这条命令会建立独立资料库和服务，但不会自动录音。测试前确认端口未被其他任务占用；不要为方便而关闭未知进程。

## 4. Swift 回归与构建

### 4.1 只测试数据层

以下命令复用项目内 module cache，避免每次重建大型 SDK 缓存。测试可执行文件写入 Git 忽略的目录：

```bash
mkdir -p .data/swift-module-cache .data/swift-validation

xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 \
  -module-cache-path "$PWD/.data/swift-module-cache" \
  macos/AudioAgentBar/DocumentClient.swift \
  tests/swift/DocumentClientTests.swift \
  -o .data/swift-validation/DocumentClientTests
.data/swift-validation/DocumentClientTests

xcrun swiftc -swift-version 5 -target arm64-apple-macosx13.0 \
  -module-cache-path "$PWD/.data/swift-module-cache" \
  macos/AudioAgentBar/StorageConfiguration.swift \
  tests/swift/StorageConfigurationTests.swift \
  -o .data/swift-validation/StorageConfigurationTests
.data/swift-validation/StorageConfigurationTests
```

DocumentClient 测试用 URLProtocol 提供内存响应，不连真实 HTTP，不采音。它验证完整分页才提交、Server 头/loopback、CSRF token、仅 invalid_token 重试、开始/停止意图、延迟状态不覆盖用户动作、门槛草稿/保存竞态、标签与完整复制。StorageConfiguration 测试使用临时 home 和项目路径，不修改用户真实存储设置。

整份源码类型检查：

```bash
xcrun swiftc -typecheck -swift-version 5 -target arm64-apple-macosx13.0 \
  -module-cache-path "$PWD/.data/swift-module-cache" \
  macos/AudioAgentBar/*.swift
```

### 4.2 生成 `.app`

```bash
./scripts/build-macos-app
```

输出为 `dist/Audio Agent.app`。脚本编译原生文件、生成 Info.plist，并做本地 ad-hoc codesign/plist 检查。构建会覆盖该输出目录，**不会**重启正在运行的菜单栏或后端。

当前 Info.plist 嵌入构建时项目绝对路径和 8767 端口；Python `.venv`、ffmpeg 和模型仍在外部。这不是可复制到任意 Mac 即用的安装包。项目搬家或换机器后，需要准备环境并重新构建，不能仅修改文档宣称已完成打包。

部署前区分三件事：磁盘上的新源码、磁盘上的新 `.app`、正在运行的旧进程。既有后端可能继续使用启动时已加载的 Python 模块；新 Web 静态资源也不等于原生二进制已更新。若正在采集，先通过已授权的会话停止并等末段完成，再安排明确的服务更新；不能为了测试默默截断录音。

菜单栏只在显式开始时请求系统麦克风权限，启动应用或读取正文不应弹权限请求。退出菜单栏故意不终止共享后端；验证“退出后是否录音停止”时必须遵守这一产品语义。

## 5. 显式离线模型验证

真实模型测试与普通单元测试分开。只使用已经在本地、来源明确且获许可的公开测试音频，不把用户正在录音的内容用作 fixture。脚本不负责下载权重或公开音频。

### 5.1 ASR

```bash
mkdir -p .data/development-model-check
.venv/bin/python scripts/verify_asr.py /absolute/path/to/public-fixture.wav \
  --language en --output .data/development-model-check/asr.json
```

该脚本打印转写结果，只适合公开或已允许输出内容的 fixture。CPU 路径可明确使用 `AUDIO_AGENT_USE_GPU=false`；不要在运行失败时自动静默切换硬件策略。报告音频时长、模型名称/哈希或版本、线程、GPU设置、端到端耗时及运行库版本。

真实 MCP→ASR 链路的现有脚本针对公开 JFK 内容有文字断言，应使用相应 fixture：

```bash
.venv/bin/python scripts/verify_mcp_asr.py /absolute/path/to/jfk.wav \
  --data-dir "$PWD/.data/development-mcp-check"
```

这是主动模型测试，不能把普通 MCP 连接检查替换成它。MCP 配置和协议细节见 [MCP 接入](mcp-integration.md)与 [MCP 使用](mcp-usage.md)。

### 5.2 VAD 与说话人

```bash
.venv/bin/python -m pytest -q tests/test_vad.py -k real_installed_vad

.venv/bin/python scripts/verify_speakers.py \
  --fixture /absolute/path/to/four-speakers-zh.wav \
  --single-fixture /absolute/path/to/jfk.wav \
  --output-dir "$PWD/.data/development-speaker-check"
```

VAD 的语音 fixture 用例还要求项目约定路径中的公开 JFK WAV 已存在，否则 skip。说话人脚本在 CPU 上验证自动/指定人数及同一公共说话人的不重叠参考窗口；不会登记用户“我”或人物资料。输出中可能包含公共测试向量，仍不应混入生产 `speakers/` 或参考库。

已知人数检查通过只说明该 fixture 的链路和结果符合预期；不是 DER/WER/CER 准确率评估。性能与质量记录见[性能](performance.md)，不得把 smoke 结果扩写为各种会议环境下的准确率保证。

## 6. 有界硬件测试：必须明确执行

以下命令会打开真实麦克风；不能放进默认 pytest、自动启动钩子、页面 GET 或后台健康检查。先确认可用设备和本次测试授权，再选精确设备名/索引，不自动换成另一个设备。

```bash
.venv/bin/python -m audio_agent devices

# 连续缓存生命周期测试：脚本约录制3秒，使用独立目录。
.venv/bin/python scripts/verify_capture.py \
  --device '<精确设备名>' \
  --data-dir "$PWD/.data/development-capture-check"

# 动态采集+本地ASR：duration范围1–20秒；实际结束还要等待末段处理。
.venv/bin/python scripts/verify_live_capture.py \
  --device '<精确设备名>' \
  --data-dir "$PWD/.data/development-live-check" \
  --duration 8 --vad --expect-segments 1
```

静音测试可保持 `--expect-segments 0`；说话测试应明确发出可控测试短句。核对 `observed_seconds>0`、真实 `vad_ready`、最终 `capture.active=false` 和 `live.active=false`。有声音才出现可变时长 WAV，长静音不生成 5 秒文件；45 秒连续语音单段行为主要由合成管道回归覆盖，无须每次真实录 45 秒。

权限错误要通过正常系统授权流程解决。不能清除 TCC 数据库、替换用户许可、让 UI 自动化绕过系统确认，或把启动子进程当作权限已允许。硬件/系统交互需记录实际观察；单元测试通过不证明该机器麦克风权限已正常。

## 7. 变更与验收约束

### 7.1 必须保留的行为

1. 明确动作才采集/推理；GET、初始化、刷新、MCP 信息页面不得触发。枚举设备与加载模型是不同能力。
2. Web/菜单栏以声音边界切片；静音无文件，连续讲话不被固定周期拆开。CLI 连续缓存的既有行为继续可用。
3. 保存原文；显示、复制、导出与搜索不能悄悄截正文或重排句内空白。完整文档应在全页校验成功后整批提交。
4. 声纹只来自显式确认的有效当前分析；私有向量不出 HTTP、MCP 公共响应或 Git。相似度不得标成身份概率。
5. 设置保存失败不改变内存已应用值；延迟 GET/状态响应不能覆盖用户新动作或未保存草稿。
6. 迁移只接受空/新目标，不复制模型、测试库或缓存；源库保留，旧对象拒写，导入源 WAV 不改。
7. 模型忙碌、原生崩溃、设备断开、磁盘不足均明确报错，不静默换设备、关闭 VAD、改用云端或丢弃已保存音频。

### 7.2 改测试而不是掩盖回归

优先测试用户可观察的边界与竞态：停止前后末段只保存一次、并发清理后的 fd 仍可读、ASR 保存期间锁仍持有、旧库 fence 在获得锁后检查、任务关闭后不再开始、分析版本变化后拒绝过期登记、同名不同 speaker 不合并、同时间分页无遗漏。

HTTP handler 的无 socket fixture 需要真实契约上的 `_request_lock=threading.RLock()`，以及路由会读取的 manager 字段。生产代码新增必需字段时更新 fixture，不能通过删除断言或让生产校验在测试中静默跳过来“修好”测试。

使用临时库、假模型和 Events 控制竞态；不要用私人的真实转写凑测试，不要无期限 sleep 等待。需要模型或硬件时使用上面的显式脚本，并在结果中分别写清测试层级。

### 7.3 文档与提交内容

- 新 HTTP/CLI 契约同步 [API 参考](api-reference.md)和相关测试；MCP 工具改动同步 [MCP 使用](mcp-usage.md)。
- 模型路径、默认值、限制、支持平台变更同步对应文档，不只改截图。
- PR 描述写清触发条件、最终行为、验证命令和仍未验证的边界，不复述聊天过程。
- 不提交 `.data/`、WAV、转写、声纹、模型、用户绝对路径、token、测试输出或机器专用 MCP 配置内容。提交前检查 `git status`、diff 和被跟踪文件。
- 第三方模型/公开 fixture 的许可、下载来源与再分发限制单独核对；“本地使用”不等于可以打包再分发。
- 不用修改根用户或全局客户端配置完成普通仓库测试。配置测试应使用临时 home/config path；切库测试不得迁移真实资料库。

## 8. 交付检查建议

| 层级 | 应提供的证据 |
| --- | --- |
| Python/API | 相关及最终全套 pytest 结果；失败/skip原因；无真实麦克风副作用。 |
| 文档处理 | Node测试与语法检查；超过200条分页、标签更名、完整复制/导出。 |
| 原生客户端 | 数据层Swift测试、整应用类型检查/构建；状态竞态和权限流程的实际观察。 |
| 模型 | 明确是fake还是本地权重，fixture/参数/耗时；不把链路成功当准确率。 |
| 硬件 | 有界录制、精确设备、收到真实PCM、停止确认、独立数据目录。 |
| 部署 | 当前运行版本/路径/端口，服务是否重启、原会话是否已按授权恢复；不能只说“源码已改”。 |
| 隐私 | 无私人音频/文本/向量/路径进入仓库、日志示例和公开截图。 |

纯文档改动通常只需核对来源、链接、命令与格式，不必重复启动模型、编译程序或录音。遇到运行故障先按[故障排查](troubleshooting.md)分类，避免把权限、进程版本、资料库选择和模型质量混为同一个问题。
