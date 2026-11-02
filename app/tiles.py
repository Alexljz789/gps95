"""Server-side raster tile cache with bounded prefetch.

The browser never talks to OpenStreetMap directly.  Every tile goes through
this cache, which means the map keeps working with no network as long as the
area was viewed or explicitly prefetched.  The cache is a plain directory of
PNG files plus a small SQLite index for statistics and LRU eviction.
"""

from __future__ import annotations

import io
import math
import os
import shutil
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

DEFAULT_UPSTREAM = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
USER_AGENT = "GPS95/1.0 (ClockworkPi uConsole; offline map cache)"


def _lon_to_tile(lon, z):
    n = 2.0 ** z
    return int((lon + 180.0) / 360.0 * n)


def _lat_to_tile(lat, z):
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 2.0 ** z
    rad = math.radians(lat)
    return int((1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n)


def _clamp_tile(v, z):
    n = 2 ** z
    return max(0, min(n - 1, int(v)))


class TileCache:
    def __init__(self, root="/var/lib/gps95/tiles", db_path="/var/lib/gps95/tiles.db",
                 upstream=DEFAULT_UPSTREAM, max_mb=512):
        self.root = root
        self.db_path = db_path
        self.upstream = upstream or DEFAULT_UPSTREAM
        self.max_bytes = int(max_mb) * 1024 * 1024
        self.lock = threading.RLock()
        self.prefetch_lock = threading.Lock()
        self.prefetch_state = {
            "running": False, "done": 0, "total": 0, "errors": 0,
            "started": 0.0, "finished": 0.0, "message": "", "cancel": False,
        }
        self.online = None
        self.last_fetch_error = ""
        self.hits = 0
        self.misses = 0
        self.fetches = 0
        self.nodata = self._make_nodata()
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        try:
            self.evict()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self.lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tiles (
                    z INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
                    last_access REAL NOT NULL DEFAULT 0, hits INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '', PRIMARY KEY (z, x, y)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_access ON tiles(last_access)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
            """)

    @staticmethod
    def _make_nodata():
        # Keep the fallback tile as a static asset so the server has no Pillow
        # dependency (the uConsole image is intentionally minimal).
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "img", "nodata.png")
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except Exception:
            return b""

    # ------------------------------------------------------------------
    def _path(self, z, x, y):
        return os.path.join(self.root, str(z), str(x), f"{y}.png")

    def _record_hit(self, z, x, y):
        try:
            with self.lock, self._conn() as conn:
                conn.execute(
                    "UPDATE tiles SET hits=hits+1,last_access=? WHERE z=? AND x=? AND y=?",
                    (time.time(), z, x, y),
                )
        except Exception:
            pass

    def _record_tile(self, z, x, y, size):
        try:
            with self.lock, self._conn() as conn:
                conn.execute("""
                    INSERT INTO tiles(z,x,y,size,mtime,last_access,hits,source)
                    VALUES(?,?,?,?,?,?,0,?)
                    ON CONFLICT(z,x,y) DO UPDATE SET
                        size=excluded.size, mtime=excluded.mtime,
                        last_access=excluded.last_access, source=excluded.source
                """, (z, x, y, size, time.time(), time.time(), self.upstream))
        except Exception:
            pass

    # ------------------------------------------------------------------
    def get_tile(self, z, x, y, offline=False):
        try:
            z, x, y = int(z), int(x), int(y)
        except (TypeError, ValueError):
            return None, "image/png", "bad-request"
        if z < 0 or z > 19:
            return None, "image/png", "bad-zoom"
        n = 2 ** z
        if not (0 <= x < n and 0 <= y < n):
            return None, "image/png", "bad-tile"
        path = self._path(z, x, y)
        if os.path.exists(path) and os.path.getsize(path) > 32:
            self.hits += 1
            self._record_hit(z, x, y)
            with open(path, "rb") as fh:
                data = fh.read()
            return data, self._mime(data), "hit"
        if offline:
            self.misses += 1
            return self.nodata, "image/png", "miss-offline"
        subdomain = "abcd"[abs(x + y + z) % 4]
        try:
            url = self.upstream.format(z=z, x=x, y=y, s=subdomain)
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/png,image/jpeg,image/*;q=0.8",
                "Referer": "http://127.0.0.1:8765/",
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read(3 * 1024 * 1024 + 1)
                ctype = resp.headers.get("Content-Type", "")
            if len(data) > 3 * 1024 * 1024:
                raise ValueError("tile too large")
            if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
                raise ValueError(f"not an image ({ctype})")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            self._record_tile(z, x, y, len(data))
            self.fetches += 1
            self.misses += 1
            self.online = True
            self.last_fetch_error = ""
            if self.fetches % 40 == 0:
                self.evict()
            return data, self._mime(data), "miss"
        except Exception as exc:
            self.last_fetch_error = str(exc)[:200]
            self.online = False
            self.misses += 1
            return self.nodata, "image/png", "miss-offline"

    @staticmethod
    def _mime(data):
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        return "image/png"

    # ------------------------------------------------------------------
    def prefetch(self, south, west, north, east, minz, maxz, max_tiles=450):
        with self.prefetch_lock:
            if self.prefetch_state["running"]:
                return False, "已有缓存任务在运行"
            if not (-85 <= south < north <= 85) or not (-180 <= west < east <= 180):
                return False, "地图范围无效"
            minz, maxz = max(3, int(minz)), min(19, int(maxz))
            if maxz < minz:
                return False, "缩放级别无效"
            tiles = []
            for z in range(minz, maxz + 1):
                x0, x1 = _lon_to_tile(west, z), _lon_to_tile(east, z)
                y0, y1 = _lat_to_tile(north, z), _lat_to_tile(south, z)
                x0, x1 = sorted((_clamp_tile(x0, z), _clamp_tile(x1, z)))
                y0, y1 = sorted((_clamp_tile(y0, z), _clamp_tile(y1, z)))
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        tiles.append((z, x, y))
                        if len(tiles) > max_tiles * 2:
                            break
                    if len(tiles) > max_tiles * 2:
                        break
                if len(tiles) > max_tiles * 2:
                    break
            if not tiles:
                return False, "地图范围没有瓦片"
            if len(tiles) > max_tiles:
                return False, f"范围过大（{len(tiles)} 块），请缩小范围或降低层级（上限 {max_tiles}）"
            self.prefetch_state = {
                "running": True, "done": 0, "total": len(tiles), "errors": 0,
                "started": time.time(), "finished": 0.0,
                "message": "正在缓存地图…", "cancel": False,
            }
        threading.Thread(target=self._prefetch_worker, args=(tiles,), daemon=True).start()
        return True, f"开始缓存 {len(tiles)} 块瓦片"

    def _prefetch_worker(self, tiles):
        errors = 0
        for idx, (z, x, y) in enumerate(tiles, 1):
            if self.prefetch_state.get("cancel"):
                self.prefetch_state["message"] = "缓存已取消"
                break
            path = self._path(z, x, y)
            if not (os.path.exists(path) and os.path.getsize(path) > 32):
                data, mime, state = self.get_tile(z, x, y, offline=False)
                if state.startswith("miss-offline"):
                    errors += 1
                time.sleep(0.04)
            self.prefetch_state["done"] = idx
            self.prefetch_state["errors"] = errors
        self.prefetch_state["running"] = False
        self.prefetch_state["finished"] = time.time()
        if not self.prefetch_state.get("message") or self.prefetch_state["message"].startswith("正在"):
            self.prefetch_state["message"] = (
                f"缓存完成：{self.prefetch_state['done'] - errors}/{self.prefetch_state['total']} 块"
            )

    def cancel_prefetch(self):
        self.prefetch_state["cancel"] = True
        return True

    # ------------------------------------------------------------------
    def stats(self):
        files = 0
        size = 0
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                if name.endswith(".png"):
                    try:
                        size += os.path.getsize(os.path.join(dirpath, name))
                        files += 1
                    except OSError:
                        pass
        p = dict(self.prefetch_state)
        if p.get("started") and p.get("running"):
            p["elapsed"] = round(time.time() - p["started"], 1)
        return {
            "files": files,
            "bytes": size,
            "mb": round(size / (1024 * 1024), 1),
            "max_mb": round(self.max_bytes / (1024 * 1024), 1),
            "hits": self.hits,
            "misses": self.misses,
            "fetches": self.fetches,
            "online": self.online,
            "last_error": self.last_fetch_error,
            "prefetch": p,
            "upstream": self.upstream,
        }

    def clear(self):
        with self.lock:
            if os.path.isdir(self.root):
                shutil.rmtree(self.root, ignore_errors=True)
            os.makedirs(self.root, exist_ok=True)
            try:
                with self._conn() as conn:
                    conn.execute("DELETE FROM tiles")
            except Exception:
                pass
            self.hits = self.misses = self.fetches = 0
        return self.stats()

    def evict(self):
        total = 0
        rows = []
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                if not name.endswith(".png"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    total += os.path.getsize(path)
                    rows.append((os.path.getmtime(path), path))
                except OSError:
                    pass
        if total <= self.max_bytes:
            return 0
        rows.sort()
        removed = 0
        for _, path in rows:
            if total <= self.max_bytes:
                break
            try:
                size = os.path.getsize(path)
                os.unlink(path)
                total -= size
                removed += 1
            except OSError:
                pass
        try:
            with self.lock, self._conn() as conn:
                conn.execute("DELETE FROM tiles WHERE size=0")
        except Exception:
            pass
        return removed

    def set_upstream(self, url):
        parsed = urllib.parse.urlparse(url or "")
        if parsed.scheme not in ("http", "https") or "{z}" not in url or "{x}" not in url or "{y}" not in url:
            raise ValueError("瓦片地址必须包含 {z}/{x}/{y} 且为 http(s)")
        self.upstream = url
