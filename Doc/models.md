# 本地模型与运行参数

[文档目录](README.md) · [安装](installation.md) · [性能](performance.md)

基线：2026-10-24 源码。三个环节使用不同模型，不需要一起开启；安装 Python 的 `asr` / `speech` extras 只安装运行时，不包含模型权重。程序不会下载模型或在失败时切换云端。

## 1. 模型职责与加载时机

| 环节 | 当前运行时 / 模型 | 何时加载 | 不能替代的能力 |
| --- | --- | --- | --- |
| ASR 语音转文字 | whisper.cpp / pywhispercpp；Whisper GGML `.bin` | 明确请求转写，或已开始的自动转写会话产生有效片段 | 不负责识别人物身份 |
| VAD 人声检测 | ONNX Runtime CPU；Silero **v4** | 用户启用 VAD 并开始监听时；采音前先校验模型 | 不判断电视声/现场声音或对话关系 |
| 说话人分段 | sherpa-onnx CPU；pyannote segmentation 3.0 ONNX | 手动分析，或明确开启自动分析后的新监听片段 | 不保证人数估计准确，不是音源分离 |
| 声纹向量 | sherpa-onnx CPU；3D-Speaker ERes2Net-Base 中文 16k ONNX | 同一次说话人分析中提取参考，再与本地登记资料比较 | 相似度不是身份概率或认证凭据 |

ASR 与说话人分析使用同一资料库推理锁，串行处理。状态检查只检查本地文件与依赖，不代表模型已成功推理。

## 2. Whisper 模型准备

兼容范围以 `audio_agent/asr.py` 为准：自动发现扫描 `*.bin`，显式路径主要校验文件存在及 Whisper GGML 文件头，不单独限制扩展名。ONNX、GGUF、SenseVoice 或其他 Handy 模型当前不能交给这个适配器。只填 `large-v3` 等名称也不会下载模型。

发现与选择顺序：

1. 单次 CLI 转写的 `--model`（该选项用于 CLI 转写）。
2. 服务环境变量 `AUDIO_AGENT_MODEL`。
3. `HANDY_MODELS_DIR` 指定的目录，或 macOS 默认 `~/Library/Application Support/com.pais.handy/models/`。
4. 自动发现中优先 `ggml-large-v3-q5_0.bin`，否则使用找到的兼容文件。

