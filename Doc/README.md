# Audio Agent 文档目录

本目录是对外文档入口，文件名和链接统一使用大小写敏感的 `Doc/`。基线为 2026-10-24 源码；文档描述当前行为，实验与未来计划单独标注。项目首页见 [README](../README.md)。

## 按阅读目的选择

| 你要完成的事情 | 建议阅读顺序 |
| --- | --- |
| 第一次安装使用 | [安装](installation.md) → [模型](models.md) → [使用指南](user-guide.md) |
| 理解产品与交互 | [产品设计](product-design.md) → [使用指南](user-guide.md) |
| 接入 Claude Code / Codex | [Agent 一键接入](agent-setup.md) → [MCP 接入](mcp-integration.md) → [MCP 使用](mcp-usage.md) |
| 开发或修改功能 | [技术方案](technical-design.md) → [API 参考](api-reference.md) → [开发指南](development.md) |
| 管理文件、保留期和人物资料 | [存储与隐私](storage-and-privacy.md) |
| 了解常驻影响或排查问题 | [性能](performance.md) → [故障排查](troubleshooting.md) |
| 发布到 GitHub | [发布指南](publishing.md) → [验收记录](verification.md) |
| 追溯硬件与模型选择 | [调研索引](research/README.md) |

## 文档维护规则

1. 先核对源码，再更新“已实现”；未落地的设计放入路线图，不能与正式能力混写。
2. 改动参数、路径、接口或保留策略时，同步更新使用、技术和 MCP 文档中的相关段落。
3. 性能数据写明硬件、模型、负载、测量口径和时间，不把单条样例外推为准确率或全天功耗。
4. 命令使用相对路径或明确的通用占位，不提交开发者主目录、私人录音、真实人物向量或客户端凭证。
5. 研究文件保留核查日期和原始来源；当前产品行为以本目录主文档及源码为准。
6. 发布前检查大小写敏感的相对链接、代码示例及 `.gitignore`。运行状态与源码构建状态分别记录。
