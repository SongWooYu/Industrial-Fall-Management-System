# 사용자 추가 커밋 b917579 검토 — 2026-10-29

`29a3844 → b917579` fast-forward 완료. 변경 70파일/7,852줄은 모두
`src/RaspberryPi5_Car/` 추가이며 기존 낙상 코드 변경은 없다.
이번 검토는 Python·배치·안내문 정적 검토다. HWP/PSD 내부 설계와 실제 주행은 검증하지 않았다.
Python 파일 12개의 AST 구문 검사는 통과했다. 의존성 설치·하드웨어 동작 검증과는 다르다.
차량 프로그램을 import/실행하지 않았고, 낙상 서비스에 등록하지 않았다.

## 현재 구조

- `Rasp5_code/car_ws_serverV3.py`: aiohttp `0.0.0.0:5000`, `/control`과 `/video` WebSocket.
  USB 영상 JPEG + 8바이트 sequence를 전송한다. WebRTC가 아니다.
- `PC_code/car_tk_clientV3.py`: Windows/Tkinter 제어·영상 표시, PC에서 YOLO 검색,
  사진 특징 비교, TTS와 SQLite 탐사 이력을 관리한다.
- 모터 독립 프로세스, 연결 소유권, 짧은 명령 lease, 영상/명령 timeout,
  장애물 정지/stop acknowledgment 등 방어 장치가 있으나 실제 장비 동작은 별도 검증 필요.
- 차량 코드의 YOLO11은 PC 검색용이다. 낙상 운영 경로의 Hailo YOLOv8s-Pose/EdgeFall을
  바꾼 것이 아니며 Pi5 낙상 서비스에서 중복 실행되지 않는다.

## 통합 전에 해결할 점

1. `/control`에 사전 사용자 인증이 없다. 첫 접속자에게 token을 발급하는 것은 소유권/영상 연동용이지
   접속 권한 검증이 아니다. 그대로 공개 터널에 연결하지 않는다.
2. 상태 경로가 `/home/humanoid/Desktop/Car3/car_state.json`으로 고정되어 있다.
   실행 장비·사용자·GPIO/I2C·카메라 번호 설정을 분리해야 한다.
3. 실행 안내가 참조하는 `download_helmet_model.py`, `test_helmet_photos.py`,
   `car_tk_clientV3(20).py` 및 `models/structural_defects.pt`, `models/helmet_detection.pt`가
   이번 tree에 없다. 배치 파일의 실제 진입점은 `car_tk_clientV3.py`다.
   문서와 배포 파일을 맞추고 모델 출처·라이선스·checksum을 정해야 한다.
4. USB 카메라·모터·센서 라이브러리 설치 목록과 버전 잠금이 낙상 앱과 별개로 필요하다.
   두 프로그램의 패키지를 같은 venv에 무조건 설치하지 않는다.
5. Git에 SQLite 탐사 이력과 사람/현장 캡처가 포함되어 있다. 공개 허용·동의 여부를 확인하고,
   이후 실행 데이터는 Git 밖 runtime 디렉토리에 두는 것이 좋다.
   이번에는 사용자가 올린 파일을 임의 삭제하거나 Git 이력을 재작성하지 않았다.

차량과 낙상 연결은 이후 별도 이벤트 adapter로 설계한다.
지금 추가한 영상 연결 이력 API는 **산업 낙상 웹 플레이어만** 계측하며,
차량 PC/WS의 연결 로그가 자동 통합된 것은 아니다.
