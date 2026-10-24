"""Opt-in end-to-end local model test through a real MCP stdio client."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from audio_agent.store import AudioStore


async def check(audio, root):
    store = AudioStore(root)
    clip = store.import_wav(audio, source="official-test-fixture")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "audio_agent", "--data-dir", str(root), "mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("audio_transcribe_clip", {"clip_id": clip["id"], "language": "en"})
            if result.isError:
                raise RuntimeError(str(result.content))
            transcript = result.structuredContent or json.loads(result.content[0].text)
            assert "country" in transcript["text"].lower(), transcript
            assert transcript["execution"] == "isolated-local-process"
            status = await session.call_tool("audio_status", {})
            assert not status.isError
            print(json.dumps({"transcript": transcript, "mcp_alive_after_asr": True}, ensure_ascii=False, indent=2))


p = argparse.ArgumentParser()
p.add_argument("audio", type=Path)
p.add_argument("--data-dir", type=Path, required=True)
args = p.parse_args()
asyncio.run(check(args.audio.resolve(), args.data_dir.resolve()))
