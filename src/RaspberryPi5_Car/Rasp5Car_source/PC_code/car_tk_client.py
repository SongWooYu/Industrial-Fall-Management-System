# 파일명: car_tk_client.py
# Tkinter 영상 표시 및 WebSocket 메카넘휠 제어

import io
import json
import struct
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from time import monotonic

import websocket
from PIL import Image, ImageTk


class Connection:
    def __init__(self, ip):
        self.ip = ip
        self.stop = threading.Event()
        self.lock = threading.Lock()

        self.control = None
        self.video = None

        self.direction = "STOP"
        self.pwm = 50.0
        self.frame_ack = -1
        self.display_time = 0.0
        self.pending_frame = None

        self.connected = False
        self.message = "연결 중..."
        self.server_status = {}

    def start(self):
        threading.Thread(target=self.control_loop, daemon=True).start()

    def control_loop(self):
        try:
            self.control = websocket.create_connection(
                f"ws://{self.ip}:5000/control",
                timeout=2,
                http_no_proxy=[self.ip]
            )

            hello = json.loads(self.control.recv())

            if hello.get("type") != "hello":
                raise RuntimeError("서버 응답 오류")

            with self.lock:
                self.connected = True
                self.message = "연결됨 — 영상 대기"

            threading.Thread(
                target=self.video_loop,
                args=(hello["token"],),
                daemon=True
            ).start()

            while not self.stop.is_set():
                with self.lock:
                    fresh = monotonic() - self.display_time < 1.0
                    direction = self.direction if fresh else "STOP"

                    if not fresh:
                        self.direction = "STOP"

                    data = {
                        "direction": direction,
                        "pwm": self.pwm,
                        "frame": self.frame_ack,
                    }

                self.control.send(json.dumps(data))
                response = json.loads(self.control.recv())

                with self.lock:
                    self.server_status = response
                    self.message = response.get("reason", "연결됨")

                    # 오류 상태에서는 버튼을 다시 눌러야 이동
                    if response.get("reason") in (
                        "VIDEO NOT READY",
                        "COMMAND TIMEOUT",
                        "ENCODER FAULT: RELEASE BUTTON",
                        "CONTROL DELAY",
                        "MOTOR ERROR",
                    ):
                        self.direction = "STOP"

                self.stop.wait(0.1)

        except Exception as error:
            with self.lock:
                self.message = f"연결 종료: {error}"

        finally:
            self.stop.set()

            with self.lock:
                self.connected = False
                self.direction = "STOP"

            if self.control is not None:
                try:
                    self.control.send(json.dumps({
                        "direction": "STOP", "pwm": 0, "frame": -1
                    }))
                except Exception:
                    pass
                self.control.close()

    def video_loop(self, token):
        try:
            self.video = websocket.create_connection(
                f"ws://{self.ip}:5000/video?token={token}",
                timeout=2,
                http_no_proxy=[self.ip]
            )

            while not self.stop.is_set():
                packet = self.video.recv()

                if not isinstance(packet, bytes) or len(packet) <= 8:
                    continue

                seq = struct.unpack("!Q", packet[:8])[0]

                # 영상 해독은 통신 스레드에서 처리
                with Image.open(io.BytesIO(packet[8:])) as source:
                    image = source.convert("RGB")
                    image.thumbnail((640, 480))

                with self.lock:
                    # 오래된 영상은 쌓지 않고 최신 영상으로 교체
                    self.pending_frame = (seq, image)

        except Exception as error:
            with self.lock:
                self.message = f"영상 연결 종료: {error}"
                self.direction = "STOP"

            self.stop.set()

        finally:
            if self.video is not None:
                self.video.close()


