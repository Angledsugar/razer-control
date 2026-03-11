#!/usr/bin/env python3
"""
Razer Control Daemon

Comprehensive Razer laptop control daemon that applies a full profile on startup
(Performance, Battery, Lighting, Fan Curve) and then continuously monitors
temperature to adjust fan speed.

Usage:
    sudo python3 razer_control_daemon.py [--config /path/to/config.json] [--once]
"""

import json
import os
import sys
import time
import signal
import fcntl
import subprocess
import logging
import argparse
from pathlib import Path

# --- Constants ---
RAZER_VENDOR_ID = 0x1532
REPORT_SIZE = 91

# Command classes
COMMAND_CLASS_BATTERY = 0x07
COMMAND_CLASS_PERFORMANCE = 0x0D
COMMAND_CLASS_LIGHTING = 0x03

# Battery commands
CMD_SET_BATTERY_LIMIT = 0x12
CMD_GET_BATTERY_LIMIT = 0x92

# Performance commands
CMD_SET_PERF_MODE = 0x02
CMD_GET_CUSTOM_PERF = 0x87
CMD_SET_CUSTOM_PERF = 0x07
CMD_GET_FAN_STATUS = 0x82
CMD_GET_FAN_SPEED = 0x81
CMD_SET_FAN_SPEED = 0x01

# Lighting commands
CMD_GET_LOGO_STATUS = 0x80
CMD_SET_LOGO_STATUS = 0x00
CMD_GET_LOGO_MODE = 0x82
CMD_SET_LOGO_MODE = 0x02
CMD_GET_KEYB_MODE = 0x8A
CMD_SET_KEYB_MODE = 0x0A

# Performance constants
PERF_ARG_CPU = 0x01
PERF_ARG_GPU = 0x02
PERF_MODE_CUSTOM = 0x04

# Lighting constants
LOGO_MODE_MAP = {"Static": 0x00, "Breathing": 0x02}
KEYB_MODE_MAP = {
    "Off": 0x00, "Wave": 0x01, "Reactive": 0x02,
    "Spectrum": 0x03, "Static": 0x04, "Starlight": 0x07,
}
PERF_LEVEL_MAP = {"Low": 0x00, "Medium": 0x01, "High": 0x02, "Boost": 0x03}
PERF_LEVEL_REV = {v: k for k, v in PERF_LEVEL_MAP.items()}

# --- Default config ---
DEFAULT_CONFIG = {
    "poll_interval_sec": 5,
    "fan_curve": [
        {"temp": 40, "rpm": 800},
        {"temp": 50, "rpm": 1500},
        {"temp": 60, "rpm": 2500},
        {"temp": 70, "rpm": 3500},
        {"temp": 80, "rpm": 4500},
        {"temp": 90, "rpm": 5400},
    ],
    "min_rpm": 800,
    "max_rpm": 5400,
    "temp_sources": [
        {"name": "CPU", "type": "thermal_zone", "zone_name": "x86_pkg_temp"},
        {"name": "GPU", "type": "nvml", "gpu_index": 0},
    ],
    "log_level": "INFO",
    "log_file": "/var/log/razer-control-daemon.log",
    "hysteresis_deg": 3,
}

# --- Globals ---
running = True
logger = logging.getLogger("razer-control-daemon")


def signal_handler(signum, frame):
    global running
    logger.info("Received signal %d, shutting down...", signum)
    running = False


# ==============================================================================
# HID Communication
# ==============================================================================

def build_packet(cmd_class, cmd_id, data_size, args=None):
    """Build a 91-byte Razer HID packet."""
    pkt = bytearray(REPORT_SIZE)
    pkt[0] = 0x00  # report ID
    pkt[2] = 0x1F  # protocol version
    pkt[6] = data_size
    pkt[7] = cmd_class
    pkt[8] = cmd_id
    if args:
        for i, val in enumerate(args):
            pkt[9 + i] = val
    return bytes(pkt)


def send_recv(fd, packet):
    """Send a feature report and read the response via ioctl."""
    HIDIOCSFEATURE = lambda size: 0xC0004806 | (size << 16)
    HIDIOCGFEATURE = lambda size: 0xC0004807 | (size << 16)
    report = bytearray(packet)
    try:
        fcntl.ioctl(fd, HIDIOCSFEATURE(len(report)), report)
        time.sleep(0.05)
        recv_buf = bytearray(REPORT_SIZE)
        recv_buf[0] = 0x00
        fcntl.ioctl(fd, HIDIOCGFEATURE(len(recv_buf)), recv_buf)
        return bytes(recv_buf)
    except OSError as e:
        logger.error("HID ioctl error: %s", e)
        return None


