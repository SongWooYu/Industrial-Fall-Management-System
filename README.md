# 산업안전 낙상관리시스템

**Industrial Fall Management System** — Raspberry Pi 5 + Hailo-8에서 실행하는
단독 카메라 낙상 감지 POC. 스마트홈 통합이 아닌 **산업 현장 낙상 관측·확인**에 집중한다.

현재 범위: Dahua RTSP 입력, Hailo 자세 추론, EdgeFall 낙상 판정,
실시간 확인 화면, 최근 이벤트 50개, 감지 순간 박스·관절 캡처, **브라우저 음성 알림**.
Pi4·MQTT·DB·기기 제어·서버/스피커 TTS·얼굴 인식·얼굴 등록·Camera Module 3·웹캠·GRU는 포함하지 않는다.
미니 차량 및 위험 구역 작업은 이후 별도 기능으로 설계한다.

> 개발·시연용이며 인증된 산업 안전 장치가 아니다. 산업 현장의 보호구, 가림,
> 작업 자세, 카메라 설치 각도에 대한 오탐·미탐 검증을 아직 수행하지 않았다.
> 이 시스템만으로 사고·부상이나 안전 여부를 확정해서는 안 된다.

## 실행 구조

```text
Dahua H.264 RTSP
 ├─ 최신 프레임 디코드(960×540) → Hailo YOLOv8s-Pose
 │   → EdgeFall tracker + 시간 MLP + 상태 머신
 │   → Pi5 로컬 이벤트 / 동일 프레임 캡처 / 웹 상태
 │   → 요청이 있을 때만 박스·관절 MJPEG 생성
 └─ MediaMTX 원본 H.264 전달 → 브라우저 WebRTC
     (연결 실패 시 브라우저가 Pi5 MJPEG에 연결)
```

Pi4/브로커 연결이 없어도 동작한다. 한 프로세스에서 Hailo 모델을 한 번만 실행한다.
원본 WebRTC에는 박스가 없다. 화면의 **분석 영상 · 박스·관절**을 선택하면
추론한 프레임에 직접 그린 MJPEG를 표시한다. Canvas로 별도 좌표를 맞추지 않는다.
낙상 의심·감지·회복 상태 배지는 두 영상 모드 모두에 크게 표시한다.
이는 현재 분석 상태 안내이며 영상 프레임과 정밀 동기화된 관절 오버레이는 아니다.

### 화면 경보와 소리 시험

1. 화면을 새로고침하고 **소리만 테스트**를 누르면 접속한 PC·휴대폰에서
   “낙상이 감지되었습니다.”를 재생한다. 실제 이벤트·캡처를 만들지 않는다.
   목소리 메뉴에서 **자연스러운 음성 · 안내형**(기본)과 **기존 음성 · 브라우저 TTS**를
   고를 수 있다. 목소리 선택은 저장되고, 안내형은 짧은 고정 음성 파일을 재생한다.
2. **음성 알림 켜기**를 누르면 이후 새 낙상 확정 이벤트를 음성 안내한다.
   과거 기록·반복 조회·의심 단계는 읽지 않는다. 새로고침 후 다시 켜야 한다.
3. **기록 확인**은 이 탭의 낙상 안내만 접는다. 모델 상태나 감지 기록을 삭제하지 않는다.
   회복 동작·추적 종료는 사고 해결을 의미하지 않는다.

안내형 목소리는 브라우저와 무관하게 같은 파일이다. 파일 재생 실패 시 기존 TTS로 대체하고,
기존 한국어 음성도 사용할 수 없으면 경고음으로 대체한다.
탭 음소거·기기 볼륨을 확인한다. 절전·화면 잠금·백그라운드 알림은 보장하지 않는다.
사용법·상태 의미·검증 범위: [브라우저 경보 안내](docs/BROWSER_ALERTS.md).

### 영상 연결 이력

화면 아래 **영상 연결 이력**은 WebRTC와 MJPEG를 모두 기록하며 기본으로 전체를 표시한다.
필터에서 **WebRTC / MJPEG / 실패·대체**를 선택할 수 있다. WebRTC 연결 시도·협상 응답·
실제 프레임 수신·종료와 MJPEG 전환 사유를 구분한다. 협상 성공만으로 영상 수신 성공으로
표시하지 않는다. 최근 200건과 순환 로그는 재시작 후 복원되며 영상 파일 녹화는 아니다.
세부 사항: [영상 연결 이력](docs/VIDEO_CONNECTION_HISTORY.md).

## 현재 장비 접속

- 작업 경로: `/home/pi/Industrial-Fall-Management-System`
- Pi5 LAN: <http://192.168.1.20:8080>
- 기존 HTTPS 주소 유지: <https://vision.aihous.site>
- 서비스: `industrial-fall.service`, `industrial-fall-media.service`
- 설정: `config/cameras.yaml`, `config/mediamtx.yml`, 비공개 `.env`

