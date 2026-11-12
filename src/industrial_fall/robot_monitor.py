"""Read-only telemetry, live WebSocket video stream, and AI analysis monitor for the Safety Exploration Robot.

Connects to 192.168.1.206:5000 (/monitor/video and /monitor/status with X-Monitor-Key) completely
independently of the Windows controller PC (/control and /video), and runs real-time safety helmet /
structural defect / object analysis without issuing any control commands.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hmac
import json
import logging
import math
import os
from pathlib import Path
import pty
import re
import select
import sqlite3
import struct
import threading
import time
import uuid
from typing import Any

import cv2
import numpy as np
from flask import Response, abort, jsonify, request

from .config import _load_dotenv
from .request_origin import same_origin_request
from .robot_vision import ANALYSIS_MODES, ANOMALY_TARGETS, RobotVisionAnalyzer, SimpleWebSocket

LOGGER = logging.getLogger(__name__)
EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

DIRECTION_LABELS = {
    "STOP": "정지",
    "FORWARD": "전진",
    "BACKWARD": "후진",
    "LEFT": "좌측 수평이동",
    "RIGHT": "우측 수평이동",
    "FORWARD_LEFT": "좌상 대각전진",
    "FORWARD_RIGHT": "우상 대각전진",
    "BACKWARD_LEFT": "좌하 대각후진",
    "BACKWARD_RIGHT": "우하 대각후진",
    "TURN_LEFT": "제자리 좌회전",
    "TURN_RIGHT": "제자리 우회전",
    "CURVE_LEFT": "좌측 곡선주행",
    "CURVE_RIGHT": "우측 곡선주행",
    "ARC_LEFT": "좌측 원호주행",
    "ARC_RIGHT": "우측 원호주행",
}

REASON_LABELS = {
    "WAITING": "원격 조종 PC 대기 중",
    "STOP": "정지 상태 (연결됨)",
    "RUNNING": "주행 중",
    "FRONT OBSTACLE": "전방 장애물 감지 · 자동 정지 (≤30cm)",
    "COMMAND TIMEOUT": "제어 신호 시간 초과 · 안전 정지",
    "VIDEO NOT READY": "카메라 프레임 대기 · 안전 정지",
    "DISTANCE NOT READY": "초음파 거리 센서 대기 · 안전 정지",
    "DISTANCE COMPLETE": "목표 거리 이동 완료 · 정지",
    "MOTOR ERROR": "모터 I2C 통신 확인 필요",
}


def _make_placeholder_jpeg(title: str, subtitle: str) -> bytes:
    canvas = np.zeros((480, 640, 3), dtype=np.uint8)
    canvas[:, :] = (28, 22, 18)
    cv2.rectangle(canvas, (20, 20), (620, 460), (70, 55, 45), 2)
    cv2.putText(canvas, title, (48, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (235, 240, 245), 2, cv2.LINE_AA)
    cv2.putText(canvas, subtitle, (48, 265), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (160, 175, 190), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else b""


def _ssh_read_robot_state(host: str, user: str, password: str, timeout: float = 4.0) -> dict[str, Any]:
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=2",
        f"{user}@{host}",
        "cat ~/Desktop/Car3/car_state.json 2>/dev/null; echo ''; echo '---DIAG---'; tail -n 1 ~/Desktop/Car3/logs/server_diagnostics.log 2>/dev/null",
    ]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
    output = b""
    sent_pw = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if fd in r:
                try:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    output += chunk
                    if not sent_pw and b"password:" in output.lower():
                        os.write(fd, (password + "\n").encode("utf-8"))
                        sent_pw = True
                except OSError:
                    break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass

    text = output.decode("utf-8", errors="replace")
    state_match = re.search(r"\{[^{}]*\"boot_id\"[^{}]*\}", text)
    if not state_match:
        raise RuntimeError("로봇 상태 응답 없음")
    state = json.loads(state_match.group(0))
    diag: dict[str, Any] = {}
    if "---DIAG---" in text:
        diag_part = text.split("---DIAG---", 1)[1].strip()
        for line in diag_part.splitlines():
            m = re.search(
                r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d+)\s+reason=(.*?)\s+command_age=([\d.]+)\s+motor_age=([\d.]+)\s+camera_age=([\d.]+)\s+loop_age=([\d.]+)",
                line.strip(),
            )
            if m:
                diag = {
                    "timestamp": m.group(1),
                    "reason": m.group(2),
                    "command_age_sec": float(m.group(3)),
                    "motor_age_sec": float(m.group(4)),
                    "camera_age_sec": float(m.group(5)),
                    "loop_age_sec": float(m.group(6)),
                }
    direction = str(state.get("direction", "STOP"))
    reason = str(state.get("reason", "WAITING"))
    distance_cm = state.get("distance_cm")
    return {
        "online": True,
        "host": host,
        "port": state.get("port", 5000),
        "direction": direction,
        "direction_label": DIRECTION_LABELS.get(direction, direction),
        "pwm": state.get("pwm", 0),
        "rpm": [0, 0, 0, 0],
        "distance_cm": distance_cm,
        "obstacle_warning": bool(distance_cm is not None and distance_cm <= 30.0),
        "pc_connected": bool(state.get("pc_connected", False)),
        "control_fresh": bool(state.get("control_fresh", False)),
        "reason": reason,
        "reason_label": REASON_LABELS.get(reason, reason),
        "motor_error": state.get("motor_error", ""),
        "camera_error": state.get("camera_error", ""),
        "dht22": None,
        "mq135": None,
        "diagnostics": diag,
        "sampled_at": datetime.now(timezone.utc).isoformat(),
    }


class RobotMonitor:
    def __init__(
        self,
        project_root: Path | str,
        *,
        host: str = "192.168.1.206",
        port: int = 5000,
        user: str = "humanoid",
        password: str = "1234",
        poll_remote: bool = True,
    ):
        self.project_root = Path(project_root)
        _load_dotenv(self.project_root / ".env")
        self.source_dir = self.project_root / "src/RaspberryPi5_Car/Rasp5Car_source"
        self.pc_code_dir = self.source_dir / "PC_code"
        self.db_path = self.pc_code_dir / "exploration_history.db"
        self.runtime_captures_dir = self.project_root / "runtime/robot_captures"
        self.runtime_events_file = self.runtime_captures_dir / "events.json"

        self.host = os.environ.get("ROBOT_CAR_HOST", host)
        self.port = int(os.environ.get("ROBOT_CAR_PORT", str(port)))
        self.user = os.environ.get("ROBOT_CAR_USER", user)
        self.password = os.environ.get("ROBOT_CAR_PASSWORD", password)
        self.monitor_key = os.environ.get("CAR_MONITOR_KEY", "").strip()
        self.poll_remote = poll_remote

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._frame_cond = threading.Condition(self._lock)

        self.stream_enabled = True
        self.analysis_mode = "all"
        self._video_state = "connecting" if poll_remote else "offline"
        self._video_message = "로봇 영상 채널 연결 준비 중"
        self._raw_jpeg: bytes = _make_placeholder_jpeg("ROBOT CAMERA", "Connecting to 192.168.1.206:5000...")
        self._annotated_jpeg: bytes = self._raw_jpeg
        self._latest_bgr: np.ndarray | None = None
        self._latest_seq: int = -1
        self._latest_frame_time: float = 0.0
        self._analyzed_seq: int = -1

        self._vision_detections: list[dict[str, Any]] = []
        self._vision_summary: str = "분석 대기 중"
        self._vision_ms: float | None = None
        self._vision_fps: float = 0.0
        self._confirm_streak: dict[str, int] = {}
        self._last_saved_by_label: dict[str, float] = {}
        self._live_events: list[dict[str, Any]] = []
        self._latest_alert: dict[str, Any] | None = None
        self._load_runtime_events()

        self._last_ssh_poll = 0.0
        self._ssh_polling = False
        self._telemetry: dict[str, Any] = {
            "online": False,
            "host": self.host,
            "port": self.port,
            "direction": "STOP",
            "direction_label": "확인 중",
            "pwm": 0,
            "rpm": [0, 0, 0, 0],
            "distance_cm": None,
            "obstacle_warning": False,
            "pc_connected": False,
            "control_fresh": False,
            "reason": "OFFLINE",
            "reason_label": "로봇 상태 확인 중",
            "motor_error": "",
            "camera_error": "",
            "dht22": None,
            "mq135": None,
            "diagnostics": {},
            "sampled_at": None,
        }

        self._analyzer: RobotVisionAnalyzer | None = None
        if self.poll_remote:
            self._trigger_ssh_poll_if_due()
            threading.Thread(target=self._ws_loop, daemon=True).start()
            threading.Thread(target=self._vision_loop, daemon=True).start()

    def _get_analyzer(self) -> RobotVisionAnalyzer:
        if self._analyzer is None:
            self._analyzer = RobotVisionAnalyzer(self.source_dir)
        return self._analyzer

    def _load_runtime_events(self) -> None:
        if not self.runtime_events_file.is_file():
            return
        try:
            items = json.loads(self.runtime_events_file.read_text(encoding="utf-8"))
            if isinstance(items, list):
                self._live_events = [it for it in items if isinstance(it, dict) and EVENT_ID_RE.fullmatch(str(it.get("event_id", "")))][:50]
                if self._live_events:
                    self._latest_alert = self._live_events[0]
        except Exception:
            pass

    def _save_runtime_event(self, det: dict[str, Any], frame_bgr: np.ndarray) -> dict[str, Any] | None:
        try:
            self.runtime_captures_dir.mkdir(parents=True, exist_ok=True)
            event_id = uuid.uuid4().hex
            now = datetime.now()
            h, w = frame_bgr.shape[:2]
            x1, y1, x2, y2 = det["box"]
            x1, y1 = max(0, int(x1)), max(0, int(y1))
            x2, y2 = min(w, int(x2)), min(h, int(y2))
            if x2 <= x1 or y2 <= y1:
                crop = frame_bgr.copy()
            else:
                crop = frame_bgr[y1:y2, x1:x2].copy()

            overview = self._get_analyzer()._draw_annotations(frame_bgr, [det])
            detail_path = self.runtime_captures_dir / f"{event_id}_detail.jpg"
            overview_path = self.runtime_captures_dir / f"{event_id}_overview.jpg"
            cv2.imwrite(str(detail_path), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(str(overview_path), overview, [cv2.IMWRITE_JPEG_QUALITY, 91])

            record = {
                "id": 1000 + len(self._live_events) + 1,
                "event_id": event_id,
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                "item": det["label"],
                "abnormal": det["abnormal"],
                "match_score": float(det["score"]),
                "detail_url": f"/api/robot/captures/detail/{event_id}.jpg",
                "overview_url": f"/api/robot/captures/overview/{event_id}.jpg",
                "source": "live_web",
            }
            with self._lock:
                self._live_events.insert(0, record)
                self._live_events = self._live_events[:50]
                self._latest_alert = record
                tmp = self.runtime_events_file.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self._live_events, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.runtime_events_file)
            return record
        except Exception:
            LOGGER.exception("Failed to save live robot detection event")
            return None

    def configure(self, *, mode: str | None = None, stream_enabled: bool | None = None) -> dict[str, Any]:
        with self._lock:
            if mode is not None and mode in ANALYSIS_MODES:
                self.analysis_mode = mode
                self._confirm_streak.clear()
                if mode == "off":
                    self._vision_detections = []
                    self._vision_summary = "분석 꺼짐 (원본 영상 표시)"
                    self._annotated_jpeg = self._raw_jpeg
            if stream_enabled is not None:
                self.stream_enabled = bool(stream_enabled)
                if not self.stream_enabled:
                    self._video_state = "paused"
                    self._video_message = "웹 영상 연결 일시정지됨 (외부 PC 조종기 접속 가능)"
            self._frame_cond.notify_all()
        return self.snapshot()

    def _ws_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                enabled = self.stream_enabled
            if not enabled:
                self._trigger_ssh_poll_if_due()
                time.sleep(0.5)
                continue

            stat_ws: SimpleWebSocket | None = None
            vid_ws: SimpleWebSocket | None = None
            try:
                if not self.monitor_key:
                    raise RuntimeError("CAR_MONITOR_KEY 환경변수 미설정")
                auth_headers = {"X-Monitor-Key": self.monitor_key}
                stat_ws = SimpleWebSocket(
                    self.host,
                    self.port,
                    "/monitor/status",
                    timeout=3.0,
                    headers=auth_headers,
                )
                vid_ws = SimpleWebSocket(
                    self.host,
                    self.port,
                    "/monitor/video",
                    timeout=3.0,
                    headers=auth_headers,
                )
                with self._lock:
                    self._video_state = "streaming"
                    self._video_message = "실시간 관제 영상 수신 중 (/monitor/video · 조종 PC와 동시 작동)"

                stop_video = threading.Event()

                def video_reader():
                    try:
                        while not stop_video.is_set() and not self._stop_event.is_set():
                            with self._lock:
                                if not self.stream_enabled:
                                    break
                            opcode, payload = vid_ws.recv()
                            if opcode == 0x2 and len(payload) > 8:
                                seq = struct.unpack("!Q", payload[:8])[0]
                                jpeg_bytes = payload[8:]
                                arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                                bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                                if bgr is not None:
                                    now = time.monotonic()
                                    with self._lock:
                                        self._raw_jpeg = jpeg_bytes
                                        if self.analysis_mode == "off":
                                            self._annotated_jpeg = jpeg_bytes
                                        self._latest_bgr = bgr
                                        self._latest_seq = seq
                                        self._latest_frame_time = now
                                        self._frame_cond.notify_all()
                    except Exception:
                        stop_video.set()

                vt = threading.Thread(target=video_reader, daemon=True)
                vt.start()

                while not stop_video.is_set() and not self._stop_event.is_set():
                    with self._lock:
                        if not self.stream_enabled:
                            break
                    opcode, payload = stat_ws.recv()
                    if opcode == 0x1:
                        status = json.loads(payload.decode("utf-8"))
                        direction = str(status.get("direction", "STOP"))
                        reason = str(status.get("reason", "WAITING"))
                        distance_cm = status.get("distance_cm")
                        pc_connected = bool(status.get("pc_connected", False))
                        control_fresh = bool(status.get("control_fresh", False))
                        with self._lock:
                            self._telemetry = {
                                "online": True,
                                "host": self.host,
                                "port": self.port,
                                "direction": direction,
                                "direction_label": DIRECTION_LABELS.get(direction, direction),
                                "pwm": status.get("pwm", 0),
                                "wheel_pwm": status.get("wheel_pwm", [0, 0, 0, 0]),
                                "rpm": status.get("rpm", [0, 0, 0, 0]),
                                "distance_cm": distance_cm,
                                "obstacle_warning": bool(distance_cm is not None and distance_cm <= 30.0),
                                "pc_connected": pc_connected,
                                "control_fresh": control_fresh,
                                "reason": reason,
                                "reason_label": REASON_LABELS.get(reason, reason),
                                "motor_error": status.get("motor_error", ""),
                                "camera_error": status.get("camera_error", ""),
                                "dht22": status.get("dht22"),
                                "mq135": status.get("mq135"),
                                "diagnostics": self._telemetry.get("diagnostics", {}),
                                "sampled_at": datetime.now(timezone.utc).isoformat(),
                            }
                stop_video.set()
                vt.join(timeout=1.0)
            except Exception as exc:
                with self._lock:
                    if self.stream_enabled:
                        self._video_state = "reconnecting"
                        self._video_message = f"로봇 관제 영상 연결 재시도 중 ({exc})"
                self._trigger_ssh_poll_if_due()
                time.sleep(2.0)
            finally:
                if vid_ws is not None:
                    vid_ws.close()
                if stat_ws is not None:
                    stat_ws.close()

    def _vision_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                while (
                    not self._stop_event.is_set()
                    and (self._latest_bgr is None or self._latest_seq == self._analyzed_seq or self.analysis_mode == "off")
                ):
                    self._frame_cond.wait(timeout=0.5)
                if self._stop_event.is_set():
                    return
                if self._latest_bgr is None or self.analysis_mode == "off":
                    continue
                frame = self._latest_bgr.copy()
                seq = self._latest_seq
                mode = self.analysis_mode
                self._analyzed_seq = seq

            t0 = time.monotonic()
            try:
                analyzer = self._get_analyzer()
                dets, annotated_bgr = analyzer.analyze(frame, mode=mode)
                elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
                ok, enc = cv2.imencode(".jpg", annotated_bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
                annotated_bytes = enc.tobytes() if ok else None

                counts = Counter(d["label"] for d in dets)
                summary = ", ".join(f"{k} {v}건" for k, v in counts.items()) if counts else "탐지된 대상 없음"

                now = time.monotonic()
                to_save: dict[str, Any] | None = None
                with self._lock:
                    if annotated_bytes:
                        self._annotated_jpeg = annotated_bytes
                    self._vision_detections = [
                        {
                            "label": d["label"],
                            "abnormal": d["abnormal"],
                            "score": d["score"],
                            "box": list(d["box"]),
                        }
                        for d in dets
                    ]
                    self._vision_summary = summary
                    self._vision_ms = elapsed_ms
                    self._vision_fps = round(1000.0 / max(1.0, elapsed_ms), 1)

                    seen_labels = {d["label"] for d in dets}
                    for k in list(self._confirm_streak.keys()):
                        if k not in seen_labels:
                            self._confirm_streak[k] = 0
                    for d in dets:
                        lbl = d["label"]
                        self._confirm_streak[lbl] = self._confirm_streak.get(lbl, 0) + 1
                        last_saved = self._last_saved_by_label.get(lbl, 0.0)
                        if self._confirm_streak[lbl] >= 2 and (now - last_saved) >= 15.0:
                            self._last_saved_by_label[lbl] = now
                            to_save = d
                            break
                if to_save is not None:
                    self._save_runtime_event(to_save, frame)
            except Exception:
                LOGGER.exception("Robot vision analysis error")
            time.sleep(0.15)

    def _trigger_ssh_poll_if_due(self) -> None:
        if not self.poll_remote:
            return
        now = time.monotonic()
        with self._lock:
            if self._ssh_polling or (now - self._last_ssh_poll < 2.5):
                return
            self._ssh_polling = True
            self._last_ssh_poll = now
        threading.Thread(target=self._ssh_poll_worker, daemon=True).start()

    def _ssh_poll_worker(self) -> None:
        try:
            data = _ssh_read_robot_state(self.host, self.user, self.password)
            with self._lock:
                # Preserve dht22/mq135 if we had them recently
                if self._telemetry.get("dht22") and not data.get("dht22"):
                    data["dht22"] = self._telemetry["dht22"]
                if self._telemetry.get("mq135") and not data.get("mq135"):
                    data["mq135"] = self._telemetry["mq135"]
                self._telemetry = data
        except Exception as exc:
            with self._lock:
                self._telemetry = {
                    **self._telemetry,
                    "online": False,
                    "reason": "OFFLINE",
                    "reason_label": f"로봇 연결 대기 ({self.host})",
                    "error": str(exc)[:120],
                    "sampled_at": datetime.now(timezone.utc).isoformat(),
                }
        finally:
            with self._lock:
                self._ssh_polling = False

    def load_history(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        with self._lock:
            records.extend(dict(r) for r in self._live_events)

        if self.db_path.is_file():
            try:
                conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                try:
                    cur = conn.cursor()
                    rows = cur.execute(
                        "SELECT id, event_id, event_date, event_time, item, abnormal, photo_path, overview_path, match_score "
                        "FROM inspection_history ORDER BY id DESC LIMIT 100"
                    ).fetchall()
                finally:
                    conn.close()
                seen_ids = {r["event_id"] for r in records}
                for row_id, event_id, ev_date, ev_time, item, abnormal, photo_path, overview_path, score in rows:
                    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id) or event_id in seen_ids:
                        continue
                    records.append({
                        "id": int(row_id),
                        "event_id": event_id,
                        "timestamp": f"{ev_date} {ev_time}",
                        "item": str(item),
                        "abnormal": str(abnormal),
                        "match_score": round(float(score), 4),
                        "detail_url": f"/api/robot/captures/detail/{event_id}.jpg",
                        "overview_url": f"/api/robot/captures/overview/{event_id}.jpg",
                        "source": "db",
                    })
            except Exception:
                pass
        counts = Counter(r["item"] for r in records)
        abnormal_count = sum(1 for r in records if r["abnormal"] == "이상")
        normal_count = sum(1 for r in records if r["abnormal"] == "정상")
        return {
            "total_count": len(records),
            "abnormal_count": abnormal_count,
            "normal_count": normal_count,
            "by_item": dict(counts),
            "records": records,
        }

    def resolve_capture_bytes(self, kind: str, event_id: str) -> bytes | None:
        if kind not in {"detail", "overview"} or not EVENT_ID_RE.fullmatch(event_id):
            return None
        runtime_file = self.runtime_captures_dir / f"{event_id}_{kind}.jpg"
        if runtime_file.is_file():
            return runtime_file.read_bytes()
        if not self.db_path.is_file():
            return None
        column = "photo_path" if kind == "detail" else "overview_path"
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    f"SELECT {column} FROM inspection_history WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
            finally:
                conn.close()
            if not row or not row[0]:
                return None
            rel = str(row[0]).replace("\\", "/")
            base = self.pc_code_dir.resolve()
            target = (base / rel).resolve()
            if not str(target).startswith(str(base) + os.sep) or not target.is_file():
                return None
            return target.read_bytes()
        except Exception:
            return None

    def get_jpeg_frame(self, overlay: bool = True) -> bytes:
        with self._lock:
            return self._annotated_jpeg if overlay else self._raw_jpeg

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            video_online = (
                self._video_state == "streaming"
                and self._latest_frame_time > 0
                and (time.monotonic() - self._latest_frame_time) < 3.0
            )
            telemetry = {
                **self._telemetry,
                "video_online": video_online,
                "video_state": self._video_state,
                "video_message": self._video_message,
                "stream_enabled": self.stream_enabled,
            }
            vision = {
                "mode": self.analysis_mode,
                "mode_label": ANALYSIS_MODES.get(self.analysis_mode, self.analysis_mode),
                "available_modes": ANALYSIS_MODES,
                "detections": list(self._vision_detections),
                "summary": self._vision_summary,
                "inference_ms": self._vision_ms,
                "fps": self._vision_fps,
                "latest_alert": self._latest_alert,
            }
        if not video_online:
            self._trigger_ssh_poll_if_due()
        history = self.load_history()
        if vision["latest_alert"] is None and history["records"]:
            vision["latest_alert"] = history["records"][0]
        return {
            "robot": telemetry,
            "vision": vision,
            "inspection": history,
        }


def register_robot_routes(app, monitor: RobotMonitor) -> None:
    @app.get("/api/robot/status")
    def robot_status():
        return jsonify(monitor.snapshot())

    @app.post("/api/robot/config")
    def robot_config():
        if not same_origin_request():
            abort(403)
        data = request.get_json(silent=True) or {}
        admin_token = os.environ.get("ROBOT_ADMIN_TOKEN", "").strip()
        is_external_tunnel = bool(request.headers.get("Cf-Connecting-Ip"))
        if admin_token or is_external_tunnel:
            auth_header = request.headers.get("Authorization", "")
            bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            candidate = (
                request.headers.get("X-Admin-Token")
                or bearer
                or str(data.get("admin_token") or "")
            ).strip()
            if not admin_token or not candidate or not hmac.compare_digest(candidate, admin_token):
                return jsonify(error="외부/관리자 설정 변경에는 유효한 관리자 인증 토큰(X-Admin-Token)이 필요합니다."), 403
        mode = data.get("mode")
        stream_enabled = data.get("stream_enabled")
        return jsonify(monitor.configure(mode=mode, stream_enabled=stream_enabled))

    @app.get("/stream/robot")
    def robot_stream():
        overlay = request.args.get("overlay", "1") != "0"

        def generate():
            while True:
                frame = monitor.get_jpeg_frame(overlay=overlay)
                if frame:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                time.sleep(0.12)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    @app.get("/api/robot/captures/<kind>/<event_id>.jpg")
    def robot_capture(kind: str, event_id: str):
        data = monitor.resolve_capture_bytes(kind, event_id)
        if data is None:
            abort(404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
