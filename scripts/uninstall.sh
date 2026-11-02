#!/bin/bash
# Remove GPS95 and restore the distribution gpsd configuration.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "uninstall.sh must be run as root" >&2
  exit 1
fi

systemctl stop gps95-web.service gps95-gpsbridge.service gpsd.socket gpsd.service 2>/dev/null || true
systemctl disable gps95-web.service gps95-gpsbridge.service 2>/dev/null || true

rm -f /etc/systemd/system/gps95-gpsbridge.service
rm -f /etc/systemd/system/gps95-web.service
rm -f /etc/systemd/system/gpsd.service.d/gps95.conf
rmdir /etc/systemd/system/gpsd.service.d 2>/dev/null || true
rm -f /usr/local/sbin/gps95-ctl
rm -f /usr/local/bin/gps95-ui
rm -rf /usr/local/lib/gps95
rm -f /etc/sudoers.d/gps95
rm -f /usr/share/applications/gps95.desktop
rm -f /etc/chromium/policies/managed/gps95.json
rm -f /etc/opt/chrome/policies/managed/gps95.json
rm -f /home/alex/Desktop/GPS95.desktop

if [ -f /etc/default/gpsd.gps95.bak ]; then
  mv /etc/default/gpsd.gps95.bak /etc/default/gpsd
fi

systemctl daemon-reload
systemctl reset-failed gpsd.service 2>/dev/null || true
systemctl start gpsd.socket gpsd.service 2>/dev/null || true

echo "GPS95 removed. The source tree in /opt/gps95 and cached tiles in /var/lib/gps95 were kept."
echo "Remove them manually if desired: rm -rf /opt/gps95 /var/lib/gps95 /var/log/gps95"
