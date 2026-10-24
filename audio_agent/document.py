"""Read-only chronological transcript document with snapshot/keyset pagination.

Each clip contributes its latest retained transcription at the snapshot cutoff.
Export/range transcriptions retain their own identity. No audio is read, no model
is invoked, and no files are written. Cursors belong to this process and query;
after a server restart the caller should request the first page again.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time


_CURSOR_KEY = secrets.token_bytes(32)
_MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _finite_json(value):
    if isinstance(value, dict):
        return all(_finite_json(child) for child in value.values())
    if isinstance(value, list):
        return all(_finite_json(child) for child in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number(value)
    return True


def _store_key(store):
    return hashlib.sha256(str(store.root).encode()).hexdigest()


def _encode_cursor(payload):
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    signed = data + hmac.digest(_CURSOR_KEY, data, "sha256")
    return base64.urlsafe_b64encode(signed).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor, store, limit, source, now):
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError()
        signed = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        data, signature = signed[:-32], signed[-32:]
        if not hmac.compare_digest(signature, hmac.digest(_CURSOR_KEY, data, "sha256")):
            raise ValueError()
        value = json.loads(data)
        if (not isinstance(value, dict) or set(value) != {"v", "store", "limit", "source", "snapshot", "after"}
                or value["v"] != 1 or value["store"] != _store_key(store)
                or value["limit"] != limit or value["source"] != source
                or not _number(value["snapshot"]) or not 0 < value["snapshot"] <= now + 1
                or not isinstance(value["after"], list) or len(value["after"]) != 2
                or not _number(value["after"][0]) or not isinstance(value["after"][1], str)
                or not _ID.fullmatch(value["after"][1])):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("文档分页游标无效或查询条件已改变，请重新加载第一页。") from exc


def _transcripts(store, snapshot, cutoff):
    folder = store.root / "transcripts"
    if folder.is_symlink():
        raise ValueError("转写目录不能是符号链接。")
    records = []
    for path in folder.glob("*.json"):
        if not _ID.fullmatch(path.stem):
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_TRANSCRIPT_BYTES
                        or info.st_mtime < cutoff or info.st_mtime > snapshot):
                    continue
                raw = handle.read(_MAX_TRANSCRIPT_BYTES + 1)
                if len(raw) > _MAX_TRANSCRIPT_BYTES:
                    continue
                item = json.loads(raw)
            if (not isinstance(item, dict) or item.get("id") != path.stem
                    or not isinstance(item.get("text", ""), str)
                    or not isinstance(item.get("segments", []), list)
                    or not _number(item.get("created_at", 0)) or item.get("created_at", 0) > snapshot
                    or not _finite_json(item)):
                continue
            clip_id = item.get("clip_id")
            if clip_id is not None and (not isinstance(clip_id, str) or not _ID.fullmatch(clip_id)):
                continue
            if any(not isinstance(segment, dict) for segment in item.get("segments", [])):
                continue
            records.append((item, info.st_mtime_ns, info.st_size))
        except (OSError, ValueError, UnicodeError, OverflowError, RecursionError):
            continue
    return records


def _clip_metadata(store):
    # A metadata read only; do not open WAVs or trigger retention cleanup.
    with store._lock(exclusive=False), store._connect() as db:
        return {row["id"]: dict(row) for row in db.execute("SELECT * FROM clips")}


def _times(item, clip):
    audio = item.get("audio") if isinstance(item.get("audio"), dict) else {}
    for values in (clip or {}, audio, item):
        start, end = values.get("start_time"), values.get("end_time")
        if _number(start) and _number(end) and end >= start:
            return float(start), float(end)
    created = float(item.get("created_at", 0))
    return created, created


def _render(item, clip, cutoff):
    audio = item.get("audio") if isinstance(item.get("audio"), dict) else {}
    start, end = _times(item, clip)
    source = (clip or {}).get("source") or item.get("source") or audio.get("source")
    if not isinstance(source, str):
        source = None
    clip_id = item.get("clip_id")
    if source and source.startswith("live:"):
        kind = "live"
    elif source and source.startswith("avfoundation:"):
        kind = "microphone"
    elif clip_id and (source == "import" or source and source.startswith(("import:", "watch:"))):
        kind = "import"
    elif not clip_id:
        kind = "recent"
    else:
        kind = "recording"
    available = False
    if clip:
        retention_time = clip["created_time"] if clip.get("import_sha256") else clip["end_time"]
        relative = Path(clip["path"])
        if relative.is_absolute():
            audio_path = relative
        else:
            audio_path = Path(clip["_root"]) / relative
        # stat metadata only, under no audio decoding; a missing/symlink WAV
        # must not present a usable playback button.
        try:
            available = retention_time > cutoff and stat.S_ISREG(audio_path.lstat().st_mode)
        except OSError:
            pass
    name = (clip or {}).get("original_name") or item.get("display_name") or audio.get("display_name")
    if not isinstance(name, str) or not name:
        try:
            stamp = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            stamp = item["id"][:8]
        prefix = "麦克风" if kind in ("live", "microphone") else "最近音频" if kind == "recent" else "录音"
        name = f"{prefix} {stamp}"
    result = {"id": item["id"], "clip_id": clip_id, "text": item.get("text", ""),
              "segments": item.get("segments", []), "source": source, "created_at": item.get("created_at", 0),
              "start_time": start, "end_time": end, "source_kind": kind, "display_name": name,
              "recording_available": available}
    for key in ("language", "backend", "selection", "duration_seconds"):
        if key in item:
            result[key] = item[key]
    return result


def document_page(store, *, limit=200, cursor=None, source=None):
    """Return full retained text ordered by recording time, with no inference.

    Pagination freezes a created_at/file-mtime cutoff. Files pruned, expired or
    edited after that cutoff are not resurrected; a fresh first page/revision
    reflects the current library. Empty successful transcripts are kept intact.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("文档每页数量必须是 1–200 的整数。")
    if source is not None and (not isinstance(source, str) or not 0 < len(source) <= 1000 or "\x00" in source):
        raise ValueError("文档来源必须是有效的完整来源名称。")
    now = time.time()
    state = (_decode_cursor(cursor, store, limit, source, now) if cursor is not None else
             {"v": 1, "store": _store_key(store), "limit": limit, "source": source, "snapshot": now, "after": None})
    cutoff = now - store.retention_hours * 3600
    latest = {}
    for item, mtime, size in _transcripts(store, state["snapshot"], cutoff):
        identity = ("clip", item["clip_id"]) if item.get("clip_id") else ("transcript", item["id"])
        previous = latest.get(identity)
        if previous is None or (item.get("created_at", 0), item["id"]) > (previous[0].get("created_at", 0), previous[0]["id"]):
            latest[identity] = (item, mtime, size)
    metadata = _clip_metadata(store)
    rendered = []
    for item, mtime, size in latest.values():
        clip = metadata.get(item.get("clip_id"))
        if clip is not None:
            clip = clip | {"_root": str(store.root)}
        output = _render(item, clip, cutoff)
        if source is None or output["source"] == source:
            rendered.append((output, mtime, size))
    rendered.sort(key=lambda record: (record[0]["start_time"], record[0]["id"]))
    digest = hashlib.sha256()
    for item, mtime, size in rendered:
        digest.update(json.dumps((item["id"], mtime, size, item["start_time"], item["end_time"],
                                  item["source"], item["display_name"], item["recording_available"]),
                                 ensure_ascii=False, separators=(",", ":")).encode())
    after = tuple(state["after"]) if state["after"] is not None else None
    eligible = [item for item, _, _ in rendered if after is None or (item["start_time"], item["id"]) > after]
    items = eligible[:limit]
    next_cursor = None
    if len(eligible) > limit:
        last = items[-1]
        next_cursor = _encode_cursor(state | {"after": [last["start_time"], last["id"]]})
    return {"items": items, "total": len(rendered), "next_cursor": next_cursor,
            "snapshot": state["snapshot"], "revision": digest.hexdigest()}
