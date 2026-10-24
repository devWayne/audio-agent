# Audio Agent

**Mac 上的本地语音记录助手：说话后生成完整文字，随时复制，也可交给 Claude Code 或 Codex 读取。**

Audio Agent 把麦克风采集、本地转写、说话人分析、文档式阅读与 MCP 接入放在同一个本地资料库里。Web 控制台负责详细管理，Mac 菜单栏负责快速开关和复制。默认数据集中保存在项目 `.data/`，不需要云端 ASR API Key。

当前为 **Alpha 源码项目**：主要验证平台是 Apple Silicon Mac；菜单栏应用需要在目标 Mac 从源码构建，还不是独立安装包。Python 包版本为 `0.1.0`，菜单栏构建版本单独管理。本文与 `Doc/` 按 2026-10-24 的源码整理。

[安装与模型准备](Doc/installation.md) · [使用指南](Doc/user-guide.md) · [MCP 接入](Doc/mcp-integration.md) · [全部文档](Doc/README.md)

## 可以做什么

| 功能 | 当前行为 |
| --- | --- |
| 声音触发记录 | 显式开始监听后采音；达到门槛才形成片段，停顿约 0.8 秒结束，不按固定 5 秒切片 |
| 本地转写 | 使用 whisper.cpp / pywhispercpp 与已有 Whisper GGML 模型；片段结束后更新文字 |
| 完整转写文档 | 最新在前、紧凑展开、单段或全文复制、TXT 下载，保留同一录音的最新转写 |
| 本地 VAD | 可启用 Silero v4 判断人声；缺少模型时明确报错，不自动下载 |
| 说话人和人物备注 | 手动分析，或显式开启新监听片段自动分析；试听确认后可登记“我”及其他人物 |
| Mac 菜单栏 | 监听开关、收音门槛滑块、最近文字、人物标签、搜索与复制 |
| WAV 导入 | 本地导入、格式归一化、目录监听与内容去重，原始源文件保持不变 |
| MCP | 六个 stdio 工具；读取已有结果、增量读取新文字和标签，或明确请求本地转写 |
| 存储位置 | 默认 `.data/`；可查看路径、复制资料到新目录并切换，原目录保留 |

文字在一段语音结束并识别完成后出现，**当前不是逐字流式转录**。声音门槛和 VAD 不能保证排除电视人声或远处同事；说话人分析也不是音源分离或身份认证。完整边界见 [产品设计](Doc/product-design.md)。

## 从源码开始

