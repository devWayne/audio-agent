import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tomllib

import pytest

from audio_agent import agent_install as installer
from audio_agent.agent_install import InstallError, MANIFEST, install_integration


@pytest.fixture
def layout(tmp_path):
    repo = tmp_path / 'repo 中文 "quote"'
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir(); workspace.mkdir()
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / ".venv/bin/python").symlink_to(sys.executable)
    skill = repo / "skills/audio-agent"
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "empty").mkdir()
    (skill / "SKILL.md").write_text("---\nname: audio-agent\ndescription: test\n---\nRead local text.\n")
    (skill / "references/guide.md").write_text("完整参考中文\n")
    (skill / "scripts/check.py").write_text("print('fixture only')\n")
    (skill / "scripts/check.py").chmod(0o700)
    return dict(project_root=repo, home=home, workspace=workspace, env={"HOME": str(home)})


def install(layout, client="codex", **kwargs):
    return install_integration(client, **(layout | kwargs))


def config_data(result, client):
    raw = Path(result["config_path"]).read_text()
    return tomllib.loads(raw) if client == "codex" else json.loads(raw)


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("scope", ["project", "user"])
def test_scope_complete_skill_idempotence_and_venv_path(layout, client, scope):
    result = install(layout, client, scope=scope)
    assert result["status"] == "installed" and result["changed"]
    doc = config_data(result, client)
    entry = doc["mcp_servers" if client == "codex" else "mcpServers"]["audio-agent"]
    assert entry["command"] == str(layout["project_root"] / ".venv/bin/python")
    assert entry["command"] != str(Path(sys.executable).resolve())
    assert entry["args"] == ["-m", "audio_agent", "mcp"]
    assert entry.get("cwd") == (str(layout["project_root"]) if client == "codex" else None)
    skill = Path(result["skill_path"])
    assert (skill / "references/guide.md").read_text() == "完整参考中文\n"
    assert (skill / "empty").is_dir()
    assert (skill / "scripts/check.py").stat().st_mode & stat.S_IXUSR
    assert (skill / MANIFEST).is_file()
    before = Path(result["config_path"]).stat().st_mtime_ns
    repeated = install(layout, client, scope=scope)
    assert repeated["status"] == "unchanged" and not repeated["changed"]
    assert repeated["backup_paths"] == []
    assert Path(result["config_path"]).stat().st_mtime_ns == before
    assert result["connection_status"] == "unknown"
    assert not (layout["project_root"] / ".data").exists()


def test_user_override_locations(layout):
    env = layout["env"] | {"CODEX_HOME": str(layout["home"] / "codex-other"),
                           "CLAUDE_CONFIG_DIR": str(layout["home"] / "claude-other")}
    codex = install(layout, scope="user", env=env)
    assert Path(codex["config_path"]) == layout["home"] / "codex-other/config.toml"
    assert Path(codex["skill_path"]) == layout["home"] / ".agents/skills/audio-agent"
    claude = install(layout, "claude", scope="user", env=env)
    assert Path(claude["config_path"]) == layout["home"] / "claude-other/.claude.json"
    assert Path(claude["skill_path"]) == layout["home"] / "claude-other/skills/audio-agent"


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_dry_run_has_zero_writes(layout, client):
    before = sorted(str(p) for p in layout["home"].parent.rglob("*"))
    result = install(layout, client, dry_run=True)
    assert result["status"] == "dry-run" and result["changed"]
    assert result["backup_paths"] == []
    assert sorted(str(p) for p in layout["home"].parent.rglob("*")) == before


