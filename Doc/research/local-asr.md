# 本地 ASR 复用调研

> 历史调研与选型依据（2026-10-24），不作为当前功能清单。后续已实现本地 VAD、自动说话人分析与人物备注；当前行为见 [产品设计](../product-design.md)、[模型配置](../models.md)、[用户指南](../user-guide.md)。

[调研目录](README.md) · [文档目录](../README.md)

核查日期：2026-10-24。ASR 指自动语音识别；用户提到的“AMR 模型”在本任务中按语音识别模型理解，AMR 本身也可指一种音频编码。

## 结论

首版直接复用 Handy 已安装的 Whisper 模型文件，用独立本地推理运行时读它。无需启动 Handy、模拟快捷键、读取历史录音或重复下载 1 GB 模型。豆包输入法保留为人工输入工具；目前未找到能把它的 macOS 内置识别直接作为第三方离线文件转写引擎使用的公开接口。

## 这台 Mac 上核实的资源

| 资源 | 实际发现 | 对本项目的意义 |
| --- | --- | --- |
| Handy | `/Applications/Handy.app`，Info.plist 版本 0.6.0 | 已安装可用的离线语音输入应用 |
| Whisper 模型 | `~/Library/Application Support/com.pais.handy/models/ggml-large-v3-q5_0.bin`，1,081,140,203 字节，GGML 文件头 | 本项目直接读取此文件；不复制、不改动 |
| Handy VAD | 应用包中 `resources/models/silero_vad_v4.onnx` | 是语音活动检测模型，不是语音转文字模型；后续已接入 v4 |
| 豆包输入法 | `/Library/Input Methods/DoubaoIme.app`，Info.plist 版本 1.0.2 | 已安装 macOS 输入法 |
| 豆包应用包 | 包含 ONNX Runtime 和 `asr/` 资源目录，三个无扩展名资源文件约 9 KB、14 KB、6.9 MB | 只能确认存在相关组件，不能据此证明完整离线识别模型、格式、可调用接口或识别范围 |
| 本地解码器 | `/opt/homebrew/bin/ffmpeg` | 将录音转换成 16 kHz、单声道 PCM16；保留原音频 |
| Python 推理运行时 | 项目虚拟环境内 `pywhispercpp==1.5.1` | 通过 whisper.cpp 使用既有 GGML 模型 |

以上仅检查应用包元信息、资源名、模型文件名/大小/文件头，没有读取 Handy 的用户录音、历史转写、账号设置或豆包账号数据。

## Handy：可复用模型，不把输入法界面当服务

