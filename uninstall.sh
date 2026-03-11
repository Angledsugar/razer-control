#!/bin/bash
set -e

echo "=== Razer Control Daemon Uninstaller ==="

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo ./uninstall.sh)"
    exit 1
fi

echo "Stopping and disabling services..."
# New service
systemctl stop razer-control-daemon.service 2>/dev/null || true
systemctl disable razer-control-daemon.service 2>/dev/null || true
# Old service (cleanup)
systemctl stop razer-fan-daemon.service 2>/dev/null || true
systemctl disable razer-fan-daemon.service 2>/dev/null || true

echo "Removing files..."
rm -f /etc/systemd/system/razer-control-daemon.service
rm -f /etc/systemd/system/razer-fan-daemon.service
rm -f /etc/udev/rules.d/99-razer-hidraw.rules
rm -rf /opt/razer-control-daemon
rm -rf /opt/razer-fan-daemon

systemctl daemon-reload
udevadm control --reload-rules

echo ""
echo "Uninstalled. Config file preserved at /etc/razer-control-daemon.json"
echo "To remove config: sudo rm /etc/razer-control-daemon.json"
echo "To remove logs: sudo rm /var/log/razer-control-daemon.log"
