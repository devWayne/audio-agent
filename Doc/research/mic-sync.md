# DJI Mic 3 与 micSync 接入调研

> 历史调研与选型依据（2026-10-24），不作为当前功能清单。后续已实现本地 VAD、自动说话人分析与人物备注；当前行为见 [产品设计](../product-design.md)、[模型配置](../models.md)、[用户指南](../user-guide.md)。

[调研目录](README.md) · [文档目录](../README.md)

核查日期：2026-10-24（Asia/Shanghai）。本次核查了官方资料和上游源代码；没有连接 Mic 3、扫描无线网络、变更配对、运行上游程序或进行实机验证。

## 结论

基础 Audio Agent 应先接入 **Mac 本地音频文件和音频输入设备**。`GP2P/micSync` 可以承担回到充电盒后的 USB 文件导入，它没有实现 Mic 3 内录文件的 Wi-Fi 下载。无线文件导出确实存在于 DJI Mimo，但不能由此推出 Mac 上已有可调用的下载 API，也不能承诺边录音边下载。Cloudflare 只能解决文件已离开麦克风之后的中转。

## 已确认的产品能力

| 能力 | 证据与边界 |
| --- | --- |
| TX 内录为 WAV，24-bit / 32-bit float 可选，约每 30 分钟保存一个文件 | DJI 官方 FAQ；长录音必须按文件分段处理，不能只等待一个最终大文件。[来源](https://www.dji.com/mic-3/faq) |
| 内录可经磁吸数据线，或 TX 放入充电盒后连接电脑导出 | DJI 官方 FAQ。可作为自动归档的首选硬件路径。[来源](https://www.dji.com/mic-3/faq) |
| TX 可以连接 Mimo 使用 File Download 导出 | DJI 官方 FAQ。确认的是手机 App 功能，不是公开 Mac SDK。[来源](https://www.dji.com/mic-3/faq) |
| Mimo 通过 Wi-Fi 连接；连接 Mimo 时不能录音 | DJI 官方帮助明确写明；RX 经 Wi-Fi 连 App 时，其 TX 也会与 RX 断开。现阶段应设计为录制与导出分开的流程。[来源](https://repair.dji.com/help/content?customId=01700043587&lang=en&paperDocType=ARTICLE&re=US&spaceId=17) |
| RX 可以通过线缆或适配器连接电脑，作为外部音频输入 | 官方手册第 21 页。Mac 附近实时采集优先验证这条路径：TX 无线到 RX，RX 通过 USB 到 Mac。[来源](https://dl.djicdn.com/downloads/DJI%20Mic%203/202604/DJI_MIC3_um_en.pdf) |
| TX 与 RX 的参数都列有 Wi-Fi 和独立的 SDR 模式 | 不能将 TX→RX 的音频链路当成普通局域网文件共享。[来源](https://www.dji.com/mic-3/specs) |

这些是文档事实，不代表本项目已用用户的硬件完成验收。蓝牙直连 Mac 的音质、声道和稳定性也未实测，不作为基础实现依赖。

## micSync 当前代码实际实现

本次固定审阅提交 `a29d917f6bb0e44d01984beacd242cfdef170d15`；GitHub API 返回该提交时间为 `2026-05-03T23:31:15Z`。下载快照留在本机研究工作区，不随本公开仓库分发，仅供审阅，未作为 Audio Agent 的运行依赖，也未执行其安装或脚本。[提交](https://github.com/GP2P/micSync/commit/a29d917f6bb0e44d01984beacd242cfdef170d15)

- `scanner.py:53–129`：默认遍历 `/Volumes`，也接受显式目录；根目录只向 `TX_MIC*` 和回收站目录递归，文件还需通过 DJI 文件名解析。因此它并非任意 WAV 目录的通用监听器。[代码](https://github.com/GP2P/micSync/blob/a29d917f6bb0e44d01984beacd242cfdef170d15/src/micsync/scanner.py#L53)
- `parser.py:8–11`：识别 `TXNN_MICNNN_YYYYMMDD_HHMMSS[_orig|_edit].wav`。文件里的 TX 序号和实物身份不可混用：官方说明重新配对后 TX 序号可能变化。[代码](https://github.com/GP2P/micSync/blob/a29d917f6bb0e44d01984beacd242cfdef170d15/src/micsync/parser.py#L8)
- `importer.py:129–297`：先写本地临时文件，再移入 `raw/`，记录 SHA-256、大小和来源信息到 SQLite。重复文件有元数据快速路径，不是每轮都完整复核源文件和目标文件哈希；不应描述成每次导入都做独立读回校验。[代码](https://github.com/GP2P/micSync/blob/a29d917f6bb0e44d01984beacd242cfdef170d15/src/micsync/importer.py#L129)
- `config.py` 与 `cli.py`：支持可选整理副本、通知、导入后弹盘；默认数据根目录是 `~/Downloads/micSync/recordings/`。本项目最适合消费其中的 `raw/`。[配置](https://github.com/GP2P/micSync/blob/a29d917f6bb0e44d01984beacd242cfdef170d15/src/micsync/config.py#L51) · [CLI](https://github.com/GP2P/micSync/blob/a29d917f6bb0e44d01984beacd242cfdef170d15/src/micsync/cli.py#L61)
- 审阅全部 `src/`、`scripts/` 与依赖声明，没有发现设备发现、无线配对、Mimo 下载协议、HTTP 下载、Cloudflare/R2 上传或语音识别实现。`lock.py` 的 `socket` 只用于读取主机名。项目声明 Python 3.11+，开发状态为 Alpha。[依赖声明](https://github.com/GP2P/micSync/blob/a29d917f6bb0e44d01984beacd242cfdef170d15/pyproject.toml)

上游 README 的主要流程是：充电盒经 USB 常接 Mac，放回 TX 触发挂盘，由 Shortcuts 启动导入，再弹盘。是否可用对应的自动化触发器，仍应按用户当前 macOS 实机确认。[上游说明](https://github.com/GP2P/micSync/blob/a29d917f6bb0e44d01984beacd242cfdef170d15/README.md)

## Audio Agent 的接入约定

建议文件路径：

```text
TX 内录 → USB / Mimo 手工导出 → 本地落盘
                                   ↓
                         文件导入 / 目录监听
                                   ↓
                         本地 ASR → 文本与索引

TX 无线 → RX → USB 音频输入 → 本地分段录音 → 同一处理流程
```

1. 目录监听以显式配置路径为边界，例如 micSync 的 `recordings/raw/` 或用户导出目录。不要让 Audio Agent 无差别遍历所有挂载盘。
2. 跳过临时文件，确认文件大小与修改时间稳定，复制时再次检查来源没有变化，再按音频内容哈希去重。不要直接改写 micSync 的 SQLite 或原始录音。
3. `orig` 和 `edit` 可能是同一段录音的两个版本；内容哈希不同。初期应只监听选定版本，或将版本字段入库并在转写前选一个，避免同一对话出现两份转写。不要误将所有 `edit` 文件视为内容重复。
4. 保存 DJI 文件名解析结果与来源路径；本地录音时间要附时区。`TX01` 只是配对槽位，不能作为稳定的人物身份。多人区分应优先保存独立声道，声纹或说话人分离属于后续功能。
5. 将 48 kHz、24-bit / float WAV 的转换留给支持该格式的解码器，ASR 输入可转换为 16 kHz 单声道；始终保存原始音频。30 分钟文件在识别端仍宜分块，不能假定短句模型可直接吞入整段。

上面是设计建议，具体已经实现的命令与行为以项目 README 为准。

## 无线和云中转的真正缺口

尚未验证：Mic 3 是否可通过官方公开协议让 Mac 直接枚举和下载文件，是否可加入普通路由器网络，以及 Mimo 导出能否被稳定地自动化。本次资料不足以断言这些能力不存在，但不足以把它们纳入已实现范围。

Cloudflare/R2 的推导路径是 `Mic 3 → 手机/桥接设备取得 WAV → 上传 → Mac 下载`。目前卡点在第一段；云存储不会让 Mic 3 自动具备文件上传能力。基础实现先保持全部音频和识别在本机，未来可把任何同步软件的本地下载目录作为相同的导入接口，无需绑定云厂商。

## 下一轮实机验证

按顺序做小样本验证，所有项目只读保留原录音：

1. 用 RX USB 接 Mac，列出音频输入设备，录制一段明确的 20 秒测试语音；验证声道、断连提示和本地转写。
2. TX 单独内录同一段语音，停止录音后放回已接 Mac 的充电盒；确认挂载目录、WAV 位深、时长和文件名。验证重复导入不会再次识别。
3. 用 Mimo 手工连接 TX，检查导出时录音状态；导出一个短 WAV，传到 Mac 导入目录，和 USB 样本比较哈希、长度与转写。记录手机系统、Mimo 版本和 TX/RX 固件版本。
4. 最后再做 35 分钟样本，验证 30 分钟边界、最后一段落盘、重复 `orig/edit` 的选择、可恢复失败和磁盘占用。不要用全天录音作为首次试验。

在上述实验通过前，项目只应声称支持本地文件/设备接入，不应声称已实现 Mic 3 无线内录自动同步。
