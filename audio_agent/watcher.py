"""Import completed WAVs from a local/synced folder, without automatic ASR."""
from pathlib import Path
import time


class FolderWatcher:
    def __init__(self, store, folder, stable_seconds=3, include_edits=False):
        self.store = store
        self.folder = Path(folder).expanduser().resolve()
        if not self.folder.is_dir():
            raise ValueError(f"监听目录不存在：{self.folder}")
        if self.folder == store.root or self.folder in store.root.parents or store.root in self.folder.parents:
            raise ValueError("监听目录与数据目录不能互相包含，避免重复导入缓存。")
        if stable_seconds < 0:
            raise ValueError("stable_seconds 不能为负数。")
        self.stable_seconds = stable_seconds
        self.include_edits = include_edits
        self.observed = {}
        self.imported = {}

    def scan(self, now=None):
        now = time.monotonic() if now is None else now
        results = []
        present = set()
        for path in self.folder.rglob("*"):
            if path.suffix.lower() != ".wav" or not path.is_file() or path.is_symlink():
                continue
            if not self.include_edits and ("edit" in [part.lower() for part in path.relative_to(self.folder).parts]
                                           or path.stem.lower().endswith("_edit")):
                continue
            # Resolve only files within the selected directory, including parent symlinks.
            if not path.resolve().is_relative_to(self.folder):
                continue
            present.add(path)
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if self.imported.get(path) == signature:
                    continue
                previous = self.observed.get(path)
                if previous is None or previous[0] != signature:
                    self.observed[path] = (signature, now)
                    continue
                if now - previous[1] < self.stable_seconds:
                    continue
                clip = self.store.import_wav(path, source="folder-import")
                # Files still being written are retried on next stable version.
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) == signature:
                    self.imported[path] = signature
                    results.append({"file": str(path), "clip": clip})
            except (OSError, ValueError, RuntimeError) as exc:
                results.append({"file": str(path), "error": str(exc)})
        self.observed = {p: value for p, value in self.observed.items() if p in present}
        self.imported = {p: value for p, value in self.imported.items() if p in present}
        return results
