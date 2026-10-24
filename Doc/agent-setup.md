# 把安装交给 Codex / Claude Code

把下面整段指令粘贴给**运行在你的 Mac 本机、可以执行终端命令和修改配置文件**的 Codex 或 Claude Code。它会安装 `audio-agent` MCP 配置与配套 Skill，并检查连接；不会录音或下载识别模型。纯网页或云端会话不能直接启动你 Mac 上的 stdio 服务。

已有 Web 控制台时，优先使用「MCP 接入」页生成的完整指令，里面可以带上真实安装目录和当前资料库。没有控制台也可以使用下文，Agent 会先从当前工作区定位已有项目；未找到时可从官方仓库 [devWayne/audio-agent](https://github.com/devWayne/audio-agent) 克隆到新目录，保留原有工作区与文件。

## 整段复制给 Agent

```text
请在这台 Mac 上实际完成 Audio Agent 的 MCP + Skill 接入，不要只给我一份操作建议。按下面规则执行，保留已有客户端配置和正在进行的录音。

目标：让当前使用的 Codex 或 Claude Code 可以通过名为 audio-agent 的本地 stdio MCP，配合 audio-agent Skill 读取已有转写，并在我明确要求时按需运行本地识别。此次安装验收只调用 audio_status，不读取私人转写正文、不录音、不运行 ASR 或说话人分析、不登记声纹、不下载模型。

1. 确定目标并定位项目。
   - 如果我已经指定客户端、作用域、工作区、项目路径或资料库，使用这些值。
   - 否则以当前 Agent 所属客户端为目标（Codex -> codex；Claude Code -> claude），默认 scope=project，workspace=当前客户端实际打开的工作区根目录。不要把 Audio Agent 源码子目录误当作所有客户端的工作区根。
   - 只在目标仍无法判定时问我；不要同时安装两个客户端，也不要默认改用户级配置。
   - 在当前目录或我给出的绝对路径定位 Audio Agent checkout，核对 pyproject.toml、uv.lock、audio_agent/mcp_server.py、scripts/install-agent-integration、scripts/check-mcp-connection 和 skills/audio-agent/SKILL.md。
   - 如果没有 checkout 且我未另指定来源，使用准确的官方地址 https://github.com/devWayne/audio-agent.git。默认可克隆到当前工作区下新的 audio-agent 目录；若我指定了安装目录则使用该位置。克隆前检查目标目录，已有所需 checkout 就复用，同名非项目目录则询问新位置，不覆盖、删除或强制重置。使用其他仓库时只接受我明确提供的准确 URL，不猜同名项目。
   - 克隆源码不改变上面确定的客户端 workspace；项目级 MCP 和 Skill 仍安装到客户端实际打开的工作区根。克隆后再次核对项目文件，只有目标目录或工作区仍无法确定时才询问。
   - 阅读 checkout 的 AGENTS.md 和 Doc/agent-setup.md，使用本版本安装器与参数说明。

2. 检查本机依赖，保持已有运行环境。
   - 检查 uv 和 FFmpeg。缺少时先使用这台机器已采用的包管理方式；已有 Homebrew 可安装缺失的 uv/ffmpeg，不为此自动安装新包管理器或要求 sudo。需要交互权限或本机没有可用安装方式时，说明具体缺失项和下一步。
   - 如果项目尚无 .venv，在 Audio Agent 项目根目录执行：uv sync --frozen --python 3.12 --inexact
   - 如果已有可用 .venv，先核对它的 Python 版本并沿用该解释器，再执行 uv sync --frozen --inexact；不要强制改为 Python 3.12 或重建环境。若运行环境不兼容，需要另行安排更换，不能在正在监听时直接替换。
   - --inexact 必须保留，避免同步时移除已有 ASR/speech extras；它不能保证重建虚拟环境时保留这些依赖。不要停止、重启后台监听，也不要更新锁文件或升级现有模型运行时。
   - 使用项目的 .venv/bin/python，保留这个虚拟环境路径，不把符号链接解析成系统 Python 路径。
   - 可运行 ./scripts/audio-agent doctor 查看依赖和现有模型；只查看发现结果，不下载模型、不查看 Handy 的设置/历史。只读 MCP 不需要 ASR/speech extras 或模型权重，但当前服务初始化需要 FFmpeg。

3. 确认连接的是正确资料库。
   - 默认不传 --data-dir，按本机配置解析：服务显式参数 > AUDIO_AGENT_DATA_DIR > 保存的 storage.json > Audio Agent 项目 .data。
   - 如果我从 Web 控制台复制的指令指定了当前资料库，使用该绝对路径；若明确要求固定访问某库，安装和协议检查都使用相同 --data-dir。
   - 如果运行中的 Web 库与默认解析结果不同，先依据已知控制台状态核实当前库。必要时用 --data-dir 固定到已确认的库；不要为了让检查成功切换到空库。无法核实则报告尚未确认与 Web 一致。
   - 保留现有 MCP 服务 env/权限/超时等设置；如果 audio-agent 项已有 AUDIO_AGENT_DATA_DIR 环境覆盖，必须检查它是否与目标一致，不能只看命令行。不要打印 token、密钥或其他服务的私密配置。

4. 使用仓库安装器，先预览再实际写入。
   - 先执行 .venv/bin/python scripts/install-agent-integration --help，按当前实现使用以下参数：
     --client codex|claude --scope project|user --workspace 实际工作区绝对路径
     仅在上一步需要固定库时添加 --data-dir 已确认的绝对路径。
   - 先用同一组参数加 --dry-run 检查目标和冲突；预览成功且范围符合上述要求后，去掉 --dry-run 执行安装。不要把两个选项值的竖线字面量传入命令。
   - 使用安全参数数组或正确 shell 引号处理空格和路径。不要用重定向整份覆盖已有 .mcp.json、.claude.json 或 config.toml。
   - 安装器应保留其他 MCP 项、客户端设置以及现有授权规则；已有未知同名服务或被改过的同名 Skill 冲突时，保留它们并向我说明具体冲突，不强制删除或覆盖。
   - 检查结果中的 config_path、skill_path、备份路径及 data_dir/follows_storage_setting。技能必须含 SKILL.md 和 references/tools.md，独立于源码 Doc 目录可用。
   - 不降低客户端信任、批准或组织策略；不要写跳过审批配置。用户级配置修改时避免客户端同时写相同文件，必要时等用户关闭该客户端后再处理该写入。

5. 分三层验收，不能把其中一层冒充全部成功。
   A. 文件：确认安装器成功，配置指向本项目 .venv/bin/python，stdio args 正确，Skill 文件完整。MCP 名字是 audio-agent；HTTP Web 端口不是 MCP 地址，不填写假的 MCP URL。
   B. 协议：运行 .venv/bin/python scripts/check-mcp-connection；如果安装时固定了资料库，这里使用同样的 --data-dir。它只做初始化、工具发现和 audio_status，不读取转写正文或启动模型。若客户端保存了会影响库/FFmpeg的服务环境覆盖，检查时也应使用相同的有效环境，避免探针与客户端访问不同库。
   C. 当前客户端：从当前会话实际发现 audio-agent 工具，并仅调用一次 audio_status；确认能发现 Skill。Codex 可显式用 $audio-agent，Claude Code 可用 /audio-agent，普通相关请求也可触发 Skill。
   - 如果当前会话没有新工具或 Skill，说明“文件已安装/协议检查已通过，当前客户端仍需重新加载”，给出本客户端的重连或新开会话步骤；不要虚报已连接，也不要反复重启正在录音的 Audio Agent 后台。
   - 初始 clip_count=0 是有效状态，不导入样本、不读取已有文字来制造演示。服务启动可能初始化数据库，但不会启麦克风。

6. 最后简要报告实际结果。
   - 客户端、project/user 范围、工作区、配置路径、Skill 路径、访问的资料库和备份位置。
   - 文件安装、协议探针、当前客户端实际调用/Skill 发现分别成功还是待完成；失败给出真实原因。
   - 提醒：本地 MCP 无需 API Key；Codex/Claude 本身的登录及模型 token 用量另计。只有未来读取文字时，文字才会进入调用方上下文；使用云模型时这部分不是全程离线。
   - 给我一句安装后使用示例：“使用 Audio Agent 读取最近已经完成的转写，保留时间和说话人标签，不重新识别。”不要现在执行这条示例。
```

## 安装了什么

MCP 是六个实际工具；Skill 是工具选择、隐私边界、人物标签和增量游标的使用说明。二者都以 `audio-agent` 为名称，安装器一起配置。`agents/openai.yaml` 仅提供 Codex 界面元数据，不会单凭这个文件自动安装或连接 MCP。

| 客户端 / 范围 | MCP 配置 | Skill 位置 |
| --- | --- | --- |
| Codex / project | 工作区 `.codex/config.toml` | 工作区 `.agents/skills/audio-agent/` |
| Codex / user | `$CODEX_HOME/config.toml`，未设置时为 `~/.codex/config.toml` | `~/.agents/skills/audio-agent/` |
| Claude Code / project | 工作区 `.mcp.json` | 工作区 `.claude/skills/audio-agent/` |
| Claude Code / user | 默认 `~/.claude.json` | 默认 `~/.claude/skills/audio-agent/` |

Codex 的项目技能按工作目录到仓库根发现；Claude 的项目与个人技能可以显式调用，也可按描述自动选择。安装器保留正常隐式触发，不设置自动执行录音或识别的钩子。[Codex Skill 官方规则](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skill 官方规则](https://code.claude.com/docs/en/skills)

如果使用 `CLAUDE_CONFIG_DIR`，以安装器输出的实际路径为准；不要把默认路径和自定义配置目录混用。项目配置优先满足当前工作区的需要；需要多个工作区共用才选择用户级。客户端仍可能要求项目信任、批准 MCP 或重新加载，且同名的更高优先级配置可能覆盖新条目。[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp)、[Claude Code MCP 作用域](https://code.claude.com/docs/en/mcp)

## 安装后使用

在已经发现 Skill 和工具的新会话里，可以说：

- Codex：`$audio-agent 读取最近 5 条已经完成的转写，保留时间和人物标签，不重新识别。`
- Claude Code：`/audio-agent 读取最近 5 条已经完成的转写，保留时间和人物标签，不重新识别。`
- 按需识别：`用 Audio Agent 转写最近 30 秒已保存的音频，语言中文。`
- 有界跟进：`在接下来两分钟读取新完成的文字并更新摘要；到时停止，不开启录音。`

后两种请求分别授权本地推理和有期限的读取，不意味着允许开启麦克风。全文、重复转写、人物编号和游标失效的详细规则见 [MCP 使用](mcp-usage.md)。如果模型尚未准备，读取现有文字仍不需要模型；识别能力按[模型配置](models.md)另行准备。

## 重装、换目录与撤销

移动 Audio Agent 项目或重建虚拟环境后，重新运行安装器更新绝对命令路径，再让客户端重连。切换存储位置后，跟随本机设置的配置无需改路径，但已运行的 MCP 进程仍需重连并重新建立游标；固定 `--data-dir` 或环境覆盖要先调整。不要通过重启正在监听的 Web 后台来代替客户端 MCP 重连。

安装器只更新自己的连接项和可确认未被改动的已管理技能，更新已有内容时提供备份。未知同名技能、人工改过的技能、损坏配置或符号链接冲突会报错；保留原件，核实后单独处理。普通异常回退不等于断电时完全事务化；异常退出后根据输出和本地备份检查实际状态。

本版本没有卸载命令。撤销时只移除选定配置中的 `audio-agent` 服务项和本次安装的 Skill 目录，保留其他服务、录音库和模型。删除 Skill 不会关闭正在运行的 MCP 或后台采音；在客户端断开 MCP，不要连带停止用户没有要求停止的监听。

## 核查与文档导航

本页基于仓库的[安装器](../scripts/install-agent-integration)、[协议探针](../scripts/check-mcp-connection)和[技能包](../skills/audio-agent/SKILL.md)，客户端规则核查于 2026-10-24。它不保证任意客户端版本、组织策略或远程执行环境都能加载本机配置；最终以当前客户端工具调用为准。

[产品设计](product-design.md) · [使用指南](user-guide.md) · [技术设计](technical-design.md) · [API 参考](api-reference.md) · [安装](installation.md) · [模型](models.md) · [Agent 一键接入](agent-setup.md) · [MCP 接入](mcp-integration.md) · [MCP 使用](mcp-usage.md) · [存储与隐私](storage-and-privacy.md) · [性能](performance.md) · [开发](development.md) · [故障排查](troubleshooting.md)
