# 재사용 코드·모델 출처

이 프로젝트는 사용자 관리 `SongWooYu/Home-Safety-System`의 낙상 관련 구현
(기준 commit `1cac57e`)에서 분리했다. 해당 저장소의 스마트홈 기능이나 Git 이력을 포함하지 않는다.

## EdgeFallKit

- Upstream: https://github.com/suharvest/edgefallkit
- Pinned commit: `ef3bda2fd865fe6b80119fb1c896a63bfc4228ee`.
- License: Apache-2.0. 원문: `licenses/EdgeFallKit-Apache-2.0.txt`.
- 로컬 변경: native C ABI bridge, `strict_confirmation.patch`(v6), 별도 Python 입력/로컬 이벤트 연결.
- tracker, temporal classifier와 frozen Hailo weights는 setup script가 위 commit에서 빌드한다.
- `native/edgefall_shadow`의 `shadow`는 ABI 호환 이름이며 운영은 단일 경로다.

## YOLOv8s-Pose / Hailo

Hailo Model Zoo의 v2.15 Hailo-8 HEF를 사용한다. HEF·Hailo SDK·펌웨어는 이 저장소에
재배포하지 않는다. EdgeFallKit Apache-2.0 라이선스가 이 외부 모델·SDK까지 재라이선스하지 않는다.
업스트림 Ultralytics/Hailo 모델 사용·상용 배포 조건은 별도로 확인해야 한다.
`--accept-upstream-license`는 조건 확인 표시이지 추가 사용권 부여가 아니다.

## 기타 런타임

OpenCV, NumPy, GStreamer, Flask, MediaMTX, HailoRT/TAPPAS는 각자 라이선스와
OS/공급자 설치 방식으로 사용한다. 시스템 package 및 실행 binary를 Git에 포함하지 않는다.
얼굴 모델, 학습 데이터셋, 촬영 영상은 포함하지 않는다.

## 고정 한국어 안내 음성

`src/industrial_fall/static/audio/fall-detected-ko-sunhi-v1.mp3`는 프로젝트에서
2026-10-22에 합성한 “낙상이 감지되었습니다.” 문구를 변환한 자산이다.
원래 생성 도구는 edge-tts 7.3.1, Microsoft Edge `ko-KR-SunHiNeural` 음성이다.
개인 음성 복제나 카메라 녹음이 아니다. 이 파일을 제공할 뿐 운영 시 합성 서비스를 호출하지 않는다.
생성 음성에 EdgeFallKit Apache-2.0 라이선스가 적용되는 것은 아니다.
출처와 변환 설정은 자산 디렉토리 README 참조. 상용 배포 시 공급자 이용 조건을 별도 확인한다.

`src/industrial_fall/static/audio/fall-detected-ko-espeak-v1.mp3`는 2026-10-29에
eSpeak NG 1.52.0으로 Pi4 코드의 한국어 기본값을 재현해 생성한 고정 안내 문장이다.
eSpeak 엔진이나 음성 데이터 패키지를 재배포하는 것이 아니며 운영 시 이 파일만 재생한다.
원래 Pi4 녹음과 비트 단위 동일성을 주장하지 않는다. 생성 명령·체크섬은 자산 README 참조.