需要 Apple Silicon Mac、Python 3.11+（已验证 3.12）、[uv](https://docs.astral.sh/uv/getting-started/installation/)、ffmpeg，以及已有兼容模型。构建菜单栏还需要 Xcode Command Line Tools。安装依赖可能联网，模型由使用者自行准备。

克隆本仓库，进入项目根目录后运行：

```sh
git clone https://github.com/devWayne/audio-agent.git
cd audio-agent
uv sync --frozen --python 3.12 --extra asr --extra speech --extra dev
./scripts/audio-agent doctor
./scripts/audio-agent devices
./scripts/audio-agent web --port 8767
```

打开 [本地控制台](http://127.0.0.1:8767/)，选择麦克风和语言，点击“开始监听并自动转写”。首次采音需允许 macOS 麦克风访问。要结束采音，请点击“停止监听”；关闭网页或退出菜单栏不会停止已经启动的后台监听。

**模型不随仓库提供，也不会自动下载。** 如果已经安装 Handy，程序会只读查找其模型目录中的 Whisper GGML `.bin` 文件；Handy 不需要运行，其他模型格式不会因为来自 Handy 就自动兼容。也可在启动后台的同一终端指定已有文件：

```sh
export AUDIO_AGENT_MODEL='/absolute/path/to/ggml-model.bin'
./scripts/audio-agent web --port 8767
```

VAD 和说话人分析还需要各自的 ONNX 文件。先按 [安装指南](Doc/installation.md) 和 [模型配置](Doc/models.md) 检查依赖；不需要的功能可以保持关闭。当前 Web VAD 查找的是 Handy 应用包中的 Silero **v4**，不能直接替换为任意新版模型。没有该模型时，先在 Web「录音设置」取消默认勾选的 VAD 再开始；菜单栏没有独立 VAD 开关，需沿用这个 Web 会话的设置。

### Mac 菜单栏

```sh
./scripts/build-macos-app
open 'dist/Audio Agent.app'
```

菜单栏内可快速开始/停止监听、调整门槛并复制文字。应用连接 `127.0.0.1:8767`，后台未运行时会启动本项目服务；打开应用本身不会开始录音。构建产物绑定项目绝对路径、依赖 `.venv` 和本地模型，移动项目或换 Mac 后须重新安装依赖并构建。[详细操作](Doc/user-guide.md)

首次构建后，重启电脑只需双击 `dist/Audio Agent.app`，不必重新编译。可以将 App 复制到“应用程序”方便打开，但仍需保留原项目环境。当前未自动设置登录自启，也未提供独立 DMG/PKG 安装包；[日常启动与安装说明](Doc/installation.md#52-日常打开与放入应用程序)包括放入“应用程序”、登录项和升级副本的方法。

### MCP 接入

控制台侧栏的 **MCP 接入 → MCP + Skill 一键接入** 可选择当前项目或用户级范围，点击“复制 Codex 完整安装指令”或“复制 Claude Code 完整安装指令”，粘贴到对应客户端执行。Agent 会检查依赖、合并 MCP 配置、安装 `audio-agent` Skill，并分别验证协议与客户端加载状态。

没有打开控制台时，可直接复制 [Agent 一键接入文档](Doc/agent-setup.md) 中的整段指令。客户端可能要求确认信任或重新加载；复制成功不代表安装完成。安装器会保留其他配置、备份改动，并拒绝覆盖未知或被修改的同名 Skill。

手动接入仍可用 CLI 生成纯配置：

```sh
./scripts/audio-agent mcp-config --client claude
./scripts/audio-agent mcp-config --client codex
```

生成后合并到对应客户端配置，保留其他服务，按客户端要求批准并重新加载。stdio 由客户端启动，**不要把 Web 的 8767 地址填成 MCP HTTP 地址**。

- 读取已有文字：`audio_recent_transcripts`。
- 读取新文字与后补人物标签：`audio_get_updates`，后续传回 `next_cursor`。
- 只有明确需要识别时才调用 `audio_transcribe_recent` / `audio_transcribe_clip`。

MCP 不会自动唤醒客户端，也不会自己开始采音。客户端读到的文字会进入其模型上下文；本地 ASR 无云端调用费，不代表客户端模型使用没有 token 或额度成本。[接入步骤](Doc/mcp-integration.md) · [工具与游标用法](Doc/mcp-usage.md)

安装后可说：`$audio-agent 读取最近已完成的转写，保留时间和人物标签，不重新识别。` Claude Code 中使用 `/audio-agent`。

## 数据与性能

- WAV 位于 `.data/clips/`，转写正文及时间信息位于 `.data/transcripts/`（JSON）；TXT 在点击下载时生成，保存位置由浏览器决定。
- 默认保留期为 24 小时，适合短期工作缓存。需要长期保存的文字或原始音频应另行备份；本项目不是永久录音档案系统。
- ASR 和说话人重推理按片段串行运行；安静且队列为空时不运行这两类任务。VAD 和采音仍有持续开销。
- 有 M1 Pro 的短时测量记录，未完成全天功耗/耐久测试，也没有 M4 Max 实测结果。不把 CPU 使用率描述为硬件“损耗率”。

详见 [存储与隐私](Doc/storage-and-privacy.md)、[性能与测量边界](Doc/performance.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [产品设计](Doc/product-design.md) | 目标用户、场景、范围、交互、验收和路线图 |
| [技术方案](Doc/technical-design.md) | 模块、进程、状态机、队列、锁、数据模型和失败恢复 |
| [安装部署](Doc/installation.md) / [模型配置](Doc/models.md) | 新 Mac 从零安装、环境变量、模型文件和权限 |
| [使用指南](Doc/user-guide.md) / [故障排查](Doc/troubleshooting.md) | Web、菜单栏、声纹、阈值、导入和常见问题 |
| [MCP 接入](Doc/mcp-integration.md) / [MCP 使用](Doc/mcp-usage.md) | Claude Code、Codex、配置、工具、游标与调用示例 |
| [CLI 与 HTTP API](Doc/api-reference.md) | 请求参数、返回数据、控制接口和限制 |
| [开发指南](Doc/development.md) / [验收记录](Doc/verification.md) | 代码导览、测试、贡献和已验证范围 |
| [GitHub 发布](Doc/publishing.md) | 发布文件范围、仓库配置、许可证和推送步骤 |
| [调研记录](Doc/research/README.md) | Handy/豆包选型、DJI Mic 3/micSync 接入证据与未验证项 |

## 开发与发布

```sh
.venv/bin/python -m pytest -q
node --test tests/document.test.cjs tests/agent-setup.test.cjs
```

默认测试不打开麦克风；多数使用模拟推理，本地存在 Silero 模型时两项测试会运行离线 VAD。真实 ASR/说话人及硬件验证脚本需显式运行。最近一次完整 Python 回归为 573 项通过，前端文档与安装指令测试 28 项通过，具体环境和限制见 [验收记录](Doc/verification.md)。

提交时不包含 `.data/`、模型、录音、转写、人物声纹、本机 MCP 配置和构建产物。可分享的通用配置位于 [`examples/`](examples/README.md)。本项目尚未选定源码许可证，公开源码不代表已授予 MIT/Apache 等开源许可；所有者确定授权方式后再添加对应 `LICENSE`。第三方运行时与模型仍遵循各自许可证。
