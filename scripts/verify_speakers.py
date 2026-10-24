#!/usr/bin/env python3
"""Opt-in CPU verification with the downloaded public four-speaker fixture.

No microphone, downloads, ASR, or user voiceprint registration. The embeddings
below describe public test speakers only and never enter voiceprint.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import wave

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from audio_agent.speakers import (SAMPLE_RATE, _audio_info, _exclusive_ranges,
                                  _model_paths, analyze_audio)


def save(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=PROJECT / ".data/test-fixtures/four-speakers-zh.wav")
    parser.add_argument("--single-fixture", type=Path, default=PROJECT / ".data/test-fixtures/jfk.wav")
    parser.add_argument("--output-dir", type=Path, default=PROJECT / ".data/speaker-smoke")
    parser.add_argument("--reuse-results", action="store_true", help="Reuse auto.json/four.json from a completed run of this fixture")
    args = parser.parse_args()
    frames, duration = _audio_info(args.fixture)
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cases = {}
    for name, count, source in (("auto", None, args.fixture), ("four", 4, args.fixture),
                                ("single-auto", None, args.single_fixture)):
        target = args.output_dir / f"{name}.json"
        _, source_duration = _audio_info(source)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if args.reuse_results:
            cases[name] = json.loads(target.read_text(encoding="utf-8"))
            if abs(cases[name]["duration_seconds"] - source_duration) > 1 / SAMPLE_RATE:
                raise ValueError("Saved analysis duration does not match the requested fixture")
            if cases[name].get("fixture_sha256", source_hash) != source_hash:
                raise ValueError("Saved analysis hash does not match the requested fixture")
        else:
            cases[name] = analyze_audio(source, num_speakers=count)
            cases[name]["fixture_sha256"] = source_hash
            save(target, cases[name])

    # Use disjoint halves of each speaker's non-overlapping voice. Comparing an
    # embedding against itself would not test the intended matching behavior.
    import numpy as np
    import sherpa_onnx
    with wave.open(str(args.fixture), "rb") as handle:
        raw = handle.readframes(frames)
    if len(raw) != frames * 2:
        raise ValueError("Fixture is truncated")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_model_paths()["embedding"]), num_threads=2, provider="cpu")
    if not config.validate():
        raise ValueError("Local embedding model is missing")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def vector(audio):
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=audio)
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise ValueError("Not enough speech for the embedding extractor")
        value = np.asarray(extractor.compute(stream), dtype=np.float64)
        norm = float(np.linalg.norm(value))
        if not np.isfinite(value).all() or norm <= 1e-12:
            raise ValueError("Invalid public-test embedding")
        return value / norm

    halves, skipped, windows = {}, [], []
    for person in cases["four"]["speakers"]:
        ranges = _exclusive_ranges(cases["four"]["turns"], person["id"], frames)
        selected = np.concatenate([samples[start:end] for start, end in ranges]) if ranges else np.array([], dtype=np.float32)
        middle = len(selected) // 2
        if middle < 2 * SAMPLE_RATE:
            skipped.append(person["id"])
            continue
        halves[person["id"]] = (vector(selected[:middle]), vector(selected[middle:]))
        windows.append({"speaker": person["id"], "first_seconds": middle / SAMPLE_RATE,
                        "second_seconds": (len(selected) - middle) / SAMPLE_RATE})
    comparisons = []
    for left, (first, _) in halves.items():
        for right, (_, second) in halves.items():
            comparisons.append({"first_half_speaker": left, "second_half_speaker": right,
                                "same_diarized_speaker": left == right,
                                "cosine": round(float(first @ second), 6)})
    summary = {"fixture": str(args.fixture.resolve()), "duration_seconds": duration,
               "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
               "reused_results": args.reuse_results,
               "public_test_only": True, "no_user_voiceprint_created": True,
               "cases": [{"name": name, "speaker_count": value["speaker_count"],
                          "elapsed_seconds": value["elapsed_seconds"], "turn_count": len(value["turns"]),
                          "cluster_threshold": value.get("cluster_threshold"),
                          "speakers": [{key: person[key] for key in ("id", "speech_seconds", "reference_seconds")}
                                       for person in value["speakers"]]}
                         for name, value in cases.items()],
               "disjoint_embedding_windows": windows, "cosine_comparisons": comparisons, "skipped_short_speakers": skipped,
               "notes": ["Cosine similarity is not an identity probability or a calibrated threshold.",
                         "The four-speaker assignments are model predictions; this smoke test is not a diarization error benchmark."]}
    summary["automatic_known_count_checks_passed"] = cases["auto"]["speaker_count"] == 4 and cases["single-auto"]["speaker_count"] == 1
    save(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["automatic_known_count_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
