"""Storage selection/config writes use only isolated temporary paths."""
import json
import os
from pathlib import Path
import stat
from unittest import mock

import pytest

from audio_agent.storage_config import (
    MAX_CONFIG_BYTES, StorageConfigError, default_data_dir, read_saved_data_dir,
    resolve_storage_location, storage_config_path, write_storage_config,
)


@pytest.fixture
def options(tmp_path):
    return {"home": tmp_path / "home", "env": {}, "platform": "darwin",
            "project_root": tmp_path / "project"}


def config_options(options):
    return {key: value for key, value in options.items() if key != "project_root"}


def make_legacy(options):
    directory = options["project_root"] / ".data"
    directory.mkdir(parents=True)
    (directory / "audio.sqlite3").write_bytes(b"Existing library: resolver must not open it")
    return directory


def write_raw(options, raw):
    path = storage_config_path(**config_options(options))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    return path


def test_mac_paths_and_default_read_do_not_create_anything(options):
    before = list(options["home"].parent.rglob("*"))
    result = resolve_storage_location(**options)
    base = options["home"] / "Library" / "Application Support" / "Audio Agent"
    assert result.data_dir == options["project_root"] / ".data"
    assert result.default_data_dir == options["project_root"] / ".data"
    assert result.config_path == base / "storage.json"
    assert result.source == "default" and result.saved_data_dir is None
    assert read_saved_data_dir(**config_options(options)) is None
    assert list(options["home"].parent.rglob("*")) == before
    assert json.loads(json.dumps(result.as_dict()))["data_dir"] == str(options["project_root"] / ".data")


def test_precedence_and_saved_metadata(options, tmp_path):
    legacy = make_legacy(options)
    assert resolve_storage_location(**options).data_dir == legacy
    assert resolve_storage_location(**options).source == "legacy"
    saved = tmp_path / "saved"
    write_storage_config(saved, **config_options(options))
    selected = resolve_storage_location(**options)
    assert selected.source == "saved" and selected.data_dir == saved
    environment = options | {"env": {"AUDIO_AGENT_DATA_DIR": str(tmp_path / "environment")}}
    selected = resolve_storage_location(**environment)
    assert selected.source == "environment" and selected.data_dir == tmp_path / "environment"
    assert selected.saved_data_dir == saved
    selected = resolve_storage_location(tmp_path / "explicit", **environment)
    assert selected.source == "explicit" and selected.data_dir == tmp_path / "explicit"
    assert selected.saved_data_dir == saved
    assert not saved.exists() and not selected.data_dir.exists()
    assert (legacy / "audio.sqlite3").read_bytes().startswith(b"Existing library")


def test_legacy_requires_database_not_just_a_dot_data_directory(options):
    (options["project_root"] / ".data").mkdir(parents=True)
    assert resolve_storage_location(**options).source == "default"


@pytest.mark.parametrize("source", ["explicit", "environment", "saved"])
@pytest.mark.parametrize("occupied", ["file", "symlink-loop"])
def test_unselected_default_cannot_block_higher_priority_library(options, tmp_path, source, occupied):
    default = default_data_dir(**options)
    default.parent.mkdir(parents=True)
    if occupied == "file":
        default.write_bytes(b"unselected file must remain untouched")
    else:
        default.symlink_to(default)
    target = tmp_path / "working-library"
    kwargs = options
    explicit = None
    if source == "explicit":
        explicit = target
    elif source == "environment":
        kwargs = options | {"env": {"AUDIO_AGENT_DATA_DIR": str(target)}}
    elif source == "saved":
        write_storage_config(target, **config_options(options))
    selected = resolve_storage_location(explicit, **kwargs)
    assert selected.source == source and selected.data_dir == target
    assert selected.default_data_dir == default
    if occupied == "file":
        assert default.read_bytes() == b"unselected file must remain untouched"
    else:
        assert default.is_symlink()


def test_invalid_default_still_errors_when_selected(options):
    default = default_data_dir(**options)
    default.parent.mkdir(parents=True)
    default.write_bytes(b"preserve")
    with pytest.raises(StorageConfigError, match="目录"):
        resolve_storage_location(**options)
    assert default.read_bytes() == b"preserve"


def test_unknown_user_expansion_has_a_configuration_error(options):
    with mock.patch.object(Path, "expanduser", side_effect=RuntimeError("unknown user")):
        with pytest.raises(StorageConfigError, match="解析"):
            resolve_storage_location("~unknown-user/library", **options)


def test_cli_environment_relative_paths_and_user_expansion(options, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_storage_location(".data", **options).data_dir == tmp_path / ".data"
    assert resolve_storage_location("~/Audio Notes", **options).data_dir == options["home"] / "Audio Notes"
    environment = options | {"env": {"AUDIO_AGENT_DATA_DIR": "relative/library"}}
    assert resolve_storage_location(**environment).data_dir == tmp_path / "relative/library"
    environment["env"]["AUDIO_AGENT_DATA_DIR"] = "~/Library Notes"
    assert resolve_storage_location(**environment).data_dir == options["home"] / "Library Notes"


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"{}", b'{"data_dir":null}',
    b'{"data_dir":true}', b'{"data_dir":123}', b'{"data_dir":"relative"}',
    b'{"data_dir":"~/relative"}', b'{"data_dir":""}', b'{"data_dir":"/a","token":"hidden"}',
    b'{"data_dir":"/a","data_dir":"/b"}', b"\xff", b" " * (MAX_CONFIG_BYTES + 1)])
def test_bad_saved_configuration_never_silently_falls_back(options, raw):
    make_legacy(options)
    path = write_raw(options, raw)
    before = path.read_bytes()
    with pytest.raises(StorageConfigError):
        resolve_storage_location(**options)
    assert path.read_bytes() == before


