"""Shared GNSS data model and constellation metadata.

The model is deliberately protocol agnostic: NMEA, UBX and gpsd JSON all
feed the same :class:`GPSState`.  It is safe to update from a reader thread
while the web thread serialises snapshots.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone

# gnssid values follow the u-blox / gpsd convention.
SYSTEMS = {
    "GPS": {
        "gnssid": 0, "zh": "GPS", "full_zh": "全球定位系统",
        "country": "美国", "orbit": "MEO", "color": "#000080",
        "series": "GPS Block II/III",
    },
    "SBAS": {
        "gnssid": 1, "zh": "SBAS", "full_zh": "星基增强系统",
        "country": "多国", "orbit": "GEO", "color": "#7a6a00",
        "series": "WAAS/EGNOS/MSAS",
    },
    "GAL": {
        "gnssid": 2, "zh": "伽利略", "full_zh": "Galileo",
        "country": "欧盟", "orbit": "MEO", "color": "#6a0dad",
        "series": "Galileo FOC/IOV",
    },
    "BDS": {
        "gnssid": 3, "zh": "北斗", "full_zh": "BeiDou",
        "country": "中国", "orbit": "MEO/IGSO/GEO", "color": "#a00000",
        "series": "BeiDou-2/3",
    },
    "IMES": {
        "gnssid": 4, "zh": "IMES", "full_zh": "室内定位",
        "country": "日本", "orbit": "室内", "color": "#555555",
        "series": "IMES",
    },
    "QZSS": {
        "gnssid": 5, "zh": "准天顶", "full_zh": "QZSS",
        "country": "日本", "orbit": "IGSO/GEO", "color": "#006a6a",
        "series": "QZSS",
    },
    "GLO": {
        "gnssid": 6, "zh": "格洛纳斯", "full_zh": "GLONASS",
        "country": "俄罗斯", "orbit": "MEO", "color": "#006400",
        "series": "GLONASS-M/K",
    },
    "NAVIC": {
        "gnssid": 7, "zh": "印度导航", "full_zh": "NavIC",
        "country": "印度", "orbit": "GEO/IGSO", "color": "#b35c00",
        "series": "NavIC",
    },
    "UNKNOWN": {
        "gnssid": -1, "zh": "未知", "full_zh": "未知系统",
        "country": "-", "orbit": "-", "color": "#666666",
        "series": "Unknown",
    },
}

GNSSID_TO_SYSTEM = {v["gnssid"]: k for k, v in SYSTEMS.items() if k != "UNKNOWN"}
TALKER_TO_SYSTEM = {
    "GP": "GPS", "GL": "GLO", "BD": "BDS", "GB": "BDS",
    "GA": "GAL", "GQ": "QZSS", "GS": "SBAS", "GI": "NAVIC",
    "GN": "MIX", "GZ": "MIX",
}

SYSTEM_ORDER = ["GPS", "GLO", "BDS", "GAL", "QZSS", "NAVIC", "SBAS", "IMES", "UNKNOWN"]


def sat_series(system: str, prn: int | None) -> str:
    """Return a human readable satellite family/block for a PRN."""
    if prn is None:
        return SYSTEMS.get(system, SYSTEMS["UNKNOWN"])["series"]
    if system == "GPS":
        if prn >= 65:
            return "GPS III/IIIF"
        if prn >= 33:
            return "GPS IIR-M/IIF"
        return "GPS Block II/III"
    if system == "GLO":
        return "GLONASS-M/K"
    if system == "BDS":
        if 1 <= prn <= 5:
            return "BeiDou-2 GEO"
        if 6 <= prn <= 10:
            return "BeiDou-2 IGSO"
        if 11 <= prn <= 30:
            return "BeiDou-2/3 MEO"
        return "BeiDou-3"
    if system == "GAL":
        return "Galileo FOC/IOV"
    if system == "QZSS":
        return "QZSS IGSO/GEO"
    if system == "SBAS":
        return "SBAS GEO"
    if system == "NAVIC":
        return "NavIC GEO/IGSO"
    return SYSTEMS.get(system, SYSTEMS["UNKNOWN"])["series"]


def system_from_gnssid(gnssid: int | None) -> str:
    if gnssid is None:
        return "UNKNOWN"
    return GNSSID_TO_SYSTEM.get(int(gnssid), "UNKNOWN")


def _round(value, digits=6):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


class GPSState:
    """Thread safe current GNSS state plus a short breadcrumb trail."""

    def __init__(self, max_track: int = 3000):
        self.lock = threading.RLock()
        self.sats: dict[tuple[str, int], dict] = {}
        self.pending_used: dict[tuple[str, int], float] = {}
        self.track: deque = deque(maxlen=max_track)
        self.last_track_ts = 0.0
        self.last_track_pos = None
        self.source = "starting"
        self.source_detail = ""
        self.source_mode = "auto"
        self.source_status = "init"
        self.source_error = ""
        self.antenna = ""
        self.fix_quality = 0
        self.fix_mode = 0
        self.lat = None
        self.lon = None
        self.alt = None
        self.alt_msl = None
        self.geoid = None
        self.speed = None
        self.track_deg = None
        self.climb = None
        self.hdop = None
        self.pdop = None
        self.vdop = None
        self.gdop = None
        self.tdop = None
        self.eph = None
        self.epv = None
        self.sep = None
        self.acc1 = None
        self.acc2 = None
        self.num_sats = 0
        self.used_sats = 0
        self.time_utc: datetime | None = None
        self.last_update = 0.0
        self.last_fix = 0.0
        self.last_fix_lat = None
        self.last_fix_lon = None
        self.last_fix_alt = None
        self.last_sky = 0.0
        self.raw_last = ""
        self.raw_count = 0
        self.bad_checksums = 0
        self.demo = False
        self.messages: deque = deque(maxlen=80)
        self.ubx_last = 0.0

    # ------------------------------------------------------------------
    # messages / diagnostics
    def log(self, text: str, level: str = "info"):
        with self.lock:
            self.messages.append({
                "ts": time.time(),
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "text": str(text)[:500],
            })

    def set_source(self, name: str, status: str = "ok", detail: str = "", error: str = ""):
        with self.lock:
            self.source = name
            self.source_status = status
            self.source_detail = detail
            self.source_error = error
            if error:
                self.log(f"{name}: {error}", "error")

    # ------------------------------------------------------------------
    # position / fix
    def set_position(self, lat=None, lon=None, alt=None, alt_msl=None, geoid=None,
                     speed=None, track=None, climb=None, quality=None, mode=None,
                     time_utc=None, hdop=None, pdop=None, vdop=None, gdop=None,
                     tdop=None, eph=None, epv=None, sep=None, num_sats=None):
        with self.lock:
            now = time.time()
            if quality is not None:
                self.fix_quality = int(quality)
            if mode is not None:
                self.fix_mode = int(mode)
            if lat is not None and lon is not None:
                # Reject the CASIC power-on placeholder and impossible values.
                if -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0:
                    if not (abs(float(lat)) < 1e-7 and abs(float(lon)) < 1e-7):
                        self.lat = float(lat)
                        self.lon = float(lon)
            if alt is not None:
                self.alt = float(alt)
            if alt_msl is not None:
                self.alt_msl = float(alt_msl)
            if geoid is not None:
                self.geoid = float(geoid)
            if speed is not None:
                self.speed = max(0.0, float(speed))
            if track is not None:
                self.track_deg = float(track) % 360.0
            if climb is not None:
                self.climb = float(climb)
            if time_utc is not None:
                self.time_utc = time_utc
            if hdop is not None:
                self.hdop = float(hdop)
            if pdop is not None:
                self.pdop = float(pdop)
            if vdop is not None:
                self.vdop = float(vdop)
            if gdop is not None:
                self.gdop = float(gdop)
            if tdop is not None:
                self.tdop = float(tdop)
            if eph is not None:
                self.eph = float(eph)
            if epv is not None:
                self.epv = float(epv)
            if sep is not None:
                self.sep = float(sep)
            if num_sats is not None:
                self.num_sats = int(num_sats)
            self.last_update = now
            # Accuracy: prefer explicit gpsd/GST values, else HDOP * UERE.
            if self.eph and self.eph > 0:
                self.acc1 = self.eph
            elif self.hdop and self.fix_mode >= 2 and 0 < self.hdop < 50:
                self.acc1 = max(2.0, self.hdop * 5.0)
            if self.acc1:
                self.acc2 = self.acc1 * 1.96
            if self.fix_mode >= 2 and self.lat is not None:
                self.last_fix = now
                self.last_fix_lat = self.lat
                self.last_fix_lon = self.lon
                self.last_fix_alt = self.alt
                self._append_track(now, self.lat, self.lon)

    def _append_track(self, now: float, lat: float, lon: float):
        if self.last_track_pos is None:
            add = True
        else:
            dlat = (lat - self.last_track_pos[0]) * 111320.0
            dlon = (lon - self.last_track_pos[1]) * 111320.0 * math.cos(math.radians(lat))
            dist = math.hypot(dlat, dlon)
            add = dist >= 1.0 or (now - self.last_track_ts) >= 8.0
        if add:
            self.track.append([round(lat, 7), round(lon, 7), round(now, 2)])
            self.last_track_pos = (lat, lon)
            self.last_track_ts = now

    # ------------------------------------------------------------------
    # satellites
    def update_sat(self, system: str, prn: int, el=None, az=None, snr=None,
                   used=None, sig=None, source="nmea"):
        if system not in SYSTEMS:
            system = "UNKNOWN"
        try:
            prn = int(prn)
        except (TypeError, ValueError):
            return
        if prn <= 0:
            return
        now = time.time()
        key = (system, prn)
        with self.lock:
            sat = self.sats.get(key)
            if sat is None:
                sat = {
                    "system": system, "prn": prn, "el": None, "az": None,
                    "snr": None, "used": False, "sig": None, "last_seen": now,
                    "first_seen": now, "used_ts": 0.0, "series": sat_series(system, prn),
                }
                self.sats[key] = sat
            sat["last_seen"] = now
            if el is not None and el != "":
                try:
                    sat["el"] = max(0.0, min(90.0, float(el)))
                except ValueError:
                    pass
            if az is not None and az != "":
                try:
                    sat["az"] = float(az) % 360.0
                except ValueError:
                    pass
            if snr is not None and snr != "":
                try:
                    sat["snr"] = max(0.0, min(99.0, float(snr)))
                except ValueError:
                    pass
            if sig is not None:
                sat["sig"] = sig
            if used is True:
                sat["used"] = True
                sat["used_ts"] = now
            elif used is False and key not in self.pending_used:
                sat["used"] = False
            pending = self.pending_used.get(key)
            if pending and now - pending < 8.0:
                sat["used"] = True
                sat["used_ts"] = max(sat["used_ts"], pending)
            elif pending:
                self.pending_used.pop(key, None)

    def mark_used(self, system: str, prns, timestamp=None):
        ts = timestamp or time.time()
        with self.lock:
            for prn in prns:
                try:
                    key = (system, int(prn))
                except (TypeError, ValueError):
                    continue
                self.pending_used[key] = ts
                sat = self.sats.get(key)
                if sat is not None:
                    sat["used"] = True
                    sat["used_ts"] = ts
            # Garbage collect stale pending entries.
            for key, when in list(self.pending_used.items()):
                if time.time() - when > 30.0:
                    self.pending_used.pop(key, None)

    def clear_sats(self):
        with self.lock:
            self.sats.clear()
            self.pending_used.clear()

    def _active_sats(self, now=None):
        now = now or time.time()
        out = []
        for sat in self.sats.values():
            if now - sat["last_seen"] <= 15.0:
                if sat["used"] and now - sat.get("used_ts", 0) > 6.0:
                    sat["used"] = False
                out.append(sat)
        out.sort(key=lambda s: (SYSTEM_ORDER.index(s["system"]) if s["system"] in SYSTEM_ORDER else 99,
                                s["prn"]))
        return out

    # ------------------------------------------------------------------
    @property
    def total_in_view(self) -> int:
        return len(self._active_sats())

    @property
    def total_used(self) -> int:
        return sum(1 for sat in self._active_sats() if sat["used"])

    def counts(self):
        active = self._active_sats()
        counts = {k: 0 for k in SYSTEM_ORDER}
        used = {k: 0 for k in SYSTEM_ORDER}
        for sat in active:
            counts[sat["system"]] = counts.get(sat["system"], 0) + 1
            if sat["used"]:
                used[sat["system"]] = used.get(sat["system"], 0) + 1
        return counts, used

    def snapshot(self, cache=None, bridge=None, gpsd=None, system=None, config=None):
        with self.lock:
            now = time.time()
            active = self._active_sats(now)
            counts, used_counts = self.counts()
            used_total = sum(1 for s in active if s["used"])
            if used_total:
                self.used_sats = used_total
            fix_age = (now - self.last_fix) if self.last_fix else None
            age = (now - self.last_update) if self.last_update else None
            quality = self.fix_quality
            fix_text = {
                0: "无定位", 1: "GPS 定位", 2: "DGPS 定位", 3: "PPS 定位",
                4: "RTK 固定", 5: "RTK 浮点", 6: "推算", 7: "手动", 8: "模拟",
            }.get(quality, "未知")
            if self.fix_mode >= 3:
                mode_text = "3D 定位"
            elif self.fix_mode == 2:
                mode_text = "2D 定位"
            elif self.fix_mode == 1:
                mode_text = "无定位"
            else:
                mode_text = "搜索卫星"
            if self.fix_mode < 2:
                fix_text = mode_text
            sats = []
            for sat in active:
                item = dict(sat)
                item["series"] = sat_series(sat["system"], sat["prn"])
                item["system_zh"] = SYSTEMS.get(sat["system"], SYSTEMS["UNKNOWN"])["zh"]
                item["color"] = SYSTEMS.get(sat["system"], SYSTEMS["UNKNOWN"])["color"]
                sats.append(item)
            return {
                "ts": now,
                "version": "1.0.0",
                "demo": self.demo,
                "source": {
                    "name": self.source,
                    "mode": self.source_mode,
                    "status": self.source_status,
                    "detail": self.source_detail,
                    "error": self.source_error,
                },
                "fix": {
                    "lat": _round(self.lat, 7),
                    "lon": _round(self.lon, 7),
                    "alt": _round(self.alt, 1),
                    "alt_msl": _round(self.alt_msl, 1),
                    "geoid": _round(self.geoid, 1),
                    "speed": _round(self.speed, 2),
                    "track": _round(self.track_deg, 1),
                    "climb": _round(self.climb, 2),
                    "quality": quality,
                    "quality_text": fix_text,
                    "mode": self.fix_mode,
                    "mode_text": mode_text,
                    "hdop": _round(self.hdop, 2),
                    "pdop": _round(self.pdop, 2),
                    "vdop": _round(self.vdop, 2),
                    "gdop": _round(self.gdop, 2),
                    "tdop": _round(self.tdop, 2),
                    "eph": _round(self.eph, 1),
                    "epv": _round(self.epv, 1),
                    "sep": _round(self.sep, 1),
                    "acc1": _round(self.acc1, 1),
                    "acc2": _round(self.acc2, 1),
                    "num_sats": self.num_sats,
                    "used_sats": used_total,
                    "time": self.time_utc.isoformat().replace("+00:00", "Z") if self.time_utc else None,
                    "age": _round(age, 2),
                    "fix_age": _round(fix_age, 2),
                    "last_fix_lat": _round(self.last_fix_lat, 7),
                    "last_fix_lon": _round(self.last_fix_lon, 7),
                    "last_fix_alt": _round(self.last_fix_alt, 1),
                },
                "sats": sats,
                "counts": counts,
                "used_counts": used_counts,
                "total_in_view": len(sats),
                "total_used": used_total,
                "antenna": self.antenna,
                "track": list(self.track)[-600:],
                "cache": cache or {},
                "bridge": bridge or {},
                "gpsd": gpsd or {},
                "system": system or {},
                "config": config or {},
                "messages": list(self.messages),
                "ubx_last": _round(self.ubx_last, 2),
            }

    def last_raw(self):
        with self.lock:
            return self.raw_last
