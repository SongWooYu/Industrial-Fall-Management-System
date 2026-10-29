MAX_PWM = 100  # 현재 Raspberry Pi V3 서버와 동일한 PWM 범위
# 파일명: car_tk_clientV3(20).py
# PC: py -m pip install websocket-client Pillow ultralytics opencv-python torch edge-tts
# 이미지: 이 파이썬 파일과 같은 폴더의 images/button_back1.png
import io
import os
import re
import logging
from logging.handlers import RotatingFileHandler
import base64
import asyncio
import ctypes
import subprocess
import sys
from collections import Counter
from contextlib import suppress
import json
import math
import sqlite3
import uuid
import struct
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from pathlib import Path
from time import monotonic

# pythonw.exe 실행에서도 오류 기록을 남기고 콘솔 없는 라이브러리 호출을 지원합니다.
LOGGER = logging.getLogger('car_client')
LOGGER.setLevel(logging.INFO)
try:
    log_dir = Path(__file__).resolve().parent / 'logs'
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'client.log', maxBytes=1000000,
                                  backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOGGER.addHandler(handler)
except OSError:
    LOGGER.addHandler(logging.NullHandler())
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = sys.stdout


import websocket
from PIL import Image, ImageTk, ImageOps, ImageDraw, ImageFont

AUDIO_PATH = Path(__file__).resolve().parent / 'sounds' / 'collision_warning.mp3'


def prepare_collision_audio():
    """최초 한 번 온라인 생성. 시작 시 백그라운드에서 준비한다."""
    if AUDIO_PATH.is_file() and AUDIO_PATH.stat().st_size > 0:
        return
    temporary = AUDIO_PATH.with_suffix('.tmp.mp3')
    try:
        import edge_tts
        AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
        async def generate():
            await edge_tts.Communicate('충돌 위험이 있어서 정지했습니다.',
                voice='ko-KR-SunHiNeural', rate='-5%').save(str(temporary))
        async def bounded():
            await asyncio.wait_for(generate(), timeout=25)
        asyncio.run(bounded())
        if temporary.stat().st_size == 0:
            raise RuntimeError('빈 음성 파일')
        temporary.replace(AUDIO_PATH)
        print('고음질 한국어 여성 음성 준비 완료: sounds/collision_warning.mp3')
    except Exception as error:
        with suppress(OSError):
            temporary.unlink()
        print(f'고음질 음성 준비 실패: {error} / 이번에는 Windows 기본 음성을 사용합니다.')


def play_cached_audio(path=AUDIO_PATH):
    if not path.is_file():
        return False
    mci = ctypes.windll.winmm.mciSendStringW
    mci.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    mci.restype = ctypes.c_uint
    alias = 'car_collision_voice'
    if mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None):
        return False
    try:
        return mci(f'play {alias} wait', None, 0, None) == 0
    finally:
        mci(f'close {alias}', None, 0, None)


_speech_lock = threading.Lock()


def speak_collision():
    """Windows의 설치된 한국어 여성 SAPI 음성을 사용. UI와 통신은 차단하지 않는다."""
    def worker():
        if sys.platform != 'win32' or not _speech_lock.acquire(blocking=False):
            return
        try:
            if play_cached_audio():
                return
            script = r"""
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voice = $speaker.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Gender -eq 'Female' -and
        $_.VoiceInfo.Culture.Name -like 'ko-*'
    } | Select-Object -First 1
    if ($null -eq $voice) { throw 'Korean female SAPI voice is not installed.' }
    $speaker.SelectVoice($voice.VoiceInfo.Name)
    $speaker.Rate = 0
    $speaker.Speak('충돌 위험이 있어서 정지 했습니다.')
} finally { $speaker.Dispose() }
"""
            encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                     '-EncodedCommand', encoded], capture_output=True,
                                    timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                print('음성 안내 실패: Windows 한국어 여성 음성 설치 상태를 확인하세요.')
        except Exception as error:
            print(f'음성 안내 오류: {error}')
        finally:
            _speech_lock.release()
    threading.Thread(target=worker, daemon=True).start()



# 검색 항목마다 다른 이름으로 저장하여 과거의 공통 음성이 재생되지 않게 합니다.
# 인터넷 사용이 가능하면 Edge TTS의 한국어 여성 음성을 캐싱합니다.
# 인터넷이 없을 때는 Windows에 설치된 한국어 여성 SAPI 음성을 사용합니다.
_search_audio_build_lock = threading.Lock()


def found_voice_message(target):
    """검색 항목 이름과 한국어 목적격 조사(을/를)를 결합합니다."""
    if target not in SEARCH_TARGETS:
        raise ValueError('등록되지 않은 검색 항목입니다.')
    if target == HELMET_TARGET:
        return '안전모를 착용해 주세요.'
    last = target[-1]
    has_batchim = ('가' <= last <= '힣' and (ord(last) - ord('가')) % 28 != 0)
    return f"{target}{'을' if has_batchim else '를'} 발견했습니다."


def search_audio_path(target):
    index = list(SEARCH_TARGETS).index(target) + 1
    return AUDIO_PATH.parent / f'target_found_{index:02d}.mp3'


def prepare_search_audio(target):
    """선택한 항목만 네트워크 TTS로 미리 저장; 실패 시 SAPI 대체를 허용합니다."""
    if target not in SEARCH_TARGETS:
        return
    path = search_audio_path(target)
    if path.is_file() and path.stat().st_size > 0:
        return
    with _search_audio_build_lock:
        if path.is_file() and path.stat().st_size > 0:
            return
        tmp = path.with_suffix('.tmp.mp3')
        try:
            import edge_tts
            path.parent.mkdir(exist_ok=True, parents=True)
            async def generate():
                await edge_tts.Communicate(found_voice_message(target),
                    voice='ko-KR-SunHiNeural', rate='-5%').save(str(tmp))
            asyncio.run(asyncio.wait_for(generate(), timeout=18))
            if tmp.stat().st_size == 0:
                raise RuntimeError('빈 음성 파일')
            tmp.replace(path)
        except Exception as error:
            LOGGER.warning('검색 음성 MP3 준비 실패 (%s): %s', target, error)
            with suppress(OSError):
                tmp.unlink()


def speak_found(target):
    """발견 시 목표 이름을 한국어 여성 목소리로 안내. UI/제어 통신은 차단하지 않습니다."""
    if target not in SEARCH_TARGETS:
        return
    message = found_voice_message(target)

    def worker():
        if sys.platform != 'win32':
            LOGGER.warning('음성 재생은 Windows 전용입니다: %s', message)
            return
        with _speech_lock:
            try:
                if play_cached_audio(search_audio_path(target)):
                    return
                # 설치된 여성 한국어 SAPI 음성을 먼저 시도합니다.
                safe = message.replace("'", "''")
                script = """Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
$v = $s.GetInstalledVoices() | Where-Object {
    $_.Enabled -and $_.VoiceInfo.Gender -eq 'Female' -and
    $_.VoiceInfo.Culture.Name -like 'ko-*'
} | Select-Object -First 1
if ($null -eq $v) { exit 2 }
$s.SelectVoice($v.VoiceInfo.Name)
$s.Rate = 0
$s.Speak('""" + safe + """')
} finally { $s.Dispose() }
"""
                encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
                result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                         '-EncodedCommand', encoded], capture_output=True,
                                        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode == 0:
                    return
                # 한국어 여성 음성이 설치되지 않았으면 온라인 여성 음성을 시도합니다.
                prepare_search_audio(target)
                if not play_cached_audio(search_audio_path(target)):
                    LOGGER.warning('한국어 여성 음성을 재생하지 못했습니다 (%s). '
                                   'edge-tts 인터넷 연결이나 Windows 한국어 음성을 확인하세요.', target)
            except Exception:
                LOGGER.exception('검색 음성 안내 실패 (%s)', target)
    threading.Thread(target=worker, daemon=True).start()


# 기존 COCO 물체는 클래스 번호, 건축물 결함은 별도 모델에서 매핑한 한글 이름으로 검색합니다.
# 리스트박스의 '사람'은 COCO 인식, '안전모 미 착용'은 전용 모델로 별도 검사합니다.
SEARCH_TARGETS = {'사람': 0, '개': 16, '코끼리': 20, '자전거': 1,
                  '오토바이': 3, '트럭': 7, '승용차': 2, '바나나': 46,
                  '사과': 47, '오렌지': 49, '칼': 43, '컵': 41,
                  '벽면 균열': '벽면 균열', '노출 철근': '노출 철근',
                  '누수 흔적': '누수 흔적', '안전모 미 착용': '안전모 미 착용'}
DEFECT_TARGETS = frozenset(('벽면 균열', '노출 철근', '누수 흔적'))
HELMET_TARGET = '안전모 미 착용'  # 이 항목을 선택했을 때에만 안전모 검사
ANOMALY_TARGETS = DEFECT_TARGETS | {HELMET_TARGET}


def display_target(target):
    """검색 항목을 그대로 표시합니다."""
    return target


def inspection_status(target):
    """선택 항목의 표시 구분. '정상'은 건축물 안전 진단이 아닌 일반 물체 분류입니다."""
    return '이상' if target in ANOMALY_TARGETS or target in ('안전모 미 착용', '안전모 미착용') else '정상'


def inspection_color(target):
    return '#b71c1c' if target in ANOMALY_TARGETS or target in ('안전모 미 착용', '안전모 미착용') else '#237a3b'

DEFECT_PREVIEW_ALL = '모든 결함'  # 분석 창에서는 3종을 함께 표시. 검색 시작 시 선택한 항목만 검색

# Roboflow/Ultralytics에서 학습한 결함 탐지 모델의 다양한 클래스 명칭에 대응합니다.
# spalling(박락)이나 rust(녹)는 '철근 노출'로 판정하지 않습니다.
# efflorescence(백화)는 누수의 단서이지만 누수 확정 진단은 아닙니다.
DEFECT_ALIASES = {
    '벽면 균열': ('crack', 'cracks', 'wallcrack', 'wallcracks', 'concretecrack',
              'concretecracks', 'acrack', 'alligatorcrack', '균열', '벽면균열'),
    '노출 철근': ('rebar', 'rebars', 'exposedrebar', 'exposedrebars',
              'exposedreinforcement', 'reinforcementexposure', 'rebarexposure',
              'exposedsteelbar', '노출철근', '철근노출'),
    '누수 흔적': ('leak', 'leaks', 'leakage', 'waterleak', 'waterleakage',
              'waterstain', 'waterstains', 'wetspot', 'wetspots',
              'seepage', 'waterseepage', 'efflorescence',
              'dampness', 'moisture', 'waterdamage', '누수', '누수흔적', '백화'),
}
DEFECT_CLASS_LOOKUP = {
    re.sub(r'[^a-z0-9가-힣]', '', alias.casefold()): target
    for target, aliases in DEFECT_ALIASES.items() for alias in aliases
}


def defect_target_for_class(class_name):
    """모델의 영문/한글 클래스명을 검색 항목의 이름으로 변환합니다."""
    normalized = re.sub(r'[^a-z0-9가-힣]', '', str(class_name).casefold())
    return DEFECT_CLASS_LOOKUP.get(normalized)


SEARCH_CONFIDENCE = 0.55
SEARCH_CONFIRM_FRAMES = 3


STOP_DISTANCE_CM = 30.0
FORWARD_MOVES = {'FORWARD', 'FORWARD_LEFT', 'FORWARD_RIGHT', 'CURVE_LEFT', 'CURVE_RIGHT'}


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def distance_clear(status):
    value = status.get('distance_cm')
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > STOP_DISTANCE_CM)


def motion_allowed(direction, status):
    return direction not in FORWARD_MOVES or distance_clear(status)


