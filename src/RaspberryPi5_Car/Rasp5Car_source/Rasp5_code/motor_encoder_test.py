# 파일명: motor_encoder_test.py
# 손으로 바퀴를 돌리면 회전수와 엔코더 카운트 표시

import struct
from time import sleep
from smbus2 import SMBus

ADDRESS = 0x34
TYPE_REG = 20
PWM_REG = 31
ENCODER_REG = 60

COUNTS_PER_TURN = 2600          # 바퀴 1회전당 카운트


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
    # 32비트 카운터 넘침 처리
    return (current - previous + 2**31) % 2**32 - 2**31


def show_counts(counts):
    print("\n모터       누적 펄스       회전수")
    print("--------------------------------")

    for i, count in enumerate(counts):
        turns = count / COUNTS_PER_TURN
        print(f"M{i + 1}     {count:+10d}     {turns:+9.3f}")

    print(flush=True)


def main():
    with SMBus(1) as bus:
        try:
            stop_all(bus)

            # 자기식 엔코더 모드
            bus.write_i2c_block_data(
                ADDRESS, TYPE_REG, [3]
            )
            sleep(0.2)
            stop_all(bus)

            previous = read_counts(bus)
            total = [0, 0, 0, 0]

            print("손으로 바퀴를 돌려 주세요.")
            print("프로그램 시작 위치를 0으로 표시합니다.")
            print("반대로 돌리면 누적값이 감소합니다.")
            print("Ctrl+C: 프로그램 종료")

            show_counts(total)

            while True:
                sleep(0.2)
                current = read_counts(bus)
                changed = False

                for i in range(4):
                    delta = difference(current[i], previous[i])

                    if delta != 0:
                        total[i] += delta
                        changed = True

                previous = current

                # 값이 바뀔 때만 출력
                if changed:
                    show_counts(total)

        except KeyboardInterrupt:
            print("\n프로그램을 종료합니다.")

        except OSError as error:
            print(f"\nI2C 통신 오류: {error}")

        finally:
            try:
                stop_all(bus)
            except OSError:
                print("정지 명령 전송 실패: 모터 전원을 꺼 주세요.")


if __name__ == "__main__":
    main()
    
    