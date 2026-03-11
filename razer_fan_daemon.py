#!/usr/bin/env python3
"""
Razer Blade Fan Control Daemon

Automatically controls fan speed based on CPU/GPU temperature.
Communicates with Razer HID device via /dev/hidraw*.

Usage:
    sudo python3 razer_fan_daemon.py [--config /path/to/config.json] [--once]
"""

import json
import os
import sys
import time
import signal
import struct
import subprocess
import logging
import argparse
from pathlib import Path

# --- Constants (from RazerPacket.js) ---
RAZER_VENDOR_ID = 0x1532
REPORT_SIZE = 91

COMMAND_CLASS_PERFORMANCE = 0x0D
COMMAND_ID_SET_PERFORMANCE_MODE = 0x02
COMMAND_ID_GET_FAN_STATUS = 0x82
COMMAND_ID_GET_FAN_SPEED = 0x81
COMMAND_ID_SET_FAN_SPEED = 0x01

PERFORMANCE_ARG_CPU = 0x01
PERFORMANCE_ARG_GPU = 0x02
PERFORMANCE_MODE_CUSTOM = 0x04

DATA_SIZE_3 = 0x03
DATA_SIZE_4 = 0x04

# --- Default config ---
DEFAULT_CONFIG = {
    "poll_interval_sec": 5,
    "fan_curve": [
        {"temp": 40, "rpm": 800},
        {"temp": 50, "rpm": 1500},
        {"temp": 60, "rpm": 2500},
        {"temp": 70, "rpm": 3500},
        {"temp": 80, "rpm": 4500},
        {"temp": 90, "rpm": 5400}
    ],
    "min_rpm": 800,
    "max_rpm": 5400,
    "temp_sources": [
        {"name": "SEN1", "type": "thermal_zone", "zone_name": "SEN1"},
        {"name": "SEN2", "type": "thermal_zone", "zone_name": "SEN2"}
    ],
    "log_level": "INFO",
    "log_file": "/var/log/razer-fan-daemon.log",
    "hysteresis_deg": 3
}

# --- Globals ---
running = True
logger = logging.getLogger("razer-fan-daemon")


def signal_handler(signum, frame):
    global running
    logger.info("Received signal %d, shutting down...", signum)
    running = False


def build_packet(command_class, command_id, data_size, args=None):
    """Build a 91-byte Razer HID packet."""
    pkt = bytearray(REPORT_SIZE)
    pkt[0] = 0x00  # report ID
    pkt[1] = 0x00
    pkt[2] = 0x1F  # protocol version
    pkt[6] = data_size
    pkt[7] = command_class
    pkt[8] = command_id
    if args:
        for i, val in enumerate(args):
            pkt[9 + i] = val
    return bytes(pkt)


def send_feature_report(fd, packet):
    """Send a feature report and read the response via ioctl."""
    import fcntl

    # HID_SET_FEATURE = HIDIOCSFEATURE(size)
    # HID_GET_FEATURE = HIDIOCGFEATURE(size)
    # These are ioctl codes for hidraw
    HIDIOCSFEATURE = lambda size: 0xC0004806 | (size << 16)
    HIDIOCGFEATURE = lambda size: 0xC0004807 | (size << 16)

    report = bytearray(packet)

    try:
        # Send feature report
        fcntl.ioctl(fd, HIDIOCSFEATURE(len(report)), report)
        time.sleep(0.05)  # small delay for device to process

        # Receive feature report
        recv_buf = bytearray(REPORT_SIZE)
        recv_buf[0] = 0x00  # report ID
        fcntl.ioctl(fd, HIDIOCGFEATURE(len(recv_buf)), recv_buf)
        return bytes(recv_buf)
    except OSError as e:
        logger.error("HID ioctl error: %s", e)
        return None


def find_razer_hidraw():
    """Find the correct hidraw device for the Razer laptop."""
    hidraw_devices = []

    for entry in sorted(Path("/sys/class/hidraw").iterdir()):
        uevent_path = entry / "device" / "uevent"
        if not uevent_path.exists():
            continue

        uevent = uevent_path.read_text()
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                # Format: HID_ID=BUS:VENDOR:PRODUCT
                parts = line.split("=")[1].split(":")
                vendor = int(parts[1], 16)
                if vendor == RAZER_VENDOR_ID:
                    hidraw_name = entry.name
                    hidraw_devices.append(f"/dev/{hidraw_name}")
                    break

    logger.info("Found Razer hidraw devices: %s", hidraw_devices)
    return hidraw_devices


