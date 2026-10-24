"""Install local MCP configuration and a managed skill without starting a client.

The installer never changes trust/approval settings, calls MCP, or imports an
inference/capture module. Configuration and skill updates are staged separately;
normal failures roll them back. Backups remain local for manual recovery after
an interrupted process. Clients should be closed while their config is edited.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid

import tomlkit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ".audio-agent-install.json"
OWNER = "local-audio-agent/agent-integration"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_SKILL_FILES = 500


class InstallError(ValueError):
    """A conflict or unsafe destination requires an explicit user resolution."""


def _absolute(value, label):
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw or not Path(raw).is_absolute():
        raise InstallError(f"{label}必须是非空绝对路径。")
    return Path(os.path.abspath(raw))


def _safe_path(path):
    """Reject symlinks in every existing component, before reads and writes."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise InstallError(f"拒绝跟随符号链接：{part}")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise InstallError(f"父路径不是目录：{part}")


def _read(path):
    _safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise InstallError(f"配置/技能文件必须是大小有效的普通文件：{path}")
        data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise InstallError(f"文件过大：{path}")
        return data


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallError("JSON 配置包含重复字段，请先修复。")
        result[key] = value
    return result


def _json(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(InstallError("JSON 包含非有限数值。")))
        # JSON's parse_constant does not catch numeric overflow such as 1e999.
        # Validate before any write/backup so malformed user config stays intact.
        json.dumps(value, allow_nan=False)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InstallError("无法解析 JSON 配置，请先修复；原文件未修改。") from exc
    if not isinstance(value, dict):
        raise InstallError("配置必须是对象，不能是数组或其他值。")
    return value


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def _tree(path):
    _safe_path(path)
    if not path.is_dir():
        raise InstallError(f"技能目录不存在或不是目录：{path}")
    files, directories = {}, []
    def walk_error(error):
        raise error
    for current, dirs, names in os.walk(path, followlinks=False, onerror=walk_error):
        for name in sorted(dirs):
            child = Path(current) / name
            _safe_path(child)
            directories.append(child.relative_to(path).as_posix())
        for name in sorted(names):
            child = Path(current) / name
            raw = _read(child)
            if raw is None:
                raise InstallError("技能文件在读取时发生变化，请重试。")
            files[child.relative_to(path).as_posix()] = (raw, bool(child.stat().st_mode & 0o111))
            if len(files) > MAX_SKILL_FILES or sum(len(item[0]) for item in files.values()) > MAX_FILE_BYTES:
                raise InstallError("技能包过大。")
    return files, sorted(directories)


def _metadata(files, directories):
    return {"owner": OWNER, "version": 1, "directories": directories,
            "files": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "executable": executable}
                      for name, (raw, executable) in sorted(files.items())}}


def _managed_tree(path):
    if not path.exists() and not path.is_symlink():
        return None
    files, dirs = _tree(path)
    marker = files.pop(MANIFEST, None)
    if marker is None:
        raise InstallError(f"已存在未知同名技能，未覆盖：{path}。请先另行保存或移走该目录。")
    actual = _metadata(files, dirs)
    if _json(marker[0]) != actual:
        raise InstallError(f"已安装技能被修改或来源不明，未覆盖：{path}。请先保留自定义内容。")
    return files, dirs


def _server_conflict(server):
    if not isinstance(server, Mapping):
        raise InstallError("同名 audio-agent 配置不是对象，未覆盖。")
    if server.get("type", "stdio") != "stdio" or any(k in server for k in ("url", "http_headers", "bearer_token_env_var", "headers")):
        raise InstallError("同名 audio-agent 已用于其他传输配置，未覆盖。")
    args = server.get("args")
    command = server.get("command")
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(v, str) for v in args):
        raise InstallError("无法确认同名 audio-agent 是本项目服务，未覆盖。")
    if args[:2] != ["-m", "audio_agent"] or not args or args[-1] != "mcp":
        raise InstallError("同名 audio-agent 指向其他命令，未覆盖。")


