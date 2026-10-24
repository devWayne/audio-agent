# GitHub 发布指南

[文档目录](README.md) · [开发](development.md) · [验收](verification.md)

这份指南用于发布源码仓库，发布目标为 [devWayne/audio-agent](https://github.com/devWayne/audio-agent)。仓库可见性由所有者在 GitHub 管理；当前未指定源码许可证。以下命令用于首次发布和后续复核，实际提交与分支状态以 Git 为准。

## 1. 对外目录

```text
audio-agent/
├── README.md             # 项目首页、安装入口与文档导航
├── Doc/                  # 产品、技术、使用、MCP、运维和研究文档
├── examples/             # 通用占位配置，无个人路径
├── audio_agent/          # Python服务、采集、存储、ASR、MCP、Web资源
├── macos/                # Swift 菜单栏源码
├── scripts/              # 启动、构建与显式验证脚本
├── skills/audio-agent/   # 随源码发布的通用 Skill
├── tests/                # 隔离回归测试
├── pyproject.toml
├── uv.lock
├── AGENTS.md
└── .gitignore
```

`.venv/`、`.data/`、`dist/`、缓存、模型、录音、转写、人物参考和本机客户端配置不属于发布文件。当前 `.mcp.json` 与 `.codex/config.toml` 保留在开发机，已通过 `.gitignore` 排除；公开模板放在 `examples/`。

## 2. 发布者需要决定的事项

| 事项 | 处理方式 |
| --- | --- |
| GitHub 账号/组织与仓库名 | 当前目标为 `devWayne/audio-agent`；发布到自己的 fork 时更换远程地址 |
| Public 或 Private | 由仓库所有者选择；不要因“本地识别”就忽略仓库里可能存在私人测试资料 |
| 源码许可证 | 当前未添加 `LICENSE`，不擅自替所有者选择 MIT/Apache 等授权；选定后更新 README 和包元数据 |
| 发行定位 | 当前是 Alpha 源码项目，不能描述为已公证、跨平台或自包含的 `.app` 安装包 |
| 版本 | Python `0.1.0` 与菜单栏 `0.1.4 (5)` 独立；发布标签、变更记录和实际制品须注明对应关系 |

第三方依赖与模型的许可和通知应另行核查。模型权重不随本仓库发布；即使源码选择一种许可，也不改变上游模型的授权条件。[模型来源](models.md)

## 3. 本地发布检查

在项目根目录检查候选文件：

```sh
git status --short --untracked-files=all
git ls-files --cached --others --exclude-standard
git check-ignore -v .data/example.wav .mcp.json .codex/config.toml dist/example.app
```

逐项核对：

- 候选文件中没有 WAV/音频、SQLite、ONNX/GGML 等权重、voiceprint、人物资料和真实转写。
- Markdown、示例和截图没有开发者个人路径、账号、录音文字、访问令牌或私有 URL。
- 大文件不是模型、临时归档、虚拟环境或历史测试产物。
- 配置模板采用占位或在目标机器生成，不用开发机绝对路径。
- 既有 Git 历史或暂存区中的敏感文件不能仅靠新 `.gitignore` 排除；必须另行检查。

本轮将旧文档原件保存在忽略的 `.data/documentation-original-20261024/`，供开发机追溯；它不属于公开文档。公开调研经过状态标注，放在 `Doc/research/`。

完成代码/文档检查：

```sh
.venv/bin/python -m pytest -q
node --check audio_agent/web_assets/app.js
node --test tests/document.test.cjs tests/agent-setup.test.cjs
```

编译菜单栏会覆盖本机 `dist/Audio Agent.app` 并改变签名，不适合为检查文档而无意义地反复运行。需要发布构建时按 [开发指南](development.md) 验证，记录平台和构建版本。硬件/真模型脚本必须明确选择隔离目录，不能为了发布检查自动录下用户环境。

## 4. 创建首次提交

检查完成后，明确暂存准备公开的范围。许可证由所有者另行选定；未选择时保留未授权状态，不代选 MIT/Apache 等许可。以下是首次提交示例：

```sh
git add README.md Doc examples skills audio_agent macos scripts tests pyproject.toml uv.lock AGENTS.md .gitignore
# 如果已添加许可证，另外 git add LICENSE
git diff --cached --stat
git diff --cached --check
git diff --cached

git commit -m "Initial Audio Agent source and documentation"
```

不推荐在尚未检查目录时直接执行 `git add -A`。提交命令失败时先确认 Git 用户名/邮箱配置，不把访问凭证写入仓库。

## 5. 关联并推送 GitHub

首次发布时，先在目标账号或组织中建立空仓库；已有本地 README 时，创建页面无需额外初始化另一份 README，以免产生独立历史。

当前项目的远程地址如下，URL 中不包含 token：

```sh
git remote add origin https://github.com/devWayne/audio-agent.git
git remote -v
# 首次提交后确认分支名；仅在该首次发布仓库将当前分支命名为 main
git branch --show-current
git branch -M main
git push -u origin main
```

如果已经有 `origin`，先核对地址，不要盲目重复添加或覆盖。使用正常 GitHub 登录/凭证管理流程，不把 token 放进命令、文档或 `.env.example`。不要用 force push 来掩盖首次远程历史冲突。

仓库创建和连接方式参见 [GitHub 官方导入本地代码指南](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)。执行推送后，核对远程分支与本地提交一致，不以文档中的示例命令作为已发布的证明。

## 6. 推送后的复核

1. GitHub 首页 README 的全部 `Doc/` 链接可打开，大小写准确。
2. 在干净目录克隆，按安装指南建立新 `.venv`；没有模型时应明确提示缺失，不自动下载。
3. Web 打开不开始录音，MCP 列工具不开始录音；不要依靠开发机已有状态证明新安装可用。
4. 无个人 `.data`、本机 MCP 配置、数据库和模型出现在仓库或 Release 附件。
5. 若提供 Release，说明目标平台、安装依赖、签名/公证状态、模型来源、已知限制与对应源码版本。

当前仓库没有持续集成工作流、自动发布流水线或通用二进制安装包。不能把本机一次测试通过写成 GitHub CI 徽章或已完成跨平台验收。