Cloudflare의 기존 `localhost:8080` 웹 경로를 그대로 이용한다. HTTP 터널은
WebRTC 영상의 TURN 중계기가 아니므로 외부 NAT에서 WebRTC가 실패할 수 있다.
그 경우 같은 HTTPS 주소의 MJPEG로 전환한다. Tailscale은 영상 접속의 필수 조건이 아니다.

## 설치 (현재와 호환되는 Pi5 + Hailo OS 환경)

기존 HailoRT/TAPPAS/GStreamer가 검증된 장비를 전제로 한다. 이 설치는 OS를 초기화하거나
Hailo 드라이버를 임의로 업그레이드하지 않는다. 다른 OS/SDK에서는 먼저 호환성 검증이 필요하다.

```bash
cd /home/pi
git clone https://github.com/SongWooYu/Industrial-Fall-Management-System.git
cd Industrial-Fall-Management-System
python3 -m venv --system-site-packages .venv
cp .env.example .env
chmod 600 .env
# .env의 DAHUA_RTSP_URL을 실제 카메라 계정으로 편집
```

시스템 패키지: Python 3.13, python3-venv, python3-opencv, python3-numpy,
python3-flask, python3-yaml, python3-dotenv, python3-gi, GStreamer RTSP/avdec,
HailoRT/TAPPAS, C++17 compiler, patch, git, curl. MediaMTX는 `/usr/local/bin/mediamtx`
(이 장비 검증 버전 v1.21.0). Python pip OpenCV wheel로 시스템 OpenCV를 덮어쓰지 않는다.
`requirements-runtime.txt`는 현재 사용한 시스템 Python 모듈 버전 기록이다.

모델·업스트림 약관을 확인한 뒤:

```bash
./scripts/setup_edgefall_shadow.sh --accept-upstream-license
.venv/bin/python scripts/check_runtime.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
sudo ./scripts/install.sh
```

설치 스크립트의 서비스 경로는 위 작업 경로와 사용자 `pi` 기준이다.
기존 `smarthome-vision`/`smarthome-mediamtx`가 실행 중이면 설치는 중단한다.
동일 Hailo 장치와 포트를 두 프로젝트가 동시에 점유하지 않도록 전환 창에 중지해야 한다.

## 운영

```bash
systemctl status industrial-fall industrial-fall-media
journalctl -u industrial-fall -n 80 --no-pager
curl -f http://127.0.0.1:8080/health
sudo systemctl restart industrial-fall
```

- `/health`: 카메라 **및 최근 추론**이 정상이면 200, 아니면 503.
- `/api/status`: 분석 상태·FPS·모델 점수·트랙. 점수는 사고 확률이 아니다.
- `/api/events`: 최신 이벤트 최대 50개. DB/디스크 영구 보관 없음.
- `/api/events/<id>/snapshot.jpg`: 감지한 프레임의 JPEG. 최대 50장, 장당 512 KB, TTL 1시간.
- `/stream/dahua?overlay=true`: 오버레이 영상. `false`: 원본 MJPEG.
- `/api/webrtc/dahua/whep`: WebRTC SDP 교환. 영상은 Pi5와 브라우저 간 전송.
- `/api/video-connections`: 최근 영상 연결 이력 200건. 실제 WebRTC 수신/MJPEG 전환 사유 구분.

이벤트·캡처는 프로세스 재시작 시 사라진다. 포즈 진단 로그는 `runtime/diagnostics`에
20 MB × 최대 3개로 순환 보관한다. RAW 영상·얼굴 특징은 저장하지 않는다.
MJPEG 동시 접속은 6개로 제한하고 추론은 접속자 수와 무관하게 한 번 수행한다.
영상 연결 이력은 별도로 `runtime/logs/video-connections.jsonl`에 2 MB × 3개로
순환 저장하며 재시작 후 복원한다. 공개 도메인으로 열었다는 것만으로 외부 WebRTC 성공은 아니다.

웹 화면은 POC용 단일 Flask 프로세스다. 앱 인증은 별도 구현하지 않았다.
공개 도메인의 열람 제한이 필요하면 기존 게이트웨이에서 제한해야 한다.
WSGI 멀티프로세스를 그대로 적용하면 Hailo를 중복 실행하므로 사용하지 않는다.

## 문서

- [구조·모델·상태의 의미](docs/ARCHITECTURE.md)
- [스마트홈에서의 분리와 복구](docs/MIGRATION_2026-10-29.md)
- [시험·산업 현장 검증 계획](docs/VALIDATION.md)
- [영상 연결 이력·MJPEG 전환 확인](docs/VIDEO_CONNECTION_HISTORY.md)
- [화면 낙상 상태·브라우저 음성 알림](docs/BROWSER_ALERTS.md)
- [Hailo 커널 로그 폭주 수정·복구](docs/HAILO_KERNEL_LOG_FIX_2026-10-29.md)
- [추가된 탐사 차량 코드 검토](docs/REVIEW_b917579.md)
- [출처와 모델 배포 고지](THIRD_PARTY_NOTICES.md)
