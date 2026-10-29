# 파일명: car_ws_serverV3.py
# Raspberry Pi 5 / USB 웹캠 / 엔코더 피드백 / Ctrl+C 종료
# 안정화 수정판: 2026-10-21 / 업로드해주신 원본을 기준으로 수정
import asyncio
import json
import math
import multiprocessing as mp
import logging
from logging.handlers import RotatingFileHandler
import os
import struct
import signal
import socket
import sys
import threading
import uuid
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from time import monotonic, sleep

# 가상환경 없이 설치한 로컬 추가 라이브러리를 사용합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))

import cv2
from aiohttp import web, WSMsgType
from smbus2 import SMBus
from gpiozero import DistanceSensor
from gpiozero.pins.lgpio import LGPIOFactory

# DHT22 DAT: GPIO16 (물리36), VCC:3.3V
# ADS1115: 3.3V, 주소0x48(ADDR=GND), A0 <- MQ-135 AO의 10k/10k 분압
DHT_INTERVAL = 2.5
ADS_ADDRESS = 0x48
MQ135_DIVIDER_RATIO = 2.0
MQ135_WARMUP_SECONDS = 180.0  # 시작 후 판정 대기. 초기 에이징/실제 안정화 시간과 다름
MQ135_ALARM_VOLTAGE = None  # 보정한 AO 전압 기준이 있을 때만 지정. 단위 V
# 임의의 ppm 변환은 하지 않습니다. 전압만 전송합니다.
STOP_DISTANCE_CM = 30.0
FORWARD_MOVES = {'FORWARD', 'FORWARD_LEFT', 'FORWARD_RIGHT', 'CURVE_LEFT', 'CURVE_RIGHT'}
MAX_PWM = 100  # 모든 모드와 바퀴에 적용하는 최종 출력 상한
PORT = 5000
I2C_ADDRESS = 0x34
DIRECTION = [-1, 1, 1, -1]
# 초기 가정값입니다. 실제 바퀴 지름(cm)과 실습 결과에 맞게 수정하세요.
WHEEL_DIAMETER_CM = 10.0
# 보정: 새 값 = 기존 값 × 입력 거리 / 실제 이동 거리 (장애물 없이 완료된 경우)
DISTANCE_SCALE = 1.25  # 실측 보정: 50cm 입력 → 40cm 이동, 50/40 = 1.25
DIAGONAL_SCALE = 1.0  # 대각선 이동만 추가 보정
DISTANCE_PWM_LIMIT = 35
DISTANCE_PWM_MIN = 20  # 저속에서 모터가 멈춘다면 실습으로 조정
DISTANCE_SLOWDOWN_CM = 5.0  # 마지막 5cm에서 바퀴별 감속
DISTANCE_ACCEL_PER_SEC = 60.0
DISTANCE_LOOP_SECONDS = 0.01  # 초 정밀 제어의 목표 대기 간격 (실제 주기는 통신시간 포함)
DISTANCE_MOVES = {'FORWARD_LEFT', 'FORWARD', 'FORWARD_RIGHT'}
COUNTS_PER_REV = 2600  # 기존 설정: 실제 바퀴 1회전으로 검증 필요
CAMERA_INDEX = 0
COMMAND_TIMEOUT = 0.8  # 약 20Hz 제어 명령; 0.8초 수신 없으면 강제 정지
VIDEO_TIMEOUT = 1.5
ENCODER_TIMEOUT = 2.5  # PWM 공급 중 엔코더 펄스가 없으면 정지
FEEDBACK_GAIN = 35.0
PWM_CHANGE_RATE = 220.0  # 이전 20.0: 짧은 클릭에서 모터가 거의 가속하지 못함
MIN_PWM_RATIO = 0.7
STATE_FILE = Path('/home/humanoid/Desktop/Car3/car_state.json')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

# M1 왼쪽 앞 / M2 왼쪽 뒤 / M3 오른쪽 앞 / M4 오른쪽 뒤
# 표준 X 배치 메카넘휠 기준. 값의 크기는 바퀴 간 목표 속도 비율.
MOVES = {
    'STOP': (0, 0, 0, 0),
    'FORWARD': (1, 1, 1, 1),
    'BACKWARD': (-1, -1, -1, -1),
    'LEFT': (-1, 1, 1, -1),
    'RIGHT': (1, -1, -1, 1),
    'FORWARD_LEFT': (0, 1, 1, 0),
    'FORWARD_RIGHT': (1, 0, 0, 1),
    'BACKWARD_LEFT': (-1, 0, 0, -1),
    'BACKWARD_RIGHT': (0, -1, -1, 0),
    'TURN_LEFT': (-1, -1, 1, 1),
    'TURN_RIGHT': (1, 1, -1, -1),
    'CURVE_LEFT': (0.4, 0.4, 1, 1),
    'CURVE_RIGHT': (1, 1, 0.4, 0.4),
    'ARC_LEFT': (-1, 0.4, 1, -0.4),
    'ARC_RIGHT': (1, -0.4, -1, 0.4),
}
motor_tick = 0.0
event_loop_tick = 0.0
lock = threading.Lock()
quit_event = threading.Event()
command = dict(direction='STOP', pwm=0.0, time=0.0, frame=-1)
status = dict(direction='STOP', pwm=0, wheel_pwm=[0]*4, rpm=[0]*4,
              distance_cm=None, distance_time=0.0, reason='WAITING', motor_error='',
              motor_tick=0.0, stop_ack_id=0)
