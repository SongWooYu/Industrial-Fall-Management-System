# 파일명: motor_turns.py
# 입력한 바퀴 수만큼 전진 후 정지 / Ctrl+C 종료

import math
import struct
from time import sleep, monotonic
from smbus2 import SMBus

ADDRESS = 0x34
TYPE_REG = 20
PWM_REG = 31
ENCODER_REG = 60

POWER = 60                     # PWM 출력 60% (60RPM이 아님)
DIRECTION = [-1, 1, 1, -1]     # M1~M4 전진 방향

# 13PPR × 4배 계수 × 감속비 50
COUNTS_PER_TURN = 2600


def set_motors(bus, values):
    data = [int(value) & 0xFF for value in values]
    bus.write_i2c_block_data(ADDRESS, PWM_REG, data)


def stop_all(bus):
    set_motors(bus, [0, 0, 0, 0])


def read_counts(bus):
    # M1~M4: 각 32비트 부호 있는 누적 카운트
    data = bus.read_i2c_block_data(ADDRESS, ENCODER_REG, 16)
    return struct.unpack("<4i", bytes(data))


def difference(current, previous):
    # 32비트 카운터 넘침 처리
    return (current - previous + 2**31) % 2**32 - 2**31


def rotate(bus, turns):
    start = read_counts(bus)
    previous = start
    target = round(turns * COUNTS_PER_TURN)
    finished = [False] * 4

    began = monotonic()
    last_motion = [began] * 4
    last_print = began

    print(f"\n목표: 각 바퀴 {turns:g}회전 / PWM {POWER}%")

    try:
        set_motors(bus, [POWER * d for d in DIRECTION])

        while not all(finished):
            sleep(0.02)
            now = monotonic()
            current = read_counts(bus)
            counts = [
                abs(difference(current[i], start[i]))
                for i in range(4)
            ]
            output = [0] * 4

            for i in range(4):
                if finished[i]:
                    continue

                if counts[i] >= target:
                    finished[i] = True
                    continue

                if current[i] != previous[i]:
                    last_motion[i] = now

                if now - last_motion[i] > 2.0:
                    raise RuntimeError(
                        f"M{i + 1}: 엔코더 응답 없음 또는 모터 걸림"
                    )

                output[i] = POWER * DIRECTION[i]

            set_motors(bus, output)
            previous = current

            if now - last_print >= 0.5:
                print(" | ".join(
                    f"M{i + 1}: {counts[i] / COUNTS_PER_TURN:.2f}바퀴"
                    for i in range(4)
                ))
                last_print = now

            if now - began > max(15.0, turns * 10.0):
                raise RuntimeError("회전 시간 초과")

    finally:
        stop_all(bus)

    # 관성으로 더 회전한 양까지 포함하여 표시
    sleep(0.5)
    final = read_counts(bus)

    print("모든 모터 정지")
    for i in range(4):
        count = abs(difference(final[i], start[i]))
        print(f"M{i + 1}: {count / COUNTS_PER_TURN:.3f}바퀴")


def main():
    with SMBus(1) as bus:
        try:
            stop_all(bus)

            # 자기식 엔코더 모드, 구동은 PWM 명령 사용
            bus.write_i2c_block_data(ADDRESS, TYPE_REG, [3])
            sleep(0.2)
            stop_all(bus)

            print("회전할 바퀴 수를 입력하세요. 예: 1, 5, 10")
            print("0: 정지 유지 / Ctrl+C: 프로그램 종료")

            while True:
                text = input("\n회전할 바퀴 수: ").strip()

                try:
                    turns = float(text)
                except ValueError:
                    print("숫자를 입력해 주세요.")
                    continue

                if not math.isfinite(turns) or turns < 0:
                    print("0 이상의 유한한 숫자를 입력해 주세요.")
                    continue

                if turns == 0:
                    stop_all(bus)
                    print("모든 모터 정지")
                    continue

                if turns * COUNTS_PER_TURN < 1:
                    print("입력값이 엔코더 1카운트보다 작습니다.")
                    continue

                if turns * COUNTS_PER_TURN >= 2**31:
                    print("회전 수가 너무 큽니다.")
                    continue

                rotate(bus, turns)

        except (KeyboardInterrupt, EOFError):
            print("\n프로그램을 종료합니다.")

        except (OSError, RuntimeError) as error:
            print(f"\n오류: {error}")

        finally:
            try:
                stop_all(bus)
            except OSError:
                print("정지 명령 전송 실패: 모터 전원을 꺼 주세요.")


if __name__ == "__main__":
    main()
    
    