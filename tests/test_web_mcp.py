"""Read-only MCP instructions with isolated home paths and inert HTTP state."""
import ast
import builtins
import json
import os
from pathlib import Path
import shlex
import threading
import tomllib
from types import SimpleNamespace
from unittest import mock

import pytest

from audio_agent import mcp_info, storage_config, web
from test_web_live import request


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    project = tmp_path / "项目 空格"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.delenv("AUDIO_AGENT_DATA_DIR", raising=False)
    monkeypatch.setattr(web, "PROJECT_ROOT", project)
    store = SimpleNamespace(root=project / ".data")
    server = SimpleNamespace(store=store, token="test", server_port=8767,
                             _request_lock=threading.RLock())
    return server, project, storage_config.storage_config_path()


def configurations(info):
    clients = {client["id"]: client for client in info["clients"]}
    claude = json.loads(clients["claude"]["config"])["mcpServers"]["audio-agent"]
    codex = tomllib.loads(clients["codex"]["config"])["mcp_servers"]["audio-agent"]
    return clients, claude, codex


def test_http_get_is_read_only_and_reports_unknown_connection(setup, tmp_path, monkeypatch):
    server, project, config = setup
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    forbidden = mock.Mock(side_effect=AssertionError("MCP information must not run hardware/models/servers"))
    for name in ("AudioStore", "discover_models", "list_audio_devices", "transcribe_clip", "read_capture_status"):
        monkeypatch.setattr(web, name, forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    original_open = os.open
    opened = []
    def read_only_open(path, flags, *args, **kwargs):
        assert Path(path) == config  # No credentials or client configurations are read.
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        opened.append(Path(path))
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", read_only_open)
    original_import = builtins.__import__
    def safe_import(name, *args, **kwargs):
        assert "mcp_server" not in name and not name.startswith("mcp.")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", safe_import)
    status, info = request(server, "/api/mcp")
    assert status == 200
    assert info["transport"] == "stdio" and info["server_name"] == "audio-agent"
    assert info["connection_status"] == "unknown"
    assert info["data_dir"] == str(project / ".data")
    assert info["project_dir"] == str(project)
    assert info["python_path"] == str(project / ".venv/bin/python")
    assert info["follows_storage_setting"] is True
    assert opened == [config]
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert not config.exists()
    assert not server.store.root.exists()
    forbidden.assert_not_called()
    clients, claude, codex = configurations(info)
    assert claude["args"] == codex["args"] == ["-m", "audio_agent", "mcp"]
    assert "cwd" not in claude and codex["cwd"] == str(project)
    assert codex["startup_timeout_sec"] == 20 and codex["tool_timeout_sec"] == 300
    assert clients["claude"]["config_file"] == ".mcp.json"
    assert clients["codex"]["config_file"] == ".codex/config.toml"
    assert "重新连接" in info["configuration_note"]


def test_paths_with_quotes_spaces_unicode_and_python_symlink_are_literal(setup, tmp_path):
    server, _, _ = setup
    project = tmp_path / '项目 "双引号" 单\'引号 \\ 还有😀\x7f'
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    executable = tmp_path / "outside-python"
    executable.write_text("This is a fixture; never execute it.")
    python.symlink_to(executable)
    server.store.root = project / ".data"
    info = mcp_info.mcp_configuration_info(server.store, project_root=project)
    clients, claude, codex = configurations(info)
    assert claude["command"] == codex["command"] == info["python_path"] == str(python)
    assert info["python_path"] != str(python.resolve())
    assert codex["cwd"] == str(project)
    assert shlex.split(clients["claude"]["check_command"]) == ["cd", str(project), "&&", "claude", "mcp", "get", "audio-agent"]
    assert shlex.split(clients["codex"]["check_command"]) == ["codex", "-C", str(project), "mcp", "get", "audio-agent"]


def test_saved_custom_library_matches_without_fixed_data_argument(setup, tmp_path, monkeypatch):
    server, project, config = setup
    selected = tmp_path / "自定义资料库"
    storage_config.write_storage_config(selected)
    saved = config.read_bytes()
    server.store.root = selected
    monkeypatch.setenv("AUDIO_AGENT_DATA_DIR", str(tmp_path / "environment-override"))
    status, info = request(server, "/api/mcp")
    assert status == 200 and info["follows_storage_setting"] is True
    assert info["data_dir"] == str(selected)
    _, claude, codex = configurations(info)
    assert claude["args"] == codex["args"] == ["-m", "audio_agent", "mcp"]
    assert config.read_bytes() == saved
    assert os.environ["AUDIO_AGENT_DATA_DIR"] == str(tmp_path / "environment-override")


def test_current_override_is_pinned_when_standard_client_would_open_another_library(setup, tmp_path, monkeypatch):
    server, _, _ = setup
    selected = tmp_path / '其他 "库"'
    server.store.root = selected
    monkeypatch.setenv("AUDIO_AGENT_DATA_DIR", str(selected))
    status, info = request(server, "/api/mcp")
    assert status == 200 and info["follows_storage_setting"] is False
    _, claude, codex = configurations(info)
    assert claude["args"] == codex["args"] == ["-m", "audio_agent", "--data-dir", str(selected), "mcp"]
    assert "固定" in info["configuration_note"] and "重新生成" in info["configuration_note"]


def test_broken_saved_configuration_returns_pinned_recovery_without_repairing_file(setup):
    server, _, config = setup
    config.parent.mkdir(parents=True)
    config.write_text("{broken json")
    status, info = request(server, "/api/mcp")
    assert status == 200 and info["follows_storage_setting"] is False
    assert "无法读取" in info["configuration_note"]
    _, claude, _ = configurations(info)
    assert claude["args"][-3:] == ["--data-dir", str(server.store.root), "mcp"]
    assert config.read_text() == "{broken json"


def test_private_web_config_path_does_not_misrepresent_independent_client_config(setup, tmp_path):
    server, _, _ = setup
    selected = tmp_path / "web-only-library"
    custom = tmp_path / "web-only-config.json"
    storage_config.write_storage_config(selected, config_path=custom)
    server.storage_config_path = custom
    server.store.root = selected
    status, info = request(server, "/api/mcp")
    assert status == 200 and info["follows_storage_setting"] is False
    assert "--data-dir" in configurations(info)[1]["args"]


def test_tool_descriptions_match_declared_mcp_tools_without_importing_server(setup):
    server, _, _ = setup
    _, info = request(server, "/api/mcp")
    tree = ast.parse(Path(mcp_info.__file__).with_name("mcp_server.py").read_text())
    builder = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "build_server")
    actual = {}
    for node in builder.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "tool":
                annotation = next(keyword.value.id for keyword in decorator.keywords if keyword.arg == "annotations")
                actual[node.name] = annotation == "read"
    assert len(info["tools"]) == len(actual) == 6
    assert {tool["name"]: tool["read_only"] for tool in info["tools"]} == actual
    assert all(isinstance(tool["description"], str) and tool["description"] for tool in info["tools"])
    examples = {value["id"]: value for value in info["examples"]}
    assert set(examples) == {"latest", "updates"}
    assert json.loads(examples["latest"]["arguments"]) == {"limit": 5}
    assert json.loads(examples["updates"]["arguments"]) == {"limit": 20, "wait_seconds": 0}
    assert examples["updates"]["tool"] == "audio_get_updates"
    assert "next_cursor" in examples["updates"]["note"] and "主动调用" in examples["updates"]["note"]


def test_mcp_information_keeps_same_origin_protection_and_has_no_write_route(setup, monkeypatch):
    server, _, _ = setup
    forbidden = mock.Mock(side_effect=AssertionError("Invalid request must not read configuration"))
    monkeypatch.setattr(web, "mcp_configuration_info", forbidden)
    status, error = request(server, "/api/mcp", origin="https://example.invalid")
    assert status == 403 and error["error"]["code"] == "invalid_origin"
    status, _ = request(server, "/api/mcp", method="POST", body={}, token=True)
    assert status == 404
    forbidden.assert_not_called()