Handy 官方仓库将产品描述为本地离线语音转写应用。其版本不断演进，因此本机 0.6.0 的实际模型文件是实现依据，不把上游最新模型目录等同于本机已安装资源。[官方仓库](https://github.com/cjpais/Handy)、[0.6.0 发布说明](https://github.com/cjpais/Handy/releases/tag/v0.6.0)。

Whisper GGML 可由 whisper.cpp 读取，支持量化推理和 Apple Silicon；pywhispercpp 的 `Model` 接受本地 GGML 路径。因此基础方案是“同一个模型文件 + 独立推理进程”，不依赖 Handy GUI 的录音和粘贴流程。[whisper.cpp 官方仓库](https://github.com/ggml-org/whisper.cpp)、[pywhispercpp 官方仓库](https://github.com/absadiki/pywhispercpp)。

`audio_agent.asr` 实现模型发现、显式路径校验、解码归一化、语言选择、分段时间戳和本地推理。每次公开转写调用启动一个独立推理子进程，加载模型后完成请求并退出释放资源；父进程不保留模型。缺模型或缺运行时直接报告错误，不静默请求云端，不接受模型名称触发自动下载。现有适配器仅支持 Whisper GGML，未声称兼容 Handy 所有新模型。

可用环境变量：

| 变量 | 含义 |
| --- | --- |
| `AUDIO_AGENT_MODEL` | 指定已有 GGML `.bin` 文件的完整路径 |
| `HANDY_MODELS_DIR` | 覆盖 Handy 模型查找目录 |
| `AUDIO_AGENT_THREADS` | 正整数，默认最多 4 个 CPU 线程 |
| `AUDIO_AGENT_USE_GPU` | `true`/`false`；默认开启，Metal 不可用时可显式用 CPU |
| `AUDIO_AGENT_ASR_TIMEOUT` | 每次转写子进程的超时秒数，默认 300，上限 1800 |

直接 API：`discover_models()` 返回 JSON 可序列化列表；`transcribe(path, model_path=None, language="auto")` 返回正文、模型路径、后端、耗时、音频时长和分段时间戳。`zh-CN` 归一化为 `zh`，`auto` 做语言自动识别。ASR 适配器本身不做说话人分段与身份识别；后续已增加独立说话人模块，见[模型配置](../models.md)。声道不直接等同于人物身份。

## 豆包输入法：能力与接口分开看

官网确认 macOS 版提供语音输入、方言识别、中英混输和文字整理。但其产品页面未提供第三方离线文件转写 SDK、CLI 或模型格式文档。本次检查到本地资源，并不能推导出可以直接复用的 ASR API。结论是“公开可用接口未核实”，不是“它绝不使用本地模型”。[豆包输入法官网](https://shurufa.doubao.com/pc)。

火山引擎提供正式的豆包语音识别开发者接口，包括录音文件识别；这些接口需要凭证并请求云服务，与调用已安装输入法不同。因此它可作为未来明确选择云端时的备选，但没有接入首版，也没有上传任何音频。[火山引擎录音文件识别标准版文档](https://docs.volcengine.com/docs/DoubaoVoice/LargemodelrecordingfilerecognitionstandardversionAPI?lang=zh)。

## 验证与已知边界

自动化测试覆盖：缺模型提前失败、拒绝不兼容格式、发现结果去重、无 ffmpeg 时标准 WAV 仍可用、48 kHz 立体声真实转换为 16 kHz 单声道、临时文件清理、源文件保持不变、空音频及错误线程配置。通过模拟子进程崩溃、超时、不产出结果和无效 JSON，检查父进程返回明确错误、原生 stdout 不污染 MCP 通道、临时文件始终清理。

沙箱内 macOS `say` 返回成功却生成没有采样的 AIFF；适配器正确拒绝了空音频。随后改用 whisper.cpp 官方 `samples/jfk.wav` 公开测试样例，在沙箱外使用本机已有模型完成实际识别：11.0 秒英语音频完整识别为 3 个分段，首次调用耗时 13.65 秒，其中包含 Metal 首次编译开销；结果记录在 `.data/test-fixtures/jfk-result.json`。这是单条英文样例的功能验收，不是中文准确率、长时间吞吐或多人场景评测。[公开样例来源](https://github.com/ggml-org/whisper.cpp/blob/master/samples/jfk.wav)。

沙箱内 Metal 初始化出现原生崩溃，因此公开转写 API 隔离推理子进程，把崩溃或超时转换为可报告的错误。原生输出写入临时日志，结果通过独立 JSON 文件返回，避免破坏 MCP 标准输出。若日志出现 `ggml_metal_buffer_init` 失败，可在允许访问 Metal 的普通终端执行，或明确切换 CPU：

```sh
AUDIO_AGENT_USE_GPU=false .venv/bin/python scripts/verify_asr.py \
  .data/test-fixtures/jfk.wav --language en \
  --output .data/test-fixtures/jfk-cpu-result.json
```

CPU 模式仍使用同一份本地模型，可能更慢；不因 GPU 不可用自动上传到云端。超过超时会终止该推理子进程，可缩短片段，或在 1–1800 秒范围内调整 `AUDIO_AGENT_ASR_TIMEOUT`。单元测试不能替代真实识别与设备环境验收。

现有公开 API 每次请求会冷启动并加载约 1 GB 模型。处理录音宜按需求提取时间窗口，后续可增加常驻但可重启的推理工作进程以降低重复加载成本。现有大型模型适合优先验证中文准确率；若持续处理的实时系数不足，再比较 Whisper large-v3-turbo 或中文 SenseVoice 等模型，届时需要另行获取对应模型文件。当前没有下载这些替代模型。

## 本机计算成本观察（2026-10-24）

机器为 Apple M1 Pro、16 GB 内存。本次仅统计项目本地转写 JSON 的 `duration_seconds` 与 `elapsed_seconds`，未读取或使用 Handy 历史记录。统计时得到：

| 样本 | 音频时长 | worker 墙钟耗时 | 依据与限制 |
| --- | --- | --- | --- |
| 主库 64 条转写 | 合计 388.984 秒 | 合计 175.020 秒 | `.data/transcripts/*.json`；来自早期固定 5 秒版本，不能当作当前声音触发分段的标准性能 |
| 一条 QA 音频 | 56.861 秒 | 7.513 秒 | `.data/web-qa/transcripts/d66895927a7c49e9a75af84f90882d8c.json`；是单条样例，不是所有 QA 文件的合计 |

这里的 worker 耗时是一次调用从启动独立推理进程到完成的实际经过时间，包含反复加载模型等开销，**不等于 CPU 占用率或 CPU 核秒数**。当前每段都启动新的 worker，短句也承担进程启动和模型加载的固定开销；这两组样本的内容和长度不同，不能据此断言所有长音频都会达到同样速度。

识别在本机完成，没有云端 ASR API 调用费，但会消耗本机计算资源。**本轮未测量耗电和峰值内存。** 现有模型文件为 1,081,140,203 字节，约 1.081 GB（十进制）；文件大小不能当作推理峰值内存。

按需模式主要靠不识别不需要的音频来减少总计算量。若同样的全部音频最终仍需补转写，仅延后运行不能证明总算力更省；合并请求可能减少重复加载开销，但本轮没有对这一点做受控比较。以上是本机这一批日志的观察，不外推到全天耗电、内存峰值或其他 Mac。
