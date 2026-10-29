# 구조와 모델 계약

## 역할

Pi5 한 대가 영상 수신·추론·상태 관리·웹 화면을 담당한다. Dahua만 낙상 입력으로 사용한다.
`pipeline.py`는 capture, analyze, render 스레드를 하나씩 사용한다.
capture의 latest 슬롯 1개와 분석 결과 슬롯 1개를 갱신하며, 분석에는 새로운 frame_seq만 넣는다.
GStreamer appsink는 max-buffers=1/drop=true. Hailo는 metadata-only이므로
별도 전체 화면 영상 표시나 중복 CPU 모델을 실행하지 않는다.

render는 구독자 있는 모드만 10 FPS 이하로 JPEG를 생성한다. 동일 모드 접속자는
인코딩 결과를 공유한다. 원본 WebRTC는 Dahua H.264를 MediaMTX가 재인코딩 없이 전달한다.
입력 age와 Hailo 추론 ms는 끝단 브라우저까지의 지연을 뜻하지 않는다.

## 재사용한 정확한 모델

- Hailo Model Zoo v2.15 YOLOv8s-Pose, COCO17 관절.
- HEF: `yolov8s_pose_v2_15_h8.hef`.
- SHA256: `e19856699ed47cf866d23265827f960b263f287dab5e54e82c7ce37e12525a2d`.
- EdgeFallKit 고정 commit: `ef3bda2fd865fe6b80119fb1c896a63bfc4228ee`.
- native ABI 2, profile: `edgefallkit-hailo8-yolov8s-pose-v1+units-separated-v6`.
- 이 장비 재빌드 library SHA256:
  `b9daabf1c00e6070b5548d8f509bd5662688d404bea18f08d1ba46a0f59fa48d`.

Hailo는 자세 추론을 수행한다. tracker와 작은 시간 분류 MLP/상태 머신은 native CPU 코드다.
사람당 48개 프레임을 모으며 15 FPS에서 약 3.13초의 구간을 본다.
프레임당 56 features(17 × x,y,confidence + 5global)를 시간 요약 504 features로 만들고,
동결된 MLP(504 → 16 ReLU → sigmoid)에 넣는다. 임계 .75, 3회 연속 양성,
3프레임 주기 평가와 실제 동작/기하 조건이 함께 만족해야 새 낙상을 확정한다.
확률처럼 생긴 값은 모델 출력 점수이며 보정된 사고 확률/산업현장 정확도가 아니다.

## 과거 수정 중 반드시 보존하는 부분

`native/edgefall_shadow/strict_confirmation.patch`는 업스트림에 적용할 패치다.
`edgefall_c_api.cpp`는 Python과 연결하는 ABI다. `shadow`라는 파일·심볼 이름은
과거 ABI 이름일 뿐, 지금 shadow 모드 또는 두 번째 추론 경로는 없다.

v6 핵심: `observationFrom()`의 기하용 hip-Y는 0..1,
`temporalFrameFrom()`의 동결 MLP용 hip-Y는 **640-pixel 공간**.
학습 scaler hip-Y 평균 약 301–319인데 이를 0..1로 잘못 넣으면 모델 판정이 깨진다.
이전 실패 원인은 임계값 문제가 아니라 이 단위 계약 오류였고, v6에서 분리했다.
이번 프로젝트 분리에는 재학습, 새 임계값, 새로운 낙상 알고리즘을 추가하지 않았다.

기존 패치의 빠른 하강/수평 자세/원근상 짧아진 자세 경로는 후보 생성 조건이다.
기하만으로 경보를 우회시키지 않는다. 관절 누락·골반 지지점 변경은 미확정 동작 증거를
초기화한다. 원래의 YOLO11/GRU/독립 규칙 탐지기는 포함하지 않는다.
그렇지만 EdgeFall 자체에 상태 머신과 기하 조건이 있다는 사실을 숨기지 않는다.

## 상태와 이벤트

| 상태 | 화면 의미 | 로컬 이벤트 |
| --- | --- | --- |
| normal | 확정 경보 없음 (안전 보증 아님) | 이전 비정상이 정상으로 돌아오면 fall_cleared |
| suspected | 낙상 의심, 확인 중 | fall_suspected |
| fallen | 낙상 감지, 현장 확인 필요 | native 새 event_id마다 fall_confirmed 1회 |
| recovering | 회복 동작 관측 | fall_recovering |
| unobserved | 추적 대상 없거나 최신 분석 없음 | 감지 후 트랙 소실 2초면 tracking_lost |

추적 번호는 신원이 아니다. 단조 증가해도 최근 상태만 유지하므로 무한 버퍼가 아니다.
이벤트 중복 방지 기록은 최근 256개만 유지한다.
트랙 소실을 사고 해결로 간주하지 않는다. 과거 경보는 감지 기록에 남는다.
카메라/추론 단절 시 오래된 정상/낙상 점수를 최신 결과처럼 표시하지 않는다.
입력 프레임 간격이 1초를 넘으면 temporal runtime을 재생성해 재연결 전후 동작을 섞지 않는다.

## 확장 원칙

현재 이벤트는 메모리 저널에만 기록한다. 차량·알림 통신은 나중에 이벤트 adapter로
추가하되 카메라/추론 스레드를 네트워크 ACK 대기로 막지 않는다. 지금은 미구현이다.
산업환경용 데이터 재검증 후 위험지역 정의, 복수 카메라, 작업자 식별 등을 별도 결정한다.
