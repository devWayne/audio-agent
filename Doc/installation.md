# 安装与部署

[文档目录](README.md) · [使用指南](user-guide.md) · [故障排查](troubleshooting.md)

本文面向一台尚未安装 Audio Agent 的 Mac。项目不会替你下载模型、申请云端密钥或打开麦克风；安装 Python 依赖和手动获取模型可能需要网络。

## 1. 支持范围与前置条件

| 项目 | 当前要求与验证范围 |
| --- | --- |
| 主平台 | Apple Silicon Mac；本机验收为 M1 Pro、10 核 CPU、16 GiB 内存 |
| macOS | 原生应用编译最低目标 macOS 13；硬件实测为 macOS 26.2，不能视为所有旧版本都已验收 |
| Python | 项目声明 3.11+；推荐复现时用已验证的 3.12 arm64 |
| 包管理 | uv，使用仓库 `uv.lock` 安装；也可用 pip 的可编辑安装 |
| 音频工具 | ffmpeg，可执行文件须可发现；采音使用 macOS AVFoundation |
| ASR 模型 | 已有 Whisper GGML `.bin`，不是 GGUF、ONNX 或模型名称 |
| 可选人声检测/说话人 | `speech` 依赖和对应 ONNX 文件，见 [模型配置](models.md) |
| 菜单栏编译 | Xcode Command Line Tools，能运行 `xcrun swiftc` 和 `codesign` |
| 开发测试 | `dev` 依赖；Node.js 用于无第三方依赖的前端单元测试 |

没有完成 Intel Mac、Windows 或 Linux 整套采音/UI 验收。部分文件处理模块包含跨平台路径逻辑，不等于已提供这些平台的桌面应用。

## 2. 获取源码与依赖

