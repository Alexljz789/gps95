"""Flask web application, JSON API, SSE stream and tile endpoint."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from flask import Flask, Response, jsonify, request, send_from_directory

from . import hw
from .model import GPSState
from .source import SourceManager
from .tiles import TileCache

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
CONFIG_PATH = os.environ.get("GPS95_CONFIG", "/var/lib/gps95/config.json")

DEFAULT_CONFIG = {
    "source_mode": "auto",
    "gnss_mode": 7,
    "units": "metric",
    "coord_format": "dd",
    "theme": "classic",
    "follow": True,
    "trail": True,
    "accuracy": True,
    "heading_cone": True,
    "sound": False,
    "auto_repair": True,
    "map": {
        "upstream": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        "offline": False,
        "max_cache_mb": 512,
        "zoom": 15,
    },
    "ui": {
        "sat_tab": "list",
        "show_unused": True,
    },
}


def _deep_merge(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = _deep_merge(cfg, json.load(fh))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


class AppContext:
    def __init__(self):
        self.state = GPSState()
        self.config = load_config()
        self.cache = TileCache(
            upstream=self.config["map"].get("upstream"),
            max_mb=int(self.config["map"].get("max_cache_mb", 512)),
        )
        self.source = SourceManager(self.state, self.config)
        self.started = time.time()
        self.demo = False
        self.cache_stats = {}
        self.cache_stats_ts = 0.0
        self.system_stats = {}
        self.system_stats_ts = 0.0
        self.bridge_stats = {}
        self.bridge_stats_ts = 0.0
        self._lock = threading.Lock()

    def start(self):
        self.source.start()

    def cfg(self):
        with self._lock:
            return json.loads(json.dumps(self.config))

    def update_config(self, patch):
        with self._lock:
            self.config = _deep_merge(self.config, patch)
            cfg = json.loads(json.dumps(self.config))
        save_config(cfg)
        if "gnss_mode" in (patch or {}) and not self.demo:
            try:
                hw.set_gnss_mode(cfg["gnss_mode"])
            except Exception:
                pass
        if "map" in (patch or {}):
            if "upstream" in patch["map"]:
                try:
                    self.cache.set_upstream(patch["map"]["upstream"])
                except Exception:
                    pass
            if "max_cache_mb" in patch["map"]:
                self.cache.max_bytes = int(patch["map"]["max_cache_mb"]) * 1024 * 1024
        if "source_mode" in (patch or {}) and not self.demo:
            threading.Thread(target=self.source.set_mode,
                             args=(cfg["source_mode"],), daemon=True).start()
        return cfg

    def refresh_stats(self):
        now = time.time()
        if now - self.cache_stats_ts > 5:
            self.cache_stats = self.cache.stats()
            self.cache_stats_ts = now
        if now - self.system_stats_ts > 10:
            self.system_stats = hw.system_info()
            self.system_stats_ts = now
        if now - self.bridge_stats_ts > 3:
            self.bridge_stats = hw.bridge_status()
            self.bridge_stats_ts = now

    def snapshot(self):
        self.refresh_stats()
        gpsd = self.source.gpsd.status() if self.source.gpsd else {
            "connected": False, "error": self.source.last_error,
        }
        bridge = dict(self.bridge_stats)
        bridge["service"] = hw.bridge_service_state()
        return self.state.snapshot(
            cache=self.cache_stats,
            bridge=bridge,
            gpsd=gpsd,
            system=self.system_stats,
            config=self.cfg(),
        )


def create_app(ctx: AppContext):
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.after_request
    def add_headers(resp):
        if request.path.startswith("/api/") or request.path.startswith("/tiles/"):
            resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-GPS95"] = "1.0.0"
        return resp

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(os.path.join(STATIC_DIR, "img"), "gps95.png")

    @app.route("/api/health")
    def health():
        return jsonify({"ok": True, "uptime": round(time.time() - ctx.started, 1),
                        "source": ctx.source.active_name})

    @app.route("/api/state")
    def api_state():
        return jsonify(ctx.snapshot())

    @app.route("/api/stream")
    def api_stream():
        def generate():
            last = 0
            while True:
                snap = ctx.snapshot()
                payload = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
                yield f"event: state\ndata: {payload}\n\n"
                last = time.time()
                time.sleep(1.0)
        return Response(generate(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if request.method == "GET":
            return jsonify(ctx.cfg())
        patch = request.get_json(silent=True) or {}
        if not isinstance(patch, dict):
            return jsonify({"ok": False, "error": "JSON object required"}), 400
        try:
            cfg = ctx.update_config(patch)
            return jsonify({"ok": True, "config": cfg})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.route("/api/source", methods=["POST"])
    def api_source():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode")
        if mode not in ("auto", "gpsd", "serial"):
            return jsonify({"ok": False, "error": "mode must be auto, gpsd or serial"}), 400
        ctx.config["source_mode"] = mode
        save_config(ctx.cfg())
        if not ctx.demo:
            threading.Thread(target=ctx.source.set_mode, args=(mode,), daemon=True).start()
        return jsonify({"ok": True, "mode": mode, "message": "正在切换数据源"})

    @app.route("/api/repair", methods=["POST"])
    def api_repair():
        if ctx.demo:
            return jsonify({"ok": True, "demo": True, "message": "演示模式不会触碰真实 GPS 硬件"})
        body = request.get_json(silent=True) or {}
        if body.get("powercycle"):
            result = hw.powercycle()
        else:
            result = hw.repair()
        ctx.state.log("已执行 GPS 修复序列", "info")
        return jsonify({"ok": True, "result": result, "message": "GPS 修复序列已发送"})

    @app.route("/api/gnss_mode", methods=["POST"])
    def api_gnss_mode():
        body = request.get_json(silent=True) or {}
        try:
            mode = int(body.get("mode", 7))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid mode"}), 400
        if mode not in (1, 2, 3, 4, 5, 6, 7):
            return jsonify({"ok": False, "error": "mode must be 1..7"}), 400
        ctx.config["gnss_mode"] = mode
        save_config(ctx.cfg())
        result = {"ok": True, "demo": True} if ctx.demo else hw.set_gnss_mode(mode)
        names = {1: "GPS", 2: "BDS", 3: "GPS+BDS", 4: "GLONASS",
                 5: "GPS+GLONASS", 6: "BDS+GLONASS", 7: "GPS+BDS+GLONASS"}
        return jsonify({"ok": True, "mode": mode, "name": names[mode], "result": result})

    @app.route("/api/hardware")
    def api_hardware():
        return jsonify({
            "system": hw.system_info(),
            "source": ctx.source.status(),
            "bridge": hw.bridge_status(),
            "cache": ctx.cache.stats(),
        })

    @app.route("/api/log")
    def api_log():
        try:
            lines = int(request.args.get("lines", 200))
        except ValueError:
            lines = 200
        text = ""
        path = "/var/log/gps95/bridge.log"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = "".join(fh.readlines()[-lines:])
        except Exception:
            text = "\n".join(f"{m['time']} [{m['level']}] {m['text']}" for m in ctx.state.messages)
        return jsonify({"ok": True, "text": text, "lines": lines})

    @app.route("/api/cache/stats")
    def api_cache_stats():
        return jsonify(ctx.cache.stats())

    @app.route("/api/cache/clear", methods=["POST"])
    def api_cache_clear():
        return jsonify({"ok": True, "stats": ctx.cache.clear()})

    @app.route("/api/cache/prefetch", methods=["POST"])
    def api_cache_prefetch():
        body = request.get_json(silent=True) or {}
        try:
            south = float(body["south"])
            west = float(body["west"])
            north = float(body["north"])
            east = float(body["east"])
            minz = int(body.get("minzoom", 13))
            maxz = int(body.get("maxzoom", 16))
            max_tiles = int(body.get("max_tiles", 450))
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "bounds/minzoom/maxzoom required"}), 400
        ok, message = ctx.cache.prefetch(south, west, north, east, minz, maxz, max_tiles)
        return jsonify({"ok": ok, "message": message,
                        "prefetch": ctx.cache.prefetch_state}), (200 if ok else 400)

    @app.route("/api/cache/cancel", methods=["POST"])
    def api_cache_cancel():
        ctx.cache.cancel_prefetch()
        return jsonify({"ok": True})

    @app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
    def tile(z, x, y):
        offline = request.args.get("offline", "0") in ("1", "true", "yes")
        data, mime, state = ctx.cache.get_tile(z, x, y, offline=offline)
        if data is None:
            return ("", 404)
        resp = Response(data, mimetype=mime)
        resp.headers["X-GPS95-Tile"] = state
        resp.headers["Cache-Control"] = "public, max-age=86400" if state == "hit" else "no-store"
        return resp

    @app.route("/api/track.gpx")
    def track_gpx():
        snap = ctx.state.snapshot()
        points = snap.get("track") or []
        name = datetime.now().strftime("GPS95 %Y-%m-%d %H:%M")
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="GPS95 1.0" xmlns="http://www.topografix.com/GPX/1/1">',
            f"<metadata><name>{escape(name)}</name></metadata>",
            "<trk><name>GPS95 track</name><trkseg>",
        ]
        for lat, lon, ts in points:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            parts.append(f'<trkpt lat="{lat}" lon="{lon}"><time>{dt}</time></trkpt>')
        parts.append("</trkseg></trk></gpx>")
        return Response("\n".join(parts), mimetype="application/gpx+xml", headers={
            "Content-Disposition": 'attachment; filename="gps95-track.gpx"',
        })

    @app.route("/api/nmea")
    def api_nmea():
        return jsonify({
            "ok": True,
            "last": ctx.state.last_raw(),
            "sentences": ctx.state.raw_count,
            "bad_checksums": ctx.state.bad_checksums,
            "source": ctx.source.active_name,
        })

    @app.errorhandler(404)
    def not_found(_):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not found"}), 404
        return "Not found", 404

    return app