IMAGE_PATH = Path(__file__).resolve().parent / 'images' / 'button_back1.png'
LOGO_PATH = Path(__file__).resolve().parent / 'images' / 'smartICT.png'
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
# 화면의 세로로 긴 버튼 배치에 클릭 영역도 같은 비율로 맞춥니다.
REGIONS = [(code, name, x1, round(y1*600/480), x2, round(y2*600/480))
           for code, name, x1, y1, x2, y2 in REGIONS]
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
        self.motion_blocked = False
        self.stop_id = 0
        self.stop_ack_supported = False
        self.pwm = max(0, min(MAX_PWM, pwm))
        self.mode = 'precision'
        self.distance_cm = 1
        self.move_id = 0
        self.distance_supported = False
        self.ultra_scaled_pwm = False
        self.frame_ack = -1
        self.display_time = 0.0
        self.ui_time = monotonic()
        self.connected = False
        self.pending_frame = None
        self.status = {}
        self.status_time = 0.0
        self.collision_notice = False
        self.message = '연결 중...'
        threading.Thread(target=self.control_loop, daemon=True).start()

    def stop(self):
        with self.lock:
            self.direction = 'STOP'

    def block_motion(self):
        with self.lock:
            self.motion_blocked = True
            self.direction = 'STOP'
            self.stop_id += 1
            return self.stop_id

    def unblock_motion(self):
        with self.lock:
            self.direction = 'STOP'
            self.motion_blocked = False

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
            if hello.get('version') not in (2, 3):
                raise RuntimeError('car_ws_serverV3.py 또는 V2 서버를 실행해 주세요.')
            token = hello['token']
            self.stop_ack_supported = hello.get('stop_ack') is True
            self.distance_supported = hello.get('distance_control') is True
            self.ultra_scaled_pwm = hello.get('ultra_scaled_pwm') is True
            with self.lock:
                self.connected = True
                self.message = '연결됨 / 영상 대기'
            video_thread = threading.Thread(target=self.video_loop, args=(token,), daemon=True)
            video_thread.start()
            while not self.ended.is_set():
                with self.lock:
                    now = monotonic()
                    fresh = now-self.display_time < 1 and now-self.ui_time < 0.4
                    if self.motion_blocked:
                        self.direction = 'STOP'
                    if not fresh or now-self.status_time > 1 or not motion_allowed(self.direction, self.status):
                        if self.direction != 'STOP':
                            LOGGER.warning('Local stop: video_age=%.2f ui_age=%.2f status_age=%.2f distance=%s',
                                           now-self.display_time, now-self.ui_time,
                                           now-self.status_time, self.status.get('distance_cm'))
                        self.direction = 'STOP'
                    # 서버가 직전에 보낸 lease를 다음 명령에 사용합니다.
                    # STOP은 항상 PWM 0으로 보내고, 이동 PWM은 0~100으로 제한합니다.
                    stopping = self.direction == 'STOP' or self.pwm == 0
                    data = dict(
                        direction='STOP' if stopping else self.direction,
                        pwm=0 if stopping else max(0, min(MAX_PWM, self.pwm)),
                        frame=self.frame_ack,
                        lease=self.status.get('lease', ''),
                        mode=self.mode,
                        distance_cm=self.distance_cm,
                        move_id=str(self.move_id),
                        stop_id=self.stop_id,
                    )
                ws.send(json.dumps(data))
                result = json.loads(ws.recv())
                with self.lock:
                    distance = result.get('distance_cm')
                    was_moving = (self.direction in FORWARD_MOVES and self.pwm > 0
                                  and (self.status.get('direction') in FORWARD_MOVES
                                       or result.get('reason') == 'FRONT OBSTACLE'))
                    if was_moving and finite_number(distance) and 0 <= distance <= STOP_DISTANCE_CM:
                        self.collision_notice = True
                    self.status = result
                    self.status_time = monotonic()
                    if not motion_allowed(self.direction, result):
                        self.direction = 'STOP'
                    self.message = result.get('reason', '')
                    if self.message not in ('RUNNING', 'STOP', 'WAITING'):
                        if self.direction != 'STOP':
                            LOGGER.warning('Server stop: %s', self.message)
                        self.direction = 'STOP'
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
                    ws.send(json.dumps(dict(direction='STOP', pwm=0, frame=-1, stop_id=0)))
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



# 건축물 결함 3종은 일반 YOLO에 없는 클래스입니다.
# 선택한 항목에 한해 등록된 사진을 찾는 다중 해상도 SIFT+RANSAC 비교를 수행합니다.
# models/structural_defects.pt는 36개 특징점 집합이지 YOLO의 학습 가중치가 아닙니다.
class PhotoReferenceMatcher:
    """2장의 예시 사진 x 3가지 크기 x 2가지 대비로 대상별 사진을 검색합니다.

    실제 건축물 손상의 종류를 범용 인식하는 모델은 아닙니다.
    반환 점수는 일치 강도이며 통계적 확률이 아닙니다.
    """

    def __init__(self, path):
        import cv2
        import numpy as np
        import torch
        self.cv2, self.np = cv2, np
        saved = torch.load(str(path), map_location='cpu', weights_only=True)
        if saved.get('format') != 'sift_reference_set_v3':
            raise ValueError('사진 데이터 버전이 다릅니다. V3(11)의 새 structural_defects.pt를 복사해 주세요.')
        settings = saved['settings']
        self.sift = cv2.SIFT_create(nfeatures=int(settings['nfeatures']),
                                    contrastThreshold=float(settings['contrastThreshold']))
        # KD Tree로 SIFT(float32) 특징점의 최근접 이웃을 검색합니다.
        self.matcher = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=48))
        self.label_font = None
        # 음성 및 결과 목록뿐 아니라 영상의 테두리에도 한글 클래스명을 표시합니다.
        for fontpath in (Path('C:/Windows/Fonts/malgun.ttf'),
                         Path('/usr/share/fonts/truetype/nanum/NanumGothic.ttf'),
                         Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')):
            if fontpath.is_file():
                with suppress(Exception):
                    self.label_font = ImageFont.truetype(str(fontpath), 19)
                    break
        self.references = {}
        for label, entries in saved['references'].items():
            self.references[label] = [
                (entry['points'].numpy().astype(np.float32),
                 entry['descriptors'].numpy().astype(np.float32),
                 int(entry['size'][0]), int(entry['size'][1]),
                 entry['mode']) for entry in entries
            ]

    def predict(self, frame, target=None):
        """PIL RGB frame -> [(검색 이름, 일치 강도, (x1,y1,x2,y2))], 표시 영상."""
        cv2, np = self.cv2, self.np
        rgb = np.asarray(frame.convert('RGB'))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        # 두 대비 설정 각각의 키포인트는 프레임당 1회만 계산합니다.
        scene_cache = {}
        annotated = rgb.copy()
        detections = []
        groups = ((target, self.references[target]),) if target in self.references \
            else self.references.items()

        for label, records in groups:
            best = None
            for ref_xy, ref_des, ref_w, ref_h, mode in records:
                if mode not in scene_cache:
                    processed = cv2.equalizeHist(gray) if mode == 'equalized' else gray
                    scene_kps, desc = self.sift.detectAndCompute(processed, None)
                    scene_cache[mode] = (
                        np.float32([kp.pt for kp in scene_kps]).reshape(-1, 2), desc)
                scene_xy, scene_des = scene_cache[mode]
                if scene_des is None or len(scene_des) < 10:
                    continue
                try:
                    pairs = self.matcher.knnMatch(ref_des, scene_des, k=2)
                except cv2.error:
                    continue
                good = [m for pair in pairs if len(pair) == 2
                        for m, n in [pair] if m.distance < 0.76 * n.distance]
                if len(good) < 11:
                    continue
                src = np.float32([ref_xy[m.queryIdx] for m in good]).reshape(-1, 1, 2)
                dst = np.float32([scene_xy[m.trainIdx] for m in good]).reshape(-1, 1, 2)
                H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.5)
                if H is None or mask is None:
                    continue
                inliers = mask.ravel().astype(bool)
                count = int(inliers.sum())
                if count < 10 or count / len(good) < 0.52:
                    continue
                # 서로 다른 위치에 있는 특징점이 함께 일치해야 오검출을 줄일 수 있습니다.
                spread = np.ptp(src[inliers].reshape(-1, 2), axis=0)
                if spread[0] < ref_w * .14 or spread[1] < ref_h * .14:
                    continue
                original_corners = np.float32([
                    [0, 0], [ref_w - 1, 0], [ref_w - 1, ref_h - 1], [0, ref_h - 1]
                ]).reshape(-1, 1, 2)
                corners = cv2.perspectiveTransform(original_corners, H).reshape(4, 2)
                if not np.all(np.isfinite(corners)):
                    continue
                polygon = corners.astype(np.float32)
                area = abs(cv2.contourArea(polygon))
                if not cv2.isContourConvex(polygon) or area < 550 or area > frame.width * frame.height * 3:
                    continue
                x1 = max(0, int(np.floor(corners[:, 0].min())))
                y1 = max(0, int(np.floor(corners[:, 1].min())))
                x2 = min(frame.width, int(np.ceil(corners[:, 0].max())))
                y2 = min(frame.height, int(np.ceil(corners[:, 1].max())))
                if x2 <= x1 or y2 <= y1:
                    continue
                # 일치한 특징점 수를 인식 강도로 표현하며 확률은 아닙니다.
                score = min(0.99, 0.60 + count / 180.0)
                if best is None or count > best[0]:
                    best = (count, score, (x1, y1, x2, y2), polygon)
            if best is not None:
                count, score, bounds, polygon = best
                detections.append((label, score, bounds))
                color = {'벽면 균열': (235, 55, 55), '노출 철근': (30, 175, 65),
                         '누수 흔적': (30, 95, 235)}[label]
                cv2.polylines(annotated, [polygon.astype(np.int32)], True, color, 3)
        output = Image.fromarray(annotated)
        if self.label_font is not None:
            painter = ImageDraw.Draw(output)
            for label, _, (x1, y1, x2, y2) in detections:
                painter.text((max(3, x1 + 3), max(2, y1 + 4)), label,
                             font=self.label_font, fill='white',
                             stroke_width=2, stroke_fill='#18242c')
        # 한글 글꼴을 찾지 못한 환경에서도 검색 결과 리스트는 한글로 표시됩니다.
        return detections, output


# YOLO는 PC의 단일 작업 스레드에서만 실행합니다.
MODEL_PATH = Path(__file__).resolve().parent / 'yolo11n.pt'
# 6장의 기준 사진에서 다양한 크기·대비의 SIFT 특징을 저장한 파일. YOLO 가중치가 아닙니다.
DEFECT_MODEL_PATH = Path(__file__).resolve().parent / 'models' / 'structural_defects.pt'
HELMET_MODEL_PATH = Path(__file__).resolve().parent / 'models' / 'helmet_detection.pt'  # 별도 학습된 안전모 YOLO 가중치
YOLO_IMAGE_SIZE = 640
YOLO_CONFIDENCE = 0.25
YOLO_DEVICE = 'cpu'  # CUDA 환경을 따로 구성한 경우 '0'으로 변경 가능

# COCO 클래스 이름. 사전학습 모델의 클래스 순서와 대응합니다.
KOREAN_CLASSES = [
    '사람', '자전거', '자동차', '오토바이', '비행기', '버스', '기차', '트럭', '보트',
    '신호등', '소화전', '정지 표지판', '주차 요금기', '벤치', '새', '고양이', '개', '말',
    '양', '소', '코끼리', '곰', '얼룩말', '기린', '배낭', '우산', '핸드백', '넥타이',
    '여행 가방', '프리스비', '스키', '스노보드', '스포츠 공', '연', '야구 방망이',
    '야구 글러브', '스케이트보드', '서핑보드', '테니스 라켓', '병', '와인잔', '컵',
    '포크', '칼', '숟가락', '그릇', '바나나', '사과', '샌드위치', '오렌지', '브로콜리',
    '당근', '핫도그', '피자', '도넛', '케이크', '의자', '소파', '화분', '침대', '식탁',
    '변기', 'TV', '노트북', '마우스', '리모컨', '키보드', '휴대전화', '전자레인지',
    '오븐', '토스터', '싱크대', '냉장고', '책', '시계', '꽃병', '가위', '곰 인형',
    '헤어드라이어', '칫솔',
]


# 안전모 모델에는 반드시 '미착용 머리'를 식별하는 별도 클래스가 있어야 합니다.
# 일반 YOLO11 COCO는 안전모를 구분하지 못합니다.
def helmet_class_kind(label):
    cleaned = re.sub(r'[^a-z0-9가-힣]', '', str(label).casefold())
    if cleaned in {'head', 'nohelmet', 'nohardhat', 'withouthelmet', 'withoutahardhat',
                   'barehead', 'unprotectedhead', 'nohat', 'nocap', 'nohardhelmet',
                   'headwithouthelmet', 'headwithoutahardhat', 'nohardhats',
                   '미착용', '안전모미착용', '안전모미착용자'}:
        return 'no_helmet'
    if cleaned in {'helmet', 'hardhat', 'safetyhelmet', 'withhelmet',
                   'wearinghelmet', 'helmeton', 'hardhaton', 'withhardhat',
                   '안전모', '안전모착용'}:
        return 'helmet'
    if cleaned in {'person', 'people', 'worker', 'human'}:
        return 'person'
    return None


