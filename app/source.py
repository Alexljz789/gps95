"""Resilient GPS source manager: gpsd -> NMEA bridge -> direct serial."""

from __future__ import annotations

import socket
import threading
import time

import serial

from .gpsd_client import GpsdClient
from .model import GPSState
from .nmea import NMEAParser

NMEA_HOST = "127.0.0.1"
NMEA_PORT = 2958
SERIAL_PORT = "/dev/serial0"
BAUD = 9600


def _pcas(body: str) -> bytes:
    ck = 0
    for ch in body:
        ck ^= ord(ch)
    return f"${body}*{ck:02X}\r\n".encode("ascii")


PCAS_NMEA = _pcas("PCAS03,1,1,1,1,1,1,1,1,0,0,0,0")
PCAS_RESTART = _pcas("PCAS10,2")
PCAS_MODE = lambda mode: _pcas(f"PCAS04,{int(mode)}")


class NmeaTcpSource:
    name = "bridge"

    def __init__(self, state: GPSState):
        self.state = state
        self.parser = NMEAParser(state)
        self.stop_event = threading.Event()
        self.thread = None
        self.sock = None
        self.last_line = 0.0
        self.last_error = ""
        self.connected = False
        self.count = 0

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="nmea-tcp", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        if self.thread:
            self.thread.join(timeout=2)

    def healthy(self):
        return self.connected and (time.time() - self.last_line) < 5.0

    def status(self):
        return {
            "connected": self.connected,
            "last_age": round(time.time() - self.last_line, 2) if self.last_line else None,
            "error": self.last_error,
            "count": self.count,
            "port": NMEA_PORT,
        }

    def _run(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                sock = socket.create_connection((NMEA_HOST, NMEA_PORT), timeout=3)
                sock.settimeout(1.0)
                self.sock = sock
                self.connected = True
                self.last_error = ""
                self._read(sock)
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)[:200]
            finally:
                try:
                    if self.sock:
                        self.sock.close()
                except OSError:
                    pass
                self.sock = None
                self.connected = False
            if self.stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(8.0, backoff * 1.5)

    def _read(self, sock):
        buf = b""
        while not self.stop_event.is_set():
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("bridge closed the connection")
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                raw = raw.strip()
                if raw.startswith(b"$"):
                    self.count += 1
                    self.last_line = time.time()
                    self.parser.feed(raw)


class DirectSerialSource:
    name = "serial-direct"

    def __init__(self, state: GPSState, mode=7):
        self.state = state
        self.parser = NMEAParser(state)
        self.mode = mode
        self.stop_event = threading.Event()
        self.thread = None
        self.ser = None
        self.last_line = 0.0
        self.last_error = ""
        self.connected = False
        self.count = 0

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="serial-direct", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        if self.thread:
            self.thread.join(timeout=2)

    def healthy(self):
        return self.connected and (time.time() - self.last_line) < 5.0

    def status(self):
        return {
            "connected": self.connected,
            "last_age": round(time.time() - self.last_line, 2) if self.last_line else None,
            "error": self.last_error,
            "count": self.count,
            "port": SERIAL_PORT,
        }

    def _run(self):
        # Taking the UART away from gpsd is a last-resort path; the bridge is
        # normally the owner of the device.
        try:
            from . import hw
            hw.run_ctl("stop-gpsd")
        except Exception:
            pass
        while not self.stop_event.is_set():
            try:
                self.ser = serial.Serial(SERIAL_PORT, BAUD, timeout=0.2, write_timeout=1.0)
                self.ser.reset_input_buffer()
                # Do not send PCAS03 here: on GP-02 it suppresses GGA.
                self.ser.write(PCAS_MODE(self.mode))
                self.ser.flush()
                self.connected = True
                self.last_error = ""
                buf = b""
                self.last_line = 0.0
                opened = time.time()
                reset_sent = False
                while not self.stop_event.is_set():
                    chunk = self.ser.read(8192)
                    if chunk:
                        buf += chunk
                    if (not chunk and not reset_sent and self.last_line == 0
                            and time.time() - opened > 5.0):
                        # The receiver was left in a binary-only state.
                        self.ser.write(PCAS_RESTART)
                        self.ser.flush()
                        time.sleep(4.0)
                        self.ser.write(PCAS_MODE(self.mode))
                        self.ser.flush()
                        reset_sent = True
                        opened = time.time()
                        continue
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        raw = raw.strip()
                        if raw.startswith(b"$"):
                            self.count += 1
                            self.last_line = time.time()
                            self.parser.feed(raw)
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)[:200]
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
            if self.stop_event.is_set():
                break
            time.sleep(2.0)