sensor_state = {
    'dht22': dict(connected=False, temperature_c=None, humidity_pct=None,
                  last_success=None, error=None),
    'mq135': dict(connected=False, warming_up=True, voltage_v=None,
               adc_voltage_v=None, detected=None, calibrated=False, ppm=None,
               last_success=None, error=None),
}
camera = dict(seq=0, jpeg=None, time=0.0, error='')
frame_times = OrderedDict()
owner_token = None
control_socket = None
video_sockets = set()
control_leases = OrderedDict()


def stop_request():
    with lock:
        command.update(direction='STOP', pwm=0.0, time=monotonic(), frame=-1)


def snapshot():
    with lock:
        result = dict(status, type='status', camera_error=camera['error'], build='V3-FIX-20261021')
        now = monotonic()
        last_motor_write = status.get('motor_tick', 0)
        result['motor_age'] = max(0.0, now-last_motor_write) if last_motor_write else None
        if result['motor_age'] is None or result['motor_age'] > 1.5:
            result.update(reason='MOTOR ERROR', motor_error=result.get('motor_error') or
                          '모터 I2C 출력 확인 없음 (전원·I2C 0x34·서비스 로그 확인)')
        for name, values in sensor_state.items():
            reading = dict(values)
            last = reading.pop('last_success')
            reading['age_seconds'] = None if last is None else max(0.0, now-last)
            result[name] = reading
        return result


def dht_process_main(pipe):
    """DHT 타이밍 측정을 서버 및 모터의 Python 실행과 분리합니다."""
    device = None
    try:
        while True:
            result = dict(connected=False, error=None)
            try:
                if device is None:
                    import board
                    import adafruit_dht
                    device = adafruit_dht.DHT22(board.D16, use_pulseio=False)
                temperature, humidity = device.temperature, device.humidity
                if (temperature is None or humidity is None or not math.isfinite(temperature)
                        or not math.isfinite(humidity) or not -40 <= temperature <= 80
                        or not 0 <= humidity <= 100):
                    raise RuntimeError('유효하지 않은 DHT22 측정값')
                result.update(connected=True, temperature_c=round(temperature, 1),
                              humidity_pct=round(humidity, 1), last_success=monotonic())
            except Exception as error:
                result['error'] = str(error)
            pipe.send(result)
            sleep(DHT_INTERVAL)
    except (BrokenPipeError, EOFError, KeyboardInterrupt):
        pass
    finally:
        if device is not None:
            with suppress(Exception):
                device.exit()
        pipe.close()


def dht_worker(pipe):
    previous_error = None
    last_reply = monotonic()
    while not quit_event.is_set():
        try:
            if pipe.poll(0.2):
                result = pipe.recv()
                last_reply = monotonic()
                with lock:
                    sensor_state['dht22'].update(result)
                error = result.get('error')
                if error and error != previous_error:
                    print('DHT22: ' + error, flush=True)
                previous_error = error
            elif monotonic()-last_reply > 10:
                with lock:
                    sensor_state['dht22'].update(connected=False, error='DHT22 응답 시간 초과')
        except (EOFError, OSError):
            with lock:
                sensor_state['dht22'].update(connected=False, error='DHT22 측정 프로세스 종료')
            return


def ads_read_a0(bus):
    # ADS1115 config: OS=1, MUX=A0-GND, PGA=±4.096V,
    # single-shot, 128SPS, comparator disabled. MSB first (0xC383).
    bus.write_i2c_block_data(ADS_ADDRESS, 0x01, [0xC3, 0x83])
    deadline = monotonic() + 0.3
    while not quit_event.wait(0.01):
        config = bus.read_i2c_block_data(ADS_ADDRESS, 0x01, 2)
        if config[0] & 0x80:
            raw = bus.read_i2c_block_data(ADS_ADDRESS, 0x00, 2)
            count = (raw[0] << 8) | raw[1]
            if count & 0x8000:
                count -= 65536
            if count < -8:
                raise RuntimeError('ADS1115 A0 음전압: 배선 확인')
            return max(0, count) * 4.096 / 32768
        if monotonic() >= deadline:
            raise TimeoutError('ADS1115 변환 시간 초과')
    return None


