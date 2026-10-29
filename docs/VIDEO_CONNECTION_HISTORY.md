# 영상 연결 이력 — 2026-10-29

## 목적과 확인 위치

현장 모니터 아래 **영상 연결 이력**에서 최근 200개 전송 이벤트를 확인한다.
기본 필터는 **전체 · WebRTC + MJPEG**다. **WebRTC / MJPEG / 실패·대체**로 좁혀 볼 수 있다.
WebRTC는 연결 시도, 협상 응답, 실제 영상 수신 확인, 종료 및 실패 후 MJPEG 대체를 표시한다.
박스·관절 모드를 의도적으로 선택한 MJPEG는 WebRTC 실패에서 제외한다.
필터는 표시만 바꾸며 모든 전송 방식의 기록은 계속 저장한다.
상단 수신 확인 건수는 최근 보관된 200개 이벤트 안의 `rtc_playing`/`mjpeg_decoded` 건수로,
현재 접속자 수나 누적 접속 횟수·성공률이 아니다.
이는 **영상 파일 녹화가 아니라 연결 정보 기록**이다. 낙상 감지 기록과 별도다.

- API: `GET /api/video-connections`.
- 파일: `runtime/logs/video-connections.jsonl`, `.1`, `.2`.
- JSONL 각 2,000,000바이트 미만, 세 파일 합계 약 6MB 이하 순환 보관.
- 메모리 최근 200건, 서비스 재시작 시 보존 파일에서 최근 200건 복원.
- 기록 대기열 256, 초당 5건/순간 60건 토큰 제한. 초과·디스크 오류 제외 건수를 API/화면에 표시.
- 브라우저 전송 대기 최대 8개, 4초 타임아웃, 무한 재전송 큐 없음.
- 원본 영상·캡처·SDP·카메라 계정·IP·사용자 이름·User-Agent는 기록하지 않는다.
- 파일 기록은 별도 worker에서 수행하며 AI 추론 스레드는 로그 저장을 기다리지 않는다.

## 어떤 기록이 무엇을 입증하는가

| 이벤트 | 의미 | 입증하지 않는 것 |
| --- | --- | --- |
| `attempt` | 브라우저 연결 시도, 임시 연결 ID 생성 | 영상 수신 |
| `whep_accepted` | MediaMTX가 SDP 협상 HTTP 응답 | ICE 성공·영상 수신 |
| `rtc_playing` | 브라우저 playing + 복호화 프레임 > 0 + 선택된 ICE pair 관측 | 외부망 접속, 장시간 안정성 |
| `fallback` | WebRTC 실패/미지원 또는 의도된 분석 모드로 MJPEG 선택 | MJPEG 수신 성공 |
| `mjpeg_open` / `mjpeg_sent` | 서버가 요청 수락 / 첫 JPEG 송신 시도 | 브라우저 디코드 |
| `mjpeg_decoded` | 브라우저에서 유효 크기의 JPEG 이미지 관측 | 정확한 MJPEG FPS·끝단 지연·계속 갱신됨 |
| `mjpeg_error` | 브라우저 이미지 오류, 3초 후 재시도 | AI 분석 중단 |
| `ended` / `mjpeg_closed` | 브라우저 정리 / 서버 송신 종료 | 사고 해결 |

`overlay_selected`는 박스·관절 확인을 위해 **사용자가 선택한 MJPEG**이며,
외부 WebRTC 실패 통계에 포함하면 안 된다. `whep_failed`, `video_stalled`,
`startup_timeout`, `ice_failed` 등은 별도 사유다.
HTTP 상태, ICE 상태, WebRTC local/remote candidate 종류(host/srflx/prflx/relay),
UDP/TCP, 수신 크기, 연결 시도 이후 경과 시간을 함께 남긴다.
`elapsed_ms`는 **영상 촬영→표시 지연**이 아니다.