官方源码仓库为 [devWayne/audio-agent](https://github.com/devWayne/audio-agent)。在准备存放项目的父目录执行：

```sh
git clone https://github.com/devWayne/audio-agent.git audio-agent && cd audio-agent
```

执行前先检查目标 `audio-agent` 目录。若已经存在，核对它是否是所需 checkout：已有项目应直接进入，不要覆盖、删除或强制重置；同名目录另有用途时选择新的目标目录。也可从仓库页面下载 ZIP 并解压到新目录，再进入包含 `pyproject.toml` 的项目根目录。

如果随后安装项目级 MCP，配置目标是 Codex/Claude Code 实际打开的工作区根，不一定是这个源码目录；两者分别指定，见 [Agent 一键接入](agent-setup.md)。

如果使用 Homebrew，可自行安装所需工具：

```sh
brew install uv ffmpeg
xcode-select --install
```

已有 Command Line Tools 时无需重复安装。也可按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 和 [FFmpeg 官方下载入口](https://ffmpeg.org/download.html) 使用其他安装方式。

完整开发/使用环境：

```sh
uv sync --frozen --python 3.12 --extra asr --extra speech --extra dev
./scripts/audio-agent doctor
./scripts/audio-agent devices
```

`uv sync` 在项目建立 `.venv`；首次可能下载 Python 和依赖。`doctor` 检查依赖与可发现的 ASR 模型，`devices` 只枚举音频设备，不开始录音。模型文件缺失不会由安装依赖自动补齐。

只使用基本 ASR 可省略 `speech`；不开发可省略 `dev`。缺少 `speech` 时不要启用 VAD 或说话人分析。没有 uv 时可用以下替代方式，但它不会复现 `uv.lock` 的全部精确版本：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[asr,speech,dev]'
```

建议从源码做 editable install。当前数据默认位置和菜单栏构建均围绕项目目录设计，尚未把 wheel 安装到系统目录作为主要部署方式验证。

## 3. 准备本地模型

有 Handy 时，ASR 默认查找：

```text
~/Library/Application Support/com.pais.handy/models/*.bin
```

找到兼容文件时优先使用 `ggml-large-v3-q5_0.bin`。只读复用，Handy 不必启动；不会读它的历史转写、录音或账号设置。

若使用自备模型，在启动服务的同一终端设置：

```sh
export AUDIO_AGENT_MODEL='/absolute/path/to/ggml-model.bin'
./scripts/audio-agent doctor
```

ASR、VAD、说话人是三套不同模型。格式要求、配置变量和手动准备步骤见 [模型配置](models.md)。没有 Handy 的用户可以先运行基础声音能量门槛 + ASR；当前 Web 的 Silero v4 模型路径没有通用设置入口。

## 4. 启动 Web 控制台

```sh
./scripts/audio-agent web --port 8767
```

打开 `http://127.0.0.1:8767/`。CLI 未指定端口时默认 **8765**，而菜单栏应用固定连接 **8767**；同时使用两者时请显式传入 `--port 8767`。服务只支持本机 loopback 地址，不部署到公网或局域网监听地址。

开始使用：

1. 按状态提示确认模型可用。
2. 选择 `devices` 列出的麦克风和识别语言。
3. 展开「录音设置」核对 VAD。Web 默认勾选 VAD；没有 Handy 的兼容 Silero v4 文件时，取消勾选后再开始，使用基础能量检测。
4. 点击“开始监听并自动转写”，允许 macOS 对相应宿主的麦克风访问。
5. 说一段话，停顿后等待识别结果；完成后点击“停止监听”。

打开页面不会开始录音；关闭页面不会停止录音。前台 Web 服务可用终端 `Ctrl+C` 关闭，关闭前建议先在界面停止监听并等待队列收尾。

## 5. 构建菜单栏应用

### 5.1 新 Mac 首次构建

在新 Mac 克隆后，先完成上面的依赖和模型准备，再执行下列命令。构建通常只需首次进行；重启电脑、退出再打开应用都不需要重新构建。更新原生 Swift 代码或移动项目目录后，才需要再次构建。

```sh
./scripts/build-macos-app
open 'dist/Audio Agent.app'
```

脚本使用本机 Swift 编译器生成 arm64 应用并做本地 ad-hoc 签名，输出到 `dist/`。这不是 Apple 公证或 Developer ID 签名的通用安装包。

应用的项目路径和 8767 端口写入构建时的 `Info.plist`。打开时会验证已有本地服务是否属于同一资料库；没有服务才启动后台，不能替换其他端口占用者。后台使用项目 `.venv`，不会启动录音或推理。

**移动项目后重新构建；换一台 Mac 时重新安装依赖、准备模型、再构建。** 仅复制 `.app` 不包含 Python、模型与录音库。没有设置登录自启；退出菜单栏只退出客户端，监听开关才控制采音。

菜单栏当前没有独立 VAD 开关，首次状态按启用 VAD 处理。没有 Silero v4 时，先在 Web 取消 VAD 并开始一次监听，菜单栏会读取并沿用该后台会话的设置。这不是持久化 VAD 设置：新启动的菜单栏连到尚无会话的新后台时仍默认启用 VAD，重启组合后应重新检查；仅取消 Web 复选框而未实际开始会话不会通知菜单栏。不能只安装 ASR 权重就假定菜单栏默认启动可用。

### 5.2 日常打开与放入“应用程序”

1. 在 Finder 打开项目的 `dist` 文件夹，双击 `Audio Agent.app`。
2. 顶部菜单栏出现波形图标；它没有独立主窗口，也不显示 Dock 图标。
3. 点开图标可查看、复制现有文字；点击“开始监听”才启用采音与本次自动转写。

也可把生成的 `Audio Agent.app` 复制到 `/Applications`（Finder 的“应用程序”），之后从那里双击打开，无需每次打开终端。这个操作不搬移项目或资料库：原克隆目录、`.venv` 和模型必须保留在可用位置。单独移动 `.app` 不改变其中记录的项目路径。重新构建后，`dist` 中的版本会更新，已复制到“应用程序”的副本需要在退出菜单栏后重新复制替换。

**电脑重启后**：双击已有 App 即可恢复菜单栏和后台服务；不用重新克隆、安装依赖或编译。后台重启后的监听初始为停止，需要再次主动点击开始。若自备模型只通过终端临时 `export` 指定，仍须按下一节的启动环境说明恢复配置；打开 App 本身不会记住上一次终端环境。

当前没有应用内“登录时启动”开关，也不会自动注册登录项。如果希望登录后出现图标，可手动到 macOS“系统设置 → 通用 → 登录项与扩展”，在“登录时打开”列表添加这个 App。选择稳定位置中的副本。登录启动只打开界面和后台，不会自动开始录音。参见 [Apple 登录项说明](https://support.apple.com/zh-cn/guide/mac-help/mh15189/mac)。

退出菜单栏与停止监听是两个动作。要结束采音，先点停止；仅退出 App 后，已经开启的后台监听仍会继续。重新打开 App 会连接已有后台。

### 5.3 `.app`、`.dmg` 与独立安装包

| 文件形式 | 含义 | 当前状态 |
| --- | --- | --- |
| `.app` | macOS 应用本体，双击即可启动 | 已有构建脚本，但依赖构建时的项目环境 |
| `.dmg` | 常用于分发 App 的磁盘映像，通常让使用者拖入“应用程序” | 未提供；仅把当前 App 放进 DMG 不会消除外部依赖 |
| `.pkg` | 安装器，可把组件部署到指定位置 | 未提供；当前本地使用不需要它 |

若希望另一台 Mac“下载 → 拖入应用程序 → 打开”即可使用，需要继续实现独立打包：把 Python 后端、运行时和必要工具随 App 部署，去掉构建机绝对路径依赖，提供明确的本地模型选择/导入，并处理分发签名、公证与升级。模型与个人资料仍应分别管理，不把现有录音或声纹打入发行包。现有源码构建流程不等于这个安装体验已经完成。

## 6. 权限与启动环境

- 麦克风权限属于实际启动采音的宿主；终端启动与菜单栏启动可能需要分别确认。
- 项目在“文稿”等受保护目录时，应用或宿主可能请求该文件夹权限。由使用者在系统提示或系统设置中允许。
- 本地重建改变 ad-hoc 签名后，macOS 可能重新确认权限。这是已观察到的行为，不保证每台机器的授权流程相同。
- Finder 启动应用不保证继承交互式终端中的 `export`。自定义模型变量应放到实际后台启动环境；最直接的方式是在已设置变量的终端先启动 Web，再打开菜单栏。
- 仓库提供 [环境变量示例](../examples/.env.example)，程序**不会自动读取 `.env`**；按需导出变量再启动。

## 7. 默认目录与多台电脑

默认资料库为当前项目 `.data/`，不把开发者的主目录写进公开配置。优先级是：

```text
--data-dir > AUDIO_AGENT_DATA_DIR > 本机 storage.json > 项目 .data
```

Mac 本机可选路径配置保存到 `~/Library/Application Support/Audio Agent/storage.json`，不存录音内容。可在“存储位置”页停止监听后复制资料到新空目录。新旧目录保留规则、24 小时保留策略及备份见 [存储与隐私](storage-and-privacy.md)。

在另一台电脑安装时，每台机器分别生成 MCP 配置、重新构建菜单栏。目录名相同不等于自动同步；项目没有跨设备同步服务。

## 8. 首次安装验收

| 验收 | 预期 |
| --- | --- |
| `doctor` / `devices` | 能报告依赖、已有模型与设备；不打开麦克风 |
| 打开 Web / 菜单栏 | 能读取状态，无自动采音 |
| 显式开始并说话 | 有音量变化；停顿后产生有效片段与转写 |
| 安静一段时间 | 不周期性产生空录音；不触发无必要 ASR |
| 显式停止 | 麦克风关闭，已有片段完成收尾 |
| 关闭/重开页面 | 保留期内已有文字继续可读 |
| 开启可选功能 | VAD 或说话人缺模型时报错；不自动下载或回退云端 |

这是一组有界的首次检查，不替代长时间运行、外接设备或不同声场测试。更完整的研发检查见 [开发指南](development.md)。