def gas_worker():
    bus = None
    warmup_start = monotonic()
    previous_error = None
    try:
        while not quit_event.is_set():
            try:
                if bus is None:
                    bus = SMBus(1)
                adc_voltage = ads_read_a0(bus)
                if adc_voltage is None:
                    break
                voltage = adc_voltage * MQ135_DIVIDER_RATIO
                if adc_voltage > 2.75:
                    raise RuntimeError('A0 전압 범위 초과: 분압 및 전원 확인')
                warming = monotonic()-warmup_start < MQ135_WARMUP_SECONDS
                detected = None
                if not warming and MQ135_ALARM_VOLTAGE is not None:
                    if not 0 < MQ135_ALARM_VOLTAGE <= 5.5:
                        raise ValueError('MQ135_ALARM_VOLTAGE 설정 오류')
                    detected = voltage >= MQ135_ALARM_VOLTAGE
                with lock:
                    sensor_state['mq135'].update(connected=True, warming_up=warming,
                        voltage_v=round(voltage, 4), adc_voltage_v=round(adc_voltage, 4),
                        detected=detected, calibrated=False, ppm=None,
                        last_success=monotonic(), error=None)
                previous_error = None
            except Exception as error:
                message = str(error)
                with lock:
                    sensor_state['mq135'].update(connected=False, error=message,
                        detected=None, calibrated=False, ppm=None)
                if message != previous_error:
                    print(f'MQ-135 / ADS1115: {message}', flush=True)
                    previous_error = message
                if bus is not None:
                    with suppress(Exception):
                        bus.close()
                    bus = None
            quit_event.wait(0.5 if bus is not None else 2.0)
    finally:
        if bus is not None:
            with suppress(Exception):
                bus.close()


def motor_write(bus, direction, powers):
    global motor_tick
    ratios = MOVES[direction]
    values = [round(powers[i]) * DIRECTION[i] *
              (1 if ratios[i] > 0 else -1 if ratios[i] < 0 else 0)
              for i in range(4)]
    bus.write_i2c_block_data(I2C_ADDRESS, 31,
                            [max(-MAX_PWM, min(MAX_PWM, v)) & 255 for v in values])
    motor_tick = monotonic()


def read_counts(bus):
    return struct.unpack('<4i', bytes(bus.read_i2c_block_data(I2C_ADDRESS, 60, 16)))


def delta(current, previous):
    return ((current - previous + 0x80000000) & 0xffffffff) - 0x80000000


def feedback_targets(direction, base, rates):
    ratios = [abs(v) for v in MOVES[direction]]
    active = [i for i, ratio in enumerate(ratios) if ratio > 0]
    if not active:
        return [0.0]*4
    # 곡선 이동에서는 지정한 속도 비율에 대한 차이를 보정
    reference = min(rates[i] / ratios[i] for i in active)
    targets = [0.0]*4
    for i in active:
        limit = base * ratios[i]
        excess = max(0.0, rates[i] / ratios[i] / reference - 1) if reference >= 1 else 0
        targets[i] = max(limit * MIN_PWM_RATIO,
                         limit - FEEDBACK_GAIN * ratios[i] * excess)
    return targets