def find_razer_device():
    """Find and open the Razer HID device."""
    for entry in sorted(Path("/sys/class/hidraw").iterdir()):
        uevent_path = entry / "device" / "uevent"
        if not uevent_path.exists():
            continue
        uevent = uevent_path.read_text()
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                parts = line.split("=")[1].split(":")
                vendor = int(parts[1], 16)
                if vendor == RAZER_VENDOR_ID:
                    dev_path = f"/dev/{entry.name}"
                    try:
                        fd = os.open(dev_path, os.O_RDWR)
                        pkt = build_packet(COMMAND_CLASS_PERFORMANCE, CMD_GET_FAN_STATUS,
                                           0x04, [0x00, PERF_ARG_CPU, 0x00, 0x00])
                        resp = send_recv(fd, pkt)
                        if resp and len(resp) == REPORT_SIZE:
                            logger.info("Connected to %s", dev_path)
                            return fd, dev_path
                        os.close(fd)
                    except OSError as e:
                        logger.debug("Cannot open %s: %s", dev_path, e)
    return None, None


# ==============================================================================
# Device Control Functions
# ==============================================================================

def set_performance(fd, device, level):
    """Set CPU/GPU performance level."""
    arg = PERF_ARG_GPU if device == "GPU" else PERF_ARG_CPU
    level_byte = PERF_LEVEL_MAP.get(level, 0x01)
    # Force custom power mode first
    send_recv(fd, build_packet(COMMAND_CLASS_PERFORMANCE, CMD_SET_PERF_MODE, 0x04,
                               [0x00, PERF_ARG_CPU, PERF_MODE_CUSTOM, 0x00]))
    time.sleep(0.05)
    resp = send_recv(fd, build_packet(COMMAND_CLASS_PERFORMANCE, CMD_SET_CUSTOM_PERF, 0x03,
                                      [0x00, arg, level_byte]))
    if resp:
        result = PERF_LEVEL_REV.get(resp[11], level)
        logger.info("%s performance set to %s", device, result)
        return result
    return level


def set_battery_limit(fd, enabled, level):
    """Set battery charge limit."""
    byte_val = level | (0x80 if enabled else 0x00)
    resp = send_recv(fd, build_packet(COMMAND_CLASS_BATTERY, CMD_SET_BATTERY_LIMIT, 0x01, [byte_val]))
    if resp:
        r_enabled = bool(resp[9] & 0x80)
        r_level = resp[9] & 0x7F
        logger.info("Battery limit: %s at %d%%", "enabled" if r_enabled else "disabled", r_level)
        return r_enabled, r_level
    return None, None


def set_logo(fd, enabled, mode="Static"):
    """Set logo LED status and mode."""
    send_recv(fd, build_packet(COMMAND_CLASS_LIGHTING, CMD_SET_LOGO_STATUS, 0x03,
                               [0x01, 0x04, 0x01 if enabled else 0x00]))
    time.sleep(0.05)
    if enabled:
        mode_byte = LOGO_MODE_MAP.get(mode, 0x00)
        send_recv(fd, build_packet(COMMAND_CLASS_LIGHTING, CMD_SET_LOGO_MODE, 0x03,
                                   [0x01, 0x04, mode_byte]))
    logger.info("Logo LED: %s (mode=%s)", "ON" if enabled else "OFF", mode)


def set_keyboard(fd, mode, speed=1, rgb=None, direction="Left"):
    """Set keyboard lighting mode."""
    if rgb is None:
        rgb = [255, 255, 255]
    mode_byte = KEYB_MODE_MAP.get(mode, 0x00)
    args = bytearray(80)
    args[0] = mode_byte
    if mode == "Wave":
        args[1] = 0x01 if direction == "Left" else 0x02
    elif mode in ("Reactive", "Static", "Starlight"):
        args[1] = speed
        args[2] = rgb[0]
        args[3] = rgb[1]
        args[4] = rgb[2]
    elif mode == "Spectrum":
        pass  # no extra args
    send_recv(fd, build_packet(COMMAND_CLASS_LIGHTING, CMD_SET_KEYB_MODE, 0x50, list(args)))
    logger.info("Keyboard: mode=%s, speed=%d, rgb=%s, dir=%s", mode, speed, rgb, direction)


def enable_custom_fan(fd):
    """Enable custom fan RPM mode."""
    resp = send_recv(fd, build_packet(COMMAND_CLASS_PERFORMANCE, CMD_SET_PERF_MODE, 0x04,
                                      [0x00, PERF_ARG_CPU, PERF_MODE_CUSTOM, 0x01]))
    if resp:
        logger.info("Custom fan mode enabled")
    return resp


