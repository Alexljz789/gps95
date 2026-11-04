# GPS95 — Windows 95 styled GNSS navigator for the uConsole

GPS95 is a self-contained GPS/GNSS application for the ClockworkPi uConsole
with a HackerGadgets AIO v2 board and an Ai-Thinker **GP-02** (CASIC) GNSS
module.  It has a Windows 95 interface, a live Leaflet map with a server-side
offline tile cache, a satellite list/sky plot, accuracy circles, and a
hardware bridge that fixes the well-known GP-02 ↔ gpsd problem.

**中文简介：[README.zh-CN.md](README.zh-CN.md)**

> 隐私说明：本仓库不包含设备实际位置、实景截图或真实坐标。演示模式使用伦敦作为通用示例位置，仅用于地图初始化。
>
> **Privacy note:** this repository contains no real device location,
> location screenshot or real coordinates. The demo feed uses London as a
> generic starting point for map initialisation only.

## Why this exists

The GP-02 inside the AIO v2 is not a genuine u-blox receiver.  It understands
CASIC `$PCAS` commands and normally emits NMEA, but gpsd's u-blox
autoconfiguration can put it into a binary-only mode:

* `gpspipe -w` still shows a TPV fix, but `SKY` contains no satellites;
* `xgps`/`cgps` therefore show a position but no satellite list;
* stopping gpsd does not restore NMEA, so PyGPSClient sees only binary noise;
* PyGPSClient's "reset" works because it sends a reset/configuration sequence.

GPS95 solves this at the source instead of patching every client:

```
GP-02 UART ──► gps95-gpsbridge (root)
                 │  PCAS10,2 only on fault, then PCAS04,7
                 │  filters binary frames
                 ├── /run/gps95/nmea  (PTY, clean NMEA) ──► gpsd ──► xgps/cgps
                 └── 127.0.0.1:2958   (TCP, clean NMEA) ──► GPS95 direct mode
```

gpsd only sees NMEA, so its SKY message is complete.  GPS95's AUTO source
manager prefers gpsd; if gpsd is missing or unhealthy it reads the bridge TCP
stream (or the serial port directly as a last resort).  The bridge applies the
GP-02 profile again whenever it starts or detects stale data.

### Important GP-02 finding

Do **not** send `PCAS03,1,1,1,1,1,1,1,1,...` on this module.  Testing showed
that enabling several sentences with `PCAS03` suppresses `$GNGGA` (and with it
the altitude).  The safe recovery is `PCAS10,2` (factory NMEA profile, which
includes GGA/GST/GSV) followed by `PCAS04,7` for the constellation mode.  The
bridge therefore never sends `PCAS03` in its normal path; the constant is kept
only for manual experimentation and last-resort recovery.

## Features

* Windows 95 window chrome, menu bar, toolbar, status bar, dialogs and pixel
  font (`W95FA`), based on `98.css`.
* Left: Leaflet map with live position, heading cone, breadcrumb trail,
  accuracy circles (1σ and 95%), scale/attribution and follow mode.
* Right top: satellite list with constellation, PRN, elevation, azimuth, SNR
  and used flag; plus sky plot and signal bar chart tabs.
* Right bottom: large lat/lon readout, speed, altitude, course, HDOP, accuracy,
  source status and quick actions.
* Dual-mode GPS: gpsd JSON → NMEA bridge → direct serial, with automatic
  failover and repair.
* GPS + BeiDou + GLONASS simultaneously (`PCAS04,7`) — the maximum the GP-02
  supports.  Galileo/QZSS are recognized if a future receiver provides them.
* Server-side tile cache with explicit prefetch, offline mode, LRU eviction,
  stats, and a "NO MAP DATA" fallback tile.
* GPX track export, NMEA monitor, diagnostics, log viewer, night theme,
  keyboard shortcuts and a kiosk launcher.

## Layout