def _merge_config(raw, client, project, data_dir):
    args = ["-m", "audio_agent"]
    if data_dir is not None:
        args += ["--data-dir", str(data_dir)]
    args.append("mcp")
    command = str(project / ".venv" / "bin" / "python")  # Keep the venv symlink path.
    if client == "codex":
        try:
            doc = tomlkit.parse(raw.decode("utf-8")) if raw is not None else tomlkit.document()
        except (ValueError, UnicodeError) as exc:
            raise InstallError("无法解析 Codex TOML 配置，请先修复；原文件未修改。") from exc
        key = "mcp_servers"
        if key not in doc:
            doc[key] = tomlkit.table()
        if not isinstance(doc[key], Mapping):
            raise InstallError("mcp_servers 必须是 TOML 表。")
        if "audio-agent" in doc[key]:
            _server_conflict(doc[key]["audio-agent"])
        else:
            doc[key]["audio-agent"] = tomlkit.table()
        server = doc[key]["audio-agent"]
        # Only connection fields are owned. Preserve enabled, approvals, env,
        # tool lists and any explicit timeout the user already selected.
        server["command"], server["args"], server["cwd"] = command, args, str(project)
        server.setdefault("startup_timeout_sec", 20)
        server.setdefault("tool_timeout_sec", 300)
        result = tomlkit.dumps(doc).encode()
    else:
        doc = _json(raw) if raw is not None else {}
        if "mcpServers" not in doc:
            doc["mcpServers"] = {}
        if not isinstance(doc["mcpServers"], dict):
            raise InstallError("mcpServers 必须是 JSON 对象。")
        if "audio-agent" in doc["mcpServers"]:
            _server_conflict(doc["mcpServers"]["audio-agent"])
        server = doc["mcpServers"].setdefault("audio-agent", {})
        server["command"], server["args"] = command, args
        # Semantic no-op also preserves the user's existing JSON whitespace.
        result = raw if raw is not None and doc == _json(raw) else _encode(doc)
    return result


def _locations(client, scope, workspace, home, env):
    if scope == "project":
        return ((workspace / ".codex" / "config.toml", workspace / ".agents" / "skills" / "audio-agent")
                if client == "codex" else
                (workspace / ".mcp.json", workspace / ".claude" / "skills" / "audio-agent"))
    if client == "codex":
        config_home = _absolute(env.get("CODEX_HOME") or home / ".codex", "CODEX_HOME")
        return config_home / "config.toml", home / ".agents" / "skills" / "audio-agent"
    config_home = _absolute(env.get("CLAUDE_CONFIG_DIR") or home / ".claude", "CLAUDE_CONFIG_DIR")
    # With CLAUDE_CONFIG_DIR, Claude puts its global .claude.json inside it.
    config = config_home / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    return config, config_home / "skills" / "audio-agent"


def _configuration_warnings(raw, client, data_dir):
    doc = tomlkit.parse(raw.decode()) if client == "codex" else _json(raw)
    server = doc["mcp_servers" if client == "codex" else "mcpServers"]["audio-agent"]
    env = server.get("env", {})
    forwarded = server.get("env_vars", [])
    forwards_data = isinstance(forwarded, list) and any(
        value == "AUDIO_AGENT_DATA_DIR" or
        (isinstance(value, Mapping) and value.get("name") == "AUDIO_AGENT_DATA_DIR")
        for value in forwarded)
    override = data_dir is None and ((isinstance(env, Mapping) and "AUDIO_AGENT_DATA_DIR" in env)
                                     or forwards_data)
    warnings = []
    if override:
        warnings.append("已保留服务的 AUDIO_AGENT_DATA_DIR 环境配置，它可能固定资料库；未宣称跟随存储设置。")
    if server.get("enabled") is False or server.get("disabled") is True:
        warnings.append("已保留用户停用状态；安装配置不会启用此服务。")
    return not override and data_dir is None, warnings


def _mkdir(path, created):
    _safe_path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for child in reversed(missing):
        child.mkdir(mode=0o700)
        created.append(child)
    if not path.is_dir():
        raise InstallError(f"目标父路径不是目录：{path}")


