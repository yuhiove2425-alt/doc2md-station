import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .convert import ConversionError, convert
from .registry import EXTENSIONS as SUPPORTED
from .registry import lookup


class Pipeline:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.logs = deque(maxlen=300)
        self.watching = False
        self.busy = None
        self.processed = 0
        self.failed = 0
        self.by_format = {}
        self._seen = {}
        self._blocked = set()
        self._thread = None
        self._stop = threading.Event()
        self._ensure_dirs()

    @property
    def root(self):
        return Path(self.cfg["root"]).expanduser()

    @property
    def inbox(self):
        return self.root / self.cfg["inbox_name"]

    @property
    def done(self):
        return self.root / self.cfg["done_name"]

    @property
    def failed_dir(self):
        return self.root / self.cfg["failed_name"]

    @property
    def output(self):
        return self.root / self.cfg["output_name"]

    @property
    def redact_dir(self):
        """치환 리포트와 원문 매핑이 쌓이는 곳. 치환본(.masked.md)은 마크다운 폴더에 둔다."""
        return self.root / self.cfg.get("redact_name", "치환기록")

    def _ensure_dirs(self):
        for d in (self.inbox, self.done, self.failed_dir, self.output, self.redact_dir):
            d.mkdir(parents=True, exist_ok=True)

    def log(self, level, message):
        with self.lock:
            self.logs.appendleft(
                {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message}
            )

    def list_dir(self, path, limit=200):
        if not path.exists():
            return []
        items = []
        for p in sorted(path.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            if p.is_dir():
                continue
            items.append(
                {
                    "name": p.name,
                    "size": p.stat().st_size,
                    "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M"),
                }
            )
        return items

    def pending(self):
        return [f for f in self.list_dir(self.inbox) if Path(f["name"]).suffix.lower() in SUPPORTED]

    def start(self):
        if self.watching:
            return
        self._ensure_dirs()
        self._stop.clear()
        self.watching = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log("info", "감시를 시작했습니다.")

    def stop(self):
        if not self.watching:
            return
        self._stop.set()
        self.watching = False
        self.log("info", "감시를 멈췄습니다.")

    def _loop(self):
        interval = float(self.cfg.get("poll_seconds", 2))
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:
                self.log("error", f"감시 루프 오류: {exc}")
            self._stop.wait(interval)

    def scan_once(self):
        self._ensure_dirs()
        stable_after = float(self.cfg.get("stable_seconds", 3))
        now = time.time()
        ready = []

        current = {}
        for p in self.inbox.iterdir():
            if p.is_dir() or p.suffix.lower() not in SUPPORTED:
                continue
            if p.name in self._blocked:
                current[p.name] = None
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            current[p.name] = size
            prev = self._seen.get(p.name)
            if prev and prev["size"] == size:
                if now - prev["since"] >= stable_after:
                    ready.append(p)
            else:
                self._seen[p.name] = {"size": size, "since": now}

        for gone in set(self._seen) - set(current):
            self._seen.pop(gone, None)
        self._blocked &= set(current)

        for path in ready:
            self._process(path)

    def _process(self, path):
        self.busy = path.name
        self._seen.pop(path.name, None)
        try:
            target, stats = convert(path, self.output, self.cfg)
            if self._move(path, self.done):
                self.processed += 1
                self._count(path)
                self.log("ok", f"{path.name} → {target.name} ({_summary(stats)})")
            else:
                self._discard(target)
                self._block(path.name)
        except ConversionError as exc:
            if self._move(path, self.failed_dir):
                self.failed += 1
                self.log("error", f"{path.name}: {exc}")
            else:
                self._block(path.name)
        except Exception as exc:
            if self._move(path, self.failed_dir):
                self.failed += 1
                self.log("error", f"{path.name}: 예상치 못한 오류 ({exc})")
            else:
                self._block(path.name)
        finally:
            self.busy = None

    def _count(self, path):
        fmt = lookup(path.name)
        key = fmt.ext.lstrip(".") if fmt else "기타"
        self.by_format[key] = self.by_format.get(key, 0) + 1

    def _discard(self, target):
        try:
            if target and target.exists():
                target.unlink()
        except OSError:
            pass

    def _block(self, name):
        self._blocked.add(name)
        self.log(
            "error",
            f"{name}: 다른 프로그램이 파일을 쓰고 있어 옮기지 못했습니다. "
            "해당 파일을 닫은 뒤 작업전에서 뺐다가 다시 넣어주세요.",
        )

    def _move(self, path, dest):
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / path.name
        if target.exists():
            for i in range(1, 1000):
                candidate = dest / f"{path.stem}_{i}{path.suffix}"
                if not candidate.exists():
                    target = candidate
                    break
        for attempt in range(3):
            try:
                shutil.move(str(path), str(target))
                return True
            except OSError:
                if attempt < 2:
                    time.sleep(0.6)
        return False

    def state(self):
        with self.lock:
            logs = list(self.logs)
        return {
            "watching": self.watching,
            "busy": self.busy,
            "processed": self.processed,
            "failed": self.failed,
            "by_format": dict(sorted(self.by_format.items(), key=lambda kv: -kv[1])),
            "pending": self.pending(),
            "done": self.list_dir(self.done, 40),
            "failed_files": self.list_dir(self.failed_dir, 40),
            "output": self.list_dir(self.output, 40),
            "logs": logs,
            "paths": {
                "root": str(self.root),
                "inbox": str(self.inbox),
                "done": str(self.done),
                "failed": str(self.failed_dir),
                "output": str(self.output),
                "redact": str(self.redact_dir),
            },
        }


def _summary(stats):
    """형식마다 셀 수 있는 게 달라서, 값이 있는 항목만 골라 한 줄로 적는다."""
    labels = [
        ("pages", "쪽"),
        ("slides", "슬라이드"),
        ("sheets", "시트"),
        ("headings", "제목"),
        ("tables", "표"),
        ("images", "그림"),
        ("footnotes", "각주"),
    ]
    parts = [f"{name} {stats[key]}" for key, name in labels if stats.get(key)]
    return " · ".join(parts) if parts else "본문만"
