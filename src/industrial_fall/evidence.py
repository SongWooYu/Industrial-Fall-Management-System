"""Small, expiring in-memory and optional disk-backed event stills."""
from collections import OrderedDict
from pathlib import Path
import re
import threading
import time

EVENT_ID = re.compile(r"^[0-9a-f]{32}$")


class EvidenceStore:
    def __init__(self, limit=50, ttl=3600, max_bytes=512_000, storage_dir=None):
        self.limit, self.ttl, self.max_bytes = limit, ttl, max_bytes
        self.storage_dir = Path(storage_dir) if storage_dir else None
        if self.storage_dir is not None:
            try:
                self.storage_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.storage_dir = None
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def _expire(self):
        now = time.monotonic()
        for key in list(self.items):
            if now - self.items[key][0] >= self.ttl:
                del self.items[key]

    def put(self, event_id, jpeg):
        if not EVENT_ID.fullmatch(event_id) or not jpeg or len(jpeg) > self.max_bytes:
            return False
        payload = bytes(jpeg)
        with self.lock:
            self._expire()
            self.items[event_id] = (time.monotonic(), payload)
            while len(self.items) > self.limit:
                evicted, _ = self.items.popitem(last=False)
                if self.storage_dir is not None:
                    try:
                        (self.storage_dir / f"{evicted}.jpg").unlink(missing_ok=True)
                    except OSError:
                        pass
            if self.storage_dir is not None:
                try:
                    target = self.storage_dir / f"{event_id}.jpg"
                    tmp = self.storage_dir / f"{event_id}.jpg.tmp"
                    tmp.write_bytes(payload)
                    tmp.replace(target)
                except OSError:
                    pass
        return True

    def get(self, event_id):
        if not EVENT_ID.fullmatch(event_id):
            return None
        with self.lock:
            self._expire()
            item = self.items.get(event_id)
            if item is not None:
                return item[1]
            if self.storage_dir is not None:
                try:
                    path = self.storage_dir / f"{event_id}.jpg"
                    if path.is_file():
                        data = path.read_bytes()
                        if 0 < len(data) <= self.max_bytes:
                            self.items[event_id] = (time.monotonic(), data)
                            return data
                except OSError:
                    pass
            return None
