"""Native inference worker; stdout is never used for the result protocol."""
import argparse
import json
from pathlib import Path
import sys

from .asr import _transcribe_inprocess, ASRError


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audio", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--language", default="auto")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        result = _transcribe_inprocess(args.audio, args.model, args.language)
        with args.output.open("x", encoding="utf-8") as handle:
            args.output.chmod(0o600)
            json.dump(result, handle, ensure_ascii=False)
        return 0
    except (ASRError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