def disable_custom_fan(fd):
    """Disable custom fan RPM mode (return to auto)."""
    resp = send_recv(fd, build_packet(COMMAND_CLASS_PERFORMANCE, CMD_SET_PERF_MODE, 0x04,
                                      [0x00, PERF_ARG_CPU, PERF_MODE_CUSTOM, 0x00]))
    if resp:
        logger.info("Custom fan mode disabled (auto mode restored)")
    return resp


def set_fan_speed(fd, device_type, rpm_unit):
    """Set fan speed. rpm_unit = RPM / 100 (e.g., 25 = 2500 RPM)."""
    arg = PERF_ARG_GPU if device_type == "GPU" else PERF_ARG_CPU
    return send_recv(fd, build_packet(COMMAND_CLASS_PERFORMANCE, CMD_SET_FAN_SPEED, 0x03,
                                      [0x00, arg, rpm_unit]))


# ==============================================================================
# Temperature Reading
# ==============================================================================

# --- NVML (NVIDIA Management Library) via ctypes ---
_nvml_lib = None
_nvml_initialized = False
_nvml_handles = {}  # gpu_index -> handle


def nvml_init():
    """Initialize NVML library. Returns True if successful."""
    global _nvml_lib, _nvml_initialized
    if _nvml_initialized:
        return True
    try:
        import ctypes
        _nvml_lib = ctypes.CDLL("libnvidia-ml.so.1")
        ret = _nvml_lib.nvmlInit_v2()
        if ret == 0:
            _nvml_initialized = True
            logger.info("NVML initialized successfully")
            return True
        else:
            logger.warning("nvmlInit_v2 returned %d", ret)
    except OSError as e:
        logger.warning("NVML not available (libnvidia-ml.so.1): %s", e)
    return False


def nvml_shutdown():
    """Shutdown NVML library."""
    global _nvml_initialized, _nvml_handles
    if _nvml_initialized and _nvml_lib:
        _nvml_lib.nvmlShutdown()
        _nvml_initialized = False
        _nvml_handles.clear()
        logger.info("NVML shut down")


def nvml_get_temp(gpu_index=0):
    """Read GPU temperature via NVML. Returns float or None."""
    import ctypes
    if not _nvml_initialized:
        return None
    if gpu_index not in _nvml_handles:
        handle = ctypes.c_void_p()
        ret = _nvml_lib.nvmlDeviceGetHandleByIndex_v2(gpu_index, ctypes.byref(handle))
        if ret != 0:
            logger.error("nvmlDeviceGetHandleByIndex(%d) returned %d", gpu_index, ret)
            return None
        _nvml_handles[gpu_index] = handle
    temp = ctypes.c_uint()
    ret = _nvml_lib.nvmlDeviceGetTemperature(_nvml_handles[gpu_index], 0, ctypes.byref(temp))
    if ret == 0:
        return float(temp.value)
    logger.error("nvmlDeviceGetTemperature returned %d", ret)
    return None


def find_thermal_zone_path(zone_name):
    """Find thermal zone sysfs path by type name."""
    thermal_base = Path("/sys/class/thermal")
    for zone in sorted(thermal_base.iterdir()):
        type_file = zone / "type"
        if type_file.exists() and type_file.read_text().strip() == zone_name:
            temp_path = str(zone / "temp")
            logger.info("Found thermal zone '%s' -> %s", zone_name, temp_path)
            return temp_path
    logger.warning("Thermal zone '%s' not found", zone_name)
    return None


def resolve_temp_sources(sources_config):
    """Resolve temp source configs to actual paths."""
    resolved = []
    for src in sources_config:
        name = src["name"]
        src_type = src.get("type", "thermal_zone")
        if src_type == "thermal_zone":
            path = find_thermal_zone_path(src.get("zone_name", name))
            if path:
                resolved.append({"name": name, "path": path, "type": "sysfs"})
        elif src_type == "sysfs_path":
            path = src.get("path")
            if path and os.path.exists(path):
                resolved.append({"name": name, "path": path, "type": "sysfs"})
        elif src_type == "nvml":
            gpu_index = src.get("gpu_index", 0)
            if nvml_init():
                resolved.append({"name": name, "type": "nvml", "gpu_index": gpu_index})
            else:
                logger.warning("NVML unavailable, falling back to nvidia-smi for '%s'", name)
                resolved.append({"name": name, "type": "nvidia-smi"})
        elif src_type == "nvidia-smi":
            resolved.append({"name": name, "type": "nvidia-smi"})
    return resolved


def read_temp(source):
    """Read temperature from a resolved source."""
    if source["type"] == "sysfs":
        try:
            with open(source["path"], "r") as f:
                return int(f.read().strip()) / 1000.0
        except (IOError, ValueError) as e:
            logger.error("Failed to read temp from %s: %s", source["path"], e)
    elif source["type"] == "nvml":
        return nvml_get_temp(source.get("gpu_index", 0))
    elif source["type"] == "nvidia-smi":
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
    return None


