# Hailo 커널 로그 폭주 점검 및 최소 수정

## 원인 확인

장비: Pi5, 커널 `6.12.75+rpt-rpi-2712`, `hailo_pci` / HailoRT 4.23.0.
2026-10-29 14:50 KST 점검에서 최근 10초의 커널 로그는 30,030행,
`find_vma` WARNING은 **858회**였다. 특정 구간 실측이며 상시 평균은 아니다.
RAM journal 약 154MB가 약 2분마다 오래된 기록을 밀어냈다.
그러므로 이전 WebRTC 성공 기록이 없다는 사실만으로 과거 성공이 없었다고 단정할 수 없다.

스택: `find_vma → hailo_vdma_buffer_map → hailo_vdma_buffer_map_ioctl`.
설치 소스 `/usr/src/hailo_pci-4.23.0/linux/vdma/memory.c:170`에서
`find_vma(current->mm, addr_or_fd)`를 mmap read lock 없이 호출했다.
커널의 `rwsem.h:80` lock assertion과 일치한다.
WebRTC 실패가 경고의 원인이거나 모델이 느려서 생긴 메시지가 아니다.

Hailo 공식 [v4.24 소스](https://github.com/hailo-ai/hailort-drivers/blob/v4.24.0/linux/vdma/memory.c#L153-L156)는
해당 호출 전후에 `mmap_read_lock` / `mmap_read_unlock`을 추가한다.
[업스트림 PR #44](https://github.com/hailo-ai/hailort-drivers/pull/44)도 같은 경고를 설명한다.
이 변경은 경고 발생 원인을 수정하며 로그 수준을 낮추거나 WARN을 숨기는 방법이 아니다.

## 사용자 승인 후 적용

사용자가 원본 백업 후 드라이버 수정·중지를 승인했다.
현재 APT 캐시의 Hailo 패키지 후보는 4.23이므로 4.24 드라이버만 섞어 올리지 않았다.
**현재 4.23 ABI를 유지한 채 공식 두 줄 수정을 backport**했다.
추적 패치: `native/hailo-driver/find-vma-lock-4.23.patch`.

- 백업: `/var/backups/industrial-fall-hailo-20261029.3lstU8/`.
- `source/`: 전체 원본 DKMS 소스, `hailo_pci.ko.xz`: 원본 설치 모듈,
  `dkms-kernel/`: 해당 커널의 원본 DKMS 빌드본.
- 수정 소스를 DKMS build/install --force로 현재 커널용 빌드.
- 14:53:08 KST `industrial-fall`과 `hailort`를 중지하고 정상 `modprobe -r`/`modprobe`로 교체.
- 강제 모듈 제거, OS 재부팅, firmware/HEF/SDK/모델/낙상 임계값 변경은 하지 않았다.
- 서비스 재기동으로 새 연결 이력 코드도 함께 활성화했다.

| 항목 | 원본 | 수정 후 |
| --- | --- | --- |
| srcversion | `6AA96411C62B9CA56BF4262` | `C06D8EB430F7CE38169C3D4` |
| 모듈 SHA256 | `289ed31dea8341dfbfacba58edf03c8318a8bef6fc98d1e4036a34113524b0a0` | `ae32bceddf12d9aee1e01b18641cdd4a542c1e61a5e262ce18a08efc0425c6ff` |
| memory.c SHA256 | `a79d6dc23badfec6b716a990610f8acbfb67f6a884dfed2cff45b2d73eb65d7b` | `6073adb02760507d2a4e36986ac6c023982bf56d17845ba0c6c88ef2db4ad498` |

## 확인 명령

14:53:08 교체 후 14:58:46까지 실제 추론과 브라우저 재생을 반복하는 동안
해당 WARNING 재발 **0회**. `/health` 정상, AI 약 13.8 FPS를 확인했다.
아직 24시간 안정성 검증은 아니다.

```bash
cat /sys/module/hailo_pci/srcversion
modinfo -F srcversion hailo_pci
sudo journalctl -k --since '2026-10-29 14:53:08' --no-pager
curl -f http://127.0.0.1:8080/health
```

로드된 모듈과 디스크 모듈의 srcversion이 모두 수정본인지 확인한다.
서비스가 실행되고 실제 Hailo 추론 중일 때 경고가 사라져야 한다.
단순 서비스 중단 때문에 경고가 멈춘 것을 성공으로 판정하지 않는다.
기존 WARN으로 커널 taint의 W가 남을 수 있으며, 이것만으로 재발이라고 판단하지 않는다.

## 복구 (현재 커널·이 장비 전용, 필요할 때만)

서비스를 중지하고 `/dev/hailo0` 사용자가 없는지 확인한 후 실행한다.
`modprobe -r`가 실패하면 강제 제거하지 말고 사용자를 확인한다.

```bash
sudo systemctl stop industrial-fall.service hailort.service
sudo modprobe -r hailo_pci
sudo install -m 644 /var/backups/industrial-fall-hailo-20261029.3lstU8/hailo_pci.ko.xz /lib/modules/6.12.75+rpt-rpi-2712/updates/dkms/hailo_pci.ko.xz
sudo install -m 644 /var/backups/industrial-fall-hailo-20261029.3lstU8/source/linux/vdma/memory.c /usr/src/hailo_pci-4.23.0/linux/vdma/memory.c
sudo depmod -a 6.12.75+rpt-rpi-2712
sudo modprobe hailo_pci
sudo systemctl start hailort.service industrial-fall.service
```

긴급 복구 후 원본 소스로 DKMS build/install을 다시 수행해 캐시까지 일치시킨다.
원본 복구는 같은 커널 경고를 다시 발생시킨다.

## 한계·유지보수

- 이는 공식 수정의 최소 backport다. 이후 `vma` 사용 구간 전체의 lifetime까지
  보호하는 확장 패치는 아니며, 모든 드라이버 race/장시간 안정성을 증명하지 않는다.
- APT 드라이버 재설치·업데이트 시 로컬 소스 수정이 덮어써질 수 있다.
  버전 번호는 4.23으로 같으므로 srcversion/실제 소스와 경고를 함께 재확인한다.
- 공식 호환 패키지 조합으로 전환할 때 SDK·Python binding·드라이버·펌웨어를 같이 검증한다.
  본 backport를 버전이 다른 소스에 자동 적용하지 않는다. 설치 스크립트에 자동 패치를 넣지 않았다.
- systemd journal의 과거 유실 로그는 복구할 수 없다. 새 영상 연결 이력은 journal과 독립 파일에 남는다.
