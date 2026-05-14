#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/task-manager}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

SERVICE_NAMES=(
  "taskmanager-gmail-poll.service"
  "taskmanager-gmail-digest.service"
)
TIMER_NAMES=(
  "taskmanager-gmail-poll.timer"
  "taskmanager-gmail-digest.timer"
)
RUNNER_NAMES=(
  "run-gmail-poll.sh"
  "run-gmail-digest.sh"
)

if [ ! -d "$APP_DIR" ]; then
  echo "App dir not found: $APP_DIR" >&2
  exit 1
fi

for runner in "${RUNNER_NAMES[@]}"; do
  sudo chmod 0755 "$APP_DIR/deploy/systemd/$runner"
done

for service in "${SERVICE_NAMES[@]}"; do
  sudo install -m 0644 "$APP_DIR/deploy/systemd/$service" "$SYSTEMD_DIR/$service"
done

for timer in "${TIMER_NAMES[@]}"; do
  sudo install -m 0644 "$APP_DIR/deploy/systemd/$timer" "$SYSTEMD_DIR/$timer"
done

sudo systemctl daemon-reload
for timer in "${TIMER_NAMES[@]}"; do
  sudo systemctl enable --now "$timer"
done

echo
echo "== Gmail timer status =="
for timer in "${TIMER_NAMES[@]}"; do
  sudo systemctl status "$timer" --no-pager
done

echo
echo "== Last service logs =="
for service in "${SERVICE_NAMES[@]}"; do
  sudo journalctl -u "$service" -n 20 --no-pager
done

echo
echo "Remove old crontab entries for /internal/gmail/poll or /internal/gmail/digest if any exist."
