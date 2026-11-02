"""gpsd JSON client (TCP 127.0.0.1:2947)."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timezone

from .model import GPSState, system_from_gnssid


class GpsdClient:
    def __init__(self, state: GPSState, host="127.0.0.1", port=2947):
        self.state = state
        self.host = host
        self.port = port
        self.sock = None
        self.thread = None
        self.stop_event = threading.Event()
        self.connected = False
        self.last_error = ""
        self.last_tpv = 0.0
        self.last_sky = 0.0
        self.version = ""
        self.devices = []
        self.last_line = ""
        self._lock = threading.Lock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="gpsd-client", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        if self.thread:
            self.thread.join(timeout=2)

    # ------------------------------------------------------------------
    def healthy(self) -> bool:
        now = time.time()
        if not self.connected:
            return False
        if now - self.last_tpv > 4.0:
            return False
        # A TPV without any SKY satellites is the known gpsd/GP-02 failure.
        if now - self.last_sky > 8.0:
            return False
        # Without a fix gpsd often emits an empty SKY for this NMEA stream;
        # the bridge parser is more complete in that state.  Use gpsd only
        # when it can provide both a fix and satellites.
        if self.state.fix_mode < 2:
            return False
        if self.state.total_used <= 0 and self.state.total_in_view <= 0:
            return False
        return True

    def status(self):
        return {
            "connected": self.connected,
            "version": self.version,
            "devices": self.devices,
            "last_tpv_age": round(time.time() - self.last_tpv, 2) if self.last_tpv else None,
            "last_sky_age": round(time.time() - self.last_sky, 2) if self.last_sky else None,
            "error": self.last_error,
            "satellites": self.state.total_in_view,
        }

    # ------------------------------------------------------------------
    def _run(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._connect_and_read()
                backoff = 1.0
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)[:300]
                if not self.stop_event.is_set():
                    self.state.set_source("gpsd", "error", "gpsd 连接中断", self.last_error)
                time.sleep(backoff)
                backoff = min(8.0, backoff * 1.7)

    def _connect_and_read(self):
        sock = socket.create_connection((self.host, self.port), timeout=3)
        sock.settimeout(1.0)
        self.sock = sock
        self.connected = True
        self.last_error = ""
        sock.sendall(b'?WATCH={"enable":true,"json":true,"nmea":false,"raw":0};\n')
        buf = b""
        while not self.stop_event.is_set():
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("gpsd closed the connection")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle_line(line.decode("utf-8", "replace").strip())

    def _handle_line(self, line):
        if not line:
            return
        self.last_line = line
        if line.startswith("{"):
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                return
            cls = msg.get("class")
            if cls == "VERSION":
                self.version = msg.get("release", "")
            elif cls == "DEVICES":
                self.devices = msg.get("devices", [])
            elif cls == "TPV":
                self._tpv(msg)
            elif cls == "SKY":
                self._sky(msg)
            elif cls == "GST":
                self._gst(msg)

    def _tpv(self, msg):
        mode = int(msg.get("mode") or 0)
        lat, lon = msg.get("lat"), msg.get("lon")
        quality = 0
        if mode >= 3:
            quality = 1
        if msg.get("status") == 2:
            quality = 2
        t = None
        if msg.get("time"):
            try:
                t = datetime.fromisoformat(msg["time"].replace("Z", "+00:00"))
            except ValueError:
                t = None
        self.state.set_position(
            lat=lat, lon=lon, alt=msg.get("altMSL", msg.get("alt")),
            alt_msl=msg.get("altMSL", msg.get("alt")),
            geoid=msg.get("geoidSep"), speed=msg.get("speed"),
            track=msg.get("track"), climb=msg.get("climb"),
            quality=quality, mode=mode, time_utc=t,
            hdop=self.state.hdop, pdop=self.state.pdop, vdop=self.state.vdop,
            eph=msg.get("eph"), epv=msg.get("epv"), sep=msg.get("sep"),
        )
        self.last_tpv = time.time()
        self.state.last_update = self.last_tpv

    def _sky(self, msg):
        sats = msg.get("satellites") or []
        now = time.time()
        for sat in sats:
            gnssid = sat.get("gnssid")
            system = system_from_gnssid(gnssid)
            # For BeiDou gpsd's PRN is 400+svid; the SVID is what users expect.
            svid = sat.get("svid")
            prn = sat.get("PRN")
            if system == "BDS" and svid:
                prn = svid
            elif svid:
                prn = svid
            self.state.update_sat(
                system, prn, el=sat.get("el"), az=sat.get("az"),
                snr=sat.get("ss"), used=bool(sat.get("used")),
                sig=sat.get("sigid"), source="gpsd",
            )
        hdop, pdop, vdop = msg.get("hdop"), msg.get("pdop"), msg.get("vdop")
        gdop, tdop = msg.get("gdop"), msg.get("tdop")
        self.state.set_position(hdop=hdop, pdop=pdop, vdop=vdop, gdop=gdop, tdop=tdop)
        if msg.get("nSat") is not None:
            self.state.num_sats = int(msg["nSat"])
        self.last_sky = now
        self.state.last_sky = now

    def _gst(self, msg):
        lat_sigma = msg.get("lat")
        lon_sigma = msg.get("lon")
        if isinstance(lat_sigma, (int, float)) and isinstance(lon_sigma, (int, float)):
            import math
            self.state.set_position(eph=math.hypot(lat_sigma, lon_sigma))