已有 Handy 模型时无需复制。自行获取模型时，应使用 [whisper.cpp 官方模型说明](https://github.com/ggml-org/whisper.cpp/tree/master/models) 中适用的 Whisper GGML 制品，并核对来源、格式与许可。项目不代替用户获取权重。

```sh
export AUDIO_AGENT_MODEL='/absolute/path/to/ggml-model.bin'
./scripts/audio-agent doctor
./scripts/audio-agent web --port 8767
```

当前参考配置使用多语言 `large-v3-q5_0`（约 1.081 GB 文件）。这不是硬编码的必选型号，也不是推理峰值内存。更小模型的质量、速度需自行测试；没有把尚未实现的后端列作可用选项。

## 3. Silero v4 VAD

默认模型路径是：

```text
/Applications/Handy.app/Contents/Resources/resources/models/silero_vad_v4.onnx
```

运行依赖来自 `speech` extra 中的 ONNX Runtime。适配器要求 v4 的 `input/h/c/sr` 循环状态接口，输入为 16 kHz、512 个采样点一帧，CPU 单线程。Handy 后续版本可能更换资源路径或模型格式，不能假定最新安装一定兼容。

**当前 Web/菜单栏没有任意 VAD 路径配置项，也没有 `AUDIO_AGENT_VAD_MODEL` 变量。** 底层 `SileroVad(model_path=...)` 支持开发者显式传入路径，但不是现有 UI/CLI 的可用设置。缺少默认文件时先关闭 VAD，使用能量门槛；或在后续开发中接入经过验证的 v4 路径配置。不要覆盖 Handy 资源或把其他版本模型改名来绕过格式检查。

VAD 概率门槛当前为开始 0.5、释放 0.35，仍与用户可调声音能量门槛配合。Web/Mac 的收音滑块改变响度门槛，不会改变上述概率门槛。[Silero 官方项目](https://github.com/snakers4/silero-vad)

## 4. 说话人模型准备

在项目根目录准备以下文件（不提交 Git）：

```text
.data/models/speakers/
├── segmentation.onnx
└── embedding.onnx
```

已经验证的上游制品：

| 目标文件 | 已验证制品与手动获取入口 | 本地验证尺寸 |
| --- | --- | ---: |
| `segmentation.onnx` | sherpa-onnx 的 [pyannote segmentation 3.0 ONNX 包](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2)，选包内 FP32 权重，复制为此文件名 | 5,992,913 字节 |
| `embedding.onnx` | [ERes2Net-Base 中文 16k ONNX](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx)，复制为此文件名 | 39,593,761 字节 |

这些链接用于使用者主动获取；本项目打开界面、刷新状态或安装依赖均不下载它们。使用其他目录可设置 `AUDIO_AGENT_SPEAKER_MODELS_DIR`，或分别指定两个文件。切换录音资料库不会搬动模型；默认模型目录仍相对于源码项目 `.data/models/speakers/`。

上游许可分别参考 [pyannote 模型卡](https://huggingface.co/pyannote/segmentation-3.0)、[3D-Speaker 仓库](https://github.com/modelscope/3D-Speaker) 与下载包所附许可。历史验证制品记录为 pyannote MIT、3D-Speaker Apache-2.0；重新获取或重新发布时应核对实际制品，不能用项目源码许可替代模型许可。模型不打包进本仓库。

历史验证文件 SHA-256 如下，仅标识本地验收过的制品，不是项目提供的上游数字签名：

```text
Silero v4:   a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28
segmentation: 220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079
embedding:  1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b
```

## 5. 支持的环境变量

必须设置在实际后台进程或 MCP 服务的启动环境中。程序不自动读取 `.env`。

| 变量 | 默认 / 取值 | 作用 |
| --- | --- | --- |
| `AUDIO_AGENT_MODEL` | 无；显式已有文件路径 | 指定 Whisper GGML |
| `HANDY_MODELS_DIR` | 平台 Handy 模型目录 | 覆盖 ASR 自动发现目录 |
| `AUDIO_AGENT_THREADS` | `min(4, CPU 核数)`；正整数 | Whisper CPU 线程数 |
| `AUDIO_AGENT_USE_GPU` | `true`；支持 `true/false/1/0/yes/no` | 控制 Whisper GPU；Apple Silicon 使用 Metal，不使用 Neural Engine |
| `AUDIO_AGENT_ASR_TIMEOUT` | 300；1–1800 秒有限数值 | 单次 ASR worker 超时 |
| `AUDIO_AGENT_SPEAKER_MODELS_DIR` | 项目 `.data/models/speakers` | 两个说话人模型的默认文件夹 |
| `AUDIO_AGENT_SPEAKER_SEGMENTATION_MODEL` | 上述文件夹 `segmentation.onnx` | 覆盖分段模型路径 |
| `AUDIO_AGENT_SPEAKER_EMBEDDING_MODEL` | 上述文件夹 `embedding.onnx` | 覆盖向量模型路径 |
| `AUDIO_AGENT_SPEAKER_THREADS` | 2；整数 1–16 | 说话人 CPU 线程数 |
| `AUDIO_AGENT_SPEAKER_TIMEOUT` | 300；1–600 秒有限数值 | 说话人 worker 超时 |
| `AUDIO_AGENT_SPEAKER_CLUSTER_THRESHOLD` | 0.9；严格大于 0、小于 1 | 自动人数聚类门槛，不是收音门槛或身份相似度阈值 |
| `AUDIO_AGENT_FFMPEG` | PATH / `/opt/homebrew/bin/ffmpeg` | 存储/采集的 ffmpeg 路径；底层 ASR 非标准音频归一化仍查 PATH，建议同时配置 PATH |
| `AUDIO_AGENT_DATA_DIR` | 未设时读保存设置或项目 `.data` | 资料库覆盖；使用它时 UI 目录切换会提示先移除覆盖 |

## 6. 登记参考与匹配边界

输入分析的 WAV 必须为 16 kHz 单声道 PCM16，时长大于 0 且不超过 600 秒。UI 使用已归一化的库内录音。

每次分析对每位说话人最多使用 30 秒未被其他已检测说话人重叠的语音提取向量。登记不设固定 5 秒或 2 秒门槛，以提取器能否返回有效向量为准；1–2 秒可能成功，也可能没有可用向量。每条参考推荐清晰单人语音 10–20 秒。后续自动匹配对候选语音另有至少 2 秒要求，不应与人工登记门槛混淆。

本人可登记最多 10 条参考，其他人物仍是每人一条。可先选择 3–5 段不同常见说话方式的清晰本人语音，逐条试听确认；这不是硬性数量要求，也不保证增加样本就更准确。网页默认追加，显式替换会用当前一条替换整组，单条移除只撤销该样本。新增参考保存已有分析提取的向量，不重新训练模型，也不自动收集近期语音。不同模型生成的向量不能混用；状态列表的 `model_compatible: null` 表示该只读请求没有核验当前安装模型，不能理解为已兼容或已不兼容。

默认匹配要求相同 embedding 模型、余弦相似度至少 0.65 和候选差距至少 0.1。短音频、噪声、重叠发言会导致漏认或误认；未匹配时保留匿名。模型 ID 由权重哈希标识，模型变化后旧参考不能直接混用。本人和其他人物都必须经过用户试听与确认；打开页面、自动分析都不会自动登记身份。

本人多参考只取与当前分析模型 ID 相同的样本，分别计算余弦相似度，再按 `min(该参考有效秒数, 10)` 加权平均。不会取多条里的最高分，也不会将平均向量重新归一化来抬高分数。单条较长参考在 10 秒后不再增加权重；很短的旧参考仍保留，但权重较低。加入不一致、噪声大或录错人的参考可能拉低匹配分数，因此应试听并移除不合适的样本，不能保证“每加一条就更准”。候选语音至少 2 秒、0.65 门槛及人物/说话人两侧 0.1 差距仍保留；多参考不会绕过这些判断。

## 7. 已验证结果与未验证项

- M1 Pro、CPU 2 线程，公开中文四人 56.861 秒样例：自动阈值 0.5 估为 7 人，0.9 估为 4 人，后者约 10.138 秒；单人 JFK 样例约 0.952 秒。样例正确不等于现场准确率。
- 公开语音互不重叠前后半段的同人相似度约 0.601–0.798、异人约 −0.043–0.227；默认 0.65 会漏掉部分同人结果，没有据此宣称可做身份认证。
- Whisper 的真实样例、VAD 微基准和后台资源观察见 [性能报告](performance.md)、[验收记录](verification.md)。
- 没有测定不同房间的电视过滤率、用户本人误认率、长期电耗或 M4 Max 加速倍数。

原理和实现参考：[whisper.cpp](https://github.com/ggml-org/whisper.cpp)、[pywhispercpp](https://github.com/absadiki/pywhispercpp)、[Handy](https://github.com/cjpais/Handy)、[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)。这些项目提供不同组件，Audio Agent 并未宣称支持它们的全部模型和功能。
