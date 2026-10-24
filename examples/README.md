# 可公开的配置模板

本目录只包含通用占位，不包含开发者的本机配置、密钥、录音或模型。请替换 `/absolute/path/to/audio-agent` 为目标机器上的项目绝对路径。

- [`claude-mcp.json`](claude-mcp.json)：合并到 Claude Code 项目的 `.mcp.json`，不要覆盖其他服务。
- [`codex-mcp.toml`](codex-mcp.toml)：合并到目标 Codex 配置；项目级文件为 `.codex/config.toml`，须按客户端要求信任并加载。
- [`.env.example`](.env.example)：可选环境变量说明。程序不自动加载此文件；按需在实际启动后台的 shell 中 `export`。

更稳妥的方式是在目标机器运行 `./scripts/audio-agent mcp-config --client claude` 或 `--client codex`，或在控制台“MCP 接入”页复制生成的配置。生成/复制不代表客户端已连接。

希望连同配套 Skill 一起自动配置时，直接复制 [Agent 一键接入](../Doc/agent-setup.md) 的完整指令，或使用控制台顶部“复制完整安装指令”。本目录的 JSON/TOML 仍是手动配置模板。

详见 [接入文档](../Doc/mcp-integration.md)、[模型配置](../Doc/models.md)。
