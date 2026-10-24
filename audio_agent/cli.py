"""Command-line entry point and microphone lifecycle."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import data_root, ffmpeg_path, PROJECT_ROOT


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def capture_state(root):
    from .capture import read_capture_status
    return read_capture_status(root)


def _own_process(state, root):
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        command = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "command="], text=True).strip()
        return "audio_agent" in command and " record " in command and str(root) in command
    except (subprocess.CalledProcessError, OSError):
        return False


def start_background(root, device, chunk_seconds=5, retention_hours=24):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    previous = capture_state(root)
    if previous.get("active") and _own_process(previous, root):
        raise ValueError("此数据目录已有采音进程。请先 stop。")
    command = [sys.executable, "-m", "audio_agent", "--data-dir", str(root),
               "record", "--device", device, "--chunk-seconds", str(chunk_seconds),
               "--retention-hours", str(retention_hours)]
    with (root / "capture.log").open("a") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, cwd=PROJECT_ROOT)
    for _ in range(100):
        current = capture_state(root)
        if current.get("pid") == proc.pid and current.get("running"):
            return current
        if proc.poll() is not None:
            raise RuntimeError(f"采音启动失败。请查看 {root / 'capture.log'}：{current.get('last_error', '')}")
        time.sleep(0.1)
    # Don't leave a hidden capture process behind after a failed start request.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    raise RuntimeError("10 秒内未收到麦克风数据，已停止。请检查系统麦克风权限，或用 record 前台查看错误。")


def stop_background(root):
    current = capture_state(root)
    if not current.get("active"):
        return current
    if not _own_process(current, root):
        raise RuntimeError("采音状态已过期或进程身份不匹配；未向其他进程发送信号。")
    os.kill(current["pid"], signal.SIGTERM)
    for _ in range(100):
        updated = capture_state(root)
        if not updated.get("active"):
            return updated
        time.sleep(0.1)
    raise RuntimeError("采音尚未确认停止，请查看 capture.log。")


def parser():
    p = argparse.ArgumentParser(description="Mac 本地音频缓存与按需转写")
    p.add_argument("--data-dir", help="覆盖本机存储设置；未设置时使用项目 .data")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查依赖和可复用的 Handy 模型")
    commands.add_parser("devices", help="列出麦克风输入设备")
    commands.add_parser("status", help="查看录音与存储状态")
    commands.add_parser("stop", help="停止后台采音并保存最后一段")
    for name in ("record", "start"):
        record = commands.add_parser(name, help="前台采音（Ctrl+C 停止）" if name == "record" else "启动后台采音")
        record.add_argument("--device", required=True, help="devices 输出的精确设备名或索引")
        record.add_argument("--chunk-seconds", type=float, default=5)
        record.add_argument("--retention-hours", type=float, default=24)
        if name == "record":
            record.add_argument("--duration", type=float, help="限定测试录音秒数")
    imp = commands.add_parser("import-wav", help="导入 WAV，不运行转写")
    imp.add_argument("path", type=Path)
    imp.add_argument("--source", default="manual-import")
    imp.add_argument("--start-time", type=float, help="录音开始 Unix 时间；默认文件修改时间减时长")
    ls = commands.add_parser("clips", help="列出音频片段")
    ls.add_argument("--limit", type=int, default=20)
    tx = commands.add_parser("transcribe", help="按需运行本地 Whisper")
    tx.add_argument("--seconds", type=float, default=30)
    tx.add_argument("--last-utterance", action="store_true", help="通过能量阈值选择最近一段讲话")
    tx.add_argument("--clip", help="指定 clips 输出的片段 ID")
    tx.add_argument("--language", default="auto", help="auto、zh、en 等")
    tx.add_argument("--model", help="已有 Whisper GGML 模型文件")
    tx.add_argument("--copy", action="store_true", help="把文本复制到剪贴板")
    tx.add_argument("--text", action="store_true", help="只输出文本")
    history = commands.add_parser("transcripts", help="列出已完成的转写")
    history.add_argument("--limit", type=int, default=5)
    watch = commands.add_parser("watch", help="监听本地同步目录，稳定 WAV 自动入库")
    watch.add_argument("folder", type=Path)
    watch.add_argument("--interval", type=float, default=2)
    watch.add_argument("--stable-seconds", type=float, default=3)
    watch.add_argument("--include-edits", action="store_true")
    prune = commands.add_parser("prune", help="删除超过保留时长的项目缓存")
    prune.add_argument("--retention-hours", type=float, default=24)
    commands.add_parser("mcp", help="启动 stdio MCP，不自动采音")
    web = commands.add_parser("web", help="打开本地 Web 工作台（录音、试听和转写）")
    web.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765")
    cfg = commands.add_parser("mcp-config", help="生成当前路径的 Codex/Claude Code 配置")
    cfg.add_argument("--client", choices=["codex", "claude"], default="codex")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        root = data_root(args.data_dir)
        if args.command == "mcp-config":
            executable = str(Path(sys.executable).absolute())
            argv = ["-m", "audio_agent"]
            if args.data_dir is not None:
                argv += ["--data-dir", str(root)]
            argv += ["mcp"]
            if args.client == "claude":
                emit({"mcpServers": {"audio-agent": {"command": executable, "args": argv}}})
            else:
                print('[mcp_servers.audio-agent]\ncommand = ' + json.dumps(executable)
                      + '\nargs = ' + json.dumps(argv) + '\nstartup_timeout_sec = 20\ntool_timeout_sec = 300')
            return 0
        if args.command == "doctor":
            from .asr import discover_models
            versions = {}
            for name in ("mcp", "pywhispercpp", "numpy"):
                try:
                    versions[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    versions[name] = "not installed"
            emit({"python": sys.version.split()[0], "ffmpeg": ffmpeg_path(), "data_dir": str(root),
                  "packages": versions, "models": discover_models(), "cloud_asr": False})
            return 0
        if args.command == "devices":
            from .capture import list_audio_devices
            emit(list_audio_devices(ffmpeg_path=ffmpeg_path()))
            return 0
        if args.command == "mcp":
            from .mcp_server import build_server
            build_server(root).run(transport="stdio")
            return 0
        if args.command == "web":
            from .web import create_server
            if not 1 <= args.port <= 65535:
                raise ValueError("port 必须在 1–65535 之间。")
            server = create_server(root, port=args.port)
            print(f"Audio Agent Web: http://127.0.0.1:{server.server_address[1]}", flush=True)
            print(f"音频库：{root}\n按 Ctrl+C 关闭界面服务；打开页面不会启动录音。", flush=True)
            try:
                server.serve_forever(poll_interval=0.25)
            finally:
                server.server_close()
            return 0
        from .store import AudioStore
        store = AudioStore(root, retention_hours=getattr(args, "retention_hours", 24), ffmpeg_path=ffmpeg_path())
        if args.command == "status":
            emit({"storage": store.status(), "capture": capture_state(root)})
        elif args.command == "start":
            emit(start_background(root, args.device, args.chunk_seconds, args.retention_hours))
        elif args.command == "stop":
            emit(stop_background(root))
        elif args.command == "record":
            from .capture import AudioCapture, CaptureConfig
            if args.duration is not None and args.duration <= 0:
                raise ValueError("duration 必须大于 0。")
            capture = AudioCapture(store, CaptureConfig(device=args.device, chunk_seconds=args.chunk_seconds,
                                                        ffmpeg_path=ffmpeg_path()))
            def stop(signum=None, frame=None):
                capture.stop()
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            timer = None
            try:
                capture.start()
                if args.duration:
                    timer = threading.Timer(args.duration, stop)
                    timer.daemon = True
                    timer.start()
                capture.wait()
            finally:
                if timer:
                    timer.cancel()
            emit(capture.status())
            if capture.status().get("last_error"):
                return 1
        elif args.command == "import-wav":
            emit(store.import_wav(args.path, source=args.source, start_time=args.start_time))
        elif args.command == "clips":
            if not 1 <= args.limit <= 100:
                raise ValueError("limit 必须在 1–100 之间。")
            emit(store.list_clips(limit=args.limit))
        elif args.command == "transcribe":
            from .pipeline import transcribe_clip, transcribe_recent, copy_text
            kwargs = {"model_path": args.model, "language": args.language}
            result = (transcribe_clip(store, args.clip, **kwargs) if args.clip else
                      transcribe_recent(store, args.seconds, last_utterance=args.last_utterance, **kwargs))
            if args.copy:
                copy_text(result["text"])
                result["copied"] = True
            if args.text:
                print(result["text"])
            else:
                emit(result)
        elif args.command == "transcripts":
            from .pipeline import recent_transcripts
            emit(recent_transcripts(store, args.limit))
        elif args.command == "watch":
            from .watcher import FolderWatcher
            if args.interval < 0.2:
                raise ValueError("interval 必须至少为 0.2 秒。")
            watcher = FolderWatcher(store, args.folder, args.stable_seconds, args.include_edits)
            emit({"watching": str(watcher.folder), "automatic_transcription": False})
            while True:
                for result in watcher.scan():
                    emit(result)
                store.prune()
                time.sleep(args.interval)
        elif args.command == "prune":
            from .pipeline import prune_transcripts
            emit({"audio": store.prune(), "transcripts_removed": prune_transcripts(store, args.retention_hours)})
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"audio-agent: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
