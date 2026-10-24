# MCP 接入：Claude Code 与 Codex

Audio Agent 提供本机 **stdio MCP**。客户端启动项目的 Python 子进程，通过标准输入和标准输出调用六个工具；Web 控制台的 HTTP 端口不是 MCP 地址。接入后可以读取已完成的转写，也可以在用户明确要求时运行本地 ASR。

本文面向在自己的 Mac 上新安装项目的用户。先完成[安装](installation.md)，再按本文配置客户端。工具参数和增量读取规则见 [MCP 使用](mcp-usage.md)。客户端文档核查日期：2026-10-24；客户端界面、组织策略和实际可用功能可能随版本变化。

## 让 Agent 自动完成 MCP + Skill 接入

打开控制台“MCP 接入”，选择“当前项目”或“所有项目（用户级）”，复制对应客户端的完整安装指令并粘贴执行。指令包括依赖检查、预览、安装、协议自检及客户端加载确认；它带上本机项目路径，资料库需要固定时也包含 `--data-dir`。跨电脑粘贴时必须重新定位该电脑的克隆目录。

也可直接复制[完整 Agent 安装文档](agent-setup.md)。仓库的 `scripts/install-agent-integration` 会合并本服务配置，并把 `skills/audio-agent/` 安装到客户端发现目录。Skill 指导六个 MCP 工具的使用；它不是额外模型，也不会自动开麦。

Web 按钮只写剪贴板；实际安装由收到指令的 Agent 执行。客户端信任、重载和 Skill 发现仍须单独核验。下面保留手动接入方式；`mcp-config` 只生成配置，不会安装 Skill。

## 接入前确认

在 Audio Agent 项目根目录执行：

```sh
cd /absolute/path/to/audio-agent
./scripts/audio-agent doctor
```

把示例路径换成你的真实路径。`doctor` 检查 Python、FFmpeg、已安装依赖和可发现的本地模型，不打开麦克风、不下载模型。只读取已有文字不需要加载 ASR 模型；运行 `audio_transcribe_*` 前需要准备[本地 ASR 依赖及模型](models.md)。

接入所需的是项目虚拟环境中的 `.venv/bin/python`。不要把它替换成系统 Python，也不要把这个符号链接解析成虚拟环境外的 Python 实体路径，否则可能找不到项目及其依赖。按安装文档完成 editable install 后，客户端从其他工作目录也能导入 `audio_agent`。

本项目 MCP 不需要 API Key、OAuth 或 Web 会话令牌。Claude Code/Codex 自身的登录、订阅、模型调用和 token 用量是另一件事；本地音频识别不产生云 ASR 账单，但发送给客户端模型的转写文字仍会占用其上下文和相应额度。

## 先确定客户端的工作区根目录

优先把项目配置放在**你实际使用 Claude Code/Codex 打开的工作区根目录**，并保留现有的其他服务设置。例如：

```text
workspace/                     ← 客户端打开这个目录
├── .mcp.json                  ← Claude Code 项目配置
├── .codex/config.toml         ← Codex 项目配置
└── audio-agent/               ← Audio Agent 源码、.venv、默认 .data
```

如果客户端直接打开 `audio-agent/`，配置就放在那里。不要以为只在某个子目录放配置，父目录会话就一定发现它。机器绝对路径由各使用者自行生成，不应把某个人的工作目录作为其他人的配置模板。

生成配置只打印文本，不写入客户端配置、不完成客户端信任或批准，也不验证连接：

```sh
./scripts/audio-agent mcp-config --client claude
./scripts/audio-agent mcp-config --client codex
```

把输出里的 `audio-agent` 服务项合并进所选配置文件。若文件已经存在，不要用 `>` 整份覆盖它。若已有同名服务，更新该项；不要重复添加相同 TOML 表或多个互相覆盖的 JSON 键。

Web 控制台的「MCP 接入」页也能生成配置。它使用当前库与本机保存位置进行比较：一致时跟随本机设置；不一致或设置损坏时，用 `--data-dir` 固定到当前库。页面会说明是哪一种，并把连接状态显示为未知，因为它不探测 Claude/Codex 的进程或账号。其配置和 CLI 输出都需要由客户端实际加载。

## Claude Code：项目级接入

把 CLI 生成的 JSON 中 `mcpServers.audio-agent` 合并到工作区根目录 `.mcp.json`。一个独立的新文件形如：

```json
{
  "mcpServers": {
    "audio-agent": {
      "command": "/absolute/path/to/audio-agent/.venv/bin/python",
      "args": ["-m", "audio_agent", "mcp"]
    }
  }
}
```

也可以让已安装的 Claude Code CLI 写入项目配置。以下是另一种接入方式，与手工合并二选一：