```
+--------------------------------------------------------------+------------------+
| title bar                                                    |                  |
+--------------------------------------------------------------+   satellites     |
| menu / toolbar                                               |   list / sky     |
+--------------------------------------------------------------+                  |
|                                                              |                  |
|                         MAP                                  +------------------+
|              position + accuracy circles                     |   navigation     |
|                                                              |   data + buttons |
+--------------------------------------------------------------+------------------+
| status bar: source | fix | sats | cache | message | clock                       |
+-------------------------------------------------------------------------------+
```

The uConsole panel is 1280×720 after the LightDM rotation script.

## Install

On the uConsole:

```bash
sudo apt update
sudo apt install -y python3 python3-serial python3-flask curl
# optional, already present on most images:
sudo apt install -y gpsd gpsd-clients chromium
```

Copy this directory to the device (or clone it), then:

```bash
sudo bash scripts/install.sh
```

The installer:

1. installs the app to `/opt/gps95`;
2. installs `gps95-gpsbridge.service` (root) and `gps95-web.service` (user
   `alex`);
3. backs up `/etc/default/gpsd` and points gpsd at `/run/gps95/nmea` with
   `-n -b` (read-only, NMEA-only);
4. installs a narrow sudoers rule for `/usr/local/sbin/gps95-ctl`;
5. creates `/var/lib/gps95` (config + tile cache);
6. adds a **GPS95** desktop icon and application-menu entry.

Launch it from the desktop icon, or:

```bash
/usr/local/bin/gps95-ui
```

The kiosk URL is <http://127.0.0.1:8765>.

### Uninstall

```bash
sudo bash scripts/uninstall.sh
```

This restores the original `/etc/default/gpsd` and removes the services,
desktop entry and sudoers rule.  Cached tiles and `/opt/gps95` are kept.

## Usage

| Action | How |
|---|---|
| Follow / center | `C`, toolbar **跟随**, map **◎** |
| Toggle trail | `T` |
| Toggle accuracy circles | `A` |
| Repair GPS | `R` or **修复 GPS** |
| Diagnostics | `D` |
| Settings | `S` |
| Help | `F1` |
| Zoom | `+` / `-` |
| Exit | `Alt+F4` or title-bar close |

### Offline map cache

Every tile requested by the map is fetched through the backend and stored in
`/var/lib/gps95/tiles`.  Viewing an area online is enough to cache it.  For a
larger area:

1. Pan/zoom to the area.
2. **工具 → 缓存当前地图区域**.
3. Choose the zoom range and tile limit (default 450).
4. Wait for the progress bar; then enable **离线地图**.

When offline, missing tiles show a Windows 95-style "NO MAP DATA" tile.  The
cache is capped at 512 MB by default and evicts the oldest tiles first.

> The default is Esri World Street Map, which requires attribution but no API
> key.  The prefetch limit and 40 ms delay are deliberate.  For large offline
> regions, point the tile URL at your own tile server or an MBTiles-backed
> service.  The map URL is configurable in Settings.

## Configuration

`/var/lib/gps95/config.json` (edited by the Settings dialog):

```json
{
  "source_mode": "auto",
  "gnss_mode": 7,
  "units": "metric",
  "coord_format": "dd",
  "theme": "classic",
  "follow": true,
  "trail": true,
  "accuracy": true,
  "heading_cone": true,
  "auto_repair": true,
  "map": {
    "upstream": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    "offline": false,
    "max_cache_mb": 512
  }
}
```

