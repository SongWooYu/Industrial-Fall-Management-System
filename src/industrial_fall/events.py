"""Local-only, bounded event journal. No broker, database, or remote service."""
from collections import deque, OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import threading
import uuid

from .evidence import EvidenceStore


class EventJournal:
    def __init__(self, limit=50):
        self._events = deque(maxlen=limit)
        self._lock = threading.Lock()
        self.evidence = EvidenceStore(limit=limit)

    def emit(self, kind, *, track=None, reason=None, snapshot=None):
        event = {"id": uuid.uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(),
                 "camera_id": "dahua", "type": kind, "track": deepcopy(track),
                 "reason": reason, "snapshot_url": None}
        if snapshot is not None:
            jpeg, metadata = snapshot
            if self.evidence.put(event["id"], jpeg):
                event["snapshot_url"] = f"/api/events/{event['id']}/snapshot.jpg"
                event["snapshot"] = metadata
        with self._lock:
            self._events.appendleft(event)
        return event

    def recent(self):
        with self._lock:
            return deepcopy(list(self._events))


class FallTransitions:
    """Translate native states once; track loss is NOT proof of recovery."""
    def __init__(self):
        self.previous = {}
        self.seen = OrderedDict()
        self.missing_since = None
        self.state = "normal"

    def update(self, tracks, now):
        events = []
        if not tracks:
            if self.state not in {"normal", "unobserved"}:
                if self.missing_since is None:
                    self.missing_since = now
                elif now - self.missing_since >= 2:
                    events.append(("tracking_lost", None))
                    self.state = "unobserved"
            self.previous = {}
            return events
        self.missing_since = None
        states = [t["state"] for t in tracks]
        self.state = next((s for s in ("fallen", "suspected", "recovering") if s in states), "normal")
        cleared = False
        for track in tracks:
            tid, state = track["track_id"], track["state"]
            event_id = track.get("event_id", 0)
            previous = self.previous.get(tid)
            if track.get("fall_event") and event_id > 0 and self.seen.get(tid) != event_id:
                self.seen[tid] = event_id
                self.seen.move_to_end(tid)
                while len(self.seen) > 256:
                    self.seen.popitem(last=False)
                events.append(("fall_confirmed", track))
            elif state != previous:
                if state == "suspected":
                    events.append(("fall_suspected", track))
                elif state == "recovering" and previous is not None:
                    events.append(("fall_recovering", track))
                elif state == "normal" and previous not in {None, "normal"} and self.state == "normal" and not cleared:
                    events.append(("fall_cleared", track))
                    cleared = True
        self.previous = {t["track_id"]: t["state"] for t in tracks}
        return events
