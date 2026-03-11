#!/usr/bin/env python3
"""
Razer Control GUI

GTK3 + Cairo based Razer laptop control panel.
Features: Fan Curve, Performance Mode, Battery Limit, Logo LED, Keyboard Lighting.
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

import cairo
import json
import os
import fcntl
import subprocess
import signal
import sys
import time
import tempfile
from pathlib import Path

# --- HID Constants ---
RAZER_VENDOR_ID = 0x1532
REPORT_SIZE = 91

COMMAND_CLASS_BATTERY = 0x07
COMMAND_CLASS_PERFORMANCE = 0x0D
COMMAND_CLASS_LIGHTING = 0x03

CMD_GET_BATTERY_LIMIT = 0x92
CMD_SET_BATTERY_LIMIT = 0x12
CMD_SET_PERF_MODE = 0x02
CMD_GET_CUSTOM_PERF = 0x87
CMD_SET_CUSTOM_PERF = 0x07
CMD_GET_FAN_STATUS = 0x82
CMD_GET_FAN_SPEED = 0x81
CMD_SET_FAN_SPEED = 0x01
CMD_GET_LOGO_STATUS = 0x80
CMD_SET_LOGO_STATUS = 0x00
CMD_GET_LOGO_MODE = 0x82
CMD_SET_LOGO_MODE = 0x02
CMD_GET_KEYB_MODE = 0x8A
CMD_SET_KEYB_MODE = 0x0A

PERF_ARG_CPU = 0x01
PERF_ARG_GPU = 0x02
PERF_MODE_CUSTOM = 0x04

# --- App Constants ---
DAEMON_CONFIG_PATH = "/etc/razer-control-daemon.json"
PROFILES_DIR = os.path.join(str(Path.home()), ".config", "razer-control-daemon", "profiles")
DAEMON_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "razer_control_daemon.py")
DAEMON_PID_FILE = "/tmp/razer-control-daemon.pid"

THERMAL_BASE = Path("/sys/class/thermal")
TEMP_MIN, TEMP_MAX = 30, 100
RPM_MIN, RPM_MAX, RPM_STEP = 0, 5400, 100
GRAPH_PAD_L, GRAPH_PAD_R, GRAPH_PAD_T, GRAPH_PAD_B = 60, 30, 30, 50
POINT_RADIUS = 8

PRESETS = {
    "Silent": [
        {"temp": 40, "rpm": 0}, {"temp": 55, "rpm": 800}, {"temp": 65, "rpm": 1200},
        {"temp": 75, "rpm": 2000}, {"temp": 85, "rpm": 3000}, {"temp": 95, "rpm": 4000},
    ],
    "Balanced": [
        {"temp": 40, "rpm": 800}, {"temp": 50, "rpm": 1500}, {"temp": 60, "rpm": 2500},
        {"temp": 70, "rpm": 3500}, {"temp": 80, "rpm": 4500}, {"temp": 90, "rpm": 5400},
    ],
    "Performance": [
        {"temp": 35, "rpm": 1500}, {"temp": 45, "rpm": 2500}, {"temp": 55, "rpm": 3500},
        {"temp": 65, "rpm": 4500}, {"temp": 75, "rpm": 5000}, {"temp": 85, "rpm": 5400},
    ],
    "Max": [{"temp": 30, "rpm": 5400}, {"temp": 100, "rpm": 5400}],
}

KEYB_MODE_ARGS = {
    "Off": 0x00, "Wave": 0x01, "Reactive": 0x02,
    "Spectrum": 0x04, "Static": 0x06, "Starlight": 0x19,
}
KEYB_ARG_TO_MODE = {
    0x00: "Off", 0x01: "Wave", 0x02: "Reactive",
    0x04: "Spectrum", 0x06: "Static", 0x07: "Starlight", 0x19: "Starlight",
}
KEYB_MODE_FEATURES = {
    "Off": {"Direction": False, "Speed": False, "RGB": False},
    "Wave": {"Direction": True, "Speed": False, "RGB": False},
    "Reactive": {"Direction": False, "Speed": True, "RGB": True},
    "Spectrum": {"Direction": False, "Speed": False, "RGB": False},
    "Static": {"Direction": False, "Speed": False, "RGB": True},
    "Starlight": {"Direction": False, "Speed": True, "RGB": True},
}


# ==============================================================================
# HID Communication Layer
# ==============================================================================

class RazerHID:
    """Direct HID communication with Razer device via /dev/hidraw*."""

    def __init__(self):
        self.fd = None
        self.dev_path = None

    @property
    def connected(self):
        return self.fd is not None

    def find_and_connect(self):
        """Find and connect to the Razer device."""
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
                            # Test connection
                            pkt = self._build_packet(COMMAND_CLASS_PERFORMANCE, CMD_GET_FAN_STATUS, 0x04,
                                                     [0x00, PERF_ARG_CPU, 0x00, 0x00])
                            resp = self._send_recv(fd, pkt)
                            if resp and len(resp) == REPORT_SIZE:
                                self.fd = fd
                                self.dev_path = dev_path
                                return True
                            os.close(fd)
                        except OSError:
                            continue
        return False

    def disconnect(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            self.dev_path = None

    def _build_packet(self, cmd_class, cmd_id, data_size, args=None):
        pkt = bytearray(REPORT_SIZE)
        pkt[0] = 0x00
        pkt[2] = 0x1F
        pkt[6] = data_size
        pkt[7] = cmd_class
        pkt[8] = cmd_id
        if args:
            for i, val in enumerate(args):
                pkt[9 + i] = val
        return bytes(pkt)

    def _send_recv(self, fd, packet):
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
        except OSError:
            return None

    def send(self, cmd_class, cmd_id, data_size, args=None):
        if not self.connected:
            return None
        pkt = self._build_packet(cmd_class, cmd_id, data_size, args)
        return self._send_recv(self.fd, pkt)

    # --- Battery ---
    def get_battery_limit(self):
        resp = self.send(COMMAND_CLASS_BATTERY, CMD_GET_BATTERY_LIMIT, 0x01)
        if resp:
            print(f"[DEBUG] get_battery resp[6..12]: {[hex(b) for b in resp[6:13]]}")
            enabled = bool(resp[9] & 0x80)
            level = resp[9] & 0x7F
            print(f"[DEBUG] get_battery parsed: enabled={enabled}, level={level}")
            return enabled, level
        print("[DEBUG] get_battery: no response")
        return None, None

    def set_battery_limit(self, enabled, level):
        byte_val = level | (0x80 if enabled else 0x00)
        print(f"[DEBUG] set_battery: enabled={enabled}, level={level}, byte=0x{byte_val:02X}")
        resp = self.send(COMMAND_CLASS_BATTERY, CMD_SET_BATTERY_LIMIT, 0x01, [byte_val])
        if resp:
            print(f"[DEBUG] set_battery resp[6..12]: {[hex(b) for b in resp[6:13]]}")
            r_enabled = bool(resp[9] & 0x80)
            r_level = resp[9] & 0x7F
            print(f"[DEBUG] set_battery parsed: enabled={r_enabled}, level={r_level}")
            return r_enabled, r_level
        print("[DEBUG] set_battery: no response")
        return None, None

    # --- Performance ---
    def get_performance(self, device):
        arg = PERF_ARG_GPU if device == "GPU" else PERF_ARG_CPU
        resp = self.send(COMMAND_CLASS_PERFORMANCE, CMD_GET_CUSTOM_PERF, 0x03, [0x00, arg, 0x00])
        if resp:
            level_map = {0x00: "Low", 0x01: "Medium", 0x02: "High", 0x03: "Boost"}
            return level_map.get(resp[11], "Medium")
        return "Medium"

    def set_performance(self, device, level):
        level_map = {"Low": 0x00, "Medium": 0x01, "High": 0x02, "Boost": 0x03}
        arg = PERF_ARG_GPU if device == "GPU" else PERF_ARG_CPU
        # Force custom power mode first
        self.send(COMMAND_CLASS_PERFORMANCE, CMD_SET_PERF_MODE, 0x04,
                  [0x00, PERF_ARG_CPU, PERF_MODE_CUSTOM, 0x00])
        time.sleep(0.05)
        resp = self.send(COMMAND_CLASS_PERFORMANCE, CMD_SET_CUSTOM_PERF, 0x03,
                         [0x00, arg, level_map.get(level, 0x01)])
        if resp:
            level_map_rev = {0x00: "Low", 0x01: "Medium", 0x02: "High", 0x03: "Boost"}
            return level_map_rev.get(resp[11], level)
        return level

    # --- Fan ---
    def get_fan_speed(self):
        resp = self.send(COMMAND_CLASS_PERFORMANCE, CMD_GET_FAN_SPEED, 0x03,
                         [0x00, PERF_ARG_CPU, 0x00])
        if resp:
            return resp[11] * 100
        return 0

    # --- Logo LED ---
    def get_logo_status(self):
        resp = self.send(COMMAND_CLASS_LIGHTING, CMD_GET_LOGO_STATUS, 0x03,
                         [0x01, 0x04, 0x00])
        if resp:
            return resp[11] == 1
        return False

    def set_logo_status(self, on):
        resp = self.send(COMMAND_CLASS_LIGHTING, CMD_SET_LOGO_STATUS, 0x03,
                         [0x01, 0x04, 0x01 if on else 0x00])
        if resp:
            return resp[11] == 1
        return on

    def get_logo_mode(self):
        resp = self.send(COMMAND_CLASS_LIGHTING, CMD_GET_LOGO_MODE, 0x03,
                         [0x01, 0x04, 0x00])
        if resp:
            mode_map = {0x00: "Static", 0x02: "Breathing"}
            return mode_map.get(resp[11], "Static")
        return "Static"

    def set_logo_mode(self, mode):
        mode_map = {"Static": 0x00, "Breathing": 0x02}
        self.send(COMMAND_CLASS_LIGHTING, CMD_SET_LOGO_MODE, 0x03,
                  [0x01, 0x04, mode_map.get(mode, 0x00)])

    # --- Keyboard Lighting ---
    def get_keyboard_mode(self):
        resp = self.send(COMMAND_CLASS_LIGHTING, CMD_GET_KEYB_MODE, 0x50)
        if resp:
            mode_byte = resp[9]
            mode_name = KEYB_ARG_TO_MODE.get(mode_byte, "Off")
            speed = 1
            rgb = [255, 255, 255]
            direction = "Left"

            if mode_name == "Starlight":
                speed = resp[11]
                rgb = [resp[12], resp[13], resp[14]]
            elif mode_name == "Reactive":
                speed = resp[10]
                rgb = [resp[11], resp[12], resp[13]]
            elif mode_name == "Static":
                rgb = [resp[10], resp[11], resp[12]]
            elif mode_name == "Wave":
                direction = "Right" if resp[10] == 0x02 else "Left"

            return mode_name, speed, rgb, direction
        return "Off", 1, [255, 255, 255], "Left"

    def set_keyboard_mode(self, mode, speed=1, rgb=None, direction="Left"):
        if rgb is None:
            rgb = [255, 255, 255]

        args = [0] * 71  # pad to 80 bytes total (9 header + 71 args)
        args[0] = KEYB_MODE_ARGS.get(mode, 0x00)

        if mode == "Wave":
            args[1] = 0x01 if direction == "Left" else 0x02
        elif mode == "Reactive":
            args[1] = speed
            args[2], args[3], args[4] = rgb[0], rgb[1], rgb[2]
        elif mode == "Static":
            args[1], args[2], args[3] = rgb[0], rgb[1], rgb[2]
        elif mode == "Starlight":
            args[1] = 0x01
            args[2] = speed
            args[3], args[4], args[5] = rgb[0], rgb[1], rgb[2]

        resp = self.send(COMMAND_CLASS_LIGHTING, CMD_SET_KEYB_MODE, 0x50, args)
        if resp:
            return KEYB_ARG_TO_MODE.get(resp[9], mode)
        return mode


# ==============================================================================
# Fan Curve Graph Widget
# ==============================================================================

def find_thermal_zone_path(zone_name):
    """Find thermal zone sysfs path by type name (e.g. 'SEN1')."""
    for zone in sorted(THERMAL_BASE.iterdir()):
        type_file = zone / "type"
        if type_file.exists() and type_file.read_text().strip() == zone_name:
            return str(zone / "temp")
    return None


def read_sysfs_temp(path):
    """Read temperature in Celsius from a sysfs thermal zone."""
    try:
        with open(path, "r") as f:
            return int(f.read().strip()) / 1000.0
    except (IOError, ValueError):
        return None


# --- NVML GPU temp via ctypes (no nvidia-smi process spawn) ---
_nvml_lib = None
_nvml_ok = False
_nvml_handle = None


def nvml_init_gui(gpu_index=0):
    """Initialize NVML for GUI usage. Returns True if successful."""
    global _nvml_lib, _nvml_ok, _nvml_handle
    if _nvml_ok:
        return True
    try:
        import ctypes
        _nvml_lib = ctypes.CDLL("libnvidia-ml.so.1")
        if _nvml_lib.nvmlInit_v2() != 0:
            return False
        handle = ctypes.c_void_p()
        if _nvml_lib.nvmlDeviceGetHandleByIndex_v2(gpu_index, ctypes.byref(handle)) != 0:
            return False
        _nvml_handle = handle
        _nvml_ok = True
        return True
    except OSError:
        return False


def read_gpu_temp():
    """Read GPU temperature via NVML. Returns float or None."""
    if not _nvml_ok:
        return None
    import ctypes
    temp = ctypes.c_uint()
    if _nvml_lib.nvmlDeviceGetTemperature(_nvml_handle, 0, ctypes.byref(temp)) == 0:
        return float(temp.value)
    return None


class FanCurveGraph(Gtk.DrawingArea):
    TEMP_COLORS = {
        "CPU": (0.0, 0.75, 1.0),    # cyan
        "GPU": (0.0, 1.0, 0.4),     # green
    }

    def __init__(self):
        super().__init__()
        self.points = list(PRESETS["Balanced"])
        self.dragging_index = -1
        self.hover_index = -1
        self.current_temps = {}  # {"SEN1": 45.2, "SEN2": 52.1}
        self.set_size_request(600, 350)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("motion-notify-event", self.on_motion)
        self.on_points_changed = None

    def get_graph_rect(self):
        a = self.get_allocation()
        return GRAPH_PAD_L, GRAPH_PAD_T, a.width - GRAPH_PAD_L - GRAPH_PAD_R, a.height - GRAPH_PAD_T - GRAPH_PAD_B

    def temp_to_x(self, t):
        gx, _, gw, _ = self.get_graph_rect()
        return gx + (t - TEMP_MIN) / (TEMP_MAX - TEMP_MIN) * gw

    def rpm_to_y(self, r):
        _, gy, _, gh = self.get_graph_rect()
        return gy + gh - (r / RPM_MAX) * gh

    def x_to_temp(self, x):
        gx, _, gw, _ = self.get_graph_rect()
        return max(TEMP_MIN, min(TEMP_MAX, round(TEMP_MIN + (x - gx) / gw * (TEMP_MAX - TEMP_MIN))))

    def y_to_rpm(self, y):
        _, gy, _, gh = self.get_graph_rect()
        return max(RPM_MIN, min(RPM_MAX, round((gy + gh - y) / gh * RPM_MAX / RPM_STEP) * RPM_STEP))

    def set_temps(self, temps_dict):
        """Update current temperature readings. temps_dict = {"SEN1": 45.2, ...}"""
        self.current_temps = dict(temps_dict)
        self.queue_draw()

    def set_points(self, pts):
        self.points = sorted([dict(p) for p in pts], key=lambda p: p["temp"])
        self.queue_draw()
        if self.on_points_changed:
            self.on_points_changed(self.points)

    def on_draw(self, widget, cr):
        a = self.get_allocation()
        gx, gy, gw, gh = self.get_graph_rect()

        cr.set_source_rgb(0.12, 0.12, 0.14)
        cr.rectangle(0, 0, a.width, a.height)
        cr.fill()
        cr.set_source_rgb(0.16, 0.16, 0.19)
        cr.rectangle(gx, gy, gw, gh)
        cr.fill()
        cr.set_line_width(0.5)

        for rpm in range(0, RPM_MAX + 1, 1000):
            y = self.rpm_to_y(rpm)
            cr.set_source_rgba(0.4, 0.4, 0.4, 0.4)
            cr.move_to(gx, y); cr.line_to(gx + gw, y); cr.stroke()
            cr.set_source_rgb(0.7, 0.7, 0.7); cr.set_font_size(11)
            cr.move_to(5, y + 4); cr.show_text(f"{rpm}")

        for t in range(TEMP_MIN, TEMP_MAX + 1, 10):
            x = self.temp_to_x(t)
            cr.set_source_rgba(0.4, 0.4, 0.4, 0.4)
            cr.move_to(x, gy); cr.line_to(x, gy + gh); cr.stroke()
            cr.set_source_rgb(0.7, 0.7, 0.7); cr.set_font_size(11)
            cr.move_to(x - 12, gy + gh + 18); cr.show_text(f"{t}\u00b0")

        cr.set_source_rgb(0.85, 0.85, 0.85); cr.set_font_size(13)
        cr.move_to(gx + gw / 2 - 40, gy + gh + 42); cr.show_text("Temperature (\u00b0C)")
        cr.save(); cr.move_to(15, gy + gh / 2 + 30); cr.rotate(-1.5708); cr.show_text("Fan RPM"); cr.restore()

        if not self.points:
            return

        cr.move_to(self.temp_to_x(self.points[0]["temp"]), self.rpm_to_y(0))
        for p in self.points:
            cr.line_to(self.temp_to_x(p["temp"]), self.rpm_to_y(p["rpm"]))
        cr.line_to(self.temp_to_x(self.points[-1]["temp"]), self.rpm_to_y(0))
        cr.close_path(); cr.set_source_rgba(0.13, 0.77, 0.37, 0.15); cr.fill()

        cr.set_line_width(2.5); cr.set_source_rgb(0.13, 0.77, 0.37)
        cr.move_to(self.temp_to_x(self.points[0]["temp"]), self.rpm_to_y(self.points[0]["rpm"]))
        for p in self.points[1:]:
            cr.line_to(self.temp_to_x(p["temp"]), self.rpm_to_y(p["rpm"]))
        cr.stroke()

        for i, p in enumerate(self.points):
            px, py = self.temp_to_x(p["temp"]), self.rpm_to_y(p["rpm"])
            if i == self.dragging_index:
                cr.set_source_rgba(0.13, 0.77, 0.37, 0.4); cr.arc(px, py, POINT_RADIUS + 4, 0, 6.2832); cr.fill()
            cr.set_source_rgb(0.2, 0.9, 0.45) if (i == self.hover_index or i == self.dragging_index) else cr.set_source_rgb(0.13, 0.77, 0.37)
            cr.arc(px, py, POINT_RADIUS, 0, 6.2832); cr.fill()
            cr.set_source_rgb(1, 1, 1); cr.arc(px, py, 3, 0, 6.2832); cr.fill()
            cr.set_font_size(10); cr.move_to(px - 15, py - POINT_RADIUS - 8)
            cr.show_text(f"{p['temp']}\u00b0C, {p['rpm']}")

        # --- Draw current temperature indicators ---
        legend_y = gy + 16
        for name, temp in self.current_temps.items():
            if temp is None or temp < TEMP_MIN or temp > TEMP_MAX:
                continue
            color = self.TEMP_COLORS.get(name, (0.8, 0.8, 0.0))
            tx = self.temp_to_x(temp)

            # Vertical dashed line
            cr.set_source_rgba(*color, 0.8)
            cr.set_line_width(1.5)
            cr.set_dash([6, 4])
            cr.move_to(tx, gy)
            cr.line_to(tx, gy + gh)
            cr.stroke()
            cr.set_dash([])

            # Triangle marker at top
            cr.set_source_rgb(*color)
            cr.move_to(tx, gy)
            cr.line_to(tx - 5, gy - 8)
            cr.line_to(tx + 5, gy - 8)
            cr.close_path()
            cr.fill()

            # Legend entry (top-right inside graph)
            cr.set_font_size(11)
            cr.set_source_rgb(*color)
            label = f"{name}: {temp:.1f}\u00b0C"
            cr.move_to(gx + gw - 110, legend_y)
            cr.show_text(label)
            legend_y += 16

    def find_point_at(self, mx, my):
        for i, p in enumerate(self.points):
            px, py = self.temp_to_x(p["temp"]), self.rpm_to_y(p["rpm"])
            if ((mx - px)**2 + (my - py)**2)**0.5 <= POINT_RADIUS + 4:
                return i
        return -1

    def on_button_press(self, w, e):
        if e.button == 1:
            idx = self.find_point_at(e.x, e.y)
            if idx >= 0:
                self.dragging_index = idx
            else:
                gx, gy, gw, gh = self.get_graph_rect()
                if gx <= e.x <= gx + gw and gy <= e.y <= gy + gh:
                    nt, nr = self.x_to_temp(e.x), self.y_to_rpm(e.y)
                    self.points.append({"temp": nt, "rpm": nr})
                    self.points.sort(key=lambda p: p["temp"])
                    self.dragging_index = next(i for i, p in enumerate(self.points) if p["temp"] == nt and p["rpm"] == nr)
                    self.queue_draw()
                    if self.on_points_changed: self.on_points_changed(self.points)
        elif e.button == 3:
            idx = self.find_point_at(e.x, e.y)
            if idx >= 0 and len(self.points) > 2:
                self.points.pop(idx); self.queue_draw()
                if self.on_points_changed: self.on_points_changed(self.points)

    def on_button_release(self, w, e):
        if self.dragging_index >= 0:
            self.dragging_index = -1; self.queue_draw()
            if self.on_points_changed: self.on_points_changed(self.points)

    def on_motion(self, w, e):
        if self.dragging_index >= 0:
            nt, nr = self.x_to_temp(e.x), self.y_to_rpm(e.y)
            idx = self.dragging_index
            if idx > 0: nt = max(nt, self.points[idx - 1]["temp"] + 1)
            if idx < len(self.points) - 1: nt = min(nt, self.points[idx + 1]["temp"] - 1)
            self.points[idx]["temp"] = nt; self.points[idx]["rpm"] = nr; self.queue_draw()
        else:
            old = self.hover_index; self.hover_index = self.find_point_at(e.x, e.y)
            if old != self.hover_index: self.queue_draw()


# ==============================================================================
# Helper: create styled button row
# ==============================================================================

def make_button_row(labels, active, callback, sensitive=True):
    box = Gtk.Box(spacing=4)
    for label in labels:
        btn = Gtk.Button(label=label)
        if label == active:
            btn.get_style_context().add_class("active-btn")
        btn.set_sensitive(sensitive)
        btn.connect("clicked", lambda b, l=label: callback(l))
        box.pack_start(btn, False, False, 0)
    return box


# ==============================================================================
# Main Application
# ==============================================================================

class RazerControlApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="Razer Control")
        self.set_default_size(820, 650)
        self.set_border_width(8)
        self.current_profile_name = "Balanced"
        self.hid = RazerHID()

        os.makedirs(PROFILES_DIR, exist_ok=True)

        # Main layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        # Connection bar
        conn_bar = Gtk.Box(spacing=6)
        vbox.pack_start(conn_bar, False, False, 0)

        self.connect_btn = Gtk.Button(label="Connect Device")
        self.connect_btn.connect("clicked", self.on_connect)
        conn_bar.pack_start(self.connect_btn, False, False, 0)

        self.conn_label = Gtk.Label(label="Disconnected")
        self.conn_label.set_halign(Gtk.Align.START)
        conn_bar.pack_start(self.conn_label, True, True, 0)

        # Status bar (created early so tabs can call set_status)
        self.statusbar = Gtk.Label(halign=Gtk.Align.START)
        self.statusbar.set_markup("<small>Ready</small>")

        # Profile bar (visible on all tabs)
        profile_bar = Gtk.Box(spacing=6)
        vbox.pack_start(profile_bar, False, False, 0)

        profile_bar.pack_start(Gtk.Label(label="Profile:"), False, False, 0)
        self.profile_combo = Gtk.ComboBoxText()
        self.refresh_profile_list()
        self.profile_combo.set_active_id("Balanced")
        self.profile_combo.connect("changed", self.on_profile_selected)
        profile_bar.pack_start(self.profile_combo, False, False, 0)

        for lbl, cb in [("Save", self.on_save), ("Save As...", self.on_save_as), ("Delete", self.on_delete_profile)]:
            b = Gtk.Button(label=lbl)
            b.connect("clicked", cb)
            profile_bar.pack_start(b, False, False, 0)

        profile_bar.pack_start(Gtk.Box(), True, True, 0)

        stop_btn = Gtk.Button(label="Stop Daemon")
        stop_btn.get_style_context().add_class("destructive-action")
        stop_btn.connect("clicked", self.on_stop_daemon)
        profile_bar.pack_end(stop_btn, False, False, 0)

        apply_btn = Gtk.Button(label="Apply to Daemon")
        apply_btn.get_style_context().add_class("suggested-action")
        apply_btn.connect("clicked", self.on_apply)
        profile_bar.pack_end(apply_btn, False, False, 0)

        # Notebook (tabs)
        self.notebook = Gtk.Notebook()
        vbox.pack_start(self.notebook, True, True, 0)

        self._build_fan_tab()
        self._build_performance_tab()
        self._build_battery_tab()
        self._build_lighting_tab()

        vbox.pack_start(self.statusbar, False, False, 0)

        self.apply_css()

        # Resolve thermal zones for live temperature display
        self.temp_sources = {}
        cpu_path = find_thermal_zone_path("x86_pkg_temp")
        if cpu_path:
            self.temp_sources["CPU"] = cpu_path

        # Initialize NVML for GPU temp
        self.has_gpu_temp = nvml_init_gui(gpu_index=0)

        # Start periodic temperature polling (every 3 seconds)
        if self.temp_sources or self.has_gpu_temp:
            self._poll_temps()
            GLib.timeout_add_seconds(3, self._poll_temps)

        # Auto-connect
        GLib.idle_add(self.on_connect, None)

    # === Fan Curve Tab ===
    def _build_fan_tab(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_border_width(6)

        # Graph
        self.graph = FanCurveGraph()
        self.graph.on_points_changed = self.on_graph_changed
        vbox.pack_start(self.graph, True, True, 0)

        # Live temperature display
        self.temp_label = Gtk.Label(label="Waiting for temperature data...")
        self.temp_label.set_halign(Gtk.Align.START)
        self.temp_label.get_style_context().add_class("temp-label")
        vbox.pack_start(self.temp_label, False, False, 0)

        # Bottom
        bottom = Gtk.Box(spacing=10)
        vbox.pack_start(bottom, False, False, 0)

        # Settings
        sf = Gtk.Frame(label="Settings")
        sg = Gtk.Grid(column_spacing=8, row_spacing=4)
        sg.set_border_width(6); sf.add(sg); bottom.pack_start(sf, False, False, 0)

        sg.attach(Gtk.Label(label="Min RPM:", halign=Gtk.Align.END), 0, 0, 1, 1)
        self.min_rpm_spin = Gtk.SpinButton.new_with_range(0, 5400, 100); self.min_rpm_spin.set_value(800)
        sg.attach(self.min_rpm_spin, 1, 0, 1, 1)
        sg.attach(Gtk.Label(label="Max RPM:", halign=Gtk.Align.END), 0, 1, 1, 1)
        self.max_rpm_spin = Gtk.SpinButton.new_with_range(100, 5400, 100); self.max_rpm_spin.set_value(5400)
        sg.attach(self.max_rpm_spin, 1, 1, 1, 1)
        sg.attach(Gtk.Label(label="Poll (sec):", halign=Gtk.Align.END), 0, 2, 1, 1)
        self.poll_spin = Gtk.SpinButton.new_with_range(1, 60, 1); self.poll_spin.set_value(5)
        sg.attach(self.poll_spin, 1, 2, 1, 1)
        sg.attach(Gtk.Label(label="Hysteresis (\u00b0C):", halign=Gtk.Align.END), 0, 3, 1, 1)
        self.hyst_spin = Gtk.SpinButton.new_with_range(0, 10, 1); self.hyst_spin.set_value(3)
        sg.attach(self.hyst_spin, 1, 3, 1, 1)

        # Points table
        pf = Gtk.Frame(label="Curve Points (L-click: add, R-click: remove)")
        self.points_store = Gtk.ListStore(int, str, str)
        self.points_view = Gtk.TreeView(model=self.points_store)
        for i, (title, w) in enumerate([("#", 30), ("Temp", 80), ("RPM", 80)]):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_min_width(w); self.points_view.append_column(col)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(100); scroll.add(self.points_view)
        pf.add(scroll); bottom.pack_start(pf, True, True, 0)

        self.notebook.append_page(vbox, Gtk.Label(label="Fan Curve"))
        self.load_profile("Balanced")

    # === Performance Tab ===
    def _build_performance_tab(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_border_width(16)

        vbox.pack_start(Gtk.Label(label="Performance Mode", halign=Gtk.Align.START), False, False, 0)

        # CPU
        cpu_box = Gtk.Box(spacing=8)
        cpu_box.pack_start(Gtk.Label(label="CPU:", halign=Gtk.Align.END), False, False, 0)
        self.cpu_perf_box = Gtk.Box(spacing=4)
        cpu_box.pack_start(self.cpu_perf_box, False, False, 0)
        vbox.pack_start(cpu_box, False, False, 0)

        # GPU
        gpu_box = Gtk.Box(spacing=8)
        gpu_box.pack_start(Gtk.Label(label="GPU:", halign=Gtk.Align.END), False, False, 0)
        self.gpu_perf_box = Gtk.Box(spacing=4)
        gpu_box.pack_start(self.gpu_perf_box, False, False, 0)
        vbox.pack_start(gpu_box, False, False, 0)

        # Fan speed display
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        vbox.pack_start(sep, False, False, 8)
        self.fan_speed_label = Gtk.Label(label="Current Fan Speed: --")
        self.fan_speed_label.set_halign(Gtk.Align.START)
        vbox.pack_start(self.fan_speed_label, False, False, 0)

        # Apply + Refresh buttons
        btn_box = Gtk.Box(spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self.on_apply_performance)
        btn_box.pack_start(apply_btn, False, False, 0)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", self.on_refresh_performance)
        btn_box.pack_start(refresh_btn, False, False, 0)
        vbox.pack_start(btn_box, False, False, 0)

        self.cpu_perf = "Medium"
        self.gpu_perf = "Medium"
        self._rebuild_perf_buttons()

        self.notebook.append_page(vbox, Gtk.Label(label="Performance"))

    def _rebuild_perf_buttons(self):
        for child in self.cpu_perf_box.get_children():
            self.cpu_perf_box.remove(child)
        for child in self.gpu_perf_box.get_children():
            self.gpu_perf_box.remove(child)

        for level in ["Low", "Medium", "High", "Boost"]:
            btn = Gtk.Button(label=level)
            if level == self.cpu_perf:
                btn.get_style_context().add_class("active-btn")
            btn.connect("clicked", lambda b, l=level: self.on_set_cpu_perf(l))
            self.cpu_perf_box.pack_start(btn, False, False, 0)

        for level in ["Low", "Medium", "High"]:
            btn = Gtk.Button(label=level)
            if level == self.gpu_perf:
                btn.get_style_context().add_class("active-btn")
            btn.connect("clicked", lambda b, l=level: self.on_set_gpu_perf(l))
            self.gpu_perf_box.pack_start(btn, False, False, 0)

        self.cpu_perf_box.show_all()
        self.gpu_perf_box.show_all()

    def on_set_cpu_perf(self, level):
        self.cpu_perf = level
        self._rebuild_perf_buttons()

    def on_set_gpu_perf(self, level):
        self.gpu_perf = level
        self._rebuild_perf_buttons()

    def on_apply_performance(self, btn):
        if not self.hid.connected:
            self.set_status("Not connected")
            return
        self.cpu_perf = self.hid.set_performance("CPU", self.cpu_perf)
        self.gpu_perf = self.hid.set_performance("GPU", self.gpu_perf)
        self._rebuild_perf_buttons()
        self.set_status(f"Applied: CPU={self.cpu_perf}, GPU={self.gpu_perf}")

    def on_refresh_performance(self, btn):
        if self.hid.connected:
            self.cpu_perf = self.hid.get_performance("CPU")
            self.gpu_perf = self.hid.get_performance("GPU")
            self._rebuild_perf_buttons()
            fan = self.hid.get_fan_speed()
            self.fan_speed_label.set_text(f"Current Fan Speed: {fan} RPM")
            self.set_status("Performance refreshed")

    # === Battery Tab ===
    def _build_battery_tab(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_border_width(16)

        vbox.pack_start(Gtk.Label(label="Battery Charge Limit", halign=Gtk.Align.START), False, False, 0)

        # Toggle
        hbox = Gtk.Box(spacing=8)
        self.battery_switch = Gtk.Switch()
        self.battery_switch.connect("state-set", self.on_battery_toggle)
        hbox.pack_start(Gtk.Label(label="Enable Charge Limit:"), False, False, 0)
        hbox.pack_start(self.battery_switch, False, False, 0)
        vbox.pack_start(hbox, False, False, 0)

        # Slider
        sl_box = Gtk.Box(spacing=8)
        sl_box.pack_start(Gtk.Label(label="Limit (%):"), False, False, 0)
        self.battery_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 50, 80, 5)
        self.battery_scale.set_value(60)
        self.battery_scale.set_hexpand(True)
        self.battery_scale.connect("value-changed", self.on_battery_level_changed)
        self.battery_scale.connect("button-release-event", self.on_battery_scale_released)
        self.battery_scale.set_draw_value(True)
        self.battery_scale.set_value_pos(Gtk.PositionType.RIGHT)
        for v in range(50, 85, 5):
            self.battery_scale.add_mark(v, Gtk.PositionType.BOTTOM, str(v))
        sl_box.pack_start(self.battery_scale, True, True, 0)
        vbox.pack_start(sl_box, False, False, 0)

        self.battery_status_label = Gtk.Label(label="Status: --")
        self.battery_status_label.set_halign(Gtk.Align.START)
        vbox.pack_start(self.battery_status_label, False, False, 0)

        # Apply + Refresh buttons
        btn_box = Gtk.Box(spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self.on_apply_battery)
        btn_box.pack_start(apply_btn, False, False, 0)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", self.on_refresh_battery)
        btn_box.pack_start(refresh_btn, False, False, 0)
        vbox.pack_start(btn_box, False, False, 0)

        self.notebook.append_page(vbox, Gtk.Label(label="Battery"))

    def on_battery_toggle(self, switch, state):
        # UI only - update label preview
        level = int(self.battery_scale.get_value())
        self.battery_status_label.set_text(
            f"Status: {'Enabled' if state else 'Disabled'} at {level}%"
        )
        return False

    def on_battery_level_changed(self, scale):
        """Update label preview only; actual send happens on button-release or Apply."""
        level = int(scale.get_value())
        enabled = self.battery_switch.get_state()
        self.battery_status_label.set_text(
            f"Status: {'Enabled' if enabled else 'Disabled'} at {level}%"
        )

    def on_battery_scale_released(self, scale, event):
        """UI only - no auto send."""
        return False

    def on_apply_battery(self, btn):
        """Explicitly apply current battery settings to device."""
        self._send_battery_to_device()

    def _send_battery_to_device(self):
        """Send current battery switch + level to device."""
        if not self.hid.connected:
            self.set_status("Not connected")
            return
        enabled = self.battery_switch.get_state()
        level = int(self.battery_scale.get_value())
        resp_enabled, resp_lvl = self.hid.set_battery_limit(enabled, level)
        if resp_enabled is not None:
            self.battery_switch.set_state(resp_enabled)
            self.battery_scale.set_value(resp_lvl)
            self.battery_status_label.set_text(
                f"Status: {'Enabled' if resp_enabled else 'Disabled'} at {resp_lvl}%"
            )
            self.set_status(f"Battery limit {'enabled' if resp_enabled else 'disabled'} at {resp_lvl}%")
        else:
            self.set_status("Failed to set battery limit")

    def on_refresh_battery(self, btn):
        if self.hid.connected:
            enabled, level = self.hid.get_battery_limit()
            if enabled is not None:
                self.battery_switch.set_state(enabled)
                self.battery_scale.set_value(level)
                self.battery_status_label.set_text(
                    f"Status: {'Enabled' if enabled else 'Disabled'} at {level}%"
                )

    # === Lighting Tab ===
    def _build_lighting_tab(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_border_width(12)

        # --- Logo LED ---
        logo_frame = Gtk.Frame(label="Logo LED")
        logo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        logo_box.set_border_width(8)
        logo_frame.add(logo_box)

        logo_toggle_box = Gtk.Box(spacing=8)
        self.logo_switch = Gtk.Switch()
        self.logo_switch.connect("state-set", self.on_logo_toggle)
        logo_toggle_box.pack_start(Gtk.Label(label="Logo LED:"), False, False, 0)
        logo_toggle_box.pack_start(self.logo_switch, False, False, 0)
        logo_box.pack_start(logo_toggle_box, False, False, 0)

        self.logo_mode_box = Gtk.Box(spacing=4)
        logo_box.pack_start(self.logo_mode_box, False, False, 0)
        self.logo_mode = "Static"
        self._rebuild_logo_buttons()

        vbox.pack_start(logo_frame, False, False, 0)

        # --- Keyboard Lighting ---
        keyb_frame = Gtk.Frame(label="Keyboard Lighting")
        keyb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        keyb_box.set_border_width(8)
        keyb_frame.add(keyb_box)

        # On/Off toggle
        keyb_toggle_box = Gtk.Box(spacing=8)
        self.keyb_switch = Gtk.Switch()
        self.keyb_switch.connect("state-set", self.on_keyb_toggle)
        keyb_toggle_box.pack_start(Gtk.Label(label="Keyboard:"), False, False, 0)
        keyb_toggle_box.pack_start(self.keyb_switch, False, False, 0)
        keyb_box.pack_start(keyb_toggle_box, False, False, 0)

        # Mode buttons
        self.keyb_mode_box = Gtk.Box(spacing=4)
        keyb_box.pack_start(self.keyb_mode_box, False, False, 0)

        # Speed slider
        speed_box = Gtk.Box(spacing=8)
        speed_box.pack_start(Gtk.Label(label="Speed:"), False, False, 0)
        self.keyb_speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 15, 1)
        self.keyb_speed_scale.set_value(1)
        self.keyb_speed_scale.set_hexpand(True)
        self.keyb_speed_scale.connect("value-changed", self.on_keyb_param_changed)
        speed_box.pack_start(self.keyb_speed_scale, True, True, 0)
        keyb_box.pack_start(speed_box, False, False, 0)

        # RGB color
        rgb_box = Gtk.Box(spacing=8)
        rgb_box.pack_start(Gtk.Label(label="Color:"), False, False, 0)
        self.keyb_color_btn = Gtk.ColorButton()
        self.keyb_color_btn.set_rgba(Gdk.RGBA(1, 1, 1, 1))
        self.keyb_color_btn.connect("color-set", self.on_keyb_param_changed)
        rgb_box.pack_start(self.keyb_color_btn, False, False, 0)
        keyb_box.pack_start(rgb_box, False, False, 0)

        # Direction
        dir_box = Gtk.Box(spacing=8)
        dir_box.pack_start(Gtk.Label(label="Direction:"), False, False, 0)
        self.keyb_dir_box = Gtk.Box(spacing=4)
        dir_box.pack_start(self.keyb_dir_box, False, False, 0)
        keyb_box.pack_start(dir_box, False, False, 0)

        vbox.pack_start(keyb_frame, False, False, 0)

        # Apply + Refresh buttons
        btn_box = Gtk.Box(spacing=8)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self.on_apply_lighting)
        btn_box.pack_start(apply_btn, False, False, 0)
        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", self.on_refresh_lighting)
        btn_box.pack_start(refresh_btn, False, False, 0)
        vbox.pack_start(btn_box, False, False, 0)

        self.keyb_mode = "Off"
        self.keyb_speed = 1
        self.keyb_rgb = [255, 255, 255]
        self.keyb_direction = "Left"
        self._rebuild_keyb_buttons()

        self.notebook.append_page(vbox, Gtk.Label(label="Lighting"))

    def _rebuild_logo_buttons(self):
        for c in self.logo_mode_box.get_children():
            self.logo_mode_box.remove(c)
        for mode in ["Static", "Breathing"]:
            btn = Gtk.Button(label=mode)
            if mode == self.logo_mode:
                btn.get_style_context().add_class("active-btn")
            btn.connect("clicked", lambda b, m=mode: self.on_set_logo_mode(m))
            self.logo_mode_box.pack_start(btn, False, False, 0)
        self.logo_mode_box.show_all()

    def _rebuild_keyb_buttons(self):
        for c in self.keyb_mode_box.get_children():
            self.keyb_mode_box.remove(c)
        for mode in ["Wave", "Reactive", "Spectrum", "Static", "Starlight"]:
            btn = Gtk.Button(label=mode)
            if mode == self.keyb_mode:
                btn.get_style_context().add_class("active-btn")
            btn.connect("clicked", lambda b, m=mode: self.on_set_keyb_mode(m))
            self.keyb_mode_box.pack_start(btn, False, False, 0)
        self.keyb_mode_box.show_all()

        # Update direction buttons
        for c in self.keyb_dir_box.get_children():
            self.keyb_dir_box.remove(c)
        features = KEYB_MODE_FEATURES.get(self.keyb_mode, {})
        for d in ["Left", "Right"]:
            btn = Gtk.Button(label=d)
            if d == self.keyb_direction and features.get("Direction"):
                btn.get_style_context().add_class("active-btn")
            btn.set_sensitive(features.get("Direction", False))
            btn.connect("clicked", lambda b, dd=d: self.on_set_keyb_direction(dd))
            self.keyb_dir_box.pack_start(btn, False, False, 0)
        self.keyb_dir_box.show_all()

        # Update sensitivity
        self.keyb_speed_scale.set_sensitive(features.get("Speed", False))
        self.keyb_color_btn.set_sensitive(features.get("RGB", False))

    def on_logo_toggle(self, switch, state):
        # UI only - no device communication
        return False

    def on_set_logo_mode(self, mode):
        self.logo_mode = mode
        self._rebuild_logo_buttons()

    def on_keyb_toggle(self, switch, state):
        if state and self.keyb_mode == "Off":
            self.keyb_mode = "Spectrum"
        elif not state:
            self.keyb_mode = "Off"
        self._rebuild_keyb_buttons()
        return False

    def on_set_keyb_mode(self, mode):
        self.keyb_mode = mode
        self.keyb_switch.set_state(mode != "Off")
        self._rebuild_keyb_buttons()

    def on_set_keyb_direction(self, direction):
        self.keyb_direction = direction
        self._rebuild_keyb_buttons()

    def on_keyb_param_changed(self, widget):
        # UI only - no device communication
        pass

    def on_apply_lighting(self, btn):
        """Apply all lighting settings to device."""
        if not self.hid.connected:
            self.set_status("Not connected")
            return
        # Logo
        logo_on = self.logo_switch.get_state()
        result = self.hid.set_logo_status(logo_on)
        if logo_on:
            self.hid.set_logo_mode(self.logo_mode)
        # Keyboard
        if self.keyb_mode == "Off":
            self.hid.set_keyboard_mode("Off", 1, [0, 0, 0], "Left")
        else:
            rgb = self._get_keyb_color()
            speed = int(self.keyb_speed_scale.get_value())
            result_mode = self.hid.set_keyboard_mode(
                self.keyb_mode, speed, rgb, self.keyb_direction
            )
            self.keyb_mode = result_mode
            self._rebuild_keyb_buttons()
        self.set_status(f"Applied: Logo={'ON' if logo_on else 'OFF'}, Keyboard={self.keyb_mode}")

    def _get_keyb_color(self):
        rgba = self.keyb_color_btn.get_rgba()
        return [int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255)]

    def on_refresh_lighting(self, btn):
        if not self.hid.connected:
            return
        # Logo
        logo_on = self.hid.get_logo_status()
        self.logo_switch.set_state(logo_on)
        if logo_on:
            self.logo_mode = self.hid.get_logo_mode()
            self._rebuild_logo_buttons()

        # Keyboard
        mode, speed, rgb, direction = self.hid.get_keyboard_mode()
        self.keyb_mode = mode
        self.keyb_speed = speed
        self.keyb_rgb = rgb
        self.keyb_direction = direction
        self.keyb_switch.set_state(mode != "Off")
        self.keyb_speed_scale.set_value(speed)
        self.keyb_color_btn.set_rgba(Gdk.RGBA(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1))
        self._rebuild_keyb_buttons()
        self.set_status("Lighting refreshed")

    # === Connection ===
    def on_connect(self, btn):
        if self.hid.connected:
            self.hid.disconnect()
            self.conn_label.set_text("Disconnected")
            self.connect_btn.set_label("Connect Device")
            self.set_status("Disconnected")
        else:
            if self.hid.find_and_connect():
                self.conn_label.set_text(f"Connected: {self.hid.dev_path}")
                self.connect_btn.set_label("Disconnect")
                self.set_status(f"Connected to {self.hid.dev_path}")
                # Load current state
                GLib.idle_add(self._load_device_state)
            else:
                self.set_status("Failed to connect. Check device permissions.")

    def _poll_temps(self):
        """Read current temperatures and update graph + label."""
        temps = {}
        for name, path in self.temp_sources.items():
            t = read_sysfs_temp(path)
            if t is not None:
                temps[name] = t
        if self.has_gpu_temp:
            gpu_t = read_gpu_temp()
            if gpu_t is not None:
                temps["GPU"] = gpu_t
        self.graph.set_temps(temps)
        # Update temperature label
        parts = [f"{name}: {t:.0f}°C" for name, t in sorted(temps.items())]
        self.temp_label.set_text("  |  ".join(parts) if parts else "No temperature data")
        return True  # keep timer alive

    def _load_device_state(self):
        """Load current device state into all tabs, then match to saved profile."""
        if not self.hid.connected:
            return
        # Performance
        self.cpu_perf = self.hid.get_performance("CPU")
        self.gpu_perf = self.hid.get_performance("GPU")
        self._rebuild_perf_buttons()
        fan = self.hid.get_fan_speed()
        self.fan_speed_label.set_text(f"Current Fan Speed: {fan} RPM")

        # Battery
        enabled, level = self.hid.get_battery_limit()
        if enabled is not None:
            self.battery_switch.set_state(enabled)
            self.battery_scale.set_value(level)
            self.battery_status_label.set_text(
                f"Status: {'Enabled' if enabled else 'Disabled'} at {level}%"
            )

        # Lighting
        logo_on = self.hid.get_logo_status()
        self.logo_switch.set_state(logo_on)
        if logo_on:
            self.logo_mode = self.hid.get_logo_mode()
            self._rebuild_logo_buttons()

        mode, speed, rgb, direction = self.hid.get_keyboard_mode()
        self.keyb_mode = mode
        self.keyb_speed = speed
        self.keyb_rgb = rgb
        self.keyb_direction = direction
        self.keyb_switch.set_state(mode != "Off")
        self.keyb_speed_scale.set_value(speed)
        self.keyb_color_btn.set_rgba(Gdk.RGBA(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1))
        self._rebuild_keyb_buttons()

        # Find matching saved profile
        self._match_current_profile()

    def _match_current_profile(self):
        """Compare current device state to saved profiles and select the best match."""
        current = self.build_config_dict()
        best_name = None
        best_score = -1

        # Check user profiles
        if os.path.isdir(PROFILES_DIR):
            for f in sorted(os.listdir(PROFILES_DIR)):
                if not f.endswith(".json"):
                    continue
                name = f[:-5]
                try:
                    with open(os.path.join(PROFILES_DIR, f), "r") as fh:
                        saved = json.load(fh)
                    score = self._profile_match_score(current, saved)
                    if score > best_score:
                        best_score = score
                        best_name = name
                except (json.JSONDecodeError, IOError):
                    continue

        if best_name and best_score >= 3:
            # Good enough match - select it without re-applying to device
            self.current_profile_name = best_name
            # Load fan curve from profile (device state doesn't have fan curve)
            try:
                with open(os.path.join(PROFILES_DIR, f"{best_name}.json"), "r") as fh:
                    data = json.load(fh)
                points = data.get("fan_curve", PRESETS["Balanced"])
                self.graph.set_points(points)
                self.update_points_table(points)
                self.min_rpm_spin.set_value(data.get("min_rpm", 800))
                self.max_rpm_spin.set_value(data.get("max_rpm", 5400))
                self.poll_spin.set_value(data.get("poll_interval_sec", 5))
                self.hyst_spin.set_value(data.get("hysteresis_deg", 3))
            except (json.JSONDecodeError, IOError):
                pass
            self.profile_combo.set_active_id(best_name)
            self.set_status(f"Connected - matched profile: {best_name}")
        else:
            self.set_status("Connected - no matching profile found")

    @staticmethod
    def _profile_match_score(current, saved):
        """Score how well a saved profile matches current device state (0-6)."""
        score = 0
        if saved.get("cpu_perf") == current.get("cpu_perf"):
            score += 1
        if saved.get("gpu_perf") == current.get("gpu_perf"):
            score += 1
        if saved.get("battery_enabled") == current.get("battery_enabled"):
            score += 1
        if saved.get("logo_enabled") == current.get("logo_enabled"):
            score += 1
        if saved.get("keyb_mode") == current.get("keyb_mode"):
            score += 1
        if saved.get("keyb_rgb") == current.get("keyb_rgb"):
            score += 1
        return score

    # === Fan curve profile methods (unchanged logic) ===
    def refresh_profile_list(self):
        self.profile_combo.remove_all()
        for name in PRESETS:
            self.profile_combo.append(name, f"[Preset] {name}")
        if os.path.isdir(PROFILES_DIR):
            for f in sorted(os.listdir(PROFILES_DIR)):
                if f.endswith(".json"):
                    self.profile_combo.append(f[:-5], f[:-5])

    def load_profile(self, name):
        self.current_profile_name = name
        data = None

        if name in PRESETS:
            points = PRESETS[name]
        else:
            path = os.path.join(PROFILES_DIR, f"{name}.json")
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                points = data.get("fan_curve", PRESETS["Balanced"])
            except (json.JSONDecodeError, IOError):
                points = PRESETS["Balanced"]

        # Fan curve
        self.graph.set_points(points)
        self.update_points_table(points)

        if data:
            # Fan daemon settings
            self.min_rpm_spin.set_value(data.get("min_rpm", 800))
            self.max_rpm_spin.set_value(data.get("max_rpm", 5400))
            self.poll_spin.set_value(data.get("poll_interval_sec", 5))
            self.hyst_spin.set_value(data.get("hysteresis_deg", 3))

            # Performance
            self.cpu_perf = data.get("cpu_perf", self.cpu_perf)
            self.gpu_perf = data.get("gpu_perf", self.gpu_perf)
            self._rebuild_perf_buttons()

            # Battery
            if "battery_enabled" in data:
                self.battery_switch.set_state(data["battery_enabled"])
                self.battery_scale.set_value(data.get("battery_level", 60))

            # Logo LED
            if "logo_enabled" in data:
                self.logo_switch.set_state(data["logo_enabled"])
                self.logo_mode = data.get("logo_mode", "Static")
                self._rebuild_logo_buttons()

            # Keyboard lighting
            if "keyb_mode" in data:
                self.keyb_mode = data["keyb_mode"]
                self.keyb_speed = data.get("keyb_speed", 1)
                self.keyb_rgb = data.get("keyb_rgb", [255, 255, 255])
                self.keyb_direction = data.get("keyb_direction", "Left")
                self.keyb_switch.set_state(self.keyb_mode != "Off")
                self.keyb_speed_scale.set_value(self.keyb_speed)
                r, g, b = self.keyb_rgb
                self.keyb_color_btn.set_rgba(Gdk.RGBA(r / 255, g / 255, b / 255, 1))
                self._rebuild_keyb_buttons()

        self.set_status(f"Loaded: {name}")

    def _apply_profile_to_device(self, data):
        """Send all profile settings to the connected device."""
        try:
            # Performance
            if "cpu_perf" in data:
                self.hid.set_performance("CPU", data["cpu_perf"])
                time.sleep(0.05)
            if "gpu_perf" in data:
                self.hid.set_performance("GPU", data["gpu_perf"])
                time.sleep(0.05)

            # Battery
            if "battery_enabled" in data:
                self.hid.set_battery_limit(data["battery_enabled"], data.get("battery_level", 60))
                time.sleep(0.05)

            # Logo
            if "logo_enabled" in data:
                self.hid.set_logo_status(data["logo_enabled"])
                time.sleep(0.05)
                if data["logo_enabled"]:
                    self.hid.set_logo_mode(data.get("logo_mode", "Static"))
                    time.sleep(0.05)

            # Keyboard
            if "keyb_mode" in data:
                self.hid.set_keyboard_mode(
                    data["keyb_mode"],
                    data.get("keyb_speed", 1),
                    data.get("keyb_rgb", [255, 255, 255]),
                    data.get("keyb_direction", "Left"),
                )
        except Exception as e:
            self.set_status(f"Apply error: {str(e)[:80]}")

    def on_profile_selected(self, combo):
        name = combo.get_active_id()
        if name: self.load_profile(name)

    def on_graph_changed(self, points):
        self.update_points_table(points)

    def update_points_table(self, points):
        self.points_store.clear()
        for i, p in enumerate(points):
            self.points_store.append([i + 1, f"{p['temp']}\u00b0C", f"{p['rpm']}"])

    def build_config_dict(self):
        """Build full profile dict including all settings."""
        rgba = self.keyb_color_btn.get_rgba()
        return {
            # Fan daemon settings
            "poll_interval_sec": int(self.poll_spin.get_value()),
            "fan_curve": [dict(p) for p in self.graph.points],
            "min_rpm": int(self.min_rpm_spin.get_value()),
            "max_rpm": int(self.max_rpm_spin.get_value()),
            "temp_sources": [
                {"name": "CPU", "type": "thermal_zone", "zone_name": "x86_pkg_temp"},
                {"name": "GPU", "type": "nvml", "gpu_index": 0}
            ],
            "log_level": "INFO",
            "log_file": "/var/log/razer-control-daemon.log",
            "hysteresis_deg": int(self.hyst_spin.get_value()),
            # Performance
            "cpu_perf": self.cpu_perf,
            "gpu_perf": self.gpu_perf,
            # Battery
            "battery_enabled": self.battery_switch.get_state(),
            "battery_level": int(self.battery_scale.get_value()),
            # Logo LED
            "logo_enabled": self.logo_switch.get_state(),
            "logo_mode": self.logo_mode,
            # Keyboard lighting
            "keyb_mode": self.keyb_mode,
            "keyb_speed": int(self.keyb_speed_scale.get_value()),
            "keyb_rgb": [int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255)],
            "keyb_direction": self.keyb_direction,
        }

    def on_save(self, btn):
        name = self.current_profile_name
        if name in PRESETS: return self.on_save_as(btn)
        data = self.build_config_dict()
        with open(os.path.join(PROFILES_DIR, f"{name}.json"), "w") as f:
            json.dump(data, f, indent=4)
        self.set_status(f"Saved: {name}")

    def on_save_as(self, btn):
        dialog = Gtk.Dialog(title="Save Profile As", parent=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        box = dialog.get_content_area(); box.set_spacing(8); box.set_border_width(10)
        box.add(Gtk.Label(label="Profile name:"))
        entry = Gtk.Entry()
        entry.set_text(self.current_profile_name if self.current_profile_name not in PRESETS else "")
        box.add(entry); dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            name = entry.get_text().strip()
            if name:
                with open(os.path.join(PROFILES_DIR, f"{name}.json"), "w") as f:
                    json.dump(self.build_config_dict(), f, indent=4)
                self.current_profile_name = name
                self.refresh_profile_list(); self.profile_combo.set_active_id(name)
                self.set_status(f"Saved: {name}")
        dialog.destroy()

    def on_delete_profile(self, btn):
        name = self.current_profile_name
        if name in PRESETS:
            self.set_status("Cannot delete built-in presets"); return
        dialog = Gtk.MessageDialog(parent=self, flags=Gtk.DialogFlags.MODAL,
                                   type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                                   text=f"Delete profile '{name}'?")
        if dialog.run() == Gtk.ResponseType.YES:
            path = os.path.join(PROFILES_DIR, f"{name}.json")
            if os.path.exists(path): os.remove(path)
            self.refresh_profile_list(); self.profile_combo.set_active_id("Balanced")
            self.load_profile("Balanced"); self.set_status(f"Deleted: {name}")
        dialog.destroy()

    # === Daemon control ===
    def is_systemd_service_installed(self):
        return subprocess.run(["systemctl", "cat", "razer-control-daemon"],
                              capture_output=True, text=True).returncode == 0

    def find_running_daemon_pid(self):
        if os.path.exists(DAEMON_PID_FILE):
            try:
                with open(DAEMON_PID_FILE, "r") as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0); return pid
            except (IOError, ValueError, ProcessLookupError, PermissionError):
                pass
        try:
            result = subprocess.run(["pgrep", "-f", "razer_control_daemon.py"], capture_output=True, text=True)
            if result.returncode == 0:
                pids = result.stdout.strip().split("\n")
                if pids and pids[0]: return int(pids[0])
        except (ValueError, FileNotFoundError):
            pass
        return None

    def stop_running_daemon(self):
        pid = self.find_running_daemon_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    time.sleep(0.25)
                    try: os.kill(pid, 0)
                    except ProcessLookupError: return True
                os.kill(pid, signal.SIGKILL); return True
            except (ProcessLookupError, PermissionError):
                return True
        return False

    def on_stop_daemon(self, btn):
        if self.is_systemd_service_installed():
            result = subprocess.run(["pkexec", "systemctl", "stop", "razer-control-daemon"],
                                    capture_output=True, text=True, timeout=15)
            self.set_status("Daemon stopped (systemd)." if result.returncode == 0
                            else f"Error: {result.stderr.strip()[:80]}")
        else:
            self.set_status("Daemon stopped." if self.stop_running_daemon() else "No running daemon found.")

    def on_apply(self, btn):
        data = self.build_config_dict()

        config_json = json.dumps(data, indent=4)
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="razer-control-")
            with os.fdopen(tmp_fd, "w") as f:
                f.write(config_json)

            if self.is_systemd_service_installed():
                result = subprocess.run(
                    ["pkexec", "bash", "-c", f"cp {tmp_path} {DAEMON_CONFIG_PATH} && systemctl restart razer-control-daemon"],
                    capture_output=True, text=True, timeout=30)
                os.unlink(tmp_path)
                self.set_status("Applied! Daemon restarted via systemd." if result.returncode == 0
                                else f"Error: {result.stderr.strip()[:100]}")
            else:
                self.stop_running_daemon()
                local_config = os.path.join(os.path.dirname(DAEMON_SCRIPT_PATH), "config.json")
                try:
                    result = subprocess.run(["pkexec", "cp", tmp_path, DAEMON_CONFIG_PATH],
                                            capture_output=True, text=True, timeout=15)
                    config_to_use = DAEMON_CONFIG_PATH if result.returncode == 0 else None
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    config_to_use = None

                if not config_to_use:
                    with open(local_config, "w") as f:
                        f.write(config_json)
                    config_to_use = local_config

                os.unlink(tmp_path)
                proc = subprocess.Popen(
                    ["python3", DAEMON_SCRIPT_PATH, "--config", config_to_use],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
                try:
                    with open(DAEMON_PID_FILE, "w") as f:
                        f.write(str(proc.pid))
                except IOError:
                    pass
                time.sleep(1)
                if proc.poll() is None:
                    self.set_status(f"Applied! Daemon started (PID: {proc.pid})")
                else:
                    err = proc.stderr.read().decode() if proc.stderr else ""
                    self.set_status(f"Failed: {err[:100]}")
        except Exception as e:
            self.set_status(f"Error: {str(e)[:100]}")

    # === CSS ===
    def apply_css(self):
        css = b"""
        /* === Razer Dark Theme === */

        /* Global reset - force dark on everything */
        * {
            color: #c8c8c8;
            background-color: transparent;
            background-image: none;
            text-shadow: none;
            -gtk-icon-shadow: none;
            box-shadow: none;
        }

        window, dialog, messagedialog {
            background-color: #0a0a0a;
        }

        /* --- Buttons --- */
        button {
            background-color: #1a1a1a;
            background-image: none;
            color: #c8c8c8;
            border: 1px solid #2a2a2a;
            border-radius: 4px;
            padding: 5px 14px;
            min-height: 20px;
        }
        button:hover {
            background-color: #252525;
            background-image: none;
            border-color: #44bb44;
        }
        button:active, button:checked {
            background-color: #1a1a1a;
            background-image: none;
        }
        button.suggested-action {
            background-color: #44bb44;
            background-image: none;
            color: #000;
            font-weight: bold;
            border-color: #44bb44;
        }
        button.suggested-action:hover {
            background-color: #33aa33;
            background-image: none;
        }
        button.destructive-action {
            background-color: #cc3333;
            background-image: none;
            color: #fff;
            font-weight: bold;
            border-color: #cc3333;
        }
        button.destructive-action:hover {
            background-color: #aa2222;
            background-image: none;
        }
        button.active-btn {
            background-color: #44bb44;
            background-image: none;
            color: #000;
            font-weight: bold;
            border-color: #44bb44;
        }

        /* --- Labels --- */
        label {
            color: #c8c8c8;
        }
        label.temp-label {
            font-family: monospace;
            font-size: 13px;
            font-weight: bold;
            color: #44bb44;
            padding: 4px 8px;
        }

        /* --- Entry / SpinButton --- */
        entry, spinbutton {
            background-color: #141414;
            background-image: none;
            color: #c8c8c8;
            border: 1px solid #2a2a2a;
            border-radius: 3px;
            padding: 2px 6px;
        }
        entry:focus, spinbutton:focus {
            border-color: #44bb44;
        }
        spinbutton button {
            background-color: #1a1a1a;
            background-image: none;
            border: none;
            color: #44bb44;
            min-width: 20px;
        }
        spinbutton button:hover {
            background-color: #252525;
            background-image: none;
        }

        /* --- ComboBox --- */
        combobox button.combo {
            background-color: #141414;
            background-image: none;
            color: #c8c8c8;
            border: 1px solid #2a2a2a;
        }
        combobox button.combo:hover {
            border-color: #44bb44;
            background-image: none;
        }
        combobox window.popup, combobox window {
            background-color: #141414;
        }
        modelbutton, cellview {
            color: #c8c8c8;
        }

        /* --- Notebook / Tabs --- */
        notebook {
            background-color: #0a0a0a;
        }
        notebook header {
            background-color: #0a0a0a;
            border-color: #1a1a1a;
        }
        notebook header tab {
            background-color: #141414;
            background-image: none;
            color: #666;
            border: 1px solid #1a1a1a;
            padding: 6px 16px;
        }
        notebook header tab:hover {
            color: #44bb44;
            background-color: #1a1a1a;
            background-image: none;
        }
        notebook header tab:checked {
            background-color: #44bb44;
            background-image: none;
            color: #000;
            font-weight: bold;
            border-color: #44bb44;
        }
        notebook > stack {
            background-color: #0f0f0f;
        }

        /* --- Frame --- */
        frame {
            border: 1px solid #1e1e1e;
            border-radius: 4px;
        }
        frame > label {
            color: #44bb44;
            font-weight: bold;
        }

        /* --- Scale / Slider --- */
        scale {
            min-height: 20px;
        }
        scale trough {
            background-color: #1a1a1a;
            border-radius: 4px;
            min-height: 6px;
        }
        scale trough highlight {
            background-color: #44bb44;
            border-radius: 4px;
            min-height: 6px;
        }
        scale slider {
            background-color: #44bb44;
            background-image: none;
            border: 2px solid #33aa33;
            border-radius: 50%;
            min-width: 18px;
            min-height: 18px;
        }
        scale slider:hover {
            background-color: #55cc55;
            background-image: none;
        }
        scale value, scale marks, scale mark label {
            color: #666;
        }

        /* --- Switch --- */
        switch {
            background-color: #1a1a1a;
            background-image: none;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            min-width: 48px;
            min-height: 24px;
        }
        switch:checked {
            background-color: #44bb44;
            background-image: none;
            border-color: #44bb44;
        }
        switch slider {
            background-color: #c8c8c8;
            background-image: none;
            border-radius: 50%;
            min-width: 20px;
            min-height: 20px;
            border: none;
        }

        /* --- TreeView --- */
        treeview {
            background-color: #0f0f0f;
            color: #c8c8c8;
        }
        treeview:selected {
            background-color: #44bb44;
            color: #000;
        }
        treeview header button {
            background-color: #141414;
            background-image: none;
            color: #44bb44;
            border: 1px solid #1a1a1a;
            font-weight: bold;
        }

        /* --- ScrolledWindow --- */
        scrolledwindow {
            background-color: #0f0f0f;
        }
        scrollbar {
            background-color: #0a0a0a;
        }
        scrollbar slider {
            background-color: #2a2a2a;
            border-radius: 4px;
            min-width: 6px;
        }
        scrollbar slider:hover {
            background-color: #44bb44;
        }

        /* --- Separator --- */
        separator {
            background-color: #1e1e1e;
            min-height: 1px;
        }

        /* --- Color Button --- */
        colorbutton button {
            padding: 2px;
            border: 2px solid #2a2a2a;
        }
        colorbutton button:hover {
            border-color: #44bb44;
        }

        /* --- Tooltip --- */
        tooltip, tooltip.background {
            background-color: #1a1a1a;
            color: #c8c8c8;
            border: 1px solid #44bb44;
        }

        /* --- Dialog --- */
        dialog, messagedialog {
            background-color: #0f0f0f;
        }
        dialog entry {
            background-color: #141414;
            background-image: none;
            color: #c8c8c8;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)

    def set_status(self, msg):
        self.statusbar.set_markup(f"<small>{GLib.markup_escape_text(msg)}</small>")


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = RazerControlApp()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
