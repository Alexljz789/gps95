"""NMEA 0183 parser for GPS, GLONASS, BeiDou and Galileo receivers.

The Ai-Thinker GP-02 on the uConsole AIO v2 emits a mixed stream: standard
NMEA plus periodic UBX-looking binary frames and, after a gpsd autoconfig,
sometimes only binary frames.  This parser only consumes complete ``$``
sentences, so binary noise is harmless.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone

from .model import GPSState, TALKER_TO_SYSTEM

_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def checksum(line: str) -> str:
    value = 0
    for ch in line:
        value ^= ord(ch)
    return f"{value:02X}"


def checksum_ok(sentence: str) -> bool:
    """Validate ``$...*hh``.  Sentences without a checksum are accepted."""
    if not sentence.startswith("$"):
        return False
    star = sentence.rfind("*")
    if star < 0:
        return len(sentence) > 6
    body = sentence[1:star]
    if star + 3 > len(sentence):
        return False
    return checksum(body) == sentence[star + 1:star + 3].upper()


def _f(value, default=None):
    if value in (None, "", "None"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_latlon(value: str, hemi: str, degree_digits: int):
    """Parse ``ddmm.mmmm`` / ``dddmm.mmmm`` into signed decimal degrees."""
    value = (value or "").strip()
    hemi = (hemi or "").strip().upper()
    if not value or hemi not in ("N", "S", "E", "W"):
        return None
    try:
        raw = float(value)
    except ValueError:
        return None
    deg = int(raw // 100)
    minutes = raw - deg * 100
    if minutes >= 60:
        return None
    result = deg + minutes / 60.0
    if hemi in ("S", "W"):
        result = -result
    return result


def parse_utc(hhmmss: str, ddmmyy: str | None = None):
    hhmmss = (hhmmss or "").strip()
    if len(hhmmss) < 6:
        return None
    try:
        hh = int(hhmmss[0:2])
        mm = int(hhmmss[2:4])
        ss = float(hhmmss[4:])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss < 61):
        return None
    date = None
    if ddmmyy and len(ddmmyy) >= 6:
        try:
            day = int(ddmmyy[0:2])
            month = int(ddmmyy[2:4])
            year = int(ddmmyy[4:6])
            year += 2000 if year < 80 else 1900
            date = (year, month, day)
        except ValueError:
            date = None
    if date is None:
        today = datetime.now(timezone.utc)
        date = (today.year, today.month, today.day)
    try:
        base = datetime(date[0], date[1], date[2], tzinfo=timezone.utc)
        return base + timedelta(hours=hh, minutes=mm, seconds=ss)
    except ValueError:
        return None


def talker_system(talker: str) -> str:
    return TALKER_TO_SYSTEM.get((talker or "")[:2].upper(), "UNKNOWN")


class NMEAParser:
    """Stateful NMEA parser feeding a :class:`GPSState`."""

    def __init__(self, state: GPSState):
        self.state = state
        self.last_rmc_date = None
        self.last_gga_time = None
        self.last_gsv_time = 0.0
        self.last_gsa_time = 0.0
        self.gsv_seen: dict[tuple[str, int], float] = {}
        self.sentences = 0
        self.bad = 0

    # ------------------------------------------------------------------
    def feed(self, raw, source: str = "nmea"):
        if isinstance(raw, bytes):
            text = raw.decode("ascii", "replace")
        else:
            text = str(raw)
        text = text.strip("\r\n\x00 ")
        if not text.startswith("$"):
            return False
        self.state.raw_last = text
        self.state.raw_count += 1
        if not checksum_ok(text):
            self.bad += 1
            self.state.bad_checksums += 1
            return False
        star = text.rfind("*")
        body = text[1:star] if star > 0 else text[1:]
        fields = body.split(",")
        if len(fields) < 2:
            return False
        head = fields[0]
        if len(head) < 5:
            return False
        talker = head[:2].upper()
        kind = head[2:5].upper()
        self.sentences += 1
        try:
            if kind == "GGA":
                self._gga(talker, fields)
            elif kind == "RMC":
                self._rmc(talker, fields)
            elif kind == "GSA":
                self._gsa(talker, fields)
            elif kind == "GSV":
                self._gsv(talker, fields)
            elif kind == "GLL":
                self._gll(talker, fields)
            elif kind == "VTG":
                self._vtg(talker, fields)
            elif kind == "ZDA":
                self._zda(talker, fields)
            elif kind == "GST":
                self._gst(talker, fields)
            elif kind == "GNS":
                self._gns(talker, fields)
            elif kind == "TXT":
                self._txt(talker, fields)
        except Exception as exc:  # never let one malformed sentence kill the reader
            self.state.log(f"NMEA {kind} parse error: {exc}", "warn")
            return False
        return True

    # ------------------------------------------------------------------
    def _gga(self, talker, f):
        if len(f) < 10:
            return
        utc = parse_utc(f[1], self.last_rmc_date)
        lat = parse_latlon(f[2], f[3], 2)
        lon = parse_latlon(f[4], f[5], 3)
        quality = _i(f[6], 0) or 0
        num = _i(f[7], 0) or 0
        hdop = _f(f[8])
        alt = _f(f[9])
        geoid = _f(f[11]) if len(f) > 11 else None
        self.last_gga_time = utc
        mode = 0
        if quality in (4, 5):
            mode = 3
        elif quality in (1, 2, 3, 6):
            mode = 3 if lat is not None and lon is not None else 1
        if lat is not None and lon is not None and quality not in (0,):
            self.state.set_position(
                lat=lat, lon=lon, alt=alt, alt_msl=alt, geoid=geoid,
                quality=quality, mode=mode, time_utc=utc, hdop=hdop,
                num_sats=num,
            )
        else:
            self.state.set_position(quality=quality, mode=0, time_utc=utc,
                                    hdop=hdop, num_sats=num)

    def _rmc(self, talker, f):
        if len(f) < 10:
            return
        status = (f[2] or "").upper()
        lat = parse_latlon(f[3], f[4], 2)
        lon = parse_latlon(f[5], f[6], 3)
        speed_knots = _f(f[7])
        track = _f(f[8])
        date = f[9]
        if date:
            self.last_rmc_date = date
        utc = parse_utc(f[1], date)
        mode = 3 if status == "A" and lat is not None and lon is not None else 0
        # RMC alone is enough for a 2D/3D fix (many receivers omit GGA).
        if status == "A" and lat is not None and lon is not None:
            self.state.set_position(
                lat=lat, lon=lon,
                speed=(speed_knots * 0.514444) if speed_knots is not None else None,
                track=track, mode=mode, time_utc=utc,
            )

    def _gsa(self, talker, f):
        if len(f) < 18:
            return
        mode = (f[1] or "").upper()
        fix = _i(f[2], 1) or 1
        prns = []
        for value in f[3:15]:
            prn = _i(value)
            if prn and prn > 0:
                prns.append(prn)
        pdop = _f(f[15])
        hdop = _f(f[16])
        vdop = _f(f[17])
        system = talker_system(talker)
        # NMEA 4.1 adds a system id after VDOP; it disambiguates GNGSA.
        sysid = _i(f[18]) if len(f) > 18 else None
        if system == "MIX" and sysid:
            system = {1: "GPS", 2: "GLO", 3: "GAL", 4: "BDS", 5: "QZSS", 6: "NAVIC"}.get(sysid, "UNKNOWN")
        if system == "GLO":
            prns = [p - 64 if p > 64 else p for p in prns]
        if system not in ("MIX", "UNKNOWN") and prns:
            self.state.mark_used(system, prns)
        elif system in ("MIX", "UNKNOWN") and prns:
            # Without a system id, match the PRN against every visible system;
            # this is a best effort and gpsd's SKY data is authoritative.
            for sys in ("GPS", "GLO", "BDS", "GAL", "QZSS", "NAVIC"):
                self.state.mark_used(sys, prns)
        self.last_gsa_time = time.time()
        if fix in (2, 3):
            self.state.set_position(mode=fix, pdop=pdop, hdop=hdop, vdop=vdop)
        else:
            self.state.set_position(pdop=pdop, hdop=hdop, vdop=vdop)

    def _gsv(self, talker, f):
        if len(f) < 4:
            return
        total = _i(f[1], 0) or 0
        number = _i(f[2], 0) or 0
        system = talker_system(talker)
        if system in ("MIX", "UNKNOWN"):
            return
        # Each GSV carries up to four (PRN, elevation, azimuth, SNR) groups.
        for idx in range(4, len(f) - 3, 4):
            prn = _i(f[idx])
            if not prn:
                continue
            if system == "GLO" and prn > 64:
                prn -= 64
            el = _f(f[idx + 1])
            az = _f(f[idx + 2])
            snr = _f(f[idx + 3])
            self.state.update_sat(system, prn, el=el, az=az, snr=snr, source="nmea")
            self.gsv_seen[(system, prn)] = time.time()
        self.last_gsv_time = time.time()
        self.state.last_sky = self.last_gsv_time

    def _gll(self, talker, f):
        if len(f) < 7:
            return
        lat = parse_latlon(f[1], f[2], 2)
        lon = parse_latlon(f[3], f[4], 3)
        status = (f[6] or "").upper()
        if status == "A" and lat is not None and lon is not None:
            utc = parse_utc(f[5], self.last_rmc_date)
            self.state.set_position(lat=lat, lon=lon, mode=3, time_utc=utc)

    def _vtg(self, talker, f):
        if len(f) < 8:
            return
        track = _f(f[1])
        speed_kmh = _f(f[7])
        if speed_kmh is not None:
            self.state.set_position(speed=speed_kmh / 3.6)
        if track is not None:
            self.state.set_position(track=track)

    def _zda(self, talker, f):
        if len(f) < 5:
            return
        try:
            day, month, year = int(f[2]), int(f[3]), int(f[4])
            hhmmss = f[1]
            hh, mm = int(hhmmss[0:2]), int(hhmmss[2:4])
            ss = float(hhmmss[4:])
            utc = datetime(year, month, day, hh, mm, int(ss),
                           int((ss % 1) * 1e6), tzinfo=timezone.utc)
            self.state.set_position(time_utc=utc)
        except (ValueError, IndexError):
            pass

    def _gst(self, talker, f):
        # $GPGST,hhmmss,rangeRms,stdMajor,stdMinor,orient,stdLat,stdLon,stdAlt
        if len(f) < 9:
            return
        lat_sigma = _f(f[6])
        lon_sigma = _f(f[7])
        if lat_sigma and lon_sigma:
            eph = math.hypot(lat_sigma, lon_sigma)
            self.state.set_position(eph=eph)
        alt_sigma = _f(f[8])
        if alt_sigma:
            self.state.set_position(epv=alt_sigma)

    def _gns(self, talker, f):
        # $GNGNS,time,lat,N,lon,E,mode,numSV,HDOP,alt,geoid,...
        if len(f) < 8:
            return
        mode = f[6] or ""
        if "N" in mode or not mode:
            return
        lat = parse_latlon(f[2], f[3], 2)
        lon = parse_latlon(f[4], f[5], 3)
        if lat is None or lon is None:
            return
        num = sum(1 for ch in mode if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.state.set_position(
            lat=lat, lon=lon, mode=3,
            num_sats=num or None,
            hdop=_f(f[8]),
            alt=_f(f[9]),
        )

    def _txt(self, talker, f):
        text = ",".join(f[1:]).upper()
        if "ANTENNA" in text:
            if "OPEN" in text:
                self.state.antenna = "ANTENNA OPEN"
            elif "SHORT" in text:
                self.state.antenna = "ANTENNA SHORT"
            elif "OK" in text:
                self.state.antenna = "ANTENNA OK"
            else:
                self.state.antenna = text[:80]