def _write_new(path, raw, mode=0o600):
    _safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_config(path, raw):
    _safe_path(path)
    fd, name = tempfile.mkstemp(prefix=".audio-agent-stage-", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _safe_path(path)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def install_integration(client, scope="project", workspace=None, data_dir=None, dry_run=False,
                        *, project_root=None, home=None, env=None):
    """Install both pieces, or raise. dry_run performs no filesystem writes."""
    if client not in {"codex", "claude"} or scope not in {"project", "user"}:
        raise InstallError("client 仅支持 codex/claude，scope 仅支持 project/user。")
    environment = dict(os.environ if env is None else env)
    project = _absolute(PROJECT_ROOT if project_root is None else project_root, "项目目录")
    user_home = _absolute(home if home is not None else environment.get("HOME") or Path.home(), "用户主目录")
    workspace = _absolute(Path.cwd() if workspace is None else workspace, "工作区")
    if not workspace.is_dir():
        raise InstallError("工作区必须是已存在目录。")
    if data_dir is not None:
        data_dir = _absolute(data_dir, "资料库目录")
        if data_dir.exists() and not data_dir.is_dir():
            raise InstallError("资料库目录不能指向文件。")
    python = project / ".venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise InstallError("项目 .venv/bin/python 不可执行，请先安装本项目依赖。")
    source = project / "skills" / "audio-agent"
    source_files, source_dirs = _tree(source)
    if "SKILL.md" not in source_files or not source_files["SKILL.md"][0].strip() or MANIFEST in source_files:
        raise InstallError("源技能包必须含非空 SKILL.md，且不能含安装器私有清单。")
    config, skill = _locations(client, scope, workspace, user_home, environment)
    for target in (config, skill):
        _safe_path(target)
    old_raw = _read(config)
    merged = _merge_config(old_raw, client, project, data_dir)
    follows, warnings = _configuration_warnings(merged, client, data_dir)
    old_skill = _managed_tree(skill)
    new_skill = (source_files, source_dirs)
    config_changed, skill_changed = merged != old_raw, old_skill != new_skill
    summary = {"client": client, "scope": scope, "config_path": str(config), "skill_path": str(skill),
               "project_dir": str(project), "workspace": str(workspace), "data_dir": str(data_dir) if data_dir else None,
               "follows_storage_setting": follows, "warnings": warnings, "backup_paths": [],
               "changed": config_changed or skill_changed, "dry_run": bool(dry_run),
               "status": "dry-run" if dry_run else "unchanged", "connection_status": "unknown",
               "note": "仅安装配置和技能；请重新连接客户端并自行确认项目信任与工具审批。未启动服务、麦克风或模型。"
                       "未指定 --data-dir 时，客户端外部 AUDIO_AGENT_DATA_DIR 环境变量仍可覆盖本机存储设置。"}
    if dry_run or not summary["changed"]:
        return summary
    created, stage = [], None
    old_skill_backup = None
    config_written = skill_written = False
    lock_fd = None
    try:
        _mkdir(config.parent, created)
        _mkdir(skill.parent, created)
        lock_path = config.parent / ".audio-agent-install.lock"
        _safe_path(lock_path)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise InstallError("安装锁不是普通文件。")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("另一个安装器正在修改此配置，请稍后重试。") from exc
        if _read(config) != old_raw or _managed_tree(skill) != old_skill:
            raise InstallError("目标配置或技能在准备期间发生变化，请关闭客户端后重试。")
        suffix = uuid.uuid4().hex
        if config_changed and old_raw is not None:
            backup = config.with_name(config.name + ".audio-agent-backup-" + suffix)
            _write_new(backup, old_raw)
            summary["backup_paths"].append(str(backup))
        if skill_changed:
            # Keep staging and retained backups OUTSIDE the skills discovery
            # root: a second SKILL.md there would be loaded as another skill.
            stage = Path(tempfile.mkdtemp(prefix=".audio-agent-stage-", dir=skill.parent.parent))
            for name in source_dirs:
                (stage / name).mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, (raw, executable) in source_files.items():
                (stage / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_new(stage / name, raw, 0o700 if executable else 0o600)
            _write_new(stage / MANIFEST, _encode(_metadata(source_files, source_dirs)))
            if old_skill is not None:
                backup_root = skill.parent.parent / ".audio-agent-backups"
                _mkdir(backup_root, created)
                old_skill_backup = backup_root / (skill.name + "-" + suffix)
                os.rename(skill, old_skill_backup)
                summary["backup_paths"].append(str(old_skill_backup))
            os.rename(stage, skill)
            stage = None
            skill_written = True
        if config_changed:
            if _read(config) != old_raw:
                raise InstallError("配置被其他进程修改，请关闭客户端后重试。")
            _replace_config(config, merged)
            config_written = True
        summary["status"] = "installed"
        return summary
    except Exception as exc:
        recovery = []
        try:
            if config_written:
                if _read(config) != merged:
                    raise InstallError("配置又被其他进程修改，保留现场及备份。")
                if old_raw is None:
                    config.unlink()
                else:
                    _replace_config(config, old_raw)
            if skill_written:
                if _managed_tree(skill) != new_skill:
                    raise InstallError("技能又被其他进程修改，保留现场及备份。")
                shutil.rmtree(skill)
            if old_skill_backup is not None and old_skill_backup.exists():
                if skill.exists():
                    raise InstallError("技能目标已存在，无法自动恢复备份。")
                os.rename(old_skill_backup, skill)
        except (OSError, InstallError) as rollback:
            recovery.append(str(rollback))
        detail = "；回滚需要检查：" + "；".join(recovery) if recovery else "；未报告安装成功。"
        raise InstallError(f"接入安装失败：{exc}{detail}") from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        if lock_fd is not None:
            os.close(lock_fd)
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装 Audio Agent MCP 与技能；不启动录音或模型。")
    parser.add_argument("--client", required=True, choices=("codex", "claude"))
    parser.add_argument("--scope", default="project", choices=("project", "user"))
    parser.add_argument("--workspace", help="项目配置工作区绝对路径，默认当前目录")
    parser.add_argument("--data-dir", help="固定到指定资料库绝对路径；省略则跟随本机存储设置")
    parser.add_argument("--dry-run", action="store_true", help="只检查并显示计划，不写任何文件")
    args = parser.parse_args(argv)
    try:
        result = install_integration(**vars(args))
    except (InstallError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
