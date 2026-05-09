#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/task-manager}"
ENV_FILE="${ENV_FILE:-$APP_DIR/backend/.env}"
API_URL="${REMINDER_TICK_API_URL:-http://127.0.0.1:8010/internal/reminders/tick}"

if [ ! -f "$ENV_FILE" ]; then
  echo "ENV file not found: $ENV_FILE" >&2
  exit 1
fi

TOKEN="$(grep '^NOTIFY_INTERNAL_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
if [ -z "${TOKEN:-}" ]; then
  echo "NOTIFY_INTERNAL_TOKEN is missing in $ENV_FILE" >&2
  exit 1
fi

curl -fsS -X POST "$API_URL" \
  -H "X-Internal-Token: $TOKEN" \
  -H "Content-Type: application/json"
