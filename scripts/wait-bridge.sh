#!/bin/bash
# Wait until the GPS95 NMEA PTY exists, then let gpsd start.
for _ in $(seq 1 100); do
  if [ -e /run/gps95/nmea ]; then
    exit 0
  fi
  sleep 0.2
done
echo "gps95: NMEA bridge PTY did not appear" >&2
exit 1
