import asyncio
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest


@pytest.mark.asyncio
async def test_stdio_tools_and_no_implicit_microphone(tmp_path):
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "audio_agent", "--data-dir", str(tmp_path / "data"), "mcp"],
        cwd=str(Path(__file__).resolve().parents[1]))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "audio_status", "audio_list_clips", "audio_transcribe_recent",
                "audio_transcribe_clip", "audio_recent_transcripts", "audio_get_updates"}
            result = await session.call_tool("audio_status", {})
            assert not result.isError
            payload = result.structuredContent or json.loads(result.content[0].text)
            assert payload["storage"]["clip_count"] == 0
            assert payload["capture"]["active"] is False
            assert not (tmp_path / "data" / "capture.json").exists()
            invalid = await session.call_tool("audio_transcribe_recent", {"seconds": -1})
            assert invalid.isError
            missing = await session.call_tool("audio_transcribe_clip", {"clip_id": "missing"})
            assert missing.isError


def payload(result):
    assert not result.isError, result.content
    return result.structuredContent or json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_stdio_incremental_updates_long_poll_annotations_and_no_inference(tmp_path):
    from audio_agent.store import AudioStore
    from test_updates import put_transcript, put_analysis

    root = tmp_path / "updates"
    store = AudioStore(root)
    # Instrument the actual subprocess, so a side effect cannot hide behind a
    # mock in the parent process. The transport remains real MCP stdio.
    code = '''
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import sys
root = Path(sys.argv[1])
def forbidden(*args, **kwargs):
    (root / "unexpected-model-or-mic").write_text("unexpected side effect")
    raise AssertionError("Read-only incremental tool invoked audio/model code")
with ExitStack() as stack:
    for name in ("audio_agent.capture.AudioCapture.start", "audio_agent.pipeline.transcribe_clip",
                 "audio_agent.pipeline.transcribe_recent", "audio_agent.speakers.analyze_audio", "wave.open"):
        stack.enter_context(patch(name, side_effect=forbidden))
    from audio_agent.mcp_server import build_server
    build_server(root).run()
'''
    params = StdioServerParameters(command=sys.executable, args=["-c", code, str(root)],
                                   cwd=str(Path(__file__).resolve().parents[1]))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert "资料" in initialized.instructions
            tools = await session.list_tools()
            tool = next(tool for tool in tools.tools if tool.name == "audio_get_updates")
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.openWorldHint is False
            assert "wait_seconds" in tool.inputSchema["properties"]
            initial = payload(await session.call_tool("audio_get_updates", {}))
            assert initial["items"] == [] and initial["initial"]
            cursor = initial["next_cursor"]
            waiting = asyncio.create_task(session.call_tool("audio_get_updates", {"cursor": cursor, "wait_seconds": 2}))
            await asyncio.sleep(.05)
            # Async long-polling must not monopolize the MCP dispatcher.
            status = payload(await asyncio.wait_for(session.call_tool("audio_status", {}), timeout=1))
            assert status["capture"]["active"] is False
            clip = store.add_pcm(bytes(6 * 32000), start_time=time.time()-10, source="test:synthetic")
            text = "这是一段完整文字。" * 1000
            record, _ = put_transcript(store, clip_id=clip["id"], text=text,
                                       segments=[{"start": 0, "end": 6, "text": text}])
            fresh = payload(await waiting)
            assert fresh["items"][0]["text"] == text
            assert fresh["items"][0]["segments"][0]["text"] == text
            assert fresh["items"][0]["id"] == record["id"] and not fresh["timed_out"]
            # Another client can independently replay the same cursor.
            replay = payload(await session.call_tool("audio_get_updates", {"cursor": cursor}))
            assert replay == fresh
            put_analysis(store, clip, two=True)
            labels = payload(await session.call_tool("audio_get_updates", {"cursor": fresh["next_cursor"]}))
            assert labels["items"][0]["document_key"] == fresh["items"][0]["document_key"]
            assert labels["items"][0]["segments"][0]["speaker_labels"] == ["说话人 1", "说话人 2"]
            assert "embedding" not in json.dumps(labels)
            started = time.monotonic()
            timeout = payload(await session.call_tool("audio_get_updates", {"cursor": labels["next_cursor"], "wait_seconds": .15}))
            assert .12 <= time.monotonic()-started < 1.5
            assert timeout["items"] == [] and timeout["timed_out"]
            assert timeout["next_cursor"] == labels["next_cursor"]
            for arguments in ({"cursor": "not-a-cursor"}, {"limit": 0}, {"wait_seconds": 31},
                              {"limit": True}, {"wait_seconds": True}):
                invalid = await session.call_tool("audio_get_updates", arguments)
                assert invalid.isError
            assert not (root / "capture.json").exists()
            assert not (root / "unexpected-model-or-mic").exists()
            assert not (root / "voiceprint.json").exists()


def test_generated_config_uses_environment_python(tmp_path):
    import subprocess
    result = subprocess.run([sys.executable, "-m", "audio_agent", "--data-dir", str(tmp_path),
                             "mcp-config", "--client", "claude"], capture_output=True, text=True, check=True)
    config = json.loads(result.stdout)["mcpServers"]["audio-agent"]
    assert config["command"] == str(Path(sys.executable).absolute())
    assert config["args"][-1] == "mcp"
