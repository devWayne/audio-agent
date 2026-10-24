"""Read-only, per-client incremental views of the retained transcript document.

Clients must call/poll this API; it never wakes a client model or starts capture
or inference. Each cursor is an independent watermark, not a shared consumer.
The in-memory index holds only the latest fingerprint/sequence per retained
document identity. Unread intermediate edits may coalesce into their latest
version; clients upsert by document_key rather than append duplicate text.
After this process restarts, initialize again without a cursor.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time

from .document import document_page
from .speaker_service import SpeakerManager


def _finite_number(value):
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


class _ReadOnlySpeakerAnnotations(SpeakerManager):
    """Reuse public annotation logic without the job manager's writable setup."""

    def __init__(self, store):
        # annotate()/analysis() only need these paths and their read lock. Do
        # not call SpeakerManager.__init__: that creates/chmods a directory.
        self.store = store
        self.folder = store.root / "speakers"
        self.profile_path = store.root / "voiceprint.json"
        self.profiles_path = store.root / "speaker-profiles.json"
        self._lock = threading.RLock()

    def annotate(self, transcript):
        if self.folder.is_symlink():
            raise ValueError("说话人分析目录不能是符号链接。")
        return super().annotate(transcript)


class UpdatesFeed:
    def __init__(self, store, *, poll_interval=1.0):
        if not _finite_number(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval 必须为正的有限数值。")
        self.store = store
        self.poll_interval = poll_interval
        self._annotations = _ReadOnlySpeakerAnnotations(store)
        self._key = secrets.token_bytes(32)
        self._epoch = secrets.token_hex(16)
        self._store = hashlib.sha256(str(store.root).encode()).hexdigest()
        self._lock = asyncio.Lock()
        self._index = {}
        self._sequence = 0

    def _encode(self, sequence):
        data = json.dumps({"v": 1, "epoch": self._epoch, "store": self._store, "after": sequence},
                          sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(data + hmac.digest(self._key, data, "sha256")).rstrip(b"=").decode()

    def _decode(self, cursor):
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
                raise ValueError()
            signed = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            data, signature = signed[:-32], signed[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._key, data, "sha256")):
                raise ValueError()
            value = json.loads(data)
            if (not isinstance(value, dict) or set(value) != {"v", "epoch", "store", "after"}
                    or value["v"] != 1 or value["epoch"] != self._epoch or value["store"] != self._store
                    or not isinstance(value["after"], int) or isinstance(value["after"], bool)
                    or not 0 <= value["after"] <= self._sequence):
                raise ValueError()
            return value["after"]
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
            raise ValueError("增量游标无效或已因 MCP 服务重启而过期；请省略 cursor 重新读取最近文字。") from exc

    def _snapshot(self):
        # A full document read is necessary to notice edits to old clips and
        # speaker annotations that finish after their text. No WAV is opened.
        for _ in range(3):
            records, cursor, expected = [], None, None
            seen = set()
            while True:
                page = document_page(self.store, cursor=cursor, limit=200)
                metadata = (page["total"], page["snapshot"], page["revision"])
                if expected is None:
                    expected = metadata
                if metadata != expected or any(item["id"] in seen for item in page["items"]):
                    break
                records.extend(page["items"])
                seen.update(item["id"] for item in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    if len(records) != expected[0]:
                        break
                    values = []
                    for record in records:
                        item = self._annotations.annotate(record)
                        key = "clip:" + item["clip_id"] if item["clip_id"] else "transcript:" + item["id"]
                        fingerprint = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True,
                                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                        values.append((key, fingerprint, item))
                    return values
        raise RuntimeError("转写文档正在变化，尚未取得完整快照；请使用同一 cursor 稍后重试。")

    async def _page(self, after, limit):
        async with self._lock:
            values = await asyncio.to_thread(self._snapshot)
            index, items = {}, []
            for key, fingerprint, record in values:
                previous = self._index.get(key)
                if previous is None or previous[0] != fingerprint:
                    self._sequence += 1
                    sequence = self._sequence
                else:
                    sequence = previous[1]
                index[key] = (fingerprint, sequence)
                items.append(record | {"document_key": key, "update_sequence": sequence})
            # Removed/expired documents are dropped from the index. The feed
            # serves retained latest text, not an archival deletion/event log.
            self._index = index
            if after is None:
                # document_page already orders recording time, with ID ties.
                selected, has_more = items[-limit:], False
            else:
                eligible = sorted((item for item in items if item["update_sequence"] > after),
                                  key=lambda item: item["update_sequence"])
                selected, has_more = eligible[:limit], len(eligible) > limit
            watermark = selected[-1]["update_sequence"] if has_more else self._sequence
            return {"items": selected, "next_cursor": self._encode(watermark),
                    "has_more": has_more, "initial": after is None, "timed_out": False}

    async def get_updates(self, cursor=None, limit=20, wait_seconds=0):
        """Read complete new/updated text; optionally wait, without inference.

        No cursor returns only the latest `limit` documents and establishes a
        baseline. Continue with next_cursor, draining has_more before waiting.
        Changes to an existing document are upserts with the same document_key.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须是 1–100 的整数。")
        if not _finite_number(wait_seconds) or not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds 必须是 0–30 秒的有限数值。")
        after = None if cursor is None else self._decode(cursor)
        deadline = time.monotonic() + wait_seconds
        while True:
            page = await self._page(after, limit)
            if after is None or page["items"] or wait_seconds == 0:
                return page
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                page["timed_out"] = True
                return page
            await asyncio.sleep(min(self.poll_interval, remaining))
