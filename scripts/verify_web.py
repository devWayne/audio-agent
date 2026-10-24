"""Opt-in real HTTP verification; defaults to disposable synthetic audio.

No microphone is opened. Real local ASR runs only with --transcribe --audio.
The server, uploaded fixtures and transcripts are removed when the check ends.
"""
from __future__ import annotations

import argparse
import http.client
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from urllib.parse import quote
import wave

from audio_agent.pipeline import _persist
from audio_agent.store import AudioStore
from audio_agent.web import create_server


def synthetic_wav(value: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(struct.pack("<h", value) * 8000)
    return output.getvalue()


class HTTPCheck:
    def __init__(self, port: int):
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.token = None
        self.completed = []

    def request(self, method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            content_type = response.getheader("Content-Type", "")
            payload = json.loads(data) if "application/json" in content_type else data
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def expect(self, label, condition, detail=None):
        if not condition:
            raise AssertionError(f"{label}: {detail!r}")
        self.completed.append(label)

    def upload(self, data, name, *, token=True, origin=None):
        headers = {"Content-Type": "audio/wav", "X-Filename": quote(name)}
        if token:
            headers["X-Audio-Agent-Token"] = self.token
        if origin is not None:
            headers["Origin"] = origin
        return self.request("POST", "/api/import", body=data, headers=headers)


def verify(root: Path, *, audio=None, transcribe=False, expect_text=None, timeout=300):
    server = create_server(root, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, name="verify-web-http", daemon=True)
    thread.start()
    check = HTTPCheck(server.server_address[1])
    try:
        status, _, page = check.request("GET", "/")
        check.expect("page serves locally", status == 200 and b"<html" in page.lower())
        status, _, session = check.request("GET", "/api/session")
        check.expect("session token issued", status == 200 and isinstance(session.get("token"), str))
        check.token = session["token"]
        status, _, initial = check.request("GET", "/api/status")
        check.expect("web does not start microphone", status == 200 and not initial["capture"]["active"])

        first_data = audio.read_bytes() if audio else synthetic_wav(1000)
        first_name = audio.name if audio else "HTTP 验证 alpha.wav"
        status, _, first = check.upload(first_data, first_name, origin=check.origin)
        check.expect("WAV import succeeds", status == 201 and "clip" in first, first)
        clip_id = first["clip"]["id"]
        status, _, second = check.upload(synthetic_wav(2000), "HTTP 验证 beta.wav")
        check.expect("second WAV import succeeds", status == 201 and "clip" in second, second)
        status, _, listing = check.request("GET", "/api/clips?limit=1&offset=0")
        status2, _, next_page = check.request("GET", "/api/clips?limit=1&offset=1")
        check.expect("recording pagination", status == status2 == 200 and listing["total"] == 2
                     and len(listing["items"]) == len(next_page["items"]) == 1
                     and listing["items"][0]["id"] != next_page["items"][0]["id"])

        status, _, playback = check.request("GET", f"/api/clips/{clip_id}/audio")
        check.expect("recording playback returns WAV", status == 200 and playback[:4] == b"RIFF"
                     and playback[8:12] == b"WAVE")
        status, headers, partial = check.request("GET", f"/api/clips/{clip_id}/audio",
                                                 headers={"Range": "bytes=0-11"})
        check.expect("audio range for seeking", status == 206 and partial == playback[:12]
                     and headers.get("Content-Range", "").startswith("bytes 0-11/"))

        # Explicitly synthetic transcript fixture, never presented as ASR output.
        fixture = _persist(AudioStore(root), {"clip_id": clip_id, "text": "HTTP smoke searchable fixture",
                          "source": "verification-fixture", "backend": "verification-fixture", "segments": []})
        status, _, transcripts = check.request("GET", "/api/transcripts?q=searchable")
        check.expect("transcript search", status == 200 and any(item["id"] == fixture["id"]
                                                                for item in transcripts["items"]))
        status, _, detail = check.request("GET", f"/api/transcripts/{fixture['id']}")
        check.expect("transcript detail", status == 200 and detail["text"] == fixture["text"])

        status, _, _ = check.upload(synthetic_wav(3000), "blocked.wav", token=False)
        check.expect("mutation needs session token", status == 403)
        status, _, _ = check.upload(synthetic_wav(3000), "blocked.wav", origin="https://example.invalid")
        check.expect("cross-origin mutation blocked", status == 403)
        status, _, _ = check.request("GET", "/api/clips", headers={"Origin": "https://example.invalid"})
        check.expect("cross-origin reads blocked", status == 403)
        status, _, _ = check.request("GET", "/api/session", headers={"Host": f"example.invalid:{check.port}"})
        check.expect("foreign Host blocked", status in (400, 403))
        status, _, _ = check.request("GET", "/api/clips/../../etc/passwd/audio")
        check.expect("arbitrary path rejected", status in (400, 404))
        status, _, _ = check.upload(b"this is not a WAV", "bad.wav")
        check.expect("invalid upload rejected", status == 400)
        check.expect("upload staging cleaned", not any((root / "incoming").iterdir()))

        if transcribe:
            status, _, response = check.request("POST", "/api/transcribe",
                body=json.dumps({"clip_id": clip_id, "language": "auto"}),
                headers={"Content-Type": "application/json", "X-Audio-Agent-Token": check.token,
                         "Origin": check.origin})
            check.expect("on-demand local ASR accepted", status == 202, response)
            job_id = response["job"]["id"]
            deadline = time.monotonic() + timeout
            while True:
                status, _, response = check.request("GET", f"/api/jobs/{job_id}")
                check.expect("ASR job readable", status == 200, response)
                job = response["job"]
                if job["status"] in ("completed", "failed", "error", "succeeded"):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"ASR did not finish within {timeout} seconds")
                time.sleep(0.5)
            check.expect("ASR completes", job["status"] in ("completed", "succeeded"), job)
            if expect_text:
                check.expect("expected speech recognized", expect_text.casefold() in job["result"]["text"].casefold())

        status, _, final = check.request("GET", "/api/status")
        check.expect("microphone remains stopped", status == 200 and not final["capture"]["active"])
        return {"passed": list(dict.fromkeys(check.completed)), "real_http": True,
                "local_asr_tested": transcribe, "microphone_opened": False,
                "data": "disposable temporary fixtures"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, help="Optional existing WAV; source stays unchanged")
    parser.add_argument("--transcribe", action="store_true", help="Explicitly run the existing local ASR model")
    parser.add_argument("--expect-text", help="Optional expected speech substring for ASR")
    parser.add_argument("--timeout", type=float, default=300, help="ASR completion timeout in seconds")
    args = parser.parse_args()
    if args.transcribe and not args.audio:
        parser.error("--transcribe requires --audio with a speech WAV")
    if args.expect_text and not args.transcribe:
        parser.error("--expect-text requires --transcribe")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="audio-agent-web-smoke-") as temporary:
        result = verify(Path(temporary), audio=args.audio.resolve() if args.audio else None,
                        transcribe=args.transcribe, expect_text=args.expect_text, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