class CarApp:
    def __init__(self, root):
        self.root = root
        self.connection = None
        self.photo = None

        root.title("Raspberry Pi 5 메카넘휠 제어")
        root.geometry("850x800")

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="서버 IP:").pack(side="left")

        self.ip = tk.StringVar(value="192.168.0.112")
        ttk.Entry(top, textvariable=self.ip, width=18).pack(
            side="left", padx=5
        )

        ttk.Button(top, text="연결", command=self.connect).pack(
            side="left", padx=3
        )
        ttk.Button(top, text="연결 해제", command=self.disconnect).pack(
            side="left", padx=3
        )

        self.video_label = tk.Label(
            root,
            text="라즈베리파이 서버에 연결해 주세요.",
            bg="black",
            fg="white"
        )
        self.video_label.pack(fill="both", expand=True, padx=10, pady=5)

        speed_frame = ttk.Frame(root, padding=5)
        speed_frame.pack(fill="x")

        self.speed = tk.DoubleVar(value=50)
        self.speed_text = tk.StringVar(value="기본 PWM: 50%")

        ttk.Label(speed_frame, textvariable=self.speed_text).pack(
            side="left", padx=10
        )
        ttk.Scale(
            speed_frame,
            from_=0,
            to=100,
            variable=self.speed,
            command=self.change_speed
        ).pack(side="left", fill="x", expand=True, padx=10)

        pad = ttk.Frame(root, padding=5)
        pad.pack()

        buttons = [
            ("좌회전", "TURN_LEFT", 0, 0),
            ("전진", "FORWARD", 0, 1),
            ("우회전", "TURN_RIGHT", 0, 2),
            ("왼쪽 이동", "LEFT", 1, 0),
            ("정지", "STOP", 1, 1),
            ("오른쪽 이동", "RIGHT", 1, 2),
            ("후진", "BACKWARD", 2, 1),
        ]

        for text, direction, row, column in buttons:
            button = tk.Button(
                pad, text=text, width=14, height=2,
                bg="#e85d5d" if direction == "STOP" else "#e5edf5"
            )
            button.grid(row=row, column=column, padx=4, pady=3)

            button.bind(
                "<ButtonPress-1>",
                lambda event, d=direction: self.drive(d)
            )
            button.bind("<Leave>", lambda event: self.drive("STOP"))

        root.bind_all(
            "<ButtonRelease-1>", lambda event: self.drive("STOP")
        )
        root.bind("<FocusOut>", self.focus_out)
        root.bind("<space>", lambda event: self.drive("STOP"))

        self.info = tk.StringVar(value="연결 안 됨")
        ttk.Label(root, textvariable=self.info, padding=8).pack()

        ttk.Label(
            root,
            text="방향 버튼을 누르는 동안 이동 / 놓으면 정지 / Space: 정지"
        ).pack(pady=3)

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.update_screen)

    def connect(self):
        if self.connection is not None:
            messagebox.showinfo(
                "연결", "기존 연결을 해제한 뒤 다시 연결해 주세요."
            )
            return

        ip = self.ip.get().strip()

        if not ip:
            return

        self.connection = Connection(ip)
        self.connection.pwm = self.speed.get()
        self.connection.start()

    def disconnect(self):
        connection = self.connection

        if connection is not None:
            with connection.lock:
                connection.direction = "STOP"
            connection.stop.set()

        self.connection = None
        self.video_label.configure(image="", text="연결 해제")
        self.photo = None
        self.info.set("연결 안 됨")

    def drive(self, direction):
        connection = self.connection

        if connection is None:
            return

        with connection.lock:
            ready = (
                connection.connected
                and not connection.stop.is_set()
                and monotonic() - connection.display_time < 1.0
            )

            connection.direction = direction if ready else "STOP"

    def change_speed(self, value):
        pwm = round(float(value))
        self.speed_text.set(f"기본 PWM: {pwm}%")

        if self.connection is not None:
            with self.connection.lock:
                self.connection.pwm = pwm

                if pwm == 0:
                    self.connection.direction = "STOP"

    def focus_out(self, event):
        # 다른 앱으로 전환하거나 창을 최소화하면 정지
        self.root.after(50, self.check_focus)

    def check_focus(self):
        if self.root.focus_displayof() is None:
            self.drive("STOP")

    def update_screen(self):
        connection = self.connection

        if connection is not None:
            with connection.lock:
                frame = connection.pending_frame
                connection.pending_frame = None
                snapshot = dict(connection.server_status)
                message = connection.message

            if frame is not None:
                seq, image = frame
                self.photo = ImageTk.PhotoImage(image)
                self.video_label.configure(image=self.photo, text="")

                # 실제 Tkinter 화면에 올린 프레임 번호를 서버에 회신
                with connection.lock:
                    connection.frame_ack = seq
                    connection.display_time = monotonic()

            distance = snapshot.get("distance_cm")
            distance_text = (
                "---" if distance is None else f"{distance:.1f}"
            )

            self.info.set(
                f"{message}  |  "
                f"방향: {snapshot.get('direction', 'STOP')}  |  "
                f"평균 PWM: {snapshot.get('pwm', 0)}%  |  "
                f"전방: {distance_text}cm"
            )

        self.root.after(50, self.update_screen)

    def close(self):
        self.disconnect()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = CarApp(root)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\n클라이언트를 종료합니다.")
        app.close()