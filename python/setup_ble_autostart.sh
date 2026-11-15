#!/bin/sh
# One-time setup, run ON EACH BOARD as the "arduino" user:
#   sh /home/arduino/ArduinoApps/hard-wear/python/setup_ble_autostart.sh
#
# The BLE radio (BlueZ) lives on the Linux host, outside the app's Docker container, so
# ble_host_daemon.py has to run on the host. This installs a cron job that starts it at boot,
# re-starts it within a minute if it stops, and restarts it when a new app version is flashed.

APP=/home/arduino/ArduinoApps/hard-wear
RUNNER=/home/arduino/hardwear-ble-run.sh

cat > "$RUNNER" << 'EOF'
#!/bin/sh
# Single-instance launcher (flock prevents @reboot and the per-minute job racing into 2 daemons).
D=/home/arduino/ArduinoApps/hard-wear/python/ble_host_daemon.py
LOG=/home/arduino/hardwear-ble.log
STATE=/home/arduino/.hardwear-ble.sum
[ -f "$D" ] || exit 0
exec 9>/tmp/hardwear-ble.lock
flock -n 9 || exit 0
SUM=$(md5sum "$D" | cut -d' ' -f1)
if pgrep -f "[b]le_host_daemon.py" > /dev/null 2>&1; then
  # Already running: keep it unless the daemon code changed (new flash).
  [ "$(cat "$STATE" 2>/dev/null)" = "$SUM" ] && exit 0
  pkill -f "[b]le_host_daemon.py"
  sleep 2
fi
echo "$SUM" > "$STATE"
nohup /usr/bin/python3 -u "$D" >> "$LOG" 2>&1 9>&- < /dev/null &
EOF
chmod +x "$RUNNER"

( crontab -l 2>/dev/null | grep -v hardwear-ble-run.sh
  echo "@reboot sleep 20 && $RUNNER"
  echo "* * * * * $RUNNER" ) | crontab -

# Clean slate now (also removes any duplicate daemons), then start exactly one.
for pid in $(pgrep -f "[b]le_host_daemon.py"); do kill "$pid" 2>/dev/null; done
sleep 2
rm -f "$APP/.cache/ble.sock" /home/arduino/.hardwear-ble.sum
"$RUNNER"
sleep 4
COUNT=$(pgrep -fc "[b]le_host_daemon.py")
if [ -S "$APP/.cache/ble.sock" ] && [ "$COUNT" = "1" ]; then
  echo "OK: BLE daemon running (1 instance), socket ready."
else
  echo "Problem: daemons=$COUNT - check /home/arduino/hardwear-ble.log"
fi