def motor_worker():
    """유일한 모터 I2C 출력 소유자(독립 프로세스).

    이동 전·중에 명령/영상/전방거리를 검증합니다. 일시적인 영상·통신 실패는
    조건이 회복되면 새 명령으로 동작할 수 있고, 엔코더 스톨은 명시적 STOP 후 재허용합니다.
    I2C 실패 시 장치에 0 출력 시도 후 재연결하며 실제 정지 성공을 가정하지 않습니다.
    """
    bus = None
    actual = 'STOP'
    powers = [0.0] * 4
    rates = [0.0] * 4
    previous = None
    last_sample = monotonic()
    motion = [last_sample] * 4
    reversal_until = 0.0
    distance_id = None
    distance_start = None
    distance_done = False
    wheel_done = [False] * 4
    target_counts = 0.0
    encoder_fault = ''
    last_reason = None

    try:
        while not quit_event.is_set():
            now = monotonic()
            if bus is None:
                try:
                    bus = SMBus(1)
                    motor_write(bus, 'STOP', [0] * 4)
                    bus.write_i2c_block_data(I2C_ADDRESS, 20, [0])
                    sleep(0.1)
                    previous = read_counts(bus)
                    last_sample = monotonic()
                    actual = 'STOP'
                    powers = [0.0] * 4
                    rates = [0.0] * 4
                    motion = [last_sample] * 4
                    encoder_fault = ''
                    with lock:
                        status['motor_error'] = ''
                    print('모터 초기화 성공: I2C=0x34 / M1~M4', flush=True)
                except Exception as error:
                    print(f'모터 I2C 초기화 실패: {error}', flush=True)
                    with lock:
                        status.update(direction='STOP', pwm=0, wheel_pwm=[0]*4,
                                      reason='MOTOR ERROR', motor_error=f'I2C 0x34: {error}')
                    if bus is not None:
                        with suppress(Exception):
                            bus.close()
                    bus = None
                    quit_event.wait(1.0)
                    continue

            try:
                now = monotonic()
                dt = max(0.001, now-last_sample)
                last_sample = now
                with lock:
                    req = dict(command)
                    cam_time, cam_error = camera['time'], camera['error']
                    ack_time = frame_times.get(req['frame'], 0.0)
                    front_distance = status['distance_cm']
                    distance_time = status['distance_time']
                    connected = owner_token is not None

                counts = read_counts(bus)
                pulses = ([abs(delta(counts[i], previous[i])) for i in range(4)]
                          if previous is not None else [0]*4)
                previous = counts
                # 엔코더 센서값은 항상 측정하여 정지 상태에서 쌓인 펄스가 섞이지 않게 합니다.
                rates = [0.5*rates[i] + 0.5*pulses[i]/dt for i in range(4)]

                requested = req['direction']
                base = max(0.0, min(MAX_PWM, float(req['pwm'])))
                desired = requested
                reason = 'RUNNING'
                ultra_requested = req.get('mode') == 'ultra' and requested != 'STOP'

                # 일시적인 fault를 변수에 저장하지 않고 매 주기 다시 판정합니다.
                # 중대한 엔코더 스톨만 명시적인 정지 명령 후 해제합니다.
                if requested == 'STOP' or base <= 0:
                    desired = 'STOP'
                    reason = 'STOP' if connected else 'WAITING'
                    encoder_fault = ''
                    distance_id = None
                    distance_start = None
                    distance_done = False
                    wheel_done = [False]*4
                elif not connected:
                    desired, reason = 'STOP', 'WAITING'
                elif now-req['time'] > COMMAND_TIMEOUT:
                    desired, reason = 'STOP', 'COMMAND TIMEOUT'
                elif cam_error or now-cam_time > VIDEO_TIMEOUT or now-ack_time > VIDEO_TIMEOUT:
                    desired, reason = 'STOP', 'VIDEO NOT READY'
                elif (requested in FORWARD_MOVES or ultra_requested) and (
                        front_distance is None or now-distance_time > 1.5):
                    desired, reason = 'STOP', 'DISTANCE NOT READY'
                elif (requested in FORWARD_MOVES or ultra_requested) and front_distance <= STOP_DISTANCE_CM:
                    desired, reason = 'STOP', 'FRONT OBSTACLE'
                elif encoder_fault:
                    desired, reason = 'STOP', encoder_fault

                precision = ultra_requested and desired != 'STOP'
                if precision:
                    base = min(base, DISTANCE_PWM_LIMIT)
                    if req.get('move_id') != distance_id:
                        # 거리제어 새 클릭은 이전 기준점을 버리고 다시 시작합니다.
                        distance_id = req.get('move_id')
                        distance_start = counts[:]
                        distance_done = False
                        wheel_done = [False]*4
                        factor = math.sqrt(2.0) if '_' in desired else 1.0
                        target_counts = (req['distance_cm'] * COUNTS_PER_REV /
                                         (math.pi * WHEEL_DIAMETER_CM) *
                                         DISTANCE_SCALE * factor *
                                         (DIAGONAL_SCALE if '_' in desired else 1.0))
                    if distance_done:
                        desired, reason = 'STOP', 'DISTANCE COMPLETE'

                active_for_motion = [i for i, value in enumerate(MOVES[desired]) if value != 0]
                if precision and distance_start is not None and desired != 'STOP':
                    wheel_done = [
                        (wheel_done[i] or abs(delta(counts[i], distance_start[i])) >= target_counts)
                        if i in active_for_motion else False
                        for i in range(4)
                    ]
                    if all(wheel_done[i] for i in active_for_motion):
                        desired, reason = 'STOP', 'DISTANCE COMPLETE'
                        distance_done = True

                # 방향을 바꾸는 순간 한번 0을 써서 물리적으로 반전 지연을 둡니다.
                if desired != actual:
                    motor_write(bus, 'STOP', [0]*4)
                    if actual != 'STOP':
                        reversal_until = monotonic() + 0.18
                    actual = 'STOP'
                    powers = [0.0]*4
                    motion = [now]*4
                    rates = [0.0]*4

                if desired != 'STOP' and monotonic() >= reversal_until:
                    if actual == 'STOP':
                        actual = desired
                        # 이전의 20% 출력은 토크가 부족할 수 있으므로 빠르게 설정 PWM으로 상승
                        start_pwm = min(base, DISTANCE_PWM_MIN if precision else 35.0)
                        powers = [start_pwm*abs(v) for v in MOVES[actual]]
                        motion = [now]*4
                    ratios = [abs(v) for v in MOVES[actual]]
                    limits = [base*r for r in ratios]
                    wheel_active = [i for i in range(4) if ratios[i] > 0]
                    if precision and distance_start is not None:
                        counts_per_cm = target_counts/max(1, req['distance_cm'])
                        for i in wheel_active:
                            if wheel_done[i]:
                                limits[i] = 0.0
                                continue
                            remaining = max(0.0, target_counts-abs(delta(counts[i], distance_start[i])))
                            fraction = min(1.0, remaining/max(1.0, counts_per_cm*DISTANCE_SLOWDOWN_CM))
                            low_pwm = min(base, DISTANCE_PWM_MIN)
                            limits[i] = min(limits[i], low_pwm+(base-low_pwm)*fraction)
                    targets = feedback_targets(actual, base, rates)
                    step = (DISTANCE_ACCEL_PER_SEC if precision else PWM_CHANGE_RATE)*min(dt, 0.3)
                    powers = [max(0.0, min(limits[i], powers[i] +
                              max(-step, min(step, min(targets[i], limits[i])-powers[i]))))
                              for i in range(4)]
                    for i in wheel_active:
                        if limits[i] > 0 and pulses[i]:
                            motion[i] = now
                    stalled = [i+1 for i in wheel_active
                               if limits[i] > 0 and now-motion[i] > ENCODER_TIMEOUT]
                    if stalled:
                        encoder_fault = 'ENCODER FAULT: ' + ','.join(f'M{i}' for i in stalled)
                        actual, powers, reason = 'STOP', [0.0]*4, encoder_fault
                else:
                    actual, powers = 'STOP', [0.0]*4

                # 전송 직전에 다시 확인: 클릭 해제/연결 문제를 오래된 명령보다 우선합니다.
                with lock:
                    latest = dict(command)
                if (latest['direction'] == 'STOP' or latest['pwm'] <= 0 or
                        monotonic()-latest['time'] > COMMAND_TIMEOUT):
                    actual, powers = 'STOP', [0.0]*4
                    if latest['direction'] == 'STOP' or latest['pwm'] <= 0:
                        reason = 'STOP' if connected else 'WAITING'
                    else:
                        reason = 'COMMAND TIMEOUT'
                motor_write(bus, actual, powers)
                with lock:
                    if (latest['direction'] == 'STOP' or latest['pwm'] <= 0) and actual == 'STOP':
                        status['stop_ack_id'] = latest.get('stop_id', 0)
                    status.update(direction=actual, pwm=round(sum(powers)/4),
                                  wheel_pwm=[round(p) for p in powers],
                                  rpm=[round(r*60/COUNTS_PER_REV, 1) for r in rates],
                                  reason=reason, motor_error='')
                if reason != last_reason:
                    print(f'모터 상태: {reason}', flush=True)
                    last_reason = reason
            except Exception as error:
                print(f'모터 I2C 제어 오류: {error}', flush=True)
                with lock:
                    status.update(direction='STOP', pwm=0, wheel_pwm=[0]*4,
                                  reason='MOTOR ERROR', motor_error=str(error))
                if bus is not None:
                    # 물리 PWM0 전송을 시도하되 실패했다면 정지했다고 가정하지 않습니다.
                    with suppress(Exception):
                        motor_write(bus, 'STOP', [0]*4)
                    with suppress(Exception):
                        bus.close()
                bus = None
                actual, powers = 'STOP', [0.0]*4
                previous = None
                quit_event.wait(1.0)
                continue
            quit_event.wait(DISTANCE_LOOP_SECONDS if precision else 0.04)
    finally:
        if bus is not None:
            try:
                motor_write(bus, 'STOP', [0]*4)
            except Exception as error:
                print(f'모터 종료 정지 실패: {error} (모터 전원 확인)', flush=True)
            finally:
                with suppress(Exception):
                    bus.close()
        with lock:
            status.update(direction='STOP', pwm=0, wheel_pwm=[0]*4)


