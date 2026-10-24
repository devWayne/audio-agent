"""Paths and local defaults; no credentials or remote endpoints."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_root(value=None) -> Path:
    from .storage_config import resolve_storage_location
    return resolve_storage_location(value, project_root=PROJECT_ROOT).data_dir


def ffmpeg_path() -> str:
    import shutil
    configured = os.environ.get("AUDIO_AGENT_FFMPEG")
    found = configured or shutil.which("ffmpeg")
    if not found and Path("/opt/homebrew/bin/ffmpeg").is_file():
        found = "/opt/homebrew/bin/ffmpeg"
    if not found:
        raise RuntimeError("找不到 ffmpeg，请先安装并加入 PATH，或设置 AUDIO_AGENT_FFMPEG。")
    return found
