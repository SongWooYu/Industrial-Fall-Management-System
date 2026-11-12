"""Read-only monitor and inspection history viewer for the Safety Exploration Robot.

Does not modify or send control commands to the robot (192.168.1.206) or its source tree.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pty
import re
import select
import sqlite3
import threading
import time
from typing import Any

from flask import Response, abort, jsonify

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
    "STOP": "정지 상태 (조종 연결됨)",
    "RUNNING": "주행 중",
    "FRONT OBSTACLE": "전방 장애물 감지 · 자동 정지 (≤30cm)",
    "COMMAND TIMEOUT": "제어 신호 시간 초과 · 안전 정지",
    "VIDEO NOT READY": "카메라 프레임 대기 · 안전 정지",
    "DISTANCE NOT READY": "초음파 거리 센서 대기 · 안전 정지",
    "DISTANCE COMPLETE": "목표 거리 이동 완료 · 정지",
    "MOTOR ERROR": "모터 I2C 통신 확인 필요",
}


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
        "distance_cm": distance_cm,
        "obstacle_warning": bool(distance_cm is not None and distance_cm <= 30.0),
        "pc_connected": bool(state.get("pc_connected", False)),
        "control_fresh": bool(state.get("control_fresh", False)),
        "reason": reason,
        "reason_label": REASON_LABELS.get(reason, reason),
        "motor_error": state.get("motor_error", ""),
        "camera_error": state.get("camera_error", ""),
        "diagnostics": diag,
        "sampled_at": datetime.now(timezone.utc).isoformat(),
    }


class RobotMonitor:
    def __init__(self, project_root: Path | str, *, host: str = "192.168.1.206", user: str = "humanoid", password: str = "1234", poll_remote: bool = True):
        self.project_root = Path(project_root)
        self.pc_code_dir = self.project_root / "src/RaspberryPi5_Car/Rasp5Car_source/PC_code"
        self.db_path = self.pc_code_dir / "exploration_history.db"
        self.host = os.environ.get("ROBOT_CAR_HOST", host)
        self.user = os.environ.get("ROBOT_CAR_USER", user)
        self.password = os.environ.get("ROBOT_CAR_PASSWORD", password)
        self.poll_remote = poll_remote
        self._lock = threading.Lock()
        self._last_poll = 0.0
        self._polling = False
        self._telemetry: dict[str, Any] = {
            "online": False,
            "host": self.host,
            "port": 5000,
            "direction": "STOP",
            "direction_label": "확인 중",
            "pwm": 0,
            "distance_cm": None,
            "obstacle_warning": False,
            "pc_connected": False,
            "control_fresh": False,
            "reason": "OFFLINE",
            "reason_label": "로봇 상태 확인 중",
            "motor_error": "",
            "camera_error": "",
            "diagnostics": {},
            "sampled_at": None,
        }
        if self.poll_remote:
            self._trigger_poll_if_due()

    def _trigger_poll_if_due(self) -> None:
        if not self.poll_remote:
            return
        now = time.monotonic()
        with self._lock:
            if self._polling or (now - self._last_poll < 2.5):
                return
            self._polling = True
            self._last_poll = now
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self) -> None:
        try:
            data = _ssh_read_robot_state(self.host, self.user, self.password)
            with self._lock:
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
                self._polling = False

    def load_history(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
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
                for row_id, event_id, ev_date, ev_time, item, abnormal, photo_path, overview_path, score in rows:
                    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
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
        if kind not in {"detail", "overview"} or not EVENT_ID_RE.fullmatch(event_id) or not self.db_path.is_file():
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

    def snapshot(self) -> dict[str, Any]:
        self._trigger_poll_if_due()
        with self._lock:
            telemetry = dict(self._telemetry)
        return {
            "robot": telemetry,
            "inspection": self.load_history(),
        }


def register_robot_routes(app, monitor: RobotMonitor) -> None:
    @app.get("/api/robot/status")
    def robot_status():
        return jsonify(monitor.snapshot())

    @app.get("/api/robot/captures/<kind>/<event_id>.jpg")
    def robot_capture(kind: str, event_id: str):
        data = monitor.resolve_capture_bytes(kind, event_id)
        if data is None:
            abort(404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