class MotorChannel:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
    def send(self, value):
        self.writer.send(value)
    def recv(self):
        return self.reader.recv()
    def poll(self, timeout=0):
        return self.reader.poll(timeout)
    def close(self):
        self.reader.close()
        self.writer.close()


def motor_process_main(pipe):
    """단독 모터 소유자. 부모의 명령 시각을 보존하므로 부모 정지 시에도 만료됩니다."""
    def exchange():
        global owner_token
        try:
            while not quit_event.is_set():
                packet = None
                if pipe.poll(0.01):
                    packet = pipe.recv()
                    # IPC 버퍼에 쌓인 과거 명령은 폐기하고 가장 최신 제어값만 반영.
                    # STOP 명령이 낡은 FORWARD 명령 뒤에 묻혀 지연되지 않게 합니다.
                    for _ in range(64):
                        if not pipe.poll(0):
                            break
                        packet = pipe.recv()
                if packet is not None:
                    if packet.get('shutdown'):
                        stop_request()
                        quit_event.set()
                        break
                    with lock:
                        command.clear()
                        command.update(packet['command'])
                        camera.update(packet['camera'])
                        status.update(packet['front'])
                        frame_times.clear()
                        frame_times[command['frame']] = packet['frame_time']
                        owner_token = packet['owner']
                with lock:
                    output = {k: status.get(k) for k in
                              ('direction', 'pwm', 'wheel_pwm', 'rpm', 'reason', 'motor_error', 'stop_ack_id')}
                    output['stop_ack_id'] = status.get('stop_ack_id', 0)
                    output['motor_tick'] = motor_tick
                pipe.send(output)
                quit_event.wait(0.015)
        except (EOFError, BrokenPipeError, OSError):
            stop_request()
            quit_event.set()
    threading.Thread(target=exchange, daemon=True).start()
    try:
        motor_worker()
    finally:
        quit_event.set()
        pipe.close()


