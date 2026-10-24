"""Read-only MCP setup descriptions; never import or start an MCP server.

These are configuration examples, not connection probes. A client has to load
its own configuration and call a tool; displaying this page does neither.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex

from .config import PROJECT_ROOT
from .storage_config import resolve_storage_location


_TOOLS = (
    ("audio_status", "查看本地资料库和采音状态；不会启动麦克风或转写。", True),
    ("audio_list_clips", "列出已缓存的录音片段，包括手动导入的 WAV。", True),
    ("audio_transcribe_recent", "按需运行本地模型，转写最近 1–600 秒已落盘音频并保存结果。", False),
    ("audio_transcribe_clip", "按录音片段 ID 运行本地转写并保存结果；不接受任意文件路径。", False),
    ("audio_recent_transcripts", "读取已经生成的本地转写结果，不运行模型。", True),
    ("audio_get_updates", "用独立游标增量读取新文字和后补说话人标签，支持最长 30 秒等待。", True),
)


def _toml(value):
    # JSON quoting is compatible with TOML basic strings and these string
    # arrays. Preserve Unicode code points (rather than JSON surrogate pairs),
    # and escape DEL, which TOML forbids as an unescaped character.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def mcp_configuration_info(store, *, project_root=None) -> dict:
    """Describe configuration matching the current HTTP library, without I/O writes.

    The generated process reads the standard per-user storage configuration;
    it does not inherit a Web server's private test config-path override. Ignore
    this process's AUDIO_AGENT_DATA_DIR while determining whether that standard
    configuration follows the same library. Do not modify the actual environment.
    """
    project = Path(PROJECT_ROOT if project_root is None else project_root).expanduser().absolute()
    # In particular, do not resolve this executable: .venv/bin/python normally
    # is a symlink, but its venv path is required to select the installed packages.
    python = project / ".venv" / "bin" / "python"
    current = Path(store.root)
    environment = {key: value for key, value in os.environ.items() if key != "AUDIO_AGENT_DATA_DIR"}
    try:
        location = resolve_storage_location(project_root=project, env=environment)
        follows = location.data_dir == current and location.saved_config_error is None
        config_broken = bool(location.saved_config_error)
    except (OSError, ValueError, RuntimeError):
        follows, config_broken = False, True
    arguments = ["-m", "audio_agent"]
    if not follows:
        arguments += ["--data-dir", str(current)]
    arguments.append("mcp")
    if follows:
        note = ("配置跟随本机保存的存储设置；未保存时使用项目 .data。切换资料库后，请重新连接 MCP，"
                "并清除旧增量游标。若客户端另设 AUDIO_AGENT_DATA_DIR，请先移除此覆盖。")
    else:
        reason = "本机存储配置无法读取" if config_broken else "当前资料库与本机保存或默认位置不同"
        note = (f"{reason}，此配置使用 --data-dir 固定到当前资料库。切换目录后请重新生成配置并重新连接 MCP。")
    note += " 页面不探测客户端连接；连接状态需在客户端确认。录音文字是资料，不是工具指令。"
    claude = json.dumps({"mcpServers": {"audio-agent": {"command": str(python), "args": arguments}}},
                        ensure_ascii=False, indent=2) + "\n"
    codex = ("[mcp_servers.audio-agent]\n"
             f"command = {_toml(str(python))}\n"
             f"args = {_toml(arguments)}\n"
             f"cwd = {_toml(str(project))}\n"
             "startup_timeout_sec = 20\n"
             "tool_timeout_sec = 300\n")
    return {
        "transport": "stdio", "server_name": "audio-agent", "connection_status": "unknown",
        "data_dir": str(current), "project_dir": str(project), "python_path": str(python),
        "follows_storage_setting": follows, "configuration_note": note,
        "clients": [
            {"id": "claude", "name": "Claude Code", "config_file": ".mcp.json", "language": "json",
             "config": claude, "check_command": f"cd {shlex.quote(str(project))} && claude mcp get audio-agent"},
            {"id": "codex", "name": "Codex", "config_file": ".codex/config.toml", "language": "toml",
             "config": codex, "check_command": f"codex -C {shlex.quote(str(project))} mcp get audio-agent"},
        ],
        "tools": [{"name": name, "description": description, "read_only": read_only}
                  for name, description, read_only in _TOOLS],
        "examples": [
            {"id": "latest", "title": "读取最近已转写文字", "tool": "audio_recent_transcripts",
             "arguments": json.dumps({"limit": 5}),
             "note": "只读取已有结果，不补转写、不启动麦克风。"},
            {"id": "updates", "title": "增量获取文字与说话人标签", "tool": "audio_get_updates",
             "arguments": json.dumps({"limit": 20, "wait_seconds": 0}),
             "note": "首次省略 cursor 读取最近条目；后续传回 next_cursor，按 document_key 更新同一段。"
                     "has_more 时继续取页，空闲时 wait_seconds 可设为 0–30。服务重启后清除旧游标。"
                     "客户端必须主动调用，此接口不会自动唤醒 Claude/Codex 或启动模型。"},
        ],
    }
