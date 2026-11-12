"""Read-only telemetry, on-demand raw WebSocket video relay, and inspection event store for the Safety Exploration Robot.

- Connects to 192.168.1.206:5000 (/monitor/video and /monitor/status with X-Monitor-Key)
  completely independently of the Windows controller PC (/control and /video).
- Does NOT run any Edge AI re-analysis on robot camera frames and does NOT generate
  synthetic live_web events.
- Connects to /monitor/video ONLY while at least one web operator has the Robot tab open,
  and closes the /monitor/video WebSocket 10 seconds after the last viewer leaves.
- Stores confirmed inspection events uploaded by the Windows exploration client
  (or future robot event producer) in SQLite with strict auth, deduplication, and image validation.
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing
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
import socket
import sqlite3
import struct
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from typing import Any

import cv2
import numpy as np
from flask import Response, abort, jsonify, request

from .config import _load_dotenv
from .request_origin import same_origin_request
from .robot_vision import ANALYSIS_MODES, RobotVisionAnalyzer
from .robot_ws import (
    ANOMALY_TARGETS,
    DEFECT_TARGETS,
    VALID_INSPECTION_ITEMS,
    SimpleWebSocket,
)

LOGGER = logging.getLogger(__name__)
EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
MONITOR_STATUS_PATH = "/monitor/status"
MONITOR_VIDEO_PATH = "/monitor/video"

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
    "WAITING": "조종 PC 대기 중",
    "STOP": "정지 상태",
    "RUNNING": "주행 중",
    "FRONT OBSTACLE": "전방 장애물 정지 (≤30cm)",
    "COMMAND TIMEOUT": "제어 신호 시간 초과 정지",
    "VIDEO NOT READY": "카메라 프레임 대기 정지",
    "DISTANCE NOT READY": "초음파 센서 대기 정지",
    "DISTANCE COMPLETE": "목표 거리 이동 완료",
    "MOTOR ERROR": "모터 I2C 통신 점검 필요",
}

SOURCE_LABELS = {
    "windows_client": "Windows 탐사 클라이언트",
    "edge_analysis": "Edge 실시간 분석",
    "robot_event": "로봇 자체 탐사",
    "legacy_import": "기존 DB 보관본",
}


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _format_score_info(item: str, score: float) -> tuple[str, str]:
    if item in DEFECT_TARGETS:
        return "sift_strength", f"사진 비교 일치 강도 {score:.2f} (안전 확률 아님)"
    return "yolo_conf", f"YOLO 탐지 신뢰도 {score * 100.0:.1f}% (안전 판정 아님)"


def _evaluate_dht22(online: bool, dht: Any) -> dict[str, Any]:
    if not online:
        return {"state": "offline", "label": "오프라인 (측정 중단)", "detail": "로봇 서버 연결 끊김"}
    if not isinstance(dht, dict) or dht.get("connected") is False or dht.get("error"):
        err = dht.get("error") if isinstance(dht, dict) else None
        return {"state": "error", "label": "측정 오류 (점검 필요)", "detail": str(err or "DHT22 응답 없음")}
    t = dht.get("temperature_c")
    h = dht.get("humidity_pct")
    age = dht.get("age_seconds")
    if age is None:
        age = dht.get("age")
    if not (_finite_number(t) and _finite_number(h) and -40.0 <= t <= 80.0 and 0.0 <= h <= 100.0):
        return {"state": "waiting", "label": "첫 측정 대기", "detail": "유효한 온습도 측정값 대기 중"}
    if not _finite_number(age) or age < 0.0 or age > 10.0:
        age_txt = f"{int(round(age))}초 전" if _finite_number(age) else "시간 미상"
        return {
            "state": "stale",
            "label": f"갱신 지연 ({age_txt})",
            "detail": f"마지막 측정: {t:.1f}°C / {h:.1f}% ({age_txt})",
        }
    return {
        "state": "ok",
        "label": f"{t:.1f}°C · {h:.1f}%",
        "detail": f"DHT22 정상 수신 ({age:.1f}초 전)",
    }


def _evaluate_mq135(online: bool, mq: Any) -> dict[str, Any]:
    if not online:
        return {"state": "offline", "label": "오프라인 (측정 중단)", "detail": "로봇 서버 연결 끊김"}
    if not isinstance(mq, dict) or mq.get("connected") is False:
        return {"state": "error", "label": "미연결 (ADC 점검)", "detail": "ADS1115 / MQ-135 연결 확인 필요"}
    if mq.get("error"):
        return {"state": "error", "label": "측정 오류 (센서 점검)", "detail": str(mq.get("error"))}
    age = mq.get("age_seconds")
    if age is None:
        age = mq.get("age")
    voltage = mq.get("voltage_v")
    if not _finite_number(age) or age < 0.0 or age > 10.0:
        return {"state": "stale", "label": "수신 지연 · 판정 불가", "detail": "최신 측정값이 10초 이상 지연됨"}
    if not _finite_number(voltage) or not (0.0 <= voltage <= 5.5):
        return {"state": "error", "label": "전압값 오류 · 판정 불가", "detail": "유효한 AO 전압 범위 초과"}
    if mq.get("warming_up") is True:
        return {
            "state": "warming_up",
            "label": f"{voltage:.3f} V · 예열 중 (판정 불가)",
            "detail": "MQ-135 예열 중이므로 가스 유무를 판정하지 않습니다.",
        }
    if mq.get("calibrated") is True and _finite_number(mq.get("ppm")) and mq["ppm"] >= 0:
        ppm = float(mq["ppm"])
        det = mq.get("detected")
        verdict = "기준 초과" if det is True else ("기준 미만" if det is False else "판정 불가")
        return {
            "state": "calibrated",
            "label": f"{ppm:.0f} ppm · {verdict}",
            "detail": f"AO 전압 {voltage:.3f} V · 보정값 기반 추정 농도",
        }
    return {
        "state": "uncalibrated",
        "label": f"{voltage:.3f} V · 미보정 (판정 불가)",
        "detail": "MQ-135 농도 미보정 상태(calibrated=false)이므로 유해가스 유무/정상을 단정하지 않습니다.",
    }


def _make_placeholder_jpeg(title: str, subtitle: str) -> bytes:
    canvas = np.zeros((480, 640, 3), dtype=np.uint8)
    canvas[:, :] = (24, 20, 18)
    cv2.rectangle(canvas, (20, 20), (620, 460), (65, 55, 48), 2)
    cv2.putText(canvas, title, (44, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.82, (235, 240, 245), 2, cv2.LINE_AA)
    cv2.putText(canvas, subtitle, (44, 265), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (160, 175, 190), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else b""


def _validate_jpeg_bytes(data: bytes) -> bool:
    if not isinstance(data, (bytes, bytearray)) or len(data) < 100 or len(data) > 1_500_000:
        return False
    if not (data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"):
        return False
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.ndim != 3:
        return False
    h, w = img.shape[:2]
    return 16 <= w <= 4096 and 16 <= h <= 4096


def _ssh_read_robot_state(host: str, user: str, password: str, timeout: float = 3.5) -> dict[str, Any]:
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=2",
        f"{user}@{host}",
        "cat ~/Desktop/Car3/car_state.json 2>/dev/null",
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
    return json.loads(state_match.group(0))


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
        idle_grace_sec: float = 10.0,
        max_viewers: int = 8,
    ):
        self.project_root = Path(project_root)
        _load_dotenv(self.project_root / ".env")
        self.source_dir = self.project_root / "src/RaspberryPi5_Car/Rasp5Car_source"
        self.pc_code_dir = self.source_dir / "PC_code"
        self.legacy_db_path = self.pc_code_dir / "exploration_history.db"

        # Dedicated store for verified incoming events from Windows client or robot
        self.events_dir = self.project_root / "runtime/robot_events"
        self.events_captures_dir = self.events_dir / "captures"
        self.events_db_path = self.events_dir / "robot_events.db"
        self._init_events_db()

        self.host = os.environ.get("ROBOT_CAR_HOST", host)
        self.port = int(os.environ.get("ROBOT_CAR_PORT", str(port)))
        self.user = os.environ.get("ROBOT_CAR_USER", user)
        self.password = os.environ.get("ROBOT_CAR_PASSWORD", password)
        self.monitor_key = os.environ.get("CAR_MONITOR_KEY", "").strip()
        self.poll_remote = poll_remote
        self.idle_grace_sec = float(idle_grace_sec)
        self.max_viewers = int(max_viewers)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._frame_cond = threading.Condition(self._lock)

        self._viewer_count: int = 0
        self._last_viewer_left_at: float = 0.0
        self._video_ws_connected: bool = False
        self._video_state: str = "idle"
        self._video_message: str = "대기 중 (로봇 관제 탭을 열면 실시간 영상을 연결합니다)"
        self._raw_jpeg: bytes = _make_placeholder_jpeg("ROBOT CAMERA", "Open Robot Monitor tab to stream")
        self._annotated_jpeg: bytes = self._raw_jpeg
        self._latest_seq: int = -1
        self._analyzed_seq: int = -1
        self._latest_frame_time: float = 0.0

        self._analysis_mode: str = "off"
        self._auto_record: bool = True
        self._analyzer: RobotVisionAnalyzer | None = None
        self._latest_detections: list[dict[str, Any]] = []
        self._vision_summary: str = "원본 영상 모드 (분석 끔)"
        self._vision_inference_ms: float | None = None
        self._confirm_streak: dict[str, int] = {}
        self._last_auto_saved_at: dict[str, float] = {}

        self._last_status_poll: float = 0.0
        self._status_polling: bool = False
        self._http_opener = build_opener(ProxyHandler({}))
        self._telemetry: dict[str, Any] = self._build_offline_telemetry("로봇 상태 확인 대기")

        if self.poll_remote:
            threading.Thread(target=self._video_ws_loop, daemon=True).start()
            threading.Thread(target=self._on_demand_analysis_loop, daemon=True).start()

    def _init_events_db(self) -> None:
        try:
            self.events_captures_dir.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.events_db_path, timeout=5)) as conn, conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS robot_inspection_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        event_date TEXT NOT NULL,
                        event_time TEXT NOT NULL,
                        item TEXT NOT NULL,
                        abnormal TEXT NOT NULL CHECK(abnormal IN ('이상', '정상')),
                        match_score REAL NOT NULL,
                        source TEXT NOT NULL,
                        detail_file TEXT NOT NULL,
                        overview_file TEXT NOT NULL,
                        received_at TEXT NOT NULL
                    )"""
                )
                # Migrate older CHECK constraint on source if needed
                sql_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='robot_inspection_events'"
                ).fetchone()
                if sql_row and "edge_analysis" not in (sql_row[0] or "") and "CHECK(source IN" in (sql_row[0] or ""):
                    conn.execute(
                        """CREATE TABLE robot_inspection_events_v2 (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            event_id TEXT NOT NULL UNIQUE,
                            event_date TEXT NOT NULL,
                            event_time TEXT NOT NULL,
                            item TEXT NOT NULL,
                            abnormal TEXT NOT NULL CHECK(abnormal IN ('이상', '정상')),
                            match_score REAL NOT NULL,
                            source TEXT NOT NULL,
                            detail_file TEXT NOT NULL,
                            overview_file TEXT NOT NULL,
                            received_at TEXT NOT NULL
                        )"""
                    )
                    conn.execute(
                        "INSERT INTO robot_inspection_events_v2 SELECT * FROM robot_inspection_events"
                    )
                    conn.execute("DROP TABLE robot_inspection_events")
                    conn.execute("ALTER TABLE robot_inspection_events_v2 RENAME TO robot_inspection_events")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_robot_events_ts "
                    "ON robot_inspection_events(event_date DESC, event_time DESC, id DESC)"
                )
        except Exception:
            LOGGER.exception("Failed to initialize robot_inspection_events SQLite DB")

    def _build_offline_telemetry(self, message: str, error: str = "") -> dict[str, Any]:
        return {
            "online": False,
            "host": self.host,
            "port": self.port,
            "direction": "STOP",
            "direction_label": "오프라인",
            "pwm": 0,
            "wheel_pwm": [0, 0, 0, 0],
            "rpm": [0, 0, 0, 0],
            "distance_cm": None,
            "obstacle_warning": False,
            "pc_connected": False,
            "control_fresh": False,
            "reason": "OFFLINE",
            "reason_label": message,
            "motor_error": "",
            "camera_error": "",
            "dht22": None,
            "dht22_eval": _evaluate_dht22(False, None),
            "mq135": None,
            "mq135_eval": _evaluate_mq135(False, None),
            "error": error,
            "sampled_at": datetime.now(timezone.utc).isoformat(),
        }

    def _format_telemetry_from_raw(self, status: dict[str, Any]) -> dict[str, Any]:
        direction = str(status.get("direction", "STOP"))
        reason = str(status.get("reason", "WAITING"))
        distance_cm = status.get("distance_cm")
        valid_dist = _finite_number(distance_cm) and distance_cm >= 0.0
        dist_val = round(float(distance_cm), 1) if valid_dist else None
        dht22 = status.get("dht22")
        mq135 = status.get("mq135")
        return {
            "online": True,
            "host": self.host,
            "port": int(status.get("port", self.port)),
            "direction": direction,
            "direction_label": DIRECTION_LABELS.get(direction, direction),
            "pwm": int(status.get("pwm", 0) or 0),
            "wheel_pwm": status.get("wheel_pwm", [0, 0, 0, 0]),
            "rpm": status.get("rpm", [0, 0, 0, 0]),
            "distance_cm": dist_val,
            "obstacle_warning": bool(dist_val is not None and dist_val <= 30.0),
            "pc_connected": bool(status.get("pc_connected", False)),
            "control_fresh": bool(status.get("control_fresh", False)),
            "reason": reason,
            "reason_label": REASON_LABELS.get(reason, reason),
            "motor_error": str(status.get("motor_error") or ""),
            "camera_error": str(status.get("camera_error") or ""),
            "dht22": dht22,
            "dht22_eval": _evaluate_dht22(True, dht22),
            "mq135": mq135,
            "mq135_eval": _evaluate_mq135(True, mq135),
            "error": "",
            "sampled_at": datetime.now(timezone.utc).isoformat(),
        }

    def subscribe_video(self) -> bool:
        with self._lock:
            if self._viewer_count >= self.max_viewers:
                return False
            self._viewer_count += 1
            if self._video_state == "idle":
                self._video_state = "connecting"
                self._video_message = "로봇 카메라 영상 연결 중..."
            self._wake_event.set()
            return True

    def unsubscribe_video(self) -> None:
        with self._lock:
            if self._viewer_count > 0:
                self._viewer_count -= 1
            if self._viewer_count == 0:
                self._last_viewer_left_at = time.monotonic()
            self._frame_cond.notify_all()
        self._wake_event.set()

    @property
    def viewer_count(self) -> int:
        with self._lock:
            return self._viewer_count

    @property
    def video_ws_connected(self) -> bool:
        with self._lock:
            return self._video_ws_connected

    def _should_keep_video_ws(self) -> bool:
        with self._lock:
            if self._viewer_count > 0:
                return True
            if self._last_viewer_left_at <= 0.0:
                return False
            return (time.monotonic() - self._last_viewer_left_at) < self.idle_grace_sec

    def _video_ws_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._should_keep_video_ws():
                with self._lock:
                    self._video_ws_connected = False
                    if self._viewer_count == 0:
                        self._video_state = "idle"
                        self._video_message = "대기 중 (로봇 관제 탭을 열면 실시간 영상을 연결합니다)"
                self._wake_event.wait(timeout=0.5)
                self._wake_event.clear()
                continue

            vid_ws: SimpleWebSocket | None = None
            try:
                if not self.monitor_key:
                    with self._lock:
                        self._video_ws_connected = False
                        self._video_state = "video_failed"
                        self._video_message = "영상 연결 실패 (CAR_MONITOR_KEY 미설정)"
                    time.sleep(2.0)
                    continue

                with self._lock:
                    if self._video_state not in ("streaming", "camera_error"):
                        self._video_state = "connecting"
                        self._video_message = "로봇 카메라 연결 중..."

                vid_ws = SimpleWebSocket(
                    self.host,
                    self.port,
                    "/monitor/video",
                    timeout=3.0,
                    headers={"X-Monitor-Key": self.monitor_key},
                )
                with self._lock:
                    self._video_ws_connected = True

                while not self._stop_event.is_set() and self._should_keep_video_ws():
                    try:
                        opcode, payload = vid_ws.recv()
                    except TimeoutError:
                        with self._lock:
                            cam_err = self._telemetry.get("camera_error", "")
                            age = (time.monotonic() - self._latest_frame_time) if self._latest_frame_time > 0 else 999.0
                            if cam_err or age > 3.5:
                                self._video_state = "camera_error"
                                self._video_message = f"카메라 오류 ({cam_err or '프레임 수신 지연'})"
                        continue

                    if opcode == 0x2 and len(payload) > 8:
                        seq = struct.unpack("!Q", payload[:8])[0]
                        jpeg_bytes = payload[8:]
                        now = time.monotonic()
                        with self._lock:
                            self._raw_jpeg = jpeg_bytes
                            self._latest_seq = seq
                            self._latest_frame_time = now
                            self._video_state = "streaming"
                            self._video_message = "실시간 원본 영상 수신 중 (640×480)"
                            self._frame_cond.notify_all()
            except PermissionError as exc:
                with self._lock:
                    self._video_ws_connected = False
                    self._video_state = "video_failed"
                    self._video_message = f"영상 연결 실패 ({exc})"
                    self._frame_cond.notify_all()
                time.sleep(2.5)
            except (ConnectionError, OSError) as exc:
                with self._lock:
                    self._video_ws_connected = False
                    online = bool(self._telemetry.get("online"))
                    cam_err = self._telemetry.get("camera_error", "")
                    if not online:
                        self._video_state = "robot_offline"
                        self._video_message = "로봇 오프라인 (연결 대기 중)"
                    elif cam_err:
                        self._video_state = "camera_error"
                        self._video_message = f"카메라 오류 ({cam_err})"
                    else:
                        self._video_state = "video_failed"
                        self._video_message = f"영상 연결 실패 ({exc})"
                    self._frame_cond.notify_all()
                time.sleep(2.0)
            except Exception as exc:
                with self._lock:
                    self._video_ws_connected = False
                    self._video_state = "video_failed"
                    self._video_message = f"영상 연결 실패 ({exc})"
                    self._frame_cond.notify_all()
                time.sleep(2.0)
            finally:
                with self._lock:
                    self._video_ws_connected = False
                if vid_ws is not None:
                    vid_ws.close()

    def _poll_status_if_due(self) -> None:
        if not self.poll_remote:
            return
        now = time.monotonic()
        with self._lock:
            if self._status_polling or (now - self._last_status_poll < 0.8):
                return
            self._status_polling = True
            self._last_status_poll = now
        threading.Thread(target=self._status_poll_worker, daemon=True).start()

    def _status_poll_worker(self) -> None:
        try:
            if self.monitor_key:
                req = Request(
                    f"http://{self.host}:{self.port}/monitor/status",
                    headers={"X-Monitor-Key": self.monitor_key},
                )
                with self._http_opener.open(req, timeout=1.8) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                telemetry = self._format_telemetry_from_raw(raw)
                with self._lock:
                    self._telemetry = telemetry
                    if telemetry["camera_error"] and self._video_state in ("streaming", "camera_error", "connecting"):
                        self._video_state = "camera_error"
                        self._video_message = f"카메라 오류 ({telemetry['camera_error']})"
                return

            raw = _ssh_read_robot_state(self.host, self.user, self.password)
            telemetry = self._format_telemetry_from_raw(raw)
            with self._lock:
                self._telemetry = telemetry
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                self._telemetry = self._build_offline_telemetry(
                    f"로봇 오프라인 ({self.host})",
                    error=str(exc)[:120],
                )
                if self._viewer_count > 0 and self._video_state != "streaming":
                    self._video_state = "robot_offline"
                    self._video_message = "로봇 오프라인 (서버 응답 없음)"
        finally:
            with self._lock:
                self._status_polling = False

    def ingest_event(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if not isinstance(payload, dict):
            raise ValueError("JSON 객체 형식이 필요합니다.")
        event_id = str(payload.get("event_id") or "").strip().lower()
        if not EVENT_ID_RE.fullmatch(event_id):
            raise ValueError("유효하지 않은 event_id입니다 (32자 hex 필요).")
        item = str(payload.get("item") or "").strip()
        if item not in VALID_INSPECTION_ITEMS:
            raise ValueError(f"허용되지 않은 탐사 항목입니다: {item}")
        if item == "안전모 미착용":
            item = "안전모 미 착용"
        abnormal = str(payload.get("abnormal") or "").strip()
        if abnormal not in {"이상", "정상"}:
            abnormal = "이상" if item in ANOMALY_TARGETS else "정상"
        source = str(payload.get("source") or "windows_client").strip()
        if source not in {"windows_client", "robot_event", "edge_analysis"}:
            raise ValueError("허용되지 않은 데이터 출처(source)입니다.")

        event_date = str(payload.get("event_date") or "").strip()
        event_time = str(payload.get("event_time") or "").strip()
        now_local = datetime.now()
        if not DATE_RE.fullmatch(event_date):
            event_date = now_local.strftime("%Y-%m-%d")
        if not TIME_RE.fullmatch(event_time):
            event_time = now_local.strftime("%H:%M:%S")

        try:
            match_score = float(payload.get("match_score", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("유효하지 않은 match_score입니다.") from exc
        if not math.isfinite(match_score) or not (0.0 <= match_score <= 1.0):
            raise ValueError("match_score는 0.0~1.0 범위여야 합니다.")

        with closing(sqlite3.connect(self.events_db_path, timeout=5)) as conn:
            existing = conn.execute(
                "SELECT id FROM robot_inspection_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing:
                return {"ok": True, "duplicate": True, "event_id": event_id, "id": existing[0]}, 200

        detail_b64 = payload.get("detail_jpeg_b64")
        overview_b64 = payload.get("overview_jpeg_b64")
        if not isinstance(detail_b64, str) or not isinstance(overview_b64, str):
            raise ValueError("detail_jpeg_b64 및 overview_jpeg_b64 이미지가 필요합니다.")
        try:
            detail_bytes = base64.b64decode(detail_b64, validate=True)
            overview_bytes = base64.b64decode(overview_b64, validate=True)
        except Exception as exc:
            raise ValueError("Base64 이미지 디코딩에 실패했습니다.") from exc

        if not _validate_jpeg_bytes(detail_bytes) or not _validate_jpeg_bytes(overview_bytes):
            raise ValueError("유효한 JPEG 이미지가 아닙니다.")

        self.events_captures_dir.mkdir(parents=True, exist_ok=True)
        detail_file = f"{event_id}_detail.jpg"
        overview_file = f"{event_id}_overview.jpg"
        detail_path = self.events_captures_dir / detail_file
        overview_path = self.events_captures_dir / overview_file
        received_at = datetime.now(timezone.utc).isoformat()

        detail_path.write_bytes(detail_bytes)
        overview_path.write_bytes(overview_bytes)
        try:
            with closing(sqlite3.connect(self.events_db_path, timeout=5)) as conn, conn:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO robot_inspection_events
                        (event_id, event_date, event_time, item, abnormal, match_score,
                         source, detail_file, overview_file, received_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        event_date,
                        event_time,
                        item,
                        abnormal,
                        round(match_score, 4),
                        source,
                        detail_file,
                        overview_file,
                        received_at,
                    ),
                )
                row_id = cur.lastrowid
        except Exception:
            detail_path.unlink(missing_ok=True)
            overview_path.unlink(missing_ok=True)
            raise

        return {"ok": True, "duplicate": False, "event_id": event_id, "id": row_id}, 201

    def load_history(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # 1. Load verified uploaded events (windows_client / robot_event)
        if self.events_db_path.is_file():
            try:
                with closing(sqlite3.connect(f"file:{self.events_db_path}?mode=ro", uri=True)) as conn:
                    rows = conn.execute(
                        "SELECT id, event_id, event_date, event_time, item, abnormal, match_score, source "
                        "FROM robot_inspection_events ORDER BY event_date DESC, event_time DESC, id DESC LIMIT 200"
                    ).fetchall()
                for row_id, event_id, ev_date, ev_time, item, abnormal, score, source in rows:
                    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
                        continue
                    seen_ids.add(event_id)
                    score_val = round(float(score), 4)
                    score_kind, score_label = _format_score_info(str(item), score_val)
                    src = str(source) if source in SOURCE_LABELS else "windows_client"
                    records.append({
                        "id": int(row_id),
                        "event_id": event_id,
                        "date": str(ev_date),
                        "time": str(ev_time),
                        "timestamp": f"{ev_date} {ev_time}",
                        "item": str(item),
                        "abnormal": str(abnormal),
                        "match_score": score_val,
                        "score_kind": score_kind,
                        "score_label": score_label,
                        "detail_url": f"/api/robot/captures/detail/{event_id}.jpg",
                        "overview_url": f"/api/robot/captures/overview/{event_id}.jpg",
                        "source": src,
                        "source_label": SOURCE_LABELS[src],
                    })
            except Exception:
                LOGGER.exception("Failed reading robot_inspection_events DB")

        # 2. Load legacy imported DB records (marked explicitly as source='legacy_import')
        if self.legacy_db_path.is_file():
            try:
                with closing(sqlite3.connect(f"file:{self.legacy_db_path}?mode=ro", uri=True)) as conn:
                    rows = conn.execute(
                        "SELECT id, event_id, event_date, event_time, item, abnormal, photo_path, overview_path, match_score "
                        "FROM inspection_history ORDER BY id DESC LIMIT 200"
                    ).fetchall()
                for row_id, event_id, ev_date, ev_time, item, abnormal, _photo_path, _overview_path, score in rows:
                    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id) or event_id in seen_ids:
                        continue
                    seen_ids.add(event_id)
                    score_val = round(float(score), 4)
                    score_kind, score_label = _format_score_info(str(item), score_val)
                    records.append({
                        "id": int(row_id),
                        "event_id": event_id,
                        "date": str(ev_date),
                        "time": str(ev_time),
                        "timestamp": f"{ev_date} {ev_time}",
                        "item": str(item),
                        "abnormal": str(abnormal),
                        "match_score": score_val,
                        "score_kind": score_kind,
                        "score_label": score_label,
                        "detail_url": f"/api/robot/captures/detail/{event_id}.jpg",
                        "overview_url": f"/api/robot/captures/overview/{event_id}.jpg",
                        "source": "legacy_import",
                        "source_label": SOURCE_LABELS["legacy_import"],
                    })
            except Exception:
                pass

        records.sort(key=lambda r: (r["date"], r["time"], r["id"]), reverse=True)
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
        # 1. Check uploaded events store first
        uploaded_file = self.events_captures_dir / f"{event_id}_{kind}.jpg"
        if uploaded_file.is_file():
            return uploaded_file.read_bytes()
        # 2. Check legacy exploration_history.db
        if not self.legacy_db_path.is_file():
            return None
        column = "photo_path" if kind == "detail" else "overview_path"
        try:
            with closing(sqlite3.connect(f"file:{self.legacy_db_path}?mode=ro", uri=True)) as conn:
                row = conn.execute(
                    f"SELECT {column} FROM inspection_history WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
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

    def _get_analyzer(self) -> RobotVisionAnalyzer:
        if self._analyzer is None:
            self._analyzer = RobotVisionAnalyzer(self.source_dir)
        return self._analyzer

    def set_analysis_config(self, mode: str | None = None, auto_record: bool | None = None) -> dict[str, Any]:
        with self._lock:
            if mode is not None and mode in ANALYSIS_MODES:
                self._analysis_mode = mode
                if mode == "off":
                    self._latest_detections = []
                    self._vision_summary = "원본 영상 모드 (분석 끔)"
                    self._vision_inference_ms = None
                    self._confirm_streak.clear()
                else:
                    self._vision_summary = f"{ANALYSIS_MODES[mode]} 실행 중"
            if auto_record is not None:
                self._auto_record = bool(auto_record)
            self._wake_event.set()
            self._frame_cond.notify_all()
        return self.snapshot()

    def _record_frame_detection(
        self,
        frame_bgr: np.ndarray,
        annotated_bgr: np.ndarray,
        det: dict[str, Any],
        source: str = "edge_analysis",
    ) -> dict[str, Any] | None:
        h, w = frame_bgr.shape[:2]
        x1, y1, x2, y2 = det.get("box", (0, 0, w, h))
        x1 = max(0, min(w - 2, int(x1)))
        y1 = max(0, min(h - 2, int(y1)))
        x2 = max(x1 + 2, min(w, int(x2)))
        y2 = max(y1 + 2, min(h, int(y2)))
        crop_bgr = frame_bgr[y1:y2, x1:x2]
        if crop_bgr.size == 0:
            crop_bgr = frame_bgr

        ok_det, det_buf = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        ok_ov, ov_buf = cv2.imencode(".jpg", annotated_bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not (ok_det and ok_ov):
            return None

        now_local = datetime.now()
        event_id = uuid.uuid4().hex
        item = str(det.get("label") or "사람")
        if item not in VALID_INSPECTION_ITEMS:
            item = "사람"
        abnormal = "이상" if item in ANOMALY_TARGETS else "정상"
        score = float(det.get("score") or 0.85)
        payload = {
            "event_id": event_id,
            "event_date": now_local.strftime("%Y-%m-%d"),
            "event_time": now_local.strftime("%H:%M:%S"),
            "item": item,
            "abnormal": abnormal,
            "match_score": min(1.0, max(0.0, score)),
            "source": source,
            "detail_jpeg_b64": base64.b64encode(det_buf.tobytes()).decode("ascii"),
            "overview_jpeg_b64": base64.b64encode(ov_buf.tobytes()).decode("ascii"),
        }
        res, _code = self.ingest_event(payload)
        return res

    def capture_current_scene(self, item_override: str | None = None) -> dict[str, Any]:
        with self._lock:
            raw_jpeg = self._raw_jpeg
            mode = self._analysis_mode
        if not raw_jpeg:
            raise ValueError("현재 수신된 로봇 카메라 프레임이 없습니다.")
        arr = np.frombuffer(raw_jpeg, dtype=np.uint8)
        frame_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame_bgr is None:
            raise ValueError("카메라 프레임을 디코딩할 수 없습니다.")
        analyzer = self._get_analyzer()
        eval_mode = mode if mode != "off" else "all"
        detections, annotated_bgr = analyzer.analyze(frame_bgr, mode=eval_mode)
        if detections:
            chosen = detections[0]
        else:
            h, w = frame_bgr.shape[:2]
            label = item_override if item_override in VALID_INSPECTION_ITEMS else "사람"
            chosen = {
                "label": label,
                "abnormal": "이상" if label in ANOMALY_TARGETS else "정상",
                "score": 0.80,
                "box": (int(w * 0.15), int(h * 0.15), int(w * 0.85), int(h * 0.85)),
            }
        res = self._record_frame_detection(frame_bgr, annotated_bgr, chosen, source="edge_analysis")
        if not res:
            raise ValueError("캡처 이미지 저장에 실패했습니다.")
        return res

    def _on_demand_analysis_loop(self) -> None:
        last_seq = -1
        while not self._stop_event.is_set():
            with self._lock:
                mode = self._analysis_mode
                auto_rec = self._auto_record
                seq = self._latest_seq
                raw_jpeg = self._raw_jpeg
            if mode == "off" or not self._should_keep_video_ws() or seq < 0 or seq == last_seq:
                time.sleep(0.12)
                continue

            last_seq = seq
            try:
                arr = np.frombuffer(raw_jpeg, dtype=np.uint8)
                frame_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame_bgr is None:
                    continue
                t0 = time.monotonic()
                analyzer = self._get_analyzer()
                detections, annotated_bgr = analyzer.analyze(frame_bgr, mode=mode)
                elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
                ok, buf = cv2.imencode(".jpg", annotated_bgr, [cv2.IMWRITE_JPEG_QUALITY, 82])
                annotated_bytes = buf.tobytes() if ok else raw_jpeg

                now = time.monotonic()
                if detections:
                    top = detections[0]
                    summary = f"[{top['abnormal']}] {top['label']} ({int(top['score'] * 100)}%)"
                else:
                    summary = f"{ANALYSIS_MODES.get(mode, mode)} · 특이사항 없음"

                with self._lock:
                    self._annotated_jpeg = annotated_bytes
                    self._analyzed_seq = seq
                    self._latest_detections = detections
                    self._vision_summary = summary
                    self._vision_inference_ms = elapsed_ms
                    self._frame_cond.notify_all()

                if auto_rec and detections:
                    seen_labels = {d["label"] for d in detections}
                    for k in list(self._confirm_streak.keys()):
                        if k not in seen_labels:
                            self._confirm_streak[k] = 0
                    for det in detections:
                        lbl = det["label"]
                        self._confirm_streak[lbl] = self._confirm_streak.get(lbl, 0) + 1
                        last_saved = self._last_auto_saved_at.get(lbl, 0.0)
                        if self._confirm_streak[lbl] >= 2 and (now - last_saved) >= 25.0:
                            self._last_auto_saved_at[lbl] = now
                            self._record_frame_detection(frame_bgr, annotated_bgr, det, source="edge_analysis")
                else:
                    self._confirm_streak.clear()
            except Exception:
                LOGGER.exception("On-demand robot vision analysis error")
                time.sleep(0.3)

    def wait_for_frame(self, last_seq: int, timeout: float = 0.5, overlay: bool = False) -> tuple[int, bytes, str]:
        with self._lock:
            if self._latest_seq == last_seq:
                self._frame_cond.wait(timeout=timeout)
            use_annotated = overlay and self._analysis_mode != "off" and self._analyzed_seq >= 0
            frame = self._annotated_jpeg if use_annotated else self._raw_jpeg
            return self._latest_seq, frame, self._video_state

    def snapshot(self) -> dict[str, Any]:
        self._poll_status_if_due()
        with self._lock:
            now = time.monotonic()
            video_online = (
                self._video_ws_connected
                and self._video_state == "streaming"
                and self._latest_frame_time > 0.0
                and (now - self._latest_frame_time) < 3.5
            )
            telemetry = {
                **self._telemetry,
                "video_online": video_online,
                "video_state": self._video_state,
                "video_message": self._video_message,
                "viewers": self._viewer_count,
                "viewer_count": self._viewer_count,
                "video_ws_connected": self._video_ws_connected,
                "analysis_mode": self._analysis_mode,
                "analysis_mode_label": ANALYSIS_MODES.get(self._analysis_mode, self._analysis_mode),
                "analysis_active": self._analysis_mode != "off",
                "auto_record": self._auto_record,
                "vision_summary": self._vision_summary,
                "vision_inference_ms": self._vision_inference_ms,
                "detections": list(self._latest_detections),
            }
        history = self.load_history()
        return {
            "robot": telemetry,
            "inspection": history,
        }

    def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            self._frame_cond.notify_all()


def _verify_event_write_auth(monitor: RobotMonitor) -> bool:
    key = monitor.monitor_key
    if not key:
        return False
    auth_header = request.headers.get("Authorization", "")
    bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    candidate = (request.headers.get("X-Monitor-Key") or bearer or "").strip()
    return bool(candidate and hmac.compare_digest(candidate, key))


def _extract_request_socket() -> socket.socket | None:
    sock = request.environ.get("werkzeug.socket")
    if isinstance(sock, socket.socket):
        return sock
    stream = request.environ.get("wsgi.input")
    for attr in ("_sock", "raw"):
        inner = getattr(stream, attr, None)
        if isinstance(inner, socket.socket):
            return inner
        inner_sock = getattr(inner, "_sock", None)
        if isinstance(inner_sock, socket.socket):
            return inner_sock
    return None


def _is_socket_closed(sock: socket.socket | None) -> bool:
    if sock is None:
        return False
    try:
        r, _, _ = select.select([sock], [], [], 0.0)
        if not r:
            return False
        data = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        return len(data) == 0
    except BlockingIOError:
        return False
    except OSError:
        return True


def register_robot_routes(app, monitor: RobotMonitor) -> None:
    @app.get("/api/robot/status")
    def robot_status():
        return jsonify(monitor.snapshot())

    @app.post("/api/robot/events")
    def robot_ingest_event():
        if (request.content_length or 0) > 4 * 1024 * 1024:
            abort(413)
        if not _verify_event_write_auth(monitor):
            return jsonify(error="유효한 관제 인증키(X-Monitor-Key)가 필요합니다."), 401
        if request.mimetype != "application/json":
            abort(415)
        raw_body = request.get_data(cache=False)
        if len(raw_body) > 4 * 1024 * 1024:
            abort(413)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
            result, status_code = monitor.ingest_event(payload)
            return jsonify(result), status_code
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/robot/analysis")
    def robot_set_analysis():
        if not same_origin_request():
            abort(403)
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode")
        auto_record = payload.get("auto_record")
        if mode is not None and mode not in ANALYSIS_MODES:
            return jsonify(error="유효하지 않은 분석 모드입니다."), 400
        return jsonify(monitor.set_analysis_config(mode=mode, auto_record=auto_record))

    @app.post("/api/robot/capture-now")
    def robot_capture_now():
        if not same_origin_request():
            abort(403)
        payload = request.get_json(silent=True) or {}
        try:
            res = monitor.capture_current_scene(item_override=payload.get("item"))
            return jsonify({"ok": True, "capture": res, "status": monitor.snapshot()}), 201
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.get("/stream/robot")
    def robot_stream():
        # Do not allow stale unrefreshed pages requesting legacy ?overlay=... without ?live=1 to hold a viewer slot
        if "overlay" in request.args and request.args.get("live") != "1":
            return Response(
                _make_placeholder_jpeg("REFRESH PAGE", "Please refresh browser"),
                mimetype="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )
        req_mode = request.args.get("mode")
        if req_mode in ANALYSIS_MODES:
            monitor.set_analysis_config(mode=req_mode)
        use_overlay = request.args.get("analysis") == "1" or (req_mode is not None and req_mode != "off")
        if not monitor.subscribe_video():
            return jsonify(error="동시 로봇 영상 시청 한도에 도달했습니다."), 503

        client_sock = _extract_request_socket()
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                monitor.unsubscribe_video()

        def generate():
            last_seq = -1
            last_sent = 0.0
            try:
                while not monitor._stop_event.is_set():
                    if _is_socket_closed(client_sock):
                        break
                    seq, frame, _state = monitor.wait_for_frame(
                        last_seq,
                        timeout=0.25,
                        overlay=use_overlay or monitor._analysis_mode != "off",
                    )
                    if _is_socket_closed(client_sock):
                        break
                    now = time.monotonic()
                    if frame and (seq != last_seq or (now - last_sent) >= 0.5):
                        yield (
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(frame)).encode("ascii")
                            + b"\r\n\r\n"
                            + frame
                            + b"\r\n"
                        )
                        last_seq = seq
                        last_sent = now
            finally:
                release()

        response = Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "X-Accel-Buffering": "no",
            },
        )
        response.call_on_close(release)
        return response

    @app.get("/api/robot/captures/<kind>/<event_id>.jpg")
    def robot_capture(kind: str, event_id: str):
        data = monitor.resolve_capture_bytes(kind, event_id)
        if data is None:
            abort(404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