class HelmetDetectionModel:
    """별도 YOLO 모델로 맨머리만 위반 처리하고, 관련 작업자 상반신을 기록합니다.

    분류되지 않은 머리나 사람이 있다는 이유만으로 미착용으로 판정하지 않습니다.
    머리/사람이 가려지거나 작을 때는 검출을 놓칠 수 있습니다.
    """
    def __init__(self, model_path):
        from ultralytics import YOLO
        self.model = YOLO(str(model_path))
        names = self.model.names
        self.class_kinds = {int(i): helmet_class_kind(label) for i, label in
                            (names.items() if isinstance(names, dict) else enumerate(names))}
        if 'no_helmet' not in self.class_kinds.values() or 'helmet' not in self.class_kinds.values():
            raise ValueError(f'안전모 모델의 클래스가 예상과 다릅니다: {names}. '
                             'helmet 및 head/no_helmet 클래스가 있는 가중치가 필요합니다.')
        self.label_font = None
        for path in (Path('C:/Windows/Fonts/malgun.ttf'),
                     Path('/usr/share/fonts/truetype/nanum/NanumGothic.ttf'),
                     Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')):
            if path.is_file():
                with suppress(Exception):
                    self.label_font = ImageFont.truetype(str(path), 17)
                    break

    @staticmethod
    def person_crop(headbox, persons, width, height):
        hx1, hy1, hx2, hy2 = headbox
        cx, cy = (hx1 + hx2) / 2, (hy1 + hy2) / 2
        # 머리가 안에 있는 사람 박스를 찾되 가장 가까운 인물 1명만 기록합니다.
        matched = [p for p in persons if p[0] <= cx <= p[2] and p[1] <= cy <= p[3]
                   and p[1] <= hy1 + (hy2 - hy1) * 0.5]
        if matched:
            x1,y1,x2,y2 = min(matched, key=lambda p: (p[2]-p[0])*(p[3]-p[1]))
        else:
            # 모델이 person 박스를 제공하지 않으면 맨머리를 기준으로 상반신까지 확장.
            hw, hh = max(1, hx2-hx1), max(1, hy2-hy1)
            x1, x2 = cx-1.6*hw, cx+1.6*hw
            y1, y2 = hy1-0.4*hh, hy2+3.5*hh
        return (max(0, int(x1)), max(0, int(y1)),
                min(width, int(x2 + 1)), min(height, int(y2 + 1)))

    def predict(self, frame):
        results = self.model.predict(source=frame, imgsz=YOLO_IMAGE_SIZE, conf=0.25,
                                     device=YOLO_DEVICE, max_det=40, verbose=False)[0]
        if results.boxes is None:
            raise ValueError('안전모 탐지에는 detect 방식의 YOLO 모델이 필요합니다.')
        raw = [(self.class_kinds.get(int(cls)), float(score), tuple(map(float, box)))
               for cls, score, box in zip(results.boxes.cls.cpu().tolist(),
                   results.boxes.conf.cpu().tolist(), results.boxes.xyxy.cpu().tolist())]
        people = [box for cls,score,box in raw if cls == 'person' and score >= 0.30]
        helmets = [box for cls,score,box in raw if cls == 'helmet' and score >= 0.35]
        # 서로 같은 머리 위치에서 helmet와 head가 동시에 검출되면 오경고를 피합니다.
        # head는 해당 가중치에서 '안전모 없는 머리'라는 뜻이어야 합니다.
        def overlaps_helmet(head_box):
            hx1, hy1, hx2, hy2 = head_box
            head_area = max(1., (hx2-hx1)*(hy2-hy1))
            for ex1, ey1, ex2, ey2 in helmets:
                inter = max(0., min(hx2,ex2)-max(hx1,ex1)) * max(0., min(hy2,ey2)-max(hy1,ey1))
                if inter / head_area >= 0.25:
                    return True
            return False
        # 0.50 이상 안전모 없는 머리 + 연속 3개 프레임 확인 시 경고합니다.
        violations = []
        canvas = frame.convert('RGB').copy()
        paint = ImageDraw.Draw(canvas)
        counts = Counter()
        for kind, score, headbox in raw:
            if kind == 'helmet' and score >= 0.35:
                counts['안전모 착용'] += 1
                x1,y1,x2,y2 = map(int, headbox)
                paint.rectangle((x1,y1,x2,y2), outline='#22aa50', width=3)
                paint.text((x1,max(0,y1-21)), 'HELMET', fill='#22aa50')
            elif kind == 'no_helmet' and score >= 0.50 and not overlaps_helmet(headbox):
                counts['안전모 미 착용'] += 1
                bbox = self.person_crop(headbox, people, frame.width, frame.height)
                violations.append((HELMET_TARGET, score, bbox))
                paint.rectangle(bbox, outline='#e53935', width=4)
                paint.rectangle(tuple(map(int, headbox)), outline='#e53935', width=3)
                label = '안전모 미 착용' if self.label_font is not None else 'NO HELMET'
                paint.text((max(2,bbox[0]), max(0,bbox[1]-23)),
                           f'{label} {score:.2f}', fill='#e53935',
                           font=self.label_font)
        return violations, canvas, counts


class YoloWorker:
    """COCO 물체 모델과 건축물 결함 모델을 하나의 작업 스레드에서 전환합니다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.enabled = False
        self.epoch = 0
        self.model_kind = 'coco'
        self.target = None
        self.pending = None
        self.result = None
        self.search_result = None
        self.message = 'YOLO 꺼짐'
        threading.Thread(target=self.run, daemon=True).start()

    def configure(self, enabled, target=None):
        with self.lock:
            self.enabled = enabled
            self.target = target if enabled else None
            self.model_kind = ('helmet' if enabled and target == HELMET_TARGET else
                               'defect' if enabled and (target in DEFECT_TARGETS or target == DEFECT_PREVIEW_ALL) else 'coco')
            self.epoch += 1  # 이전 설정의 추론 결과 폐기
            self.pending = self.result = self.search_result = None
            if not enabled:
                self.message = 'YOLO 꺼짐'
            elif self.model_kind == 'defect':
                self.message = f'{target} 사진 비교 데이터 준비 중'
            elif self.model_kind == 'helmet':
                self.message = '안전모 미 착용 감지 모델 준비 중'
            else:
                self.message = 'YOLO 준비 중 / 첫 실행은 모델 다운로드가 필요합니다.'
        self.wake.set()

    def submit(self, seq, frame):
        with self.lock:
            if self.enabled:
                # 대기열을 쌓지 않고 최신 프레임만 추론합니다.
                self.pending = (self.epoch, seq, frame, monotonic())
        self.wake.set()

    def read(self):
        with self.lock:
            return self.result, self.message

    def read_search(self):
        with self.lock:
            return self.search_result, self.message

    def close(self):
        self.closed.set()
        self.wake.set()

    def run(self):
        models = {}  # 모델을 재선택해도 다시 로드하지 않습니다.
        while not self.closed.is_set():
            self.wake.wait(0.2)
            self.wake.clear()
            with self.lock:
                enabled, epoch = self.enabled, self.epoch
                kind, target = self.model_kind, self.target
            if not enabled:
                continue
            try:
                if kind not in models:
                    if kind == 'defect':
                        if not DEFECT_MODEL_PATH.is_file():
                            raise FileNotFoundError(
                                f'사진 비교 데이터 없음: {DEFECT_MODEL_PATH}')
                        models[kind] = PhotoReferenceMatcher(DEFECT_MODEL_PATH)
                    elif kind == 'helmet':
                        if not HELMET_MODEL_PATH.is_file():
                            raise FileNotFoundError(f'안전모 전용 가중치 없음: {HELMET_MODEL_PATH}. '
                                                    'download_helmet_model.py를 먼저 실행해 주세요.')
                        models[kind] = HelmetDetectionModel(HELMET_MODEL_PATH)
                    else:
                        # 기존 물체는 YOLO11 COCO 탐지 모델을 사용합니다.
                        import torch
                        torch.set_num_threads(2)
                        from ultralytics import YOLO
                        models[kind] = YOLO(str(MODEL_PATH))
                model = models[kind]
                with self.lock:
                    if not self.enabled or self.epoch != epoch:
                        continue
                    job, self.pending = self.pending, None
                    self.message = '영상 분석 준비 완료 / 영상 대기' if job is None else '영상 분석 중'
                if job is None:
                    continue
                generation, seq, frame, received = job
                started = monotonic()
                detections = []
                counts = Counter()
                if kind == 'defect':
                    detections, annotated = model.predict(frame, target=target if target in DEFECT_TARGETS else None)
                    for label, _, _ in detections:
                        counts[label] += 1
                elif kind == 'helmet':
                    detections, annotated, counts = model.predict(frame)
                else:
                    result = model.predict(source=frame, imgsz=YOLO_IMAGE_SIZE,
                                           conf=YOLO_CONFIDENCE, device=YOLO_DEVICE,
                                           max_det=30, verbose=False)[0]
                    if result.boxes is None:
                        raise ValueError('탐지(detect) 방식의 YOLO 모델만 지원합니다.')
                    cls_ids = result.boxes.cls.cpu().tolist()
                    confidences = result.boxes.conf.cpu().tolist()
                    boxes = result.boxes.xyxy.cpu().tolist()
                    for class_id, confidence, box in zip(cls_ids, confidences, boxes):
                        index = int(class_id)
                        label = (KOREAN_CLASSES[index] if 0 <= index < len(KOREAN_CLASSES)
                                 else str(result.names[index]))
                        detections.append((index, float(confidence), tuple(box)))
                        counts[label] += 1
                    annotated = Image.fromarray(result.plot()[:, :, ::-1].copy())
                summary = ', '.join(f'{name}: {count}' for name, count in counts.items()) or '탐지된 물체 없음'
                elapsed = monotonic() - started
                with self.lock:
                    if self.enabled and self.epoch == generation:
                        self.search_result = (generation, seq, frame, detections, received)
                        self.result = (generation, seq, annotated, summary, received, elapsed)
                        self.message = ('사진 비교 분석 중' if kind == 'defect' else
                                        '안전모 미 착용 분석 중' if kind == 'helmet' else 'YOLO 분석 중')
                self.closed.wait(max(0, 0.2 - elapsed))
            except Exception as error:
                LOGGER.exception('YOLO 모델/분석 오류 (%s)', kind)
                with self.lock:
                    if self.epoch == epoch:
                        self.enabled = False
                        self.pending = self.result = self.search_result = None
                        self.message = f'영상 분석 오류: {error}'
                # 실패해도 GUI와 차량 제어는 계속 유지합니다.



# SQLite는 별도 서버 없이 같은 PC에 기록을 보관합니다.
# 모든 검색 항목 [닫기] → [예]에서만 사진과 탐사 이력을 저장합니다.
# 이전 V3(14) 데이터베이스의 이상 기록과 이미지 경로는 그대로 유지합니다.
PROJECT_ROOT = Path(__file__).resolve().parent
HISTORY_DB_PATH = PROJECT_ROOT / 'exploration_history.db'
# 기존 V3(14)의 저장 폴더를 유지하여 기존 사진 조회/삭제와 호환합니다.
HISTORY_CAPTURE_DIR = PROJECT_ROOT / 'captures' / 'defects'


class ExplorationHistory:
    """승인된 검색 이벤트마다 기록 1건과 이미지 2장(부분·전체)을 보관합니다."""

    COLUMNS = ('id', 'event_id', 'event_date', 'event_time', 'item', 'abnormal',
               'photo_path', 'overview_path', 'match_score')
    CREATE_SQL = """CREATE TABLE inspection_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_date TEXT NOT NULL,
        event_time TEXT NOT NULL,
        item TEXT NOT NULL,
        abnormal TEXT NOT NULL CHECK(abnormal IN ('이상', '정상')),
        photo_path TEXT NOT NULL,
        overview_path TEXT NOT NULL,
        match_score REAL NOT NULL
    )"""

    def __init__(self, db_path=HISTORY_DB_PATH, capture_dir=HISTORY_CAPTURE_DIR):
        self.db_path = Path(db_path)
        self.capture_dir = Path(capture_dir)
        with sqlite3.connect(self.db_path, timeout=5) as db:
            original = db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                                  "AND name='inspection_history'").fetchone()
            if original is None:
                db.execute(self.CREATE_SQL)
            elif "'정상'" not in original[0]:
                # V3(14): abnormal CHECK(abnormal = '이상') 때문에 '정상' INSERT 불가.
                # 트랜잭션으로 옛 레코드와 id를 유지하며 테이블을 교체합니다.
                actual_columns = tuple(row[1] for row in
                                       db.execute('PRAGMA table_info(inspection_history)'))
                if actual_columns != self.COLUMNS:
                    raise sqlite3.DatabaseError(
                        '기존 DB의 열 구조가 예상과 다릅니다. DB를 변경하지 않았습니다.')
                db.execute(self.CREATE_SQL.replace('inspection_history',
                                                  'inspection_history_v15', 1))
                cols = ', '.join(self.COLUMNS)
                db.execute(f'INSERT INTO inspection_history_v15 ({cols}) '
                           f'SELECT {cols} FROM inspection_history')
                db.execute('DROP TABLE inspection_history')
                db.execute('ALTER TABLE inspection_history_v15 RENAME TO inspection_history')
                LOGGER.info('탐사 이력 DB 확장: 이전 이상 기록 보존, 정상 기록 저장 가능')
            db.execute('CREATE INDEX IF NOT EXISTS idx_history_date '
                       'ON inspection_history(event_date DESC, event_time DESC)')

    def record_inspection(self, record, event_id):
        """[예]로 승인된 16개 검색 항목만 저장하며 같은 이벤트는 중복 저장하지 않습니다."""
        item = record['target']
        if item not in SEARCH_TARGETS:
            raise ValueError('검색 목록에 없는 항목은 이력에 저장할 수 없습니다.')
        if not event_id:
            raise ValueError('검색 세션 ID가 없습니다.')
        status = inspection_status(item)
        stored_item = display_target(item)
        when = record['timestamp']
        confidence = float(record['confidence'])
        created = []
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            existing = db.execute('SELECT id FROM inspection_history WHERE event_id=?',
                                  (event_id,)).fetchone()
            if existing:
                return existing[0]
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{when:%Y%m%d_%H%M%S_%f}_{event_id[:10]}"
            part = self.capture_dir / f'{stem}_부분.jpg'
            scene_path = self.capture_dir / f'{stem}_전체.jpg'
            created.append(part)
            record['image'].convert('RGB').save(part, 'JPEG', quality=92)
            scene = record['scene'].convert('RGB').copy()
            ImageDraw.Draw(scene).rectangle(record['bounds'],
                                            outline='#e53935' if status == '이상' else '#237a3b',
                                            width=4)
            created.append(scene_path)
            scene.save(scene_path, 'JPEG', quality=91)
            photo_rel = os.path.relpath(part, self.db_path.parent)
            scene_rel = os.path.relpath(scene_path, self.db_path.parent)
            cursor = db.execute("""INSERT INTO inspection_history
                (event_id, event_date, event_time, item, abnormal,
                 photo_path, overview_path, match_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, when.strftime('%Y-%m-%d'), when.strftime('%H:%M:%S'),
                 stored_item, status, photo_rel, scene_rel, confidence))
            db.commit()
            return cursor.lastrowid
        except Exception:
            db.rollback()
            for image_path in created:
                with suppress(OSError):
                    image_path.unlink()
            raise
        finally:
            db.close()

    def list_inspections(self, limit=1000):
        with sqlite3.connect(self.db_path, timeout=5) as db:
            return db.execute("""SELECT id, event_date, event_time, item, abnormal
                FROM inspection_history ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()

    def get_inspection(self, row_id):
        with sqlite3.connect(self.db_path, timeout=5) as db:
            return db.execute("""SELECT id, event_date, event_time, item, abnormal,
                photo_path, overview_path, match_score FROM inspection_history WHERE id=?""",
                (row_id,)).fetchone()

    def photo_file(self, relative_path):
        """DB 사진 경로를 기존 증거 사진 폴더 아래로 제한합니다."""
        candidate = (self.db_path.parent / relative_path).resolve()
        if not candidate.is_relative_to(self.capture_dir.resolve()):
            raise ValueError('허용된 탐사 사진 경로가 아닙니다.')
        return candidate

    def delete_inspection(self, row_id):
        with sqlite3.connect(self.db_path, timeout=5) as db:
            row = db.execute('SELECT photo_path, overview_path '
                             'FROM inspection_history WHERE id=?', (row_id,)).fetchone()
            if row is None:
                return False
            db.execute('DELETE FROM inspection_history WHERE id=?', (row_id,))
        for relative in row:
            try:
                self.photo_file(relative).unlink(missing_ok=True)
            except (OSError, ValueError):
                LOGGER.exception('삭제된 탐사 기록의 이미지 정리 실패 (%s)', relative)
        return True


class CarApp:
    def __init__(self, root):
        self.root = root
        threading.Thread(target=prepare_collision_audio, daemon=True).start()
        self.connection = None
        self.closing = False
        self.held = None
        self.direction_pressed = False
        self.control_mode = tk.StringVar(value='precision')
        self.travel_cm = tk.StringVar(value='1')
        self.photo = None
        self.detector = YoloWorker()
        self.yolo_enabled = tk.BooleanVar(value=False)
        self.analysis_window = None
        self.analysis_photo = None
        self.analysis_key = None
        self.search_active = False
        self.search_locked = False
        self.found_window = None
        self.found_record = None
        self.search_key = None
        self.search_hits = 0
        self.search_last_time = 0.0
        self.helmet_last_center = None
        self.search_mode = tk.StringVar(value='off')
        self.search_target = tk.StringVar(value='사람')
        self.search_status = tk.StringVar(value='검색 안 함')
        self.search_session = None
        self.history_window = None
        self.history_table = None
        self.history_photo_window = None
        self.history_photo_record_id = None
        self.history_photo_button = None
        self.history_delete_button = None
        self.history_selected_text = None
        try:
            self.history_store = ExplorationHistory()
            self.history_error = None
        except (OSError, sqlite3.Error) as error:
            # 이력 기능에 문제가 생겨도 차량 조종·안전 정지는 계속 작동합니다.
            self.history_store = None
            self.history_error = str(error)
            LOGGER.exception('탐사 이력 DB 초기화 실패')
        threading.Thread(target=prepare_search_audio,
                         args=(self.search_target.get(),), daemon=True).start()
        root.title('원격구역 탐사로봇 제어 시스템 V3')
        root.geometry('1490x1060')
        root.resizable(False, False)
        root.configure(bg='#edf2f7')
        self.ip = tk.StringVar(value='192.168.0.113')
        self.port = tk.StringVar(value='5000')
        self.pwm = tk.DoubleVar(value=50)
        self.pwm_text = tk.StringVar(value='설정 PWM: 50%')
        self.info = tk.StringVar(value='동작: 정지  |  전방 거리: ---  |  출력 평균 PWM: 0%')
        self.rpm = tk.StringVar(value='M1: --- RPM    M2: --- RPM    M3: --- RPM    M4: --- RPM')
        self.note = tk.StringVar(value='접속 후 방향 버튼을 누르세요.')
        # 상단 연결 영역: 큰 글씨와 선명한 대비
        style = ttk.Style(root)
        style.configure('TLabel', foreground='#000000')
        style.configure('Connection.TLabel', font=('맑은 고딕', 14, 'bold'), foreground='#000000')
        style.configure('Connection.TButton', font=('맑은 고딕', 14, 'bold'), padding=(12, 3))
        style.map('Connection.TButton', foreground=[('disabled', '#000000'), ('!disabled', '#000000')])
        bar = ttk.Frame(root, padding=(10, 3))
        bar.place(x=20, y=10, width=1100, height=48)
        ttk.Label(bar, text='서버 IP', style='Connection.TLabel').pack(side='left')
        ttk.Entry(bar, textvariable=self.ip, width=14, font=('맑은 고딕', 14), foreground='#000000').pack(side='left', padx=4)
        ttk.Label(bar, text='포트', style='Connection.TLabel').pack(side='left')
        ttk.Entry(bar, textvariable=self.port, width=5, font=('맑은 고딕', 14), foreground='#000000').pack(side='left', padx=4)
        self.connect_button = tk.Button(
            bar, text='접속', command=self.connect,
            bg='#237a3b', fg='white', activebackground='#195c2c',
            activeforeground='white', disabledforeground='#000000',
            font=('맑은 고딕', 14, 'bold'), padx=14, pady=0,
            relief='raised', bd=2, cursor='hand2')
        self.connect_button.pack(side='left', padx=5)
        ttk.Button(bar, text='접속 해제', command=self.disconnect, style='Connection.TButton', cursor='hand2').pack(side='left', padx=5)
        self.connection_label = ttk.Label(bar, text='연결 안 됨', style='Connection.TLabel', foreground='#000000')
        self.connection_label.pack(side='left', padx=16)
        self.video = tk.Canvas(root, width=640, height=480, bg='black', highlightthickness=0)
        self.video.place(x=20, y=75)
        self.video_item = self.video.create_image(320, 240)
        self.video_text = self.video.create_text(320, 240, text='라즈베리파이 서버에 접속해 주세요.',
                                                fill='white', font=('맑은 고딕', 14))
        self.panel = tk.Canvas(root, width=430, height=600, highlightthickness=0, bg='white')
        self.panel.place(x=680, y=65)
        self.button_photo = None
        try:
            with Image.open(IMAGE_PATH) as source:
                if source.size != (430, 480):
                    raise ValueError(f'이미지가 {source.size}입니다. 430×480으로 저장해 주세요.')
                self.button_photo = ImageTk.PhotoImage(source.convert('RGB').resize(
                    (430, 600), Image.Resampling.LANCZOS))
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
        self.panel.bind('<Motion>', self.panel_hover)
        self.panel.bind('<Leave>', self.panel_leave)
        root.bind_all('<ButtonRelease-1>', self.release)
        root.bind('<space>', lambda e: self.stop())
        root.bind('<FocusOut>', lambda e: root.after(50, self.check_focus))
        ttk.Label(root, textvariable=self.pwm_text).place(x=20, y=572)
        self.speed_scale = tk.Scale(root, from_=0, to=100, orient='horizontal',
                 variable=self.pwm, command=self.speed_change, resolution=1,
                 tickinterval=50, showvalue=False, sliderlength=24,
                 bg='#edf2f7', fg='#1255b0', troughcolor='#c7d9ed',
                 activebackground='#8db9e8', highlightthickness=0,
                 font=('맑은 고딕', 9), bd=1, cursor='hand2')
        self.speed_scale.place(x=160, y=565, width=500, height=45)
        self.speed_scale.bindtags(tuple(tag for tag in self.speed_scale.bindtags() if tag != 'all'))
        self.speed_scale.bind('<ButtonRelease-1>', self.slider_released)
        tk.Button(root, text='전체 정지 (Space)', command=self.stop,
                  bg='#c62828', fg='white', activebackground='#a51f1f',
                  activeforeground='white', font=('맑은 고딕', 14, 'bold'),
                  relief='raised', bd=2, cursor='hand2').place(
                      x=680, y=684, width=430, height=65)
        self.motion_text = tk.StringVar(value='정지')
        self.distance_text = tk.StringVar(value='---')
        self.output_text = tk.StringVar(value='0%')
        self.rpm_values = [tk.StringVar(value='---') for _ in range(4)]
        info_row = ttk.Frame(root)
        info_row.place(x=20, y=614)
        for label, variable, color in (
                ('동작: ', self.motion_text, '#1255b0'),
                ('  |  전방 거리: ', self.distance_text, '#c62828'),
                ('  |  출력 평균 PWM: ', self.output_text, '#000000')):
            ttk.Label(info_row, text=label,
                      foreground='#1255b0' if variable is self.motion_text else '#000000',
                      font=('맑은 고딕', 11, 'bold') if variable is self.motion_text
                      else ('맑은 고딕', 11)).pack(side='left')
            ttk.Label(info_row, textvariable=variable, foreground=color,
                      font=('맑은 고딕', 11, 'bold')).pack(side='left')
        rpm_row = ttk.Frame(root)
        rpm_row.place(x=20, y=642)
        for i, variable in enumerate(self.rpm_values):
            ttk.Label(rpm_row, text=f'M{i+1}: ', font=('맑은 고딕', 11)).pack(side='left')
            ttk.Label(rpm_row, textvariable=variable, foreground='#c62828',
                      font=('맑은 고딕', 11, 'bold')).pack(side='left')
            ttk.Label(rpm_row, text=' RPM   ', font=('맑은 고딕', 11)).pack(side='left')
        ttk.Label(root, textvariable=self.note, wraplength=640,
                  font=('맑은 고딕', 12, 'bold'), foreground='#142638').place(x=20, y=668)
        tk.Checkbutton(root, text='영상 물체 분석', variable=self.yolo_enabled,
                       command=self.toggle_yolo, fg='#1255b0', bg='#edf2f7',
                       activeforeground='#0b3d87', activebackground='#e0eafb',
                       selectcolor='white', font=('맑은 고딕', 11, 'bold'),
                       cursor='hand2').place(x=20, y=708, width=165, height=36)
        tk.Button(root, text='분석 창 열기', command=self.open_analysis,
                  bg='#1565c0', fg='white', activebackground='#104d94',
                  activeforeground='white', font=('맑은 고딕', 11, 'bold'),
                  relief='raised', bd=2, cursor='hand2').place(
                      x=190, y=708, width=115, height=36)
        ttk.Label(root, text='원본 영상은 조종용 / 분석 영상은 별도 창',
                  foreground='#1255b0', font=('맑은 고딕', 12, 'bold')).place(x=310, y=713)
        # 검색 그룹박스 하단(y=859) 아래에 탐사 이력 버튼을 배치합니다.
        # 로고 상단(y=942)과의 간격을 유지하여 버튼 글자가 가리지 않게 합니다.
        tk.Button(root, text='탐사 이력 보기', command=self.open_history,
                  bg='#175c90', fg='white', activebackground='#104b77',
                  activeforeground='white', font=('맑은 고딕', 14, 'bold'),
                  relief='raised', bd=2, cursor='hand2').place(
                      x=1130, y=871, width=340, height=58)
        # 오른쪽 아래 로고의 실제 320x80 이미지 하단(y=1030)을
        # 왼쪽 모니터링 그룹박스 하단(y=780+250=1030)에 정확히 맞춥니다.
        self.logo_canvas = tk.Canvas(root, width=340, height=88,
                                     bg='#edf2f7', highlightthickness=0)
        self.logo_canvas.place(x=1130, y=942, width=340, height=88)
        self.logo_photo = None
        try:
            with Image.open(LOGO_PATH) as source:
                logo = source.convert('RGBA').resize((320, 80), Image.Resampling.LANCZOS)
                self.logo_photo = ImageTk.PhotoImage(logo)
            self.logo_canvas.create_image(170, 88, anchor='s', image=self.logo_photo)
        except OSError as error:
            self.logo_canvas.create_text(170, 74,
                text='images/smartICT.png 파일을 확인해 주세요.',
                fill='#000000', font=('맑은 고딕', 10), width=310)
            LOGGER.warning('로고 이미지 읽기 오류: %s', error)
        self.build_gas_panel()
        self.build_search_panel()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(50, self.update)

    def open_history(self):
        """SQLite 탐사 이력 조회 창은 중복으로 열지 않습니다."""
        if self.history_store is None:
            messagebox.showerror('탐사 이력 오류',
                f'데이터베이스를 열 수 없습니다.\n{self.history_error}')
            return
        if self.history_window is not None and self.history_window.winfo_exists():
            self.refresh_history()
            self.history_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.history_window = window
        window.title('위험 구역 탐사 이력 관리')
        window.geometry('930x600')
        window.configure(bg='#f5f7fa')
        window.transient(self.root)
        window.protocol('WM_DELETE_WINDOW', self.close_history)
        tk.Label(window, text='위험 구역 탐사 이력 관리', bg='#f5f7fa', fg='#12304c',
                 font=('맑은 고딕', 19, 'bold')).pack(anchor='w', padx=20, pady=(16, 5))
        tk.Label(window, text='16개 검색 항목 모두 발견 후 [닫기] → [예]를 선택한 이력만 저장됩니다.',
                 bg='#f5f7fa', fg='#4a5564', font=('맑은 고딕', 11)).pack(anchor='w', padx=21)
        table_holder = tk.Frame(window, bg='#f5f7fa')
        table_holder.pack(fill='both', expand=True, padx=20, pady=(14, 6))
        columns = ('번호', '날짜', '시간', '탐사 항목', '이상 유무')
        style = ttk.Style(window)
        style.configure('History.Treeview', font=('맑은 고딕', 12), rowheight=32)
        style.configure('History.Treeview.Heading', font=('맑은 고딕', 12, 'bold'))
        self.history_table = ttk.Treeview(table_holder, columns=columns, show='headings',
                                          selectmode='browse', style='History.Treeview')
        widths = {'번호': 80, '날짜': 190, '시간': 170, '탐사 항목': 280, '이상 유무': 130}
        for col in columns:
            self.history_table.heading(col, text=col)
            self.history_table.column(col, width=widths[col], anchor='center', stretch=True)
        scrollbar = ttk.Scrollbar(table_holder, orient='vertical', command=self.history_table.yview)
        self.history_table.configure(yscrollcommand=scrollbar.set)
        self.history_table.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.history_table.tag_configure('anomaly', foreground='#b71c1c')
        self.history_table.tag_configure('normal', foreground='#237a3b')
        self.history_table.bind('<<TreeviewSelect>>', self.on_history_selection)
        self.history_table.bind('<Double-1>', self.history_double_click)
        self.history_count = tk.StringVar(value='')
        tk.Label(window, textvariable=self.history_count, bg='#f5f7fa', fg='#546477',
                 font=('맑은 고딕', 10)).pack(anchor='w', padx=22, pady=(0, 2))
        self.history_selected_text = tk.StringVar(value='기록을 선택하면 해당 사진을 볼 수 있습니다.')
        tk.Label(window, textvariable=self.history_selected_text, bg='#f5f7fa', fg='#16436a',
                 font=('맑은 고딕', 11, 'bold')).pack(anchor='w', padx=22, pady=(0, 6))
        buttons = tk.Frame(window, bg='#f5f7fa')
        buttons.pack(fill='x', padx=20, pady=(0, 18))
        for text, callback, bg in (
            ('사진 보기', self.view_history_photo, '#1565c0'),
            ('새로고침', self.refresh_history, '#237a3b'),
            ('선택 기록 삭제', self.delete_history_item, '#a83b3b'),
            ('닫기', self.close_history, '#424242')):
            # Windows에서는 비활성(disabled) 버튼 글자가 회색으로 바뀝니다.
            # 버튼은 항상 활성 상태로 두고, 항목 미선택은 클릭 시 안내합니다.
            button = tk.Button(buttons, text=text, command=callback,
                               bg=bg, fg='#FFFFFF',
                               activebackground=bg, activeforeground='#FFFFFF',
                               disabledforeground='#FFFFFF',
                               state='normal',
                               font=('맑은 고딕', 12, 'bold'), cursor='hand2',
                               padx=14, pady=7)
            button.pack(side='left', padx=(0, 10))
            if text == '사진 보기':
                self.history_photo_button = button
            elif text == '선택 기록 삭제':
                self.history_delete_button = button
        self.refresh_history()

    def close_history(self):
        if self.history_photo_window is not None and self.history_photo_window.winfo_exists():
            self.history_photo_window.destroy()
        self.history_photo_window = None
        self.history_photo_record_id = None
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.destroy()
        self.history_window = None
        self.history_table = None
        self.history_photo_button = None
        self.history_delete_button = None
        self.history_selected_text = None

    def on_history_selection(self, event=None):
        """행 선택 안내를 갱신합니다. 버튼은 항상 흰 글씨로 표시합니다."""
        if self.history_table is None:
            return
        selected = self.history_table.selection()
        valid = bool(selected and self.history_table.exists(selected[0]))
        # 버튼을 disabled로 바꾸지 않습니다. 미선택은 selected_history_id()에서 차단.
        if self.history_selected_text is not None:
            if valid:
                values = self.history_table.item(selected[0], 'values')
                self.history_selected_text.set(
                    f'선택한 기록: {values[0]}번 / {values[3]} / {values[1]} {values[2]}  →  [사진 보기]')
            else:
                self.history_selected_text.set('기록을 선택하면 해당 사진을 볼 수 있습니다.')

    def history_double_click(self, event):
        """빈 행을 더블클릭해 기존 선택 사진을 잘못 열지 않도록 처리합니다."""
        if self.history_table is None:
            return
        row = self.history_table.identify_row(event.y)
        if row:
            self.history_table.selection_set(row)
            self.history_table.focus(row)
            self.view_history_photo()

    def refresh_history(self):
        if self.history_table is None:
            return
        try:
            rows = self.history_store.list_inspections()
        except sqlite3.Error as error:
            messagebox.showerror('이력 조회 오류', str(error), parent=self.history_window)
            return
        tree = self.history_table
        old_selection = tree.selection()
        old_id = old_selection[0] if old_selection else None
        children = tree.get_children()
        if children:
            tree.delete(*children)
        # 화면 번호는 항상 1, 2, 3... 순서로 다시 부여합니다.
        # Treeview의 iid에는 SQLite의 원래 id를 그대로 유지하여
        # 삭제·사진 조회가 반드시 정확한 DB 레코드를 참조하도록 합니다.
        for display_number, (row_id, date, time_text, item, abnormal) in enumerate(rows, start=1):
            tree.insert('', 'end', iid=str(row_id),
                        values=(display_number, date, time_text, item, abnormal),
                        tags=('anomaly' if abnormal == '이상' else 'normal',))
        # 새로고침 후에도 이전에 선택한 행이 남아 있다면 같은 기록을 다시 선택합니다.
        if old_id is not None and tree.exists(old_id):
            tree.selection_set(old_id)
            tree.focus(old_id)
            tree.see(old_id)
        self.on_history_selection()
        self.history_count.set(f'최근 {len(rows)}건 표시 | 목록에서 선택한 뒤 [사진 보기] 클릭 또는 더블클릭')

    def selected_history_id(self):
        if self.history_table is None:
            return None
        selected = self.history_table.selection()
        if not selected or not self.history_table.exists(selected[0]):
            messagebox.showinfo('탐사 이력', '목록에서 작업할 기록을 먼저 선택해 주세요.',
                                parent=self.history_window)
            return None
        return int(selected[0])

    def view_history_photo(self):
        row_id = self.selected_history_id()
        if row_id is None:
            return
        try:
            record = self.history_store.get_inspection(row_id)
            if record is None:
                self.refresh_history()
                return
            _, date, time_text, item, abnormal, crop_rel, overview_rel, match_score = record
            photo_images = []
            for path in (overview_rel, crop_rel):
                with Image.open(self.history_store.photo_file(path)) as img:
                    photo_images.append(img.convert('RGB').copy())
        except (OSError, ValueError, sqlite3.Error) as error:
            messagebox.showerror('이미지 열기 오류', str(error), parent=self.history_window)
            return
        # 이미 열린 동일 사진 창은 앞으로 가져오며, 다른 행이면 해당 사진으로 교체합니다.
        if self.history_photo_window is not None and self.history_photo_window.winfo_exists():
            if self.history_photo_record_id == row_id:
                self.history_photo_window.lift()
                self.history_photo_window.focus_force()
                return
            self.history_photo_window.destroy()
        preview = tk.Toplevel(self.history_window)
        self.history_photo_window = preview
        self.history_photo_record_id = row_id
        preview.title(f'{item} · {abnormal} · {date} {time_text}')
        preview.geometry('970x640')
        preview.configure(bg='#f5f7fa')
        preview.transient(self.history_window)
        preview.protocol('WM_DELETE_WINDOW', self.close_history_photo)
        tk.Label(preview, text=f'{item}  |  {date} {time_text}  |  {abnormal}',
                 font=('맑은 고딕', 15, 'bold'), fg=inspection_color(item),
                 bg='#f5f7fa').pack(padx=18, pady=(14, 5))
        pane = tk.Frame(preview, bg='#f5f7fa')
        pane.pack(padx=15, pady=10)
        preview._photos = []  # 윈도우가 열려 있는 동안 Tk PhotoImage 참조 유지
        for i, (title, img, size) in enumerate(zip(
                ('발견 당시 전체 영상', '검출 부분 확대'), photo_images,
                ((600, 450), (290, 330)))):
            col = tk.Frame(pane, bg='#f5f7fa')
            col.grid(row=0, column=i, padx=7, sticky='n')
            tk.Label(col, text=title, bg='#f5f7fa', font=('맑은 고딕', 12, 'bold')).pack(pady=5)
            resized = ImageOps.contain(img, size, Image.Resampling.LANCZOS)
            shown = ImageTk.PhotoImage(resized)
            preview._photos.append(shown)
            tk.Label(col, image=shown, bg='#20252c').pack()
        tk.Label(preview, text=(
            '사진 비교 일치 강도: %.2f (정확도 확률이 아닙니다)' if item in DEFECT_TARGETS
            else 'YOLO 탐지 신뢰도: %.2f (구역 안전 판정이 아닙니다)') % match_score,
                 bg='#f5f7fa', fg='#5d6773', font=('맑은 고딕', 10)).pack(pady=(4, 8))
        tk.Button(preview, text='닫기', command=self.close_history_photo,
                  bg='#1565c0', fg='#FFFFFF',
                  activebackground='#1565c0', activeforeground='#FFFFFF',
                  font=('맑은 고딕', 12, 'bold'), width=18).pack(pady=(0, 16))
        preview.lift()

    def close_history_photo(self):
        if self.history_photo_window is not None and self.history_photo_window.winfo_exists():
            self.history_photo_window.destroy()
        self.history_photo_window = None
        self.history_photo_record_id = None

    def delete_history_item(self):
        row_id = self.selected_history_id()
        if row_id is None:
            return
        # 확인 창에도 사용자가 실제 화면에서 보는 연속 번호를 사용합니다.
        # 삭제 대상은 아래의 원래 SQLite ID(row_id)이므로 사진 연결이 유지됩니다.
        displayed = self.history_table.item(str(row_id), 'values')
        display_number = displayed[0] if displayed else str(row_id)
        display_item = displayed[3] if displayed else '선택 기록'
        if not messagebox.askyesno('이력 삭제 확인',
                                  f'{display_number}번 ({display_item}) 이력과 저장된 사진 2장을 삭제하시겠습니까?',
                                  parent=self.history_window):
            return
        try:
            self.history_store.delete_inspection(row_id)
            if self.history_photo_record_id == row_id:
                self.close_history_photo()
            self.refresh_history()
        except (OSError, sqlite3.Error) as error:
            messagebox.showerror('이력 삭제 오류', str(error), parent=self.history_window)

    def build_search_panel(self):
        group = tk.LabelFrame(self.root, text=' 물체 검색하기 ', bg='#f5f7fa',
                              fg='#142638', font=('맑은 고딕', 14, 'bold'),
                              bd=2, relief='groove')
        group.place(x=1130, y=25, width=340, height=834)
        for y, label, value in [(15, '검색 안 함', 'off'), (60, '특정 물체 검색', 'on')]:
            tk.Radiobutton(group, text=label, value=value, variable=self.search_mode,
                           command=self.search_selection_changed, bg='#f5f7fa',
                           fg='black', font=('맑은 고딕', 13), cursor='hand2',
                           anchor='w', justify='left', padx=0).place(
                               x=18, y=y, width=290, height=34)
        tk.Label(group, text='찾을 물체 (빨강: 이상 / 검정: 일반)', bg='#f5f7fa', fg='black',
                 font=('맑은 고딕', 11, 'bold')).place(x=18, y=111)
        self.target_list = tk.Listbox(group, selectmode=tk.SINGLE, exportselection=False,
            font=('맑은 고딕', 14), bg='white', fg='black',
            selectbackground='#1565c0', selectforeground='white',
            activestyle='none', height=16, relief='solid', bd=1,
            highlightthickness=1, cursor='hand2')
        for name in SEARCH_TARGETS:
            self.target_list.insert(tk.END, name)
            if name in ANOMALY_TARGETS:
                # 선택했을 때도 빨간 글자가 보이도록 선택 색상까지 항목별 설정.
                self.target_list.itemconfig(tk.END,
                    foreground='#b71c1c', background='#fff6f6',
                    selectforeground='#b71c1c', selectbackground='#ffdede')
        self.target_list.selection_set(list(SEARCH_TARGETS).index(self.search_target.get()))
        # 16개 항목이 스크롤 없이 모두 보이도록 높이를 410→454px로 확대합니다.
        # 스크롤바를 제거하고 가로 폭도 300px로 사용합니다.
        self.target_list.place(x=18, y=142, width=300, height=454)
        self.target_list.bind('<<ListboxSelect>>', self.target_selected)
        # 확대된 리스트박스 아래로 버튼/안내문을 순서대로 내립니다.
        for y, label, action, color in [(606, '검색 시작', self.start_search, '#1565c0'),
                                        (656, '검색 중지', self.cancel_search, '#424242')]:
            tk.Button(group, text=label, command=action, bg=color, fg='white',
                      activebackground=color, activeforeground='white', cursor='hand2',
                      font=('맑은 고딕', 13, 'bold')).place(x=18, y=y, width=300, height=40)
        tk.Label(group, textvariable=self.search_status, bg='#f5f7fa', fg='#1255b0',
                 font=('맑은 고딕', 12, 'bold'), wraplength=295,
                 justify='left').place(x=18, y=705, width=300, height=44)
        tk.Label(group, text='발견 시 모든 제어 모드에서 정지\n사진 창을 닫은 후 다시 조종 가능',
                 bg='#f5f7fa', fg='black', font=('맑은 고딕', 11),
                 justify='left', anchor='w').place(x=18, y=756, width=300, height=48)

    def target_selected(self, event=None):
        selection = self.target_list.curselection()
        if not selection:
            return
        target = self.target_list.get(selection[0])
        if target != self.search_target.get():
            self.search_target.set(target)
            self.search_selection_changed()
            if self.yolo_enabled.get() and not self.search_locked:
                self.detector.configure(True, self.preview_target())
                self.clear_analysis()
            threading.Thread(target=prepare_search_audio,
                             args=(target,), daemon=True).start()

    def search_selection_changed(self, event=None):
        self.cancel_search()
        if not self.search_locked:
            self.search_status.set('검색 안 함' if self.search_mode.get() == 'off'
                                   else '대상 선택 후 검색 시작을 누르세요.')

    def cancel_search(self):
        was_active = self.search_active
        self.search_active = False
        self.search_hits = 0
        self.helmet_last_center = None
        self.search_key = None
        self.analysis_rows = None
        if was_active:
            self.stop()
            # 결함 검색이 끝나면 선택된 항목에 맞는 분석 상태로 돌아갑니다.
            self.detector.configure(self.yolo_enabled.get(), self.preview_target())
            self.clear_analysis()
        if not self.search_locked:
            self.search_status.set('검색 중지')

    def start_search(self):
        if self.search_locked:
            if self.found_window is not None:
                self.found_window.lift()
            return
        c = self.connection
        if c is None or not c.connected or c.ended.is_set():
            messagebox.showinfo('물체 검색', '서버에 먼저 접속해 주세요.')
            return
        if not c.stop_ack_supported:
            messagebox.showwarning('물체 검색', '정지 처리 응답을 지원하는 V3 서버가 필요합니다.')
            return
        selected_target = self.search_target.get()
        if selected_target in DEFECT_TARGETS and not DEFECT_MODEL_PATH.is_file():
            messagebox.showwarning('건축물 결함 모델 필요',
                '벽면 균열·노출 철근·누수 흔적은 준비된 사진과 비교합니다.\n\n'
                '제공된 사진 비교 데이터 파일을\n'
                f'{DEFECT_MODEL_PATH}\n경로에 사진 비교 데이터 파일을 복사해 주세요.')
            self.search_status.set('사진 비교 데이터 파일 없음 · models/structural_defects.pt 필요')
            return
        if selected_target == HELMET_TARGET and not HELMET_MODEL_PATH.is_file():
            messagebox.showwarning('안전모 인식 모델 필요',
                '안전모 미 착용 감지는 전용 YOLO 모델이 필요합니다.\n\n'
                'download_helmet_model.py를 실행한 뒤 다시 검색해 주세요.\n'
                f'모델 위치: {HELMET_MODEL_PATH}')
            self.search_status.set('안전모 모델 필요 · models/helmet_detection.pt')
            return
        self.stop()
        self.search_mode.set('on')
        self.search_active = True
        self.active_target = self.search_target.get()
        self.search_hits = 0
        self.helmet_last_center = None
        self.search_key = None
        self.search_last_time = 0.0
        self.found_record = None
        self.analysis_rows = None
        self.search_started = monotonic()
        self.search_session = uuid.uuid4().hex  # 1회 검색에서 DB 기록은 최대 1건
        self.yolo_enabled.set(True)
        self.detector.configure(True, self.active_target)  # 선택 대상에 맞게 모델 전환
        self.clear_analysis()
        self.search_status.set(f'{self.active_target} 검색 중 · 영상 분석 준비')
        threading.Thread(target=prepare_search_audio,
                         args=(self.active_target,), daemon=True).start()

    def update_search(self):
        c = self.connection
        if self.search_locked:
            self.stop()
            record = self.found_record
            if record and c is record['connection']:
                with c.lock:
                    status = dict(c.status)
                    fresh = monotonic()-c.status_time < 1
                    connected = c.connected and not c.ended.is_set()
                acknowledged = (connected and fresh and
                    status.get('stop_ack_id') == record['stop_id'] and
                    status.get('direction') == 'STOP' and status.get('pwm') == 0 and
                    not status.get('motor_error') and
                    finite_number(status.get('motor_age')) and status['motor_age'] < 1)
                if acknowledged:
                    record['acknowledged'] = True
                    self.found_stop_text.set('정지 명령 처리 확인 · 이동 조작 잠금')
                elif not connected or monotonic()-record['requested_at'] > 2:
                    self.found_stop_text.set('정지 확인 안 됨 · 연결 및 차량 상태를 확인하세요.')
            return
        if not self.search_active:
            return
        if c is None or not c.connected or c.ended.is_set():
            self.cancel_search()
            self.search_status.set('연결 종료 · 다시 접속 후 검색 시작')
            return
        result, message = self.detector.read_search()
        if result is None:
            if message.startswith('영상 분석 오류:'):
                self.cancel_search()
                self.yolo_enabled.set(False)
            self.search_status.set(message)
            return
        epoch, seq, frame, detections, received = result
        fresh_limit = 3.0 if self.active_target in DEFECT_TARGETS else 2.0 if self.active_target == HELMET_TARGET else 1.5
        if received < self.search_started or monotonic()-received > fresh_limit:
            self.search_hits = 0
            self.search_status.set('분석 영상 지연 · 최신 결과 대기')
            return
        if self.search_key == (epoch, seq):
            return
        self.search_key = (epoch, seq)
        candidates = [d for d in detections if d[0] == SEARCH_TARGETS[self.active_target]
                      and d[1] >= (0.50 if self.active_target == HELMET_TARGET else SEARCH_CONFIDENCE)]
        gap_limit = 3.0 if self.active_target in DEFECT_TARGETS else 2.5 if self.active_target == HELMET_TARGET else 1.0
        if received-self.search_last_time > gap_limit:
            self.search_hits = 0
            self.helmet_last_center = None
        self.search_last_time = received
        if not candidates:
            self.search_hits = 0
            self.helmet_last_center = None
            self.search_status.set(f'{self.active_target} 검색 중')
            return
        if self.active_target == HELMET_TARGET:
            # 서로 다른 사람의 탐지를 섞어 3회 확인으로 계산하지 않습니다.
            selected = max(candidates, key=lambda d: d[1])
            x1, y1, x2, y2 = selected[2]
            center = ((x1+x2)/2, (y1+y2)/2)
            prev = self.helmet_last_center
            if prev is None or math.hypot(center[0]-prev[0], center[1]-prev[1]) > max(110, frame.width*0.25):
                self.search_hits = 0
            self.helmet_last_center = center
        self.search_hits += 1
        self.search_status.set(f'{self.active_target} 확인 중 {self.search_hits}/{SEARCH_CONFIRM_FRAMES}')
        if self.search_hits < SEARCH_CONFIRM_FRAMES:
            return
        _, confidence, box = max(candidates, key=lambda d: d[1])
        x1, y1, x2, y2 = box
        bounds = (max(0, int(x1)), max(0, int(y1)),
                  min(frame.width, int(x2+1)), min(frame.height, int(y2+1)))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return
        # 네트워크 작업의 잠금과 동일한 잠금으로 이동을 차단합니다.
        # 이미 전송 중이던 한 명령 이후에도 STOP을 계속 전송하여 정지 상태를 유지합니다.
        self.search_locked = True
        self.search_active = False
        stop_id = c.block_motion()
        self.stop()
        self.found_record = dict(target=self.active_target, image=frame.crop(bounds).copy(),
            scene=frame.copy(), bounds=bounds, confidence=confidence,
            timestamp=datetime.now(), connection=c, stop_id=stop_id,
            requested_at=monotonic(), acknowledged=False)
        self.analysis_rows = None
        # 발견 순간에는 DB나 사진 파일에 기록하지 않습니다.
        # 사용자가 발견 창의 [닫기] → [예]를 선택해야만 16개 항목 모두 사진 2장과 이력이 저장됩니다.
        # [아니요]는 저장 없이 닫고, [취소]는 창을 유지합니다.
        self.search_status.set(
            f'{display_target(self.active_target)} 발견 · {inspection_status(self.active_target)} · 이동 잠금')
        self.show_found()
        speak_found(self.active_target)  # 선택한 이름을 여성 음성으로 안내합니다.

    def show_found(self):
        record = self.found_record
        window = tk.Toplevel(self.root)
        self.found_window = window
        finding = inspection_status(record['target'])
        window.title(f"검색 대상 발견 — {display_target(record['target'])} ({finding})")
        window.configure(bg='#f5f7fa')
        window.transient(self.root)
        # grab_set/wait_window 없이 메인 화면의 전체 정지와 통신 갱신을 유지합니다.
        window.bind('<space>', lambda event: self.stop())
        tk.Label(window, text=f"{display_target(record['target'])} 발견 — {finding}",
                 font=('맑은 고딕', 19, 'bold'),
                 bg='#f5f7fa', fg=inspection_color(record['target'])).pack(padx=20, pady=12)
        if record['target'] not in ANOMALY_TARGETS:
            tk.Label(window, text='정상은 일반 검색 항목이라는 뜻이며 구역 전체의 안전 판정은 아닙니다.',
                     bg='#f5f7fa', fg='#236348', font=('맑은 고딕', 10),
                     wraplength=540).pack(padx=20, pady=(0, 4))
        picture = ImageOps.contain(record['image'], (560, 380), Image.Resampling.LANCZOS)
        self.found_photo = ImageTk.PhotoImage(picture)
        tk.Label(window, image=self.found_photo, bg='black').pack(padx=20, pady=5)
        tk.Label(window, text=record['timestamp'].strftime('발견 시간: %Y-%m-%d %H:%M:%S'),
                 bg='#f5f7fa', fg='black', font=('맑은 고딕', 12)).pack(pady=8)
        tk.Label(window, text='아직 저장하지 않았습니다. 닫기 버튼에서 저장 여부를 선택하세요.',
                 bg='#f5f7fa', fg='#1255b0', font=('맑은 고딕', 11, 'bold'),
                 wraplength=540).pack(padx=20, pady=3)
        self.found_stop_text = tk.StringVar(value='정지 명령 전송 · 처리 응답 대기')
        tk.Label(window, textvariable=self.found_stop_text, bg='#f5f7fa', fg='#c62828',
                 wraplength=540, font=('맑은 고딕', 12, 'bold')).pack(padx=20, pady=5)
        tk.Label(window, text='이 창을 닫은 후 다시 조종할 수 있습니다.',
                 bg='#f5f7fa', fg='black', font=('맑은 고딕', 12)).pack(padx=20, pady=5)
        tk.Button(window, text='전체 정지', command=self.stop, bg='#c62828', fg='white',
                  font=('맑은 고딕', 13, 'bold'), cursor='hand2').pack(fill='x', padx=20, pady=5)
        tk.Button(window, text='닫기', command=self.close_found, bg='#1565c0', fg='white',
                  font=('맑은 고딕', 14, 'bold'), cursor='hand2').pack(fill='x', padx=20, pady=12)
        window.protocol('WM_DELETE_WINDOW', self.close_found)
        window.lift()

    def close_found(self):
        if self.found_window is None:
            return True
        if getattr(self, 'found_prompt_open', False):
            return False
        self.stop()
        self.found_prompt_open = True
        try:
            record = self.found_record
            status = inspection_status(record['target'])
            answer = messagebox.askyesnocancel(
                '탐사 이력 저장',
                f"{display_target(record['target'])} 발견 사진을 확인하셨습니까?\n"
                f"판정: {status}\n\n"
                '이 탐사 결과를 데이터베이스에 저장하시겠습니까?\n'
                '예: 전체 사진과 부분 사진을 저장하고 SQLite에 기록\n'
                '아니요: 사진과 이력을 저장하지 않고 닫기\n'
                '취소: 창을 닫지 않고 사진 다시 확인',
                parent=self.found_window)
            if answer is None:
                return False  # 취소: 발견 창과 이동 잠금을 유지
            if answer:
                if self.history_store is None:
                    messagebox.showerror('탐사 이력 저장 오류',
                        f'데이터베이스를 열 수 없습니다.\n{self.history_error}',
                        parent=self.found_window)
                    return False
                try:
                    record_id = self.history_store.record_inspection(
                        record, self.search_session)
                except (OSError, ValueError, sqlite3.Error) as error:
                    LOGGER.exception('사용자가 승인한 탐사 이력 저장 실패')
                    messagebox.showerror('탐사 이력 저장 오류',
                        f'사진 및 데이터베이스 저장에 실패했습니다.\n{error}\n\n'
                        '저장되지 않았습니다. 다시 시도하거나 아니요를 선택해 주세요.',
                        parent=self.found_window)
                    return False
                if self.history_window is not None and self.history_window.winfo_exists():
                    self.refresh_history()
                completion = f'{display_target(record["target"])} · {status} · 이력 {record_id}번 저장 완료'
            else:
                completion = f'{display_target(record["target"])} · {status} · 저장하지 않고 닫음'
            self.found_window.destroy()
            self.found_window = None
            self.found_photo = None
            self.stop()
            if self.connection:
                self.connection.unblock_motion()
            self.search_locked = False
            self.found_record = None
            self.detector.configure(self.yolo_enabled.get(), self.preview_target())
            self.clear_analysis()
            self.search_status.set(completion)
            return True
        finally:
            self.found_prompt_open = False

    def build_gas_panel(self):
        """넓어진 하단 그룹박스에 센서 값과 공통 안전 경고를 표시합니다."""
        PANEL_TOP = 780
        PANEL_WIDTH = 350
        PANEL_HEIGHT = 250

        def panel(title, x):
            group = tk.LabelFrame(
                self.root, text=title, bg='#f5f7fa', fg='#142638',
                font=('맑은 고딕', 13, 'bold'), bd=1, relief='solid')
            group.place(x=x, y=PANEL_TOP,
                        width=PANEL_WIDTH, height=PANEL_HEIGHT)
            return group

        # 공기질 모니터링: 항목 이름 12pt, 상태·측정값 14pt
        gas = panel(' 공기질 모니터링 (MQ-135) ', 20)
        self.gas_connection = tk.StringVar(value='수신 대기')
        self.gas_exposure = tk.StringVar(value='확인 불가')
        self.gas_concentration = tk.StringVar(value='-- V')
        self.gas_value_label = tk.StringVar(value='센서 전압 (AO)')
        for y, label, value in ((15, '센서 상태', self.gas_connection),
                                (67, '센서 반응 판정', self.gas_exposure),
                                (119, None, self.gas_concentration)):
            name_kw = {'text': label} if label is not None else {'textvariable': self.gas_value_label}
            tk.Label(gas, **name_kw, bg='#f5f7fa', fg='#182737',
                     font=('맑은 고딕', 12), anchor='w').place(
                         x=16, y=y, width=158, height=33)
            result = tk.Label(
                gas, textvariable=value, bg='#e5e9ef',
                fg='#c62828' if value is self.gas_concentration else '#102f4a',
                font=('맑은 고딕', 14, 'bold'), anchor='w', padx=4)
            result.place(x=180, y=y, width=152, height=34)
            if value is self.gas_exposure:
                self.gas_result_widget = result
        self.gas_note = tk.StringVar(value='MQ-135 / ADS1115: 측정값 수신 대기')
        tk.Label(gas, textvariable=self.gas_note,
                 bg='#f5f7fa', fg='#3d5366',
                 font=('맑은 고딕', 11), anchor='nw', justify='left',
                 wraplength=312).place(x=16, y=164, width=316, height=38)

        # 온·습도 모니터링: 센서 수치를 더 큰 글자로 표시
        climate = panel(' 온·습도 모니터링 ', 395)
        self.temperature_text = tk.StringVar(value='-- °C')
        self.humidity_text = tk.StringVar(value='-- % RH')
        self.dht_text = tk.StringVar(value='DHT22: 측정값 수신 대기')
        for y, label, value in ((25, '온도:', self.temperature_text),
                                (91, '습도:', self.humidity_text)):
            tk.Label(climate, text=label, bg='#f5f7fa', fg='#182737',
                     font=('맑은 고딕', 14), anchor='w').place(
                         x=22, y=y, width=95, height=36)
            tk.Label(climate, textvariable=value, bg='#f5f7fa',
                     fg='#c62828' if value is self.temperature_text else '#1b5f99',
                     font=('맑은 고딕', 17, 'bold'), anchor='w').place(
                         x=128, y=y, width=205, height=36)
        tk.Label(climate, textvariable=self.dht_text,
                 bg='#f5f7fa', fg='#3d5366',
                 font=('맑은 고딕', 11), anchor='nw', justify='left',
                 wraplength=305).place(x=22, y=164, width=309, height=38)

        # 제어 방식: 그룹박스 내부에 높이를 확보해 경고문이 절대 잘리지 않도록 함.
        modes = panel(' 제어 방식 ', 770)
        for y, label, value in ((7, '일반 제어', 'auto'),
                                (48, '정밀 제어', 'precision'),
                                (89, '초 정밀 제어', 'ultra')):
            tk.Radiobutton(
                modes, text=label, value=value, variable=self.control_mode,
                command=self.stop, bg='#f5f7fa', fg='#182737',
                activebackground='#f5f7fa', cursor='hand2',
                font=('맑은 고딕', 13)).place(x=22, y=y, height=34)
        validate = (self.root.register(
            lambda v: v == '' or (v.isascii() and v.isdigit() and 1 <= int(v) <= 50)), '%P')
        tk.Entry(modes, textvariable=self.travel_cm, width=3,
                 font=('맑은 고딕', 13), validate='key',
                 validatecommand=validate, fg='black').place(x=204, y=94, height=32)
        tk.Label(modes, text='cm (1~50)', bg='#f5f7fa', fg='#182737',
                 font=('맑은 고딕', 11)).place(x=260, y=98)

        # 일반·정밀·초정밀 제어에 공통인 전방 장애물 경고
        self.safety_panel = tk.Frame(
            modes, bg='#fff4d6', highlightbackground='#d89e24',
            highlightthickness=1, bd=0)
        self.safety_panel.place(x=12, y=139, width=326, height=76)
        self.safety_icon = tk.Label(
            self.safety_panel, text='⚠', font=('Segoe UI Symbol', 22, 'bold'),
            bg='#fff4d6', fg='#9a6200', bd=0)
        self.safety_icon.place(x=8, y=12, width=35, height=46)
        self.safety_message = tk.Label(
            self.safety_panel,
            text='모든 제어: 전방 장애물 30cm 이내\n전진 동작이 제한됩니다.',
            font=('맑은 고딕', 11, 'bold'), bg='#fff4d6', fg='#754d00',
            anchor='w', justify='left', bd=0, wraplength=270)
        self.safety_message.place(x=48, y=7, width=269, height=60)

    def update_safety_warning(self, status, connected, fresh):
        """전방 장애물 감지 상태에 따라 안전 안내 색상과 거리를 변경합니다."""
        distance = status.get('distance_cm') if isinstance(status, dict) else None
        if connected and fresh and finite_number(distance) and 0 <= distance <= STOP_DISTANCE_CM:
            # 30cm 이내: 전진 계열 동작이 실제로 차단되는 상태
            message = f'전방 장애물 감지: {distance:.1f}cm!\n전진 동작이 제한됩니다.'
            bg, fg, icon_color, border = '#ffebee', '#9f1c20', '#c62828', '#c62828'
        elif connected and fresh and (not finite_number(distance) or distance < 0):
            # 센서 응답이 없을 때도 전진을 허용하지 않습니다.
            message = '전방 거리 측정 불가\n안전을 위해 전진이 제한됩니다.'
            bg, fg, icon_color, border = '#fff0dc', '#824900', '#ad6700', '#d89e24'
        else:
            # 30cm를 초과했거나 아직 접속하지 않은 상태의 기본 안내
            message = '모든 제어: 전방 장애물 30cm 이내\n전진 동작이 제한됩니다.'
            bg, fg, icon_color, border = '#fff4d6', '#754d00', '#9a6200', '#d89e24'
        self.safety_panel.configure(bg=bg, highlightbackground=border)
        self.safety_icon.configure(bg=bg, fg=icon_color)
        self.safety_message.configure(text=message, bg=bg, fg=fg)

    def update_climate(self, status, connected):
        """마지막 정상값을 유지하고 값의 경과시간/재시도 상태를 별도 표시한다."""
        data = status.get('dht22') if connected else None
        if not isinstance(data, dict):
            data = {}
        t, h, age = (data.get(k) for k in ('temperature_c', 'humidity_pct', 'age_seconds'))
        valid = (finite_number(t) and finite_number(h) and finite_number(age)
                 and -40 <= t <= 80 and 0 <= h <= 100 and age >= 0)
        now = monotonic()
        if valid:
            self.last_climate = (t, h, now-age)
        last = getattr(self, 'last_climate', None)
        if last is None:
            self.temperature_text.set('-- °C')
            self.humidity_text.set('-- % RH')
            message = '첫 측정 대기'
        else:
            t, h, measured = last
            elapsed = max(0.0, now-measured)
            self.temperature_text.set(f'{t:.1f} °C')
            self.humidity_text.set(f'{h:.1f} % RH')
            if not connected:
                message = f'이전값 · {elapsed:.0f}초 전 / 통신 대기'
            elif not valid or data.get('error') or data.get('connected') is False or elapsed > 10:
                message = f'이전값 · {elapsed:.0f}초 전 / 재측정 중'
            else:
                message = f'측정 중 · {elapsed:.1f}초 전'
        self.dht_text.set('DHT22: ' + message)

    def update_gas(self, status, connected):
        # 서버가 보내야 할 mq6 필드:
        # connected: ADC 통신 상태(bool), warming_up: 예열 중(bool),
        # age_seconds: 마지막 성공 측정 이후 시간(매 응답마다 갱신),
        # voltage_v: 분압 전 MQ-135 AO 전압(ADC A0 전압의 약 2배),
        # detected: 보정된 기준 초과 여부(bool), 판정 불가 시 null,
        # calibrated: 농도 보정 완료(bool), ppm: 보정된 농도 또는 null,
        # error: 오류 문자열 또는 null.
        # ADC 통신 성공만으로 MQ-135 전원/배선 정상까지 확인할 수는 없다.
        data = status.get('mq135') if connected else None
        if not isinstance(data, dict):
            data = {}
        age, voltage = data.get('age_seconds'), data.get('voltage_v')
        self.gas_value_label.set('센서 전압 (AO)')
        self.gas_concentration.set('-- V')
        self.gas_exposure.set('확인 불가')
        self.gas_result_widget.configure(fg='#000000', bg='#e5e9ef')
        if not connected:
            state, note = '수신 대기', '서버 연결 / 응답 대기'
        elif not data:
            state, note = '수신 대기', 'MQ-135 데이터 없음'
        elif data.get('connected') is False:
            state, note = '미연결', 'ADC 연결을 확인하세요'
        elif data.get('error'):
            state, note = '측정 오류', '센서 / ADC 상태 확인'
        elif not finite_number(age) or not 0 <= age <= 10:
            state, note = '수신 지연', '유효한 최신 측정값 대기'
        elif data.get('connected') is not True or not finite_number(voltage) or not 0 <= voltage <= 5.5:
            state, note = '측정값 오류', '전압 및 상태 정보 확인'
        else:
            self.gas_concentration.set(f'{voltage:.3f} V')
            if data.get('warming_up') is True:
                state, note = '예열 중', '측정 안정화 대기'
            elif data.get('warming_up') is not False:
                state, note = '상태 대기', '예열 상태 확인 대기'
            else:
                state, note = '수신 중', '미보정: AO 전압 표시'
                ppm = data.get('ppm')
                if data.get('calibrated') is True and finite_number(ppm) and ppm >= 0:
                    self.gas_value_label.set('가스 농도 (추정)')
                    self.gas_concentration.set(f'{ppm:.0f} ppm')
                    note = '보정값 기반 추정 농도'
                detected = data.get('detected')
                if detected is True:
                    self.gas_exposure.set('기준 초과')
                    self.gas_result_widget.configure(fg='white', bg='#c62828')
                elif detected is False:
                    self.gas_exposure.set('기준 미만')
                # null이면 서버에 판정 기준이 없으므로 확인 불가를 유지한다.
        self.gas_connection.set(state)
        self.gas_note.set(note)

    def panel_hover(self, event):
        self.panel.configure(cursor='hand2' if hit_test(event.x, event.y) else 'arrow')

    def panel_leave(self, event=None):
        self.panel.configure(cursor='arrow')
        if self.control_mode.get() == 'precision' and self.direction_pressed:
            self.stop()

    def release(self, event=None):
        # 방향 버튼을 누른 경우에만 해제 시 정지합니다.
        # 슬라이더/접속 버튼/라디오버튼의 해제를 방향 해제로 취급하지 않습니다.
        if self.control_mode.get() == 'precision' and self.direction_pressed:
            self.stop()

    def slider_released(self, event=None):
        self.speed_change(self.pwm.get())
        LOGGER.info('Slider selected: %s mode=%s', round(self.pwm.get()), self.control_mode.get())
        # Scale 기본 해제 처리는 유지합니다. 공통 all 바인딩은 이 위젯에서 제외했습니다.

    def preview_target(self):
        # 물체 검색 시작 버튼을 누르기 전에는 결함 3종을 함께 표시합니다.
        # 사람·승용차 등의 경우 기존 COCO YOLO의 전체 물체 분석을 유지합니다.
        return DEFECT_PREVIEW_ALL if self.search_target.get() in DEFECT_TARGETS else (
            HELMET_TARGET if self.search_target.get() == HELMET_TARGET else None)

    def toggle_yolo(self):
        if not self.yolo_enabled.get():
            self.cancel_search()
        # YOLO 분석은 체크박스로 시작하지만, 결과 창은 버튼으로 따로 엽니다.
        self.detector.configure(
            self.yolo_enabled.get(),
            self.active_target if self.search_active else self.preview_target())
        self.clear_analysis()

    def set_analysis_results(self, rows):
        """탐지 결과 목록은 내용이 바뀔 때만 갱신합니다."""
        if self.analysis_window is None:
            return
        rows = tuple(rows)
        if rows == self.analysis_rows:
            return
        self.analysis_results.delete(0, tk.END)
        for row in rows:
            detected_name = row.partition(':')[0].strip()
            selectable_name = '승용차' if detected_name == '자동차' else detected_name
            is_selectable = selectable_name in SEARCH_TARGETS and ':' in row
            suffix = ('  [이상]' if detected_name in ('안전모 미 착용', '안전모 미착용')
                      else '  [정상]' if detected_name == '안전모 착용'
                      else f'  [{inspection_status(selectable_name)}]' if is_selectable else '')
            self.analysis_results.insert(tk.END, row + suffix)
            if (is_selectable and selectable_name in DEFECT_TARGETS) or detected_name in ('안전모 미 착용', '안전모 미착용'):
                self.analysis_results.itemconfig(tk.END,
                    fg='#b71c1c', bg='#fff2f2',
                    selectforeground='#b71c1c', selectbackground='#ffe1e1')
            selected = (self.found_record['target'] if self.found_record
                        else self.search_target.get())
            if (self.search_active or self.found_record) and selectable_name == selected:
                if selectable_name in ANOMALY_TARGETS and detected_name != '안전모 착용':
                    self.analysis_results.itemconfig(tk.END, bg='#ffe0e0', fg='#a41111')
                else:
                    self.analysis_results.itemconfig(tk.END, bg='#dceee4', fg='#155c3d')
        self.analysis_rows = rows

    def clear_analysis(self):
        self.analysis_key = None
        if self.analysis_window is not None:
            self.analysis_canvas.itemconfigure(self.analysis_image, image='')
            self.analysis_canvas.itemconfigure(self.analysis_overlay, state='normal',
                                               text='분석 영상 대기')
            self.analysis_status.set('영상 분석 대기')
            self.set_analysis_results(['새로운 분석 결과를 기다리는 중입니다.'])

    def open_analysis(self):
        if self.analysis_window is not None:
            self.analysis_window.lift()
            self.analysis_window.focus_force()
            return
        window = tk.Toplevel(self.root)
        self.analysis_window = window
        window.title('영상 물체 분석 — 탐지 결과')
        # 영상 640x480 아래 목록을 배치하도록 세로 높이를 확장합니다.
        window.geometry('700x820')
        window.minsize(680, 760)
        window.configure(bg='#edf2f7')

        self.analysis_canvas = tk.Canvas(
            window, width=640, height=480, bg='black', highlightthickness=0)
        self.analysis_canvas.pack(padx=18, pady=(12, 8))
        self.analysis_image = self.analysis_canvas.create_image(320, 240)
        self.analysis_overlay = self.analysis_canvas.create_text(
            320, 240, text='분석 영상 대기', fill='white', font=('맑은 고딕', 14))

        # 영상 바로 아래 목록 박스: 탐지한 종류별 개수를 한 줄씩 표시합니다.
        results_frame = tk.LabelFrame(
            window, text='탐지 결과(개수)', font=('맑은 고딕', 14, 'bold'),
            fg='#163653', bg='#f5f7fa', bd=2, relief='groove')
        results_frame.pack(fill='both', expand=True, padx=20, pady=(6, 8))
        list_area = tk.Frame(results_frame, bg='white')
        list_area.pack(fill='both', expand=True, padx=12, pady=10)
        self.analysis_results = tk.Listbox(
            list_area, font=('맑은 고딕', 13), fg='#173b5b', bg='white',
            selectbackground='#c5dff7', activestyle='none',
            height=8, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            list_area, orient='vertical', command=self.analysis_results.yview)
        self.analysis_results.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.analysis_results.pack(side='left', fill='both', expand=True)
        self.analysis_rows = None
        self.set_analysis_results([
            '영상 물체 분석을 체크해 주세요.' if not self.yolo_enabled.get()
            else '서버 영상 및 분석 결과를 기다리는 중입니다.'])

        self.analysis_status = tk.StringVar(value='조종은 메인 화면의 원본 영상을 이용하세요.')
        tk.Label(window, textvariable=self.analysis_status, bg='#edf2f7',
                 fg='#264e71', font=('맑은 고딕', 11),
                 wraplength=645, anchor='w').pack(
                     fill='x', padx=22, pady=(0, 12))
        window.protocol('WM_DELETE_WINDOW', self.close_analysis)
        self.analysis_key = None

    def close_analysis(self):
        if self.analysis_window is not None:
            self.analysis_window.destroy()
            self.analysis_window = None
            self.analysis_photo = None
            self.analysis_key = None
            self.analysis_rows = None

    def update_analysis(self):
        # 메인 창에는 YOLO 객체 인식 결과를 표시하지 않습니다.
        # 결과와 모델 상태는 분석 창의 탐지 결과 목록에만 표시됩니다.
        if self.analysis_window is None:
            return
        result, message = self.detector.read()
        c = self.connection
        active = c is not None and c.connected and not c.ended.is_set()
        if not self.yolo_enabled.get():
            self.analysis_canvas.itemconfigure(
                self.analysis_overlay, state='normal', text='영상 분석 꺼짐')
            self.analysis_status.set('영상 물체 분석을 체크하면 분석을 시작합니다.')
            self.set_analysis_results(['영상 물체 분석이 꺼져 있습니다.'])
            return
        if not active:
            self.analysis_canvas.itemconfigure(
                self.analysis_overlay, state='normal', text='영상 연결 대기')
            self.analysis_status.set('서버 연결 및 분석 영상 대기')
            self.set_analysis_results(['서버 연결 및 영상 수신을 기다리는 중입니다.'])
            return
        if result is None:
            self.analysis_canvas.itemconfigure(
                self.analysis_overlay, state='normal', text='분석 영상 대기')
            self.analysis_status.set(message)
            self.set_analysis_results([message])
            return

        generation, seq, annotated, summary, received, elapsed = result
        age = monotonic() - received
        preview_fresh_limit = 4 if self.search_target.get() in DEFECT_TARGETS else 3 if self.search_target.get() == HELMET_TARGET else 2
        if age > preview_fresh_limit:
            self.analysis_canvas.itemconfigure(
                self.analysis_overlay, state='normal',
                text='분석 영상 지연 / 원본 영상 확인')
            self.analysis_status.set(f'분석 결과 지연 ({age:.1f}초) — 새 결과 대기')
            self.set_analysis_results(['최신 탐지 결과를 기다리는 중입니다.'])
            return

        if self.analysis_key != (generation, seq):
            self.analysis_photo = ImageTk.PhotoImage(annotated)
            self.analysis_canvas.itemconfigure(
                self.analysis_image, image=self.analysis_photo)
            self.analysis_key = (generation, seq)
        self.analysis_canvas.itemconfigure(self.analysis_overlay, state='hidden')
        mode_name = (f'사진 비교: {self.active_target}' if self.search_active
                     else '사진 비교: 결함 3종') if self.search_target.get() in DEFECT_TARGETS else ('안전모 미 착용 YOLO 분석' if self.search_target.get() == HELMET_TARGET else '일반 YOLO 분석')
        self.analysis_status.set(f'{mode_name} | 분석 {elapsed:.2f}초 | 영상 수신 후 {age:.1f}초 경과')
        if summary == '탐지된 물체 없음':
            rows = ['탐지된 물체 없음']
        else:
            rows = [item.strip() for item in summary.split(', ') if item.strip()]
        self.set_analysis_results(rows)

    def connect(self):
        if self.search_locked:
            messagebox.showinfo('검색 결과', '발견 창을 먼저 닫아 주세요.')
            return
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
        self.update_gas({}, False)
        self.update_climate({}, False)
        self.video.itemconfigure(self.video_item, image='')
        self.video.itemconfigure(self.video_text, text='영상 연결 중...', state='normal')
        self.detector.configure(self.yolo_enabled.get(), self.preview_target())
        self.clear_analysis()
        self.connection = Connection(self.ip.get().strip(), port, round(self.pwm.get()))

    def disconnect(self):
        self.cancel_search()
        self.stop()
        self.update_gas({}, False)
        self.update_climate({}, False)
        if self.connection:
            self.connection.close()
        self.video.itemconfigure(self.video_text, text='연결 해제 / 차량 정지', state='normal')
        self.detector.configure(self.yolo_enabled.get(), self.preview_target())
        self.clear_analysis()

    def stop(self):
        self.direction_pressed = False
        self.held = None
        self.panel.itemconfigure(self.outline, state='hidden')
        if self.connection:
            self.connection.stop()

    def press(self, event):
        region = hit_test(event.x, event.y)
        self.stop()
        if self.search_locked:
            if self.found_window is not None:
                self.found_window.lift()
            return
        if not region or region[0] == 'STOP':
            return
        c = self.connection
        if c is None:
            return
        mode = self.control_mode.get()
        travel = self.travel_cm.get()
        if mode == 'ultra':
            allowed = {'FORWARD_LEFT', 'FORWARD', 'FORWARD_RIGHT'}
            if not travel.isascii() or not travel.isdigit() or not 1 <= int(travel) <= 50:
                messagebox.showwarning('이동 거리', '1~50 사이 정수를 입력해 주세요.')
                return
            if region[0] not in allowed:
                messagebox.showwarning('초 정밀 제어',
                                       '초 정밀 제어에서는 좌측 전진, 전진, 우측 전진만 가능합니다.')
                return
            if not c.distance_supported:
                messagebox.showwarning('서버 업데이트', '새 서버 코드를 먼저 실행해 주세요.')
                return
        selected_pwm = round(self.pwm.get())
        with c.lock:
            c.pwm = selected_pwm
            ready = (not c.motion_blocked and c.connected and not c.ended.is_set() and
                     monotonic()-c.display_time < 1 and c.pwm > 0 and
                     monotonic()-c.status_time < 1 and motion_allowed(region[0], c.status))
            if mode == 'ultra':
                ready = ready and distance_clear(c.status)
            if ready:
                c.mode, c.distance_cm = mode, int(travel) if mode == 'ultra' else 1
                c.move_id += 1
                c.direction = region[0]
        if ready:
            self.direction_pressed = mode == 'precision'
            self.held = region
            self.panel.coords(self.outline, *region[2:])
            self.panel.itemconfigure(self.outline, state='normal')

    def drag(self, event):
        if self.control_mode.get() == 'precision' and self.held and hit_test(event.x, event.y) != self.held:
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
        try:
            self.update_once()
        except Exception:
            LOGGER.exception('UI update failed; motion stopped, display loop will retry')
            with suppress(Exception):
                self.stop()
                self.note.set('화면 갱신 오류로 정지 · logs/client.log 확인')
        finally:
            if not self.closing:
                self.root.after(50, self.update)

    def update_once(self):
        if self.closing:
            return
        # Scale 콜백 누락 여부와 무관하게 실제 슬라이더 값을 통신에 반영합니다.
        selected_pwm = round(self.pwm.get())
        if selected_pwm == 0:
            self.stop()
        c = self.connection
        if c:
            with c.lock:
                c.pwm = selected_pwm
                c.ui_time = monotonic()
                pending, c.pending_frame = c.pending_frame, None
                connected = c.connected and not c.ended.is_set()
                s, message, direction = dict(c.status), c.message, c.direction
                age = monotonic()-c.display_time
                status_fresh = monotonic()-c.status_time < 1
                collision_notice = c.collision_notice
                c.collision_notice = False
            if collision_notice:
                speak_collision()
            if pending and connected:
                seq, frame = pending
                self.photo = ImageTk.PhotoImage(frame)
                self.video.itemconfigure(self.video_item, image=self.photo)
                self.video.itemconfigure(self.video_text, state='hidden')
                with c.lock:
                    c.frame_ack, c.display_time = seq, monotonic()
                self.detector.submit(seq, frame)
                age = 0
            if not status_fresh or not motion_allowed(direction, s):
                self.stop()
            if not connected or age > 1:
                self.stop()
                self.video.itemconfigure(self.video_text, state='normal',
                                         text='영상 없음 / 조작 정지' if connected else '연결 종료 / 조작 정지')
            elif direction == 'STOP' and self.held:
                self.stop()
            self.connection_label.configure(text='연결됨' if connected else '연결 안 됨',
                                            foreground='green' if connected else '#000000')
            can_connect = c.finished.is_set()
            self.connect_button.configure(
                state='normal' if can_connect else 'disabled',
                bg='#237a3b' if can_connect else '#d5ded8')
            distance = s.get('distance_cm')
            distance_text = f'{distance:.1f} cm' if isinstance(distance, (int, float)) and math.isfinite(distance) else '---'
            self.info.set(f"동작: {NAMES.get(s.get('direction'), '정지')}  |  전방 거리: {distance_text}  |  출력 평균 PWM: {s.get('pwm', 0)}%")
            self.motion_text.set(NAMES.get(s.get('direction'), '정지') if connected else '정지')
            self.distance_text.set(distance_text if connected and status_fresh else '---')
            self.update_safety_warning(s, connected, status_fresh)
            self.output_text.set(f"{s.get('pwm', 0)}%" if connected else '0%')
            values = s.get('rpm', [0]*4)
            for i, variable in enumerate(self.rpm_values):
                value = values[i] if isinstance(values, (list, tuple)) and i < len(values) else None
                variable.set(f'{value:.1f}' if connected and status_fresh and finite_number(value) else '---')
            self.rpm.set('    '.join(f'M{i+1}: {variable.get()} RPM' for i, variable in enumerate(self.rpm_values)))
            detail = s.get('motor_error') or s.get('camera_error') or message
            if message in ('STOP', 'RUNNING') and finite_number(s.get('last_stop_age')) and s['last_stop_age'] < 5:
                detail = s.get('last_stop_reason') or detail
            if not distance_clear(s) and not detail:
                detail = '전방 이동 제한 · 초음파 거리 확인'
            mode_help = {'auto': '클릭 후 계속 이동', 'precision': '속도 설정 후 방향 버튼을 누르는 동안 이동',
                         'ultra': f'{self.travel_cm.get()}cm 이동 후 정지'}.get(self.control_mode.get(), '')
            if self.control_mode.get() == 'ultra' and c.ultra_scaled_pwm:
                selected = round(self.pwm.get())
                maximum = 0 if selected == 0 else round(20 + 15*(selected-1)/99)
                mode_help += f' · PWM 상한 {maximum}%'
            self.note.set(f'{detail} | {mode_help} · Space: 정지')
            self.update_climate(s, connected and status_fresh)
            self.update_gas(s, connected and status_fresh)
        else:
            self.update_safety_warning({}, False, False)
            self.update_climate({}, False)
            self.update_gas({}, False)
        self.update_search()
        self.update_analysis()

    def close(self):
        if self.closing:
            return
        self.stop()
        if self.found_window is not None and not self.close_found():
            return
        self.closing = True
        if self.connection:
            self.connection.close()
        self.detector.close()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    def report_error(kind, value, tb):
        LOGGER.error('Tk callback error', exc_info=(kind, value, tb))
        with suppress(Exception):
            app.stop()
    root.report_callback_exception = report_error
    app = CarApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.close()
        print('\n클라이언트를 종료합니다.')