def motor_bridge(pipe):
    """부모/모터 자식 IPC.

    상태 패킷을 무한히 비우면 자식이 계속 보내는 동안 새 이동 명령 전송이
    굶을 수 있습니다. 매 주기 먼저 최신 명령을 보내고 상태는 최대 4개만 읽습니다.
    """
    try:
        while not quit_event.is_set():
            with lock:
                req = dict(command)
                packet = dict(command=req, owner=owner_token,
                    camera={k: camera[k] for k in ('time', 'error')},
                    front={k: status[k] for k in ('distance_cm', 'distance_time')},
                    frame_time=frame_times.get(req['frame'], 0))
            pipe.send(packet)
            latest = None
            for index in range(4):
                if not pipe.poll(0.005 if index == 0 else 0):
                    break
                latest = pipe.recv()
            if latest is not None:
                with lock:
                    status.update(latest)
            quit_event.wait(0.02)
    except (EOFError, BrokenPipeError, OSError) as error:
        stop_request()
        with lock:
            status.update(reason='MOTOR ERROR',
                          motor_error='모터 프로세스 통신 중단: ' + str(error))
        print('모터 프로세스 통신 중단 / 정지 여부를 확인하세요.', flush=True)


async def loop_heartbeat():
    global event_loop_tick
    while not quit_event.is_set():
        event_loop_tick = monotonic()
        await asyncio.sleep(0.1)


