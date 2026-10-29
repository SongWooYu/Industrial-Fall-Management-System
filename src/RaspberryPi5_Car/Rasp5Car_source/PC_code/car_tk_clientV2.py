# 파일명: car_tk_clientV2.py
# PC: pip install websocket-client Pillow
# 이미지: 이 파이썬 파일과 같은 폴더의 images/button_back1.png
import io
from contextlib import suppress
import json
import struct
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from time import monotonic

import websocket
from PIL import Image, ImageTk, ImageOps

IMAGE_PATH = Path(__file__).resolve().parent / 'images' / 'button_back1.png'
# 보내주신 430x480 이미지 기준 (명령, 표시 이름, x1, y1, x2, y2)
REGIONS = [
    ('FORWARD_LEFT', '좌측 전진', 10, 8, 143, 74),
    ('FORWARD', '전진', 149, 8, 281, 74),
    ('FORWARD_RIGHT', '우측 전진', 287, 8, 420, 74),
    ('LEFT', '왼쪽 이동', 10, 80, 143, 147),
    ('STOP', '정지', 149, 80, 281, 147),
    ('RIGHT', '오른쪽 이동', 287, 80, 420, 147),
    ('BACKWARD_LEFT', '좌측 후진', 10, 153, 143, 221),
    ('BACKWARD', '후진', 149, 153, 281, 221),
    ('BACKWARD_RIGHT', '우측 후진', 287, 153, 420, 221),
    ('TURN_LEFT', '제자리 좌회전', 10, 228, 212, 298),
    ('TURN_RIGHT', '제자리 우회전', 218, 228, 420, 298),
    ('CURVE_LEFT', '전진 곡선 · 왼쪽', 10, 305, 212, 379),
    ('CURVE_RIGHT', '전진 곡선 · 오른쪽', 218, 305, 420, 379),
    ('ARC_LEFT', '측면 이동 + 좌회전', 10, 386, 212, 469),
    ('ARC_RIGHT', '측면 이동 + 우회전', 218, 386, 420, 469),
]
NAMES = {r[0]: r[1] for r in REGIONS}


def hit_test(x, y):
    for region in REGIONS:
        if region[2] <= x < region[4] and region[3] <= y < region[5]:
            return region
    return None


class Connection:
    def __init__(self, ip, port, pwm):
        self.url = f'ws://{ip}:{port}'
        self.ip = ip
        self.lock = threading.Lock()
        self.ended = threading.Event()
        self.finished = threading.Event()
        self.direction = 'STOP'
        self.pwm = pwm
        self.frame_ack = -1
        self.display_time = 0.0
        self.ui_time = monotonic()
        self.connected = False
        self.pending_frame = None
        self.status = {}
        self.message = '연결 중...'
        threading.Thread(target=self.control_loop, daemon=True).start()

    def stop(self):
        with self.lock:
            self.direction = 'STOP'

    def close(self):
        self.stop()
        self.ended.set()

    def control_loop(self):
        ws = None
        video_thread = None
        try:
            ws = websocket.create_connection(self.url+'/control', timeout=2,
                                             http_no_proxy=[self.ip])
            hello = json.loads(ws.recv())
            if hello.get('version') != 2:
                raise RuntimeError('car_ws_serverV2.py를 실행해 주세요.')
            token = hello['token']
            with self.lock:
                self.connected = True
                self.message = '연결됨 / 영상 대기'
            video_thread = threading.Thread(target=self.video_loop, args=(token,), daemon=True)
            video_thread.start()
            while not self.ended.is_set():
                with self.lock:
                    now = monotonic()
                    fresh = now-self.display_time < 1 and now-self.ui_time < 0.4
                    if not fresh:
                        self.direction = 'STOP'
                    data = dict(direction=self.direction, pwm=self.pwm, frame=self.frame_ack)
                ws.send(json.dumps(data))
                result = json.loads(ws.recv())
                with self.lock:
                    self.status = result
                    self.message = result.get('reason', '')
                    if self.message not in ('RUNNING', 'STOP', 'WAITING'):
                        self.direction = 'STOP'
                self.ended.wait(0.1)
        except Exception as error:
            with self.lock:
                if not self.ended.is_set():
                    self.message = f'연결 오류: {error}'
        finally:
            self.ended.set()
            with self.lock:
                self.connected = False
                self.direction = 'STOP'
            if ws is not None:
                with suppress(Exception):
                    ws.send(json.dumps(dict(direction='STOP', pwm=0, frame=-1)))
                with suppress(Exception):
                    ws.close(timeout=0.3)
            if video_thread:
                video_thread.join(3)
            self.finished.set()

    def video_loop(self, token):
        ws = None
        try:
            ws = websocket.create_connection(self.url+'/video?token='+token,
                                             timeout=2, http_no_proxy=[self.ip])
            while not self.ended.is_set():
                data = ws.recv()
                if not data:
                    raise RuntimeError('영상 연결 종료')
                if isinstance(data, bytes) and len(data) > 8:
                    seq = struct.unpack('!Q', data[:8])[0]
                    with Image.open(io.BytesIO(data[8:])) as source:
                        frame = ImageOps.pad(source.convert('RGB'), (640, 480),
                                             method=Image.Resampling.BILINEAR, color='black')
                    with self.lock:
                        self.pending_frame = (seq, frame)
        except Exception as error:
            with self.lock:
                if not self.ended.is_set():
                    self.message = f'영상 오류: {error}'
                self.direction = 'STOP'
            self.ended.set()
        finally:
            if ws:
                with suppress(Exception):
                    ws.close(timeout=0.3)


