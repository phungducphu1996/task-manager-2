#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/task-manager}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SERVICE_NAME="taskmanager-reminder-tick.service"
TIMER_NAME="taskmanager-reminder-tick.timer"

if [ ! -d "$APP_DIR" ]; then
  echo "App dir not found: $APP_DIR" >&2
  exit 1
fi

sudo chmod 0755 "$APP_DIR/deploy/systemd/run-reminder-tick.sh"
sudo install -m 0644 "$APP_DIR/deploy/systemd/$SERVICE_NAME" "$SYSTEMD_DIR/$SERVICE_NAME"
sudo install -m 0644 "$APP_DIR/deploy/systemd/$TIMER_NAME" "$SYSTEMD_DIR/$TIMER_NAME"

sudo systemctl daemon-reload
sudo systemctl enable --now "$TIMER_NAME"

echo
echo "== Timer status =="
sudo systemctl status "$TIMER_NAME" --no-pager
echo
echo "== Last service logs =="
sudo journalctl -u "$SERVICE_NAME" -n 20 --no-pager
echo
echo "If you still have an old crontab entry for /internal/reminders/tick, remove it to avoid duplicate sends."
