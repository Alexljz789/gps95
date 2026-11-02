"""Small hardware / service helper layer used by the web app."""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import time

CTL = "/usr/local/sbin/gps95-ctl"
CONTROL_SOCK = "/run/gps95/control.sock"
STATUS_FILE = "/run/gps95/status.json"


def _run(cmd, timeout=20):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 127, str(exc)


def bridge_status():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def control(cmd, timeout=25, **kwargs):
    """Talk to the bridge control socket; fall back to gps95-ctl."""
    req = {"cmd": cmd}
    req.update(kwargs)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(CONTROL_SOCK)
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data and len(data) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        if data:
            return json.loads(data.decode("utf-8", "replace").strip())
    except Exception:
        pass
    return run_ctl(cmd, timeout=timeout, **kwargs)


def run_ctl(cmd, timeout=25, **kwargs):
    args = [CTL, cmd]
    for key, value in kwargs.items():
        args.append(f"{key}={value}")
    if os.geteuid() != 0 and shutil.which("sudo"):
        args = ["sudo", "-n"] + args
    code, out = _run(args, timeout=timeout)
    return {"ok": code == 0, "code": code, "output": out.strip()[:2000]}


def repair():
    """Restore the GP-02 NMEA profile and restart gpsd if needed."""
    result = control("repair")
    ctl = run_ctl("restart-gpsd")
    result["gpsd_restart"] = ctl
    return result


def set_gnss_mode(mode):
    return control("set_mode", mode=int(mode))


def powercycle():
    return control("powercycle")


def restart_bridge():
    return run_ctl("restart-bridge")


def gpsd_state():
    code, out = _run(["systemctl", "is-active", "gpsd.service"], timeout=5)
    return out.strip() or "unknown"


def bridge_service_state():
    code, out = _run(["systemctl", "is-active", "gps95-gpsbridge.service"], timeout=5)
    return out.strip() or "unknown"


def gpsd_version():
    for path in ("/usr/sbin/gpsd", "/usr/bin/gpsd", "gpsd"):
        code, out = _run([path, "-V"], timeout=5)
        if code == 0 and out.strip():
            return out.strip().splitlines()[0][:120]
    return ""


def _read(path, default=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except Exception:
        return default


def _battery():
    for path in sorted(glob.glob("/sys/class/power_supply/*")):
        cap = _read(os.path.join(path, "capacity"))
        if cap and cap.isdigit():
            status = _read(os.path.join(path, "status"), "")
            name = os.path.basename(path)
            return {"name": name, "capacity": int(cap), "status": status}
    return {}


def _temp():
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if raw and raw.lstrip("-").isdigit():
        return round(int(raw) / 1000.0, 1)
    return None


def _uptime():
    raw = _read("/proc/uptime")
    if raw:
        try:
            return int(float(raw.split()[0]))
        except ValueError:
            pass
    return None


def system_info():
    info = {
        "hostname": socket.gethostname(),
        "kernel": os.uname().release if hasattr(os, "uname") else "",
        "uptime": _uptime(),
        "battery": _battery(),
        "temp": _temp(),
        "gpsd": gpsd_state(),
        "gpsd_version": gpsd_version(),
        "bridge": bridge_service_state(),
        "serial": os.path.realpath("/dev/serial0") if os.path.exists("/dev/serial0") else "",
        "now": time.time(),
    }
    try:
        usage = shutil.disk_usage("/")
        info["disk"] = {"total": usage.total, "used": usage.used, "free": usage.free}
    except Exception:
        info["disk"] = {}
    return info
