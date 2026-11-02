"""Deterministic demo feed used for UI development and screenshots."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

from .model import GPSState

SATS = [
    ("GPS", 8, 189, 5, 27), ("GPS", 27, 161, 31, 36),
    ("GPS", 28, 93, 17, 34), ("GPS", 31, 80, 42, 14),
    ("GPS", 18, 305, 55, 41), ("GPS", 22, 240, 22, 31),
    ("GLO", 6, 0, 0, 27), ("GLO", 18, 100, 8, 26),
    ("GLO", 5, 166, 15, 36), ("GLO", 14, 280, 33, 22),
    ("BDS", 41, 169, 9, 36), ("BDS", 38, 210, 38, 30),
    ("BDS", 20, 130, 62, 28), ("GAL", 11, 250, 47, 33),
]


class DemoSource:
    def __init__(self, state: GPSState):
        self.state = state
        self.stop_event = threading.Event()
        self.thread = None
        self.start_time = time.time()

    def start(self):
        self.state.demo = True
        self.state.set_source("demo", "ok", "内置演示轨迹（非真实定位）")
        self.thread = threading.Thread(target=self._run, name="demo", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        lat0, lon0 = 51.50740, -0.12780
        while not self.stop_event.is_set():
            t = time.time() - self.start_time
            # A gentle east-bound walk with a couple of turns.
            lat = lat0 + 0.00035 * math.sin(t / 40.0)
            lon = lon0 + 0.00060 * t / 60.0 + 0.0002 * math.cos(t / 30.0)
            speed = 1.25 + 0.25 * math.sin(t / 12.0)
            track = (90 + 12 * math.sin(t / 18.0)) % 360
            now = datetime.now(timezone.utc)
            self.state.set_position(
                lat=lat, lon=lon, alt=44.2 + 0.8 * math.sin(t / 9.0),
                alt_msl=44.2, geoid=-33.8, speed=speed, track=track,
                climb=0.1 * math.cos(t / 9.0), quality=1, mode=3,
                time_utc=now, hdop=0.9, pdop=1.6, vdop=1.3, gdop=2.0,
                eph=6.8, epv=11.0, sep=13.2, num_sats=len(SATS),
            )
            for system, prn, az, el, snr in SATS:
                self.state.update_sat(system, prn, el=el, az=az, snr=snr,
                                      used=snr >= 26, source="demo")
            self.state.total_used  # touch the lazy used flags
            time.sleep(1.0)