class SourceManager:
    def __init__(self, state: GPSState, config: dict):
        self.state = state
        self.config = config
        self.mode = config.get("source_mode", "auto")
        self.active = None
        self.active_name = "none"
        self.gpsd = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.last_switch = 0.0
        self.last_health = 0.0
        self.last_repair = 0.0
        self.last_gpsd_try = 0.0
        self.last_error = ""
        self.switches = 0

    def start(self):
        self.thread = threading.Thread(target=self._monitor, name="source-manager", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self._close_active()
        if self.gpsd:
            self.gpsd.stop()

    # ------------------------------------------------------------------
    def set_mode(self, mode: str):
        if mode not in ("auto", "gpsd", "serial"):
            raise ValueError("mode must be auto, gpsd or serial")
        self.mode = mode
        self.config["source_mode"] = mode
        self._force_select()

    def _close_active(self):
        if self.active is not None:
            try:
                self.active.stop()
            except Exception:
                pass
            self.active = None
        if self.gpsd is not None:
            try:
                self.gpsd.stop()
            except Exception:
                pass
            self.gpsd = None

    def _set_active(self, source, name):
        with self.lock:
            if self.active is source:
                return
            old_active, old_gpsd = self.active, self.gpsd
            self.active = None
            self.gpsd = None
            if old_active is not None:
                try:
                    old_active.stop()
                except Exception:
                    pass
            if old_gpsd is not None and old_gpsd is not old_active:
                try:
                    old_gpsd.stop()
                except Exception:
                    pass
            self.active = source
            self.gpsd = source if name == "gpsd" else None
            self.active_name = name
            self.state.clear_sats()
            self.state.set_source(name, "ok", f"source={name}")
            self.last_switch = time.time()
            self.switches += 1
            self.state.log(f"GPS 数据源切换为 {name}", "info")

    def _try_gpsd(self, wait=4.0):
        # A previous direct-serial switch may have stopped the service.
        try:
            from . import hw
            hw.run_ctl("start-gpsd")
        except Exception:
            pass
        client = GpsdClient(self.state)
        client.start()
        deadline = time.time() + wait
        while time.time() < deadline and not self.stop_event.is_set():
            if client.healthy():
                self._set_active(client, "gpsd")
                self.state.set_source("gpsd", "ok", "gpsd JSON / NMEA bridge")
                return True
            time.sleep(0.25)
        client.stop()
        self.last_error = "gpsd 未提供卫星数据"
        return False

    def _try_bridge(self, wait=3.0):
        source = NmeaTcpSource(self.state)
        source.start()
        deadline = time.time() + wait
        while time.time() < deadline and not self.stop_event.is_set():
            if source.healthy():
                self._set_active(source, "bridge")
                self.state.set_source("bridge", "ok", f"NMEA bridge 127.0.0.1:{NMEA_PORT}")
                return True
            time.sleep(0.2)
        source.stop()
        self.last_error = "NMEA bridge 不可用"
        return False

    def _try_serial(self, wait=4.0):
        source = DirectSerialSource(self.state, int(self.config.get("gnss_mode", 7)))
        source.start()
        deadline = time.time() + wait
        while time.time() < deadline and not self.stop_event.is_set():
            if source.healthy():
                self._set_active(source, "serial-direct")
                self.state.set_source("serial-direct", "ok", f"{SERIAL_PORT} @ {BAUD}")
                return True
            time.sleep(0.25)
        source.stop()
        self.last_error = f"无法读取 {SERIAL_PORT}"
        return False

    def _force_select(self):
        with self.lock:
            self._close_active()
            self.state.clear_sats()
            mode = self.mode
            if mode == "gpsd":
                if not self._try_gpsd(wait=5.0):
                    self.state.set_source("gpsd", "error", "gpsd 无数据", self.last_error)
                return
            if mode == "serial":
                if not self._try_bridge(wait=3.0) and not self._try_serial(wait=4.0):
                    self.state.set_source("bridge", "error", "串口无数据", self.last_error)
                return
            # auto
            if self._try_gpsd(wait=5.0):
                return
            if self._try_bridge(wait=3.0):
                return
            if self._try_serial(wait=4.0):
                return
            self.state.set_source("none", "error", "没有可用的 GPS 数据源", self.last_error)

    # ------------------------------------------------------------------
    def _monitor(self):
        time.sleep(1.0)
        self._force_select()
        while not self.stop_event.is_set():
            time.sleep(2.0)
            active = self.active
            if active is None:
                self._force_select()
                continue
            try:
                healthy = active.healthy()
            except Exception:
                healthy = False
            if healthy:
                self.last_health = time.time()
                if self.state.source_status != "ok":
                    self.state.set_source(self.active_name, "ok", "")
                # In AUTO, periodically re-check gpsd while the bridge is
                # active.  A cold-started receiver can take minutes to produce
                # a SKY message; once it does, switch back to gpsd.
                if (self.mode == "auto" and self.active_name == "bridge"
                        and time.time() - self.last_gpsd_try > 120):
                    self.last_gpsd_try = time.time()
                    if self._try_gpsd(wait=3.0):
                        continue
                continue
            age = time.time() - self.last_switch
            if age < 8.0:
                continue
            self.state.set_source(self.active_name, "degraded",
                                  "", getattr(active, "last_error", "") or "数据超时")
            # In AUTO, degrade gracefully.  In explicit modes, keep the chosen
            # source but try a hardware repair after a longer grace period.
            if self.mode == "auto":
                if self.active_name == "gpsd":
                    self.state.log("gpsd 数据超时，切换到 NMEA 桥", "warn")
                    if not self._try_bridge(wait=3.0) and not self._try_serial(wait=4.0):
                        self._force_select()
                elif self.active_name == "bridge":
                    self.state.log("NMEA 桥超时，尝试直接串口", "warn")
                    if not self._try_serial(wait=4.0):
                        self._force_select()
                else:
                    self._force_select()
            else:
                if time.time() - self.last_repair > 30:
                    self.last_repair = time.time()
                    try:
                        from . import hw
                        hw.repair()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    def status(self):
        return {
            "mode": self.mode,
            "active": self.active_name,
            "healthy": self.active.healthy() if self.active else False,
            "last_error": self.last_error,
            "switches": self.switches,
            "gpsd": self.gpsd.status() if self.gpsd else {},
            "source": self.active.status() if hasattr(self.active, "status") else {},
        }
