"""Read-only RFC 6455 WebSocket client and target constants for the Safety Exploration Robot.

Strictly used for receiving read-only JPEG video frames from /monitor/video.
Does not perform any Edge AI re-analysis or issue any control commands.
"""
from __future__ import annotations

import base64
import os
import socket
import struct

VALID_INSPECTION_ITEMS = frozenset({
    "사람",
    "개",
    "코끼리",
    "자전거",
    "오토바이",
    "트럭",
    "승용차",
    "바나나",
    "사과",
    "오렌지",
    "칼",
    "컵",
    "벽면 균열",
    "노출 철근",
    "누수 흔적",
    "안전모 미 착용",
    "안전모 미착용",
})

DEFECT_TARGETS = frozenset({"벽면 균열", "노출 철근", "누수 흔적"})
ANOMALY_TARGETS = DEFECT_TARGETS | {"안전모 미 착용", "안전모 미착용"}


class SimpleWebSocket:
    """Minimal RFC 6455 client over stdlib socket for read-only /monitor/video stream."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        timeout: float = 3.0,
        headers: dict[str, str] | None = None,
    ):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        extra_headers = ""
        if headers:
            for k, v in headers.items():
                extra_headers += f"{k}: {v}\r\n"
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"{extra_headers}\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(1024)
            if not chunk:
                self.close()
                raise ConnectionError("WebSocket handshake closed early")
            header += chunk
        head_bytes, self._buf = header.split(b"\r\n\r\n", 1)
        status_line = head_bytes.splitlines()[0].decode("latin1", errors="replace")
        if " 101 " not in status_line:
            self.close()
            if " 401 " in status_line or " 403 " in status_line:
                raise PermissionError(f"관제 API 인증 실패 ({status_line})")
            if " 409 " in status_line or " 429 " in status_line:
                raise PermissionError(f"관제 연결 제한 초과 ({status_line})")
            raise ConnectionError(f"WebSocket handshake failed: {status_line}")

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("WebSocket EOF")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> tuple[int, bytes]:
        while True:
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by server")
            if opcode == 0x9:
                # Respond with RFC 6455 PONG control frame (0xA)
                self._send_pong(payload)
                continue
            if opcode in (0x1, 0x2):
                return opcode, payload

    def _send_pong(self, payload: bytes) -> None:
        mask_key = os.urandom(4)
        n = min(len(payload), 125)
        payload = payload[:n]
        header = bytearray([0x8A, 0x80 | n])
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

