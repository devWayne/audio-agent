"""Bounded, explicit microphone + live-ASR HTTP verification. No cloud calls."""
import argparse
import http.client
import json
from pathlib import Path
import threading
import time

from audio_agent.web import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=8)
    parser.add_argument("--vad", action="store_true", help="Use the installed local Silero speech detector")
    parser.add_argument("--expect-segments", type=int, default=0,
                        help="Minimum expected sound events; zero permits a quiet-room test")
    args = parser.parse_args()
    if not 1 <= args.duration <= 20:
        parser.error("duration must be between 1 and 20 seconds")
    if args.expect_segments < 0:
        parser.error("expect-segments must be non-negative")
    server = create_server(args.data_dir.resolve(), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = None

    def request(method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
        try:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Audio-Agent-Token"] = token
            connection.request(method, path, body=json.dumps(payload) if payload is not None else None,
                               headers=headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            if response.status >= 400:
                raise RuntimeError(str(result))
            return result
        finally:
            connection.close()

    stopped = threading.Event()
    timer_errors = []

    def stop():
        try:
            request("POST", "/api/live/stop", {})
        except Exception as exc:
            timer_errors.append(str(exc))
        finally:
            stopped.set()

    timer = None
    try:
        token = request("GET", "/api/session")["token"]
        initial = request("GET", "/api/status")
        if initial["capture"].get("active"):
            raise RuntimeError("Verification requires an idle, separate audio library")
        request("POST", "/api/live/start", {"device": args.device, "language": "zh", "vad_enabled": args.vad})
        timer = threading.Timer(args.duration, stop)
        timer.start()
        deadline = time.monotonic() + args.duration + 120
        saw_recording = False
        saw_vad = False
        while time.monotonic() < deadline:
            live = request("GET", "/api/live")["live"]
            saw_recording |= live.get("state") == "recording"
            saw_vad |= live.get("vad_ready", False) and live.get("vad_engine") == "silero-v4"
            if stopped.is_set() and not live.get("active"):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Live transcription did not finish in time")
        final = request("GET", "/api/status")
        if timer_errors or live.get("last_error") or not saw_recording:
            raise RuntimeError(str(timer_errors or live.get("last_error") or "No PCM received"))
        if final["capture"].get("active") or live.get("active"):
            raise RuntimeError("Capture failed to stop cleanly")
        if live.get("clips_count", 0) < args.expect_segments:
            raise RuntimeError("Fewer sound events than requested were saved")
        if live.get("observed_seconds", 0) <= 0:
            raise RuntimeError("No microphone samples were observed")
        if args.vad and not saw_vad:
            raise RuntimeError("Silero VAD was not observed running")
        print(json.dumps({"microphone_tested": True, "capture_stopped": True,
                          "state": live["state"], "audio_seconds": live.get("audio_seconds"),
                          "observed_seconds": live.get("observed_seconds"),
                          "clips_count": live.get("clips_count"),
                          "transcribed_count": live.get("transcribed_count"),
                          "silent_count": live.get("silent_count"),
                          "vad_tested": args.vad, "vad_ready_observed": saw_vad,
                          "data_dir": str(args.data_dir.resolve())}, ensure_ascii=False, indent=2))
    finally:
        if timer:
            timer.cancel()
            stop()
            timer.join(timeout=20)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
