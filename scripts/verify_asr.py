"""Transcribe a provided local fixture and save a benchmark result."""
import argparse
import json
from pathlib import Path

from audio_agent.asr import transcribe

p = argparse.ArgumentParser()
p.add_argument("audio", type=Path)
p.add_argument("--language", default="en")
p.add_argument("--output", type=Path, required=True)
args = p.parse_args()
result = transcribe(args.audio, language=args.language)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False))