# ==============================================================================
# Fan Curve
# ==============================================================================

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


# ==============================================================================
# Profile Application
# ==============================================================================

def apply_profile(fd, config):
    """Apply all non-fan settings from profile to device on startup."""
    applied = []

    # Performance
    cpu_perf = config.get("cpu_perf")
    if cpu_perf:
        set_performance(fd, "CPU", cpu_perf)
        time.sleep(0.05)
        applied.append(f"CPU={cpu_perf}")

    gpu_perf = config.get("gpu_perf")
    if gpu_perf:
        set_performance(fd, "GPU", gpu_perf)
        time.sleep(0.05)
        applied.append(f"GPU={gpu_perf}")

    # Battery
    if "battery_enabled" in config:
        enabled = config["battery_enabled"]
        level = config.get("battery_level", 60)
        set_battery_limit(fd, enabled, level)
        time.sleep(0.05)
        applied.append(f"Battery={'ON' if enabled else 'OFF'}@{level}%")

    # Logo LED
    if "logo_enabled" in config:
        logo_on = config["logo_enabled"]
        logo_mode = config.get("logo_mode", "Static")
        set_logo(fd, logo_on, logo_mode)
        time.sleep(0.05)
        applied.append(f"Logo={'ON' if logo_on else 'OFF'}")

    # Keyboard
    if "keyb_mode" in config:
        keyb_mode = config["keyb_mode"]
        set_keyboard(fd, keyb_mode,
                     speed=config.get("keyb_speed", 1),
                     rgb=config.get("keyb_rgb", [255, 255, 255]),
                     direction=config.get("keyb_direction", "Left"))
        time.sleep(0.05)
        applied.append(f"Keyboard={keyb_mode}")

    if applied:
        logger.info("Profile applied: %s", ", ".join(applied))
    else:
        logger.info("No profile device settings to apply (fan-only config)")


# ==============================================================================
# Config & Logging
# ==============================================================================

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
    log_file = config.get("log_file", "/var/log/razer-control-daemon.log")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(log_level)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except PermissionError:
        logger.warning("Cannot write to %s, logging to stderr only", log_file)


# ==============================================================================
# Main
# ==============================================================================

def main():
    global running

    parser = argparse.ArgumentParser(description="Razer Control Daemon")
    parser.add_argument("--config", "-c", default="/etc/razer-control-daemon.json",
                        help="Path to config file")
    parser.add_argument("--once", action="store_true",
                        help="Apply profile once and exit (no fan loop)")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("=== Razer Control Daemon starting ===")

    # Resolve temperature sources
    temp_sources = resolve_temp_sources(config["temp_sources"])
    if not temp_sources:
        logger.error("No valid temperature sources found!")
        sys.exit(1)
    logger.info("Monitoring %d temp source(s): %s",
                len(temp_sources), ", ".join(s["name"] for s in temp_sources))

    # Find device
    fd, dev_path = find_razer_device()
    if fd is None:
        logger.error("Could not connect to any Razer device!")
        sys.exit(1)

    # === Apply full profile (Performance, Battery, Lighting) ===
    apply_profile(fd, config)

    if args.once:
        logger.info("--once mode: profile applied, exiting.")
        os.close(fd)
        return

    # === Enable custom fan mode and start fan control loop ===
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
            readings = {}
            for src in temp_sources:
                temp = read_temp(src)
                if temp is not None:
                    readings[src["name"]] = temp

            if not readings:
                logger.warning("Cannot read any temperature, skipping cycle")
                time.sleep(poll_interval)
                continue

            max_temp = max(readings.values())

            if abs(max_temp - last_effective_temp) < hysteresis and last_rpm > 0:
                time.sleep(poll_interval)
                continue

            last_effective_temp = max_temp
            target_rpm = interpolate_rpm(max_temp, fan_curve, min_rpm, max_rpm)
            target_rpm_unit = max(1, min(54, round(target_rpm / 100)))

            if target_rpm_unit * 100 != last_rpm:
                temp_str = " | ".join(f"{n}: {t:.1f}\u00b0C" for n, t in readings.items())
                logger.info("%s | Max: %.1f\u00b0C -> Fan: %d RPM",
                            temp_str, max_temp, target_rpm_unit * 100)
                set_fan_speed(fd, "CPU", target_rpm_unit)
                time.sleep(0.05)
                set_fan_speed(fd, "GPU", target_rpm_unit)
                last_rpm = target_rpm_unit * 100

            time.sleep(poll_interval)

    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
    finally:
        logger.info("Restoring auto fan control...")
        try:
            disable_custom_fan(fd)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        nvml_shutdown()
        logger.info("Daemon stopped.")


if __name__ == "__main__":
    main()
