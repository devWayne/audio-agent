"""Loopback-only web UI for explicit local WAV import, playback and ASR.

Read requests never open a microphone or load a model. Mutations require the
per-server session token; HTTP Origin and Host are checked before any route.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from .asr import discover_models
from .capture import read_capture_status, list_audio_devices
from .config import ffmpeg_path, PROJECT_ROOT
from .document import document_page
from .pipeline import asr_lock, transcribe_clip
from .store import AudioStore, assert_library_writable, LibraryMovedError
from .live import LiveManager, LiveSessionError
from .speaker_service import SpeakerManager
from .vad import vad_model_status
from .storage_config import resolve_storage_location, write_storage_config
from .storage_migration import prepared_library_copy, MigrationBusyError
from .mcp_info import mcp_configuration_info

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024
ASSETS_DIR = Path(__file__).resolve().parent / "web_assets"
_ID = re.compile(r"[0-9a-f]{32}\Z")


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, *, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.headers = headers or {}


def _valid_id(value: str) -> bool:
    return bool(_ID.fullmatch(value))


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _transcripts(store: AudioStore) -> list[dict]:
    """Read all retained results before search/pagination, ignoring stale files."""
    cutoff = time.time() - store.retention_hours * 3600
    results = []
    folder = store.root / "transcripts"
    if folder.is_symlink():
        raise APIError(500, "invalid_storage", "转写目录不能是符号链接。")
    for path in folder.glob("*.json"):
        if not _valid_id(path.stem) or path.is_symlink():
            continue
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff or stat.st_size > 32 * 1024 * 1024:
                continue
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict) or item.get("id") != path.stem:
                continue
            if not isinstance(item.get("text", ""), str):
                continue
            created_at = item.get("created_at", 0)
            if not isinstance(created_at, (float, int)) or isinstance(created_at, bool) or not math.isfinite(created_at):
                continue
            # Reject nonstandard NaN/Infinity anywhere in an untrusted JSON file.
            json.dumps(item, allow_nan=False)
            results.append(item)
        except (OSError, ValueError, UnicodeError):
            continue
    return sorted(results, key=lambda item: (float(item.get("created_at", 0)), item["id"]), reverse=True)


def _retained(clip: dict, store: AudioStore) -> bool:
    timestamp = clip["created_time"] if clip.get("import_sha256") else clip["end_time"]
    return timestamp > time.time() - store.retention_hours * 3600


def _clips(store: AudioStore) -> list[dict]:
    cutoff = time.time() - store.retention_hours * 3600
    with store._lock(exclusive=False), store._connect() as db:
        rows = db.execute("""SELECT * FROM clips
            WHERE (import_sha256 IS NULL AND end_time>?)
               OR (import_sha256 IS NOT NULL AND created_time>?)
            ORDER BY created_time DESC,start_time DESC,id DESC""", (cutoff, cutoff)).fetchall()
    return [dict(row) for row in rows]


def _display_clip(clip: dict, transcripts: list[dict]) -> dict:
    records = [item for item in transcripts if item.get("clip_id") == clip["id"]]
    name = clip.get("original_name")
    if not name:
        try:
            timestamp = datetime.fromtimestamp(clip["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            timestamp = clip["id"][:8]
        kind = "麦克风" if clip["source"].startswith(("avfoundation:", "live:")) else "录音"
        name = f"{kind} {timestamp}"
    filename = clip.get("original_name") or f"{clip['id']}.wav"
    latest = ({key: records[0].get(key) for key in ("id", "text", "created_at")} if records else None)
    # Absolute filesystem paths are not needed to operate the browser interface.
    return {key: value for key, value in clip.items() if key != "path"} | {
        "display_name": name, "label": name, "filename": filename,
        "transcribed": bool(records), "transcript_count": len(records),
        "latest_transcript": latest,
    }


def _document_revision(store: AudioStore, document_revision: str) -> str:
    """Fingerprint label dependencies without reading private voice vectors."""
    digest = hashlib.sha256(document_revision.encode("utf-8"))

    def include(path: Path, name: str):
        try:
            info = path.lstat()
            metadata = (name, info.st_mtime_ns, info.st_size, stat.S_IFMT(info.st_mode))
        except FileNotFoundError:
            metadata = (name, None)
        digest.update(json.dumps(metadata, separators=(",", ":")).encode("utf-8"))

    include(store.root / "voiceprint.json", "voiceprint")
    include(store.root / "speaker-profiles.json", "speaker-profiles")
    folder = store.root / "speakers"
    # Do not follow a substituted directory or any file symlink. Only public
    # revision hashes leave the server; filenames and metadata stay private.
    if folder.is_symlink():
        include(folder, "speaker-directory")
    else:
        for path in sorted(folder.glob("*.json")):
            if _valid_id(path.stem):
                include(path, path.stem)
    return digest.hexdigest()


class JobManager:
    """At most one requested inference, with bounded in-memory job history."""
    def __init__(self, store: AudioStore):
        self.store = store
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._closed = False
        self._thread: threading.Thread | None = None

    def items(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in reversed(self._jobs.values())]

    def get(self, job_id: str) -> dict:
        with self._lock:
            if job_id not in self._jobs:
                raise APIError(404, "job_not_found", "没有找到这个转写任务。")
            return dict(self._jobs[job_id])

    def submit(self, clip_id: str, language: str) -> dict:
        clip = self.store.get_clip(clip_id)
        if clip is None or not _retained(clip, self.store):
            raise APIError(404, "clip_not_found", "录音不存在或已超过保留期限。")
        with self._lock:
            if self._closed:
                raise APIError(503, "server_stopping", "本地服务正在关闭。")
            if any(job["status"] in ("queued", "running") for job in self._jobs.values()):
                raise APIError(409, "transcription_busy", "已有转写任务正在运行，请完成后重试。")
            try:
                # Detect another CLI/MCP worker without starting a model. The
                # actual pipeline takes this lock again for its entire job.
                with asr_lock(self.store.root):
                    pass
            except RuntimeError as exc:
                raise APIError(409, "transcription_busy", str(exc)) from exc
            job = {"id": uuid.uuid4().hex, "clip_id": clip_id, "language": language,
                   "status": "queued", "created_at": time.time(), "started_at": None,
                   "completed_at": None, "result": None, "error": None}
            self._jobs[job["id"]] = job
            while len(self._jobs) > 100:
                del self._jobs[next(iter(self._jobs))]
            result = dict(job)
            # Shutdown joins the explicit job instead of orphaning its native
            # inference subprocess. No pending inference queue is accumulated.
            self._thread = threading.Thread(target=self._run, args=(job["id"],),
                         name=f"web-asr-{job['id'][:8]}", daemon=False)
            self._thread.start()
        return result

    def close(self):
        with self._lock:
            self._closed = True
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def _run(self, job_id: str):
        with self._lock:
            job = self._jobs[job_id]
            job.update(status="running", started_at=time.time())
            clip_id, language = job["clip_id"], job["language"]
        try:
            result = transcribe_clip(self.store, clip_id, language=language)
        except Exception as exc:
            with self._lock:
                job.update(status="failed", completed_at=time.time(), error=str(exc))
        else:
            with self._lock:
                job.update(status="completed", completed_at=time.time(), result=result)


class AudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: AudioStore, *, storage_config_path=None):
        self._request_lock = threading.RLock()
        self.storage_config_path = storage_config_path
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.jobs = JobManager(store)
        self.speakers = SpeakerManager(store, asr_pending=lambda: self.live.asr_pending() or
                                       any(job["status"] in ("queued", "running") for job in self.jobs.items()))
        self.live = LiveManager(store, on_transcript=self.speakers.submit_automatic,
                                speaker_model_busy=self.speakers.model_busy)
        super().__init__(address, AudioRequestHandler)

    def server_close(self):
        with self._request_lock:
            super().server_close()
            self.live.close()
            self.jobs.close()
            self.speakers.close()

    def storage_settings(self):
        location = resolve_storage_location(self.store.root, config_path=self.storage_config_path)
        live = self.live.status()
        reasons = []
        if os.environ.get("AUDIO_AGENT_DATA_DIR"):
            reasons.append("当前使用 AUDIO_AGENT_DATA_DIR 固定目录，请移除此启动覆盖后再从界面切换。")
        try:
            assert_library_writable(self.store.root)
        except LibraryMovedError as exc:
            reasons.append(str(exc))
        if live.get("active"):
            reasons.append("请先停止监听。")
        if live.get("pending_count", 0) or any(job["status"] in ("queued", "running") for job in self.jobs.items()):
            reasons.append("请等待已录音频完成转写。")
        if any(job["status"] in ("queued", "running") for job in self.speakers.items()):
            reasons.append("请等待说话人分析完成。")
        capture = read_capture_status(self.store.root)
        if capture.get("active") and not live.get("active"):
            reasons.append("请先停止正在使用此资料库的命令行采集。")
        source = "saved" if location.saved_data_dir == self.store.root else "default" if location.default_data_dir == self.store.root else "legacy" if self.store.root == PROJECT_ROOT / ".data" else "custom"
        return {"current_dir": str(self.store.root), "default_dir": str(location.default_data_dir),
                "configured_dir": str(location.saved_data_dir) if location.saved_data_dir else None,
                "config_path": str(location.config_path), "source": source,
                "config_error": location.saved_config_error,
                "paths": {"recordings": str(self.store.root / "clips"), "transcripts": str(self.store.root / "transcripts")},
                "retention_hours": self.store.retention_hours, "can_change": not reasons, "busy_reasons": reasons}

    def change_storage(self, directory):
        current = self.storage_settings()
        if not current["can_change"]:
            raise APIError(409, "storage_busy", " ".join(current["busy_reasons"]))
        old_store, old_live, old_jobs, old_speakers = self.store, self.live, self.jobs, self.speakers
        new_live = new_jobs = new_speakers = None
        # The first use of the offered application-data default may precede
        # creation of its settings folder. Custom locations must have a parent.
        if Path(directory) == Path(current["default_dir"]):
            Path(directory).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with prepared_library_copy(old_store, directory) as prepared:
                new_store = AudioStore(prepared.path, retention_hours=old_store.retention_hours,
                                       ffmpeg_path=old_store.ffmpeg_path, min_free_bytes=old_store.min_free_bytes)
                new_jobs = JobManager(new_store)
                new_speakers = SpeakerManager(new_store, asr_pending=lambda: self.live.asr_pending() or
                                             any(job["status"] in ("queued", "running") for job in self.jobs.items()))
                new_live = LiveManager(new_store, on_transcript=new_speakers.submit_automatic,
                                       speaker_model_busy=new_speakers.model_busy)
                prepared.mark_committed()
                write_storage_config(prepared.path, config_path=self.storage_config_path)
                self.store, self.live, self.jobs, self.speakers = new_store, new_live, new_jobs, new_speakers
                copied = {"files": prepared.files, "bytes": prepared.bytes}
        except Exception:
            for manager in (new_live, new_jobs, new_speakers):
                if manager is not None:
                    manager.close()
            raise
        for manager in (old_live, old_jobs, old_speakers):
            manager.close()
        return {"storage": self.storage_settings(), "copied": copied, "previous_dir": str(old_store.root)}


class AudioRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AudioAgent"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        # Do not log filenames, query terms or transcript text to terminal logs.
        pass

    def _headers(self, status: int, *, content_type: str, length: int,
                 extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()

    def _json(self, status: int, result: dict, *, extra: dict | None = None):
        body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(status, content_type="application/json; charset=utf-8", length=len(body), extra=extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, exc: APIError):
        self.close_connection = True
        self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}},
                   extra={"Connection": "close", **exc.headers})

    def _validate_request(self):
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                raise ValueError
        except ValueError:
            raise APIError(403, "loopback_only", "此服务只允许本机访问。")
        host_header = self.headers.get("Host", "")
        if len(self.headers.get_all("Host", [])) != 1:
            raise APIError(403, "invalid_host", "请求的主机名不受支持。")
        try:
            target = urlsplit("http://" + host_header)
            if (not target.hostname or not _loopback(target.hostname) or target.username
                    or target.password or target.path or target.query or target.fragment
                    or (target.port or 80) != self.server.server_port):
                raise ValueError
        except ValueError:
            raise APIError(403, "invalid_host", "请求的主机名不受支持。")
        origin = self.headers.get("Origin")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                if (parsed.scheme != "http" or parsed.hostname != target.hostname
                        or (parsed.port or 80) != (target.port or 80) or parsed.username
                        or parsed.password or parsed.path or parsed.query or parsed.fragment):
                    raise ValueError
            except ValueError:
                raise APIError(403, "invalid_origin", "请求来源与当前本机页面不一致。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError(403, "invalid_origin", "不允许跨站请求本地录音。")
        if self.command not in ("GET", "HEAD"):
            token = self.headers.get("X-Audio-Agent-Token", "")
            if not hmac.compare_digest(token, self.server.token):
                raise APIError(403, "invalid_token", "本机会话已失效，请刷新页面后重试。")
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "unsupported_transfer", "请提供明确的请求内容长度。")

    def _route(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            raise APIError(400, "invalid_path", "不支持代理请求。")
        try:
            path = unquote(parsed.path, encoding="utf-8", errors="strict")
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
        except (UnicodeError, ValueError) as exc:
            raise APIError(400, "invalid_path", "请求路径或参数无效。") from exc
        if "\x00" in path:
            raise APIError(400, "invalid_path", "请求路径无效。")
        if any(len(values) != 1 for values in params.values()):
            code = "invalid_document_query" if path == "/api/document" else "invalid_parameter"
            raise APIError(400, code, "参数不能重复。")
        return path, {key: values[0] for key, values in params.items()}

    @staticmethod
    def _pagination(params: dict) -> tuple[int, int]:
        try:
            limit = int(params.get("limit", "50"))
            offset = int(params.get("offset", "0"))
            if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
                raise ValueError
            return limit, offset
        except ValueError:
            raise APIError(400, "invalid_pagination", "limit 必须为 1–200，offset 必须为非负整数。")

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        try:
            # A library switch cannot interleave with an HTTP write or leave
            # one response mixing records from two roots.
            with self.server._request_lock:
                self._validate_request()
                path, params = self._route()
                if self.command in ("GET", "HEAD"):
                    self._get(path, params)
                elif self.command == "POST":
                    self._post(path)
                else:
                    raise APIError(405, "method_not_allowed", "不支持这个请求方法。")
        except APIError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (ValueError, RuntimeError) as exc:
            self._error(APIError(400, "invalid_request", str(exc)))
        except OSError:
            self._error(APIError(500, "storage_error", "读取或写入本地文件失败，请检查空间和权限。"))
        except Exception:
            self._error(APIError(500, "internal_error", "本地请求未完成，请重试。"))

    def _get(self, path: str, params: dict):
        store = self.server.store
        if path == "/api/session":
            return self._json(200, {"token": self.server.token})
        if path == "/api/storage":
            return self._json(200, self.server.storage_settings())
        if path == "/api/mcp":
            return self._json(200, mcp_configuration_info(store, project_root=PROJECT_ROOT))
        if path == "/api/speakers/status":
            return self._json(200, self.server.speakers.status())
        if path == "/api/speaker-profiles":
            return self._json(200, self.server.speakers.profiles())
        if path == "/api/speaker-jobs":
            return self._json(200, {"items": self.server.speakers.items()})
        match = re.fullmatch(r"/api/clips/([0-9a-f]{32})/speakers", path)
        if match:
            return self._json(200, {"analysis": self.server.speakers.analysis(match.group(1))})
        if path == "/api/live/settings":
            return self._json(200, self.server.live.settings())
        if path == "/api/live":
            return self._json(200, {"live": self.server.live.status()})
        if path == "/api/devices":
            return self._json(200, {"items": list_audio_devices(ffmpeg_path=store.ffmpeg_path)})
        if path == "/api/status":
            clips = _clips(store)
            storage = store.status()
            storage.update(clip_count=len(clips), duration=sum(item["duration"] for item in clips),
                           size_bytes=sum(item["size_bytes"] for item in clips),
                           oldest_start_time=min((item["start_time"] for item in clips), default=None),
                           latest_end_time=max((item["end_time"] for item in clips), default=None))
            return self._json(200, {"storage": storage, "capture": read_capture_status(store.root),
                                    "models": discover_models(), "transcript_count": len(_transcripts(store)),
                                    "live": self.server.live.status(), "vad": vad_model_status()})
        if path == "/api/document":
            try:
                if set(params) - {"limit", "cursor", "source"}:
                    raise ValueError("文档查询只支持 limit、cursor 和 source。")
                limit = int(params.get("limit", "200"))
                result = document_page(store, limit=limit, cursor=params.get("cursor"), source=params.get("source"))
            except ValueError as exc:
                raise APIError(400, "invalid_document_query", str(exc)) from exc
            # Fingerprint before reading annotations: if a reference changes
            # mid-request, the next poll sees a newer revision and refreshes.
            result["revision"] = _document_revision(store, result["revision"])
            result["items"] = [self.server.speakers.annotate(item) for item in result["items"]]
            return self._json(200, result)
        if path == "/api/clips":
            limit, offset = self._pagination(params)
            state = params.get("status", "all")
            if state not in ("all", "transcribed", "pending"):
                raise APIError(400, "invalid_status", "录音状态无效。")
            query = params.get("q", "").strip().casefold()
            transcripts = _transcripts(store)
            clips = [_display_clip(clip, transcripts) for clip in _clips(store)]
            if state != "all":
                clips = [clip for clip in clips if clip["transcribed"] == (state == "transcribed")]
            if query:
                matches = {item.get("clip_id") for item in transcripts if query in item.get("text", "").casefold()}
                clips = [clip for clip in clips if clip["id"] in matches or query in " ".join(
                    str(clip.get(key, "")) for key in ("id", "source", "filename", "display_name")).casefold()]
            return self._json(200, {"items": clips[offset:offset + limit], "total": len(clips), "limit": limit, "offset": offset})
        if path == "/api/transcripts":
            limit, offset = self._pagination(params)
            items = _transcripts(store)
            clip_id, query = params.get("clip_id"), params.get("q", "").strip().casefold()
            if clip_id:
                if not _valid_id(clip_id):
                    raise APIError(400, "invalid_clip_id", "录音编号无效。")
                items = [item for item in items if item.get("clip_id") == clip_id]
            if query:
                items = [item for item in items if query in item.get("text", "").casefold()]
            return self._json(200, {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset})
        match = re.fullmatch(r"/api/transcripts/([0-9a-f]{32})", path)
        if match:
            result = next((item for item in _transcripts(store) if item["id"] == match.group(1)), None)
            if result is None:
                raise APIError(404, "transcript_not_found", "转写记录不存在或已超过保留期限。")
            return self._json(200, self.server.speakers.annotate(result))
        if path == "/api/jobs":
            return self._json(200, {"items": self.server.jobs.items()})
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
        if match:
            return self._json(200, {"job": self.server.jobs.get(match.group(1))})
        match = re.fullmatch(r"/api/clips/([0-9a-f]{32})/audio", path)
        if match:
            return self._audio(match.group(1))
        if path.startswith("/api/"):
            raise APIError(404, "not_found", "没有找到这个本地接口。")
        return self._static(path)

    def _audio(self, clip_id: str):
        store = self.server.store
        # An open file descriptor remains readable if a later prune unlinks the
        # clip. Holding the lock only until open keeps capture writes responsive.
        with store._lock(exclusive=False):
            clip = store.get_clip(clip_id)
            if clip is None or not _retained(clip, store):
                raise APIError(404, "clip_not_found", "录音不存在或已超过保留期限。")
            try:
                handle = open(clip["path"], "rb")
            except FileNotFoundError as exc:
                raise APIError(404, "clip_not_found", "录音文件已被清理。") from exc
        with handle:
            size = os.fstat(handle.fileno()).st_size
            first, last, status = 0, size - 1, 200
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                try:
                    if not match or not any(match.groups()):
                        raise ValueError
                    begin, finish = match.groups()
                    if begin:
                        first = int(begin)
                        last = min(int(finish), size - 1) if finish else size - 1
                    else:
                        suffix = int(finish)
                        if suffix <= 0:
                            raise ValueError
                        first, last = max(0, size - suffix), size - 1
                    if first >= size or first > last:
                        raise ValueError
                except ValueError:
                    raise APIError(416, "invalid_range", "请求的音频范围无效。", headers={"Content-Range": f"bytes */{size}"})
                status = 206
            extra = {"Accept-Ranges": "bytes"}
            if status == 206:
                extra["Content-Range"] = f"bytes {first}-{last}/{size}"
            length = last - first + 1
            self._headers(status, content_type="audio/wav", length=length, extra=extra)
            if self.command != "HEAD":
                handle.seek(first)
                remaining = length
                while remaining:
                    block = handle.read(min(65536, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)

    def _static(self, path: str):
        name = "index.html" if path == "/" else path.lstrip("/")
        if any(part in ("..", ".") or part.startswith(".") for part in Path(name).parts):
            raise APIError(404, "not_found", "没有找到这个页面。")
        target = (ASSETS_DIR / name).resolve()
        if ASSETS_DIR.resolve() not in target.parents or not target.is_file():
            raise APIError(404, "not_found", "没有找到这个页面。")
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() not in {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2"}:
            raise APIError(404, "not_found", "没有找到这个页面。")
        with target.open("rb") as handle:
            self._headers(200, content_type=mime + ("; charset=utf-8" if mime.startswith("text/") or target.suffix == ".js" else ""),
                          length=os.fstat(handle.fileno()).st_size)
            if self.command != "HEAD":
                while block := handle.read(65536):
                    self.wfile.write(block)

    def _body_length(self, maximum: int) -> int:
        values = self.headers.get_all("Content-Length", [])
        try:
            if len(values) != 1 or not re.fullmatch(r"\d+", values[0]):
                raise ValueError
            length = int(values[0])
        except ValueError:
            raise APIError(411, "length_required", "请提供有效的 Content-Length。")
        if length > maximum:
            raise APIError(413, "upload_too_large", f"文件大小不能超过 {maximum // (1024 * 1024)} MiB。")
        return length

    def _post(self, path: str):
        if path == "/api/storage":
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "content_type", "存储位置设置需要 JSON。")
            length = self._body_length(MAX_JSON_BYTES)
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "incomplete_body", "请求未完整接收。")
            try:
                body = json.loads(data)
                if not isinstance(body, dict) or set(body) != {"directory", "copy_existing"} or body["copy_existing"] is not True or not isinstance(body["directory"], str):
                    raise ValueError("请指定新目录，并明确选择复制现有资料。")
                return self._json(200, self.server.change_storage(body["directory"]))
            except MigrationBusyError as exc:
                raise APIError(409, "storage_busy", str(exc)) from exc
            except (ValueError, UnicodeError) as exc:
                raise APIError(400, "invalid_storage_settings", str(exc)) from exc
        if path in ("/api/speakers/analyze", "/api/speakers/settings", "/api/voiceprint/enroll", "/api/voiceprint/clear", "/api/voiceprint/samples/remove",
                    "/api/speaker-profiles/enroll", "/api/speaker-profiles/update", "/api/speaker-profiles/remove"):
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "content_type", "说话人请求需要 JSON。")
            length = self._body_length(MAX_JSON_BYTES)
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "incomplete_body", "请求未完整接收。")
            try:
                body = json.loads(data)
                if not isinstance(body, dict):
                    raise ValueError("请求必须为对象。")
                if path == "/api/speaker-profiles/enroll":
                    required = {"clip_id", "speaker_id", "analysis_id", "confirm", "name", "note"}
                    if not required <= set(body) or set(body) - required - {"replace_profile_id"}:
                        raise ValueError("请指定录音、说话人、分析版本、确认、人物名字和备注。")
                    return self._json(200, self.server.speakers.enroll_profile(**body))
                if path == "/api/speaker-profiles/update":
                    if set(body) != {"profile_id", "name", "note"}:
                        raise ValueError("请指定人物编号、名字和备注。")
                    return self._json(200, self.server.speakers.update_profile(**body))
                if path == "/api/speaker-profiles/remove":
                    if set(body) != {"profile_id"}:
                        raise ValueError("请指定要移除的人物编号。")
                    return self._json(200, self.server.speakers.remove_profile(body["profile_id"]))
                if path == "/api/speakers/settings":
                    if set(body) != {"automatic_enabled"}:
                        raise ValueError("仅接受 automatic_enabled 布尔设置。")
                    return self._json(200, self.server.speakers.update_settings(body["automatic_enabled"]))
                if path == "/api/speakers/analyze":
                    if set(body) - {"clip_id", "num_speakers"} or "clip_id" not in body:
                        raise ValueError("请指定录音及可选人数。")
                    return self._json(202, {"job": self.server.speakers.submit(body["clip_id"], body.get("num_speakers"))})
                if path == "/api/voiceprint/samples/remove":
                    if set(body) != {"sample_id"}:
                        raise ValueError("请指定要移除的本人参考样本编号。")
                    return self._json(200, {"voiceprint": self.server.speakers.remove_sample(body["sample_id"])})
                if path == "/api/voiceprint/enroll":
                    required = {"clip_id", "speaker_id", "analysis_id", "confirm"}
                    if not required <= set(body) or set(body) - required - {"mode"} or not isinstance(body["speaker_id"], str):
                        raise ValueError("请指定录音、说话人并确认本人参考语音。")
                    options = {"mode": body["mode"]} if "mode" in body else {}
                    return self._json(200, {"voiceprint": self.server.speakers.enroll(body["clip_id"], body["speaker_id"], body["confirm"], analysis_id=body["analysis_id"], **options)})
                if body:
                    raise ValueError("取消登记请求使用空对象。")
                return self._json(200, {"voiceprint": self.server.speakers.clear()})
            except (ValueError, UnicodeError) as exc:
                raise APIError(400, "invalid_speaker_request", str(exc)) from exc
            except RuntimeError as exc:
                raise APIError(409, "speaker_unavailable", str(exc)) from exc
        if path == "/api/live/settings":
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "content_type", "声音阈值设置需要 JSON。")
            length = self._body_length(MAX_JSON_BYTES)
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "incomplete_body", "请求未完整接收。")
            try:
                body = json.loads(data)
                if not isinstance(body, dict) or set(body) != {"trigger_db"}:
                    raise ValueError
                result = self.server.live.update_settings(body["trigger_db"])
            except LiveSessionError as exc:
                raise APIError(409, "live_busy", str(exc)) from exc
            except (ValueError, UnicodeError) as exc:
                raise APIError(400, "invalid_live_settings", "仅接受 trigger_db：-60 到 -18 之间的有限数值。") from exc
            return self._json(200, result)
        if path in ("/api/live/start", "/api/live/stop"):
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "content_type", "实时录音请求需要 JSON。")
            length = self._body_length(MAX_JSON_BYTES)
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "incomplete_body", "请求未完整接收。")
            try:
                body = json.loads(data)
                if not isinstance(body, dict):
                    raise ValueError
                if path.endswith("/start"):
                    if set(body) - {"device", "language", "vad_enabled"}:
                        raise ValueError
                    device, language = body.get("device"), body.get("language", "zh")
                    vad_enabled = body.get("vad_enabled", False)
                    if not isinstance(device, str) or not device.strip() or language not in ("zh", "auto", "en") or not isinstance(vad_enabled, bool):
                        raise ValueError
                elif body:
                    raise ValueError
            except (ValueError, UnicodeError) as exc:
                raise APIError(400, "invalid_live_request", "请选择明确的麦克风及 zh、auto 或 en 语言；停止请求使用空对象。") from exc
            try:
                if path.endswith("/start"):
                    if "vad_enabled" in body:
                        live = self.server.live.start(device, language=language, vad_enabled=vad_enabled)
                    else:
                        live = self.server.live.start(device, language=language)
                    return self._json(202, {"live": live})
                live = self.server.live.stop()
                return self._json(202 if live["active"] else 200, {"live": live})
            except LiveSessionError as exc:
                raise APIError(409, "live_busy", str(exc)) from exc
        if path == "/api/transcribe":
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "content_type", "转写请求需要 JSON。")
            length = self._body_length(MAX_JSON_BYTES)
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "incomplete_body", "请求未完整接收。")
            try:
                body = json.loads(data)
                if not isinstance(body, dict) or set(body) - {"clip_id", "language"}:
                    raise ValueError
                clip_id, language = body.get("clip_id"), body.get("language", "auto")
                if not isinstance(clip_id, str) or not _valid_id(clip_id) or language not in ("auto", "zh", "en"):
                    raise ValueError
            except (ValueError, UnicodeError) as exc:
                raise APIError(400, "invalid_transcription", "请提供有效的录音编号，以及 auto、zh 或 en 语言。") from exc
            return self._json(202, {"job": self.server.jobs.submit(clip_id, language)})
        if path == "/api/import":
            return self._import()
        raise APIError(404, "not_found", "没有找到这个本地接口。")

    def _import(self):
        if self.headers.get_content_type() not in ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "application/octet-stream"):
            raise APIError(415, "content_type", "请上传 WAV 音频文件。")
        length = self._body_length(MAX_UPLOAD_BYTES)
        if length < 12:
            raise APIError(400, "invalid_wav", "WAV 文件不完整。")
        try:
            filename = unquote(self.headers.get("X-Filename", "recording.wav"), encoding="utf-8", errors="strict")
            if (not filename or len(filename.encode("utf-8")) > 255 or any(ord(c) < 32 for c in filename)
                    or "/" in filename or "\\" in filename or not filename.lower().endswith(".wav")):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise APIError(400, "invalid_filename", "请使用不含路径的 WAV 文件名。") from exc
        store = self.server.store
        store.ensure_space(length * 3)
        temporary = store.root / "incoming" / f"web-{uuid.uuid4().hex}.wav"
        try:
            with store._new_private_file(temporary) as handle:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(65536, remaining))
                    if not block:
                        raise APIError(400, "incomplete_body", "上传中断，录音尚未导入。")
                    handle.write(block)
                    remaining -= len(block)
                handle.flush()
                os.fsync(handle.fileno())
            clip = store.import_wav(temporary, source="web-import", original_name=filename)
        finally:
            temporary.unlink(missing_ok=True)
        return self._json(201, {"clip": _display_clip(clip, _transcripts(store))})


def create_server(root: str | Path, host: str = "127.0.0.1", port: int = 8765, *, storage_config_path=None) -> AudioHTTPServer:
    """Create but do not run a local HTTP server; port=0 is useful for tests."""
    if not _loopback(host) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("The Audio Agent UI must bind a loopback address and a valid port")
    if host == "localhost":
        host = "127.0.0.1"
    try:
        local_ffmpeg = ffmpeg_path()
    except RuntimeError:
        # Read APIs and canonical WAV imports work without installing ffmpeg.
        local_ffmpeg = "ffmpeg"
    store = AudioStore(root, ffmpeg_path=local_ffmpeg)
    if ":" in host:
        class IPv6AudioHTTPServer(AudioHTTPServer):
            address_family = socket.AF_INET6
        return IPv6AudioHTTPServer((host, port), store, storage_config_path=storage_config_path)
    return AudioHTTPServer((host, port), store, storage_config_path=storage_config_path)
