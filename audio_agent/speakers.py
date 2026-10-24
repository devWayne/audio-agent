"""Explicit, local-only speaker diarization in a disposable CPU worker.

Status checks never import the native runtime, load weights, or download files.
Speaker numbers are local to one audio file, not stable person identities.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import wave

BACKEND = "sherpa-onnx/cpu"
SAMPLE_RATE = 16000
MAX_AUDIO_SECONDS = 600
MAX_EMBEDDING_SECONDS = 30
# Upstream recommends 0.9 for its public four-speaker Chinese example; our
# public four-speaker and JFK smoke cases also match their known counts here.
# This is a starting point, not calibration for the user's room or microphone.
DEFAULT_CLUSTER_THRESHOLD = 0.9


class SpeakerError(RuntimeError):
    """A local model, input, or inference failure."""


def _model_paths():
    directory = Path(os.environ.get("AUDIO_AGENT_SPEAKER_MODELS_DIR",
                                    Path(__file__).resolve().parents[1] / ".data/models/speakers")).expanduser()
    return {name: Path(os.environ.get(f"AUDIO_AGENT_SPEAKER_{name.upper()}_MODEL",
                                     directory / f"{name}.onnx")).expanduser().resolve()
            for name in ("segmentation", "embedding")}


def speaker_model_status() -> dict:
    models = {}
    for name, path in _model_paths().items():
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        models[name] = {"path": str(path), "present": size > 0, "size_bytes": size}
    try:
        runtime = all(importlib.util.find_spec(name) is not None for name in ("sherpa_onnx", "numpy"))
    except (ImportError, ValueError):
        runtime = False
    present = all(model["present"] for model in models.values())
    reason = ("缺少本地说话人模型；不会自动下载。" if not present else
              "缺少本地 sherpa-onnx 运行依赖。" if not runtime else
              "本地文件和依赖已找到；模型有效性在显式分析时检查。")
    return {"ready": present and runtime, "backend": BACKEND,
            "runtime_available": runtime, "models": models, "reason": reason}


def _validate_num_speakers(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10):
        raise SpeakerError("说话人数必须为空（自动估计）或 1–10 的整数。")


def _audio_info(path: Path):
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (16000, 1, 2, "NONE"):
                raise SpeakerError("说话人分析需要 16 kHz、单声道、PCM16 WAV。")
            frames = audio.getnframes()
            if not 0 < frames <= MAX_AUDIO_SECONDS * SAMPLE_RATE:
                raise SpeakerError("说话人分析音频时长必须大于 0 且不超过 600 秒。")
            return frames, frames / SAMPLE_RATE
    except (OSError, wave.Error, EOFError) as exc:
        raise SpeakerError("无法读取本地 PCM WAV 文件。") from exc


def _settings():
    try:
        timeout = float(os.environ.get("AUDIO_AGENT_SPEAKER_TIMEOUT", "300"))
        threads = int(os.environ.get("AUDIO_AGENT_SPEAKER_THREADS", "2"))
    except ValueError as exc:
        raise SpeakerError("说话人分析 timeout/threads 配置必须是数字。") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 600 or not 1 <= threads <= 16:
        raise SpeakerError("说话人分析超时须为 1–600 秒，CPU 线程数须为 1–16。")
    return timeout, threads


def _cluster_threshold():
    try:
        value = float(os.environ.get("AUDIO_AGENT_SPEAKER_CLUSTER_THRESHOLD", str(DEFAULT_CLUSTER_THRESHOLD)))
    except ValueError as exc:
        raise SpeakerError("说话人聚类阈值必须是 0–1 之间的有限数字。") from exc
    if not math.isfinite(value) or not 0 < value < 1:
        raise SpeakerError("说话人聚类阈值必须是 0–1 之间的有限数字。")
    return value


def analyze_audio(path, num_speakers=None) -> dict:
    """Analyze a managed/snapshotted WAV; no recording or ASR is performed."""
    _validate_num_speakers(num_speakers)
    audio = Path(path).expanduser().resolve()
    _audio_info(audio)
    timeout, threads = _settings()
    cluster_threshold = _cluster_threshold()
    status = speaker_model_status()
    if not status["ready"]:
        raise SpeakerError(status["reason"])
    with tempfile.TemporaryDirectory(prefix="audio-agent-speakers-") as temporary:
        output = Path(temporary) / "result.json"
        command = [sys.executable, "-m", "audio_agent.speaker_worker", "--audio", str(audio),
                   "--segmentation", status["models"]["segmentation"]["path"],
                   "--embedding", status["models"]["embedding"]["path"],
                   "--threads", str(threads), "--cluster-threshold", str(cluster_threshold), "--output", str(output)]
        if num_speakers is not None:
            command += ["--num-speakers", str(num_speakers)]
        with tempfile.TemporaryFile() as log:
            try:
                process = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=log, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise SpeakerError(f"本地说话人分析超过 {timeout:g} 秒，子进程已停止。") from exc
            except OSError as exc:
                raise SpeakerError(f"无法启动说话人分析子进程：{exc}") from exc
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 2000))
            detail = log.read().decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise SpeakerError(f"本地说话人分析失败（退出码 {process.returncode}）：{detail}")
        try:
            if output.stat().st_size > 8 * 1024 * 1024:
                raise SpeakerError("说话人分析结果超过大小限制。")
            result = json.loads(output.read_text(encoding="utf-8"))
            _validate_result(result)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise SpeakerError("说话人分析子进程未返回有效结果。") from exc
        return result


def _validate_result(result):
    if not isinstance(result, dict) or not isinstance(result.get("turns"), list) or not isinstance(result.get("speakers"), list):
        raise ValueError("Invalid speaker result")
    if (result.get("backend") != BACKEND or not isinstance(result.get("embedding_model_id"), str)
            or not result["embedding_model_id"].startswith("sha256:")
            or result.get("speaker_count") != len(result["speakers"])):
        raise ValueError("Invalid speaker metadata")
    ids = set()
    for speaker in result["speakers"]:
        label = speaker["id"]
        if not isinstance(label, str) or not label.startswith("speaker_") or label in ids:
            raise ValueError("Invalid speaker label")
        ids.add(label)
        vector = speaker["embedding"]
        if vector is not None and (not isinstance(vector, list) or not 1 <= len(vector) <= 4096
                                   or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector)):
            raise ValueError("Invalid embedding")
        if not isinstance(speaker["speech_seconds"], (int, float)) or not 0 <= speaker["speech_seconds"] <= MAX_AUDIO_SECONDS:
            raise ValueError("Invalid speech duration")
    for turn in result["turns"]:
        if turn["speaker"] not in ids or not 0 <= turn["start"] < turn["end"] <= MAX_AUDIO_SECONDS:
            raise ValueError("Invalid speaker turn")
    if not isinstance(result["elapsed_seconds"], (int, float)) or not math.isfinite(result["elapsed_seconds"]) or result["elapsed_seconds"] < 0:
        raise ValueError("Invalid elapsed time")


def _merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        elif end > start:
            merged.append((start, end))
    return merged


def _exclusive_ranges(turns, speaker, frames):
    """Select at most 30 seconds with no other detected speaker, in PCM frames."""
    own, others = [], []
    for turn in turns:
        span = (max(0, round(turn["start"] * SAMPLE_RATE)), min(frames, round(turn["end"] * SAMPLE_RATE)))
        (own if turn["speaker"] == speaker else others).append(span)
    exclusive = []
    blockers = _merge_intervals(others)
    for start, end in _merge_intervals(own):
        cursor = start
        for left, right in blockers:
            if right <= cursor:
                continue
            if left >= end:
                break
            if left > cursor:
                exclusive.append((cursor, min(left, end)))
            cursor = max(cursor, right)
            if cursor >= end:
                break
        if cursor < end:
            exclusive.append((cursor, end))
    # Prefer long uninterrupted examples. Restore chronological order afterward.
    budget = MAX_EMBEDDING_SECONDS * SAMPLE_RATE
    selected = []
    for start, end in sorted(exclusive, key=lambda item: (-(item[1] - item[0]), item[0])):
        take = min(end - start, budget)
        if take > 0:
            selected.append((start, start + take))
            budget -= take
        if not budget:
            break
    return sorted(selected)


def _analyze_inprocess(path: Path, segmentation: Path, embedding: Path, num_speakers=None, threads=2,
                       cluster_threshold=DEFAULT_CLUSTER_THRESHOLD):
    """Worker-only implementation using the upstream offline Python API."""
    started = time.monotonic()
    _validate_num_speakers(num_speakers)
    frames, duration = _audio_info(path)
    if not 1 <= threads <= 16:
        raise SpeakerError("CPU 线程数须为 1–16。")
    if not math.isfinite(cluster_threshold) or not 0 < cluster_threshold < 1:
        raise SpeakerError("说话人聚类阈值必须是 0–1 之间的有限数字。")
    for model in (segmentation, embedding):
        if not model.is_file() or model.stat().st_size == 0:
            raise SpeakerError("缺少本地说话人模型；不会自动下载。")
    try:
        import numpy as np
        import sherpa_onnx
    except ImportError as exc:
        raise SpeakerError("缺少本地 sherpa-onnx 运行依赖。") from exc
    with wave.open(str(path), "rb") as audio:
        pcm = audio.readframes(frames)
    if len(pcm) != frames * 2:
        raise SpeakerError("WAV 音频不完整，不能分析。")
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    model_hash = hashlib.sha256()
    with embedding.open("rb") as model_file:
        for block in iter(lambda: model_file.read(1024 * 1024), b""):
            model_hash.update(block)
    embedding_model_id = "sha256:" + model_hash.hexdigest()
    embedding_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=threads, provider="cpu")
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation), window_shift_ratio=0.1),
            num_threads=threads, provider="cpu"),
        embedding=embedding_config,
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers if num_speakers is not None else -1, threshold=cluster_threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    if not config.validate():
        raise SpeakerError("本地说话人模型配置无效。")
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    if diarizer.sample_rate != SAMPLE_RATE:
        raise SpeakerError("本地说话人模型采样率不匹配。")
    native_turns = diarizer.process(samples).sort_by_start_time()
    turns, labels = [], {}
    for item in native_turns:
        start, end = float(item.start), float(item.end)
        if not math.isfinite(start) or not math.isfinite(end):
            raise SpeakerError("模型返回了无效的时间段。")
        start, end = max(0.0, start), min(duration, end)
        if end <= start:
            continue
        if item.speaker not in labels:
            labels[item.speaker] = f"speaker_{len(labels) + 1}"
        turns.append({"start": start, "end": end, "speaker": labels[item.speaker]})
    # Free the diarization model before creating the enrollment extractor.
    del diarizer
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(embedding_config) if labels else None
    speakers = []
    for label in labels.values():
        intervals = _merge_intervals((turn["start"], turn["end"]) for turn in turns if turn["speaker"] == label)
        ranges = _exclusive_ranges(turns, label, frames)
        embedding_seconds = sum(end - start for start, end in ranges) / SAMPLE_RATE
        notes, vector = [], None
        if not ranges:
            notes.append("没有非重叠语音，未生成可用于本人标注的声纹。")
        else:
            # Let the extractor decide whether the samples are usable. Short
            # references are allowed when the user explicitly confirms them.
            selected = np.concatenate([samples[start:end] for start, end in ranges])
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=selected)
            stream.input_finished()
            if not extractor.is_ready(stream):
                notes.append("模型未获得足够有效语音，未生成声纹。")
            else:
                raw = np.asarray(extractor.compute(stream), dtype=np.float64).reshape(-1)
                norm = float(np.linalg.norm(raw))
                if not 0 < raw.size <= 4096 or not np.isfinite(raw).all() or not math.isfinite(norm) or norm <= 1e-12:
                    notes.append("模型未生成有效声纹，未进行本人匹配。")
                else:
                    vector = (raw / norm).tolist()
        speakers.append({"id": label, "embedding": vector,
                         "speech_seconds": round(sum(end - start for start, end in intervals), 3),
                         "embedding_seconds": round(embedding_seconds, 3),
                         "reference_seconds": embedding_seconds if vector is not None else 0.0,
                         "notes": notes})
    notes = ["说话人编号只在当前音频内有效；分段与声纹相似度不代表身份认证或参与对话的关系。"]
    if num_speakers is None:
        notes.append(f"人数由聚类阈值 {cluster_threshold:g} 自动估计，可能把同一人拆开或把不同人合并；尚未在你的声场校准。")
    else:
        notes.append(f"本次按指定的 {num_speakers} 人聚类；结果不是独立的人数验证。")
    result = {"turns": turns, "speakers": speakers, "speaker_count": len(speakers),
              "elapsed_seconds": round(time.monotonic() - started, 3), "backend": BACKEND,
              "embedding_model_id": embedding_model_id, "duration_seconds": duration,
              "cluster_threshold": cluster_threshold, "requested_speakers": num_speakers, "notes": notes}
    _validate_result(result)
    return result
