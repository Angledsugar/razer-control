#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/razer-control-daemon"
CONFIG_FILE="/etc/razer-control-daemon.json"
SERVICE_FILE="/etc/systemd/system/razer-control-daemon.service"
UDEV_RULE="/etc/udev/rules.d/99-razer-hidraw.rules"

# Old service names to clean up
OLD_SERVICE="razer-fan-daemon"
OLD_INSTALL_DIR="/opt/razer-fan-daemon"
OLD_CONFIG="/etc/razer-fan-daemon.json"

echo "=== Razer Control Daemon Installer ==="

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo ./install.sh)"
    exit 1
fi

# Stop and disable old service if exists
if systemctl is-active --quiet "$OLD_SERVICE" 2>/dev/null; then
    echo "[0/5] Stopping old $OLD_SERVICE service..."
    systemctl stop "$OLD_SERVICE"
    systemctl disable "$OLD_SERVICE"
    rm -f "/etc/systemd/system/${OLD_SERVICE}.service"
    systemctl daemon-reload
fi

# Install daemon script
echo "[1/5] Installing daemon script to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/razer_control_daemon.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/razer_control_daemon.py"

# Install config (don't overwrite existing; migrate old config if available)
echo "[2/5] Installing config file..."
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$OLD_CONFIG" ]; then
        cp "$OLD_CONFIG" "$CONFIG_FILE"
        echo "  Migrated config from $OLD_CONFIG"
    else
        cp "$SCRIPT_DIR/config.json" "$CONFIG_FILE"
        echo "  Config installed to $CONFIG_FILE"
    fi
else
    echo "  Config already exists at $CONFIG_FILE (not overwriting)"
fi

# Install udev rule
echo "[3/5] Installing udev rule..."
cp "$SCRIPT_DIR/99-razer-hidraw.rules" "$UDEV_RULE"
udevadm control --reload-rules
udevadm trigger

# Install systemd service
echo "[4/5] Installing systemd service..."
cp "$SCRIPT_DIR/razer-control-daemon.service" "$SERVICE_FILE"
systemctl daemon-reload

# Enable and start
echo "[5/5] Enabling and starting service..."
systemctl enable razer-control-daemon.service
systemctl start razer-control-daemon.service

echo ""
echo "=== Installation complete! ==="
echo ""
echo "Useful commands:"
echo "  sudo systemctl status razer-control-daemon    # Check status"
echo "  sudo systemctl restart razer-control-daemon   # Restart"
echo "  sudo systemctl stop razer-control-daemon      # Stop"
echo "  sudo journalctl -u razer-control-daemon -f    # View logs"
echo "  sudo nano /etc/razer-control-daemon.json      # Edit config"
echo ""
echo "To uninstall:"
echo "  sudo ./uninstall.sh"
