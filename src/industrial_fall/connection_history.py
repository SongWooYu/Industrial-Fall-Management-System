"""Bounded transport evidence, independent of the noisy system journal.

Browser reports are observations, not trusted audit/security records. No SDP,
camera URLs, IP addresses, tokens, user agents or images are accepted/stored.
"""
from collections import deque
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
from threading import Lock
import time
from urllib.parse import urlsplit
from uuid import uuid4

from flask import request
from .diagnostic_log import DiagnosticLog

CLIENT_EVENTS = {"attempt", "fallback", "rtc_playing", "mjpeg_decoded", "mjpeg_error", "ended"}
SERVER_EVENTS = {"whep_accepted", "whep_failed", "mjpeg_open", "mjpeg_sent", "mjpeg_closed", "mjpeg_rejected"}
REASONS = {"automatic", "overlay_selected", "rtc_unsupported", "startup_timeout",
           "play_blocked", "ice_failed", "video_stalled", "stats_error", "whep_failed",
           "rtc_error", "stream_error", "hidden", "pagehide", "restart", "upstream_http",
           "upstream_unavailable", "invalid_response", "viewer_limit", "stream_closed"}
ENUMS = {
    "transport": {"connecting", "webrtc", "mjpeg"},
    "reason": REASONS,
    "local_candidate": {"host", "srflx", "prflx", "relay", "unknown"},
    "remote_candidate": {"host", "srflx", "prflx", "relay", "unknown"},
    "protocol": {"udp", "tcp", "unknown"},
    "ice_state": {"new", "checking", "connected", "completed", "disconnected", "failed", "closed"},
}
NUMBERS = {"sequence": 1_000_000, "elapsed_ms": 604_800_000, "frames_decoded": 1_000_000_000,
           "width": 16384, "height": 16384, "http_status": 599}
CONNECTION_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def connection_id(value):
    return value if isinstance(value, str) and CONNECTION_ID.fullmatch(value) else "unattributed"


def access_scope():
    """URL scope, NOT proof that the browser is on an external network."""
    hostname = urlsplit(request.host_url).hostname or ""
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "loopback_url"
    try:
        address = ipaddress.ip_address(hostname)
        return "public_ip_url" if address.is_global else "private_ip_url"
    except ValueError:
        return "domain_url"  # domain URL can also be visited from the same LAN!


def validate(data, source="browser"):
    if (not isinstance(data, dict) or not isinstance(data.get("event"), str)
            or data["event"] not in (CLIENT_EVENTS if source == "browser" else SERVER_EVENTS)):
        raise ValueError("Invalid event")
    if not isinstance(data.get("connection_id"), str) or not CONNECTION_ID.fullmatch(data["connection_id"]):
        raise ValueError("Invalid connection id")
    result = {"event": data["event"], "connection_id": data["connection_id"]}
    for key, choices in ENUMS.items():
        if key in data:
            if not isinstance(data[key], str) or data[key] not in choices:
                raise ValueError("Invalid enum")
            result[key] = data[key]
    for key, maximum in NUMBERS.items():
        if key in data:
            if type(data[key]) is not int or not 0 <= data[key] <= maximum:
                raise ValueError("Invalid number")
            result[key] = data[key]
    if "overlay" in data:
        if type(data["overlay"]) is not bool:
            raise ValueError("Invalid overlay")
        result["overlay"] = data["overlay"]
    return result  # extra fields (including credentials/URLs) are discarded


class ConnectionHistory:
    max_bytes = 2_000_000
    backups = 2

    def __init__(self, path=None):
        self.items = deque(maxlen=200)
        self.lock = Lock()
        self.tokens = 60.0
        self.last_token = time.monotonic()
        self.rate_dropped = 0
        self.restore_error = False
        self.writer = None
        self.start_error = False
        if path:
            target = Path(path)
            # Hard byte/line limits even if a file was replaced externally.
            for file in [Path(str(target) + ".2"), Path(str(target) + ".1"), target]:
                try:
                    with file.open("rb") as stream:
                        stream.seek(0, 2)
                        offset = max(0, stream.tell() - self.max_bytes)
                        stream.seek(offset)
                        if offset:
                            stream.readline(4096)
                        for raw in stream.read(self.max_bytes).splitlines():
                            if len(raw) > 4096:
                                continue
                            try:
                                row = json.loads(raw)
                                source = row.get("source")
                                if source not in {"browser", "server"}:
                                    continue
                                clean = validate(row, source)
                                if not re.fullmatch(r"[a-f0-9]{32}", row.get("id", "")):
                                    continue
                                datetime.fromisoformat(row["timestamp"])
                                clean.update(id=row["id"], timestamp=row["timestamp"], source=source,
                                             access=row.get("access") if row.get("access") in
                                             {"loopback_url", "private_ip_url", "public_ip_url", "domain_url"} else "unknown")
                                self.items.append(clean)
                            except (ValueError, TypeError, KeyError, AttributeError):
                                continue  # incomplete last line after abrupt power loss
                except FileNotFoundError:
                    pass
                except OSError:
                    self.restore_error = True
            try:
                # Preserve an incomplete power-loss line but separate the next record.
                if target.exists():
                    with target.open("a+b") as stream:
                        if stream.tell():
                            stream.seek(-1, 2)
                            if stream.read(1) != b"\n":
                                stream.write(b"\n")
                self.writer = DiagnosticLog(str(target), max_bytes=self.max_bytes, backups=self.backups)
            except OSError:
                self.start_error = True  # telemetry failure must not take down fall inference

    def record(self, data, source="browser", access="unknown"):
        clean = validate(data, source)
        with self.lock:
            now = time.monotonic()
            self.tokens = min(60.0, self.tokens + max(0, now - self.last_token) * 5)
            self.last_token = now
            if self.tokens < 1:
                self.rate_dropped += 1
                return False
            self.tokens -= 1
            clean.update(id=uuid4().hex, timestamp=datetime.now(timezone.utc).isoformat(),
                         source=source, access=access)
            self.items.append(clean)
            if self.writer:
                self.writer.record(clean)  # bounded queue; no blocking disk I/O here
        return True

    def server(self, event, cid, access, **fields):
        return self.record({"event": event, "connection_id": connection_id(cid), **fields},
                           source="server", access=access)

    def snapshot(self):
        with self.lock:
            return {"events": [dict(row) for row in reversed(self.items)], "limit": 200,
                    "storage_error": self.start_error or self.restore_error or bool(self.writer and self.writer.error),
                    "persistent": self.writer is not None,
                    "dropped": self.rate_dropped + (self.writer.dropped if self.writer else 0)}

    def close(self):
        if self.writer:
            self.writer.close()