def diagnostic_worker():
    logger = logging.getLogger('car_diagnostics')
    logger.setLevel(logging.INFO)
    try:
        folder = Path(__file__).resolve().parent / 'logs'
        folder.mkdir(exist_ok=True)
        handler = RotatingFileHandler(folder / 'server_diagnostics.log',
                                      maxBytes=1000000, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
    except OSError as error:
        print('진단 파일 생성 실패: ' + str(error), flush=True)
    last_reason = None
    while not quit_event.wait(2):
        now = monotonic()
        with lock:
            reason = status.get('reason')
            command_age = now-command['time']
            motor_age = now-status.get('motor_tick', 0)
            camera_age = now-camera['time']
        logger.info('reason=%s command_age=%.2f motor_age=%.2f camera_age=%.2f loop_age=%.2f',
                    reason, command_age, motor_age, camera_age, now-event_loop_tick)
        if reason != last_reason:
            print('모터 상태: ' + str(reason), flush=True)
            last_reason = reason


def camera_worker():
    capture = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    try:
        if not capture.isOpened():
            raise RuntimeError('USB 웹캠을 열 수 없습니다.')
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        capture.set(cv2.CAP_PROP_FPS, 10)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        next_frame = 0.0
        while not quit_event.is_set():
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError('USB 웹캠 읽기 실패')
            now = monotonic()
            if now < next_frame:
                continue
            next_frame = now + 0.1
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
            if ok:
                with lock:
                    camera.update(seq=camera['seq']+1, jpeg=encoded.tobytes(), time=now)
                    frame_times[camera['seq']] = now
                    while len(frame_times) > 40:
                        frame_times.popitem(last=False)
    except Exception as error:
        with lock:
            camera['error'] = str(error)
        print(f'카메라 오류: {error}', flush=True)
    finally:
        capture.release()


def publish_lcd():
    s = snapshot()
    data = dict(boot_id=BOOT_ID, updated=monotonic(), direction=s['direction'],
                pwm=s['pwm'], distance_cm=s['distance_cm'], port=PORT,
                pc_connected=bool(owner_token and control_socket is not None and not control_socket.closed),
                control_fresh=bool(owner_token and monotonic()-command['time'] < 1),
                reason=s.get('reason', ''), motor_error=s.get('motor_error', ''),
                camera_error=s.get('camera_error', ''))
    temporary = STATE_FILE.with_suffix('.v3.tmp')
    temporary.write_text(json.dumps(data, allow_nan=False), encoding='utf-8')
    os.replace(temporary, STATE_FILE)


class TimedDistanceSensor(DistanceSensor):
    def __init__(self, *args, **kwargs):
        self.latest_echo = (None, 0.0)
        super().__init__(*args, **kwargs)

    def _read(self):
        value = super()._read()
        self.latest_echo = (value, monotonic())
        return value


def monitor_worker():
    factory = sensor = None
    try:
        try:
            factory = LGPIOFactory()
            sensor = TimedDistanceSensor(echo=24, trigger=23, max_distance=4,
                                    queue_len=1, partial=True, pin_factory=factory)
        except Exception as error:
            print(f'초음파 초기화 오류: {error}', flush=True)
        last_error = ''
        while not quit_event.is_set():
            distance, sample_time = None, 0.0
            if sensor:
                value, sample_time = sensor.latest_echo
                if (value is not None and math.isfinite(value)
                        and 0 <= value <= 1 and monotonic()-sample_time < 1):
                    distance = round(value * 400, 1)
            with lock:
                status['distance_cm'] = distance
                status['distance_time'] = sample_time
            quit_event.wait(0.1)
    finally:
        if sensor:
            sensor.close()
        if factory:
            factory.close()


def lcd_publish_worker():
    while not quit_event.is_set():
        try:
            publish_lcd()
        except Exception as error:
            print(f'LCD 상태 전송 오류: {error}', flush=True)
        quit_event.wait(0.25)


async def send_status(ws):
    try:
        while not ws.closed and not quit_event.is_set():
            lease = uuid.uuid4().hex
            with lock:
                control_leases[lease] = monotonic()
                while len(control_leases) > 40:
                    control_leases.popitem(last=False)
            result = snapshot()
            result['lease'] = lease
            await asyncio.wait_for(ws.send_json(result), timeout=1)
            await asyncio.sleep(0.05)
    except (ConnectionError, asyncio.TimeoutError, RuntimeError) as error:
        stop_request()
        print('상태 송신 중단: ' + str(error), flush=True)
        with suppress(Exception):
            await asyncio.wait_for(ws.close(), timeout=1.5)


async def control_handler(request):
    global owner_token, control_socket
    if owner_token:
        raise web.HTTPConflict(text='다른 클라이언트가 연결되어 있습니다.')
    token = uuid.uuid4().hex
    owner_token = token
    control_leases.clear()
    ws = web.WebSocketResponse(heartbeat=5, max_msg_size=4096, timeout=1)
    status_sender = None
    try:
        await ws.prepare(request)
        control_socket = ws
        await ws.send_json(dict(type='hello', token=token, version=3, distance_control=True, stop_ack=True, max_pwm=MAX_PWM, isolated_motor=True, build='V3-FIX-20261021'))
        status_sender = asyncio.create_task(send_status(ws))
        print(f'제어 연결: {request.remote}', flush=True)
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                break
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
                direction, pwm, frame = data['direction'], float(data['pwm']), int(data.get('frame', -1))
                stop_id = data.get('stop_id', 0)
                if type(stop_id) is not int or stop_id < 0:
                    raise ValueError('잘못된 정지 번호')
                mode = data.get('mode', 'precision')
                distance_cm = data.get('distance_cm', 1)
                move_id = data.get('move_id', '')
                if mode not in ('auto', 'precision', 'ultra'):
                    raise ValueError('잘못된 모드')
                if mode == 'ultra' and direction != 'STOP':
                    if (direction not in DISTANCE_MOVES or type(distance_cm) is not int
                            or not 1 <= distance_cm <= 50 or not isinstance(move_id, str)
                            or not 1 <= len(move_id) <= 80):
                        raise ValueError('잘못된 거리 명령')
                if direction not in MOVES or not math.isfinite(pwm) or not 0 <= pwm <= 100:
                    raise ValueError('잘못된 명령')
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                stop_request()
                await ws.close(code=1008, message=b'Invalid command')
                break
            # 지연되어 쌓인 이동 명령은 새 명령처럼 실행하지 않습니다.
            if direction != 'STOP' and pwm > 0:
                lease = data.get('lease')
                with lock:
                    issued = control_leases.get(lease, 0) if isinstance(lease, str) else 0
                if not issued or monotonic()-issued > COMMAND_TIMEOUT:
                    stop_request()
                    continue
            with lock:
                command.update(direction=direction, pwm=min(MAX_PWM, pwm), time=monotonic(), frame=frame,
                               mode=mode, distance_cm=distance_cm, move_id=token + ':' + move_id, stop_id=stop_id)
    finally:
        stop_request()
        if status_sender:
            status_sender.cancel()
            await asyncio.gather(status_sender, return_exceptions=True)
        await asyncio.gather(*[v.close() for v in list(video_sockets)], return_exceptions=True)
        owner_token, control_socket = None, None
        print('제어 연결 종료', flush=True)
    return ws


async def receive_video_messages(ws):
    async for message in ws:
        if message.type == WSMsgType.ERROR:
            break


async def video_handler(request):
    token = request.query.get('token')
    if not token or token != owner_token:
        raise web.HTTPForbidden()
    if video_sockets:
        raise web.HTTPConflict()
    ws = web.WebSocketResponse(heartbeat=5, compress=False, max_msg_size=4096, timeout=1)
    # prepare의 await 중 두 번째 연결이 들어오는 것을 방지
    video_sockets.add(ws)
    receiver = None
    try:
        await ws.prepare(request)
        receiver = asyncio.create_task(receive_video_messages(ws))
        last_seq = -1
        while not ws.closed and not receiver.done() and token == owner_token and not quit_event.is_set():
            with lock:
                seq, jpeg = camera['seq'], camera['jpeg']
            if jpeg and seq != last_seq:
                await asyncio.wait_for(ws.send_bytes(struct.pack('!Q', seq)+jpeg), 1)
                last_seq = seq
            await asyncio.sleep(0.03)
    except (ConnectionError, asyncio.TimeoutError, RuntimeError) as error:
        print(f'영상 연결 종료: {error}', flush=True)
    finally:
        stop_request()
        if receiver:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        video_sockets.discard(ws)
        if ws.prepared:
            await ws.close()
    return ws


async def startup(app):
    # spawn: GPIO/I2C 핸들을 fork로 공유하지 않습니다.
    context = mp.get_context('spawn')
    command_reader, command_writer = context.Pipe(duplex=False)
    status_reader, status_writer = context.Pipe(duplex=False)
    motor_parent = MotorChannel(status_reader, command_writer)
    motor_child = MotorChannel(command_reader, status_writer)
    dht_parent, dht_child = context.Pipe(duplex=False)
    motor_process = context.Process(target=motor_process_main, args=(motor_child,), daemon=True)
    dht_process = context.Process(target=dht_process_main, args=(dht_child,), daemon=True)
    motor_process.start()
    dht_process.start()
    motor_child.close()
    dht_child.close()
    app['processes'] = [motor_process, dht_process]
    app['pipes'] = [motor_parent, dht_parent]
    app['threads'] = [threading.Thread(target=f, args=args, daemon=True) for f, args in
        ((motor_bridge, (motor_parent,)), (camera_worker, ()), (monitor_worker, ()),
         (dht_worker, (dht_parent,)), (gas_worker, ()), (lcd_publish_worker, ()),
         (diagnostic_worker, ()))]
    for thread in app['threads']:
        thread.start()
    app['heartbeat'] = asyncio.create_task(loop_heartbeat())
    deadline = monotonic() + 8
    while monotonic() < deadline:
        with lock:
            ready = status.get('motor_tick', 0) > 0 and 'stop_ack_id' in status
        if ready or not motor_process.is_alive():
            break
        await asyncio.sleep(0.05)
    print('V3 FIX-20261021 서버 시작 / PWM 0~100 / 모터 독립 프로세스', flush=True)


async def shutdown(app):
    stop_request()
    quit_event.set()
    sockets = list(video_sockets)
    if control_socket is not None:
        sockets.append(control_socket)
    await asyncio.gather(*[s.close(code=1001) for s in sockets], return_exceptions=True)


async def cleanup(app):
    for thread in app.get('threads', []):
        await asyncio.to_thread(thread.join, 0.5)
    # 부모 통신 종료를 감지한 모터 자식은 정지 후 종료합니다.
    for pipe in app.get('pipes', []):
        with suppress(OSError):
            pipe.close()
    for process in app.get('processes', []):
        await asyncio.to_thread(process.join, 1)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 1)
    task = app.get('heartbeat')
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # 상태를 가짜 STOP으로 덮어쓰지 않습니다. LCD는 갱신 중단을 감지합니다.


if __name__ == '__main__':
    # Linux 추상 소켓은 파일 없이 중복 실행을 차단하며 종료 시 자동 해제됩니다.
    # 잠금은 하드웨어 스레드를 시작하기 전에 확보합니다.
    instance_guard = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        instance_guard.bind('\0smart_ict_car_ws_server_v3')
    except OSError:
        print('서버가 이미 실행 중입니다. 클라이언트만 실행하세요.', flush=True)
        raise SystemExit(1)
    app = web.Application()
    app.router.add_get('/control', control_handler)
    app.router.add_get('/video', video_handler)
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    web.run_app(app, host='0.0.0.0', port=PORT, shutdown_timeout=2)
