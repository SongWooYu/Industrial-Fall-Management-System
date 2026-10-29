# 파일명: ultrasonic_test.py
# HC-SR04 거리 측정 / Ctrl+C 종료

from time import sleep
from gpiozero import DistanceSensor
from gpiozero.pins.lgpio import LGPIOFactory

TRIG_PIN = 23
ECHO_PIN = 24


def main():
    factory = LGPIOFactory()
    sensor = None

    try:
        sensor = DistanceSensor(
            echo=ECHO_PIN,
            trigger=TRIG_PIN,
            max_distance=4.0,       # 최대 거리: 4m
            queue_len=3,           # 측정값 흔들림 완화
            partial=True,
            pin_factory=factory
        )

        print("HC-SR04 초음파 거리 측정")
        print("TRIG: GPIO23 / ECHO: GPIO24")
        print("종료하려면 Ctrl+C를 누르세요.\n")

        sleep(1)

        while True:
            distance_cm = sensor.distance * 100
            print(f"거리: {distance_cm:6.1f} cm", flush=True)
            sleep(0.5)

    except KeyboardInterrupt:
        print("\n프로그램을 종료합니다.")

    finally:
        if sensor is not None:
            sensor.close()
        factory.close()


if __name__ == "__main__":
    main()
    
    
    