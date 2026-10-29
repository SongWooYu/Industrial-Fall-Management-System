# 파일명: motor_forward_feedback.py
# 1: 피드백 전진 / 0: 정지 / Ctrl+C: 종료

import sys
import tty
import select
import struct
import termios
from time import monotonic, sleep
from smbus2 import SMBus

ADDRESS = 0x34
TYPE_REG = 20
PWM_REG = 31
ENCODER_REG = 60

DIRECTION = [-1, 1, 1, -1]     # 확인된 전진 방향
#BASE_PWM = 100.0               # 기본 출력 100%
#MIN_PWM = 70.0                 # 보정 시 최소 출력

#BASE_PWM = 60.0               # 기본 출력 60%
#MIN_PWM = 40.0                 # 보정 시 최소 출력

BASE_PWM = 40.0               # 기본 출력 40%
MIN_PWM = 25.0                 # 보정 시 최소 출력


COUNTS_PER_TURN = 2600         # RPM 표시용
CONTROL_INTERVAL = 0.1
PRINT_INTERVAL = 0.5
STARTUP_TIME = 1.0             # 처음 1초는 PWM 100%
NO_PULSE_TIMEOUT = 2.0

KP = 35.0                     # 속도 차이 보정 계수
ADJUST_RATE = 20.0             # 초당 PWM 변경 한도


def set_motors(bus, powers):
    data = [
        (round(power) * direction) & 0xFF
        for power, direction in zip(powers, DIRECTION)
    ]
    bus.write_i2c_block_data(ADDRESS, PWM_REG, data)


def stop_all(bus):
    bus.write_i2c_block_data(
        ADDRESS, PWM_REG, [0, 0, 0, 0]
    )


def read_counts(bus):
    data = bus.read_i2c_block_data(
        ADDRESS, ENCODER_REG, 16
    )
    return struct.unpack("<4i", bytes(data))


def difference(current, previous):
    return (current - previous + 2**31) % 2**32 - 2**31


def main():
    terminal = sys.stdin.fileno()
    original_settings = termios.tcgetattr(terminal)

    with SMBus(1) as bus:
        try:
            stop_all(bus)

            # 빠르게 동작한 motor_test.py와 동일한 설정
            bus.write_i2c_block_data(
                ADDRESS, TYPE_REG, [0]
            )
            sleep(0.1)
            stop_all(bus)

            running = False
            powers = [0.0] * 4
            rates = [0.0] * 4
            previous = [0] * 4
            last_motion = [0.0] * 4

            started = 0.0
            last_control = monotonic()
            last_print = last_control

            print("PWM 100% 기반 엔코더 피드백 전진")
            print("1: 전진 / 0: 정지 / Ctrl+C: 종료")
            print("Enter 없이 숫자 키만 누르세요.")
            print("표시: 모터별 환산 RPM / PWM %")

            tty.setcbreak(terminal)

            while True:
                # 주행 중에도 정지 키 입력 처리
                readable, _, _ = select.select(
                    [sys.stdin], [], [], 0.02
                )

                if readable:
                    key = sys.stdin.read(1)

                    if not key:
                        break

                    if key == "0":
                        stop_all(bus)
                        running = False
                        powers = [0.0] * 4
                        print("\n모든 모터 정지")

                    elif key == "1" and not running:
                        previous = read_counts(bus)
                        started = monotonic()
                        last_control = started
                        last_print = started
                        last_motion = [started] * 4

                        rates = [0.0] * 4
                        powers = [BASE_PWM] * 4
                        set_motors(bus, powers)
                        running = True

                        print("\n전진 시작 — PWM 100%")

                if not running:
                    continue

                now = monotonic()

                if now - last_control < CONTROL_INTERVAL:
                    continue

                current = read_counts(bus)
                now = monotonic()
                dt = now - last_control

                if dt > 1.0:
                    raise RuntimeError("제어 주기 지연")

                for i in range(4):
                    delta = abs(
                        difference(current[i], previous[i])
                    )

                    # 초당 엔코더 카운트
                    measured = delta / dt
                    rates[i] = 0.5 * measured + 0.5 * rates[i]

                    if delta > 0:
                        last_motion[i] = now

                    if now - last_motion[i] > NO_PULSE_TIMEOUT:
                        raise RuntimeError(
                            f"M{i + 1}: 엔코더 응답 없음. "
                            "[0] 설정의 카운트 지원, 배선, "
                            "모터 걸림을 확인해 주세요."
                        )

                previous = current
                last_control = now

                # 출발 후에는 가장 느린 바퀴를 기준으로 보정
                if now - started >= STARTUP_TIME:
                    reference = min(rates)

                    if reference >= 1.0:
                        for i in range(4):
                            excess = max(
                                0.0,
                                (rates[i] - reference) / reference
                            )

                            desired = max(
                                MIN_PWM,
                                BASE_PWM - KP * excess
                            )

                            # 출력이 급격히 변하지 않도록 제한
                            change = ADJUST_RATE * dt
                            powers[i] += max(
                                -change,
                                min(change, desired - powers[i])
                            )

                        set_motors(bus, powers)

                if now - last_print >= PRINT_INTERVAL:
                    text = " | ".join(
                        f"M{i + 1}: "
                        f"{rates[i] * 60 / COUNTS_PER_TURN:.1f}RPM"
                        f"/{powers[i]:.1f}%"
                        for i in range(4)
                    )
                    print(text, flush=True)
                    last_print = now

        except (KeyboardInterrupt, EOFError):
            print("\n프로그램을 종료합니다.")

        except (OSError, RuntimeError) as error:
            print(f"\n오류: {error}")

        finally:
            try:
                stop_all(bus)
            except OSError:
                print("정지 명령 전송 실패: 모터 전원을 꺼 주세요.")
            finally:
                termios.tcsetattr(
                    terminal,
                    termios.TCSADRAIN,
                    original_settings
                )


if __name__ == "__main__":
    main()
    
    
    
    