`gnss_mode` is a CASIC `PCAS04` mode: 1 GPS, 2 BDS, 3 GPS+BDS, 4 GLONASS,
5 GPS+GLONASS, 6 BDS+GLONASS, 7 GPS+BDS+GLONASS.  Mode 7 is the default.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/state` | Full JSON snapshot |
| `GET /api/stream` | SSE stream (1 Hz) |
| `GET/POST /api/config` | Read/update settings |
| `POST /api/source` | `{"mode":"auto|gpsd|serial"}` |
| `POST /api/repair` | Restore PCAS profile + restart gpsd |
| `POST /api/gnss_mode` | `{"mode":7}` |
| `GET /tiles/{z}/{x}/{y}.png?offline=1` | Cached raster tile |
| `POST /api/cache/prefetch` | `{south,west,north,east,minzoom,maxzoom}` |
| `GET /api/cache/stats` | Cache statistics |
| `GET /api/hardware` | gpsd/bridge/system diagnostics |
| `GET /api/log` | Bridge log tail |
| `GET /api/track.gpx` | Export breadcrumb track |
| `GET /api/nmea` | Last NMEA sentence and counters |

## Hardware notes

* GPS UART: `/dev/serial0` → `ttyS0`, 9600 8N1 (AIO v2 GPS rail GPIO 27).
* `PCAS10,2` restores the factory NMEA profile (GGA, GLL, GSA, GST, GSV, RMC,
  VTG, ZDA, TXT).  It is used only when NMEA is missing or malformed.
* `PCAS04,7` enables GPS + BeiDou + GLONASS.  The module outputs `$GPGSV`,
  `$BDGSV` and `$GLGSV`, which gpsd merges into one SKY list.
* Do not use `PCAS03` to enable multiple sentences: on this firmware it
  suppresses GGA.  It is kept only as a last-resort fallback.
* A cold restart can take minutes to reacquire; do not reset repeatedly.
* The receiver also emits UBX-looking binary frames; the bridge filters them.
  This is why gpsd must never read the raw UART directly.
* The AIO v2 GPS rail is GPIO 27.  `gps95-ctl powercycle-gps` is available as
  a last resort; the web UI uses it only when explicitly requested.

## Troubleshooting

```bash
/usr/local/sbin/gps95-ctl status
systemctl status gps95-gpsbridge gpsd gps95-web
journalctl -u gps95-gpsbridge -n 100 --no-pager
gpspipe -w -n 20 | grep -E 'SKY|TPV'
```

* **No SKY in gpspipe**: the bridge is not running or gpsd is still reading
  the raw serial port.  Check `/etc/default/gpsd` points at `/run/gps95/nmea`.
* **No NMEA from bridge**: check `status.json` / bridge log; use **修复 GPS**.
* **Map blank but position works**: tile upstream blocked; check the cache
  dialog's network state and `last_error`.
* **No fix**: antenna view of sky; first fix after cold start can take minutes.

## Project layout

```
app/
  bridge.py       serial owner, PCAS configuration, PTY/TCP/control server
  nmea.py         NMEA 0183 parser
  gpsd_client.py  gpsd JSON client
  source.py       AUTO/GPSD/SERIAL source manager
  model.py        shared state, satellites, track, accuracy
  tiles.py        tile cache, prefetch, LRU, stats
  web.py          Flask API, SSE, tile endpoint, GPX export
  main.py         entry point
  static/         HTML/CSS/JS, Leaflet, 98.css, W95FA, icons
scripts/          install/uninstall, gps95-ctl, kiosk launcher
systemd/          bridge/web units and gpsd drop-in
```

## Licenses and credits

GPS95 is released under the MIT license.

* [98.css](https://github.com/jdan/98.css) — MIT.
* [Leaflet](https://leafletjs.com/) — BSD-2-Clause.
* [W95FA](https://github.com/fontsource/fontsource) — SIL OFL 1.1.
* [gpsd](https://gpsd.gitlab.io/gpsd/) — BSD-2-Clause.
* Map data © Esri, HERE, Garmin, OSM contributors; Esri attribution terms apply.
* GP-02 / CASIC PCAS protocol reference: CASIC NMEA specification
  (V4.2.0.3), Ai-Thinker GP-02 datasheet.
* Bug analysis informed by the clockworkpi forum threads on the AIO V1/V2
  GPS, gpsd SKY and the GP-02 PCAS commands.
