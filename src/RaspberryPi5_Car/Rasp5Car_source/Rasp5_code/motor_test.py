# 파일명: motor_test.py
# 0: 모터 정지 / 1~4: 해당 모터 회전 / Ctrl+C: 종료

import sys
import termios
import tty
from time import sleep
from smbus2 import SMBus

I2C_ADDRESS = 0x34
MOTOR_TYPE_REG = 20
MOTOR_PWM_REG = 31

POWER = 100                     # 고정 PWM 출력: 30%
DIRECTION = [-1, 1, 1, -1]     # M1, M2, M3, M4 전진 방향


def set_motors(bus, values):
    # 음수를 8비트 데이터로 변환하여 전송
    data = [value & 0xFF for value in values]
    bus.write_i2c_block_data(I2C_ADDRESS, MOTOR_PWM_REG, data)


def main():
    terminal = sys.stdin.fileno()
    original_settings = termios.tcgetattr(terminal)

    with SMBus(1) as bus:
        try:
            # 엔코더를 사용하지 않는 PWM 시험 모드
            bus.write_i2c_block_data(
                I2C_ADDRESS, MOTOR_TYPE_REG, [0]
            )
            sleep(0.1)
            set_motors(bus, [0, 0, 0, 0])

            print("Hiwonder 모터 시험 — PWM 30%")
            print("0: 모두 정지")
            print("1: M1 / 2: M2 / 3: M3 / 4: M4")
            print("Ctrl+C: 모터 정지 후 프로그램 종료")
            print("숫자 키만 누르세요. Enter는 필요 없습니다.")

            # 한 글자씩 입력받고 Ctrl+C 기능은 유지
            tty.setcbreak(terminal)

            while True:
                key = sys.stdin.read(1)

                if not key:
                    break

                if key == "0":
                    set_motors(bus, [0, 0, 0, 0])
                    print("모든 모터 정지 — 입력 대기")

                elif key in ("1", "2", "3", "4"):
                    index = int(key) - 1

                    # 기존 모터 정지 후 선택한 모터만 회전
                    set_motors(bus, [0, 0, 0, 0])
                    sleep(0.1)

                    values = [0, 0, 0, 0]
                    values[index] = POWER * DIRECTION[index]
                    set_motors(bus, values)

                    print(f"M{key} 회전 — PWM {POWER}%")

        except KeyboardInterrupt:
            print("\n프로그램을 종료합니다.")

        except OSError as error:
            print(f"\nI2C 통신 오류: {error}")

        finally:
            try:
                set_motors(bus, [0, 0, 0, 0])
            except OSError:
                print("정지 명령 전송 실패: 모터 전원을 꺼 주세요.")
            finally:
                termios.tcsetattr(
                    terminal, termios.TCSADRAIN, original_settings
                )


if __name__ == "__main__":
    main()
    
    