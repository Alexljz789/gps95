#!/bin/bash
# GPS95 installer for the ClockworkPi uConsole (Debian 13 / XFCE).
# Run as root:  sudo bash scripts/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/opt/gps95"
DATA="/var/lib/gps95"
LOGS="/var/log/gps95"
RUN="/run/gps95"
USER_NAME="alex"

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must be run as root" >&2
  exit 1
fi

echo "== GPS95 installer =="
echo "source: $SRC"
echo "dest:   $DEST"

# Stop old instances before touching the serial device.
systemctl stop gps95-web.service 2>/dev/null || true
systemctl stop gps95-gpsbridge.service 2>/dev/null || true
systemctl stop gpsd.socket gpsd.service 2>/dev/null || true

# Back up the distribution gpsd defaults exactly once.
if [ -f /etc/default/gpsd ] && [ ! -f /etc/default/gpsd.gps95.bak ]; then
  cp -a /etc/default/gpsd /etc/default/gpsd.gps95.bak
fi

install -d -m 755 "$DEST" "$DEST/app" "$DEST/app/static" "$DEST/scripts"
cp -a "$SRC/app/." "$DEST/app/"
cp -a "$SRC/scripts/." "$DEST/scripts/"
cp -a "$SRC/README.md" "$DEST/README.md" 2>/dev/null || true
cp -a "$SRC/README.zh-CN.md" "$DEST/README.zh-CN.md" 2>/dev/null || true
find "$DEST" -type d -exec chmod 755 {} \;
find "$DEST" -type f -exec chmod 644 {} \;
chmod 755 "$DEST/scripts/gps95-ctl" "$DEST/scripts/wait-bridge.sh" "$DEST/scripts/gps95-ui"

install -d -o "$USER_NAME" -g "$USER_NAME" -m 775 "$DATA" "$DATA/tiles" "$LOGS"
install -d -m 755 "$RUN" /etc/gps95 /usr/local/lib/gps95

install -m 755 "$SRC/scripts/gps95-ctl" /usr/local/sbin/gps95-ctl
install -m 755 "$SRC/scripts/wait-bridge.sh" /usr/local/lib/gps95/wait-bridge.sh
install -m 755 "$SRC/scripts/gps95-ui" /usr/local/bin/gps95-ui

# Narrow sudoers rule: the web app may only call the GPS95 control helper.
cat > /etc/sudoers.d/gps95 <<'EOF'
alex ALL=(root) NOPASSWD: /usr/local/sbin/gps95-ctl
EOF
chmod 440 /etc/sudoers.d/gps95
visudo -cf /etc/sudoers.d/gps95 >/dev/null

# gpsd reads the clean NMEA PTY; the bridge does all hardware configuration.
cat > /etc/default/gpsd <<'EOF'
# Managed by GPS95.  Original file: /etc/default/gpsd.gps95.bak
# The GP-02 is driven by gps95-gpsbridge, which exposes NMEA on this PTY.
DEVICES="/run/gps95/nmea"
GPSD_OPTIONS="-n -b"
USBAUTO="false"
EOF

# Disable Chromium's translate bubble in kiosk mode.
install -d -m 755 /etc/chromium/policies/managed /etc/opt/chrome/policies/managed 2>/dev/null || true
printf '{
  "TranslateEnabled": false
}
' > /etc/chromium/policies/managed/gps95.json
printf '{
  "TranslateEnabled": false
}
' > /etc/opt/chrome/policies/managed/gps95.json 2>/dev/null || true

install -d -m 755 /etc/systemd/system/gpsd.service.d
install -m 644 "$SRC/systemd/gpsd-gps95.conf" /etc/systemd/system/gpsd.service.d/gps95.conf
install -m 644 "$SRC/systemd/gps95-gpsbridge.service" /etc/systemd/system/gps95-gpsbridge.service
install -m 644 "$SRC/systemd/gps95-web.service" /etc/systemd/system/gps95-web.service

# Bridge default mode: GPS + BeiDou + GLONASS (mode 7).
if [ ! -f /etc/gps95/bridge.json ]; then
  printf '{\n  "gnss_mode": 7\n}\n' > /etc/gps95/bridge.json
fi
chmod 644 /etc/gps95/bridge.json

# Default web config (only created if absent).
if [ ! -f "$DATA/config.json" ]; then
  cat > "$DATA/config.json" <<'EOF'
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
  "sound": false,
  "auto_repair": true,
  "map": {
    "upstream": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    "offline": false,
    "max_cache_mb": 512
  },
  "ui": {"sat_tab": "list", "show_unused": true}
}
EOF
  chown "$USER_NAME:$USER_NAME" "$DATA/config.json"
fi

# Desktop launcher and application menu entry.
install -d -o "$USER_NAME" -g "$USER_NAME" -m 755 "/home/$USER_NAME/Desktop"
install -m 755 -o "$USER_NAME" -g "$USER_NAME" "$SRC/scripts/gps95.desktop" "/home/$USER_NAME/Desktop/GPS95.desktop"
install -m 644 "$SRC/scripts/gps95.desktop" /usr/share/applications/gps95.desktop

systemctl daemon-reload
systemctl enable gps95-gpsbridge.service gps95-web.service
systemctl reset-failed gps95-gpsbridge.service gps95-web.service gpsd.service 2>/dev/null || true

echo "-- starting bridge"
systemctl start gps95-gpsbridge.service
for _ in $(seq 1 60); do
  [ -e "$RUN/nmea" ] && break
  sleep 0.2
done
ls -l "$RUN/nmea" || true

echo "-- starting gpsd"
systemctl start gpsd.socket gpsd.service
sleep 3
systemctl --no-pager --lines=3 status gpsd.service || true

echo "-- starting web"
systemctl start gps95-web.service
for _ in $(seq 1 40); do
  if curl -sf --max-time 1 http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

echo
echo "GPS95 installed."
echo "  web:    http://127.0.0.1:8765"
echo "  launch: /usr/local/bin/gps95-ui  (desktop icon GPS95)"
echo "  status: /usr/local/sbin/gps95-ctl status"
echo "  logs:   journalctl -u gps95-gpsbridge -u gpsd -u gps95-web"