def test_toml_comments_other_servers_and_user_policy_preserved(layout):
    config = layout["workspace"] / ".codex/config.toml"
    config.parent.mkdir()
    original = '''# User header\nmodel = "my-model"\n[mcp_servers.other]\ncommand = "other" # untouched\n[mcp_servers.audio-agent]\ncommand = "/old/venv/python"\nargs = ["-m", "audio_agent", "mcp"]\nenabled = false # do not enable\napproval_policy = "on-request"\nstartup_timeout_sec = 99\n[mcp_servers.audio-agent.env]\nCUSTOM = "kept"\n'''
    config.write_text(original)
    result = install(layout, data_dir=layout["home"] / "my recordings")
    updated = config.read_text()
    assert "# User header" in updated and '# untouched' in updated and '# do not enable' in updated
    doc = tomllib.loads(updated)
    assert doc["model"] == "my-model"
    assert doc["mcp_servers"]["other"] == {"command": "other"}
    server = doc["mcp_servers"]["audio-agent"]
    assert server["enabled"] is False and server["approval_policy"] == "on-request"
    assert server["startup_timeout_sec"] == 99 and server["env"] == {"CUSTOM": "kept"}
    assert server["args"][-3:] == ["--data-dir", str(layout["home"] / "my recordings"), "mcp"]
    assert not result["follows_storage_setting"]
    assert Path(result["backup_paths"][0]).read_text() == original
    assert stat.S_IMODE(Path(result["backup_paths"][0]).stat().st_mode) == 0o600


