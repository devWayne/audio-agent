"""Resolve local storage locations without creating or opening a library.

CLI/environment overrides keep relative-path compatibility. Persisted settings
contain one absolute directory, never credentials. Only write_storage_config()
writes files; it does not create, move, prune, or open the selected data library.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_CONFIG_BYTES = 16_384


class StorageConfigError(ValueError):
    """The saved configuration or a supplied location cannot be used safely."""


@dataclass(frozen=True)
class StorageLocation:
    data_dir: Path
    source: str
    config_path: Path
    default_data_dir: Path
    saved_data_dir: Path | None
    saved_config_error: str | None = None

    def as_dict(self) -> dict:
        return {"data_dir": str(self.data_dir), "source": self.source,
                "config_path": str(self.config_path), "default_data_dir": str(self.default_data_dir),
                "saved_data_dir": str(self.saved_data_dir) if self.saved_data_dir is not None else None,
                "saved_config_error": self.saved_config_error}


def _environment(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _path(value, *, label: str, absolute: bool, directory: bool = True,
          home: Path | None = None, resolve: bool = True) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise StorageConfigError(f"{label}必须为目录路径。")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise StorageConfigError(f"{label}不能为空或包含空字符。")
    candidate = Path(raw)
    if absolute and not candidate.is_absolute():
        raise StorageConfigError(f"{label}必须为绝对路径。")
    try:
        if not absolute:
            if home is not None and (raw == "~" or raw.startswith("~/")):
                candidate = home / raw[2:] if raw != "~" else home
            else:
                candidate = candidate.expanduser()
        # Config filenames are normalized lexically: resolving them would
        # follow a symlink before the no-follow check in read/write below.
        candidate = candidate.resolve() if resolve else Path(os.path.abspath(candidate))
        if directory and candidate.exists() and not candidate.is_dir():
            raise StorageConfigError(f"{label}指向文件，必须选择目录。")
    except (OSError, RuntimeError) as exc:
        raise StorageConfigError(f"无法解析{label}。") from exc
    return candidate


def _home(home, env) -> Path:
    return _path(home if home is not None else env.get("HOME") or Path.home(),
                 label="用户主目录", absolute=True)


def storage_config_path(*, home=None, env: Mapping[str, str] | None = None,
                        platform: str | None = None) -> Path:
    environment = _environment(env)
    user_home = _home(home, environment)
    if (sys.platform if platform is None else platform) == "darwin":
        return user_home / "Library" / "Application Support" / "Audio Agent" / "storage.json"
    base = _path(environment.get("XDG_CONFIG_HOME") or user_home / ".config",
                 label="XDG_CONFIG_HOME", absolute=True)
    return base / "audio-agent" / "storage.json"


def default_data_dir(*, project_root=None, home=None, env: Mapping[str, str] | None = None,
                     platform: str | None = None) -> Path:
    """Keep new and existing libraries in the project's .data by default."""
    environment = _environment(env)
    user_home = _home(home, environment)
    project = _path(PROJECT_ROOT if project_root is None else project_root,
                    label="项目目录", absolute=False, home=user_home, directory=False, resolve=False)
    candidate = project / ".data"
    return _path(candidate, label="默认数据目录", absolute=True, directory=False, resolve=False)


def _config_path(config_path, *, home=None, env=None, platform=None) -> Path:
    path = config_path if config_path is not None else storage_config_path(home=home, env=env, platform=platform)
    return _path(path, label="配置文件位置", absolute=True, directory=False, resolve=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StorageConfigError("存储配置包含重复字段；请修复配置后重试。")
        result[key] = value
    return result


def read_saved_data_dir(config_path=None, *, home=None, env: Mapping[str, str] | None = None,
                        platform: str | None = None) -> Path | None:
    """Read the saved absolute location, or None only when no file exists."""
    path = _config_path(config_path, home=home, env=env, platform=platform)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
                raise StorageConfigError("存储配置必须是大小有效的普通 JSON 文件。")
            raw = handle.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise StorageConfigError("存储配置文件过大。")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {"data_dir"} or not isinstance(value["data_dir"], str):
            raise StorageConfigError("存储配置只能包含字符串字段 data_dir。")
        return _path(value["data_dir"], label="已保存的数据目录", absolute=True)
    except FileNotFoundError:
        return None
    except StorageConfigError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise StorageConfigError(f"无法读取存储配置 {path}；请修复配置或使用 --data-dir 指定资料库。") from exc


def resolve_storage_location(explicit_data_dir=None, *, project_root=None,
                             env: Mapping[str, str] | None = None, home=None,
                             platform: str | None = None, config_path=None) -> StorageLocation:
    """Precedence: explicit > environment > saved > existing legacy > default.

    No directories or files are created. An override remains a recovery route
    when the saved JSON is broken; its diagnostic is returned without making
    the caller fall back to an empty library. Without an override, errors raise.
    """
    environment = _environment(env)
    user_home = _home(home, environment)
    path = _config_path(config_path, home=user_home, env=environment, platform=platform)
    default = default_data_dir(project_root=project_root, home=user_home, env=environment, platform=platform)
    override = None
    source = None
    if explicit_data_dir is not None:
        override = _path(explicit_data_dir, label="指定数据目录", absolute=False, home=user_home)
        source = "explicit"
    elif "AUDIO_AGENT_DATA_DIR" in environment:
        override = _path(environment["AUDIO_AGENT_DATA_DIR"], label="AUDIO_AGENT_DATA_DIR", absolute=False, home=user_home)
        source = "environment"
    saved, saved_error = None, None
    try:
        saved = read_saved_data_dir(path)
    except StorageConfigError as exc:
        if override is None:
            raise
        saved_error = str(exc)
    if override is not None:
        selected = override
    elif saved is not None:
        selected, source = saved, "saved"
    else:
        project = _path(PROJECT_ROOT if project_root is None else project_root,
                        label="项目目录", absolute=False, home=user_home)
        legacy = project / ".data"
        if (legacy / "audio.sqlite3").is_file():
            selected, source = legacy.resolve(), "legacy"
        else:
            selected, source = _path(default, label="默认数据目录", absolute=True), "default"
    return StorageLocation(selected, source, path, default, saved, saved_error)


def write_storage_config(data_dir, *, config_path=None, home=None,
                         env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Atomically save a 0600 one-field config; never create/migrate the library.

    A malformed regular config can be replaced explicitly. Symlink and special
    config files are rejected. Before os.replace succeeds, errors preserve the
    previous config; no partially written JSON is exposed to another reader.
    """
    selected = _path(data_dir, label="要保存的数据目录", absolute=True)
    path = _config_path(config_path, home=home, env=env, platform=platform)
    temporary = None
    try:
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise StorageConfigError("存储配置不能是符号链接或其他特殊文件。")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".storage-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({"data_dir": str(selected)}, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return selected
    except StorageConfigError:
        raise
    except OSError as exc:
        raise StorageConfigError(f"无法保存存储配置 {path}；原有资料库未被移动或修改。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
