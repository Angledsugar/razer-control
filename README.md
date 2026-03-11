# Razer Control Daemon

Linux에서 Razer 게이밍 노트북을 제어하는 Python 데몬 + GTK3 GUI입니다.

## Features

- **Fan Curve**: 온도 기반 팬 속도 자동 제어 (CPU/GPU 온도 모니터링)
- **Performance Mode**: CPU/GPU 성능 모드 설정
- **Battery Charge Limit**: 최대 충전량 제한 (50~80%)
- **Logo LED**: On/Off, Static/Breathing
- **Keyboard Lighting**: Wave, Reactive, Spectrum, Static, Starlight
- **Profile System**: 모든 설정을 프로파일로 통합 저장/로드

## Requirements

- Python 3.8+
- GTK3 + PyGObject (GUI)
- NVIDIA GPU: `libnvidia-ml.so.1` (GPU 온도, 선택사항)

## Quick Start

```bash
# GUI 실행
python3 razer_control_gui.py

# 데몬 설치 (systemd 서비스)
sudo ./install.sh

# 데몬 직접 실행
sudo python3 razer_control_daemon.py

# 서비스 관리
sudo systemctl status razer-control-daemon
sudo systemctl restart razer-control-daemon
sudo journalctl -u razer-control-daemon -f

# 제거
sudo ./uninstall.sh
```

## Files

| 파일 | 설명 |
|---|---|
| `razer_control_gui.py` | GTK3 GUI (프로파일 편집, 실시간 온도 표시) |
| `razer_control_daemon.py` | 백그라운드 데몬 (팬 제어 + 부팅 시 프로파일 적용) |
| `config.json` | 기본 설정 파일 |
| `install.sh` / `uninstall.sh` | systemd 서비스 설치/제거 |
| `99-razer-hidraw.rules` | udev 규칙 (HID 디바이스 권한) |
| `razer-control-daemon.service` | systemd 서비스 파일 |

## Tested Environment

| 항목 | 사양 |
|---|---|
| **Model** | Razer Blade 17 (2022)|
| **CPU** | Intel Core i7-12800H (12th Gen) |
| **GPU** | NVIDIA GeForce RTX 3070 Ti Laptop GPU |
| **OS** | Ubuntu 22.04.5 LTS |
| **Kernel** | 6.8.0-101-generic |

이 사양에서만 테스트되었습니다. 다른 Razer 모델에서는 동작이 다를 수 있습니다.

## Temperature Sources

| 소스 | 방법 |
|---|---|
| CPU | `x86_pkg_temp` thermal zone (sysfs, 실시간) |
| GPU | NVML ctypes (libnvidia-ml.so.1, nvidia-smi 불필요) |

## References

이 프로젝트는 아래 프로젝트의 HID 프로토콜 분석을 참고하여 개발되었습니다.

- [Razer-Linux/RazerControl](https://github.com/Razer-Linux/RazerControl) — WebHID API 기반 Razer 노트북 제어 웹앱