def find_working_device(hidraw_devices):
    """Try each hidraw device and return the first one that responds to fan queries."""
    for dev_path in hidraw_devices:
        try:
            fd = os.open(dev_path, os.O_RDWR)
            # Test: try to get fan status
            pkt = build_packet(
                COMMAND_CLASS_PERFORMANCE,
                COMMAND_ID_GET_FAN_STATUS,
                DATA_SIZE_4,
                args=[0x00, PERFORMANCE_ARG_CPU, 0x00, 0x00]
            )
            resp = send_feature_report(fd, pkt)
            if resp and len(resp) == REPORT_SIZE:
                logger.info("Working device found: %s", dev_path)
                return fd, dev_path
            os.close(fd)
        except OSError as e:
            logger.debug("Cannot open %s: %s", dev_path, e)
            continue

    return None, None


def enable_custom_fan(fd):
    """Enable custom fan RPM mode."""
    pkt = build_packet(
        COMMAND_CLASS_PERFORMANCE,
        COMMAND_ID_SET_PERFORMANCE_MODE,
        DATA_SIZE_4,
        args=[0x00, PERFORMANCE_ARG_CPU, PERFORMANCE_MODE_CUSTOM, 0x01]
    )
    resp = send_feature_report(fd, pkt)
    if resp:
        logger.info("Custom fan mode enabled (response byte 11: %d)", resp[11])
    return resp


def disable_custom_fan(fd):
    """Disable custom fan RPM mode (return to auto)."""
    pkt = build_packet(
        COMMAND_CLASS_PERFORMANCE,
        COMMAND_ID_SET_PERFORMANCE_MODE,
        DATA_SIZE_4,
        args=[0x00, PERFORMANCE_ARG_CPU, PERFORMANCE_MODE_CUSTOM, 0x00]
    )
    resp = send_feature_report(fd, pkt)
    if resp:
        logger.info("Custom fan mode disabled (auto mode restored)")
    return resp


def set_fan_speed(fd, device_type, rpm_value):
    """Set fan speed. rpm_value is in units of 100 RPM (e.g., 25 = 2500 RPM)."""
    arg = PERFORMANCE_ARG_GPU if device_type == "GPU" else PERFORMANCE_ARG_CPU
    pkt = build_packet(
        COMMAND_CLASS_PERFORMANCE,
        COMMAND_ID_SET_FAN_SPEED,
        DATA_SIZE_3,
        args=[0x00, arg, rpm_value]
    )
    return send_feature_report(fd, pkt)


def get_fan_speed(fd):
    """Get current CPU fan speed."""
    pkt = build_packet(
        COMMAND_CLASS_PERFORMANCE,
        COMMAND_ID_GET_FAN_SPEED,
        DATA_SIZE_3,
        args=[0x00, PERFORMANCE_ARG_CPU, 0x00]
    )
    resp = send_feature_report(fd, pkt)
    if resp:
        return resp[10] * 100
    return None


def find_thermal_zone_path(zone_name):
    """Find thermal zone sysfs path by its type name (e.g., 'SEN1', 'TCPU')."""
    thermal_base = Path("/sys/class/thermal")

    for zone in sorted(thermal_base.iterdir()):
        type_file = zone / "type"
        if type_file.exists():
            zone_type = type_file.read_text().strip()
            if zone_type == zone_name:
                temp_path = str(zone / "temp")
                logger.info("Found thermal zone '%s' -> %s", zone_name, temp_path)
                return temp_path

    logger.warning("Thermal zone '%s' not found", zone_name)
    return None


def read_sysfs_temp(thermal_path):
    """Read temperature in Celsius from a sysfs thermal zone."""
    try:
        with open(thermal_path, "r") as f:
            return int(f.read().strip()) / 1000.0
    except (IOError, ValueError) as e:
        logger.error("Failed to read temp from %s: %s", thermal_path, e)
        return None


def resolve_temp_sources(sources_config):
    """Resolve temp source configs to a list of {name, path} dicts."""
    resolved = []
    for src in sources_config:
        name = src["name"]
        src_type = src.get("type", "thermal_zone")

        if src_type == "thermal_zone":
            zone_name = src.get("zone_name", name)
            path = find_thermal_zone_path(zone_name)
            if path:
                resolved.append({"name": name, "path": path, "type": "sysfs"})
        elif src_type == "sysfs_path":
            path = src.get("path")
            if path and os.path.exists(path):
                resolved.append({"name": name, "path": path, "type": "sysfs"})
                logger.info("Using direct sysfs path for '%s': %s", name, path)
            else:
                logger.warning("sysfs path not found for '%s': %s", name, path)
        elif src_type == "nvidia-smi":
            resolved.append({"name": name, "path": None, "type": "nvidia-smi"})
            logger.info("Using nvidia-smi for '%s'", name)

    return resolved


