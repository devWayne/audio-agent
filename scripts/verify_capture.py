"""Bounded, opt-in microphone lifecycle check. Never run as a unit test."""
import argparse
import json
from pathlib import Path
import time

from audio_agent.cli import start_background, stop_background
from audio_agent.store import AudioStore

p = argparse.ArgumentParser()
p.add_argument("--device", required=True)
p.add_argument("--data-dir", type=Path, required=True)
args = p.parse_args()
root = args.data_dir.resolve()
started = None
try:
    started = start_background(root, args.device, chunk_seconds=1)
    time.sleep(3)
finally:
    if started is not None:
        stopped = stop_background(root)
if not started or stopped.get("active"):
    raise RuntimeError("Capture did not start/stop as expected")
store = AudioStore(root)
status = store.status()
if status["clip_count"] < 1:
    raise RuntimeError("No audio clips were saved")
print(json.dumps({"started": started, "stopped": stopped, "storage": status}, ensure_ascii=False, indent=2))