def test_claude_global_account_trust_disabled_and_other_server_preserved(layout):
    path = layout["home"] / ".claude.json"
    original = {"account": {"opaque": "do-not-output"}, "projects": {"somewhere": {"hasTrustDialogAccepted": False}},
                "disabledMcpServers": ["audio-agent"],
                "mcpServers": {"other": {"url": "https://example.invalid"}, "audio-agent": {
                    "command": "/old/python", "args": ["-m", "audio_agent", "mcp"],
                    "disabled": True, "autoApprove": [], "env": {"KEEP": "value"}}}}
    path.write_text(json.dumps(original))
    result = install(layout, "claude", scope="user")
    updated = json.loads(path.read_text())
    for key in ("account", "projects", "disabledMcpServers"):
        assert updated[key] == original[key]
    server = updated["mcpServers"]["audio-agent"]
    assert server["disabled"] and server["autoApprove"] == [] and server["env"] == {"KEEP": "value"}
    assert updated["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert "do-not-output" not in json.dumps(result)


@pytest.mark.parametrize("payload", [b'{"mcpServers":{},"mcpServers":{}}', b'{"x":NaN}', b'{"x":1e999}', b'[]', b'broken'])
def test_bad_json_rejected_without_write(layout, payload):
    config = layout["workspace"] / ".mcp.json"
    config.write_bytes(payload)
    with pytest.raises(InstallError):
        install(layout, "claude")
    assert config.read_bytes() == payload
    assert not (layout["workspace"] / ".claude").exists()


@pytest.mark.parametrize("payload", ['[mcp_servers]\naudio-agent = "wrong"', '[mcp_servers.audio-agent]\nurl = "https://example.invalid"', '[mcp_servers.audio-agent]\ncommand = "other"\nargs = ["serve"]', 'invalid = ['])
def test_conflicting_or_malformed_toml_no_write(layout, payload):
    config = layout["workspace"] / ".codex/config.toml"
    config.parent.mkdir(); config.write_text(payload)
    with pytest.raises(InstallError):
        install(layout)
    assert config.read_text() == payload
    assert not (layout["workspace"] / ".agents").exists()


def test_unknown_skill_never_overwritten(layout):
    target = layout["workspace"] / ".agents/skills/audio-agent"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user authored")
    with pytest.raises(InstallError, match="未知同名"):
        install(layout)
    assert (target / "SKILL.md").read_text() == "user authored"
    assert not (layout["workspace"] / ".codex").exists()


@pytest.mark.parametrize("change", ["edit", "add", "delete", "chmod"])
def test_modified_managed_skill_never_overwritten(layout, change):
    result = install(layout)
    skill = Path(result["skill_path"])
    if change == "edit": (skill / "SKILL.md").write_text("user edit")
    if change == "add": (skill / "user-notes.md").write_text("user addition")
    if change == "delete": (skill / "references/guide.md").unlink()
    if change == "chmod": (skill / "scripts/check.py").chmod(0o600)
    with pytest.raises(InstallError, match="被修改"):
        install(layout)


def test_managed_skill_update_keeps_backup(layout):
    first = install(layout)
    source = layout["project_root"] / "skills/audio-agent/references/guide.md"
    source.write_text("new version")
    second = install(layout)
    assert second["changed"] and len(second["backup_paths"]) == 1
    assert (Path(second["backup_paths"][0]) / "references/guide.md").read_text() == "完整参考中文\n"
    assert (Path(first["skill_path"]) / "references/guide.md").read_text() == "new version"
    assert install(layout)["status"] == "unchanged"


@pytest.mark.parametrize("target", ["config", "parent", "skill", "source"])
def test_symlinks_rejected_without_touching_destination(layout, target):
    outside = layout["home"] / "outside"
    outside.mkdir()
    (outside / "user.txt").write_text("keep")
    if target == "config":
        path = layout["workspace"] / ".mcp.json"
        path.symlink_to(outside / "user.txt")
    elif target == "parent":
        (layout["workspace"] / ".claude").symlink_to(outside)
    elif target == "skill":
        path = layout["workspace"] / ".claude/skills/audio-agent"
        path.parent.mkdir(parents=True); path.symlink_to(outside)
    else:
        (layout["project_root"] / "skills/audio-agent/link").symlink_to(outside / "user.txt")
    with pytest.raises(InstallError, match="符号链接"):
        install(layout, "claude")
    assert (outside / "user.txt").read_text() == "keep"


def test_config_write_failure_rolls_back_skill_update(layout, monkeypatch):
    first = install(layout)
    original_config = Path(first["config_path"]).read_bytes()
    original_skill = installer._tree(Path(first["skill_path"]))
    (layout["project_root"] / "skills/audio-agent/SKILL.md").write_text("updated")
    def fail(*args): raise OSError("disk full")
    monkeypatch.setattr(installer, "_replace_config", fail)
    with pytest.raises(InstallError, match="disk full"):
        install(layout, data_dir=layout["home"] / "other")
    assert Path(first["config_path"]).read_bytes() == original_config
    assert installer._tree(Path(first["skill_path"])) == original_skill


def test_first_config_write_failure_removes_only_new_skill(layout, monkeypatch):
    def fail(*args): raise OSError("read-only")
    monkeypatch.setattr(installer, "_replace_config", fail)
    with pytest.raises(InstallError): install(layout, "claude")
    assert not (layout["workspace"] / ".mcp.json").exists()
    assert not (layout["workspace"] / ".claude/skills/audio-agent").exists()


def test_concurrent_config_edit_is_not_lost(layout, monkeypatch):
    config = layout["workspace"] / ".mcp.json"
    old_rename = installer.os.rename
    def rename(source, target):
        old_rename(source, target)
        if Path(target).name == "audio-agent":
            config.write_text('{"external":"kept"}')
    monkeypatch.setattr(installer.os, "rename", rename)
    with pytest.raises(InstallError, match="其他进程"):
        install(layout, "claude")
    assert json.loads(config.read_text()) == {"external": "kept"}
    assert not (layout["workspace"] / ".claude/skills/audio-agent").exists()


@pytest.mark.parametrize("field", ["workspace", "data_dir"])
def test_relative_arguments_rejected(layout, field):
    with pytest.raises(InstallError, match="绝对路径"):
        install(layout, **{field: "relative"})


def test_script_uses_checkout_not_cwd_and_safe_cli_failure(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    # Only --help runs against the real source; it cannot install or open MCP.
    result = subprocess.run([sys.executable, str(repo / "scripts/install-agent-integration"), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "--workspace" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_cli_fixture_install_from_different_cwd(layout):
    repo = layout["project_root"]
    (repo / "audio_agent").mkdir()
    (repo / "audio_agent/__init__.py").touch()
    (repo / "audio_agent/agent_install.py").write_bytes(Path(installer.__file__).read_bytes())
    (repo / "scripts").mkdir()
    source_script = Path(__file__).resolve().parents[1] / "scripts/install-agent-integration"
    script = repo / "scripts/install-agent-integration"
    script.write_bytes(source_script.read_bytes())
    result = subprocess.run([sys.executable, str(script), "--client", "claude"], cwd=layout["workspace"],
                            env=os.environ | {"HOME": str(layout["home"])}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["project_dir"] == str(repo)
    assert Path(payload["config_path"]) == layout["workspace"] / ".mcp.json"


def test_updated_backup_and_staging_outside_skill_discovery(layout, monkeypatch):
    first = install(layout)
    skill = Path(first["skill_path"])
    (layout["project_root"] / "skills/audio-agent/SKILL.md").write_text("new version")
    original_rename = installer.os.rename
    def rename(source, target):
        if Path(source).name.startswith(".audio-agent-stage-"):
            assert Path(source).parent != skill.parent
        return original_rename(source, target)
    monkeypatch.setattr(installer.os, "rename", rename)
    result = install(layout)
    for backup in result["backup_paths"]:
        assert not Path(backup).is_relative_to(skill.parent)
    assert [p.parent for p in skill.parent.rglob("SKILL.md")] == [skill]


def test_busy_installer_fails_without_replacing_existing_files(layout):
    import fcntl
    result = install(layout)
    config = Path(result["config_path"])
    before = config.read_bytes()
    lock = config.parent / ".audio-agent-install.lock"
    with lock.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallError, match="另一个安装器"):
            install(layout, data_dir=layout["home"] / "different")
    assert config.read_bytes() == before


def test_failure_to_backup_does_not_replace_anything(layout, monkeypatch):
    result = install(layout)
    config = Path(result["config_path"])
    original = config.read_bytes()
    def fail(*args, **kwargs): raise OSError("backup denied")
    monkeypatch.setattr(installer, "_write_new", fail)
    with pytest.raises(InstallError, match="backup denied"):
        install(layout, data_dir=layout["home"] / "different")
    assert config.read_bytes() == original


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_preserved_environment_override_not_claimed_to_follow_settings(layout, client):
    first = install(layout, client)
    config = Path(first["config_path"])
    if client == "codex":
        with config.open("a") as handle:
            handle.write('\n[mcp_servers.audio-agent.env]\nAUDIO_AGENT_DATA_DIR = "/private-user-data"\n')
    else:
        doc = json.loads(config.read_text())
        doc["mcpServers"]["audio-agent"]["env"] = {"AUDIO_AGENT_DATA_DIR": "/private-user-data"}
        config.write_text(json.dumps(doc))
    result = install(layout, client)
    assert result["follows_storage_setting"] is False
    assert len(result["warnings"]) == 1
    assert "/private-user-data" not in json.dumps(result)
    fixed = install(layout, client, data_dir=layout["home"] / "explicit")
    assert not fixed["follows_storage_setting"] and fixed["warnings"] == []


def test_forwarded_codex_environment_reported_as_possible_override(layout):
    first = install(layout)
    config = Path(first["config_path"])
    with config.open("a") as handle:
        handle.write('\nenv_vars = ["AUDIO_AGENT_DATA_DIR"]\nenabled = false\n')
    result = install(layout)
    assert result["follows_storage_setting"] is False
    assert len(result["warnings"]) == 2


def test_object_form_forwarded_codex_environment_reported(layout):
    first = install(layout)
    config = Path(first["config_path"])
    with config.open("a") as handle:
        handle.write('\nenv_vars = [{name = "AUDIO_AGENT_DATA_DIR", source = "local"}]\n')
    result = install(layout)
    assert result["follows_storage_setting"] is False and len(result["warnings"]) == 1
    assert tomllib.loads(config.read_text())["mcp_servers"]["audio-agent"]["env_vars"] == [
        {"name": "AUDIO_AGENT_DATA_DIR", "source": "local"}]