def read_temp(source):
    """Read temperature from a resolved source."""
    if source["type"] == "sysfs":
        return read_sysfs_temp(source["path"])
    elif source["type"] == "nvidia-smi":
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
            logger.debug("nvidia-smi failed: %s", e)
    return None


def interpolate_rpm(temp, fan_curve, min_rpm, max_rpm):
    """Interpolate fan RPM from the fan curve."""
    if temp <= fan_curve[0]["temp"]:
        return min_rpm

    if temp >= fan_curve[-1]["temp"]:
        return max_rpm

    for i in range(len(fan_curve) - 1):
        t0, r0 = fan_curve[i]["temp"], fan_curve[i]["rpm"]
        t1, r1 = fan_curve[i + 1]["temp"], fan_curve[i + 1]["rpm"]
        if t0 <= temp <= t1:
            ratio = (temp - t0) / (t1 - t0)
            rpm = r0 + ratio * (r1 - r0)
            return max(min_rpm, min(max_rpm, int(rpm)))

    return max_rpm


def load_config(config_path):
    """Load config from JSON file, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)

    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                user_config = json.load(f)
            config.update(user_config)
            logger.info("Loaded config from %s", config_path)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load config %s: %s, using defaults", config_path, e)
    else:
        logger.info("No config file found, using defaults")

    return config


def setup_logging(config):
    """Setup logging to file and stderr."""
    log_level = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    log_file = config.get("log_file", "/var/log/razer-fan-daemon.log")

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    logger.setLevel(log_level)

    # Console handler (for systemd journal)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # File handler
    try:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except PermissionError:
        logger.warning("Cannot write to %s, logging to stderr only", log_file)


def main():
    global running

    parser = argparse.ArgumentParser(description="Razer Blade Fan Control Daemon")
    parser.add_argument("--config", "-c", default="/etc/razer-fan-daemon.json",
                        help="Path to config file (default: /etc/razer-fan-daemon.json)")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit (for testing)")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("=== Razer Fan Control Daemon starting ===")

    # Resolve temperature sources
    temp_sources = resolve_temp_sources(config["temp_sources"])
    if not temp_sources:
        logger.error("No valid temperature sources found!")
        sys.exit(1)
    logger.info("Monitoring %d temp source(s): %s",
                len(temp_sources), ", ".join(s["name"] for s in temp_sources))

    # Find and open Razer HID device
    hidraw_devices = find_razer_hidraw()
    if not hidraw_devices:
        logger.error("No Razer HID devices found!")
        sys.exit(1)

    fd, dev_path = find_working_device(hidraw_devices)
    if fd is None:
        logger.error("Could not connect to any Razer device!")
        sys.exit(1)

    logger.info("Connected to %s", dev_path)

    # Enable custom fan mode
    enable_custom_fan(fd)

    fan_curve = config["fan_curve"]
    min_rpm = config["min_rpm"]
    max_rpm = config["max_rpm"]
    hysteresis = config["hysteresis_deg"]
    poll_interval = config["poll_interval_sec"]

    last_rpm = 0
    last_effective_temp = 0

    try:
        while running:
            # Read all temperature sources
            readings = {}
            for src in temp_sources:
                temp = read_temp(src)
                if temp is not None:
                    readings[src["name"]] = temp

            if not readings:
                logger.warning("Cannot read any temperature, skipping cycle")
                time.sleep(poll_interval)
                continue

            # Use the highest temperature across all sources
            max_temp = max(readings.values())

            # Apply hysteresis: only change if temp moved beyond hysteresis threshold
            if abs(max_temp - last_effective_temp) < hysteresis and last_rpm > 0:
                time.sleep(poll_interval)
                continue

            last_effective_temp = max_temp
            target_rpm = interpolate_rpm(max_temp, fan_curve, min_rpm, max_rpm)

            # Round to nearest 100
            target_rpm_unit = round(target_rpm / 100)
            target_rpm_unit = max(1, min(54, target_rpm_unit))  # clamp 100-5400

            if target_rpm_unit * 100 != last_rpm:
                temp_str = " | ".join(
                    f"{name}: {temp:.1f}°C" for name, temp in readings.items()
                )
                logger.info(
                    "%s | Max: %.1f°C -> Fan: %d RPM",
                    temp_str,
                    max_temp,
                    target_rpm_unit * 100
                )
                set_fan_speed(fd, "CPU", target_rpm_unit)
                time.sleep(0.05)
                set_fan_speed(fd, "GPU", target_rpm_unit)
                last_rpm = target_rpm_unit * 100

            if args.once:
                break

            time.sleep(poll_interval)

    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
    finally:
        # Restore auto fan control on exit
        logger.info("Restoring auto fan control...")
        try:
            disable_custom_fan(fd)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        logger.info("Daemon stopped.")


if __name__ == "__main__":
    main()
