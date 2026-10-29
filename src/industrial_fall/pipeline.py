"""One RTSP decoder, one Hailo inference path, bounded latest-frame display."""
from collections import deque
from datetime import datetime, timezone
import logging
import re
import threading
import time

import cv2
import numpy as np

from .diagnostic_log import DiagnosticLog
from .edgefall_shadow import EdgeFallRuntime
from .events import EventJournal, FallTransitions
from .inference import HailoPoseEstimator
from .pose_overlay import draw_pose_shapes
from .presentation import analysis_readiness
from .sources import RtspSource

LOG = logging.getLogger(__name__)
COLORS = {"normal": (70, 155, 70), "suspected": (0, 165, 255),
          "fallen": (30, 30, 235), "recovering": (180, 80, 180)}


def safe_error(exc):
    return re.sub(r"rtsps?://\S+", "[camera URL]", str(exc))[:240]


def select_track(tracks):
    visible = [t for t in tracks if t.get("visible")]
    return max(visible or tracks, key=lambda t: (t.get("fall_detected", False),
               t.get("temporal_probability", 0)), default=None)


def render_packet(packet, state, *, evidence=False):
    """Packet keeps image and coordinates from the SAME inference frame."""
    seq, captured, raw, poses = packet
    frame = raw.copy()
    shapes = draw_pose_shapes(frame, poses)
    height = frame.shape[0]
    frame = cv2.copyMakeBorder(frame, 0, 38, 0, 0, cv2.BORDER_CONSTANT,
                              value=COLORS.get(state, (60, 60, 65)))
    title = "FALL CONFIRMED" if evidence else state.upper()
    text = f"{title} | PERSON {shapes['boxes']} / JOINTS {shapes['visible_keypoints']} | FRAME {seq}"
    cv2.putText(frame, text, (10, height + 25), cv2.FONT_HERSHEY_SIMPLEX,
                .55, (255, 255, 255), 1, cv2.LINE_AA)
    return frame, {"frame_seq": seq, "pose_frame_matched": True, "overlay": shapes,
                   "capture_age_ms": round((time.monotonic() - captured) * 1000)}


