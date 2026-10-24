# 背景人声、电视与对话范围

> 历史调研与选型依据（2026-10-24），不作为当前功能清单。后续已实现本地 VAD、自动说话人分析与人物备注；当前行为见 [产品设计](../product-design.md)、[模型配置](../models.md)、[用户指南](../user-guide.md)。

[调研目录](README.md) · [文档目录](../README.md)

研究日期：2026-10-24。目标是保留「自己与对方的谈话」，同时减少电视和旁人的谈话。仅靠音量门限无法可靠完成这个目标；加入人声检测也不会让系统自动理解谁在参与当前对话。

## 当前实现与边界

当前 `activity.py` 使用归一化 PCM 的 RMS 能量判断声音活动，初始触发门限 0.006、释放门限 0.004（约 −44.4 dBFS），持续约 0.2 秒超过触发门限后确认声音；连续安静约 0.8 秒结束片段。这些数值是数字音频幅度，不是环境分贝值。Web 已支持 −60～−18 dBFS 手动调节、预设起点、显式保存与现场音量参考；调节不会启动麦克风。操作方法见[界面说明](../user-guide.md)，实际验证范围见[验收记录](../verification.md)。

监听时现可显式启用本地 Silero VAD v4，让语音概率与音量一起决定是否保留片段。VAD 只在启用该选项的监听会话中运行。安静时不创建 WAV、不调用 ASR；电视、旁人谈话仍可能通过并被识别。提高音量门限可以减少较轻的背景声，也可能漏掉轻声讲话和较远的对话对象；背景和目标同样响时，调门限不能区分两者。

当前支持手动及显式开启后的自动本地说话人分析：对一个已保存文件估计人数、标注说话区间；试听确认后，可以将其中一人的清晰语音登记为本人参考声纹。它在文件分析后提供标注，不参与实时采集、不删除其他人的语音，也不改变现有 Whisper ASR。模型来源及真实样例结果见 [本地语音模型](../models.md)。

## 三种模型解决不同问题

| 能力 | 能回答的问题 | 对当前需求的限制 |
| --- | --- | --- |
| 人声活动检测（VAD） | 这一段有没有语音？ | 电视里的台词和旁人说话也属于语音，不能凭 VAD 保证排除。 |
| 说话人分段（diarization） | 哪些时间段像是同一个人在说话？ | 给出说话人标签和时间，不自动知道谁是用户、谁在参与本次谈话。 |
| 目标说话人验证（声纹比对） | 这段声音是否像已登记的那个人？ | 只保留本人会同时排除对话对象；也不等同于从重叠声音中分离出本人。 |

Silero 的官方接口输出语音时间段；SpeechBrain ECAPA 用说话人向量的相似度比较两段音频是否来自同一人；pyannote Community-1 输出说话人分段。上表关于电视和谈话范围的限制，是依据这些任务定义作出的工程判断。[Silero 官方项目](https://github.com/snakers4/silero-vad)、[SpeechBrain 官方模型卡](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb)、[pyannote 官方模型卡](https://huggingface.co/pyannote/speaker-diarization-community-1)

「这个人是不是正在和我交谈」还涉及会话范围、轮流发言和现场关系。说话人标签无法单独提供这些信息；转写后的语义判断可以辅助整理，但仍可能猜错，也不能撤销此前已经采集的声音。因此不宜默认根据模型猜测自动删除声音。

## 先改善拾音位置

Shure 官方说明，把麦克风移近说话者能改善语音与背景声的比例；指向性表示对不同方向声音的灵敏度。两者都按声学位置起作用，不识别人或谈话关系。[Shure：语音与背景声比例](https://service.shure.com/articles/en_US/Knowledge/predicting-speech-to-background-noise-level-at-the-microphone)、[Shure：麦克风指向性](https://www.shure.com/en-US/insights/microphone-directionality-polar-pattern-basics)

对当前场景的工程建议是：个人口述时靠近本人收音；双人交谈时，把收音位置兼顾双方，并尽量远离电视等干扰源。只把领夹麦贴近本人，会让对方相对更弱；桌面麦放在双方之间可以兼顾双方，但仍可能收到旁人。DJI Mic 3 官方规格为全指向，佩戴靠近嘴部的优势主要来自距离，不能当作只接收正前方声音的麦克风。[DJI 官方规格](https://www.dji.com/mic-3/specs)

本项目当前由 `capture.py` 将输入转换为 16 kHz 单声道（ffmpeg `-ac 1`），没有保留独立阵列通道。多通道波束形成需要各通道的信息；已经混合的单声道不能直接用来计算通道间的空间差异。这不代表 Mac 的物理麦克风只有一个，也不代表换一个模型就能得到原始阵列数据。[TorchAudio 官方 MVDR 示例](https://github.com/pytorch/audio/blob/main/examples/tutorials/mvdr_tutorial.py)

## 当前接入与后续评估

1. **人声检测复用 Handy 的 Silero VAD。** 原模型路径为 `/Applications/Handy.app/Contents/Resources/resources/models/silero_vad_v4.onnx`，按只读方式通过 ONNX Runtime CPU 运行。当前接入为 v4 的独立状态接口，没有直接套用新版模型接口。它有助于排除部分非语音，不保证排除电视人声。[Silero 官方说明](https://github.com/snakers4/silero-vad)
2. **说话人分析使用 sherpa-onnx。** 本次开发取得 pyannote segmentation 3.0 与 3D-Speaker ERes2Net 的公开 ONNX 权重，在 CPU 子进程中离线运行；应用没有自动下载模型的行为。文件级匿名分段与本人参考声纹使用同一个 embedding 模型，不额外安装 SpeechBrain 或 pyannote.audio 运行时。[sherpa-onnx 官方组合](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)
3. **本人标注保留不确定性。** 登记需要用户试听确认，不设最低秒数，但须提取到有效声纹；短片段匹配可能不稳定。后续匹配至少需要 2 秒。分数是声纹向量的余弦相似度，不是「是本人的概率」。目前保守策略可能漏认；没有针对本人、当前麦克风或电视混入校准。未匹配者仍保留原来的匿名标签。整段对话不会因为不是本人而被丢弃。

本次公开中文四人样例在聚类阈值 0.5 下被估为 7 人；按上游对该样例的建议改为 0.9 后估为 4 人，JFK 单人样例估为 1 人。因此当前采用 0.9 作为起点，并保留手动指定人数。两个公开样例通过不代表真实现场人数始终正确；指定人数得到相同人数也不能作为自动数人的验证。

后续验收应分别包含安静、键盘声、电视独播、旁人独自交谈、自己轻声、双方轮流说话、双方与电视重叠等样本，统计漏掉的目标语音、误收的背景时长、转写次数与本机资源开销；不能用「静音不生成文件」代替背景人声过滤的验证。
