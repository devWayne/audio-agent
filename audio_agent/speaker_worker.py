"""Disposable speaker-model process; result JSON never uses native stdout."""
import argparse
import json
import os
from pathlib import Path
import sys

from .speakers import DEFAULT_CLUSTER_THRESHOLD, _analyze_inprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--segmentation", type=Path, required=True)
    parser.add_argument("--embedding", type=Path, required=True)
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--cluster-threshold", type=float, default=DEFAULT_CLUSTER_THRESHOLD)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = _analyze_inprocess(args.audio, args.segmentation, args.embedding, args.num_speakers,
                                   args.threads, args.cluster_threshold)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, allow_nan=False)
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