동일 `connection_id`로 서버와 브라우저 기록을 연결한다. 브라우저 `sequence`는
해당 탭의 발생 순서이며, 서버 수신 시각 순서는 네트워크 때문에 다를 수 있다.
브라우저가 갑자기 종료되거나 네트워크가 끊기면 마지막 보고가 없을 수 있다.
새 코드 이전 탭이나 직접 MJPEG 이용자는 `unattributed`로 서버 관측만 남는다.
브라우저가 1.5초 이내에 연결을 닫으면 RTC 통계 확인 전 종료될 수 있다.

## 외부망 성공을 잘못 판정하지 않기

`domain_url`은 주소창에 도메인을 썼다는 분류이지 외부 접속 증거가 아니다.
Pi5 내부 Chromium에서 `https://vision.aihous.site`를 열어도 영상은
같은 LAN의 `host` candidate로 직접 수신할 수 있다.

외부 검증은 휴대폰 Wi-Fi와 VPN/Tailscale을 끈 LTE/5G 브라우저에서 수행한다.
새로고침 후 원본 모드를 20초 이상 유지하고, 같은 연결 ID에서
`rtc_playing`과 candidate 종류 또는 `fallback → mjpeg_decoded`를 확인한다.
이번 변경은 계측 추가이며 TURN을 설치하거나 외부 WebRTC 경로를 개통한 것이 아니다.
현재 클라이언트 `iceServers: []`와 기존 MediaMTX 설정은 유지했다.

## 보안·운영 한계

브라우저 보고는 인증된 감사 로그가 아니다. 동일 출처 검사, JSON 크기 2KB 제한,
enum/수치 검증, 입력 allowlist와 전역 rate limit만 사용한다.
허용되지 않은 추가 필드는 버리며, 브라우저의 시각/source/임의 오류 문구는 신뢰하지 않는다.
로그 API는 현재 대시보드와 같은 공개 접근 범위다. 개인·네트워크 주소 정보는 넣지 않는다.
로그가 너무 많으면 오래된 기록은 의도적으로 순환 삭제된다. 장기 분석용 영구 DB는 아니다.
저장 장애는 영상·추론을 중지시키지 않고 화면에 오류를 표시한다.

## 검증

자동 테스트는 크기 제한, 입력 검증, 원본 정보 제거, 순환 저장/복원,
잘린 마지막 줄 이후 복원, 저장 실패, CSRF/프록시 출처 검사,
WHEP 성공≠영상 성공, 브라우저 없는 직접 MJPEG 기록을 검증한다.
`tests/browser_check.py`는 실제 Chromium에서 WebRTC/분석 MJPEG와
브라우저 내 WHEP 503 강제 주입 → 원본 MJPEG 수신/이력 저장을 확인한다.
강제 실패 테스트 기록은 실제 연결 전환 기록이지만 인터넷 장애를 의미하지 않는다.
실제 낙상 이벤트나 하드웨어 경보는 테스트에서 발생시키지 않는다.

### 전체·WebRTC 필터 반영 확인

- Python 48개 테스트 통과. WebRTC 수신·종료 API 기록과 파일 저장 후 복원을 추가 검사했다.
- `tests/browser_connection_history_check.py`: 전체 기본 표시, WebRTC/MJPEG/실패 필터,
  의도적인 분석 MJPEG 제외, 협상과 수신 구분, 빈 목록 및 모바일 화면 검증 통과.
- 실제 HTTPS 배포에서도 새 `rtc_playing` 기록(복호화 21프레임)이 기본/ WebRTC 목록에 표시되고,
  WebRTC 종료 및 강제 WHEP 실패 후 대체 사유가 기록되는 것을 확인했다.
- 앱 재시작 전 WebRTC 수신 기록 17개가 모두 같은 ID로 복원됨을 확인했다.
  당시 저장 오류·제외 기록 0건, 카메라·분석 health 정상.
- 실측은 Pi5 내부 Chromium의 host ↔ host UDP이므로 외부망 WebRTC 성공 증거가 아니다.
  영상 연결/인코딩·AI 모델·음성 코드는 변경하지 않았으며 기존 로그를 삭제하지 않았다.
