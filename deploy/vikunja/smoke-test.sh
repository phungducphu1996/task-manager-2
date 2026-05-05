#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://hazeleo.com}"
TOKEN="${NOTIFY_INTERNAL_TOKEN:-${1:-}}"

if [[ -z "${TOKEN}" ]]; then
  echo "Missing token. Usage: NOTIFY_INTERNAL_TOKEN=... bash deploy/vikunja/smoke-test.sh" >&2
  exit 1
fi

need_jq() {
  if ! command -v jq >/dev/null 2>&1; then
    cat
  else
    jq
  fi
}

echo "== Vikunja UI =="
curl -sI "${BASE_URL}/vikunja/" | sed -n '1,8p'

echo "\n== Legacy Task app still alive =="
curl -sI "${BASE_URL}/task/" | sed -n '1,8p'

echo "\n== Hazel Bridge health =="
curl -s "${BASE_URL}/task-api/health" | need_jq

echo "\n== Vikunja bridge status =="
curl -s "${BASE_URL}/task-api/internal/vikunja/status" \
  -H "X-Internal-Token: ${TOKEN}" | need_jq

echo "\n== Sync users =="
curl -s -X POST "${BASE_URL}/task-api/internal/vikunja/sync-users" \
  -H "X-Internal-Token: ${TOKEN}" | need_jq

echo "\n== Migration dry run, first 10 tasks =="
curl -s -X POST "${BASE_URL}/task-api/internal/vikunja/migrate-tasks?dry_run=true&limit=10" \
  -H "X-Internal-Token: ${TOKEN}" | need_jq
