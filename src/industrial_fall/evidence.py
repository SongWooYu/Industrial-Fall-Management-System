"""Small, expiring in-memory event stills; never a video recorder."""
from collections import OrderedDict
import re
import threading
import time

EVENT_ID = re.compile(r"^[0-9a-f]{32}$")


class EvidenceStore:
    def __init__(self, limit=50, ttl=3600, max_bytes=512_000):
        self.limit, self.ttl, self.max_bytes = limit, ttl, max_bytes
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
        with self.lock:
            self._expire()
            self.items[event_id] = (time.monotonic(), bytes(jpeg))
            while len(self.items) > self.limit:
                self.items.popitem(last=False)
        return True

    def get(self, event_id):
        with self.lock:
            self._expire()
            item = self.items.get(event_id)
            return item[1] if item else None