class CarApp:
    def __init__(self, root):
        self.root = root
        self.connection = None
        self.closing = False
        self.held = None
        self.photo = None
        root.title('Raspberry Pi 5 메카넘휠 제어 V2')
        root.geometry('1140x720')
        root.resizable(False, False)
        root.configure(bg='#edf2f7')
        self.ip = tk.StringVar(value='192.168.0.112')
        self.port = tk.StringVar(value='5000')
        self.pwm = tk.DoubleVar(value=50)
        self.pwm_text = tk.StringVar(value='설정 PWM: 50%')
        self.info = tk.StringVar(value='동작: 정지  |  전방 거리: ---  |  실제 평균 PWM: 0%')
        self.rpm = tk.StringVar(value='M1: --- RPM    M2: --- RPM    M3: --- RPM    M4: --- RPM')
        self.note = tk.StringVar(value='접속 후 방향 버튼을 누르세요.')
        bar = ttk.Frame(root, padding=10)
        bar.place(x=20, y=10, width=1100, height=48)
        ttk.Label(bar, text='서버 IP').pack(side='left')
        ttk.Entry(bar, textvariable=self.ip, width=18).pack(side='left', padx=8)
        ttk.Label(bar, text='포트').pack(side='left')
        ttk.Entry(bar, textvariable=self.port, width=7).pack(side='left', padx=8)
        self.connect_button = ttk.Button(bar, text='접속', command=self.connect)
        self.connect_button.pack(side='left', padx=5)
        ttk.Button(bar, text='접속 해제', command=self.disconnect).pack(side='left', padx=5)
        self.connection_label = ttk.Label(bar, text='연결 안 됨', foreground='gray')
        self.connection_label.pack(side='left', padx=16)
        self.video = tk.Canvas(root, width=640, height=480, bg='black', highlightthickness=0)
        self.video.place(x=20, y=75)
        self.video_item = self.video.create_image(320, 240)
        self.video_text = self.video.create_text(320, 240, text='라즈베리파이 서버에 접속해 주세요.',
                                                fill='white', font=('맑은 고딕', 14))
        self.panel = tk.Canvas(root, width=430, height=480, highlightthickness=0, bg='white')
        self.panel.place(x=680, y=75)
        self.button_photo = None
        try:
            with Image.open(IMAGE_PATH) as source:
                if source.size != (430, 480):
                    raise ValueError(f'이미지가 {source.size}입니다. 430×480으로 저장해 주세요.')
                self.button_photo = ImageTk.PhotoImage(source.convert('RGB'))
            self.panel.create_image(0, 0, anchor='nw', image=self.button_photo)
        except (OSError, ValueError) as error:
            # 이미지가 없더라도 레이아웃과 제어를 확인할 수 있는 기본 표시
            for code, name, x1, y1, x2, y2 in REGIONS:
                self.panel.create_rectangle(x1, y1, x2, y2,
                                            fill='#ee2525' if code == 'STOP' else '#dcecf9', outline='#b6c5d1')
                self.panel.create_text((x1+x2)/2, (y1+y2)/2, text=name,
                                       fill='white' if code == 'STOP' else '#12304c', font=('맑은 고딕', 11, 'bold'))
            self.note.set(f'기본 버튼 표시 중: {error}')
        self.outline = self.panel.create_rectangle(0, 0, 0, 0, outline='#ff9c00', width=3, state='hidden')
        self.panel.bind('<ButtonPress-1>', self.press)
        self.panel.bind('<B1-Motion>', self.drag)
        self.panel.bind('<Leave>', lambda e: self.stop())
        root.bind_all('<ButtonRelease-1>', lambda e: self.stop())
        root.bind('<space>', lambda e: self.stop())
        root.bind('<FocusOut>', lambda e: root.after(50, self.check_focus))
        ttk.Label(root, textvariable=self.pwm_text).place(x=20, y=572)
        ttk.Scale(root, from_=0, to=100, variable=self.pwm, command=self.speed_change).place(x=160, y=572, width=500)
        ttk.Button(root, text='전체 정지 (Space)', command=self.stop).place(x=680, y=570, width=430, height=32)
        ttk.Label(root, textvariable=self.info, font=('맑은 고딕', 11)).place(x=20, y=614)
        ttk.Label(root, textvariable=self.rpm, font=('맑은 고딕', 11)).place(x=20, y=642)
        ttk.Label(root, textvariable=self.note, wraplength=1090).place(x=20, y=672)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(50, self.update)

    def connect(self):
        if self.connection and not self.connection.finished.is_set():
            messagebox.showinfo('연결', '기존 연결을 해제하고 종료될 때까지 잠시 기다려 주세요.')
            return
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535 or not self.ip.get().strip():
                raise ValueError()
        except ValueError:
            messagebox.showerror('입력 오류', '서버 IP와 포트 번호를 확인해 주세요.')
            return
        self.stop()
        self.video.itemconfigure(self.video_item, image='')
        self.video.itemconfigure(self.video_text, text='영상 연결 중...', state='normal')
        self.connection = Connection(self.ip.get().strip(), port, round(self.pwm.get()))

    def disconnect(self):
        self.stop()
        if self.connection:
            self.connection.close()
        self.video.itemconfigure(self.video_text, text='연결 해제 / 차량 정지', state='normal')

    def stop(self):
        self.held = None
        self.panel.itemconfigure(self.outline, state='hidden')
        if self.connection:
            self.connection.stop()

    def press(self, event):
        region = hit_test(event.x, event.y)
        self.stop()
        if not region or region[0] == 'STOP':
            return
        c = self.connection
        if c is None:
            return
        with c.lock:
            ready = (c.connected and not c.ended.is_set() and
                     monotonic()-c.display_time < 1 and c.pwm > 0)
            if ready:
                c.direction = region[0]
        if ready:
            self.held = region
            self.panel.coords(self.outline, *region[2:])
            self.panel.itemconfigure(self.outline, state='normal')

    def drag(self, event):
        if self.held and hit_test(event.x, event.y) != self.held:
            self.stop()  # 다른 버튼으로 드래그해도 재출발하지 않음

    def check_focus(self):
        if not self.closing and self.root.focus_displayof() is None:
            self.stop()

    def speed_change(self, value):
        pwm = round(float(value))
        self.pwm_text.set(f'설정 PWM: {pwm}%')
        if self.connection:
            with self.connection.lock:
                self.connection.pwm = pwm
        if pwm == 0:
            self.stop()

    def update(self):
        if self.closing:
            return
        c = self.connection
        if c:
            with c.lock:
                c.ui_time = monotonic()
                pending, c.pending_frame = c.pending_frame, None
                connected = c.connected and not c.ended.is_set()
                s, message, direction = dict(c.status), c.message, c.direction
                age = monotonic()-c.display_time
            if pending and connected:
                seq, frame = pending
                self.photo = ImageTk.PhotoImage(frame)
                self.video.itemconfigure(self.video_item, image=self.photo)
                self.video.itemconfigure(self.video_text, state='hidden')
                with c.lock:
                    c.frame_ack, c.display_time = seq, monotonic()
                age = 0
            if not connected or age > 1:
                self.stop()
                self.video.itemconfigure(self.video_text, state='normal',
                                         text='영상 없음 / 조작 정지' if connected else '연결 종료 / 조작 정지')
            elif direction == 'STOP' and self.held:
                self.stop()
            self.connection_label.configure(text='연결됨' if connected else '연결 안 됨',
                                            foreground='green' if connected else 'gray')
            self.connect_button.configure(state='normal' if c.finished.is_set() else 'disabled')
            distance = s.get('distance_cm')
            distance_text = '---' if distance is None else f'{distance:.1f} cm'
            self.info.set(f"동작: {NAMES.get(s.get('direction'), '정지')}  |  전방 거리: {distance_text}  |  실제 평균 PWM: {s.get('pwm', 0)}%")
            values = s.get('rpm', [0]*4)
            self.rpm.set('    '.join(f'M{i+1}: {value:.1f} RPM' for i, value in enumerate(values)))
            detail = s.get('motor_error') or s.get('camera_error') or message
            self.note.set(f'{detail}  |  누르는 동안 이동 · 놓거나 영역을 벗어나면 정지 · Space: 정지')
        self.root.after(50, self.update)

    def close(self):
        if self.closing:
            return
        self.stop()
        self.closing = True
        if self.connection:
            self.connection.close()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    app = CarApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.close()
        print('\n클라이언트를 종료합니다.')
