"""Local-only, bounded event journal with local disk & SQLite persistence for confirmed fall incidents."""
from collections import deque, OrderedDict
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from .evidence import EVENT_ID, EvidenceStore


class EventJournal:
    def __init__(self, limit=50, storage_dir=None):
        self.limit = limit
        self._events = deque(maxlen=limit)
        self._confirmed = deque(maxlen=limit)
        self._lock = threading.Lock()
        self.storage_dir = Path(storage_dir) if storage_dir else None
        ttl = 86400 * 30 if self.storage_dir is not None else 3600
        self.evidence = EvidenceStore(limit=limit, ttl=ttl, storage_dir=self.storage_dir)
        if self.storage_dir is not None:
            self._init_db()
            self._restore()

    def _init_db(self):
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            db_file = self.storage_dir / "incidents.db"
            with closing(sqlite3.connect(db_file)) as conn, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS fall_incidents ("
                    "incident_id TEXT PRIMARY KEY, "
                    "timestamp TEXT NOT NULL, "
                    "camera_id TEXT NOT NULL, "
                    "track_json TEXT, "
                    "snapshot_url TEXT, "
                    "snapshot_json TEXT, "
                    "acknowledged INTEGER NOT NULL DEFAULT 0"
                    ")"
                )
        except Exception:
            pass

    def _restore(self):
        try:
            seen_ids = set()
            db_file = self.storage_dir / "incidents.db"
            if db_file.is_file():
                with closing(sqlite3.connect(db_file)) as conn:
                    rows = conn.execute(
                        "SELECT incident_id, timestamp, camera_id, track_json, snapshot_url, snapshot_json "
                        "FROM fall_incidents ORDER BY rowid DESC LIMIT ?",
                        (self.limit,),
                    ).fetchall()
                for inc_id, ts, cam_id, track_json, snap_url, snap_json in reversed(rows):
                    if not EVENT_ID.fullmatch(str(inc_id)):
                        continue
                    if snap_url and self.evidence.get(inc_id) is None:
                        snap_url = None
                    row = {
                        "id": inc_id,
                        "timestamp": ts,
                        "camera_id": cam_id,
                        "type": "fall_confirmed",
                        "track": json.loads(track_json) if track_json else None,
                        "reason": None,
                        "snapshot_url": snap_url,
                    }
                    if snap_json:
                        row["snapshot"] = json.loads(snap_json)
                    self._confirmed.appendleft(row)
                    self._events.appendleft(row)
                    seen_ids.add(inc_id)

            index_file = self.storage_dir / "events.json"
            if not index_file.is_file():
                return
            rows = json.loads(index_file.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                return
            for row in reversed(rows[:self.limit]):
                if not isinstance(row, dict) or not EVENT_ID.fullmatch(str(row.get("id", ""))):
                    continue
                if row["id"] in seen_ids:
                    continue
                if row.get("snapshot_url") and self.evidence.get(row["id"]) is None:
                    row["snapshot_url"] = None
                self._events.appendleft(row)
                if row.get("type") == "fall_confirmed":
                    self._confirmed.appendleft(row)
        except Exception:
            pass

    def _persist(self, confirmed_event=None):
        if self.storage_dir is None:
            return
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            if confirmed_event is not None:
                db_file = self.storage_dir / "incidents.db"
                with closing(sqlite3.connect(db_file)) as conn, conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO fall_incidents "
                        "(incident_id, timestamp, camera_id, track_json, snapshot_url, snapshot_json, acknowledged) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0)",
                        (
                            confirmed_event["id"],
                            confirmed_event["timestamp"],
                            confirmed_event["camera_id"],
                            json.dumps(confirmed_event.get("track"), ensure_ascii=False)
                            if confirmed_event.get("track") is not None
                            else None,
                            confirmed_event.get("snapshot_url"),
                            json.dumps(confirmed_event.get("snapshot"), ensure_ascii=False)
                            if confirmed_event.get("snapshot") is not None
                            else None,
                        ),
                    )
            tmp = self.storage_dir / "events.json.tmp"
            target = self.storage_dir / "events.json"
            tmp.write_text(json.dumps(self._merged_events(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
        except Exception:
            pass

    def _merged_events(self):
        # Preserve all recent fall_confirmed events even if non-confirmed events flood _events
        by_id = {}
        for ev in self._events:
            by_id[ev["id"]] = ev
        for ev in self._confirmed:
            if ev["id"] not in by_id:
                by_id[ev["id"]] = ev
        ordered = sorted(by_id.values(), key=lambda x: x.get("timestamp", ""), reverse=True)
        confirmed = [e for e in ordered if e.get("type") == "fall_confirmed"]
        others = [e for e in ordered if e.get("type") != "fall_confirmed"]
        kept = confirmed[:self.limit]
        remaining = max(0, self.limit - len(kept))
        combined = kept + others[:remaining]
        combined.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return combined[:self.limit]

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
            if kind == "fall_confirmed":
                self._confirmed.appendleft(event)
            self._persist(confirmed_event=event if kind == "fall_confirmed" else None)
        return event

    def recent(self):
        with self._lock:
            return deepcopy(self._merged_events())


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