class FallPipeline:
    def __init__(self, config):
        self.config = config
        self.events = EventJournal(limit=50)
        self.transitions = FallTransitions()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads = []
        self.latest = None  # (monotonic sequence, capture monotonic time, frame)
        self.analyzed = None  # (...latest, matched poses)
        self.tracks = []
        self.capture_error = None
        self.analysis_error = None
        self.last_analysis = 0.0
        self.inference_ms = deque(maxlen=150)
        self.completed = deque(maxlen=150)
        self.captured = deque(maxlen=150)
        self.profile = None
        self.buffer_age_ms = 0.0
        self.clients = {False: 0, True: 0}
        self.jpeg_cache = {False: None, True: None}
        self.jpeg_versions = {False: 0, True: 0}
        self.diagnostics = None
        self.diagnostic_error = None

    def start(self):
        diagnostic = self.config["fall_detection"]["edgefall"].get("diagnostics", {})
        if diagnostic.get("enabled"):
            try:
                self.diagnostics = DiagnosticLog(diagnostic["path"], diagnostic["max_bytes"], diagnostic["backups"])
            except Exception as exc:
                self.diagnostic_error = safe_error(exc)
        for target in (self._capture, self._analyze, self._render):
            thread = threading.Thread(target=target, daemon=True, name=target.__name__)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=10)
        if self.diagnostics:
            self.diagnostics.close()

    def _capture(self):
        camera = self.config["camera"]
        options = {k: v for k, v in camera.items() if k not in {"id", "label"}}
        seq = 0
        while not self.stop_event.is_set():
            source = RtspSource(**options)
            try:
                source.open()
                while not self.stop_event.is_set():
                    frame = source.read()
                    now = time.monotonic()
                    seq += 1
                    with self.lock:
                        self.latest = (seq, now, frame)
                        self.captured.append(now)
                        self.capture_error = None
                        self.buffer_age_ms = source.buffer_age_ms
            except Exception as exc:
                with self.lock:
                    self.capture_error = safe_error(exc)
                LOG.warning("Camera unavailable: %s", safe_error(exc))
            finally:
                source.close()
            self.stop_event.wait(3)

    def _analyze(self):
        config = self.config["fall_detection"]
        while not self.stop_event.is_set():
            estimator = runtime = None
            try:
                estimator = HailoPoseEstimator(config["model"], config["hailo_postprocess"],
                                               config["hailo_whole_buffer"], config["confidence"],
                                               config["hailo_timeout_seconds"])
                runtime = EdgeFallRuntime(config["edgefall"]["library"], config["edgefall"]["keypoint_confidence"])
                if not runtime.profile.endswith("units-separated-v6"):
                    raise RuntimeError("Unvalidated EdgeFall model profile")
                with self.lock:
                    self.profile = runtime.profile
                    self.transitions = FallTransitions()
                    self.tracks = []
                self._analysis_loop(estimator, runtime)
            except Exception as exc:
                with self.lock:
                    self.analysis_error = safe_error(exc)
                LOG.error("Analysis unavailable: %s", safe_error(exc))
            finally:
                if runtime:
                    runtime.close()
                if estimator:
                    estimator.close()
            self.stop_event.wait(5)

    def _analysis_loop(self, estimator, runtime):
        last_seq, next_due, last_input_at = -1, 0.0, None
        while not self.stop_event.is_set():
            now = time.monotonic()
            with self.lock:
                packet = self.latest
            if packet is None or packet[0] == last_seq or now - packet[1] > 1 or now < next_due:
                self.stop_event.wait(.005)
                continue
            seq, captured_at, frame = packet
            # A reconnect must not join separate actions into one temporal window.
            if last_input_at is not None and captured_at - last_input_at > 1:
                raise RuntimeError("Input gap: restart temporal observation window")
            last_input_at = captured_at
            last_seq, next_due = seq, now + 1 / 15
            start = time.monotonic()
            poses = estimator.estimate(frame)
            finished = time.monotonic()
            tracks = runtime.update(poses, frame.shape[1], frame.shape[0], finished)
            matched = (seq, captured_at, frame, poses)
            with self.lock:
                changes = self.transitions.update(tracks, finished)
                self.analyzed = matched
                self.tracks = tracks
                self.last_analysis = finished
                self.inference_ms.append((finished - start) * 1000)
                self.completed.append(finished)
                self.analysis_error = None
            for kind, track in changes:
                snapshot = None
                if kind == "fall_confirmed" and finished - captured_at <= 2:
                    try:
                        picture, metadata = render_packet(matched, "fallen", evidence=True)
                        ok, jpeg = cv2.imencode(".jpg", picture, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        if ok:
                            snapshot = (jpeg.tobytes(), metadata)
                    except Exception:
                        LOG.exception("Snapshot unavailable; retaining fall event")
                self.events.emit(kind, track=track, snapshot=snapshot)
                LOG.info("FALL_EVENT type=%s track=%s", kind, track.get("track_id") if track else None)
            if self.diagnostics:
                self.diagnostics.record({"schema_version": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(), "monotonic_sec": finished,
                    "camera_id": "dahua", "frame_seq": seq,
                    "frame_size": [frame.shape[1], frame.shape[0]], "profile": runtime.profile,
                    "input_age_ms": round((start - captured_at) * 1000, 1),
                    "inference_ms": round((finished - start) * 1000, 1),
                    "poses": poses, "tracks": tracks, "error": None})

    def _render(self):
        previous = {False: None, True: None}
        interval = 1 / self.config["server"]["stream_fps"]
        while not self.stop_event.is_set():
            start = time.monotonic()
            with self.lock:
                clients = dict(self.clients)
                latest, analyzed = self.latest, self.analyzed
                state = self.transitions.state
                error = self.analysis_error
            for overlay in (False, True):
                if not clients[overlay]:
                    continue
                packet = analyzed if overlay else latest
                stale = packet is None or start - packet[1] > 2 or (overlay and error)
                version = (packet[0] if packet else -1, bool(stale))
                if previous[overlay] == version and self.jpeg_cache[overlay] is not None:
                    continue
                try:
                    if stale:
                        frame = np.zeros((540, 960, 3), np.uint8)
                        cv2.putText(frame, "ANALYSIS UNAVAILABLE" if overlay else "CAMERA UNAVAILABLE",
                                    (60, 270), cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 200, 240), 2)
                    elif overlay:
                        frame, _ = render_packet(packet, state)
                    else:
                        frame = packet[2]
                    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.config["server"]["jpeg_quality"]])
                    if ok:
                        with self.lock:
                            self.jpeg_cache[overlay] = jpeg.tobytes()
                            self.jpeg_versions[overlay] += 1
                        previous[overlay] = version
                except Exception as exc:
                    LOG.warning("Display unavailable: %s", safe_error(exc))
            self.stop_event.wait(max(.005, interval - (time.monotonic() - start)))

    def subscribe(self, overlay):
        with self.lock:
            if sum(self.clients.values()) >= self.config["server"].get("max_stream_clients", 6):
                return False
            self.clients[overlay] += 1
            return True

    def unsubscribe(self, overlay):
        with self.lock:
            self.clients[overlay] = max(0, self.clients[overlay] - 1)

    def jpeg(self, overlay):
        with self.lock:
            return self.jpeg_versions[overlay], self.jpeg_cache[overlay]

    @staticmethod
    def _fps(stamps, now):
        recent = [s for s in stamps if now - s < 5]
        return round((len(recent) - 1) / (recent[-1] - recent[0]), 1) if len(recent) > 1 else 0.0

    def status(self):
        now = time.monotonic()
        with self.lock:
            online = self.latest is not None and now - self.latest[1] < 2 and not self.capture_error
            fresh = bool(self.last_analysis and now - self.last_analysis < 2)
            tracks = self.tracks if online and fresh and not self.analysis_error else []
            best = select_track(tracks)
            error = self.capture_error or self.analysis_error or ("추론 결과 갱신 대기" if online and not fresh else None)
            status = {"camera_id": "dahua", "label": self.config["camera"]["label"],
                "online": bool(online), "analysis_online": bool(online and fresh and not error),
                "error": error, "pose_enabled": True, "edgefall_enabled": self.profile is not None,
                "edgefall_tracks": tracks, "edgefall_best_track_id": best["track_id"] if best else None,
                "person_count": sum(t.get("visible", False) for t in tracks),
                "fall_state": self.transitions.state if tracks else "unobserved",
                "capture_fps": self._fps(self.captured, now), "analysis_fps": self._fps(self.completed, now),
                "inference_ms": round(float(np.mean(self.inference_ms)), 1) if self.inference_ms else None,
                "frame_age_ms": round((now - self.latest[1]) * 1000) if self.latest else None,
                "input_buffer_age_ms": round(self.buffer_age_ms), "profile": self.profile,
                "viewers": sum(self.clients.values()), "event_limit": 50,
                "diagnostic_records_dropped": self.diagnostics.dropped if self.diagnostics else 0,
                "diagnostic_error": self.diagnostics.error if self.diagnostics else self.diagnostic_error}
        status["analysis"] = analysis_readiness(status)
        return status
