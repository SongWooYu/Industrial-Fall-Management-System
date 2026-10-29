# 파일명: lcd2004_test.py
# I2C 2004 LCD에 예시 차량 상태 표시

from time import sleep
from RPLCD.i2c import CharLCD

LCD_ADDRESS = 0x27  # LCD 주소에 맞게 변경

lcd = CharLCD(
    i2c_expander="PCF8574",
    address=LCD_ADDRESS,
    port=1,
    cols=20,
    rows=4,
    auto_linebreaks=False,
    backlight_enabled=True
)

try:
    lcd.clear()

    # 실제 측정값이 아닌 고정된 예시 문구
    lines = [
        "IP:192.168.0.13",
        "DIR:FORWARD",
        "SPEED: 50%",
        "DIST: 35cm"
    ]

    for row, text in enumerate(lines):
        lcd.cursor_pos = (row, 0)
        lcd.write_string(text.ljust(20))

    print("LCD 표시 완료. 종료하려면 Ctrl+C를 누르세요.")

    # 화면을 다시 쓰지 않고 표시 유지
    while True:
        sleep(1)

except KeyboardInterrupt:
    print("\n프로그램을 종료합니다.")

finally:
    lcd.close(clear=True)
    
    