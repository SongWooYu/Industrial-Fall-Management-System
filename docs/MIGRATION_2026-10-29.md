# 2026-10-29: 산업안전 낙상 전용 프로젝트로 분리

## 기준

원본: `SongWooYu/Home-Safety-System`, commit `1cac57e`.
새 저장소: `SongWooYu/Industrial-Fall-Management-System` (공개, 사용자 승인).
새 경로: `/home/pi/Industrial-Fall-Management-System`.
새 패키지: `industrial_fall`. 원본 Git 이력과 비공개 데이터를 새 repo로 복제하지 않는다.

## 새 프로젝트에서 제거한 범위

- Pi4 서버, PostgreSQL, MQTT 송수신/브로커 의존성.
- 스마트홈 센서·자동/수동 제어·협업자 웹 복제본·낙상 알림 전송·TTS/볼륨.
- 얼굴 등록/인식/관리키/모델/데이터, Camera Module 3 입력, 웹캠.
- 과거 YOLO11/NCNN/GRU 실험 및 병렬 shadow 추론 코드.
- HDMI 키오스크 관리, 불필요한 카메라별 ffmpeg 인코더.
- 방대한 기존 dashboard 대신 단일 산업안전 모니터·감지 기록 화면.

새 코드에는 MQTT 연결/발행이 없다. Pi4가 꺼져 있어도 Pi5가 단독 실행한다.
Camera Module 3와 ABKO 하드웨어가 없어도 된다. Dahua 계정 변수 하나만 이전했고,
기존 얼굴 데이터·관리키·전체 .env는 새 저장소로 가져오지 않았다.

## 보존한 범위

- Dahua 저지연 디코드, Hailo metadata-only 추론, EdgeFall v6 동결 모델/패치.
- 원본 WebRTC 우선 + 실패 시 MJPEG, 박스·관절 MJPEG 확인 모드.
- 동일 분석 프레임 캡처, 최근 50개 이벤트, 제한된 진단 로그.
- 기존 Cloudflare `vision.aihous.site → localhost:8080` 경로.

**원본 공유 저장소와 Pi4 `/opt/smarthome`·자동 동기화는 삭제/수정하지 않는다.**
이는 사용자에게 안내하고 승인받은 분리 범위다. 원본 로컬 경로는 비활성 복구본으로 남는다.
원본은 새 서비스의 실행/설정/가상환경 의존성이 아니다. 새 `.venv`를 만들었다.
초기 빌드는 로컬의 고정 upstream 소스를 읽었지만 생성물에는 원본 경로 의존성이 없다.

## 서비스 전환

기존 활성 서비스: `smarthome-vision.service`, `smarthome-mediamtx.service`.
새 서비스: `industrial-fall.service`, `industrial-fall-media.service`.
같은 8080/8889/8189와 Hailo를 동시에 점유하지 않도록 기존 것을 중지 후 새 것으로 전환한다.
HailoRT, SSH/Tailscale, OS 네트워크, Cloudflare 연결은 삭제하지 않는다.

전환 전 기존 서비스 상태: 두 개 enabled/active. 기존 repo는 clean, HEAD `1cac57e`.
기존 HEF와 새 HEF checksum 동일, 새 native 재빌드 checksum도 동일.

전환 완료: 새 서비스 2개 active/enabled, 기존 서비스 2개 inactive/disabled.
8080은 새 `industrial_fall.app`이 제공한다. 이전 로컬 repo는 clean 상태 그대로 보존했다.

### 복구 방법 (필요할 때만)

```bash
sudo systemctl disable --now industrial-fall industrial-fall-media
sudo systemctl enable --now smarthome-mediamtx smarthome-vision
curl -f http://127.0.0.1:8080/health
```

복구하면 스마트홈·얼굴 인식·MQTT가 다시 켜진다. 새 서비스와 동시에 켜지 않는다.
Pi4는 이번 전환에서 조작하지 않는다. 기존 Pi4에서는 Pi5가 offline/미수신으로 표시될 수 있다.

## 공개 저장소 위생

`.env`, `.venv`, `runtime/`, 로그, 이미지, 모델 binary는 Git 제외.
카메라 URL은 계정 없는 예시만 공개. 사용자 얼굴·이름·캡처·학습 데이터는 커밋 금지.
서비스 실제 설치와 실측 결과는 `VALIDATION.md`에 기록한다.
