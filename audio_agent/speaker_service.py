"""Explicit local diarization jobs and a user-confirmed private voice reference.

Anonymous speaker numbers belong to one clip. Embeddings never enter HTTP
responses; a reference is created only by an explicit enrollment action.
"""
from __future__ import annotations

import copy
import hashlib
from collections import deque
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid

from .pipeline import asr_lock

MAX_SELF_SAMPLES = 10


def _atomic_json(path: Path, value):
    temporary = path.parent / f".{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path, maximum=2_000_000):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError("声纹或分析文件格式无效或过大。")
            data = handle.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError("声纹或分析文件过大。")
        value = json.loads(data)
        json.dumps(value, allow_nan=False)
        if not isinstance(value, dict):
            raise ValueError("声纹或分析文件格式无效。")
        return value
    except FileNotFoundError:
        return None


def _cosine(left, right):
    if not isinstance(left, list) or not isinstance(right, list) or not 0 < len(left) <= 4096 or len(left) != len(right):
        return None
    if any(not _number(x, -math.inf) for x in left + right):
        return None
    left_scale, right_scale = max(abs(x) for x in left), max(abs(x) for x in right)
    if not left_scale or not right_scale:
        return None
    # Normalize before squaring; finite but large values must not yield NaN
    # and accidentally pass embedding validity checks.
    left = [x / left_scale for x in left]
    right = [x / right_scale for x in right]
    denominator = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return max(-1.0, min(1.0, sum(x*y for x, y in zip(left, right)) / denominator))


def _number(value, minimum=0, maximum=math.inf):
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and minimum <= value <= maximum
    except OverflowError:
        return False


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _model_identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 200


def _person_text(name, note):
    if not isinstance(name, str) or not isinstance(note, str):
        raise ValueError("人物名字和备注必须为文字。")
    name, note = name.strip(), note.strip()
    if (not 1 <= len(name) <= 80 or len(note) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in name + note)):
        raise ValueError("名字需要 1–80 字，备注最多 200 字；请使用单行文字。")
    if name == "我":
        raise ValueError("“我”用于本人声纹；请使用登记为我的操作，或为其他人物填写名字。")
    return name, note


def _valid_reference(value):
    return (isinstance(value, dict) and _model_identifier(value.get("embedding_model_id"))
            and _cosine(value.get("embedding"), value.get("embedding")) is not None
            and _identifier(value.get("analysis_id")) and _identifier(value.get("clip_id"))
            and isinstance(value.get("speaker_id"), str)
            and re.fullmatch(r"speaker_[1-9][0-9]*", value["speaker_id"]) is not None
            and _number(value.get("created_at"))
            and _number(value.get("reference_seconds"), maximum=30.001)
            and value["reference_seconds"] > 0)


def _validate_analysis(value, clip):
    """Validate persisted/native metadata before public rendering or enrollment."""
    if (not isinstance(value, dict) or not _identifier(value.get("id"))
            or value.get("clip_id") != clip["id"] or not _number(value.get("created_at"))
            or not _model_identifier(value.get("embedding_model_id"))
            or not isinstance(value.get("backend"), str)
            or not _number(value.get("elapsed_seconds"))
            or not isinstance(value.get("speakers"), list) or len(value["speakers"]) > 1000
            or not isinstance(value.get("speaker_count"), int) or isinstance(value["speaker_count"], bool)
            or value["speaker_count"] != len(value["speakers"])
            or not isinstance(value.get("turns"), list) or len(value["turns"]) > 10000
            or not isinstance(value.get("notes", []), list)
            or not all(isinstance(note, str) for note in value.get("notes", []))):
        raise ValueError("说话人分析格式无效，请重新分析。")
    ids = set()
    for person in value["speakers"]:
        if not isinstance(person, dict):
            raise ValueError("说话人分析格式无效，请重新分析。")
        speaker_id = person.get("id")
        vector = person.get("embedding")
        if (not isinstance(speaker_id, str) or not re.fullmatch(r"speaker_[1-9][0-9]*", speaker_id)
                or speaker_id in ids or not _number(person.get("speech_seconds"), maximum=clip["duration"] + .001)
                or not _number(person.get("reference_seconds"), maximum=min(30, person["speech_seconds"]) + .001)
                or vector is not None and _cosine(vector, vector) is None
                or vector is None and person["reference_seconds"] != 0):
            raise ValueError("说话人分析格式无效，请重新分析。")
        ids.add(speaker_id)
    for turn in value["turns"]:
        if (not isinstance(turn, dict) or not isinstance(turn.get("speaker"), str) or turn.get("speaker") not in ids
                or not _number(turn.get("start"), maximum=clip["duration"])
                or not _number(turn.get("end"), maximum=clip["duration"] + .001)
                or turn["end"] <= turn["start"]):
            raise ValueError("说话人分析时间段无效，请重新分析。")