@pytest.mark.parametrize("source", ["explicit", "environment"])
def test_overrides_recover_from_broken_configuration_without_rewriting_it(options, tmp_path, source):
    path = write_raw(options, b"broken settings")
    target = tmp_path / "known-library"
    selected = (resolve_storage_location(target, **options) if source == "explicit" else
                resolve_storage_location(**(options | {"env": {"AUDIO_AGENT_DATA_DIR": str(target)}})))
    assert selected.data_dir == target and selected.source == source
    assert selected.saved_data_dir is None and selected.saved_config_error
    assert path.read_bytes() == b"broken settings" and not target.exists()


def test_write_is_one_field_private_and_does_not_create_or_modify_data(options, tmp_path):
    selected = tmp_path / "声音资料 与备注"
    selected.mkdir()
    protected = selected / "recording.wav"
    protected.write_bytes(b"original unchanged")
    result = write_storage_config(selected, **config_options(options))
    path = storage_config_path(**config_options(options))
    assert result == selected
    assert json.loads(path.read_text()) == {"data_dir": str(selected)}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert protected.read_bytes() == b"original unchanged"
    assert list(path.parent.glob(".storage-*.tmp")) == []
    next_directory = tmp_path / "not-created"
    write_storage_config(next_directory, **config_options(options))
    assert not next_directory.exists()
    assert read_saved_data_dir(**config_options(options)) == next_directory


@pytest.mark.parametrize("value", [None, True, 5, "", "relative", "~/Library", "/tmp/\x00bad"])
def test_saved_location_must_be_an_absolute_directory_before_any_write(options, value):
    with pytest.raises(StorageConfigError):
        write_storage_config(value, **config_options(options))
    assert not options["home"].exists()


def test_existing_regular_file_is_not_a_data_directory(options, tmp_path):
    target = tmp_path / "file"
    target.write_bytes(b"preserve")
    with pytest.raises(StorageConfigError, match="目录"):
        write_storage_config(target, **config_options(options))
    with pytest.raises(StorageConfigError, match="目录"):
        resolve_storage_location(target, **options)
    assert target.read_bytes() == b"preserve"


def test_atomic_replace_failure_preserves_old_config_and_removes_temporary(options, tmp_path):
    write_storage_config(tmp_path / "first", **config_options(options))
    path = storage_config_path(**config_options(options))
    previous = path.read_bytes()
    with mock.patch("audio_agent.storage_config.os.replace", side_effect=OSError("simulated failure")):
        with pytest.raises(StorageConfigError, match="保存"):
            write_storage_config(tmp_path / "second", **config_options(options))
    assert path.read_bytes() == previous
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".storage-*.tmp")) == []
    assert not (tmp_path / "second").exists()


def test_explicit_save_can_repair_malformed_regular_config(options, tmp_path):
    write_raw(options, b"malformed")
    write_storage_config(tmp_path / "recovered", **config_options(options))
    assert resolve_storage_location(**options).data_dir == tmp_path / "recovered"


def test_config_symlink_is_not_followed_or_overwritten(options, tmp_path):
    destination = tmp_path / "another-file"
    destination.write_text(json.dumps({"data_dir": str(tmp_path / "elsewhere")}))
    path = storage_config_path(**config_options(options))
    path.parent.mkdir(parents=True)
    path.symlink_to(destination)
    with pytest.raises(StorageConfigError):
        read_saved_data_dir(**config_options(options))
    with pytest.raises(StorageConfigError, match="符号链接"):
        write_storage_config(tmp_path / "next", **config_options(options))
    assert path.is_symlink()
    assert json.loads(destination.read_text())["data_dir"] == str(tmp_path / "elsewhere")


def test_directory_and_fifo_configs_are_rejected_without_blocking(options):
    path = storage_config_path(**config_options(options))
    path.mkdir(parents=True)
    with pytest.raises(StorageConfigError):
        read_saved_data_dir(**config_options(options))
    path.rmdir()
    os.mkfifo(path)
    with pytest.raises(StorageConfigError):
        read_saved_data_dir(**config_options(options))


def test_explicit_config_path_and_linux_xdg_are_isolated(tmp_path):
    home = tmp_path / "home"
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}
    assert storage_config_path(home=home, env=env, platform="linux") == tmp_path / "config/audio-agent/storage.json"
    assert default_data_dir(project_root=tmp_path / "project", home=home, env=env, platform="linux") == tmp_path / "project/.data"
    assert storage_config_path(home=home, env={}, platform="linux") == home / ".config/audio-agent/storage.json"
    path = tmp_path / "isolated/storage.json"
    write_storage_config(tmp_path / "library", config_path=path, home=home, env={}, platform="linux")
    selected = resolve_storage_location(config_path=path, home=home, env={}, platform="linux", project_root=tmp_path / "project")
    assert selected.config_path == path and selected.data_dir == tmp_path / "library"
    assert not home.exists()


def test_home_environment_can_be_monkeypatched_without_real_user_access(tmp_path, monkeypatch):
    home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AUDIO_AGENT_DATA_DIR", raising=False)
    selected = resolve_storage_location(platform="darwin", project_root=tmp_path / "project")
    assert selected.config_path == home / "Library/Application Support/Audio Agent/storage.json"
    assert selected.source == "default" and not home.exists()


def test_relative_config_and_xdg_paths_are_rejected(tmp_path):
    with pytest.raises(StorageConfigError, match="绝对"):
        read_saved_data_dir("relative/storage.json")
    with pytest.raises(StorageConfigError, match="绝对"):
        storage_config_path(home=tmp_path, env={"XDG_CONFIG_HOME": "relative"}, platform="linux")