```sh
audio_agent_project='/absolute/path/to/audio-agent'
cd /absolute/path/to/your/workspace
claude mcp add --scope project --transport stdio audio-agent -- \
  "$audio_agent_project/.venv/bin/python" -m audio_agent mcp
```

`--` 后是服务命令。Claude 的 `project` scope 使用项目根 `.mcp.json`；交互会话可能要求你批准项目 MCP。配置写入后在这个工作区执行 `claude mcp get audio-agent`，并在 Claude 会话中用 `/mcp` 确认。以客户端报告和实际调用结果为准，不把“已生成配置”当成“已连接”。[Claude Code 官方 stdio 与作用域说明](https://code.claude.com/docs/en/mcp)

## Claude Code：用户级接入

只有希望自己的多个项目都能访问同一份音频库时，才选择用户级方式：

```sh
audio_agent_project='/absolute/path/to/audio-agent'
claude mcp add --scope user --transport stdio audio-agent -- \
  "$audio_agent_project/.venv/bin/python" -m audio_agent mcp
```

`user` scope 跨项目，存放在 `~/.claude.json`。Claude 的默认 `local` scope 只针对当前项目，也存在这个用户文件中；它不等于用户级共享。终端客户端同名定义通常按 local、project、user 的优先顺序选取整项。若生成后仍指向旧路径，先检查是否存在更高优先级的同名配置；具体客户端形态的例外以官方说明为准。[Claude Code 官方作用域规则](https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence)

## Codex：项目级接入

将 `mcp-config --client codex` 输出合并到工作区根 `.codex/config.toml`。建议补上 Audio Agent 项目根的 `cwd`，尤其是在父工作区配置子项目时：

```toml
[mcp_servers.audio-agent]
command = "/absolute/path/to/audio-agent/.venv/bin/python"
args = ["-m", "audio_agent", "mcp"]
cwd = "/absolute/path/to/audio-agent"
startup_timeout_sec = 20
tool_timeout_sec = 300
```

当前 CLI 生成器打印 `command`、`args` 和两个超时值，不打印 `cwd`；Web 接入页会打印 `cwd`。上述 `20` 和 `300` 是本项目建议值，不是 Codex 默认值。超时单位是秒，长音频识别可能需要根据机器和模型调整；客户端超时与 ASR worker 超时分别生效。[OpenAI 官方 MCP 配置](https://learn.chatgpt.com/docs/extend/mcp)

Codex 仅加载可信项目的 `.codex/config.toml`，按项目根到当前目录的配置层次合并，靠近当前目录的同名设置优先。把项目配置放在实际工作区根，按客户端提示确认信任，再新建或重载该工作区的会话。[OpenAI 官方配置规则](https://learn.chatgpt.com/docs/config-file/config-basic)

```sh
codex -C /absolute/path/to/your/workspace mcp list
codex -C /absolute/path/to/your/workspace mcp get audio-agent
```

在 Codex 终端会话中用 `/mcp` 查看服务。桌面或 IDE 版本可在 MCP 设置中确认并按界面要求重启连接。客户端加载规则和组织策略可能影响结果；本项目无法代替客户端授予信任。[OpenAI 官方 MCP 管理](https://learn.chatgpt.com/docs/extend/mcp)

## Codex：用户级接入

需要跨项目使用时，可以把同一 TOML 表合并到 `~/.codex/config.toml`，或用已安装的 CLI 添加：

```sh
audio_agent_project='/absolute/path/to/audio-agent'
codex mcp add audio-agent -- \
  "$audio_agent_project/.venv/bin/python" -m audio_agent mcp
```

随后检查实际写入的服务项，按需补充 `cwd` 和超时值。项目层或命令行覆盖仍可能改变最终配置。无需为本项目执行 MCP OAuth 登录。[OpenAI 官方配置与 CLI 接入](https://learn.chatgpt.com/docs/extend/mcp)

## 检查连接时只调用只读工具

在客户端提出明确请求，例如：

> 调用 Audio Agent 的 `audio_status`，只检查连接和缓存状态，不录音、不重新转写。

正常情况下客户端应能发现六个工具，并得到包含 `storage`、`capture` 的结果。没有录音时 `clip_count` 为 `0`、`active` 为 `false` 是有效状态。随后可请求 `audio_recent_transcripts` 读取已有文字，或用 `audio_get_updates` 建立增量游标。

启动 stdio 服务会打开并在需要时初始化所选资料库，因此“只读工具”不等于“整个服务启动过程完全不写文件”；但启动不会打开麦克风或运行识别。直接在终端运行 `audio-agent mcp` 后没有普通文本输出，通常是在等待 MCP 握手，不能据此判断失败。不要把日志或额外 `print` 写入服务 stdout，它用于协议传输。

## 资料库选择与路径变化

Audio Agent 的资料库选择顺序为：

1. 服务启动命令中的 `--data-dir`。
2. 客户端传给服务的 `AUDIO_AGENT_DATA_DIR`。
3. macOS 本机 `~/Library/Application Support/Audio Agent/storage.json` 中保存的位置。
4. 当前安装项目的 `.data`；新安装和已有安装都使用这个默认值。

已有项目 `.data/audio.sqlite3` 会继续使用。这里的“项目”是 Audio Agent 的安装位置，不是每个任意调用它的客户端工作区。macOS 配置文件仅保存目录位置，不存录音或声纹。详见[存储与隐私](storage-and-privacy.md)。

正常跟随设置时不要固定 `--data-dir`。确实需要访问另一份库时，可以显式生成固定配置：

```sh
./scripts/audio-agent --data-dir '/absolute/path/to/audio-library' \
  mcp-config --client codex
```

切换资料库后，已运行的 MCP 进程仍持有旧库；请在客户端断开并重连服务，丢弃旧 `audio_get_updates` 游标。迁移后的旧库会拒绝新写入，避免两个位置分叉。若配置带固定参数或环境覆盖，先更新或移除它；否则重启仍选择旧位置。

移动项目、重建 `.venv`、换电脑后，重新生成绝对路径配置。资料库切换不需要修改跟随式配置，但客户端必须重新启动服务。损坏的存储 JSON 会明确报错；不要通过随意新建空库掩盖原库不可访问的问题。

## 连接故障排查

| 现象 | 检查与处理 |
| --- | --- |
| 看到配置文字，但客户端没有工具 | 确认文件位于实际工作区根、没有更高优先级同名项，并完成客户端的加载/信任流程。 |
| `No module named audio_agent` 或缺依赖 | 使用本项目 `.venv/bin/python`，按安装文档完成依赖安装；不要指向系统 Python。 |
| 找不到 FFmpeg | 按安装文档安装；图形客户端的 PATH 可能不同，必要时在服务环境中设置 `AUDIO_AGENT_FFMPEG` 为已有可执行文件绝对路径。 |
| 连接时报资料库已迁移 | 重连服务，移除旧固定 `--data-dir`/环境覆盖，确认新的保存设置。 |
| 配置 JSON/TOML 解析失败 | 检查转义、重复键或表；从生成器重取片段后合并，保留其他服务。 |
| HTTP 404、不是 MCP 响应 | 使用本地 command/args 的 stdio 方式；`http://127.0.0.1:8767/` 和 CLI Web 默认端口 `8765` 都不是本项目的 MCP endpoint。 |
| 工具发现成功，但识别失败 | MCP 连接已经成立；转到[模型配置](models.md)与[故障排查](troubleshooting.md)检查本地模型、内存、语言和超时。 |
| CLI 看到了服务，旧会话仍没有 | 新建或重载客户端会话，在当前会话的工具列表重新确认；CLI 解析成功不保证旧会话已重新加载。 |

读取结果会进入调用方上下文。若调用方使用云模型，文字可能由该模型服务处理；stdio 本地传输不等于整个工作流离线。返回值也可能包含资料库、模型和音频文件的本机路径。只在愿意向该客户端提供这些内容的工作区启用服务。

## 实现与核查依据

- 项目配置生成：[CLI](../audio_agent/cli.py)、[Web 配置描述](../audio_agent/mcp_info.py)、[目录解析](../audio_agent/storage_config.py)。
- 协议和隔离测试：[MCP stdio 测试](../tests/test_mcp.py)、[接入信息测试](../tests/test_web_mcp.py)。
- 官方客户端规则：[Claude Code MCP](https://code.claude.com/docs/en/mcp)、[OpenAI MCP](https://learn.chatgpt.com/docs/extend/mcp)、[OpenAI 配置层次](https://learn.chatgpt.com/docs/config-file/config-basic)。

本文不声称任何读者机器上的客户端已连接，不承诺云端会话能启动读者 Mac 上的 stdio 进程，也不要求降低客户端的权限或组织策略。

## 文档导航

[产品设计](product-design.md) · [使用指南](user-guide.md) · [技术设计](technical-design.md) · [API 参考](api-reference.md) · [安装](installation.md) · [模型](models.md) · [MCP 接入](mcp-integration.md) · [MCP 使用](mcp-usage.md) · [存储与隐私](storage-and-privacy.md) · [性能](performance.md) · [开发](development.md) · [故障排查](troubleshooting.md)