class SpeakerManager:
    def __init__(self, store, *, analyzer=None, model_status=None, asr_pending=None,
                 max_pending=12, busy_timeout=120, busy_retry_seconds=.25):
        from .speakers import analyze_audio, speaker_model_status
        self.store = store
        self.analyzer = analyzer or analyze_audio
        self.model_status = model_status or speaker_model_status
        if (not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending < 1
                or not _number(busy_timeout, minimum=.001)
                or not _number(busy_retry_seconds, minimum=.001)):
            raise ValueError("说话人任务队列设置无效。")
        self.asr_pending = asr_pending or (lambda: False)
        self.max_pending = max_pending
        self.busy_timeout, self.busy_retry_seconds = busy_timeout, busy_retry_seconds
        self.folder = store.root / "speakers"
        if self.folder.is_symlink():
            raise ValueError("说话人分析目录不能是符号链接。")
        self.folder.mkdir(mode=0o700, exist_ok=True)
        self.folder.chmod(0o700)
        self.profile_path = store.root / "voiceprint.json"
        self.profiles_path = store.root / "speaker-profiles.json"
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._model_busy = threading.Event()
        self._jobs = {}
        self._queue = deque()
        self._automatic_seen = {}
        self._thread = None
        self._closed = False
        self.settings_path = store.root / "speaker-settings.json"
        self._automatic_enabled = False
        self._automatic_error = None
        try:
            settings = _read_json(self.settings_path, 4096)
            if settings is not None:
                if set(settings) != {"automatic_enabled"} or not isinstance(settings["automatic_enabled"], bool):
                    raise ValueError("自动说话人设置格式无效，请重新保存设置。")
                self._automatic_enabled = settings["automatic_enabled"]
        except (OSError, ValueError, UnicodeError) as exc:
            self._automatic_error = str(exc)

    def _clip(self, clip_id):
        if not isinstance(clip_id, str) or not re.fullmatch(r"[0-9a-f]{32}", clip_id):
            raise ValueError("录音编号无效。")
        clip = self.store.get_clip(clip_id)
        if clip is None:
            raise ValueError("录音不存在或已清理。")
        timestamp = clip["created_time"] if clip.get("import_sha256") else clip["end_time"]
        if timestamp <= time.time() - self.store.retention_hours * 3600:
            raise ValueError("录音已超过保留期限。")
        return clip

    def _profile(self):
        """Read either format without upgrading or mutating it during a GET."""
        value = _read_json(self.profile_path, 2_000_000)
        if value is None:
            return None
        if "version" not in value:
            if not _valid_reference(value):
                raise ValueError("本地声纹格式无效，请取消登记后重新登记。")
            # Stable legacy sample identity supports delete/idempotent append
            # without writing a migration merely because a UI reads status.
            sample = copy.deepcopy(value)
            binding = "|".join(value[key] for key in ("clip_id", "analysis_id", "speaker_id"))
            sample["id"] = hashlib.sha256(binding.encode()).hexdigest()[:32]
            return {"version": 2, "created_at": value["created_at"], "updated_at": value["created_at"],
                    "samples": [sample]}
        if (set(value) != {"version", "created_at", "updated_at", "samples"}
                or type(value["version"]) is not int or value["version"] != 2
                or not _number(value["created_at"]) or not _number(value["updated_at"])
                or not isinstance(value["samples"], list) or not 1 <= len(value["samples"]) <= MAX_SELF_SAMPLES):
            raise ValueError("本地多段本人声纹格式无效，请检查或取消登记。")
        ids, bindings, analysis_speakers, dimensions = set(), set(), {}, {}
        for sample in value["samples"]:
            if not _valid_reference(sample) or not _identifier(sample.get("id")):
                raise ValueError("本地本人参考样本格式无效。")
            binding = (sample["clip_id"], sample["analysis_id"], sample["speaker_id"])
            analysis_key = binding[:2]
            model = sample["embedding_model_id"]
            if (sample["id"] in ids or binding in bindings
                    or analysis_key in analysis_speakers and analysis_speakers[analysis_key] != sample["speaker_id"]
                    or model in dimensions and dimensions[model] != len(sample["embedding"])):
                raise ValueError("本地本人参考包含重复、相互冲突或模型维度不一致的样本。")
            ids.add(sample["id"]); bindings.add(binding)
            analysis_speakers[analysis_key] = sample["speaker_id"]
            dimensions[model] = len(sample["embedding"])
        return value

    @staticmethod
    def _self_candidate(profile, model_id):
        samples = [sample for sample in profile["samples"] if sample["embedding_model_id"] == model_id]
        if not samples:
            return None
        return {"id": "self", "name": "我", "note": "", "is_self": True,
                "embedding_model_id": model_id, "references": samples}

    @staticmethod
    def _candidate_score(vector, candidate):
        if not candidate["is_self"]:
            return _cosine(vector, candidate["embedding"])
        # Treat all confirmed samples as ONE identity. Weight their individual
        # normalized cosine scores by usable duration (capped at 10 s). Do not
        # renormalize the mean direction: contradictory references would then
        # inflate similarity. Do not use max-of-N, which adds false-match chances.
        values = [(_cosine(vector, sample["embedding"]), min(sample["reference_seconds"], 10.0))
                  for sample in candidate["references"]]
        if any(score is None for score, _ in values):
            return None
        total = sum(weight for _, weight in values)
        return sum(score * (weight / total) for score, weight in values) if total else None

    def _profiles(self):
        value = _read_json(self.profiles_path, 8_000_000)
        if value is None:
            return []
        if (set(value) != {"version", "items"} or type(value.get("version")) is not int or value["version"] != 1
                or not isinstance(value.get("items"), list) or len(value["items"]) > 100):
            raise ValueError("本地人物声纹文件格式无效。")
        ids, bindings = set(), set()
        for profile in value["items"]:
            if (not _valid_reference(profile) or not _identifier(profile.get("id"))
                    or not _number(profile.get("updated_at"))):
                raise ValueError("本地人物声纹文件格式无效。")
            name, note = _person_text(profile.get("name"), profile.get("note"))
            if name != profile["name"] or note != profile["note"]:
                raise ValueError("本地人物声纹文件格式无效。")
            binding = (profile["analysis_id"], profile["speaker_id"])
            if profile["id"] in ids or binding in bindings:
                raise ValueError("本地人物声纹文件包含重复人物或重复说话人登记。")
            ids.add(profile["id"])
            bindings.add(binding)
        return value["items"]

    @staticmethod
    def _public_profile(profile):
        result = {key: profile[key] for key in ("id", "name", "note", "created_at", "updated_at",
                                               "reference_seconds", "clip_id", "speaker_id", "analysis_id")}
        result["display_label"] = profile["name"] + (" · " + profile["note"] if profile["note"] else "")
        return result

    def profiles(self):
        with self._lock:
            try:
                own = self.voiceprint()
            except (OSError, ValueError) as exc:
                own = {"enrolled": False, "label": "我", "error": str(exc)}
            return {"items": [self._public_profile(value) for value in self._profiles()], "voiceprint": own}

    def _save_profiles(self, profiles):
        value = {"version": 1, "items": profiles}
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8_000_000:
            raise ValueError("本地人物声纹文件已达到容量上限，请先移除不再需要的登记。")
        _atomic_json(self.profiles_path, value)

    def voiceprint(self):
        with self._lock:
            profile = self._profile()
            samples = profile["samples"] if profile else []
            public = [{key: sample[key] for key in ("id", "created_at", "clip_id", "speaker_id",
                                                    "analysis_id", "reference_seconds")}
                      | {"model_compatible": None} for sample in samples]
            total = sum(sample["reference_seconds"] for sample in samples)
            latest = samples[-1] if samples else {}
            return {"enrolled": bool(samples), "label": "我",
                    "created_at": profile["created_at"] if profile else None,
                    "reference_seconds": total, "total_reference_seconds": total,
                    "sample_count": len(samples), "samples": public,
                    "clip_id": latest.get("clip_id"), "analysis_id": latest.get("analysis_id"),
                    "speaker_id": latest.get("speaker_id")}

    def status(self):
        model = self.model_status()
        try:
            profile = self.voiceprint()
        except (OSError, ValueError) as exc:
            profile = {"enrolled": False, "label": "我", "error": str(exc)}
        with self._lock:
            return {"available": model["ready"], "backend": model["backend"],
                    "reason": model.get("reason"), "voiceprint": profile,
                    "voiceprint_multisample": True, "voiceprint_max_samples": MAX_SELF_SAMPLES,
                    "automatic_enabled": self._automatic_enabled, "automatic_error": self._automatic_error,
                    "queued_count": sum(job["status"] == "queued" for job in self._jobs.values()),
                    "running_count": sum(job["status"] == "running" for job in self._jobs.values())}

    def update_settings(self, automatic_enabled):
        if not isinstance(automatic_enabled, bool):
            raise ValueError("automatic_enabled 必须为布尔值。")
        with self._lock, self.store._lock(exclusive=True):
            if self._closed:
                raise RuntimeError("本地服务正在关闭。")
            # Commit storage first; a failed write must not change live policy.
            _atomic_json(self.settings_path, {"automatic_enabled": automatic_enabled})
            self._automatic_enabled = automatic_enabled
            self._automatic_error = None
            return self.status()

    def model_busy(self):
        """Only this manager's actual shared-model lock ownership, not a queue."""
        return self._model_busy.is_set()

    def _analysis(self, clip_id):
        clip = self._clip(clip_id)
        value = _read_json(self.folder / f"{clip_id}.json")
        if value is not None:
            _validate_analysis(value, clip)
        return value

    def _public(self, analysis):
        if analysis is None:
            return None
        own_reference_valid = True
        try:
            profile = self._profile()
        except (OSError, ValueError):
            profile = None
            own_reference_valid = False
        try:
            named = self._profiles()
            profiles_valid = own_reference_valid
        except (OSError, ValueError):
            named, profiles_valid = [], False
        candidates = [value | {"is_self": False, "references": [value]} for value in named]
        if profile:
            own = self._self_candidate(profile, analysis["embedding_model_id"])
            if own is not None:
                candidates.append(own)
        result = {key: copy.deepcopy(analysis.get(key)) for key in
                  ("id", "clip_id", "speaker_count", "elapsed_seconds", "backend", "created_at", "notes")}
        requested = analysis.get("requested_speakers")
        result["requested_speakers"] = requested if isinstance(requested, int) and not isinstance(requested, bool) and 1 <= requested <= 10 else None
        threshold = analysis.get("cluster_threshold")
        result["cluster_threshold"] = threshold if _number(threshold, minimum=0, maximum=2) else None
        # Never copy arbitrary nested native/persisted objects to HTTP. Raw
        # embeddings are private even if an extra field appears in a turn.
        result["turns"] = [{key: turn[key] for key in ("start", "end", "speaker")} for turn in analysis["turns"]]
        result["speakers"] = []
        candidates = [value for value in candidates if value["embedding_model_id"] == analysis["embedding_model_id"]]
        by_profile = {value["id"]: value for value in candidates}
        scores = {}
        for person in analysis["speakers"]:
            for candidate in candidates:
                score = self._candidate_score(person.get("embedding"), candidate)
                if score is not None and person.get("reference_seconds", 0) >= 2:
                    scores[person["id"], candidate["id"]] = score

        def winner(values):
            # Similarity is not an identity probability. Require separation
            # both between candidate people and between speakers in this clip.
            ranked = sorted(values, key=lambda item: (-item[1], item[0]))
            return ranked[0][0] if (ranked and ranked[0][1] >= .65
                                   and (len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= .1)) else None

        profile_winners = {candidate["id"]: winner([(speaker, score) for (speaker, target), score in scores.items()
                                                     if target == candidate["id"]]) for candidate in candidates}
        # An explicitly listened-to binding owns this identity in the current
        # analysis, including references shorter than the automatic 2 s gate.
        # Do not let another speaker win the same profile merely because the
        # confirmed short speaker was absent from the automatic score matrix.
        speaker_ids = {person["id"] for person in analysis["speakers"]}
        for candidate in candidates:
            bound = {reference["speaker_id"] for reference in candidate["references"]
                     if reference["analysis_id"] == analysis["id"] and reference["clip_id"] == analysis["clip_id"]
                     and reference["speaker_id"] in speaker_ids}
            if bound:
                profile_winners[candidate["id"]] = next(iter(bound)) if len(bound) == 1 else None
        for index, person in enumerate(analysis["speakers"], 1):
            reference_seconds = person.get("reference_seconds", 0)
            identity = None
            confirmed = [candidate for candidate in candidates if any(
                self._bound(reference, analysis["clip_id"], analysis["id"], person["id"])
                for reference in candidate["references"])]
            chosen = confirmed[0] if len(confirmed) == 1 else None
            manual = chosen is not None
            if not confirmed and profiles_valid:
                selected = winner([(target, score) for (speaker, target), score in scores.items() if speaker == person["id"]])
                if selected is not None and profile_winners[selected] == person["id"]:
                    chosen = by_profile[selected]
            if chosen is not None:
                display = chosen["name"] + (" · " + chosen["note"] if chosen["note"] else "")
                identity = {"profile_id": chosen["id"], "name": chosen["name"], "note": chosen["note"],
                            "is_self": chosen["is_self"], "label": chosen["name"], "display_label": display,
                            "source": "user_reference" if manual else "voiceprint_match",
                            "score": self._candidate_score(person.get("embedding"), chosen)}
            result["speakers"].append({"id": person["id"], "label": f"说话人 {index}",
                                      "speech_seconds": person.get("speech_seconds", 0),
                                      "reference_seconds": reference_seconds,
                                      "can_enroll": reference_seconds > 0 and _cosine(person.get("embedding"), person.get("embedding")) is not None,
                                      "identity": identity})
        return result

    def analysis(self, clip_id):
        with self._lock:
            return self._public(self._analysis(clip_id))

    def annotate(self, transcript):
        clip_id = transcript.get("clip_id")
        if not clip_id:
            return transcript
        try:
            analysis = self.analysis(clip_id)
        except (OSError, ValueError):
            return transcript
        if not analysis:
            return transcript
        labels = {}
        for speaker in analysis["speakers"]:
            identity = speaker.get("identity")
            label = identity["display_label"] if identity else speaker["label"]
            if identity and identity["source"] == "voiceprint_match":
                label += "（声纹匹配）"
            labels[speaker["id"]] = label
        result = copy.deepcopy(transcript)
        result["speaker_count"] = analysis["speaker_count"]
        for segment in result.get("segments", []):
            if not isinstance(segment, dict):
                continue
            start, end = segment.get("start"), segment.get("end")
            if not _number(start) or not _number(end) or end <= start:
                continue
            overlap = {}
            for turn in analysis["turns"]:
                seconds = max(0, min(end, turn["end"]) - max(start, turn["start"]))
                if seconds > .05:
                    overlap[turn["speaker"]] = overlap.get(turn["speaker"], 0) + seconds
            ids = [key for key, value in overlap.items() if value >= min(.3, (end-start) * .2)]
            segment["speaker_ids"] = ids
            segment["speaker_labels"] = [labels.get(key, "说话人待确定") for key in ids]
        return result

    def _enrollment_reference(self, clip_id, speaker_id, analysis_id):
        analysis = self._analysis(clip_id)
        if analysis is None:
            raise ValueError("请先分析这段录音的说话人。")
        if not _identifier(analysis_id) or analysis_id != analysis["id"]:
            raise ValueError("说话人分析已更新或未指定，请重新试听并确认当前分析结果。")
        person = next((item for item in analysis["speakers"] if item["id"] == speaker_id), None)
        if person is None or person.get("reference_seconds", 0) <= 0 or _cosine(person.get("embedding"), person.get("embedding")) is None:
            raise ValueError("这位说话人尚无有效声纹，请使用清晰单人语音重新分析。登记不设最低秒数。")
        return analysis, person

    @staticmethod
    def _bound(profile, clip_id, analysis_id, speaker_id):
        return bool(profile and profile["clip_id"] == clip_id and profile["analysis_id"] == analysis_id
                    and profile["speaker_id"] == speaker_id)

    def enroll(self, clip_id, speaker_id, confirm, analysis_id=None, mode="replace"):
        if confirm is not True:
            raise ValueError("请先试听并确认这是你本人的清晰单人声音。")
        if mode not in ("append", "replace"):
            raise ValueError("本人参考操作 mode 仅支持 append 或 replace。")
        with self._lock, self.store._lock(exclusive=True):
            analysis, person = self._enrollment_reference(clip_id, speaker_id, analysis_id)
            if any(self._bound(value, clip_id, analysis_id, speaker_id) for value in self._profiles()):
                raise ValueError("这位说话人已登记为其他人物；请先在声纹与人物中移除该人物，再登记为我。")
            old = self._profile() if mode == "append" else None
            samples = copy.deepcopy(old["samples"]) if old else []
            if any(self._bound(value, clip_id, analysis_id, speaker_id) for value in samples):
                return self.voiceprint()
            if any(value["clip_id"] == clip_id and value["analysis_id"] == analysis_id
                   and value["speaker_id"] != speaker_id for value in samples):
                raise ValueError("这次分析中的另一位说话人已登记为我；请先移除该样本，避免互相矛盾的确认。")
            if len(samples) >= MAX_SELF_SAMPLES:
                raise ValueError(f"本人参考最多保存 {MAX_SELF_SAMPLES} 段；请先移除不需要的样本。")
            same_model = [value for value in samples if value["embedding_model_id"] == analysis["embedding_model_id"]]
            if any(len(value["embedding"]) != len(person["embedding"]) for value in same_model):
                raise ValueError("同模型参考的声纹维度不一致，请重新检查分析结果。")
            now = time.time()
            samples.append({"id": uuid.uuid4().hex, "created_at": now,
                            "analysis_id": analysis["id"], "clip_id": clip_id, "speaker_id": speaker_id,
                            "embedding_model_id": analysis["embedding_model_id"], "embedding": person["embedding"],
                            "reference_seconds": person["reference_seconds"]})
            _atomic_json(self.profile_path, {"version": 2, "created_at": old["created_at"] if old else now,
                                             "updated_at": now, "samples": samples})
            return self.voiceprint()

    def remove_sample(self, sample_id):
        if not _identifier(sample_id):
            raise ValueError("本人参考样本编号无效。")
        with self._lock, self.store._lock(exclusive=True):
            profile = self._profile()
            samples = [value for value in profile["samples"] if value["id"] != sample_id] if profile else []
            if profile is None or len(samples) == len(profile["samples"]):
                raise ValueError("本人参考样本不存在或已经移除。")
            if samples:
                _atomic_json(self.profile_path, profile | {"updated_at": time.time(), "samples": samples})
            else:
                self.profile_path.unlink()
            return self.voiceprint()

    def enroll_profile(self, clip_id, speaker_id, analysis_id, confirm, name, note, replace_profile_id=None):
        if confirm is not True:
            raise ValueError("请先试听并确认选中的说话人就是要登记的人物。")
        name, note = _person_text(name, note)
        if replace_profile_id is not None and not _identifier(replace_profile_id):
            raise ValueError("要替换的人物编号无效。")
        with self._lock, self.store._lock(exclusive=True):
            analysis, person = self._enrollment_reference(clip_id, speaker_id, analysis_id)
            own = self._profile()
            if own and any(self._bound(value, clip_id, analysis_id, speaker_id) for value in own["samples"]):
                raise ValueError("这位说话人已登记为“我”；请先移除对应本人样本或取消本人登记，再登记为其他人物。")
            profiles = self._profiles()
            bound = next((value for value in profiles if self._bound(value, clip_id, analysis_id, speaker_id)), None)
            previous = next((value for value in profiles if value["id"] == replace_profile_id), None)
            if bound is not None and replace_profile_id != bound["id"]:
                raise ValueError("这位说话人已有登记；请编辑该人物或指定其 replace_profile_id。若要更换为其他人物，请先移除当前登记。")
            if replace_profile_id is not None and previous is None:
                raise ValueError("要替换的人物不存在或已经移除，请重新选择并确认。")
            if previous is None and len(profiles) >= 100:
                raise ValueError("最多保存 100 位其他人物，请先移除不再需要的登记。")
            now = time.time()
            profile = {"id": previous["id"] if previous else uuid.uuid4().hex,
                       "name": name, "note": note, "created_at": previous["created_at"] if previous else now,
                       "updated_at": now, "analysis_id": analysis["id"], "clip_id": clip_id, "speaker_id": speaker_id,
                       "embedding_model_id": analysis["embedding_model_id"], "embedding": person["embedding"],
                       "reference_seconds": person["reference_seconds"]}
            values = [value for value in profiles if value["id"] != profile["id"]] + [profile]
            self._save_profiles(values)
            return self.profiles() | {"profile": self._public_profile(profile)}

    def update_profile(self, profile_id, name, note):
        if not _identifier(profile_id):
            raise ValueError("人物编号无效。")
        name, note = _person_text(name, note)
        with self._lock, self.store._lock(exclusive=True):
            profiles = self._profiles()
            profile = next((value for value in profiles if value["id"] == profile_id), None)
            if profile is None:
                raise ValueError("人物不存在或已经移除。")
            profile.update(name=name, note=note, updated_at=time.time())
            self._save_profiles(profiles)
            return self.profiles() | {"profile": self._public_profile(profile)}

    def remove_profile(self, profile_id):
        if not _identifier(profile_id):
            raise ValueError("人物编号无效。")
        with self._lock, self.store._lock(exclusive=True):
            profiles = self._profiles()
            values = [profile for profile in profiles if profile["id"] != profile_id]
            if len(values) == len(profiles):
                raise ValueError("人物不存在或已经移除。")
            self._save_profiles(values)
            return self.profiles()

    def clear(self):
        with self._lock, self.store._lock(exclusive=True):
            self.profile_path.unlink(missing_ok=True)
            return self.voiceprint()

    def items(self):
        with self._lock:
            values = []
            for item in reversed(self._jobs.values()):
                public = copy.deepcopy(item)
                if item["status"] == "completed":
                    try:
                        public["result"] = {"analysis": self.analysis(item["clip_id"])}
                    except (OSError, ValueError):
                        public["result"] = {"analysis": None}
                values.append(public)
            return values

    def submit(self, clip_id, num_speakers=None):
        return self._submit(clip_id, num_speakers, automatic=False)

    def submit_automatic(self, clip_id):
        """Called only for new successful live ASR; never scan historical clips."""
        with self._lock:
            if not self._automatic_enabled or self._closed:
                return None
            now = time.time()
            self._automatic_seen = {key: expiry for key, expiry in self._automatic_seen.items() if expiry > now}
            if clip_id in self._automatic_seen:
                return None
            try:
                clip = self._clip(clip_id)
                timestamp = clip["created_time"] if clip.get("import_sha256") else clip["end_time"]
                self._automatic_seen[clip_id] = timestamp + self.store.retention_hours * 3600
                # A manual analysis may have completed or been queued while ASR
                # ran. Reuse it instead of scheduling duplicate automatic work.
                if self._analysis(clip_id) is not None:
                    return None
                for job in self._jobs.values():
                    if job["clip_id"] == clip_id and job["status"] in ("queued", "running"):
                        return copy.deepcopy(job)
                return self._submit(clip_id, None, automatic=True)
            except Exception as exc:
                self._automatic_error = str(exc)
                return None

    def _submit(self, clip_id, num_speakers, *, automatic):
        clip = self._clip(clip_id)
        if num_speakers is not None and (not isinstance(num_speakers, int) or isinstance(num_speakers, bool) or not 1 <= num_speakers <= 10):
            raise ValueError("人数请选自动估计或 1–10 人。")
        if not 0 < clip["duration"] <= 600:
            raise ValueError("一次说话人分析支持最多 10 分钟录音，请先选择较短片段。")
        if not self.model_status()["ready"]:
            raise RuntimeError("说话人模型尚未准备好，当前只支持已配置的本地模型。")
        with self._lock:
            if self._closed:
                raise RuntimeError("本地服务正在关闭。")
            if sum(item["status"] in ("queued", "running") for item in self._jobs.values()) >= self.max_pending:
                raise RuntimeError("说话人分析队列已满；文字转写不受影响，可稍后手动分析此片段。")
            job = {"id": uuid.uuid4().hex, "kind": "diarization", "clip_id": clip_id,
                   "automatic": automatic,
                   "status": "queued", "created_at": time.time(), "completed_at": None,
                   "started_at": None, "waiting_reason": None, "result": None, "error": None}
            self._jobs[job["id"]] = job
            completed = [key for key, value in self._jobs.items() if value["status"] in ("completed", "failed")]
            for key in completed[:-50]:
                del self._jobs[key]
            self._queue.append((job["id"], num_speakers))
            result = dict(job)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="local-speaker-queue", daemon=False)
                self._thread.start()
            self._condition.notify_all()
            return result

    def _work(self):
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                job_id, count = self._queue.popleft()
            self._run(job_id, count)

    def _acquire_model(self, stack, job):
        deadline = time.monotonic() + self.busy_timeout
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("服务已关闭；此片段可稍后手动分析。")
            # Do not hold our state lock while consulting the live manager.
            # Its success callback acquires this manager's state lock.
            pending = self.asr_pending()
            reason = "等待文字转写完成" if pending else "等待本地模型空闲"
            if not pending:
                try:
                    stack.enter_context(asr_lock(self.store.root))
                except RuntimeError as exc:
                    if not (isinstance(exc.__cause__, BlockingIOError) or "已有本地转写任务" in str(exc)):
                        raise
                else:
                    self._model_busy.set()
                    return
            if time.monotonic() >= deadline:
                raise RuntimeError("本地模型持续忙碌，说话人分析未完成；文字转写不受影响，可稍后手动重试。")
            with self._condition:
                job["waiting_reason"] = reason
                self._condition.wait(timeout=self.busy_retry_seconds)

    def _run(self, job_id, count):
        with self._lock:
            job = self._jobs[job_id]
        try:
            try:
                with ExitStack() as stack:
                    self._acquire_model(stack, job)
                    with self._lock:
                        if self._closed or job["status"] != "queued":
                            raise RuntimeError("服务已关闭，排队的说话人分析已取消；可稍后手动重试。")
                        job.update(status="running", started_at=time.time(), waiting_reason=None)
                    temp = stack.enter_context(tempfile.TemporaryDirectory(dir=self.store.root))
                    # A queued job may expire before obtaining the model lock.
                    # Do not access old audio just because submit passed.
                    self._clip(job["clip_id"])
                    audio = Path(temp) / "clip.wav"
                    self.store.snapshot_clip(job["clip_id"], output_path=audio)
                    analysis = self.analyzer(audio, num_speakers=count)
                    # Keep the shared model lock until the sidecar is saved;
                    # otherwise migration can copy between inference and save.
                    with self._lock, self.store._lock(exclusive=True):
                        clip = self._clip(job["clip_id"])
                        analysis.update(id=uuid.uuid4().hex, clip_id=job["clip_id"], created_at=time.time())
                        _validate_analysis(analysis, clip)
                        _atomic_json(self.folder / f"{job['clip_id']}.json", analysis)
                        job.update(status="completed", completed_at=time.time())
            finally:
                self._model_busy.clear()
        except Exception as exc:
            with self._lock:
                job.update(status="failed", error=str(exc), completed_at=time.time(), waiting_reason=None)
                if job["automatic"]:
                    self._automatic_error = str(exc)

    def close(self):
        with self._condition:
            self._closed = True
            # Finish only inference already in progress. Cancel queued work so
            # shutdown cannot wait for an entire queue of native model runs.
            for job in self._jobs.values():
                if job["status"] == "queued":
                    job.update(status="failed", completed_at=time.time(), waiting_reason=None,
                               error="服务已关闭，排队的说话人分析已取消；可稍后手动重试。")
            self._queue.clear()
            self._condition.notify_all()
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join()
